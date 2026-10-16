import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, exists, func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.database import AgentTask, Collection, Lead, LeadEvent, LeadNote, OpenHouseEvent, OpenHouseVisitor, Property, User
from app.schemas.leads import (
    LeadDetailOut, LeadEventOut, LeadHandleRequest, LeadHideRequest, LeadMomentOut, LeadNoteCreate, LeadNoteOut, LeadNoteUpdate,
    LeadOpenHouseOut, LeadOut, LeadPropertyOut, LeadShowcaseOut, LeadSort, LeadSourceFilter, LeadStatus, LeadRepresented,
    LeadTodoOut, LeadUpdate,
)
from app.services.leads_service import QUIET_ACTIVITY_TYPES, normalize_email, normalize_phone
from app.services.collections_service import CollectionsService
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/leads", tags=["leads"])
logger = logging.getLogger(__name__)


def _categorize_source(source_type: str, raw_snapshot: Optional[dict]) -> LeadSourceFilter:
    if source_type == 'open_house_visitor':
        return 'open_house'
    if source_type == 'form_submission':
        form_type = (raw_snapshot or {}).get('form_type')
        if form_type == 'how_can_i_help':
            return 'website'
        if form_type == 'showcase_request':
            return 'showcase'
        return 'form'
    if source_type in ('open_house_disclosure', 'form_partial'):
        return 'form'
    if source_type in ('open_house_second_visit', 'property_tour', 'property_website_tour_request', 'property_page_tour_request'):
        return 'tour'
    if source_type in ('property_website_inquiry', 'portal_lead', 'property_page_inquiry', 'blog_inquiry'):
        return 'website'
    # property_comment, collection_visitor - actual engagement with an existing showcase
    return 'showcase'


def _origin_group(e: LeadEvent) -> str:
    """Where the person first came in, for the Leads page's "Came in from" filter."""
    st, snap = e.source_type, e.raw_snapshot or {}
    came = snap.get('came_from') if isinstance(snap.get('came_from'), dict) else {}
    if st in ('open_house_visitor', 'open_house_second_visit', 'open_house_disclosure'):
        return 'open_house'
    if st in ('form_submission', 'form_partial'):
        return 'showcase' if snap.get('form_type') == 'showcase_request' else 'forms'
    if st in ('collection_visitor', 'property_tour', 'property_comment') or st.startswith('showcase_'):
        return 'showcase'
    if st in ('property_website_inquiry', 'property_website_tour_request'):
        return 'listings'
    if st in ('property_page_inquiry', 'property_page_tour_request'):
        return 'website' if came.get('src') == 'site' else 'listings'
    if st == 'blog_inquiry':
        return 'website'
    if st == 'portal_lead':
        return 'portal'
    if st == 'review_left':
        return 'review'
    return 'other'


def _lead_property(events: List[LeadEvent]) -> Optional[LeadPropertyOut]:
    # events are ordered newest-first; use the most recent open-house event that still has a
    # linked OpenHouseEvent row (property metadata lives there, not on the lead/event itself).
    for event in events:
        oh = getattr(event, 'open_house_event', None)
        if event.source_type == 'open_house_visitor' and oh:
            return LeadPropertyOut(
                image=oh.cover_image_url,
                city=oh.city,
                price=oh.price,
                bedrooms=oh.bedrooms,
                bathrooms=oh.bathrooms,
            )
    return None


_DIRECTIONALS = {'N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW', 'NORTH', 'SOUTH', 'EAST', 'WEST'}
_HOUSE_NUMBER_RE = re.compile(r'^\s*(\d+[A-Za-z]?)\s+(.+)$')
_ZIP_RE = re.compile(r'\b(\d{5})\b')


