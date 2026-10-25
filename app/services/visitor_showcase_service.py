"""The agent's Showcase switch for ONE visitor (Visitors window, user 10/25).

The kit decides by default (open_house_events.auto_showcase); open_house_visitors.showcase_override
beats it for that one guest:
- ON  (kit off, e.g. sitting your own listing - "this one family wants one"): the guest gets exactly
  what a sign-in with Showcases on gives them - the same Showcase build (create_showcase_and_invite:
  same homes, same preferences) and the same "your homes are ready" invite about an hour later
  (the agent's invite settings apply). Their open follow-up is re-pushed so its message and
  calendar event carry the Showcase pitch (the pitch is read live from their ACTIVE Showcase).
  Switched back on later: their Showcase is turned back on, and if the invite never went out it's
  queued again.
- OFF: a not-yet-sent invite is cancelled (switched on then off inside the hour = no email), and
  their Showcase is turned off the same way the Showcases page does (INACTIVE: no more updates or
  Showcase emails). Nothing is deleted - switching it on again brings it back.
Guests who said they have an agent never get a Showcase (same rule as the sign-in)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update

from app.models.database import Collection, OpenHouseEvent, OpenHouseVisitor, ScheduledEmail, User

logger = logging.getLogger(__name__)

INVITE_TEMPLATE = "visitor_confirmation"
CANCEL_NOTE = "Not sent - you switched off this visitor's Showcase"
_tasks: set = set()
# Visitor ids whose Showcase is being built in the background right now (single API process).
_building: set = set()


def has_agent(visitor: OpenHouseVisitor) -> bool:
    return (visitor.has_agent or "").upper() == "YES"


def kit_on(oh: Optional[OpenHouseEvent]) -> bool:
    return oh is None or oh.auto_showcase is not False


def effective_on(oh: Optional[OpenHouseEvent], visitor: OpenHouseVisitor) -> bool:
    """What the visitor's switch shows: their own setting, else the kit's."""
    if has_agent(visitor):
        return False
    if visitor.showcase_override is not None:
        return bool(visitor.showcase_override)
    return kit_on(oh)


def wants_showcase(oh: Optional[OpenHouseEvent], visitor: OpenHouseVisitor, from_similar_qr: bool = False) -> bool:
    """The sign-in path's "build one?" (before the has-agent / plan checks it already does)."""
    if visitor.showcase_override is False:
        return False
    return from_similar_qr or visitor.showcase_override is True or kit_on(oh)


async def visitor_showcase(db, owner_id: str, visitor: OpenHouseVisitor) -> Optional[Collection]:
    """Their Showcase with this agent (newest, any status) - matched by email like everywhere else."""
    email = (visitor.email or "").strip().lower()
    if not email:
        return None
    return (await db.execute(select(Collection).where(
        Collection.owner_id == owner_id, func.lower(func.trim(Collection.visitor_email)) == email,
    ).order_by(Collection.created_at.desc()).limit(1))).scalars().first()


def _invite_rows(owner_id: str, visitor: OpenHouseVisitor):
    return (ScheduledEmail.owner_id == owner_id, ScheduledEmail.template_name == INVITE_TEMPLATE,
            func.lower(func.trim(ScheduledEmail.recipient_email)) == (visitor.email or "").strip().lower())


async def pending_invite_at(db, owner_id: str, visitor: OpenHouseVisitor) -> Optional[datetime]:
    return (await db.execute(select(func.min(ScheduledEmail.scheduled_for)).where(
        *_invite_rows(owner_id, visitor), ScheduledEmail.status == "PENDING"))).scalar_one_or_none()


async def _invite_went_or_waiting(db, owner_id: str, visitor: OpenHouseVisitor) -> bool:
    return bool((await db.execute(select(func.count(ScheduledEmail.id)).where(
        *_invite_rows(owner_id, visitor), ScheduledEmail.status.in_(("PENDING", "SENT"))))).scalar_one())


