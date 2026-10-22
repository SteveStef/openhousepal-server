"""Showcases that go to sleep (user, 10/22).

A client who ignored 3 weekly emails in a row (client_email_service.decide -> "off") stops getting
automatic emails, and - unless the agent is still minding them (starred the Showcase, or in the last
2 weeks opened it, opened the lead, marked them contacted, wrote a note or checked off a follow-up;
the agent still gets that Showcase's updates in their briefing) - their Showcases go to sleep: status INACTIVE with
inactive_reason "no_response". Asleep = no new matches, no emails, no agent alerts - it moves to the
Inactive tab on the agent's Showcases page. Nothing is deleted.

It wakes up by itself when the client shows up again - opens their Showcase link or opens/clicks an
old email - and catches up on the homes that came on the market while it slept. The agent can also
wake it. A Showcase the agent switched off wakes the same way when the client opens it: only
deleting a Showcase makes its link stop working (user, 10/22).

Only counts from the first delivered email: nothing is backdated (client_emails starts 10/15)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, select

from app.models.database import Collection

logger = logging.getLogger(__name__)

NO_RESPONSE = "no_response"
AGENT = "agent"
AGENT_WATCH = timedelta(days=14)   # the agent opened it this recently -> it stays awake


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _their_showcases(db, owner_id: str, email: str, status: str, reason: Optional[str] = None) -> list[Collection]:
    q = select(Collection).where(Collection.owner_id == owner_id, func.lower(func.trim(Collection.visitor_email)) == email,
                                 Collection.status == status)
    if reason:
        q = q.where(Collection.inactive_reason == reason)
    return list((await db.execute(q)).scalars().all())


def kept_awake(col: Collection, now: Optional[datetime] = None) -> bool:
    """The agent is still minding this one (user, 10/22): starred, or opened by the agent in the
    last AGENT_WATCH - e.g. a Showcase they set up and text the client about themselves. It never
    falls asleep for no response; the client's emails still slow down / stop as usual."""
    now = now or _now()
    looked = col.last_agent_dismissed_at   # set when the agent opens the Showcase (mark_collection_seen)
    if looked is not None and looked.tzinfo is None:
        looked = looked.replace(tzinfo=timezone.utc)
    return bool(col.favorited_at) or bool(looked and now - looked < AGENT_WATCH)


async def agent_working_lead(db, owner_id: str, email: str, now: Optional[datetime] = None) -> bool:
    """The agent did something with this person's lead in the last AGENT_WATCH (user, 10/22): opened
    the lead, marked them contacted, wrote a note, or checked off one of their follow-ups. Then
    none of their Showcases fall asleep."""
    from sqlalchemy import or_
    from app.models.database import AgentTask, Lead, LeadNote
    since = (now or _now()) - AGENT_WATCH
    lead_ids = list((await db.execute(select(Lead.id).where(
        Lead.owner_id == owner_id, func.lower(func.trim(Lead.email)) == email))).scalars().all())
    if not lead_ids:
        return False
    if (await db.execute(select(Lead.id).where(Lead.id.in_(lead_ids), or_(
            Lead.viewed_at >= since, Lead.last_contacted_at >= since)).limit(1))).first():
        return True
    if (await db.execute(select(LeadNote.id).where(LeadNote.lead_id.in_(lead_ids), LeadNote.author_id == owner_id,
                                                   LeadNote.created_at >= since).limit(1))).first():
        return True
    return (await db.execute(select(AgentTask.id).where(AgentTask.lead_id.in_(lead_ids), AgentTask.owner_id == owner_id,
                                                        AgentTask.completed_at >= since).limit(1))).first() is not None


async def sleep_for_no_response(db, owner_id: str, email: str) -> int:
    """Their ACTIVE Showcases with this agent go to sleep - except while the agent is minding them:
    working the lead (agent_working_lead) or the Showcase itself (kept_awake). Safe to call again
    later: one kept awake falls asleep once the agent stops minding it. Doesn't commit."""
    if await agent_working_lead(db, owner_id, email):
        return 0
    cols = [c for c in await _their_showcases(db, owner_id, email, "ACTIVE") if not kept_awake(c)]
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
    """The client opened a sleeping Showcase - whatever put it to sleep (no response, the agent's
    switch, their lead being deleted). Only deleting a Showcase makes it gone (user, 10/22). It
    wakes, catches up on new homes, their email count starts over and the agent is told. Commits."""
    from app.services.showcase_links import share_match
    col = (await db.execute(select(Collection).where(and_(
        share_match(share_token), Collection.is_public == True,  # noqa: E712
        Collection.status == "INACTIVE",
    )))).scalar_one_or_none()
    if col is None:
        return False
    if col.inactive_reason != NO_RESPONSE:
        await _wake_switched_off(db, col)
        return True
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


async def _wake_switched_off(db, col: Collection) -> None:
    """A Showcase the agent switched off (or that went off with a deleted lead) and the client came
    back to: on again, their automatic emails start over (an agent's own "emails off" for this
    person stays off), the agent gets a bell. Commits, then catches up on new homes."""
    from app.models.database import Notification
    from app.services.client_email_service import LEAD_DELETED, MANUAL_OFF, _first, _pref
    col.status, col.inactive_reason, col.inactive_at, col.updated_at = "ACTIVE", None, None, _now()
    email = (col.visitor_email or "").strip().lower()
    if email:
        pref = await _pref(db, col.owner_id, email)
        if pref.manual == LEAD_DELETED:
            pref.manual = None
        if pref.manual not in MANUAL_OFF:
            pref.reset_at, pref.changed_at = _now(), _now()
            pref.mode, pref.reason = "daily", "Back on - opened their Showcase"
    who = _first(col.visitor_name, email or "Your client")
    db.add(Notification(agent_id=col.owner_id, type="EMAIL_STATUS", reference_type="SHOWCASE_WOKE", reference_id=col.id,
                        title=f"{who} is back", collection_id=col.id, visitor_name=col.visitor_name or None,
                        message=f"{who} opened the Showcase you switched off, so it's back on and updating again.",
                        link=f"/showcases?showcase={col.id}", is_read=False, created_at=_now()))
    await db.commit()
    await catch_up(db, [col.id])


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
