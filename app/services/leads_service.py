import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Lead, LeadEvent
from app.config.logging import get_logger

logger = get_logger(__name__)


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    value = email.strip().lower()
    return value or None


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits or None


async def find_lead_by_contact(db: AsyncSession, owner_id: str, email: Optional[str], phone: Optional[str]) -> Optional[Lead]:
    """
    Find (never create) a Lead by the same normalized-email matching used by upsert_lead. Used to
    connect a Lead to activity that doesn't flow through upsert_lead itself, e.g. a Collection
    (showcase) created/deleted outside the LeadEvent log.

    Matches by email only (not phone) - two different people can share a phone (household,
    assistant, front-desk line), and a phone-only match was silently merging unrelated leads
    into one contact.
    """
    norm_email = normalize_email(email)
    if not norm_email:
        return None

    result = await db.execute(
        select(Lead).where(Lead.owner_id == owner_id, Lead.email == norm_email).order_by(Lead.created_at.asc())
    )
    return result.scalars().first()


async def upsert_lead(
    db: AsyncSession,
    *,
    owner_id: str,
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    source_type: str,
    source_id: Optional[str],
    summary: str,
    occurred_at: datetime,
    represented: Optional[str] = None,
    # The person's newest answer wins over an earlier one (normally the first answer sticks) - e.g.
    # "I have an agent" at the open house, then "I don't" on the Similar Homes QR.
    override_represented: bool = False,
    property_address: Optional[str] = None,
    open_house_event_id: Optional[str] = None,
    collection_id: Optional[str] = None,
    form_id: Optional[str] = None,
    raw_snapshot: Optional[dict] = None,
    commit: bool = True,
) -> Optional[Lead]:
    """
    Find-or-create a Lead for this owner matching by normalized email only, then append a
    LeadEvent recording this touchpoint. Called from every place a contact-capturing record
    (open house visitor, form submission, showcase visitor, tour request, property comment) is
    created, so the Leads page stays a live merge of all sources without a separate sync job.

    Matching is email-only (not phone) - two different people can share a phone number, and
    merging on phone was silently combining unrelated contacts into one lead. A touchpoint with no
    email always creates its own new lead, even if its phone matches an existing one.
    """
    norm_email = normalize_email(email)
    norm_phone = normalize_phone(phone)

    if not norm_email and not norm_phone:
        return None

    lead: Optional[Lead] = None
    if norm_email:
        result = await db.execute(
            select(Lead).where(Lead.owner_id == owner_id, Lead.email == norm_email).order_by(Lead.created_at.asc())
        )
        lead = result.scalars().first()

    created_new = lead is None
    if lead:
        if not lead.name and name:
            lead.name = name
        if not lead.email and norm_email:
            lead.email = norm_email
        if not lead.phone and norm_phone:
            lead.phone = norm_phone
        if represented and (lead.represented == 'unknown' or override_represented):
            lead.represented = represented
        lead.updated_at = occurred_at
        db.add(lead)
    else:
        lead = Lead(
            owner_id=owner_id,
            name=name,
            email=norm_email,
            phone=norm_phone,
            represented=represented or 'unknown',
            status='new',
            tags=[],
            created_at=occurred_at,
        )
        db.add(lead)
        await db.flush()  # populate lead.id for the event below
        # A lead the agent deleted earlier is back (signed in again, filled a form...) - their
        # emails were stopped by the delete (lead_cleanup_service); a new lead gets them as normal.
        try:
            from app.services.client_email_service import lift_lead_deleted
            await lift_lead_deleted(db, owner_id, norm_email)
        except Exception as e:  # never let email plumbing break lead capture
            logger.warning(f"Could not lift deleted-lead email hold: {e}")

    event = LeadEvent(
        lead_id=lead.id,
        source_type=source_type,
        source_id=source_id,
        summary=summary,
        property_address=property_address,
        open_house_event_id=open_house_event_id,
        collection_id=collection_id,
        form_id=form_id,
        raw_snapshot=raw_snapshot,
        occurred_at=occurred_at,
    )
    db.add(event)
    await db.flush()  # populate event.id / lead.id for the CRM push below, regardless of commit mode

    if commit:
        try:
            await db.commit()
            await db.refresh(lead)
        except Exception as e:
            logger.error(f"Failed to commit lead upsert: {e}", exc_info=True)
            await db.rollback()
            return None

    # Forward this touchpoint to the agent's real CRM (FollowUpBoss / Lofty / etc.) if they
    # connected one. Fire-and-forget on its own session + timeout so a slow CRM endpoint can't
    # add latency to, or roll back, whatever captured this lead. No-op unless crm_push_enabled.
    # A Showcase made for someone who's already a lead (right after their sign-in or form, or by the
    # agent) isn't a new inquiry - the CRM already has them, so no second copy.
    try:
        if not (source_type == "collection_visitor" and not created_new):
            from app.services.crm_dispatch_service import schedule_push
            schedule_push(owner_id, lead.id, event.id)
    except Exception as e:  # never let CRM plumbing break lead capture
        logger.warning(f"Could not schedule CRM push: {e}")

    # Automatic "reach out" follow-up for touch points that warrant one (form fill, showing
    # request, portal lead...) - per-agent switchable, see tasks_service.AUTO_FOLLOWUP_RULES.
    # Only once the lead/event are committed, since it reads them on its own session.
    if commit:
        try:
            from app.services.tasks_service import schedule_followup_for_lead_event
            schedule_followup_for_lead_event(owner_id, lead.id, event.id, source_type)
        except Exception as e:
            logger.warning(f"Could not schedule auto follow-up: {e}")

    return lead


