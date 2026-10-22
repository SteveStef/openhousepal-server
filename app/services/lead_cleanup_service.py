"""What deleting a lead switches off (user, 10/21): every automatic email to that person and their
Showcase - so a deleted lead never keeps getting emails, and their Showcase stops updating.

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

from sqlalchemy import func, or_, select, update

from app.models.database import Collection, Lead, LeadEvent, ScheduledEmail, SpotlightAlertSubscription, User
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
    turned_off = 0
    for col in showcases:
        if col.status == "ACTIVE":
            col.status, col.updated_at = "INACTIVE", now
            turned_off += 1

    cancelled = alerts_off = 0
    stopped = False
    if email:
        stopped = await ce.stop_for_deleted_lead(db, owner_id, email)
        col_ids = [c.id for c in showcases]
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
    return {"name": lead.name, "emails_stopped": stopped, "emails_cancelled": cancelled,
            "showcases_off": turned_off, "spotlight_alerts_off": alerts_off}


def deleted_message(name: Optional[str], done: dict) -> str:
    """The agent's confirmation: "Emails to Jane stopped and their Showcase was turned off." """
    who = ((name or "").strip().split(" ")[0] or "").title() or "this lead"
    n = done.get("showcases_off") or 0
    showcase = "their Showcase was turned off" if n == 1 else f"their {n} Showcases were turned off" if n else ""
    if done.get("emails_stopped") and showcase:
        return f"Emails to {who} stopped and {showcase}."
    if done.get("emails_stopped"):
        return f"Emails to {who} stopped."
    if showcase:
        return f"{who[0].upper() + who[1:]} was deleted and {showcase}."
    return f"{who[0].upper() + who[1:]} was deleted."
