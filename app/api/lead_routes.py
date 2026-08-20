import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, exists, func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.database import Collection, Lead, LeadEvent, LeadNote, OpenHouseEvent, OpenHouseVisitor, Property, User
from app.schemas.leads import (
    LeadDetailOut, LeadEventOut, LeadHideRequest, LeadNoteCreate, LeadNoteOut, LeadNoteUpdate, LeadOpenHouseOut, LeadOut, LeadPropertyOut,
    LeadSort, LeadSourceFilter, LeadStatus, LeadRepresented, LeadUpdate,
)
from app.services.leads_service import normalize_email, normalize_phone
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
    if source_type == 'open_house_disclosure':
        return 'form'
    if source_type in ('open_house_second_visit', 'property_tour'):
        return 'tour'
    # property_comment, collection_visitor - actual engagement with an existing showcase
    return 'showcase'


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
    placeholder. Best-effort: leaves lead_out.property untouched (still None) on no match."""
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
    return list(result.scalars().unique().all())


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
_ENGAGEMENT_EVENT_TYPES = {'property_comment', 'property_tour'}


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
        last_event_summary=last_event.summary if last_event else None,
        last_event_source_type=last_event.source_type if last_event else None,
        last_event_at=_last_activity_at(lead, events, matched_collections),
        last_event_occurred_at=last_event.occurred_at if last_event else None,
        property_address=next((e.property_address for e in events if e.property_address), None),
        property=_lead_property(events),
        form_title=last_event.form.title if last_event and last_event.source_type == 'form_submission' and last_event.form else None,
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
        # Only unseen-by-agent tour requests/comments count, same "cleared it by visiting the
        # showcase" rule as has_liked_property/has_disliked_property below - agent_viewed_at is
        # set in bulk on every property_tour/property_comment event for a collection the moment
        # the agent opens it (see collections_routes.py's mark-seen endpoint).
        has_tour_request=any(e.source_type == 'property_tour' and e.agent_viewed_at is None for e in events),
        has_commented=any(e.source_type == 'property_comment' and e.agent_viewed_at is None for e in events),
        # Only unseen-by-agent likes/dislikes count - agent_viewed_at is the same "cleared it in
        # their showcase management view" signal the Showcases tab's own NEW tag uses, so a
        # like/dislike the agent already looked at stops showing up here as a pending alert.
        has_liked_property=any(pi.liked and pi.agent_viewed_at is None for pi in all_interactions),
        has_disliked_property=any(pi.disliked and pi.agent_viewed_at is None for pi in all_interactions),
        needs_attention=bool(last_event) and (lead.viewed_at is None or lead.viewed_at < last_event.occurred_at),
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
    )


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
        stmt = stmt.where(or_(Lead.name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like)))
    result = await db.execute(stmt)
    return list(result.scalars().unique().all())


def _has_showcase_activity(l: LeadOut) -> bool:
    return bool(
        l.showcase_new_matches or l.has_liked_property or l.has_disliked_property or
        l.has_commented or l.has_tour_request or l.has_second_visit_request
    )


def _sort_leads(leads: List[LeadOut], sort: LeadSort) -> List[LeadOut]:
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
    if source == 'open_house':
        lead_outs = [l for l in lead_outs if l.open_houses]
    elif source != 'all':
        lead_outs = [l for l in lead_outs if l.primary_source == source]
    if open_house_event_id:
        lead_outs = [l for l in lead_outs if any(oh.id == open_house_event_id for oh in l.open_houses)]
    lead_outs = _sort_leads(lead_outs, sort)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Name', 'Email', 'Phone', 'Source', 'Status', 'Represented', 'Property', 'Tags', 'Created At', 'Last Contacted'])
    for lead in lead_outs:
        writer.writerow([
            lead.name or '', lead.email or '', lead.phone or '', lead.primary_source, lead.status, lead.represented,
            lead.property_address or '', ', '.join(lead.tags), lead.created_at.isoformat(),
            lead.last_contacted_at.isoformat() if lead.last_contacted_at else '',
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


async def _hide_owned_lead(db: AsyncSession, lead_id: str, owner_id: str) -> LeadDetailOut:
    lead = await _load_owned_lead(db, lead_id, owner_id)
    lead.hidden_at = datetime.now(timezone.utc)
    db.add(lead)
    await db.commit()
    lead = await _load_owned_lead(db, lead_id, owner_id)
    collections = await _fetch_owner_collections(db, owner_id)
    lead_out = _to_lead_out(lead, collections)
    await _fill_missing_property_photos(db, [lead], [lead_out])
    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
    return LeadDetailOut(**lead_out.model_dump(), events=[LeadEventOut.model_validate(e) for e in events])


async def _hide_lead_record(db: AsyncSession, lead: Lead, owner_id: str) -> LeadDetailOut:
    lead.hidden_at = datetime.now(timezone.utc)
    db.add(lead)
    await db.commit()
    lead = await _load_owned_lead(db, lead.id, owner_id)
    collections = await _fetch_owner_collections(db, owner_id)
    lead_out = _to_lead_out(lead, collections)
    await _fill_missing_property_photos(db, [lead], [lead_out])
    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
    return LeadDetailOut(**lead_out.model_dump(), events=[LeadEventOut.model_validate(e) for e in events])


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
    await db.delete(lead)
    await db.commit()
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
    collections = await _fetch_owner_collections(db, current_user.id)
    lead_out = _to_lead_out(lead, collections)
    await _fill_missing_property_photos(db, [lead], [lead_out])
    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
    return LeadDetailOut(**lead_out.model_dump(), events=[LeadEventOut.model_validate(e) for e in events])


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
        lead.email = normalize_email(body.email)
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
    lead = await _load_owned_lead(db, lead_id, current_user.id)
    collections = await _fetch_owner_collections(db, current_user.id)

    lead_out = _to_lead_out(lead, collections)
    await _fill_missing_property_photos(db, [lead], [lead_out])
    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)
    return LeadDetailOut(**lead_out.model_dump(), events=[LeadEventOut.model_validate(e) for e in events])


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
    await _load_owned_lead(db, lead_id, current_user.id)
    note = LeadNote(
        lead_id=lead_id,
        author_id=current_user.id,
        text=body.text,
        open_house_event_id=body.open_house_event_id,
    )
    db.add(note)
    await db.commit()
    note = await _reload_note(db, note.id)
    return _to_lead_note_out(note)


@router.patch("/notes/{note_id}", response_model=LeadNoteOut)
async def update_lead_note(
    note_id: str,
    body: LeadNoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    note = await _load_owned_note(db, note_id, current_user.id)
    note.text = body.text
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
