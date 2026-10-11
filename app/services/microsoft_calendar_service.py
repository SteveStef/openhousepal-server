"""Low-level Microsoft identity (Entra ID) OAuth + Microsoft Graph Calendar client (httpx, no
Microsoft SDK) - the Outlook/Microsoft 365 counterpart to google_calendar_service.py.

Scopes we use:
  - Calendars.ReadWrite : create/manage the ONE "OpenHousePal" calendar we create in the agent's
                          account. We never touch their other calendars.
  - offline_access      : issues a refresh token, same purpose as Google's access_type=offline.
  - User.Read           : just enough to read their email for display in Settings.

Tenant is "common" by default (MS_OAUTH_TENANT_ID) so both work/school (Microsoft 365) and
personal (outlook.com/hotmail) accounts can connect - real estate agents show up on both.

calendar_sync_service.py builds one neutral, Google-Calendar-shaped event body per entity (task /
open house / tour) and hands it to whichever provider client(s) the agent has connected. Rather
than teaching the orchestration layer two body shapes, insert_event/patch_event here translate
that same neutral body into Microsoft Graph's event shape internally - so the higher-level sync
logic (what to push, when) stays provider-agnostic.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

_GRAPH = "https://graph.microsoft.com/v1.0"

SCOPES = [
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/User.Read",
    "offline_access",
    "openid",
    "email",
]

_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class MicrosoftCalendarError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.getenv("MS_OAUTH_CLIENT_ID") and os.getenv("MS_OAUTH_CLIENT_SECRET"))


def _client_id() -> str:
    return os.getenv("MS_OAUTH_CLIENT_ID", "")


def _client_secret() -> str:
    return os.getenv("MS_OAUTH_CLIENT_SECRET", "")


def _tenant() -> str:
    return os.getenv("MS_OAUTH_TENANT_ID", "common")


def _auth_base() -> str:
    return f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0"


def redirect_uri() -> str:
    return os.getenv("MS_OAUTH_REDIRECT_URI") or (
        (os.getenv("API_URL") or ("http://localhost:8081" if os.getenv("DEV", "false") == "true" else "https://api.openhousepal.com")).rstrip("/") + "/integrations/microsoft/callback"
    )


def build_auth_url(state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{_auth_base()}/authorize?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Auth code -> {access_token, refresh_token, expiry(datetime), scope}."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_auth_base()}/token", data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
            "scope": " ".join(SCOPES),
        })
    if r.status_code >= 300:
        raise MicrosoftCalendarError(f"token exchange failed: {r.status_code} {r.text[:300]}")
    j = r.json()
    return {
        "access_token": j["access_token"],
        "refresh_token": j.get("refresh_token"),
        "expiry": datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600))),
        "scope": j.get("scope", ""),
    }


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_auth_base()}/token", data={
            "refresh_token": refresh_token,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
            "scope": " ".join(SCOPES),
        })
    if r.status_code >= 300:
        raise MicrosoftCalendarError(f"token refresh failed: {r.status_code} {r.text[:300]}")
    j = r.json()
    return {
        "access_token": j["access_token"],
        # Microsoft may or may not rotate the refresh token on each use - keep the new one if given.
        "refresh_token": j.get("refresh_token"),
        "expiry": datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600))),
    }


async def revoke(token: str) -> None:
    # Microsoft's public Graph/identity APIs don't expose a per-refresh-token revoke endpoint the
    # way Google does (revokeSignInSessions invalidates ALL of the user's sessions everywhere,
    # which is far more than we want for a single-app disconnect). Best-effort no-op: disconnect()
    # simply stops using and deletes our stored token: the agent can revoke OpenHousePal's access
    # entirely from myaccount.microsoft.com/organizations if they want to.
    return None


async def get_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_GRAPH}/me", headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code >= 300:
        return {}
    j = r.json()
    return {"email": j.get("mail") or j.get("userPrincipalName")}


# --------------------------------------------------------------------------- Calendar API

async def _req(access_token: str, method: str, path: str, *, json=None, params=None) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.request(
            method, f"{_GRAPH}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=json, params=params,
        )
    if r.status_code == 204:
        return {}
    if r.status_code >= 300:
        raise MicrosoftCalendarError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


async def create_app_calendar(access_token: str, summary: str = "OpenHousePal",
                              description: str = "Your OpenHousePal follow-ups and open houses.") -> str:
    j = await _req(access_token, "POST", "/me/calendars", json={"name": summary})
    return j["id"]


async def style_app_calendar(access_token: str, calendar_id: str) -> bool:
    """Give the OpenHousePal calendar a gold-ish colour in Outlook (Graph only offers a fixed
    palette - lightYellow is the closest). New Outlook calendars are shown by default. Best-effort,
    never raises; only touches the calendar, never events."""
    try:
        await _req(access_token, "PATCH", f"/me/calendars/{calendar_id}", json={"color": "lightYellow"})
        return True
    except Exception:  # noqa: BLE001
        return False


async def get_calendar(access_token: str, calendar_id: str) -> Optional[dict]:
    try:
        return await _req(access_token, "GET", f"/me/calendars/{calendar_id}")
    except MicrosoftCalendarError:
        return None


async def delete_calendar(access_token: str, calendar_id: str) -> None:
    try:
        await _req(access_token, "DELETE", f"/me/calendars/{calendar_id}")
    except MicrosoftCalendarError:
        pass


_GRAPH_TZ = "Eastern Standard Time"  # Windows zone id = America/New_York (DST handled by Outlook)


def _graph_local(iso: str) -> str:
    """Any ISO timestamp -> naive New York wall-clock string, as Graph wants with timeZone."""
    from zoneinfo import ZoneInfo
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S")


async def set_calendar_timezone(access_token: str, calendar_id: str, tz: str) -> bool:
    """Outlook calendars have no zone of their own - events carry it (see _to_graph_event)."""
    return True


def _to_graph_event(body: dict) -> dict:
    """Translate the neutral, Google-Calendar-shaped body (see calendar_sync_service.py's
    _task_event_body/_oh_event_body/_tour_event_body) into a Microsoft Graph event payload."""
    out: dict = {"subject": body.get("summary", "OpenHousePal")}

    desc_parts = []
    if body.get("description"):
        desc_parts.append(body["description"])
    src = body.get("source")
    if src and src.get("url"):
        desc_parts.append(src["url"])
    if desc_parts:
        out["body"] = {"contentType": "text", "content": "\n\n".join(desc_parts)}
    if body.get("transparency") == "transparent":
        out["showAs"] = "free"  # never block the agent's availability

    if body.get("location"):
        out["location"] = {"displayName": body["location"]}

    # Eastern wall-clock time with Outlook's (Windows) name for New York's zone - UTC made the
    # events read as GMT, and all-day events land on midnight of the right day only in local time.
    start, end = body.get("start"), body.get("end")
    if start and "date" in start:  # Google's all-day marker
        out["isAllDay"] = True
        out["start"] = {"dateTime": f"{start['date']}T00:00:00", "timeZone": _GRAPH_TZ}
        out["end"] = {"dateTime": f"{end['date']}T00:00:00", "timeZone": _GRAPH_TZ}
    elif start:
        out["start"] = {"dateTime": _graph_local(start["dateTime"]), "timeZone": _GRAPH_TZ}
        out["end"] = {"dateTime": _graph_local(end["dateTime"]), "timeZone": _GRAPH_TZ}

    reminders = body.get("reminders")
    if reminders and not reminders.get("useDefault", True):
        overrides = reminders.get("overrides") or []
        if overrides:
            out["isReminderOn"] = True
            out["reminderMinutesBeforeStart"] = overrides[0].get("minutes", 30)
        else:
            out["isReminderOn"] = False  # e.g. a follow-up that's been checked off

    return out


async def insert_event(access_token: str, calendar_id: str, body: dict) -> str:
    j = await _req(access_token, "POST", f"/me/calendars/{calendar_id}/events", json=_to_graph_event(body))
    return j["id"]


async def patch_event(access_token: str, calendar_id: str, event_id: str, body: dict) -> None:
    await _req(access_token, "PATCH", f"/me/calendars/{calendar_id}/events/{event_id}", json=_to_graph_event(body))


async def delete_event(access_token: str, calendar_id: str, event_id: str) -> None:
    try:
        await _req(access_token, "DELETE", f"/me/calendars/{calendar_id}/events/{event_id}")
    except MicrosoftCalendarError as e:
        if "404" in str(e) or "410" in str(e):  # already gone
            return
        raise


async def event_exists(access_token: str, calendar_id: str, event_id: str) -> Optional[bool]:
    """True = still on the calendar, False = the agent deleted it (404/410), None = couldn't tell."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_GRAPH}/me/calendars/{calendar_id}/events/{event_id}",
                            headers={"Authorization": f"Bearer {access_token}"}, params={"$select": "id,isCancelled"})
    except httpx.HTTPError:
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code >= 300:
        return None
    return not (r.json() if r.text else {}).get("isCancelled")


async def free_busy(access_token: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """List of {'start': iso, 'end': iso} busy blocks, same shape google_calendar_service.free_busy
    returns, read via Graph's getSchedule against the signed-in user's own email."""
    info = await get_userinfo(access_token)
    email = info.get("email")
    if not email:
        return []
    j = await _req(access_token, "POST", "/me/calendar/getSchedule", json={
        "schedules": [email],
        "startTime": {"dateTime": time_min.astimezone(timezone.utc).isoformat().replace("+00:00", ""), "timeZone": "UTC"},
        "endTime": {"dateTime": time_max.astimezone(timezone.utc).isoformat().replace("+00:00", ""), "timeZone": "UTC"},
        "availabilityViewInterval": 30,
    })
    schedules = j.get("value") or []
    if not schedules:
        return []
    items = schedules[0].get("scheduleItems") or []
    return [
        {"start": it["start"]["dateTime"], "end": it["end"]["dateTime"]}
        for it in items
        if (it.get("status") or "").lower() not in ("free", "")
    ]