async def set_showcase(db, owner: User, visitor: OpenHouseVisitor, oh: OpenHouseEvent, on: bool) -> dict:
    """Save the switch and make it so. Commits. The Showcase build itself (a few seconds: it pulls
    the matching homes) runs in the background, so the switch answers at once."""
    from app.services.collections_service import CollectionsService
    from app.services.open_house_service import OpenHouseService
    from app.services.tasks_service import resync_lead_followups

    visitor.showcase_override = bool(on)
    db.add(visitor)
    col = await visitor_showcase(db, owner.id, visitor)
    cancelled = 0
    build = False
    if on:
        if col is not None:
            if col.status != "ACTIVE":
                await CollectionsService.update_collection_status(db, col.id, owner.id, "ACTIVE")
            if visitor.id not in _building and not await _invite_went_or_waiting(db, owner.id, visitor):
                await db.commit()
                from app.models.database import collection_properties
                count = (await db.execute(select(func.count()).select_from(collection_properties)
                                          .where(collection_properties.c.collection_id == col.id))).scalar_one()
                await OpenHouseService.schedule_showcase_invite(
                    db, visitor, oh.id, {"share_token": col.share_token, "collection_id": col.id, "properties_added": count},
                    send_after=timedelta(hours=1))
        elif visitor.id not in _building:
            build = True
    else:
        res = await db.execute(update(ScheduledEmail).where(*_invite_rows(owner.id, visitor), ScheduledEmail.status == "PENDING")
                               .values(status="CANCELLED", error_message=CANCEL_NOTE).execution_options(synchronize_session=False))
        cancelled = res.rowcount or 0
        if col is not None and col.status == "ACTIVE":
            await CollectionsService.update_collection_status(db, col.id, owner.id, "INACTIVE")
    await db.commit()

    if build:
        _build_later(visitor.id, oh.id, owner.id, visitor.lead_id)
    elif visitor.lead_id:
        # The follow-up's message / calendar event read the Showcase live - re-push them now.
        resync_lead_followups(owner.id, visitor.lead_id)
    return {
        "showcase_on": bool(on),
        "building": build or visitor.id in _building,
        "showcase_id": col.id if col is not None else None,
        "invite_at": (await pending_invite_at(db, owner.id, visitor)) if on else None,
        "invites_cancelled": cancelled,
    }


def _build_later(visitor_id: str, oh_id: str, owner_id: str, lead_id: Optional[str]) -> None:
    """The sign-in's own Showcase build + invite (create_showcase_and_invite, ~1 hour out), on its
    own DB session after the switch has answered. Skipped if the switch went back off meanwhile."""
    async def run():
        from app.database import AsyncSessionLocal
        from app.schemas.open_house import HasAgent, OpenHouseFormSubmission
        from app.services.open_house_service import OpenHouseService
        from app.services.tasks_service import resync_lead_followups
        try:
            async with AsyncSessionLocal() as s:
                v = await s.get(OpenHouseVisitor, visitor_id)
                if v is None or v.showcase_override is not True or has_agent(v):
                    return
                if await visitor_showcase(s, owner_id, v) is not None:
                    return  # a double tap already built it
                form = OpenHouseFormSubmission(
                    full_name=v.full_name, email=v.email, phone=v.phone or "",
                    has_agent=HasAgent(v.has_agent) if v.has_agent in HasAgent._value2member_map_ else HasAgent.NO,
                    open_house_event_id=oh_id,
                )
                result = await OpenHouseService.create_showcase_and_invite(s, v, form, send_after=timedelta(hours=1))
                # Switched back off while it was building (a quick on -> off): the invite it just
                # queued is cancelled too, so no email goes out.
                await s.refresh(v)
                if v.showcase_override is not True:
                    await s.execute(update(ScheduledEmail).where(*_invite_rows(owner_id, v), ScheduledEmail.status == "PENDING")
                                    .values(status="CANCELLED", error_message=CANCEL_NOTE).execution_options(synchronize_session=False))
                    col = await visitor_showcase(s, owner_id, v)
                    if col is not None and col.status == "ACTIVE":
                        from app.services.collections_service import CollectionsService
                        await CollectionsService.update_collection_status(s, col.id, owner_id, "INACTIVE")
                    await s.commit()
                    return
                if result.get("success") and lead_id:
                    resync_lead_followups(owner_id, lead_id)
                    resync_lead_followups(owner_id, lead_id, delay=20)
        except Exception:
            logger.exception("visitor showcase build failed", extra={"visitor_id": visitor_id})

    try:
        t = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _building.add(visitor_id)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    t.add_done_callback(lambda _t: _building.discard(visitor_id))
