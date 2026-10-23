"""
Rule-based, zero-LLM-cost insights for the AI Assistant home screen (admin-only) and the
Dashboard's Daily Digest panel (every user). Reuses the same lead/collection matching logic
as the Leads page (see ai_tools.py) so the numbers here always agree with what the agent sees
elsewhere in the app, and costs nothing to compute - no model call involved.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import DigestDismissal, OpenHouseEvent, User
from app.schemas.leads import LeadOut
from app.api.lead_routes import _fetch_leads, _to_lead_out, _fetch_owner_collections


async def get_insights(db: AsyncSession, current_user: User) -> List[dict]:
    insights: List[dict] = []

    leads = await _fetch_leads(db, current_user, status=None, represented=None, search=None, hidden=False)
    collections = await _fetch_owner_collections(db, current_user.id)
    leads_out = [_to_lead_out(l, collections) for l in leads]

    # Already-represented leads aren't this agent's to pursue, so they shouldn't compete for
    # attention with leads who still need outreach - matches the Leads page's own
    # unrepresented_first prioritization (see _sort_leads in lead_routes.py).
    attention = [l for l in leads_out if l.needs_attention and l.represented != 'yes']
    if attention:
        names = ", ".join((l.name or "An unnamed lead") for l in attention[:3])
        more = f" and {len(attention) - 3} more" if len(attention) > 3 else ""
        verb = "has" if len(attention) == 1 else "have"
        insights.append({
            "id": "needs_attention",
            "title": f"{len(attention)} lead{'s' if len(attention) != 1 else ''} need{'s' if len(attention) == 1 else ''} a look",
            "detail": f"{names}{more} {verb} activity you haven't seen yet.",
            "severity": "attention",
            "link": "/leads",
        })

    # Actionable, not just a count: name the specific lead most worth a text right now, with
    # their phone number in hand, since showcase_new_matches > 0 already means "new properties
    # the client hasn't seen yet" (see CollectionsService.count_new_properties).
    new_matches = sorted(
        [l for l in leads_out if l.showcase_new_matches > 0 and l.represented != 'yes'],
        key=lambda l: -l.showcase_new_matches,
    )
    if new_matches:
        top = new_matches[0]
        total = sum(l.showcase_new_matches for l in new_matches)
        contact = f" Text them at {top.phone}." if top.phone else " No phone number on file - check their lead page."
        rest = len(new_matches) - 1
        more = f" {rest} other lead{'s' if rest != 1 else ''} also {'has' if rest == 1 else 'have'} unseen matches." if rest > 0 else ""
        insights.append({
            "id": "new_matches",
            "title": f"{top.name or 'A lead'} hasn't seen {top.showcase_new_matches} new match{'es' if top.showcase_new_matches != 1 else ''} in their Showcase",
            "detail": f"{total} new match{'es' if total != 1 else ''} total.{contact}{more}",
            "severity": "info",
            "link": f"/leads/{top.id}",
        })

    now = datetime.now(timezone.utc)
    soon = (now + timedelta(days=3)).date()
    stmt = (
        select(OpenHouseEvent)
        .where(
            OpenHouseEvent.agent_id == current_user.id,
            OpenHouseEvent.is_deleted.is_(False),
            OpenHouseEvent.is_active.is_(True),
            OpenHouseEvent.scheduled_date.isnot(None),
            OpenHouseEvent.scheduled_date >= now.date(),
            OpenHouseEvent.scheduled_date <= soon,
        )
        .order_by(OpenHouseEvent.scheduled_date.asc())
        .limit(3)
    )
    result = await db.execute(stmt)
    upcoming = result.scalars().all()
    if upcoming:
        first = upcoming[0]
        extra = f" and {len(upcoming) - 1} more" if len(upcoming) > 1 else ""
        insights.append({
            "id": "upcoming_open_house",
            "title": "Open house coming up",
            "detail": f"{first.address or 'An open house'} on {first.scheduled_date.isoformat()}{extra}.",
            "severity": "info",
            "link": "/open-houses",
        })

    return insights


# LeadEvent.source_type -> human label used in the "today" feed below.
_EVENT_LABEL = {
    'open_house_visitor': 'signed in at an open house',
    'form_submission': 'filled out a form',
    'form_partial': 'started a form',
    'property_tour': 'requested a tour',
    'property_comment': 'left a comment on a property',
    'open_house_second_visit': 'requested a second visit',
    'open_house_disclosure': 'requested a disclosure',
    'property_page_inquiry': 'sent a message from a listing page',
    'property_page_tour_request': 'requested a tour from a listing page',
}

# collection_visitor is capture-time bookkeeping (an auto-created showcase), not something the
# visitor actually did - excluded from the "today" feed for the same reason _last_activity_at
# (lead_routes.py) excludes it from "Active: ...".
_TODAY_FEED_SOURCE_TYPES = set(_EVENT_LABEL.keys())


def _showcase_link(lead: LeadOut, open_tours: bool = False, focus_first: bool = False) -> str:
    if lead.attention_collection_id:
        link = f"/showcases?showcase={lead.attention_collection_id}"
        if open_tours:
            return f"{link}&open=tours"
        # A "new match" count has no single property id behind it (see showcase_new_matches -
        # it's a count, not a record), so the best we can do is land the agent on the first
        # card as currently sorted/displayed, scrolled into view, so they definitely see
        # something concrete instead of a bare collection page.
        if focus_first:
            return f"{link}&focus=first"
        return link
    return f"/leads/{lead.id}"


def _lead_attention_item(lead: LeadOut) -> Optional[dict]:
    """Picks the single most urgent reason this lead needs a look, in priority order, and
    returns the deep link straight to where it gets addressed - the same link a click on this
    lead in the Leads tab now routes to (see /leads/page.tsx's getLeadAttentionLink).

    id is "lead-{id}-{kind}", not just "lead-{id}" - so dismissing (see DigestDismissal) one
    reason for a lead doesn't also hide a later, different reason for that same lead."""
    name = lead.name or "An unnamed lead"

    # Both are unseen-by-agent flags (see lead_routes.py's _to_lead_out) - cleared once the
    # matching request is marked Sent/Scheduled on the Open Houses page, not a permanent "did this
    # ever happen" flag. is_disclosure prefers a real disclosure flag over second_visit rather than
    # the old last_event_source_type check, which silently stopped matching the instant any newer,
    # unrelated event happened on the lead.
    if lead.has_second_visit_request or lead.has_disclosure_request:
        is_disclosure = lead.has_disclosure_request
        kind = "disclosure" if is_disclosure else "second_visit"
        return {
            "id": f"lead-{lead.id}-{kind}",
            "kind": kind,
            "title": f"{name} needs a {'disclosure' if is_disclosure else 'second visit'} response",
            "detail": "Respond on the Open Houses page to keep this moving.",
            "severity": "urgent",
            "link": f"/open-houses?request={lead.pending_request_id}" if lead.pending_request_id else "/open-houses",
            "lead_name": lead.name,
        }

    if lead.has_tour_request:
        return {
            "id": f"lead-{lead.id}-tour_request",
            "kind": "tour_request",
            "title": f"{name} requested a tour",
            "detail": f"Text them at {lead.phone} to lock in a time." if lead.phone else "No phone on file - check their lead page.",
            "severity": "urgent",
            "link": _showcase_link(lead, open_tours=True),
            "lead_name": lead.name,
        }

    if lead.showcase_new_matches > 0:
        return {
            "id": f"lead-{lead.id}-new_match",
            "kind": "new_match",
            "title": f"{name} hasn't seen {lead.showcase_new_matches} new match{'es' if lead.showcase_new_matches != 1 else ''}",
            "detail": f"Text them at {lead.phone}." if lead.phone else "No phone on file - check their lead page.",
            "severity": "attention",
            "link": _showcase_link(lead, focus_first=True),
            "lead_name": lead.name,
        }

    if lead.has_liked_property or lead.has_disliked_property:
        liked = lead.has_liked_property
        kind = "liked" if liked else "disliked"
        return {
            "id": f"lead-{lead.id}-{kind}",
            "kind": kind,
            "title": f"{name} {'liked' if liked else 'disliked'} a property in their Showcase",
            "detail": "Worth a follow-up while it's fresh.",
            "severity": "attention",
            "link": _showcase_link(lead),
            "lead_name": lead.name,
        }

    if lead.has_commented:
        return {
            "id": f"lead-{lead.id}-comment",
            "kind": "comment",
            "title": f"{name} left a comment on a property",
            "detail": "See what they said in their Showcase.",
            "severity": "attention",
            "link": _showcase_link(lead),
            "lead_name": lead.name,
        }

    if lead.needs_attention:
        return {
            "id": f"lead-{lead.id}-new_lead",
            "kind": "new_lead",
            "title": f"{name} has new activity",
            "detail": lead.last_event_summary or "Take a look at their lead page.",
            "severity": "info",
            "link": f"/leads/{lead.id}",
            "lead_name": lead.name,
        }

    return None


async def _email_problem_items(db: AsyncSession, owner_id: str, leads, leads_by_id) -> List[dict]:
    """A client's email bounced or they marked it as spam (Mailgun webhooks) in the last 2 weeks:
    the agent needs to fix the address / reach them another way. Gone once they open the lead.
    Feeds the Dash's needs-attention list and the daily briefing's "waiting on you"."""
    from app.models.database import ClientEmail
    since = datetime.now(timezone.utc) - timedelta(days=14)
    rows = (await db.execute(select(ClientEmail).where(
        ClientEmail.owner_id == owner_id, ClientEmail.delivery.in_(("bounced", "spam")), ClientEmail.delivery_at >= since,
    ).order_by(ClientEmail.delivery_at.desc()))).scalars().all()
    by_email = {(l.email or "").strip().lower(): l for l in leads if l.email}
    items, seen = [], set()
    for r in rows:
        raw = by_email.get(r.recipient_email)
        if raw is None or raw.id in seen or (raw.viewed_at and r.delivery_at and raw.viewed_at > r.delivery_at):
            continue
        seen.add(raw.id)
        out = leads_by_id.get(raw.id)
        name = (out.name if out else raw.name) or r.recipient_email
        spam = r.delivery == "spam"
        items.append({
            "id": f"lead-{raw.id}-email_{r.delivery}-{r.id[:8]}",
            "kind": f"email_{r.delivery}",
            "title": f"{name} marked your email as spam" if spam else f"{name}'s email bounced",
            "detail": "No more emails will go to them - reach out another way." if spam
                      else (f"{r.delivery_note}. Check the address on their lead." if r.delivery_note else "Check the address on their lead."),
            "severity": "attention",
            "link": f"/leads/{raw.id}",
            "lead_name": name,
        })
    return items


async def get_daily_digest(db: AsyncSession, current_user: User) -> dict:
    """Powers the Dashboard's Daily Digest panel - a "needs attention" queue (one entry per
    lead, deep-linked straight to the thing to address) plus a chronological feed of what
    actually happened today. See DashDigestPanel.tsx."""
    leads = await _fetch_leads(db, current_user, status=None, represented=None, search=None, hidden=False)
    collections = await _fetch_owner_collections(db, current_user.id)
    leads_out = [_to_lead_out(l, collections) for l in leads]
    leads_by_id = {l.id: l for l in leads_out}

    dismissed_result = await db.execute(
        select(DigestDismissal.item_id).where(DigestDismissal.owner_id == current_user.id)
    )
    dismissed_ids = set(dismissed_result.scalars().all())

    # Already-represented leads aren't this agent's to pursue - matches get_insights above and
    # the Leads page's own unrepresented_first prioritization.
    severity_order = {"urgent": 0, "attention": 1, "info": 2}
    needs_attention = [
        item for item in (_lead_attention_item(l) for l in leads_out if l.represented != 'yes')
        if item is not None and item["id"] not in dismissed_ids
    ]
    needs_attention += [i for i in await _email_problem_items(db, current_user.id, leads, leads_by_id) if i["id"] not in dismissed_ids]
    needs_attention.sort(key=lambda i: severity_order.get(i["severity"], 3))

    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_pairs: List[tuple] = []
    for raw_lead in leads:
        lead_out = leads_by_id.get(raw_lead.id)
        if not lead_out:
            continue
        for event in (raw_lead.events or []):
            if event.source_type in _TODAY_FEED_SOURCE_TYPES and event.occurred_at and event.occurred_at >= start_of_day:
                today_pairs.append((lead_out, event))
    today_pairs.sort(key=lambda pair: pair[1].occurred_at, reverse=True)

    today: List[dict] = []
    for lead_out, event in today_pairs[:30]:
        name = lead_out.name or "Someone"
        label = _EVENT_LABEL.get(event.source_type, "had activity")
        open_tours = event.source_type == 'property_tour'
        is_showcase_event = event.source_type in ('property_tour', 'property_comment')
        link = _showcase_link(lead_out, open_tours=open_tours) if is_showcase_event else (
            (f"/open-houses?request={event.source_id}" if event.source_id else "/open-houses")
            if event.source_type in ('open_house_second_visit', 'open_house_disclosure')
            else f"/leads/{lead_out.id}"
        )
        today.append({
            "id": f"event-{event.id}",
            "kind": event.source_type,
            "title": f"{name} {label}",
            "detail": event.summary,
            "severity": "info",
            "link": link,
            "lead_name": lead_out.name,
            "occurred_at": event.occurred_at,
        })

    return {"needs_attention": needs_attention, "today": today, "generated_at": now}


async def dismiss_digest_item(db: AsyncSession, owner_id: str, item_id: str) -> None:
    """Idempotent - dismissing an already-dismissed item (e.g. a double-click) is a no-op,
    not a duplicate-key error, since (owner_id, item_id) is unique."""
    existing = await db.execute(
        select(DigestDismissal).where(DigestDismissal.owner_id == owner_id, DigestDismissal.item_id == item_id)
    )
    if existing.scalar_one_or_none():
        return
    db.add(DigestDismissal(owner_id=owner_id, item_id=item_id))
    await db.commit()