def _parse_free_text_address(text: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """Best-effort parse of a free-text address (typed/autocompleted into a form's "Property
    Address" question - see GooglePlacesAutocomplete.tsx's formatAddressWithoutCountry) into
    (house_number, a distinctive street word, zip) - the only 3 tokens reliable enough to match
    against Property.street_address/zipcode without risking a false-positive match to a
    different house on a same-named street elsewhere. Returns None if the text doesn't parse
    cleanly enough (notably: no zip found) to match safely - the caller just shows no picture."""
    if not text:
        return None
    house_match = _HOUSE_NUMBER_RE.match(text)
    if not house_match:
        return None
    house_number = house_match.group(1)
    words = [w for w in re.split(r'[\s,]+', house_match.group(2)) if w]
    street_word = next((w for w in words if w.upper() not in _DIRECTIONALS and len(w) > 2), None)
    if not street_word:
        return None
    zip_match = _ZIP_RE.search(text)
    if not zip_match:
        return None
    return house_number, street_word, zip_match.group(1)


async def _batch_match_properties(db: AsyncSession, address_texts: List[str]) -> Dict[str, Property]:
    """Matches free-text addresses (e.g. from a form submission's "Property Address" question) to
    a real synced Property row, in one query for the whole batch rather than one per lead. Only a
    handful of leads at a time need this (those with no open-house-based photo already, see
    _lead_property), so this is called with a short, deduplicated list."""
    parsed_by_text = {text: parsed for text in set(address_texts) for parsed in [_parse_free_text_address(text)] if parsed}
    if not parsed_by_text:
        return {}
    conditions = [
        and_(
            Property.zipcode == zip_code,
            Property.street_address.ilike(f"{house_number}%"),
            Property.street_address.ilike(f"%{street_word}%"),
        )
        for house_number, street_word, zip_code in parsed_by_text.values()
    ]
    result = await db.execute(select(Property).where(or_(*conditions)))
    candidates = result.scalars().all()
    matches: Dict[str, Property] = {}
    for text, (house_number, street_word, zip_code) in parsed_by_text.items():
        addr_upper = house_number.upper()
        for prop in candidates:
            street = (prop.street_address or '').upper()
            if prop.zipcode == zip_code and street.startswith(addr_upper) and not street[len(addr_upper):len(addr_upper) + 1].isalnum() and street_word.upper() in street:
                matches[text] = prop
                break
    return matches


def _form_submission_address(lead: Lead) -> Optional[str]:
    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
    form_event = next((e for e in events if e.source_type == 'form_submission' and e.property_address), None)
    return form_event.property_address if form_event else None


async def _fill_missing_property_photos(db: AsyncSession, leads: List[Lead], lead_outs: List[LeadOut]) -> None:
    """A lead with no open-house-based photo (see _lead_property) still often named a specific
    property in a form submission (e.g. "What's My Home Worth?") - if that address matches a
    property we actually have synced, show its real photo instead of the generic form-icon
    placeholder. Best-effort: leaves lead_out.property untouched (still None) on no match.
    Skipped entirely on a No-MLS plan - the synced Property rows ARE the Bright MLS mirror."""
    if not leads:
        return
    from app.utils.auth import tier_has_mls
    owner = await db.get(User, leads[0].owner_id)
    if not owner or not tier_has_mls(owner.plan_tier):
        return
    pending: List[Tuple[LeadOut, str]] = []
    for lead, lead_out in zip(leads, lead_outs):
        if lead_out.property:
            continue
        address = _form_submission_address(lead)
        if address:
            pending.append((lead_out, address))
    if not pending:
        return
    matches = await _batch_match_properties(db, [addr for _, addr in pending])
    for lead_out, addr in pending:
        prop = matches.get(addr)
        if prop:
            lead_out.property = LeadPropertyOut(image=prop.img_src, city=prop.city, price=prop.price, bedrooms=prop.bedrooms, bathrooms=prop.bathrooms)


def _lead_open_houses(events: List[LeadEvent]) -> List[LeadOpenHouseOut]:
    seen = set()
    open_houses: List[LeadOpenHouseOut] = []
    for event in events:
        oh = getattr(event, 'open_house_event', None)
        if event.source_type != 'open_house_visitor' or not oh or oh.id in seen:
            continue
        seen.add(oh.id)
        open_houses.append(LeadOpenHouseOut(
            id=oh.id,
            address=oh.address,
            image=oh.cover_image_url,
            city=oh.city,
            visitor_count=1,
            visitorCount=1,
            is_deleted=bool(oh.is_deleted),
            created_at=oh.created_at,
        ))
    return open_houses


async def _fetch_owner_collections(db: AsyncSession, owner_id: str) -> List[Collection]:
    """
    All of an owner's showcases with a visitor attached, fetched once per request and matched
    to leads in Python (by normalized email/phone) rather than N+1 queries. Most showcases are
    auto-created from an open house "interested in similar properties" checkbox and never get a
    LeadEvent link, so this contact-based match is the only reliable way to connect the two.
    """
    result = await db.execute(
        select(Collection)
        .options(selectinload(Collection.properties), selectinload(Collection.property_interactions))
        .where(Collection.owner_id == owner_id, or_(Collection.visitor_email.isnot(None), Collection.visitor_phone.isnot(None)))
    )
    cols = list(result.scalars().unique().all())
    # When each home came in - the NEW count (CollectionsService.count_new_properties) needs it.
    if cols:
        from app.models.database import collection_properties
        added: dict = {}
        for r in (await db.execute(select(collection_properties.c.collection_id, collection_properties.c.property_id, collection_properties.c.added_at)
                                   .where(collection_properties.c.collection_id.in_([c.id for c in cols])))).all():
            added.setdefault(r.collection_id, {})[r.property_id] = r.added_at
        for c in cols:
            c._added_at = added.get(c.id, {})
    return cols


def _match_collections(lead: Lead, collections: List[Collection]) -> List[Collection]:
    # Email-only match, same reasoning as upsert_lead - a shared phone number shouldn't merge two
    # different people's showcase into one lead's record.
    matched = []
    for c in collections:
        c_email = normalize_email(c.visitor_email)
        if lead.email and c_email == lead.email:
            matched.append(c)
    return matched


# LeadEvent.source_type values that represent the visitor actually doing something with a
# showcase/property (commenting, requesting a tour) as opposed to merely being captured as a lead
# (open house sign-in, form submission, or a showcase being auto-created for them). The latter are
# creation-time bookkeeping, not activity - see _last_activity_at.
_ENGAGEMENT_EVENT_TYPES = {'property_comment', 'property_tour', 'property_website_inquiry', 'property_website_tour_request',
                           'property_page_inquiry', 'property_page_tour_request',
                           # inside their Showcase (leads_service.log_activity)
                           'showcase_like', 'showcase_dislike', 'showcase_view', 'showcase_refine', 'showcase_search_changed',
                           'review_left',
                           # a question or request they sent, like the Spotlight inquiry above
                           'blog_inquiry', 'open_house_second_visit', 'open_house_disclosure',
                           # clicked a link in your email (an open alone isn't reliable proof)
                           'email_clicked'}


def _last_activity_at(lead: Lead, events: List[LeadEvent], matched_collections: List[Collection]) -> Optional[datetime]:
    # "Active: ..." must reflect only things the VISITOR actually did, not how they were captured
    # as a lead in the first place. open_house_visitor (sign-in), form_submission, and
    # collection_visitor (a showcase auto-created for them, e.g. from the open house "interested in
    # similar properties" checkbox) all fire at capture time and are NOT proof of engagement - a
    # lead can have two such events (sign-in + auto-created showcase) fired milliseconds apart with
    # the visitor never touching either again. Counting any of those made every lead show
    # "Active: [signup date]" forever, even though the same lead's Showcase card correctly shows
    # blank when the visitor never opened it. Real signals are: an event type that's an actual
    # visitor action (_ENGAGEMENT_EVENT_TYPES) and Collection.last_visitor_activity_at, the same
    # "did a visitor actually open their showcase" signal the Showcases tab uses - so
    # Leads/Dashboard must match it exactly.
    candidates = [e.occurred_at for e in events if e.source_type in _ENGAGEMENT_EVENT_TYPES]
    for collection in matched_collections:
        if collection.last_visitor_activity_at:
            candidates.append(collection.last_visitor_activity_at)
    return max(candidates) if candidates else None


def _nice_summary(e: LeadEvent) -> str:
    """The activity line as stored ("Requested a showing - 22 BIRCH LN") -> with the home's city and
    normal casing ("Requested a showing - 22 Birch Ln, Wayne"); a Showcase comment also quotes what
    they said. Done at read time, so older activity reads the same way."""
    summary = e.summary or ""
    if e.property_address:
        from app.services.tasks_service import display_address
        street = e.property_address.split(",")[0].strip()
        oh = getattr(e, "open_house_event", None)
        if "," in e.property_address:
            short = display_address(e.property_address)[1]
        elif oh is not None and oh.city:
            short = display_address(street, oh.city, oh.state)[1]
        else:
            short = display_address(street)[0]
        full_addr = e.property_address.strip()
        if street and short and full_addr and full_addr.lower() in summary.lower():
            # the whole stored address is in the line ("505 PARKVIEW DR, WYNNEWOOD, PA 19096")
            summary = re.sub(re.escape(full_addr), short, summary, count=1, flags=re.I)
        elif street and short:
            summary = re.sub(re.escape(street) + r"(,\s*[A-Za-z .]+?)?(?=$|\s+in\b|[.:])", short, summary, count=1, flags=re.I)
    # Anything still in capitals at the end ("Showcase created - 840 HARRITON RD") -> normal casing.
    from app.services.tasks_service import _nice_case
    summary = re.sub(r" - ([0-9A-Z][0-9A-Z .,#'-]{3,})$", lambda m: " - " + _nice_case(m.group(1)) if m.group(1).isupper() else m.group(0), summary)
    content = ((e.raw_snapshot or {}).get("content") or "").strip() if e.source_type == "property_comment" else ""
    if content:
        summary = f"{summary}: \u201c{content[:120]}{'...' if len(content) > 120 else ''}\u201d"
    return summary


def _to_lead_out(lead: Lead, collections: Optional[List[Collection]] = None) -> LeadOut:
    events = lead.events or []
    last_event = events[0] if events else None
    earliest_event = events[-1] if events else None
    primary_source = _categorize_source(earliest_event.source_type, earliest_event.raw_snapshot) if earliest_event else 'website'
    matched_collections = _match_collections(lead, collections) if collections else []
    new_matches = sum(CollectionsService.count_new_properties(c) for c in matched_collections)
    # Only interactions on a property still actually IN the showcase - a like/dislike on a
    # property the sync (or the agent) later removed from the collection has nothing left to
    # click through to, so it should stop counting as a pending alert rather than sit stuck
    # forever with no way to clear agent_viewed_at (which normally happens by opening that
    # property card in the showcase management view - impossible once it's gone).
    all_interactions = [
        pi for c in matched_collections
        for pi in (c.property_interactions or [])
        if pi.property_id in {p.id for p in (c.properties or [])}
    ]
    # Unseen = something they DID since the agent last opened this lead - browsing homes alone
    # (QUIET_ACTIVITY_TYPES) doesn't count.
    loud = next((e for e in events if e.source_type not in QUIET_ACTIVITY_TYPES), None)
    first = _first_touch(events)
    return LeadOut(
        id=lead.id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        represented=lead.represented,
        status=lead.status,
        tags=lead.tags or [],
        notes=lead.notes,
        primary_source=primary_source,
        source_count=len({e.source_type for e in events}),
        last_event_summary=_nice_summary(last_event) if last_event else None,
        last_event_source_type=last_event.source_type if last_event else None,
        last_event_at=_last_activity_at(lead, events, matched_collections),
        last_event_occurred_at=last_event.occurred_at if last_event else None,
        # The home they reached out about - not one they just liked or looked at in their Showcase.
        property_address=next((e.property_address for e in events if e.property_address and not e.source_type.startswith('showcase_')), None),
        property=_lead_property(events),
        form_title=last_event.form.title if last_event and last_event.source_type in ('form_submission', 'form_partial') and last_event.form else None,
        open_houses=_lead_open_houses(events),
        showcase_deleted=bool(lead.showcase_deleted_at) and not matched_collections,
        showcase_new_matches=new_matches,
        # Only unseen-by-agent second-visit/disclosure requests count, same "cleared it by handling
        # the request" rule as has_liked_property/has_disliked_property below - agent_viewed_at is
        # set on this event once the agent marks the matching request Sent/Scheduled (see
        # open_houses_routes.py's complete_open_house_form_request). Used to unconditionally stay
        # true forever with no way to clear it once triggered.
        has_second_visit_request=any(e.source_type == 'open_house_second_visit' and e.agent_viewed_at is None for e in events),
        has_disclosure_request=any(e.source_type == 'open_house_disclosure' and e.agent_viewed_at is None for e in events),
        pending_request_id=next((e.source_id for e in sorted(events, key=lambda ev: ev.occurred_at, reverse=True)
                                 if e.source_type in ('open_house_second_visit', 'open_house_disclosure')
                                 and e.agent_viewed_at is None and e.source_id), None),
        # Only unseen-by-agent tour requests/comments count, same "cleared it by visiting the
        # showcase" rule as has_liked_property/has_disliked_property below - agent_viewed_at is
        # set in bulk on every property_tour/property_comment event for a collection the moment
        # the agent opens it (see collections_routes.py's mark-seen endpoint).
        has_tour_request=any(e.source_type in ('property_tour', 'property_website_tour_request', 'property_page_tour_request') and e.agent_viewed_at is None for e in events),
        has_commented=any(e.source_type == 'property_comment' and e.agent_viewed_at is None for e in events),
        # Only unseen-by-agent likes/dislikes count - agent_viewed_at is the same "cleared it in
        # their showcase management view" signal the Showcases tab's own NEW tag uses, so a
        # like/dislike the agent already looked at stops showing up here as a pending alert.
        has_liked_property=any(pi.liked and pi.agent_viewed_at is None for pi in all_interactions),
        has_disliked_property=any(pi.disliked and pi.agent_viewed_at is None for pi in all_interactions),
        needs_attention=bool(loud) and (lead.viewed_at is None or lead.viewed_at < loud.occurred_at),
        # A tour request/comment's own event.collection_id is a direct FK recorded at the moment
        # it happened - authoritative, and unaffected by whether the fuzzy email/phone match
        # below (matched_collections, e.g. thrown off by so much as a typo'd visitor_email) finds
        # anything. Only new-match/like/dislike alerts have no LeadEvent to source from at all
        # (they live on the Collection/PropertyInteraction directly), so those still fall back to
        # matched_collections.
        attention_collection_id=(
            next((e.collection_id for e in events if e.source_type in ('property_tour', 'property_comment') and e.collection_id), None)
            or (matched_collections[0].id if matched_collections else None)
        ),
        created_at=lead.created_at,
        last_contacted_at=lead.last_contacted_at,
        hidden_at=lead.hidden_at,
        # No activity on record (added by hand, or its history was removed): say when it was added
        # rather than leaving the "came in" / "latest" lines blank.
        origin=LeadMomentOut(type=first.source_type, label=_nice_summary(first), at=first.occurred_at, group=_origin_group(first)) if first
        else LeadMomentOut(type='added', label='Added to your leads', at=lead.created_at, group='other'),
        origin_group=_origin_group(first) if first else 'other',
        latest=LeadMomentOut(type=last_event.source_type, label=_nice_summary(last_event), at=last_event.occurred_at) if last_event
        else LeadMomentOut(type='added', label='No activity yet', at=lead.created_at),
        unsubscribed=any(e.source_type == 'email_unsubscribed' for e in events),
    )


def _first_touch(events: List[LeadEvent]) -> Optional[LeadEvent]:
    """How they came in: the earliest touch point (events are newest-first). Showcase browsing
    can't be first - it needs a lead already - but skip it anyway in case of clock ties."""
    for e in reversed(events):
        if not (e.source_type.startswith('showcase_') or e.source_type == 'email_unsubscribed'):
            return e
    return events[-1] if events else None


# Follow-up situation -> the plain to-do it stands for on the Leads page.
_FOLLOWUP_ACTION = {
    'disclosure_request': 'Send the disclosure',
    'second_visit_request': 'Book the second visit',
    'showing_request': 'Book the showing',
    'showcase_comment': 'Reply to their comment',
    'showcase_search_request': 'Update their Showcase search',
    'private_feedback': 'Reach out about their review',
    'how_can_we_help_seller': 'Seller lead - reach out',
}
# Waiting requests a follow-up already covers (don't list the same thing twice).
_COVERED_BY = {'disclosure': 'disclosure_request', 'second_visit': 'second_visit_request',
               'showing': 'showing_request', 'comment': 'showcase_comment', 'refine': 'showcase_search_request'}
_TOUR_TYPES = ('property_tour', 'property_website_tour_request', 'property_page_tour_request')


async def _open_tasks_by_lead(db: AsyncSession, owner_id: str, lead_ids: List[str]) -> Dict[str, List[AgentTask]]:
    if not lead_ids:
        return {}
    rows = (await db.execute(
        select(AgentTask).where(AgentTask.owner_id == owner_id, AgentTask.completed_at.is_(None), AgentTask.lead_id.in_(lead_ids))
        .order_by(func.coalesce(AgentTask.due_at, AgentTask.created_at).asc())
    )).scalars().all()
    out: Dict[str, List[AgentTask]] = {}
    for t in rows:
        out.setdefault(t.lead_id, []).append(t)
    return out


async def _enrich(db: AsyncSession, owner_id: str, leads: List[Lead], lead_outs: List[LeadOut],
                  collections: Optional[List[Collection]], with_came_from: bool = True) -> None:
    """The parts that need the database: what's waiting on the agent (open follow-ups + unhandled
    requests), and where they were before they first reached out."""
    from app.services.tasks_service import compute_bucket, followups_enabled
    from app.services.visit_source import describe_came_from
    # Follow-ups switched off: no follow-up chips on the rows (and requests show as "waiting on you"
    # again, since no follow-up covers them) - the switch promises it's hidden from Leads.
    tasks = await _open_tasks_by_lead(db, owner_id, [l.id for l in leads]) if followups_enabled(await db.get(User, owner_id)) else {}
    try:
        from app.services.client_email_service import people as email_people
        engagement = await email_people(db, owner_id)
    except Exception:  # noqa: BLE001 - the list must load even if this fails
        logger.warning("email engagement lookup failed", exc_info=True)
        engagement = {}
    from app.services.lead_warmth import warmth as lead_warmth
    for lead, lo in zip(leads, lead_outs):
        lo.email_engagement = engagement.get((lead.email or '').lower()) if lead.email else None
        try:
            w = lead_warmth(lead.events or [], lead.created_at)
            lo.warmth, lo.warmth_level, lo.heating_up, lo.warmth_reason = w['score'], w['level'], w['heating_up'], w['reason']
        except Exception:  # noqa: BLE001 - a score is never worth breaking the list
            logger.warning("warmth failed for lead %s", lead.id, exc_info=True)
    came_cache: Dict[str, Optional[str]] = {}
    for lead, lo in zip(leads, lead_outs):
        events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
        todos: List[LeadTodoOut] = []
        covered = set()
        for t in tasks.get(lead.id, []):
            covered.add(t.auto_source)
            label = _FOLLOWUP_ACTION.get(t.auto_source or '') or ('Reach out' if t.auto_source else t.title)
            bucket = compute_bucket(t)
            # A follow-up set for a later day is planned, not waiting on them yet.
            todos.append(LeadTodoOut(kind='followup', label=label, due_at=t.due_at, all_day=bool(t.all_day),
                                     bucket=bucket, task_id=t.id, href='/leads/tasks', urgent=bucket in ('overdue', 'today')))

        async def pending_link(types) -> Optional[str]:
            ev = next((e for e in events if e.source_type in types and e.agent_viewed_at is None), None)
            if ev is None:
                return None
            try:
                link = await _event_link(db, owner_id, ev)
            except Exception:
                link = None
            return (link or {}).get('href')

        if lo.has_disclosure_request and _COVERED_BY['disclosure'] not in covered:
            todos.append(LeadTodoOut(kind='disclosure', label='Send the disclosure', href=await pending_link(('open_house_disclosure',))))
        if lo.has_second_visit_request and _COVERED_BY['second_visit'] not in covered:
            todos.append(LeadTodoOut(kind='second_visit', label='Book the second visit', href=await pending_link(('open_house_second_visit',))))
        if lo.has_tour_request and _COVERED_BY['showing'] not in covered:
            todos.append(LeadTodoOut(kind='showing', label='Book the showing', href=await pending_link(_TOUR_TYPES)))
        if lo.has_commented and _COVERED_BY['comment'] not in covered:
            todos.append(LeadTodoOut(kind='comment', label='Reply to their comment', href=await pending_link(('property_comment',))))
        matched = _match_collections(lead, collections) if collections else []
        refine = next((c for c in matched if c.refine_request_message), None)
        if refine is not None and _COVERED_BY['refine'] not in covered:
            todos.append(LeadTodoOut(kind='refine', label='Update their Showcase search', href=f"/showcases?showcase={refine.id}"))
        if lo.has_liked_property:
            todos.append(LeadTodoOut(kind='likes', label='Liked homes - take a look', urgent=False,
                                     href=f"/showcases?showcase={lo.attention_collection_id}" if lo.attention_collection_id else None))
        lo.todos = todos
        lo.needs_you = any(t.urgent for t in todos)
        if with_came_from and lo.origin:
            first = _first_touch(events)
            raw = (first.raw_snapshot or {}).get('came_from') if first else None
            if isinstance(raw, dict):
                key = repr(sorted(raw.items())) + (first.property_address or '')
                if key not in came_cache:
                    try:
                        came_cache[key] = await describe_came_from(db, raw, owner_id, home=(first.property_address or '').split(',')[0] or None)
                    except Exception:
                        came_cache[key] = None
                lo.origin.came_from = came_cache[key]


async def _showcases_out(lead: Lead, collections: List[Collection]) -> List[LeadShowcaseOut]:
    out = []
    for c in _match_collections(lead, collections):
        in_it = {p.id for p in (c.properties or [])}
        pis = [pi for pi in (c.property_interactions or []) if pi.property_id in in_it]
        out.append(LeadShowcaseOut(
            id=c.id, name=c.name, share_token=c.share_token if c.is_public else None, home_count=len(in_it),
            liked=sum(1 for pi in pis if pi.liked), disliked=sum(1 for pi in pis if pi.disliked),
            new_matches=CollectionsService.count_new_properties(c), last_active_at=c.last_visitor_activity_at,
            emails_on=bool(c.notify_visitor), refine_message=c.refine_request_message,
        ))
    return out


async def _detail_out(db: AsyncSession, lead: Lead, owner_id: str) -> LeadDetailOut:
    collections = await _fetch_owner_collections(db, owner_id)
    lead_out = _to_lead_out(lead, collections)
    await _fill_missing_property_photos(db, [lead], [lead_out])
    await _enrich(db, owner_id, [lead], [lead_out], collections)
    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
    return LeadDetailOut(**lead_out.model_dump(), events=await _events_out(db, owner_id, events),
                         showcases=await _showcases_out(lead, collections))


async def _load_owned_lead(db: AsyncSession, lead_id: str, owner_id: str) -> Lead:
    result = await db.execute(
        select(Lead)
        .options(selectinload(Lead.events).selectinload(LeadEvent.open_house_event), selectinload(Lead.events).selectinload(LeadEvent.collection), selectinload(Lead.events).selectinload(LeadEvent.form), selectinload(Lead.lead_notes))
        .where(Lead.id == lead_id, Lead.owner_id == owner_id)
    )
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


async def _fetch_leads(db: AsyncSession, current_user: User, status: Optional[str], represented: Optional[str], search: Optional[str], hidden: bool = False) -> List[Lead]:
    stmt = (
        select(Lead)
        .options(selectinload(Lead.events).selectinload(LeadEvent.open_house_event), selectinload(Lead.events).selectinload(LeadEvent.collection), selectinload(Lead.events).selectinload(LeadEvent.form), selectinload(Lead.lead_notes))
        .where(Lead.owner_id == current_user.id)
    )
    if status and status != 'all':
        stmt = stmt.where(Lead.status == status)
    stmt = stmt.where(Lead.hidden_at.isnot(None) if hidden else Lead.hidden_at.is_(None))
    if represented and represented != 'all':
        stmt = stmt.where(Lead.represented == represented)
    if search:
        like = f"%{search.strip()}%"
        conds = [Lead.name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like)]
        digits = re.sub(r"\D", "", search)
        if len(digits) >= 3:  # "484-555-0177" / "(484) 555 0177" find a phone stored as 4845550177
            conds.append(func.regexp_replace(func.coalesce(Lead.phone, ''), r'\D', '', 'g').like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    result = await db.execute(stmt)
    return list(result.scalars().unique().all())


def _has_showcase_activity(l: LeadOut) -> bool:
    return bool(
        l.showcase_new_matches or l.has_liked_property or l.has_disliked_property or
        l.has_commented or l.has_tour_request or l.has_second_visit_request
    )


def _latest_at(l: LeadOut) -> datetime:
    return (l.latest.at if l.latest else None) or l.created_at


def _sort_leads(leads: List[LeadOut], sort: LeadSort) -> List[LeadOut]:
    if sort == 'activity':
        # Needs you first (most overdue follow-up first), then whoever did something most recently.
        def key(l: LeadOut):
            due = [t.due_at for t in l.todos if t.kind == 'followup' and t.due_at]
            first_due = min(due).timestamp() if due else float('inf')
            return (not l.needs_you, first_due if l.needs_you else 0, -_latest_at(l).timestamp())
        return sorted(leads, key=key)
    if sort == 'warmth':
        return sorted(leads, key=lambda l: (-l.warmth, not l.heating_up, -_latest_at(l).timestamp()))
    if sort == 'newest':
        return sorted(leads, key=lambda l: l.last_event_at or l.created_at, reverse=True)
    if sort == 'oldest':
        return sorted(leads, key=lambda l: l.created_at)
    if sort == 'name':
        return sorted(leads, key=lambda l: (l.name or '').lower())
    # unrepresented_first (default): unknown/no before yes (an agent shouldn't be chasing a
    # buyer who already has representation), then within that tier a lead with actual
    # Showcase engagement (a like, comment, tour request, new matches, ...) before one with
    # none, then most recently active first.
    return sorted(
        leads,
        key=lambda l: (l.represented == 'yes', not _has_showcase_activity(l), -(l.last_event_at or l.created_at).timestamp()),
    )


@router.get("", response_model=List[LeadOut])
async def list_leads(
    source: LeadSourceFilter = Query('all'),
    open_house_event_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    represented: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: LeadSort = Query('unrepresented_first'),
    limit: Optional[int] = Query(None, ge=1, le=500),
    hidden: bool = Query(False),
    needs_attention: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    leads = await _fetch_leads(db, current_user, status, represented, search, hidden)
    collections = await _fetch_owner_collections(db, current_user.id)
    lead_outs = [_to_lead_out(l, collections) for l in leads]
    await _fill_missing_property_photos(db, leads, lead_outs)
    await _enrich(db, current_user.id, leads, lead_outs, collections)
    if source == 'open_house':
        lead_outs = [l for l in lead_outs if l.open_houses]
    elif source != 'all':
        lead_outs = [l for l in lead_outs if l.primary_source == source]
    if open_house_event_id:
        lead_outs = [l for l in lead_outs if any(oh.id == open_house_event_id for oh in l.open_houses)]
    # needs_attention (unseen activity since the agent last opened this lead) is computed per-lead
    # in _to_lead_out, not a DB column - status='new' alone is a poor stand-in for it, since a lead
    # sits in 'new' status forever unless the agent manually reclassifies it, even after it's been
    # read and fully handled.
    if needs_attention is not None:
        lead_outs = [l for l in lead_outs if l.needs_attention == needs_attention]
    lead_outs = _sort_leads(lead_outs, sort)
    if limit:
        lead_outs = lead_outs[:limit]
    return lead_outs


FORMULA_START = ('=', '+', '-', '@', chr(9), chr(13))


class _SafeCsv:
    """Anyone can type their own name into a public form; a cell starting with = + - @ would run as
    a formula when the agent opens the file in Excel/Sheets, so those get a leading apostrophe."""
    def __init__(self, w):
        self._w = w

    def writerow(self, row):
        self._w.writerow([("'" + c) if isinstance(c, str) and c[:1] in FORMULA_START else c for c in row])


@router.get("/export.csv")
async def export_leads_csv(
    source: LeadSourceFilter = Query('all'),
    open_house_event_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    represented: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: LeadSort = Query('unrepresented_first'),
    hidden: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    leads = await _fetch_leads(db, current_user, status, represented, search, hidden)
    collections = await _fetch_owner_collections(db, current_user.id)
    lead_outs = [_to_lead_out(l, collections) for l in leads]
    await _enrich(db, current_user.id, leads, lead_outs, collections)
    if source == 'open_house':
        lead_outs = [l for l in lead_outs if l.open_houses]
    elif source != 'all':
        lead_outs = [l for l in lead_outs if l.primary_source == source]
    if open_house_event_id:
        lead_outs = [l for l in lead_outs if any(oh.id == open_house_event_id for oh in l.open_houses)]
    lead_outs = _sort_leads(lead_outs, sort)
    return _leads_csv(lead_outs)


# Same words as the Leads page's "Came in from" filter (lib/leadActivity.ts ORIGIN_META).
_ORIGIN_LABELS = {'open_house': 'Open houses', 'showcase': 'Showcases', 'forms': 'Forms', 'listings': 'Spotlights & listings',
                  'website': 'Website & blog', 'portal': 'Zillow & portals', 'review': 'Reviews', 'other': 'Other'}


class LeadExportRequest(BaseModel):
    # The leads on screen, in the order shown (the page filters and sorts itself).
    ids: List[str]
    hidden: bool = False


@router.post("/export.csv")
async def export_these_leads_csv(
    body: LeadExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """"Export" on the Leads page downloads exactly what's on screen (tab, search, filters, sort)."""
    leads = await _fetch_leads(db, current_user, None, None, None, False)
    if body.hidden:
        leads += await _fetch_leads(db, current_user, None, None, None, True)
    collections = await _fetch_owner_collections(db, current_user.id)
    lead_outs = [_to_lead_out(l, collections) for l in leads]
    await _enrich(db, current_user.id, leads, lead_outs, collections)
    by_id = {l.id: l for l in lead_outs}
    return _leads_csv([by_id[i] for i in body.ids if i in by_id])


def _leads_csv(lead_outs) -> StreamingResponse:
    buffer = io.StringIO()
    writer = _SafeCsv(csv.writer(buffer))
    writer.writerow(['Name', 'Email', 'Phone', 'Came In From', 'Came In', 'Status', 'Represented', 'Property', 'Tags', 'Created At',
                     'Last Contacted', 'Latest Activity', 'Latest Activity At', 'Needs'])
    for lead in lead_outs:
        writer.writerow([
            lead.name or '', lead.email or '', lead.phone or '', _ORIGIN_LABELS.get(lead.origin_group or 'other', 'Other'), lead.origin.label if lead.origin else '',
            lead.status, lead.represented, lead.property_address or '', ', '.join(lead.tags), lead.created_at.isoformat(),
            lead.last_contacted_at.isoformat() if lead.last_contacted_at else '',
            lead.latest.label if lead.latest else '', lead.latest.at.isoformat() if lead.latest else '',
            '; '.join(t.label for t in lead.todos if t.urgent),
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


@router.get("/open-houses/history", response_model=List[LeadOpenHouseOut])
async def list_open_house_history(
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    guest_counts = (
        select(
            LeadEvent.open_house_event_id.label("open_house_event_id"),
            func.count(func.distinct(LeadEvent.lead_id)).label("guest_count"),
        )
        .join(Lead, Lead.id == LeadEvent.lead_id)
        .where(
            Lead.owner_id == current_user.id,
            Lead.hidden_at.is_(None),
            LeadEvent.source_type == 'open_house_visitor',
            LeadEvent.open_house_event_id.isnot(None),
        )
        .group_by(LeadEvent.open_house_event_id)
        .subquery()
    )
    stmt = (
        select(OpenHouseEvent, guest_counts.c.guest_count)
        .join(guest_counts, guest_counts.c.open_house_event_id == OpenHouseEvent.id)
        .where(
            OpenHouseEvent.agent_id == current_user.id,
            guest_counts.c.guest_count > 0,
        )
    )
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(OpenHouseEvent.address.ilike(like), OpenHouseEvent.city.ilike(like)))
    stmt = stmt.order_by(OpenHouseEvent.created_at.desc()).limit(50)
    result = await db.execute(stmt)
    return [
        LeadOpenHouseOut(
            id=oh.id,
            address=oh.address,
            image=oh.cover_image_url,
            city=oh.city,
            visitor_count=int(visitor_count or 0),
            visitorCount=int(visitor_count or 0),
            is_deleted=bool(oh.is_deleted),
            created_at=oh.created_at,
        )
        for oh, visitor_count in result.all()
        if int(visitor_count or 0) > 0
    ]


async def _event_link(db: AsyncSession, owner_id: str, e: LeadEvent) -> Optional[dict]:
    """Where one activity row is handled - the exact request / submission / sign-in / home, never
    just the section it lives in. Destination pages open + highlight the item from these params:
    /open-houses?request=, /open-houses/visitors/<oh>?visitor=, /studio/forms?...&submission=,
    /showcases?showcase=&property= / &open=tours."""
    st, sid = e.source_type, e.source_id
    if st in ('open_house_second_visit', 'open_house_disclosure'):
        label = 'Send Disclosure' if st == 'open_house_disclosure' else 'Schedule Visit'
        return {"href": f"/open-houses?request={sid}" if sid else "/open-houses", "label": label}
    if st == 'open_house_visitor':
        if not e.open_house_event_id:
            return {"href": "/open-houses", "label": "View Open House"}
        return {"href": f"/open-houses/visitors/{e.open_house_event_id}" + (f"?visitor={sid}" if sid else ""), "label": "View Sign-in"}
    if st == 'form_submission' and e.form_id:
        return {"href": f"/studio/forms?tab=results&formId={e.form_id}" + (f"&submission={sid}" if sid else ""), "label": "View Submission"}
    if st == 'form_partial' and e.form_id:
        return {"href": f"/studio/forms?tab=results&formId={e.form_id}&view=unfinished", "label": "View Unfinished Form"}
    if st == 'property_tour' and e.collection_id:
        return {"href": f"/showcases?showcase={e.collection_id}&open=tours", "label": "View Tour Request"}
    if st == 'property_comment' and e.collection_id:
        from app.models.database import PropertyComment
        comment = await db.get(PropertyComment, sid) if sid else None
        prop = f"&property={comment.property_id}" if comment is not None and comment.property_id else ""
        return {"href": f"/showcases?showcase={e.collection_id}{prop}" + ("&comments=1" if prop else ""), "label": "View Comment"}
    if st == 'collection_visitor' and e.collection_id:
        return {"href": f"/showcases?showcase={e.collection_id}", "label": "View Showcase"}
    if st.startswith('showcase_') and e.collection_id:
        # A like / pass / look opens that home in their Showcase; the rest open the Showcase itself.
        if st in ('showcase_like', 'showcase_dislike', 'showcase_view') and sid and sid != e.collection_id:
            return {"href": f"/showcases?showcase={e.collection_id}&property={sid}", "label": "View Home"}
        return {"href": f"/showcases?showcase={e.collection_id}", "label": "View Showcase"}
    if st in ('property_website_inquiry', 'property_website_tour_request') and sid:
        # No in-app screen lists Spotlight inquiries - the home they asked about, on its property
        # details page (the same page a Similar Homes QR opens; never the Spotlight itself).
        from app.models.database import PropertyWebsite
        from app.services import property_website_service as pws
        site = await db.get(PropertyWebsite, sid)
        if site is not None and site.listing_key:
            owner = await db.get(User, owner_id)
            info = await pws.describe_listing_link(db, owner, site.listing_key, None, skip_spotlight=True) if owner else {}
            if info.get("url"):
                return {"href": info["url"], "label": "View Listing", "external": True}
        if site is not None:
            # A manual (non-MLS) listing has no property details page - the agent's own editor
            # for that Spotlight is the one screen about this home.
            return {"href": f"/studio/property-sites/{site.id}", "label": "Open Spotlight"}
    if st == 'review_left' and sid:
        return {"href": f"/studio/reviews?review={sid}", "label": "View Review"}
    if st == 'blog_inquiry' and sid:
        return {"href": f"/studio/blogs/{sid}", "label": "View Post"}
    if st in ('property_page_inquiry', 'property_page_tour_request'):
        listing_key = (e.raw_snapshot or {}).get("listing_key") or sid
        if listing_key:
            import os
            base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
            return {"href": f"{base}/property/{listing_key}/{owner_id}", "label": "View Listing", "external": True}
    return None


# Touch points that can only happen inside the lead's own Showcase.
_SHOWCASE_ORIGIN = {
    "property_tour": "Requested it from their Showcase",
    "property_comment": "Commented in their Showcase",
}


async def _events_out(db: AsyncSession, owner_id: str, events: List[LeadEvent]) -> List[LeadEventOut]:
    out = []
    for e in events:
        item = LeadEventOut.model_validate(e)
        item.summary = _nice_summary(e)
        item.handled = e.agent_viewed_at is not None
        try:
            item.link = await _event_link(db, owner_id, e)
        except Exception:
            logger.warning("activity link failed for event %s", e.id, exc_info=True)
        try:
            from app.services.visit_source import describe_came_from
            snap = e.raw_snapshot or {}
            if "came_from" in snap:
                home = (e.property_address or "").split(",")[0] or None
                item.came_from = await describe_came_from(db, snap.get("came_from"), owner_id, home=home)
            elif e.source_type in _SHOWCASE_ORIGIN:
                item.came_from = _SHOWCASE_ORIGIN[e.source_type]
        except Exception:
            logger.warning("came-from lookup failed for event %s", e.id, exc_info=True)
        out.append(item)
    return out


async def _clear_open_followups(db: AsyncSession, lead_id: str, owner_id: str) -> List[str]:
    """A lead you're done with (hidden or deleted) shouldn't keep reminding you: its still-open
    follow-ups go, and come off the calendar (after commit - see _unsync). Done ones stay as history."""
    open_tasks = (await db.execute(select(AgentTask.id).where(
        AgentTask.owner_id == owner_id, AgentTask.lead_id == lead_id, AgentTask.completed_at.is_(None)))).scalars().all()
    for tid in open_tasks:
        await db.delete(await db.get(AgentTask, tid))
    return list(open_tasks)


def _unsync(owner_id: str, task_ids: List[str]) -> None:
    from app.services import calendar_sync_service
    for tid in task_ids:
        calendar_sync_service.unsync_task_bg(owner_id, tid)


async def _hide_owned_lead(db: AsyncSession, lead_id: str, owner_id: str) -> LeadDetailOut:
    lead = await _load_owned_lead(db, lead_id, owner_id)
    return await _hide_lead_record(db, lead, owner_id)


async def _hide_lead_record(db: AsyncSession, lead: Lead, owner_id: str) -> LeadDetailOut:
    lead.hidden_at = datetime.now(timezone.utc)
    db.add(lead)
    cleared = await _clear_open_followups(db, lead.id, owner_id)
    await db.commit()
    _unsync(owner_id, cleared)
    return await _detail_out(db, await _load_owned_lead(db, lead.id, owner_id), owner_id)


async def _find_owned_lead_for_hide(db: AsyncSession, body: LeadHideRequest, owner_id: str) -> Optional[Lead]:
    if body.lead_id:
        result = await db.execute(
            select(Lead)
            .options(selectinload(Lead.events).selectinload(LeadEvent.open_house_event), selectinload(Lead.events).selectinload(LeadEvent.collection), selectinload(Lead.events).selectinload(LeadEvent.form), selectinload(Lead.lead_notes))
            .where(Lead.id == body.lead_id, Lead.owner_id == owner_id)
        )
        lead = result.scalar_one_or_none()
        if lead:
            return lead

    # Email-only match, same reasoning as upsert_lead - a shared phone number shouldn't resolve to
    # a different person's lead.
    email = normalize_email(body.email)
    if not email:
        return None

    stmt = (
        select(Lead)
        .options(selectinload(Lead.events).selectinload(LeadEvent.open_house_event), selectinload(Lead.events).selectinload(LeadEvent.collection), selectinload(Lead.events).selectinload(LeadEvent.form), selectinload(Lead.lead_notes))
        .where(Lead.owner_id == owner_id, Lead.email == email)
        .order_by(Lead.created_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().first()


@router.post("/hide", response_model=LeadDetailOut)
async def hide_lead_by_contact(
    body: LeadHideRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    lead = await _find_owned_lead_for_hide(db, body, current_user.id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return await _hide_lead_record(db, lead, current_user.id)


@router.patch("/hide/{lead_id}", response_model=LeadDetailOut)
async def hide_lead_by_action_path(
    lead_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    return await _hide_owned_lead(db, lead_id, current_user.id)


@router.delete("/{lead_id}")
async def delete_lead(
    lead_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    lead = await _load_owned_lead(db, lead_id, current_user.id)
    # Their still-open follow-ups would otherwise linger as "Follow up with ..." with no person
    # behind them (and stay on the agent's calendar). Done ones are just history - kept, unlinked.
    open_tasks = await _clear_open_followups(db, lead_id, current_user.id)
    await db.delete(lead)
    await db.commit()
    _unsync(current_user.id, open_tasks)
    return {"success": True}


@router.get("/{lead_id}", response_model=LeadDetailOut)
async def get_lead(
    lead_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    lead = await _load_owned_lead(db, lead_id, current_user.id)
    lead.viewed_at = datetime.now(timezone.utc)
    db.add(lead)
    await db.commit()
    return await _detail_out(db, lead, current_user.id)


@router.post("/{lead_id}/handled", response_model=LeadDetailOut)
async def mark_request_handled(
    lead_id: str,
    body: LeadHandleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The agent dealt with one kind of waiting request (booked the showing, replied to the comment,
    looked at the liked homes...) - clears it from the lead's to-dos. Only the request itself: an
    open follow-up is checked off on its own card."""
    lead = await _load_owned_lead(db, lead_id, current_user.id)
    now = datetime.now(timezone.utc)
    types = {
        'disclosure': ('open_house_disclosure',), 'second_visit': ('open_house_second_visit',),
        'showing': _TOUR_TYPES, 'comment': ('property_comment',),
    }.get(body.kind)
    if types:
        for e in lead.events or []:
            if e.source_type in types and e.agent_viewed_at is None:
                e.agent_viewed_at = now
    elif body.kind in ('likes', 'refine'):
        collections = _match_collections(lead, await _fetch_owner_collections(db, current_user.id))
        for c in collections:
            if body.kind == 'refine':
                c.refine_request_message = None
            else:
                for pi in c.property_interactions or []:
                    if (pi.liked or pi.disliked) and pi.agent_viewed_at is None:
                        pi.agent_viewed_at = now
    else:
        raise HTTPException(status_code=400, detail="Unknown request kind")
    await db.commit()
    return await _detail_out(db, await _load_owned_lead(db, lead_id, current_user.id), current_user.id)


@router.patch("/{lead_id}", response_model=LeadDetailOut)
async def update_lead(
    lead_id: str,
    body: LeadUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    lead = await _load_owned_lead(db, lead_id, current_user.id)

    if body.name is not None:
        lead.name = body.name.strip() or None
    if body.email is not None:
        # Same normalization upsert_lead uses (lowercased/trimmed) - keeps this lead matchable by
        # future activity under the corrected address instead of silently forking into a duplicate.
        new_email = normalize_email(body.email)
        if new_email and new_email != lead.email:
            other = (await db.execute(select(Lead.name).where(
                Lead.owner_id == current_user.id, Lead.email == new_email, Lead.id != lead.id))).first()
            if other:
                raise HTTPException(status_code=409, detail=f"{other[0] or 'Another lead'} already has that email. Open that lead instead of copying the email here.")
        lead.email = new_email
    if body.phone is not None:
        lead.phone = normalize_phone(body.phone)
    if body.status is not None:
        lead.status = body.status
        if body.status not in ('hidden', 'archived'):
            lead.hidden_at = None
    if body.represented is not None:
        lead.represented = body.represented
    if body.tags is not None:
        lead.tags = body.tags
    if body.notes is not None:
        lead.notes = body.notes
    if body.mark_contacted:
        lead.last_contacted_at = datetime.now(timezone.utc)
        if lead.status == 'new':
            lead.status = 'contacted'

    db.add(lead)
    await db.commit()
    # Re-fetch (rather than db.refresh with relationship attribute_names) - AsyncSession's
    # refresh() can attempt a synchronous lazy-load for relationship attributes and raise
    # MissingGreenlet; a plain re-query with selectinload is the reliable async-safe path.
    return await _detail_out(db, await _load_owned_lead(db, lead_id, current_user.id), current_user.id)


@router.patch("/{lead_id}/hide", response_model=LeadDetailOut)
async def hide_lead(
    lead_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    return await _hide_owned_lead(db, lead_id, current_user.id)


def _to_lead_note_out(note: LeadNote) -> LeadNoteOut:
    author = getattr(note, 'author', None)
    oh = getattr(note, 'open_house_event', None)
    return LeadNoteOut(
        id=note.id,
        lead_id=note.lead_id,
        author_id=note.author_id,
        author_name=f"{author.first_name} {author.last_name}".strip() if author else None,
        text=note.text,
        open_house_event_id=note.open_house_event_id,
        open_house_address=oh.address if oh else None,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


async def _load_owned_note(db: AsyncSession, note_id: str, owner_id: str) -> LeadNote:
    result = await db.execute(
        select(LeadNote)
        .join(Lead, Lead.id == LeadNote.lead_id)
        .options(selectinload(LeadNote.author), selectinload(LeadNote.open_house_event))
        .where(LeadNote.id == note_id, Lead.owner_id == owner_id)
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


async def _reload_note(db: AsyncSession, note_id: str) -> LeadNote:
    # Re-query (rather than db.refresh with relationship attribute_names) after a commit -
    # AsyncSession's refresh() can attempt a synchronous lazy-load for relationship attributes
    # and raise MissingGreenlet; a plain re-query with selectinload is async-safe.
    result = await db.execute(
        select(LeadNote)
        .options(selectinload(LeadNote.author), selectinload(LeadNote.open_house_event))
        .where(LeadNote.id == note_id)
    )
    return result.scalar_one()


@router.get("/{lead_id}/notes", response_model=List[LeadNoteOut])
async def list_lead_notes(
    lead_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    await _load_owned_lead(db, lead_id, current_user.id)
    result = await db.execute(
        select(LeadNote)
        .options(selectinload(LeadNote.author), selectinload(LeadNote.open_house_event))
        .where(LeadNote.lead_id == lead_id)
        .order_by(LeadNote.created_at.desc())
    )
    notes = result.scalars().all()
    return [_to_lead_note_out(n) for n in notes]


@router.post("/{lead_id}/notes", response_model=LeadNoteOut)
async def create_lead_note(
    lead_id: str,
    body: LeadNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    lead = await _load_owned_lead(db, lead_id, current_user.id)
    text_ = (body.text or '').strip()
    if not text_:
        raise HTTPException(status_code=422, detail="Write something in the note first.")
    oh_id = body.open_house_event_id or None
    if oh_id and not any(e.open_house_event_id == oh_id for e in lead.events or []):
        own = (await db.execute(select(OpenHouseEvent.id).where(OpenHouseEvent.id == oh_id, OpenHouseEvent.agent_id == current_user.id))).first()
        if not own:
            oh_id = None  # not one of theirs - keep the note, drop the link
    note = LeadNote(
        lead_id=lead_id,
        author_id=current_user.id,
        text=text_,
        open_house_event_id=oh_id,
    )
    db.add(note)
    await db.commit()
    note = await _reload_note(db, note.id)
    # Their open follow-up's calendar event lists the agent's latest notes - re-push it.
    from app.services.tasks_service import resync_lead_followups
    resync_lead_followups(current_user.id, lead_id)
    return _to_lead_note_out(note)


@router.patch("/notes/{note_id}", response_model=LeadNoteOut)
async def update_lead_note(
    note_id: str,
    body: LeadNoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    note = await _load_owned_note(db, note_id, current_user.id)
    if not (body.text or '').strip():
        raise HTTPException(status_code=422, detail="A note can't be empty - delete it instead.")
    note.text = body.text.strip()
    db.add(note)
    await db.commit()
    note = await _reload_note(db, note_id)
    return _to_lead_note_out(note)


@router.delete("/notes/{note_id}")
async def delete_lead_note(
    note_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    note = await _load_owned_note(db, note_id, current_user.id)
    await db.delete(note)
    await db.commit()
    return {"success": True}
