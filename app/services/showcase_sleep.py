"""Showcases that go to sleep (user, 10/22).

A client who ignored 3 weekly emails in a row (client_email_service.decide -> "off") stops getting
automatic emails, and their Showcases with that agent go to sleep: status INACTIVE with
inactive_reason "no_response". Asleep = no new matches, no emails, no agent alerts - it moves to the
Inactive tab on the agent's Showcases page. Nothing is deleted.

It wakes up by itself when the client shows up again - opens their Showcase link (the link still
works while asleep, unlike one the agent put to sleep by hand) or opens/clicks an old email - and
catches up on the homes that came on the market while it slept. The agent can also wake it.

Only counts from the first delivered email: nothing is backdated (client_emails starts 10/15)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, select

from app.models.database import Collection

logger = logging.getLogger(__name__)

NO_RESPONSE = "no_response"
AGENT = "agent"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _their_showcases(db, owner_id: str, email: str, status: str, reason: Optional[str] = None) -> list[Collection]:
    q = select(Collection).where(Collection.owner_id == owner_id, func.lower(func.trim(Collection.visitor_email)) == email,
                                 Collection.status == status)
    if reason:
        q = q.where(Collection.inactive_reason == reason)
    return list((await db.execute(q)).scalars().all())


async def sleep_for_no_response(db, owner_id: str, email: str) -> int:
    """Their ACTIVE Showcases with this agent go to sleep. Doesn't commit."""
    cols = await _their_showcases(db, owner_id, email, "ACTIVE")
    for c in cols:
        c.status, c.inactive_reason, c.inactive_at = "INACTIVE", NO_RESPONSE, _now()
    if cols:
        logger.info("showcases asleep (no response)", extra={"owner_id": owner_id, "count": len(cols)})
    return len(cols)


async def wake_no_response(db, owner_id: str, email: str) -> list[str]:
    """Wake the Showcases that slept because of no response (not ones the agent put to sleep).
    Doesn't commit - returns their ids for catch_up() after the commit."""
    cols = await _their_showcases(db, owner_id, email, "INACTIVE", NO_RESPONSE)
    for c in cols:
        c.status, c.inactive_reason, c.inactive_at = "ACTIVE", None, None
        c.updated_at = _now()
    return [c.id for c in cols]


async def catch_up(db, collection_ids: list[str]) -> None:
    """A woken Showcase picks up the homes listed while it slept (same as the agent switching it back on)."""
    from app.services.collections_service import CollectionsService
    for cid in collection_ids:
        try:
            await CollectionsService.repopulate_collection_from_preferences(db, cid, commit=True)
        except Exception:  # noqa: BLE001 - waking must still count even if the catch-up fails
            logger.warning("woken showcase catch-up failed", exc_info=True, extra={"collection_id": cid})


async def wake_on_visit(db, share_token: str) -> bool:
    """The client opened a Showcase that fell asleep for no response: wake it (and their other
    sleeping ones with this agent), emails back to daily, the agent told. Commits."""
    from app.services.showcase_links import share_match
    col = (await db.execute(select(Collection).where(and_(
        share_match(share_token), Collection.is_public == True,  # noqa: E712
        Collection.status == "INACTIVE", Collection.inactive_reason == NO_RESPONSE,
    )))).scalar_one_or_none()
    if col is None:
        return False
    email = (col.visitor_email or "").strip().lower()
    woke: list[str] = []
    if email:
        from app.services.client_email_service import _pref, refresh_status
        pref = await _pref(db, col.owner_id, email)
        pref.reset_at = _now()   # the ignored emails don't count any more - they're back
        await db.flush()
        # off -> daily: announces it and wakes every Showcase that slept for this (refresh_status).
        await refresh_status(db, col.owner_id, email, trigger="opened their Showcase")
    await db.refresh(col)
    if col.status != "ACTIVE":   # e.g. no visitor email - wake this one directly
        woke = await wake_no_response(db, col.owner_id, email) if email else []
        if not woke:
            col.status, col.inactive_reason, col.inactive_at = "ACTIVE", None, None
            woke = [col.id]
        await db.commit()
        await catch_up(db, woke)
    return True


async def agent_changed(db, col: Collection, new_status: str) -> None:
    """The agent switched a Showcase on or off by hand. Off = asleep 'agent'. Back on after a
    no-response sleep = the client's email count starts over (else the next digest would put it
    straight back to sleep). Doesn't commit."""
    if new_status == "INACTIVE":
        col.inactive_reason, col.inactive_at = AGENT, _now()
        return
    was = col.inactive_reason
    col.inactive_reason, col.inactive_at = None, None
    email = (col.visitor_email or "").strip().lower()
    if was == NO_RESPONSE and email:
        from app.services.client_email_service import MANUAL_OFF, _pref
        pref = await _pref(db, col.owner_id, email)
        if pref.manual not in MANUAL_OFF:
            pref.reset_at, pref.changed_at = _now(), _now()
            if pref.mode == "off":
                pref.mode, pref.reason = "daily", "Turned back on by you"
