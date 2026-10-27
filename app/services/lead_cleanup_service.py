"""What deleting a lead switches off (user, 10/21): every automatic email to that person and their
Showcase - so a deleted lead never keeps getting emails, and their Showcase stops updating.

10/24 (user): deleting the LEAD makes them disappear from everything - the Email Tracker (the
"You deleted their lead" hold stays on, but they're no longer listed), Activity, and their Showcase
is deleted (not just turned off). Deleting only their Showcase keeps the lead and the rest.

- Automatic emails: off for them on the Emails page ("You deleted their lead" - client_email_service
  LEAD_DELETED), which holds every future digest / Spotlight update.
- Waiting emails: every not-yet-sent email to them from this agent (scheduled invite, queued
  Showcase changes, digests) is CANCELLED now, not skipped later.
- Spotlight price / open house alerts they signed up for: unsubscribed (a new sign-up turns them on).
- Their Showcase(s): turned off (INACTIVE) - nothing is deleted, so the agent can turn one back on
  from Showcases, which also lets automatic emails go to them again.

Emails to the agent are never touched. The lead's still-open follow-ups are cleared by lead_routes.
Nothing here commits - it runs in the delete's own transaction."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, or_, select, update

from app.models.database import (Collection, Lead, LeadEvent, ScheduledEmail, SpotlightAlertSubscription, User,
                                 collection_properties)
from app.services import client_email_service as ce

# ScheduledEmail rows that haven't gone out yet: due (PENDING) or waiting for the digest (DIGEST).
_WAITING = ("PENDING", "DIGEST")
CANCEL_NOTE = "Not sent - you deleted their lead"


async def lead_showcases(db, owner_id: str, lead: Lead) -> list[Collection]:
    """The lead's Showcases: matched by email like the lead page does (lead_routes._match_collections),
    plus any their activity points to that has no email of its own (a phone-only sign-in)."""
    conds = []
    if lead.email:
        conds.append(func.lower(func.trim(Collection.visitor_email)) == lead.email)
    linked = select(LeadEvent.collection_id).where(LeadEvent.lead_id == lead.id, LeadEvent.collection_id.isnot(None))
    conds.append(Collection.id.in_(linked) & (func.coalesce(func.trim(Collection.visitor_email), "") == ""))
    return list((await db.execute(select(Collection).where(Collection.owner_id == owner_id, or_(*conds)))).scalars().all())


async def stop_everything(db, owner_id: str, lead: Lead) -> dict:
    """-> {"name", "emails_stopped", "emails_cancelled", "showcases_off", "spotlight_alerts_off"}."""
    now = datetime.now(timezone.utc)
    owner = await db.get(User, owner_id)
    me = ce._norm(owner.email if owner else None)
    email = ce._norm(lead.email)
    if email and email == me:
        email = ""  # the agent added themselves as a lead - never stop the agent's own emails

    showcases = await lead_showcases(db, owner_id, lead)
    col_ids = [c.id for c in showcases]
    # 10/24: deleting the LEAD removes them everywhere - their Showcase is deleted too (its homes
    # list, likes, comments, tours and its Activity alerts go with it). Deleting only a Showcase
    # (Showcases page) still keeps the lead and everything else.
    deleted = len(col_ids)

    cancelled = alerts_off = 0
    stopped = False
    if email:
        stopped = await ce.stop_for_deleted_lead(db, owner_id, email)
        vars_ = ScheduledEmail.template_variables
        mine = [ScheduledEmail.owner_id == owner_id, vars_["_owner_id"].astext == owner_id]
        if col_ids:
            mine.append(vars_["_collection_id"].astext.in_(col_ids))
        res = await db.execute(update(ScheduledEmail).where(
            func.lower(func.trim(ScheduledEmail.recipient_email)) == email, ScheduledEmail.status.in_(_WAITING), or_(*mine),
        ).values(status="CANCELLED", error_message=CANCEL_NOTE).execution_options(synchronize_session=False))
        cancelled = res.rowcount or 0
        res = await db.execute(update(SpotlightAlertSubscription).where(
            SpotlightAlertSubscription.owner_id == owner_id, func.lower(SpotlightAlertSubscription.email) == email,
            SpotlightAlertSubscription.unsubscribed_at.is_(None),
        ).values(unsubscribed_at=now).execution_options(synchronize_session=False))
        alerts_off = res.rowcount or 0
    activity = await _forget_activity(db, owner_id, lead, email, col_ids)
    if col_ids:
        # collection_properties has no ON DELETE rule; everything else hanging off a Showcase cascades.
        await db.execute(delete(collection_properties).where(collection_properties.c.collection_id.in_(col_ids)))
        await db.execute(delete(Collection).where(Collection.id.in_(col_ids), Collection.owner_id == owner_id)
                         .execution_options(synchronize_session=False))
    return {"name": lead.name, "emails_stopped": stopped, "emails_cancelled": cancelled,
            "showcases_deleted": deleted, "showcases_off": 0, "spotlight_alerts_off": alerts_off,
            "activity_removed": activity}


async def _forget_activity(db, owner_id: str, lead: Lead, email: str, col_ids: list[str]) -> int:
    """Their rows on the Activity page (the agent's notifications) - anything pointing at the lead,
    their sign-ins, their requests / form answers, their tours or their Showcase. The Showcase's own
    alerts also cascade when it's deleted. Doesn't commit."""
    from app.models.database import ClientEmailPref, Notification, OpenHouseVisitor, PropertyTour
    ids = {lead.id}
    for ev_id, src_id in (await db.execute(select(LeadEvent.id, LeadEvent.source_id).where(LeadEvent.lead_id == lead.id))).all():
        ids.add(ev_id)
        if src_id:
            ids.add(str(src_id))
    ids |= set((await db.execute(select(OpenHouseVisitor.id).where(OpenHouseVisitor.lead_id == lead.id))).scalars().all())
    if col_ids:
        ids |= set(col_ids)
        ids |= set((await db.execute(select(PropertyTour.id).where(PropertyTour.collection_id.in_(col_ids)))).scalars().all())
    if email:
        ids |= set((await db.execute(select(ClientEmailPref.id).where(ClientEmailPref.owner_id == owner_id, ClientEmailPref.email == email))).scalars().all())
    res = await db.execute(delete(Notification).where(Notification.agent_id == owner_id, Notification.reference_id.in_(list(ids)))
                           .execution_options(synchronize_session=False))
    removed = res.rowcount or 0
    if email:
        # "Jane opened your email" rows are keyed "<email>|<day>", not by the lead (owner, 10/26).
        res = await db.execute(delete(Notification).where(
            Notification.agent_id == owner_id, Notification.type == "EMAIL_ACTIVITY",
            Notification.reference_id.startswith(email + "|", autoescape=True),
        ).execution_options(synchronize_session=False))
        removed += res.rowcount or 0
    return removed


async def forget_orphan_email_activity(db) -> int:
    """One-time tidy (owner, 10/26): "opened your email" rows about someone who is no longer one of
    the agent's leads - deleted before that row was cleaned up with the lead. Commits."""
    from app.models.database import Notification
    rows = (await db.execute(select(Notification.id, Notification.agent_id, Notification.reference_id).where(
        Notification.type == "EMAIL_ACTIVITY", Notification.reference_type == "CLIENT_EMAIL_DAY"))).all()
    by_agent: dict[str, list[tuple[str, str]]] = {}
    for nid, agent_id, ref in rows:
        email = ce._norm((ref or "").split("|", 1)[0])
        if email:
            by_agent.setdefault(agent_id, []).append((nid, email))
    gone: list[str] = []
    for agent_id, items in by_agent.items():
        keep = await ce.listed_emails(db, agent_id, {e for _, e in items})
        gone += [nid for nid, e in items if e not in keep]
    if gone:
        await db.execute(delete(Notification).where(Notification.id.in_(gone)).execution_options(synchronize_session=False))
        await db.commit()
    return len(gone)


async def scheduled_forget_orphan_email_activity() -> None:
    import logging
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            n = await forget_orphan_email_activity(db)
        if n:
            logging.getLogger(__name__).info("Removed email-open notifications about deleted leads: %s", n)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("removing email-open notifications about deleted leads failed")


def deleted_message(name: Optional[str], done: dict) -> str:
    """The agent's confirmation: "Emails to Jane stopped and their Showcase was deleted." """
    who = ((name or "").strip().split(" ")[0] or "").title() or "this lead"
    n = done.get("showcases_deleted") or 0
    off = done.get("showcases_off") or 0
    showcase = ("their Showcase was deleted" if n == 1 else f"their {n} Showcases were deleted" if n
                else "their Showcase was turned off" if off == 1 else f"their {off} Showcases were turned off" if off else "")
    if done.get("emails_stopped") and showcase:
        return f"Emails to {who} stopped and {showcase}."
    if done.get("emails_stopped"):
        return f"Emails to {who} stopped."
    if showcase:
        return f"{who[0].upper() + who[1:]} was deleted and {showcase}."
    return f"{who[0].upper() + who[1:]} was deleted."
