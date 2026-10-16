"""Send an agent's GUEST emails from the agent's own Gmail / Outlook.

Why: OpenHousePal's shared sending domain can land in junk. When an agent connects their mailbox
(send-only permission: Gmail `gmail.send`, Microsoft `Mail.Send`) and leaves the switch on, the
emails that go to their guests and clients - Showcase invites and digests, tour confirmations,
comment replies, Spotlight alerts - are rendered here from the same Mailgun template and sent
through the agent's own account. They arrive from the agent's real address and show in their Sent
folder. Agent-only alerts (daily digest, new-lead alerts...) never use this.

Safety net: `send_guest` returns None whenever the mailbox can't be used (not connected, switched
off, daily limit reached, token refused, provider error) and the caller sends through OpenHousePal
as before - a guest email is never lost because of the mailbox.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.models.database import MailConnection, Notification

logger = logging.getLogger(__name__)
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/userinfo.email", "openid"]
MS_SCOPES = ["https://graph.microsoft.com/Mail.Send", "https://graph.microsoft.com/User.Read", "offline_access", "openid", "email"]
# Well under each provider's own daily cap (Gmail ~500, Outlook.com ~300 recipients/day); past this
# the rest of the day's guest emails go through OpenHousePal.
DAILY_CAP = {"google": 400, "microsoft": 250}
# And no bursts: a personal mailbox suddenly sending dozens of near-identical emails in a few
# minutes is what spam filters look for. Past this in any rolling hour, OpenHousePal sends the rest.
HOURLY_CAP = {"google": 60, "microsoft": 40}
_RECENT_SENDS: dict[str, list[float]] = {}


def _hour_full(owner_id: str, provider: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _RECENT_SENDS.get(owner_id, []) if now - t < 3600]
    _RECENT_SENDS[owner_id] = recent
    return len(recent) >= HOURLY_CAP.get(provider, 40)


def _note_send(owner_id: str) -> None:
    _RECENT_SENDS.setdefault(owner_id, []).append(time.monotonic())
_TZ = ZoneInfo("America/New_York")


class MailboxAuthError(RuntimeError):
    """The provider refused our token for good - the agent has to reconnect."""


# ----------------------------------------------------------------------------- OAuth

def is_configured(provider: str) -> bool:
    if provider == "google":
        return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))
    return bool(os.getenv("MS_OAUTH_CLIENT_ID") and os.getenv("MS_OAUTH_CLIENT_SECRET"))


def build_auth_url(provider: str, state: str) -> str:
    """Same OAuth apps and callback addresses as the calendar connection - only the scopes differ,
    so nothing new has to be registered with Google or Microsoft."""
    from urllib.parse import urlencode
    if provider == "google":
        from app.services import google_calendar_service as g
        params = {"client_id": g._client_id(), "redirect_uri": g.redirect_uri(), "response_type": "code",
                  "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent", "state": state}
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    from app.services import microsoft_calendar_service as m
    params = {"client_id": m._client_id(), "redirect_uri": m.redirect_uri(), "response_type": "code",
              "response_mode": "query", "scope": " ".join(MS_SCOPES), "state": state, "prompt": "select_account"}
    return f"{m._auth_base()}/authorize?{urlencode(params)}"


async def _token_request(provider: str, data: dict) -> dict:
    if provider == "google":
        from app.services import google_calendar_service as g
        url = "https://oauth2.googleapis.com/token"
        data = {**data, "client_id": g._client_id(), "client_secret": g._client_secret()}
    else:
        from app.services import microsoft_calendar_service as m
        url = f"{m._auth_base()}/token"
        data = {**data, "client_id": m._client_id(), "client_secret": m._client_secret(), "scope": " ".join(MS_SCOPES)}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(url, data=data)
    if r.status_code >= 300:
        body = r.text[:300]
        if r.status_code in (400, 401) and ("invalid_grant" in body or "interaction_required" in body or "invalid_client" in body):
            raise MailboxAuthError(body)
        raise RuntimeError(f"token request failed: {r.status_code} {body}")
    return r.json()


async def complete_oauth(db, owner_id: str, code: str, provider: str) -> MailConnection:
    from app.services import google_calendar_service as g, microsoft_calendar_service as m
    redirect = g.redirect_uri() if provider == "google" else m.redirect_uri()
    j = await _token_request(provider, {"code": code, "redirect_uri": redirect, "grant_type": "authorization_code"})
    scope = j.get("scope", "")
    needed = "gmail.send" if provider == "google" else "Mail.Send"
    if needed.lower() not in scope.lower():
        raise RuntimeError("send permission was not granted")
    info = await (g.get_userinfo(j["access_token"]) if provider == "google" else m.get_userinfo(j["access_token"]))
    conn = (await db.execute(select(MailConnection).where(MailConnection.owner_id == owner_id))).scalar_one_or_none()
    if not conn:
        conn = MailConnection(owner_id=owner_id, provider=provider)
        db.add(conn)
    conn.provider = provider
    conn.account_email = (info or {}).get("email") or conn.account_email
    conn.access_token = j["access_token"]
    if j.get("refresh_token"):
        conn.refresh_token = j["refresh_token"]
    conn.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600)))
    conn.scopes = scope
    conn.status = "ok"
    conn.enabled = True
    conn.last_error = None
    await db.commit()
    await db.refresh(conn)
    return conn


async def _access_token(db, conn: MailConnection, force: bool = False) -> str:
    if not force and conn.access_token and conn.token_expiry and conn.token_expiry > datetime.now(timezone.utc) + timedelta(minutes=2):
        return conn.access_token
    if not conn.refresh_token:
        raise MailboxAuthError("no refresh token")
    j = await _token_request(conn.provider, {"refresh_token": conn.refresh_token, "grant_type": "refresh_token"})
    conn.access_token = j["access_token"]
    if j.get("refresh_token"):
        conn.refresh_token = j["refresh_token"]
    conn.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600)))
    await db.commit()
    return conn.access_token


# ----------------------------------------------------------------------------- templates

_TEMPLATE_CACHE: dict[str, tuple[float, Any]] = {}
_TEMPLATE_TTL = 600


def _mailgun_api() -> tuple[str, str, str]:
    url = os.getenv("MAILGUN_URL") or ""
    m = re.match(r"(https?://[^/]+)/v3/([^/]+)/", url)
    if not m:
        raise RuntimeError("MAILGUN_URL is not set")
    return m.group(1), m.group(2), os.getenv("MAILGUN_API_KEY") or ""


async def _template(name: str):
    """The live Mailgun template, compiled - cached 10 minutes so an edit in Mailgun shows up."""
    hit = _TEMPLATE_CACHE.get(name)
    if hit and time.monotonic() - hit[0] < _TEMPLATE_TTL:
        return hit[1]
    base, domain, key = _mailgun_api()
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=("api", key)) as c:
        from app.services.email_service import mailgun_template_name
        r = await c.get(f"{base}/v3/{domain}/templates/{mailgun_template_name(name)}", params={"active": "yes"})
    if r.status_code >= 300:
        raise RuntimeError(f"template {name}: {r.status_code}")
    source = ((r.json().get("template") or {}).get("version") or {}).get("template") or ""
    from pybars import Compiler
    compiled = Compiler().compile(source)
    _TEMPLATE_CACHE[name] = (time.monotonic(), compiled)
    return compiled


def _html_to_text(html: str) -> str:
    t = re.sub(r"(?is)<(script|style|head).*?</\1>", "", html)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li)>", "\n", t)
    t = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", t)
    t = re.sub(r"<[^>]+>", "", t)
    import html as _h
    t = _h.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


async def render(template: str, variables: dict) -> tuple[str, str]:
    compiled = await _template(template)
    html = str(compiled(variables))
    return html, _html_to_text(html)


# ----------------------------------------------------------------------------- sending

def _mime(from_name: str, from_addr: str, to: str, subject: str, html: str, text: str, headers: dict) -> str:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    for k, v in headers.items():
        msg[k] = v
    msg.set_content(text or " ")
    msg.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def _deliver(conn: MailConnection, token: str, *, to: str, subject: str, html: str, text: str,
                   from_name: str, headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        if conn.provider == "google":
            raw = _mime(from_name, conn.account_email or "", to, subject, html, text, headers)
            return await c.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                headers={"Authorization": f"Bearer {token}"}, json={"raw": raw})
        # Microsoft Graph only accepts custom headers named X-...; the unsubscribe link is in the body.
        return await c.post("https://graph.microsoft.com/v1.0/me/sendMail", headers={"Authorization": f"Bearer {token}"}, json={
            "message": {"subject": subject, "body": {"contentType": "HTML", "content": html},
                        "toRecipients": [{"emailAddress": {"address": to}}]},
            "saveToSentItems": True,
        })


def _today() -> str:
    return datetime.now(_TZ).date().isoformat()


async def _flag_reconnect(db, conn: MailConnection, reason: str) -> None:
    """Pause sending + tell the agent once (bell). OpenHousePal keeps sending meanwhile."""
    first = conn.status != "needs_reconnect"
    conn.status = "needs_reconnect"
    conn.last_error = reason[:500]
    if first:
        db.add(Notification(
            agent_id=conn.owner_id, type="MAILBOX_DISCONNECTED", reference_type="INTEGRATION", reference_id=conn.id,
            title="Reconnect your email",
            message=f"We can't send from {conn.account_email or 'your mailbox'} right now, so guest emails are going out from OpenHousePal. Reconnect it in Integrations.",
            link="/studio/integrations#send-as", is_read=False, created_at=datetime.now(timezone.utc),
        ))
    await db.commit()


# Why a provider refused a send, from its own error text. Only a lost permission is fixed by
# reconnecting; the other two used to say "please reconnect" too, which sent agents round in circles.
#   'unavailable' - sending isn't switched on for OUR Google/Microsoft app (e.g. the Gmail API is off
#                   in the Cloud project). Nothing the agent can do; we fix it on our side.
#   'blocked'     - their mailbox / company doesn't let outside apps send for them (a Google Workspace
#                   or Microsoft 365 admin setting, or mail turned off for the account).
#   'reconnect'   - the permission was removed / the password changed.
_UNAVAILABLE_HINTS = ("accessnotconfigured", "service_disabled", "has not been used in project", "api has not been used", "is disabled")
_BLOCKED_HINTS = ("mail service not enabled", "failedprecondition", "admin_policy", "delegation denied", "domain policy",
                  "access blocked", "mailboxnotenabled", "errorsendasdenied", "sendasdenied")
REFUSAL_TEXT = {
    "unavailable": "Sending from your email isn't switched on on our side yet - guest emails go out from OpenHousePal until it is. Nothing for you to do.",
    "blocked": "Your email account doesn't let apps send for you - usually a setting your office (Google Workspace / Microsoft 365) controls. Ask your IT team to allow OpenHousePal, or connect a personal email instead.",
    "reconnect": "Your email account refused to send - please reconnect it.",
}


def refusal_kind(status_code: int, body: str) -> str:
    b = (body or "").lower()
    if any(h in b for h in _UNAVAILABLE_HINTS):
        return "unavailable"
    if any(h in b for h in _BLOCKED_HINTS):
        return "blocked"
    return "reconnect" if status_code in (401, 403) else ""


async def _note_refusal(db, conn: MailConnection, kind: str, detail: str) -> None:
    """Record a refusal that reconnecting won't fix: sending pauses (guest emails go out from
    OpenHousePal) and the agent sees why; a later successful test switches it back on."""
    conn.status = kind
    conn.last_error = detail[:500]
    if kind == "unavailable":
        logger.error("mailbox sending unavailable on our side (check the Gmail API / Graph app): %s", detail[:300])
    await db.commit()


def _dev_suppressed() -> bool:
    from app.services.email_service import EmailService
    return EmailService().is_dev and os.getenv("MAILBOX_SEND_IN_DEV", "no") != "yes"


async def send_guest(owner_id: Optional[str], to_email: str, subject: str, template: str, variables: dict,
                     *, from_name: Optional[str] = None) -> Optional[tuple[int, str]]:
    """Send one guest email from the agent's mailbox. Returns (status, info) when the mailbox sent
    it, or None when the caller should send it through OpenHousePal instead. Never raises."""
    if not owner_id or not to_email:
        return None
    from app.database import AsyncSessionLocal
    from app.services.email_service import prepare_template_email, unsubscribe_headers
    try:
        async with AsyncSessionLocal() as db:
            conn = (await db.execute(select(MailConnection).where(MailConnection.owner_id == owner_id))).scalar_one_or_none()
            if not conn or not conn.enabled or conn.status != "ok" or not conn.account_email:
                return None
            today = _today()
            if conn.sent_day != today:
                conn.sent_day, conn.sent_count = today, 0
            if conn.sent_count >= DAILY_CAP.get(conn.provider, 250) or _hour_full(owner_id, conn.provider):
                return None
            subject, variables = prepare_template_email(subject, variables)
            name = from_name or variables.get("agent_name") or ""
            if _dev_suppressed():
                logger.info("DEV MODE: mailbox email suppressed", extra={"to": to_email, "template": template, "via": conn.provider, "from": conn.account_email})
                return 200, "Mailbox email suppressed in dev mode"
            html, text = await render(template, variables)
            headers = unsubscribe_headers(template, to_email, variables)
            try:
                token = await _access_token(db, conn)
                resp = await _deliver(conn, token, to=to_email, subject=subject, html=html, text=text, from_name=name, headers=headers)
                if resp.status_code == 401:
                    token = await _access_token(db, conn, force=True)
                    resp = await _deliver(conn, token, to=to_email, subject=subject, html=html, text=text, from_name=name, headers=headers)
            except MailboxAuthError as e:
                await _flag_reconnect(db, conn, f"token refused: {e}")
                return None
            kind = refusal_kind(resp.status_code, resp.text) if resp.status_code >= 300 else ""
            if kind == "reconnect":
                await _flag_reconnect(db, conn, f"{resp.status_code} {resp.text[:200]}")
                return None
            if kind in ("unavailable", "blocked"):
                await _note_refusal(db, conn, kind, f"{resp.status_code} {resp.text[:300]}")
                return None
            if resp.status_code >= 300:
                conn.last_error = f"{resp.status_code} {resp.text[:300]}"
                await db.commit()
                return None
            conn.sent_count = (conn.sent_count or 0) + 1
            conn.total_sent = (conn.total_sent or 0) + 1
            _note_send(owner_id)
            conn.last_sent_at = datetime.now(timezone.utc)
            conn.last_error = None
            await db.commit()
            return 200, f"sent via {conn.provider}"
    except Exception as e:  # noqa: BLE001 - the OpenHousePal send is the safety net
        logger.warning("mailbox send failed, falling back to OpenHousePal: %s", e)
        return None


async def send_test(db, conn: MailConnection, to_email: str, agent_name: str) -> tuple[bool, str]:
    """A short message from the agent's mailbox to themselves - proves it works end to end."""
    html = (f"<p>Hi {agent_name or 'there'},</p><p>This is a test from OpenHousePal. Your guest emails - Showcase "
            f"invites, tour confirmations and listing alerts - will now come from <b>{conn.account_email}</b>.</p>"
            "<p>You'll find a copy of each one in your Sent folder.</p>")
    try:
        token = await _access_token(db, conn)
        resp = await _deliver(conn, token, to=to_email, subject="Test: your guest emails now come from you",
                              html=html, text=_html_to_text(html), from_name=agent_name, headers={})
        if resp.status_code == 401:
            token = await _access_token(db, conn, force=True)
            resp = await _deliver(conn, token, to=to_email, subject="Test: your guest emails now come from you",
                                  html=html, text=_html_to_text(html), from_name=agent_name, headers={})
    except MailboxAuthError as e:
        await _flag_reconnect(db, conn, f"token refused: {e}")
        return False, "Your email account refused the connection - please reconnect it."
    except Exception:  # noqa: BLE001
        return False, "We couldn't reach your email account - please try again in a minute."
    kind = refusal_kind(resp.status_code, resp.text) if resp.status_code >= 300 else ""
    if kind == "reconnect":
        await _flag_reconnect(db, conn, f"{resp.status_code} {resp.text[:200]}")
        return False, REFUSAL_TEXT["reconnect"]
    if kind in ("unavailable", "blocked"):
        await _note_refusal(db, conn, kind, f"{resp.status_code} {resp.text[:300]}")
        return False, REFUSAL_TEXT[kind]
    if resp.status_code >= 300:
        return False, "Your email account didn't accept the message - please try again in a minute."
    # it works (again): a mailbox paused by an earlier refusal goes back to sending
    if conn.status != "ok" or conn.last_error:
        conn.status, conn.last_error = "ok", None
        await db.commit()
    return True, f"Sent - check {to_email}. It came from {conn.account_email}."


# Fire-and-forget guest sends (from request handlers): keep a reference so the task isn't collected.
_PENDING: set = set()


def spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
