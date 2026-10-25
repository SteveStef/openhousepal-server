"""A lead's email / phone / name edited by the agent (user, 10/24): the change goes everywhere that
person is written down, and a new email address starts fresh.

- Their Showcase(s): the recipient (visitor_email / phone / name) - so updates, digests and invites
  go to the right address, and the Showcase still matches the lead.
- Their open house sign-in records (Guest Pass, Visitors list) and Showcase tour requests.
- Not-yet-sent emails to the old address (an invite still waiting, queued Showcase changes) - to
  the new address.
- Spotlight price / open house alerts they signed up for - moved to the new address.
- Email rules start over for the new address (nothing ignored yet, so no 3-not-opened sleep), and a
  Showcase that fell asleep because the OLD address never opened anything wakes up.

The "Send them a new invitation?" choice afterwards is send_invite() - the Showcase-from-an-open-
house invite (visitor_confirmation) or the normal Showcase invite (showcase_ready)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func, or_, select, update

from app.config.logging import get_logger
from app.models.database import (Collection, Lead, OpenHouseEvent, OpenHouseVisitor, PropertyTour, ScheduledEmail,
                                 SpotlightAlertSubscription, User, collection_properties)

logger = get_logger(__name__)

_WAITING = ("PENDING", "DIGEST")


def _norm(v: Optional[str]) -> str:
    return (v or "").strip().lower()


async def propagate(db, owner_id: str, lead: Lead, *, old_email: Optional[str], old_phone: Optional[str],
                    old_name: Optional[str]) -> dict:
    """Push the lead's (already edited) contact details out. Call before the commit; doesn't commit.
    -> {"showcases": n, "woken": [collection ids to catch up after the commit], "email_changed": bool}."""
    from app.services import client_email_service as ce
    from app.services import showcase_sleep
    from app.services.lead_cleanup_service import lead_showcases

    new_email, new_phone, new_name = _norm(lead.email), lead.phone, lead.name
    old_email = _norm(old_email)
    email_changed = bool(new_email) and new_email != old_email
    phone_changed = (new_phone or None) != (old_phone or None)
    name_changed = (new_name or None) != (old_name or None)
    if not (email_changed or phone_changed or name_changed):
        return {"showcases": 0, "woken": [], "email_changed": False}

    # Their Showcases, found the way the lead page finds them - by the OLD address.
    cols = await lead_showcases(db, owner_id, SimpleNamespace(id=lead.id, email=old_email or new_email))
    woken: list[str] = []
    for col in cols:
        if email_changed:
            col.visitor_email = new_email
            if col.status == "INACTIVE" and col.inactive_reason == showcase_sleep.NO_RESPONSE:
                # Asleep because the wrong address never opened anything - the right one starts fresh.
                col.status, col.inactive_reason, col.inactive_at = "ACTIVE", None, None
                woken.append(col.id)
        if phone_changed:
            col.visitor_phone = new_phone
        if name_changed and new_name and ((col.visitor_name or "").strip() in ("", (old_name or "").strip())):
            col.visitor_name = new_name
        col.updated_at = datetime.now(timezone.utc)

    visitors = (await db.execute(select(OpenHouseVisitor).where(OpenHouseVisitor.lead_id == lead.id))).scalars().all()
    for v in visitors:
        if email_changed:
            v.email = new_email
        if phone_changed and new_phone:
            v.phone = new_phone
        if name_changed and new_name:
            v.full_name = new_name

    if email_changed and old_email:
        col_ids = [c.id for c in cols]
        if col_ids:
            await db.execute(update(PropertyTour).where(PropertyTour.collection_id.in_(col_ids),
                                                        func.lower(PropertyTour.visitor_email) == old_email)
                             .values(visitor_email=new_email).execution_options(synchronize_session=False))
        vars_ = ScheduledEmail.template_variables
        mine = [ScheduledEmail.owner_id == owner_id, vars_["_owner_id"].astext == owner_id]
        if col_ids:
            mine.append(vars_["_collection_id"].astext.in_(col_ids))
        await db.execute(update(ScheduledEmail).where(
            func.lower(func.trim(ScheduledEmail.recipient_email)) == old_email, ScheduledEmail.status.in_(_WAITING), or_(*mine),
        ).values(recipient_email=new_email).execution_options(synchronize_session=False))
        await db.execute(update(SpotlightAlertSubscription).where(
            SpotlightAlertSubscription.owner_id == owner_id, func.lower(SpotlightAlertSubscription.email) == old_email,
        ).values(email=new_email).execution_options(synchronize_session=False))
        # The new address: a clean slate (a "you deleted their lead" hold on it, if any, is lifted).
        await ce.lift_lead_deleted(db, owner_id, new_email)
    return {"showcases": len(cols), "woken": woken, "email_changed": email_changed}


async def after_commit(owner_id: str, lead_id: str, result: dict) -> None:
    """Catch up woken Showcases and re-push the lead's open follow-ups (their calendar event shows
    the phone / email). Never raises."""
    try:
        from app.services.tasks_service import resync_lead_followups
        resync_lead_followups(owner_id, lead_id)
        if result.get("woken"):
            from app.database import AsyncSessionLocal
            from app.services import showcase_sleep
            async with AsyncSessionLocal() as db:
                await showcase_sleep.catch_up(db, result["woken"])
    except Exception:  # noqa: BLE001
        logger.warning("lead contact change follow-up failed", exc_info=True)


async def invite_options(db, owner_id: str, lead: Lead) -> dict:
    """What "Send them a new invitation?" can offer for this lead: their newest live Showcase, and
    whether it came from an open house (then the open-house invite is offered too)."""
    from app.services.lead_cleanup_service import lead_showcases
    cols = [c for c in await lead_showcases(db, owner_id, lead) if c.status == "ACTIVE"]
    if not cols:
        return {"collection_id": None, "open_house": False}
    col = max(cols, key=lambda c: c.created_at or datetime.min.replace(tzinfo=timezone.utc))
    return {"collection_id": col.id, "open_house": bool(col.original_open_house_event_id)}


async def send_invite(db, owner: User, lead: Lead, kind: str) -> bool:
    """Queue the chosen invite to the lead's (new) address, as if they just came in:
    kind "open_house" = the Showcase-from-an-open-house invite (visitor_confirmation), "showcase" =
    the normal Showcase invite (showcase_ready). Goes out through the usual email queue (and the
    agent's own mailbox when connected). -> True when queued."""
    from app.services.showcase_digest_service import queue_showcase_ready
    opts = await invite_options(db, owner.id, lead)
    cid = opts["collection_id"]
    if not cid or not lead.email:
        return False
    if kind == "showcase" or not opts["open_house"]:
        return await queue_showcase_ready(db, cid)
    col = await db.get(Collection, cid)
    oh = await db.get(OpenHouseEvent, col.original_open_house_event_id) if col else None
    if col is None or oh is None:
        return await queue_showcase_ready(db, cid)
    try:
        from app.services.agent_email_service import agent_public_phone
        from app.services.blacklist_service import BlacklistService
        from app.services.email_avatar_service import agent_email_vars
        from app.services.showcase_links import showcase_path
        from app.services.website.site_service import resolve_owner_public_base_url
        if await BlacklistService.is_blacklisted(db, lead.email):
            return False
        base = await resolve_owner_public_base_url(db, owner)
        link = f"{base.rstrip('/')}{await showcase_path(col, db=db)}"
        count = (await db.execute(select(func.count()).select_from(collection_properties)
                                  .where(collection_properties.c.collection_id == col.id))).scalar() or 0
        addr = ", ".join(x for x in [oh.address, oh.city, oh.state] if x)
        street = " ".join(str(oh.address or "").split(",")[0].split())
        name = lead.name or col.visitor_name or ""
        front = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "https://openhousepal.com")).rstrip("/")
        db.add(ScheduledEmail(
            recipient_email=lead.email,
            subject=f"Following up from the open house at {street}" if street else "Following up from the open house",
            template_name="visitor_confirmation",
            template_variables={
                "visitor_name": name, "first_name": name.strip().split(" ")[0].title(),
                "property_address": addr, "test_property_address": addr,
                "showcase_link": link, "properties_count": count,
                "agent_name": f"{owner.first_name or ''} {owner.last_name or ''}".strip(), "agent_email": owner.email or "",
                "agent_phone": await agent_public_phone(db, owner),
                "Unsub": f"{front}/unsubscribe?email={quote(lead.email, safe='')}",
                **(await agent_email_vars(db, owner)),
            },
            scheduled_for=datetime.now(timezone.utc), status="PENDING", owner_id=owner.id,
        ))
        await db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("open house re-invite failed", exc_info=True)
        await db.rollback()
        return False
