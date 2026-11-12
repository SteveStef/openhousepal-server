"""Admin "Unblock email" (owner, 11/11).

An address gets blocked two ways: Mailgun keeps a bounce list (an email to it was refused once -
Mailgun then won't even try again), and we keep our own do-not-email list (blacklisted_emails),
written by a bounce, a spam complaint or an unsubscribe.

Unblocking clears a BOUNCE only. Someone who unsubscribed or marked an email as spam asked us to
stop - emailing them again breaks the law (CAN-SPAM) and hurts every agent's inbox placement, so
that stays the person's own choice (they can turn emails back on from their Showcase).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import BlacklistedEmail, ClientEmail, ClientEmailPref, Lead, LeadEvent

logger = get_logger(__name__)
_TIMEOUT = 20


def _mailgun() -> Optional[tuple[str, str, str]]:
    url = os.getenv("MAILGUN_URL") or ""
    m = re.match(r"(https?://[^/]+)/v3/([^/]+)/", url)
    key = os.getenv("MAILGUN_API_KEY") or ""
    return (m.group(1), m.group(2), key) if m and key else None


def _norm(email: str) -> str:
    return (email or "").strip().lower()


async def _mailgun_lists(email: str) -> dict:
    """{bounce: {code, error, at} | None, complaint: bool, unsubscribe: bool, ok: bool}"""
    out: dict = {"bounce": None, "complaint": False, "unsubscribe": False, "ok": False}
    mg = _mailgun()
    if not mg:
        return out
    base, domain, key = mg
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, auth=("api", key)) as c:
            for kind in ("bounces", "complaints", "unsubscribes"):
                r = await c.get(f"{base}/v3/{domain}/{kind}/{email}")
                if r.status_code == 200:
                    j = r.json()
                    if kind == "bounces":
                        out["bounce"] = {"code": j.get("code"), "error": str(j.get("error") or "")[:400], "at": j.get("created_at")}
                    elif kind == "complaints":
                        out["complaint"] = True
                    else:
                        out["unsubscribe"] = True
                elif r.status_code != 404:
                    logger.warning("mailgun suppression lookup failed", extra={"kind": kind, "status": r.status_code})
                    return out
        out["ok"] = True
    except Exception:  # noqa: BLE001
        logger.warning("mailgun suppression lookup failed", exc_info=True)
    return out


async def _unsubscribed_here(db: AsyncSession, email: str) -> bool:
    return (await db.execute(select(LeadEvent.id).join(Lead, Lead.id == LeadEvent.lead_id).where(
        Lead.email == email, LeadEvent.source_type == "email_unsubscribed").limit(1))).scalar() is not None


async def _spam_here(db: AsyncSession, email: str) -> bool:
    return (await db.execute(select(ClientEmail.id).where(
        ClientEmail.recipient_email == email, ClientEmail.delivery == "spam").limit(1))).scalar() is not None


async def check(db: AsyncSession, email: str) -> dict:
    """What's blocking this address, in plain words, and whether Unblock is allowed."""
    email = _norm(email)
    if "@" not in email:
        return {"email": email, "blocked": False, "can_unblock": False, "why": "That isn't an email address."}
    here = (await db.execute(select(BlacklistedEmail.email).where(BlacklistedEmail.email == email))).scalar() is not None
    mg = await _mailgun_lists(email)
    unsub = mg["unsubscribe"] or await _unsubscribed_here(db, email)
    spam = mg["complaint"] or await _spam_here(db, email)
    bounce = mg["bounce"]
    blocked = here or bool(bounce) or mg["complaint"] or mg["unsubscribe"]
    out = {"email": email, "blocked": blocked, "on_our_list": here, "bounce": bounce, "unsubscribed": unsub,
           "spam": spam, "mailgun_checked": mg["ok"]}
    if unsub:
        return {**out, "can_unblock": False, "why": "They unsubscribed, so only they can turn emails back on (from their Showcase)."}
    if spam:
        return {**out, "can_unblock": False, "why": "They marked an email as spam, so emails to them stay off."}
    if not blocked:
        return {**out, "can_unblock": False, "why": "Not blocked - emails can go to this address."}
    if bounce:
        said = bounce.get("error") or ""
        return {**out, "can_unblock": True,
                "why": f"An email to this address bounced on {str(bounce.get('at') or '')[:16]}" + (f" - their server said: {said}" if said else "") + "."}
    return {**out, "can_unblock": True, "why": "An earlier email to this address bounced."}


async def unblock(db: AsyncSession, email: str) -> dict:
    """Clear a bounce: Mailgun's bounce list + our do-not-email list, then let the email rules start
    fresh (old unopened emails stop counting, a Showcase that fell asleep wakes)."""
    st = await check(db, email)
    if not st["can_unblock"]:
        return {**st, "unblocked": False}
    email = st["email"]
    mg = _mailgun()
    if st.get("bounce") and mg:
        base, domain, key = mg
        async with httpx.AsyncClient(timeout=_TIMEOUT, auth=("api", key)) as c:
            r = await c.delete(f"{base}/v3/{domain}/bounces/{email}")
        if r.status_code not in (200, 404):
            return {**st, "unblocked": False, "why": f"Mailgun didn't let go of it ({r.status_code}) - try again in a minute."}
    await db.execute(delete(BlacklistedEmail).where(BlacklistedEmail.email == email))
    now = datetime.now(timezone.utc)
    prefs = (await db.execute(select(ClientEmailPref).where(ClientEmailPref.email == email))).scalars().all()
    for p in prefs:
        p.reset_at = now
    await db.commit()
    from app.services.client_email_service import refresh_status
    for p in prefs:
        try:
            await refresh_status(db, p.owner_id, email, announce=False, trigger="was unblocked")
        except Exception:  # noqa: BLE001
            logger.warning("refresh after unblock failed", exc_info=True)
    logger.info("email unblocked", extra={"event": "email_unblocked", "agents": len(prefs)})
    return {**st, "unblocked": True, "blocked": False, "why": "Unblocked - the next email will go out normally."}
