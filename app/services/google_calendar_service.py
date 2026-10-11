"""Low-level Google OAuth + Calendar API v3 client (httpx, no Google SDK).

Scopes we use (both non-sensitive):
  - calendar.app.created : create ONE secondary "OpenHousePal" calendar in the agent's account
                           and manage only the events we put on it. Their other calendars are
                           untouched and invisible to us.
  - calendar.freebusy    : read their busy blocks so the follow-up scheduler can avoid clashes.

Everything here is pure HTTP against Google endpoints. The higher-level orchestration (which
tasks / open houses to push, when) lives in calendar_sync_service.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_REVOKE = "https://oauth2.googleapis.com/revoke"
_CAL = "https://www.googleapis.com/calendar/v3"
_USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class GoogleCalendarError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))


def _client_id() -> str:
    return os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")


def _client_secret() -> str:
    return os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")


def redirect_uri() -> str:
    return os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or (
        (os.getenv("API_URL") or ("http://localhost:8081" if os.getenv("DEV", "false") == "true" else "https://api.openhousepal.com")).rstrip("/") + "/integrations/google/callback"
    )


def build_auth_url(state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",       # we want a refresh token
        "prompt": "consent",            # force refresh-token issue every time
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Auth code -> {access_token, refresh_token, expiry(datetime)}."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(_TOKEN, data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
    if r.status_code >= 300:
        raise GoogleCalendarError(f"token exchange failed: {r.status_code} {r.text[:300]}")
    j = r.json()
    return {
        "access_token": j["access_token"],
        "refresh_token": j.get("refresh_token"),
        "expiry": datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600))),
        "scope": j.get("scope", ""),
    }


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(_TOKEN, data={
            "refresh_token": refresh_token,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        })
    if r.status_code >= 300:
        raise GoogleCalendarError(f"token refresh failed: {r.status_code} {r.text[:300]}")
    j = r.json()
    return {
        "access_token": j["access_token"],
        "expiry": datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600))),
    }


async def revoke(token: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            await c.post(_REVOKE, params={"token": token})
    except Exception:
        pass


async def get_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(_USERINFO, headers={"Authorization": f"Bearer {access_token}"})
    return r.json() if r.status_code < 300 else {}


# --------------------------------------------------------------------------- Calendar API

async def _req(access_token: str, method: str, path: str, *, json=None, params=None) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.request(
            method, f"{_CAL}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=json, params=params,
        )
    if r.status_code == 204:
        return {}
    if r.status_code >= 300:
        raise GoogleCalendarError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


async def create_app_calendar(access_token: str, summary: str = "OpenHousePal",
                              description: str = "Your OpenHousePal follow-ups and open houses.") -> str:
    j = await _req(access_token, "POST", "/calendars", json={"summary": summary, "description": description, "timeZone": "America/New_York"})
    return j["id"]


async def style_app_calendar(access_token: str, calendar_id: str) -> bool:
    """No-op on Google, on purpose. Setting the calendar's list entry (shown/colour) needs the
    broader calendar or calendar.calendarlist scope - with our narrow calendar.app.created scope
    Google answers 401 (verified 2026-09-27), and widening scopes restarts Google's app
    verification. Google already shows a newly created calendar by default, and follow-up events
    carry their own colour (colorId in calendar_sync_service._task_event_body) instead."""
    return False


async def set_calendar_timezone(access_token: str, calendar_id: str, tz: str) -> bool:
    """Set the OpenHousePal calendar's own time zone (calendars created without one default to
    GMT). This is the calendar resource we created - allowed under calendar.app.created, unlike
    the calendar-LIST entry. Best-effort, never raises."""
    try:
        await _req(access_token, "PATCH", f"/calendars/{calendar_id}", json={"timeZone": tz})
        return True
    except Exception:  # noqa: BLE001
        return False


async def get_calendar(access_token: str, calendar_id: str) -> Optional[dict]:
    try:
        return await _req(access_token, "GET", f"/calendars/{calendar_id}")
    except GoogleCalendarError:
        return None


async def delete_calendar(access_token: str, calendar_id: str) -> None:
    try:
        await _req(access_token, "DELETE", f"/calendars/{calendar_id}")
    except GoogleCalendarError:
        pass


async def insert_event(access_token: str, calendar_id: str, body: dict) -> str:
    j = await _req(access_token, "POST", f"/calendars/{calendar_id}/events", json=body)
    return j["id"]


async def patch_event(access_token: str, calendar_id: str, event_id: str, body: dict) -> None:
    await _req(access_token, "PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=body)


async def delete_event(access_token: str, calendar_id: str, event_id: str) -> None:
    try:
        await _req(access_token, "DELETE", f"/calendars/{calendar_id}/events/{event_id}")
    except GoogleCalendarError as e:
        if "410" in str(e) or "404" in str(e):   # already gone
            return
        raise


async def event_exists(access_token: str, calendar_id: str, event_id: str) -> Optional[bool]:
    """True = still on the calendar, False = the agent deleted it (Google keeps a deleted event as
    status "cancelled", or answers 404/410), None = couldn't tell (network, auth...)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_CAL}/calendars/{calendar_id}/events/{event_id}",
                            headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError:
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code >= 300:
        return None
    return (r.json() if r.text else {}).get("status") != "cancelled"


async def free_busy(access_token: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """List of {'start': iso, 'end': iso} busy blocks across the agent's primary calendar."""
    j = await _req(access_token, "POST", "/freeBusy", json={
        "timeMin": time_min.astimezone(timezone.utc).isoformat(),
        "timeMax": time_max.astimezone(timezone.utc).isoformat(),
        "items": [{"id": "primary"}],
    })
    return (j.get("calendars", {}).get("primary", {}) or {}).get("busy", []) or []