# Things a client does inside their Showcase (and a few other quiet signals) - logged on the lead's
# activity so the Leads page has the whole story. Unlike upsert_lead these never create a lead
# (the person is already one), never push to the agent's CRM and never make a follow-up (except a
# search change request - collections_service schedules that one itself).
SHOWCASE_ACTIVITY_TYPES = {
    "showcase_like", "showcase_dislike", "showcase_view", "showcase_refine", "showcase_search_changed",
    "showcase_emails_off", "showcase_emails_on",
}
OTHER_ACTIVITY_TYPES = {"email_unsubscribed", "review_left"}
# Seen-it signals: shown on the timeline and as "latest", but they don't flag the lead as needing
# the agent (a client browsing homes isn't something to act on - a like or a question is).
# Email tracking + the engagement rules (client_email_service) are history only - an open never
# makes a lead "need you".
QUIET_ACTIVITY_TYPES = {"showcase_view", "showcase_emails_on", "email_opened", "email_clicked",
                        "email_weekly", "email_paused", "email_resumed"}


async def log_activity(
    db: AsyncSession,
    *,
    owner_id: str,
    email: Optional[str],
    source_type: str,
    summary: str,
    source_id: Optional[str] = None,
    collection_id: Optional[str] = None,
    property_address: Optional[str] = None,
    raw_snapshot: Optional[dict] = None,
    occurred_at: Optional[datetime] = None,
    dedupe_hours: Optional[float] = None,
    lead: Optional[Lead] = None,
) -> Optional[LeadEvent]:
    """Add one activity row to an existing lead (matched by email). `dedupe_hours`: skip it when
    the same thing (type + source_id) was already logged that recently - e.g. one "Looked at 22
    Birch Ln" per home per half-day, not one per tap. Never raises - activity logging must never
    break the action that triggered it. Commits."""
    from datetime import timedelta, timezone as tz
    try:
        lead = lead or await find_lead_by_contact(db, owner_id, email, None)
        if lead is None:
            return None
        when = occurred_at or datetime.now(tz.utc)
        if dedupe_hours:
            recent = (await db.execute(
                select(LeadEvent.id).where(
                    LeadEvent.lead_id == lead.id, LeadEvent.source_type == source_type,
                    LeadEvent.source_id == source_id if source_id else LeadEvent.source_id.is_(None),
                    LeadEvent.occurred_at >= when - timedelta(hours=dedupe_hours),
                ).limit(1)
            )).scalar_one_or_none()
            if recent:
                return None
        event = LeadEvent(
            lead_id=lead.id, source_type=source_type, source_id=source_id, summary=summary[:500],
            property_address=property_address, collection_id=collection_id, raw_snapshot=raw_snapshot,
            occurred_at=when,
        )
        db.add(event)
        await db.commit()
        return event
    except Exception as e:
        logger.warning(f"Could not log lead activity ({source_type}): {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def log_showcase_activity(db: AsyncSession, collection_id: str, source_type: str, summary: str, *,
                                property_id: Optional[str] = None, raw_snapshot: Optional[dict] = None,
                                dedupe_hours: Optional[float] = None) -> Optional[LeadEvent]:
    """log_activity for something done inside a Showcase: finds the Showcase's person + the home's
    address. `summary` may hold "{home}" for the home's street ("Liked {home}"). Returns the row."""
    try:
        from app.models.database import Collection, Property
        collection = await db.get(Collection, collection_id)
        if collection is None or not collection.visitor_email:
            return None
        street = None
        if property_id:
            prop = await db.get(Property, property_id)
            if prop is not None:
                parts = [prop.street_address, prop.city]
                street = ", ".join(p for p in parts if p) or None
        if "{home}" in summary:
            summary = summary.replace("{home}", (street or "a home").split(",")[0])
        return await log_activity(
            db, owner_id=collection.owner_id, email=collection.visitor_email, source_type=source_type,
            summary=summary, source_id=property_id or collection_id, collection_id=collection_id,
            property_address=street, raw_snapshot=raw_snapshot, dedupe_hours=dedupe_hours,
        )
    except Exception as e:
        logger.warning(f"Could not log Showcase activity ({source_type}): {e}")
        return None


async def log_unsubscribe(db: AsyncSession, email: Optional[str]) -> None:
    """They unsubscribed from all OpenHousePal emails - on every agent's lead for that email, so
    the agent knows their Showcase updates have stopped reaching them."""
    norm = normalize_email(email)
    if not norm:
        return
    try:
        leads = (await db.execute(select(Lead).where(Lead.email == norm))).scalars().all()
        for lead in leads:
            await log_activity(db, owner_id=lead.owner_id, email=norm, lead=lead, source_type="email_unsubscribed",
                               summary="Unsubscribed from all emails - Showcase updates no longer reach them",
                               dedupe_hours=24)
    except Exception as e:
        logger.warning(f"Could not log unsubscribe: {e}")
