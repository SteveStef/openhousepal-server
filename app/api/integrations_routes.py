import re
"""Agent-facing CRM / portal / calendar integration settings.

One Settings section ("Integrations"):
  - CRM push       : forward every new OpenHousePal lead to the agent's real CRM (webhook + email)
  - Portal in      : the agent's unique inbound address for forwarding Zillow / Realtor.com leads
  - Calendar sync  : connect Google Calendar (OAuth) so OHP tasks + open houses become real,
                     editable events and the follow-up scheduler can read the agent's busy times

Available on every plan tier. Outbound push -> crm_dispatch_service; inbound parse ->
portal_inbound_service + /webhooks/inbound-email; calendar -> calendar_sync_service +
google_calendar_service.
"""
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.services import calendar_sync_service, crm_dispatch_service
from app.services import google_calendar_service as gcal
from app.services import microsoft_calendar_service as mscal
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/integrations", tags=["integrations"])

INBOUND_LEAD_DOMAIN = os.getenv("INBOUND_LEAD_DOMAIN", "parse.openhousepal.com")
_STATE_TTL = 600  # seconds an OAuth `state` value stays valid


def _state_secret() -> bytes:
    return (os.getenv("JWT_SECRET_KEY") or "dev-insecure").encode()


def _make_state(owner_id: str, back: str = "settings") -> str:
    ts = str(int(time.time()))
    back = back if back in ("setup", "mail-settings", "mail-setup", "mail-emails") else "settings"
    sig = hmac.new(_state_secret(), f"{owner_id}.{ts}.{back}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{owner_id}.{ts}.{back}.{sig}"


def _read_state(state: str) -> Optional[str]:
    return _read_state_full(state)[0]


def _read_state_full(state: str) -> tuple[Optional[str], str]:
    """(owner_id, where to send the browser back: 'settings' or 'setup')."""
    parts = (state or "").split(".")
    if len(parts) != 4:
        return None, "settings"
    owner_id, ts, back, sig = parts
    expected = hmac.new(_state_secret(), f"{owner_id}.{ts}.{back}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None, "settings"
    try:
        if abs(time.time() - int(ts)) > _STATE_TTL:
            return None, back
    except ValueError:
        return None, "settings"
    return owner_id, back


# --------------------------------------------------------------------------- schemas
class CrmSettingsUpdate(BaseModel):
    crm_push_enabled: bool = False
    crm_webhook_url: Optional[str] = Field(default=None, max_length=500)
    crm_parse_email: Optional[str] = Field(default=None, max_length=254)
    crm_fub_api_key: Optional[str] = Field(default=None, max_length=200)


class PortalInstruction(BaseModel):
    portal: str
    label: str
    steps: str


class GoogleCalendarStatus(BaseModel):
    connected: bool = True
    provider: str = "google"
    account_email: Optional[str] = None
    calendar_name: Optional[str] = None
    last_sync_at: Optional[str] = None
    last_error: Optional[str] = None
    needs_reconnect: bool = False


class MicrosoftCalendarStatus(BaseModel):
    connected: bool = True
    provider: str = "microsoft"
    account_email: Optional[str] = None
    calendar_name: Optional[str] = None
    last_sync_at: Optional[str] = None
    last_error: Optional[str] = None
    needs_reconnect: bool = False


class IntegrationsOut(BaseModel):
    crm_push_enabled: bool
    crm_webhook_url: Optional[str]
    crm_parse_email: Optional[str]
    crm_fub_connected: bool = False
    crm_fub_note: Optional[str] = None
    crm_last_push_at: Optional[str]
    crm_last_push_status: Optional[str]
    inbound_address: Optional[str]
    portal_instructions: list[PortalInstruction]
    google_calendar_available: bool = False
    google_calendar: Optional[GoogleCalendarStatus] = None
    microsoft_calendar_available: bool = False
    microsoft_calendar: Optional[MicrosoftCalendarStatus] = None


class CrmTestResult(BaseModel):
    webhook: Optional[str] = None
    email: Optional[str] = None
    fub: Optional[str] = None
    detail: str


def _inbound_address(user: User) -> Optional[str]:
    return f"leads-{user.inbound_lead_token}@{INBOUND_LEAD_DOMAIN}" if user.inbound_lead_token else None


def _portal_instructions(address: str) -> list[PortalInstruction]:
    return [
        PortalInstruction(
            portal="zillow", label="Zillow / Zillow Premier Agent",
            steps=(f"In Zillow, open Settings > Contacts / Leads > Lead forwarding (or your CRM "
                   f"connection), and add {address} as a forwarding email. New Zillow leads will "
                   f"then also appear in OpenHousePal."),
        ),
        PortalInstruction(
            portal="realtor", label="Realtor.com",
            steps=(f"In your Realtor.com dashboard, go to Lead Settings > Lead Routing and add "
                   f"{address} as an additional recipient / forwarding address."),
        ),
        PortalInstruction(
            portal="homes", label="Homes.com",
            steps=(f"In Homes.com agent settings, under Leads > Notifications, add {address} as a "
                   f"lead notification email."),
        ),
        PortalInstruction(
            portal="facebook", label="Facebook lead ads / other",
            steps=(f"Point your Facebook Lead Ads (via your CRM or Zapier) at {address}, or set up "
                   f"a filter in your own inbox that auto-forwards any lead email to {address}."),
        ),
    ]


def _serialize(
    user: User,
    google_status: Optional[dict] = None,
    microsoft_status: Optional[dict] = None,
) -> IntegrationsOut:
    addr = _inbound_address(user)
    return IntegrationsOut(
        crm_push_enabled=user.crm_push_enabled,
        crm_webhook_url=user.crm_webhook_url,
        crm_parse_email=user.crm_parse_email,
        crm_fub_connected=bool(user.crm_fub_api_key),
        crm_fub_note=user.crm_fub_account_note if user.crm_fub_api_key else None,
        crm_last_push_at=user.crm_last_push_at.isoformat() if user.crm_last_push_at else None,
        crm_last_push_status=user.crm_last_push_status,
        inbound_address=addr,
        portal_instructions=_portal_instructions(addr) if addr else [],
        google_calendar_available=gcal.is_configured(),
        google_calendar=GoogleCalendarStatus(**google_status) if google_status else None,
        microsoft_calendar_available=mscal.is_configured(),
        microsoft_calendar=MicrosoftCalendarStatus(**microsoft_status) if microsoft_status else None,
    )


async def _calendar_statuses(db: AsyncSession, owner_id: str) -> tuple[Optional[dict], Optional[dict]]:
    g = await calendar_sync_service.get_connection(db, owner_id, "google")
    m = await calendar_sync_service.get_connection(db, owner_id, "microsoft")
    return calendar_sync_service.status_dict(g), calendar_sync_service.status_dict(m)


# --------------------------------------------------------------------------- routes
@router.get("", response_model=IntegrationsOut)
async def get_integrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Current CRM push + portal inbound settings. Mints the inbound token on first read so the
    address is always ready to copy."""
    if not current_user.inbound_lead_token:
        current_user.inbound_lead_token = secrets.token_hex(6)
        db.add(current_user)
        await db.commit()
        await db.refresh(current_user)
    g, m = await _calendar_statuses(db, current_user.id)
    return _serialize(current_user, g, m)


@router.put("", response_model=IntegrationsOut)
async def update_integrations(
    payload: CrmSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    url = (payload.crm_webhook_url or "").strip() or None
    if url:
        # Our server POSTs every lead here - only a public https/http address (never our own
        # network), same rule as every other agent-typed URL the server fetches.
        from app.utils.safe_fetch import assert_public_url, UnsafeUrlError
        try:
            await assert_public_url(url)
        except UnsafeUrlError:
            raise HTTPException(status_code=422, detail="Use your CRM's public webhook address (starting with https://).")
    email = (payload.crm_parse_email or "").strip().lower() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
        raise HTTPException(status_code=422, detail="That CRM email address doesn't look right.")
    fub = (payload.crm_fub_api_key or "").strip() or None
    current_user.crm_webhook_url = url
    current_user.crm_parse_email = email
    # A blank string clears the stored key; a value replaces it. (Never echoed back.)
    if payload.crm_fub_api_key is not None:
        key_changed = fub != current_user.crm_fub_api_key
        current_user.crm_fub_api_key = fub
        if not fub:
            current_user.crm_fub_account_note = None
        elif key_changed:
            # Probe FUB once so Settings can warn team-account users that leads route to the owner.
            current_user.crm_fub_account_note = await crm_dispatch_service.probe_fub_account(fub)
    # Can't "enable" with nothing to send to.
    current_user.crm_push_enabled = bool(payload.crm_push_enabled and (url or email or current_user.crm_fub_api_key))
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return _serialize(current_user)


@router.post("/crm/test", response_model=CrmTestResult)
async def test_crm_connection(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Send a synthetic 'Test Lead' down whichever channels are set (ignores the enabled
    toggle), so the agent can confirm it lands in their CRM before switching push on."""
    if not (current_user.crm_webhook_url or current_user.crm_parse_email or current_user.crm_fub_api_key):
        return CrmTestResult(detail="Add a Follow Up Boss API key, a webhook URL, or a CRM parse email first, then test.")
    result = await crm_dispatch_service.send_test(db, current_user)
    _names = {"fub": "Follow Up Boss", "webhook": "webhook", "email": "email"}

    def _why(v: str) -> str:  # plain words instead of status codes
        if v in ("HTTP 401", "HTTP 403"):
            return "the key or login was rejected - check you copied the whole thing"
        if v == "HTTP 404":
            return "that address wasn't found - check the link"
        if v.startswith("HTTP 5"):
            return "their system had a hiccup - try again in a minute"
        if v.startswith("error:"):
            return "we couldn't reach it - check the address"
        return v
    ok = [_names.get(k, k) for k, v in result.items() if v == "ok"]
    bad = [f"{_names.get(k, k)} ({_why(v)})" for k, v in result.items() if v and v != "ok"]
    if ok and not bad:
        detail = f"Test lead delivered via {', '.join(ok)}. Check your CRM."
    elif ok and bad:
        detail = f"Delivered via {', '.join(ok)}; problem with {', '.join(bad)}."
    else:
        detail = f"Could not deliver: {', '.join(bad) or 'no channels configured'}."
    if ok and not current_user.crm_push_enabled:
        detail += " Sending is switched off, so real leads won't go there yet - turn it on above."
    return CrmTestResult(webhook=result.get("webhook"), email=result.get("email"), fub=result.get("fub"), detail=detail)


@router.post("/inbound/rotate", response_model=IntegrationsOut)
async def rotate_inbound_address(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Regenerate the inbound token. The old leads-<token>@... address stops working immediately."""
    current_user.inbound_lead_token = secrets.token_hex(6)
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    g, m = await _calendar_statuses(db, current_user.id)
    return _serialize(current_user, g, m)


# --------------------------------------------------------------------------- Google Calendar

def _frontend_settings_url(status: str, back: str = "settings") -> str:
    base = (os.getenv("CLIENT_URL") or "http://localhost:3000").rstrip("/")
    # Setup opens the sign-in in its own tab (setup is never interrupted) - that tab lands on a small
    # "connected" page that tells setup and closes itself.
    if back == "mail-setup":
        return f"{base}/settings/connected?what=email&status={status}"
    if back == "mail-settings":
        return f"{base}/studio/integrations?mailbox={status}#send-as"
    if back == "mail-emails":
        return f"{base}/leads/emails?tab=sender&mailbox={status}"
    if back == "setup":
        return f"{base}/settings/connected?what=calendar&status={status}"
    return f"{base}/settings?calendar={status}#calendar"


@router.get("/google/connect")
async def google_connect(back: str = Query("settings"), current_user: User = Depends(get_current_active_user)):
    """Return the Google consent URL for the frontend to send the agent to."""
    if not gcal.is_configured():
        raise HTTPException(503, "Google Calendar isn't configured on this server yet.")
    return {"auth_url": gcal.build_auth_url(_make_state(current_user.id, back))}


@router.get("/google/callback")
async def google_callback(
    db: AsyncSession = Depends(get_db),
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Google redirects the browser here after consent. No auth dependency - the signed `state`
    is the credential. Always ends in a redirect back to Settings."""
    owner_id, back = _read_state_full(state or "")
    if error or not code or not state:
        return RedirectResponse(_frontend_settings_url("error", back), status_code=302)
    if not owner_id:
        return RedirectResponse(_frontend_settings_url("error", back), status_code=302)
    try:
        if back.startswith("mail"):
            from app.services import agent_mailbox_service
            await agent_mailbox_service.complete_oauth(db, owner_id, code, "google")
        else:
            await calendar_sync_service.complete_oauth(db, owner_id, code, "google")
    except Exception:  # noqa: BLE001
        return RedirectResponse(_frontend_settings_url("error", back), status_code=302)
    return RedirectResponse(_frontend_settings_url("connected", back), status_code=302)


@router.delete("/google", response_model=IntegrationsOut)
async def google_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete the OHP calendar + the events we made, revoke the token, forget the connection."""
    await calendar_sync_service.disconnect(db, current_user.id, "google")
    await db.refresh(current_user)
    _, m = await _calendar_statuses(db, current_user.id)
    return _serialize(current_user, None, m)


@router.post("/google/resync", response_model=IntegrationsOut)
async def google_resync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Re-push every open dated task + upcoming scheduled open house into the connected calendar."""
    conn = await calendar_sync_service.get_connection(db, current_user.id, "google")
    if not conn:
        raise HTTPException(400, "Connect Google Calendar first.")
    await calendar_sync_service.trigger_backfill(current_user.id, "google")
    _, m = await _calendar_statuses(db, current_user.id)
    return _serialize(current_user, calendar_sync_service.status_dict(conn), m)


# --------------------------------------------------------------------------- Microsoft / Outlook Calendar

@router.get("/microsoft/connect")
async def microsoft_connect(back: str = Query("settings"), current_user: User = Depends(get_current_active_user)):
    """Return the Microsoft consent URL for the frontend to send the agent to."""
    if not mscal.is_configured():
        raise HTTPException(503, "Microsoft Calendar isn't configured on this server yet.")
    return {"auth_url": mscal.build_auth_url(_make_state(current_user.id, back))}


@router.get("/microsoft/callback")
async def microsoft_callback(
    db: AsyncSession = Depends(get_db),
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Microsoft redirects the browser here after consent. No auth dependency - the signed
    `state` is the credential. Always ends in a redirect back to Settings."""
    owner_id, back = _read_state_full(state or "")
    if error or not code or not state:
        return RedirectResponse(_frontend_settings_url("error", back), status_code=302)
    if not owner_id:
        return RedirectResponse(_frontend_settings_url("error", back), status_code=302)
    try:
        if back.startswith("mail"):
            from app.services import agent_mailbox_service
            await agent_mailbox_service.complete_oauth(db, owner_id, code, "microsoft")
        else:
            await calendar_sync_service.complete_oauth(db, owner_id, code, "microsoft")
    except Exception:  # noqa: BLE001
        return RedirectResponse(_frontend_settings_url("error", back), status_code=302)
    return RedirectResponse(_frontend_settings_url("connected", back), status_code=302)


@router.delete("/microsoft", response_model=IntegrationsOut)
async def microsoft_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete the OHP calendar + the events we made, discard the token, forget the connection."""
    await calendar_sync_service.disconnect(db, current_user.id, "microsoft")
    await db.refresh(current_user)
    g, _ = await _calendar_statuses(db, current_user.id)
    return _serialize(current_user, g, None)


@router.post("/microsoft/resync", response_model=IntegrationsOut)
async def microsoft_resync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Re-push every open dated task + upcoming scheduled open house into the connected calendar."""
    conn = await calendar_sync_service.get_connection(db, current_user.id, "microsoft")
    if not conn:
        raise HTTPException(400, "Connect Microsoft Calendar first.")
    await calendar_sync_service.trigger_backfill(current_user.id, "microsoft")
    g, _ = await _calendar_statuses(db, current_user.id)
    return _serialize(current_user, g, calendar_sync_service.status_dict(conn))



# ===========================================================================
# Send guest emails from the agent's own Gmail / Outlook (agent_mailbox_service)
# ===========================================================================

def _mailbox_out(conn) -> dict:
    if not conn:
        return {"connected": False, "provider": None, "account_email": None, "enabled": False, "status": None,
                "last_sent_at": None, "total_sent": 0, "last_error": None}
    return {"connected": True, "provider": conn.provider, "account_email": conn.account_email, "enabled": bool(conn.enabled),
            "status": conn.status, "last_sent_at": conn.last_sent_at.isoformat() if conn.last_sent_at else None,
            "total_sent": conn.total_sent or 0, "last_error": conn.last_error if conn.status != "ok" else None}


async def _mailbox(db: AsyncSession, user: User):
    from sqlalchemy import select
    from app.models.database import MailConnection
    return (await db.execute(select(MailConnection).where(MailConnection.owner_id == user.id))).scalar_one_or_none()


@router.get("/mailbox")
async def get_mailbox(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    from app.services import agent_mailbox_service as mb
    out = _mailbox_out(await _mailbox(db, current_user))
    out["google_available"] = mb.is_configured("google")
    out["microsoft_available"] = mb.is_configured("microsoft")
    return out


@router.get("/mailbox/{provider}/connect")
async def mailbox_connect(provider: str, back: str = Query("settings"), current_user: User = Depends(get_current_active_user)):
    from app.services import agent_mailbox_service as mb
    if provider not in ("google", "microsoft"):
        raise HTTPException(404, "Unknown email provider.")
    if not mb.is_configured(provider):
        raise HTTPException(503, "That email provider isn't set up on this server yet.")
    return {"auth_url": mb.build_auth_url(provider, _make_state(current_user.id, {"setup": "mail-setup", "emails": "mail-emails"}.get(back, "mail-settings")))}


class MailboxUpdate(BaseModel):
    enabled: bool


@router.put("/mailbox")
async def update_mailbox(payload: MailboxUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    conn = await _mailbox(db, current_user)
    if not conn:
        raise HTTPException(400, "Connect your email first.")
    conn.enabled = payload.enabled
    await db.commit()
    return _mailbox_out(conn)


@router.post("/mailbox/test")
async def test_mailbox(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    from app.services import agent_mailbox_service as mb
    conn = await _mailbox(db, current_user)
    if not conn:
        raise HTTPException(400, "Connect your email first.")
    name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
    ok, detail = await mb.send_test(db, conn, conn.account_email or current_user.email, name)
    return {"ok": ok, "detail": detail, **_mailbox_out(conn)}


@router.delete("/mailbox")
async def disconnect_mailbox(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    conn = await _mailbox(db, current_user)
    if conn:
        if conn.provider == "google" and (conn.refresh_token or conn.access_token):
            await gcal.revoke(conn.refresh_token or conn.access_token)
        await db.delete(conn)
        await db.commit()
    return _mailbox_out(None)
