import uuid
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import os
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    Notification,
    OpenHouseEvent,
    OpenHouseHostRequest,
    OpenHouseListingNote,
    OpenHouseTeamBroadcast,
    OpenHouseTeamMember,
    OpenHouseVisitor,
    Property,
    SystemSettings,
    User,
)
from app.schemas.open_house_manager import (
    ActionCountsResponse,
    AddTeamMemberRequest,
    AgentSearchResult,
    BroadcastResponse,
    CounterHostRequestRequest,
    CreateBroadcastRequest,
    CreateHostRequestRequest,
    CreateHostRequestResult,
    HostRequestResponse,
    IncomingBroadcastResponse,
    KeyMetrics,
    ListingDetailResponse,
    ListingHostNoteResponse,
    ManualListingCreate,
    MyListingItem,
    MyListingsResponse,
    MyListingSummary,
    OpenHouseHistoryRow,
    ScheduledOpenResponse,
    TeamMemberResponse,
    UpdateListingNotesRequest,
)

# Statuses that mean a listing is no longer actively for sale - excluded from "My Listings".
INACTIVE_STATUSES = {
    "CLOSED-BRIGHT", "EXPIRED-BRIGHT", "WITHDRAWN-BRIGHT", "CANCELED-BRIGHT", "OFF_MARKET-BRIGHT",
}

# The "Refresh Listings" button triggers the same global Bright MLS sync the nightly job runs -
# a shared, rate-limited external API, not something scoped to one agent's clicks. This cooldown
# stops repeated clicks (by one agent or many, at once) from hammering that API with overlapping
# sync runs.
MANUAL_REFRESH_COOLDOWN_SECONDS = 600
MANUAL_REFRESH_SETTING_KEY = "manual_listing_refresh_triggered_at"


async def try_start_manual_refresh(db: AsyncSession) -> tuple[bool, int]:
    """Returns (allowed, seconds_remaining). If allowed, records the trigger time immediately."""
    result = await db.execute(select(SystemSettings).where(SystemSettings.key == MANUAL_REFRESH_SETTING_KEY))
    setting = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if setting and setting.value and "triggered_at" in setting.value:
        last_triggered = datetime.fromisoformat(setting.value["triggered_at"])
        elapsed = (now - last_triggered).total_seconds()
        if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
            return False, int(MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed)

    value = {"triggered_at": now.isoformat()}
    if setting:
        setting.value = value
    else:
        db.add(SystemSettings(key=MANUAL_REFRESH_SETTING_KEY, value=value))
    await db.commit()
    return True, 0


def _agent_name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip()


def _format_date(d: date) -> str:
    # %-d (no leading zero) is a glibc-only strftime extension - not supported on Windows.
    return f"{d.strftime('%a, %b')} {d.day}"


def _format_time(t) -> str:
    if not t:
        return ""
    hour12 = t.hour % 12 or 12
    period = "PM" if t.hour >= 12 else "AM"
    return f"{hour12}:{t.minute:02d} {period}"


def _owns_listing(user: User, prop: Property) -> bool:
    return bool(user.mls_id) and user.mls_id in {prop.list_agent_mls_id, prop.co_list_agent_mls_id}


async def _get_owned_property(db: AsyncSession, current_user: User, listing_key: str) -> Property:
    result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    prop = result.scalar_one_or_none()
    if not prop or not _owns_listing(current_user, prop):
        raise ValueError("Listing not found or not owned by this agent.")
    return prop


def _find_scheduled_event(events: List[OpenHouseEvent]) -> Optional[OpenHouseEvent]:
    """The event that counts as 'has an open house lined up'. A real booked date that hasn't
    passed takes priority over an evergreen, currently-active self-hosted open (no fixed date) -
    if a listing somehow has both, showing the actual date/time is more useful than a generic
    'always open' flag, and previously this just returned whichever came first in the (unordered)
    query result. Soonest date wins among multiple dated events."""
    today = date.today()
    dated = [e for e in events if e.scheduled_date is not None and e.scheduled_date >= today]
    if dated:
        return min(dated, key=lambda e: e.scheduled_date)
    for event in events:
        if event.is_active and event.scheduled_date is None:
            return event
    return None


def _compute_open_house_status(events: List[OpenHouseEvent]) -> str:
    return "scheduled" if _find_scheduled_event(events) is not None else "needs_host"


def _counts_toward_insights(event: OpenHouseEvent, visitor_count: int, today: date) -> bool:
    """Whether a kit's guest data should still count toward the listing agent's insights. Deleting
    a kit is just the host tidying up their own list - it must never erase what the listing agent
    already has on record. An active kit always counts. A deleted kit still counts if it actually
    happened (a past date) or drew real visitors; only a deleted kit that was cancelled before ever
    being used (future/evergreen, zero visitors) is excluded, since nothing was actually hosted."""
    if not event.is_deleted:
        return True
    if event.scheduled_date is not None and event.scheduled_date < today:
        return True
    return visitor_count > 0


def _scheduled_open_previews(events: List[OpenHouseEvent], agents_by_id: Dict[str, User]) -> List[ScheduledOpenResponse]:
    """Every still-upcoming open for a listing (same 'hasn't happened yet' rule as
    _find_scheduled_event, but all matches instead of just the first) - drives the small
    date/time chips shown directly on the My Listings card."""
    today = date.today()
    previews: List[ScheduledOpenResponse] = []
    for event in events:
        is_upcoming = (event.is_active and event.scheduled_date is None) or (
            event.scheduled_date is not None and event.scheduled_date >= today
        )
        if not is_upcoming:
            continue
        agent = agents_by_id.get(event.agent_id)
        previews.append(ScheduledOpenResponse(
            id=event.id,
            scheduled_date=event.scheduled_date,
            scheduled_start_time=event.scheduled_start_time,
            scheduled_end_time=event.scheduled_end_time,
            hosting_agent_id=event.agent_id,
            hosting_agent_name=_agent_name(agent) if agent else "Unknown agent",
            hosting_agent_brokerage=agent.brokerage if agent else None,
        ))
    # Soonest date first; evergreen (no fixed date) opens sort after every dated one.
    previews.sort(key=lambda p: (p.scheduled_date is None, p.scheduled_date or date.max))
    return previews


def _confirmed_pending_kit_previews(
    approved_requests: List[OpenHouseHostRequest], events: List[OpenHouseEvent], agents_by_id: Dict[str, User]
) -> List[ScheduledOpenResponse]:
    """Approved requests whose host hasn't built a kit for that date yet - i.e. a time was agreed
    on, but there's no OpenHouseEvent to show under scheduled_opens. Once the host builds their
    kit, the matching OpenHouseEvent shows up and this entry naturally stops appearing here."""
    today = date.today()
    kit_exists = {
        (e.listing_key, e.agent_id, e.scheduled_date) for e in events
    }
    previews: List[ScheduledOpenResponse] = []
    for req in approved_requests:
        if req.requested_date < today:
            continue
        if (req.listing_key, req.requesting_agent_id, req.requested_date) in kit_exists:
            continue
        agent = agents_by_id.get(req.requesting_agent_id)
        previews.append(ScheduledOpenResponse(
            id=req.id,
            scheduled_date=req.requested_date,
            scheduled_start_time=req.requested_start_time,
            scheduled_end_time=req.requested_end_time,
            hosting_agent_id=req.requesting_agent_id,
            hosting_agent_name=_agent_name(agent) if agent else "Unknown agent",
            hosting_agent_brokerage=agent.brokerage if agent else None,
        ))
    previews.sort(key=lambda p: p.scheduled_date or date.max)
    return previews


async def get_listing_open_house_statuses(db: AsyncSession, listing_keys: List[str]) -> Dict[str, dict]:
    """Used by Discovery to show 'No Open House' on listings that aren't yours - checks any
    agent's OpenHouseEvent for that listing_key, not just the current user's. Also surfaces the
    matched event's date/time so Discovery can show when a scheduled open actually is.

    Falls back to Bright's own reported open house date/time (Property.mls_open_house_*, kept
    fresh by the separate open house sync) when nobody has hosted the listing inside OpenHousePal
    itself - most listings on Discovery belong to other agents who don't use this app, so an
    in-app OpenHouseEvent alone would leave the vast majority of real open houses unlabeled."""
    if not listing_keys:
        return {}
    events_result = await db.execute(
        select(OpenHouseEvent).where(OpenHouseEvent.listing_key.in_(listing_keys), OpenHouseEvent.is_deleted.is_(False))
    )
    events_by_listing: Dict[str, List[OpenHouseEvent]] = {}
    for event in events_result.scalars().all():
        events_by_listing.setdefault(event.listing_key, []).append(event)

    today = date.today()
    mls_result = await db.execute(
        select(Property.listing_key, Property.mls_open_house_date, Property.mls_open_house_start_time, Property.mls_open_house_end_time)
        .where(Property.listing_key.in_(listing_keys), Property.mls_open_house_date >= today)
    )
    mls_open_houses = {row.listing_key: row for row in mls_result.all()}

    result: Dict[str, dict] = {}
    for key in listing_keys:
        matched = _find_scheduled_event(events_by_listing.get(key, []))
        if matched is not None:
            result[key] = {
                "status": "scheduled",
                "scheduled_date": matched.scheduled_date,
                "scheduled_start_time": matched.scheduled_start_time,
                "scheduled_end_time": matched.scheduled_end_time,
            }
            continue

        mls_row = mls_open_houses.get(key)
        if mls_row is not None:
            result[key] = {
                "status": "scheduled",
                "scheduled_date": mls_row.mls_open_house_date,
                "scheduled_start_time": mls_row.mls_open_house_start_time,
                "scheduled_end_time": mls_row.mls_open_house_end_time,
            }
        else:
            result[key] = {"status": "needs_host"}
    return result


async def get_my_listings(db: AsyncSession, current_user: User) -> MyListingsResponse:
    if not current_user.mls_id:
        return MyListingsResponse(summary=MyListingSummary(open_scheduled=0, needs_host=0, avg_guests_per_open=0, high_priority=0), listings=[])

    prop_result = await db.execute(
        select(Property).where(
            or_(Property.list_agent_mls_id == current_user.mls_id, Property.co_list_agent_mls_id == current_user.mls_id),
            Property.home_status.notin_(INACTIVE_STATUSES),
        )
    )
    properties = prop_result.scalars().all()
    if not properties:
        return MyListingsResponse(summary=MyListingSummary(open_scheduled=0, needs_host=0, avg_guests_per_open=0, high_priority=0), listings=[])

    listing_keys = [p.listing_key for p in properties]

    # Fetch every kit, deleted or not - deleting a kit must not erase the listing agent's guest
    # history for it (see _counts_toward_insights). Scheduling/status decisions below stay scoped
    # to active-only kits via active_events_by_listing.
    events_result = await db.execute(
        select(OpenHouseEvent).where(OpenHouseEvent.listing_key.in_(listing_keys))
    )
    events = events_result.scalars().all()
    events_by_listing: Dict[str, List[OpenHouseEvent]] = {}
    active_events_by_listing: Dict[str, List[OpenHouseEvent]] = {}
    for event in events:
        events_by_listing.setdefault(event.listing_key, []).append(event)
        if not event.is_deleted:
            active_events_by_listing.setdefault(event.listing_key, []).append(event)

    event_ids = [e.id for e in events]
    visitors_by_event: Dict[str, List[OpenHouseVisitor]] = {}
    if event_ids:
        visitors_result = await db.execute(select(OpenHouseVisitor).where(OpenHouseVisitor.open_house_event_id.in_(event_ids)))
        for visitor in visitors_result.scalars().all():
            visitors_by_event.setdefault(visitor.open_house_event_id, []).append(visitor)

    approved_result = await db.execute(
        select(OpenHouseHostRequest).where(
            OpenHouseHostRequest.listing_key.in_(listing_keys),
            OpenHouseHostRequest.status == "approved",
        )
    )
    approved_requests_by_listing: Dict[str, List[OpenHouseHostRequest]] = {}
    for req in approved_result.scalars().all():
        approved_requests_by_listing.setdefault(req.listing_key, []).append(req)

    # Hosting agents can be different from the listing owner (whoever got approved to host) -
    # needed to label the date/time chips on the My Listings card. Covers both agents already
    # hosting a real kit (OpenHouseEvent) and agents who've been confirmed but haven't built one yet.
    hosting_agent_ids = {e.agent_id for e in events}
    for reqs in approved_requests_by_listing.values():
        hosting_agent_ids.update(r.requesting_agent_id for r in reqs)
    agents_by_id: Dict[str, User] = {}
    if hosting_agent_ids:
        agents_result = await db.execute(select(User).where(User.id.in_(hosting_agent_ids)))
        agents_by_id = {u.id: u for u in agents_result.scalars().all()}

    pending_result = await db.execute(
        select(OpenHouseHostRequest.listing_key, func.count(OpenHouseHostRequest.id))
        .where(OpenHouseHostRequest.listing_key.in_(listing_keys), OpenHouseHostRequest.status == "pending")
        .group_by(OpenHouseHostRequest.listing_key)
    )
    pending_counts = dict(pending_result.all())

    notes_result = await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key.in_(listing_keys)))
    notes_by_listing = {n.listing_key: n for n in notes_result.scalars().all()}

    listings: List[MyListingItem] = []
    total_guests_all = 0
    total_opens_all = 0
    today = date.today()

    for prop in properties:
        listing_events = events_by_listing.get(prop.listing_key, [])
        active_listing_events = active_events_by_listing.get(prop.listing_key, [])
        insight_events = [e for e in listing_events if _counts_toward_insights(e, len(visitors_by_event.get(e.id, [])), today)]
        listing_guest_count = sum(len(visitors_by_event.get(e.id, [])) for e in insight_events)
        opens_hosted_count = len(insight_events)
        avg_guests = (listing_guest_count / opens_hosted_count) if opens_hosted_count else 0.0

        total_guests_all += listing_guest_count
        total_opens_all += opens_hosted_count

        open_house_status = _compute_open_house_status(active_listing_events)
        scheduled_opens = _scheduled_open_previews(active_listing_events, agents_by_id)
        confirmed_pending_kit = _confirmed_pending_kit_previews(
            approved_requests_by_listing.get(prop.listing_key, []), active_listing_events, agents_by_id
        )
        note = notes_by_listing.get(prop.listing_key)
        manual_priority = bool(note and note.is_priority_override)
        # High priority = this listing has proven it draws guests (a past open had at least one
        # guest sign in) and still needs a host lined up - worth prioritizing over an unproven listing.
        computed_priority = open_house_status == "needs_host" and listing_guest_count > 0

        listings.append(MyListingItem(
            listing_key=prop.listing_key,
            image=prop.img_src,
            days_on_market=prop.days_on_market,
            status=prop.home_status,
            address=prop.street_address,
            city=prop.city,
            state=prop.state,
            zipcode=prop.zipcode,
            price=prop.price,
            bedrooms=prop.bedrooms,
            bathrooms=prop.bathrooms,
            living_area=prop.living_area,
            open_house_status=open_house_status,
            avg_guests_per_open=round(avg_guests, 1),
            opens_hosted_count=opens_hosted_count,
            scheduled_opens_count=len(scheduled_opens),
            scheduled_opens=scheduled_opens,
            confirmed_pending_kit=confirmed_pending_kit,
            is_high_priority=manual_priority or computed_priority,
            pending_request_count=pending_counts.get(prop.listing_key, 0),
            is_manual=prop.is_manual,
        ))

    summary = MyListingSummary(
        open_scheduled=sum(1 for l in listings if l.open_house_status == "scheduled"),
        needs_host=sum(1 for l in listings if l.open_house_status == "needs_host"),
        avg_guests_per_open=round((total_guests_all / total_opens_all) if total_opens_all else 0.0, 1),
        high_priority=sum(1 for l in listings if l.is_high_priority),
    )
    return MyListingsResponse(summary=summary, listings=listings)


async def create_manual_listing(db: AsyncSession, current_user: User, payload: ManualListingCreate) -> MyListingItem:
    if not current_user.mls_id:
        raise ValueError("Verify your MLS ID before adding a listing.")

    listing_key = f"manual-{uuid.uuid4().hex}"
    prop = Property(
        listing_key=listing_key,
        street_address=payload.address,
        city=payload.city,
        state=payload.state,
        zipcode=payload.zipcode,
        price=payload.price,
        bedrooms=payload.bedrooms,
        bathrooms=payload.bathrooms,
        living_area=payload.living_area,
        img_src=payload.image_url,
        home_status="ACTIVE",
        list_agent_mls_id=current_user.mls_id,
        is_manual=True,
        days_on_market=0,
    )
    db.add(prop)
    await db.commit()
    await db.refresh(prop)

    return MyListingItem(
        listing_key=prop.listing_key,
        image=prop.img_src,
        days_on_market=prop.days_on_market,
        address=prop.street_address,
        city=prop.city,
        state=prop.state,
        zipcode=prop.zipcode,
        price=prop.price,
        bedrooms=prop.bedrooms,
        bathrooms=prop.bathrooms,
        living_area=prop.living_area,
        open_house_status="needs_host",
        avg_guests_per_open=0.0,
        opens_hosted_count=0,
        is_high_priority=False,
        pending_request_count=0,
        is_manual=True,
    )


async def delete_manual_listing(db: AsyncSession, current_user: User, listing_key: str) -> None:
    result = await db.execute(select(Property).where(Property.listing_key == listing_key, Property.is_manual.is_(True)))
    prop = result.scalar_one_or_none()
    if not prop or not _owns_listing(current_user, prop):
        raise ValueError("Listing not found")

    await db.execute(OpenHouseHostRequest.__table__.delete().where(OpenHouseHostRequest.listing_key == listing_key))
    await db.execute(OpenHouseEvent.__table__.delete().where(OpenHouseEvent.listing_key == listing_key))
    await db.execute(OpenHouseListingNote.__table__.delete().where(OpenHouseListingNote.listing_key == listing_key))
    await db.delete(prop)
    await db.commit()


async def _hydrate_host_request(
    db: AsyncSession,
    req: OpenHouseHostRequest,
    property_by_key: Dict[str, Property],
    requester_by_id: Dict[str, User],
    note_by_listing_key: Optional[Dict[str, OpenHouseListingNote]] = None,
) -> HostRequestResponse:
    prop = property_by_key.get(req.listing_key)
    requester = requester_by_id.get(req.requesting_agent_id)
    note = (note_by_listing_key or {}).get(req.listing_key)
    return HostRequestResponse(
        id=req.id,
        listing_key=req.listing_key,
        property_address=prop.street_address if prop else None,
        property_image=prop.img_src if prop else None,
        listing_owner_agent_id=req.listing_owner_agent_id,
        requesting_agent_id=req.requesting_agent_id,
        requesting_agent_name=_agent_name(requester) if requester else "Unknown agent",
        requesting_agent_brokerage=requester.brokerage if requester else None,
        requesting_agent_email=requester.email if requester else None,
        requested_date=req.requested_date,
        requested_start_time=req.requested_start_time,
        requested_end_time=req.requested_end_time,
        message=req.message,
        status=req.status,
        created_at=req.created_at,
        responded_at=req.responded_at,
        counter_date=req.counter_date,
        counter_start_time=req.counter_start_time,
        counter_end_time=req.counter_end_time,
        counter_message=req.counter_message,
        counter_date_2=req.counter_date_2,
        counter_start_time_2=req.counter_start_time_2,
        counter_end_time_2=req.counter_end_time_2,
        broadcast_id=req.broadcast_id,
        listing_host_notes=note.notes if note else None,
    )


async def _fetch_notes_by_listing_key(db: AsyncSession, listing_keys: List[str]) -> Dict[str, OpenHouseListingNote]:
    if not listing_keys:
        return {}
    result = await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key.in_(listing_keys)))
    return {n.listing_key: n for n in result.scalars().all()}


async def get_host_requests(db: AsyncSession, current_user: User, status_filter: Optional[str] = "pending") -> List[HostRequestResponse]:
    stmt = select(OpenHouseHostRequest).where(OpenHouseHostRequest.listing_owner_agent_id == current_user.id)
    if status_filter:
        stmt = stmt.where(OpenHouseHostRequest.status == status_filter)
    stmt = stmt.order_by(OpenHouseHostRequest.created_at.desc())
    result = await db.execute(stmt)
    requests = result.scalars().all()
    if not requests:
        return []

    listing_keys = list({r.listing_key for r in requests})
    requester_ids = list({r.requesting_agent_id for r in requests})
    prop_result = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
    property_by_key = {p.listing_key: p for p in prop_result.scalars().all()}
    user_result = await db.execute(select(User).where(User.id.in_(requester_ids)))
    requester_by_id = {u.id: u for u in user_result.scalars().all()}
    note_by_listing_key = await _fetch_notes_by_listing_key(db, listing_keys)

    return [await _hydrate_host_request(db, r, property_by_key, requester_by_id, note_by_listing_key) for r in requests]


async def get_action_counts(db: AsyncSession, current_user: User) -> ActionCountsResponse:
    # Scoped to listings that still have a real, active Property row - same definition of "my
    # listings" get_my_listings uses - so this can't count a request the agent has no way to
    # reach (e.g. a listing that's since gone inactive, or a dangling listing_key with no
    # Property row at all) into a badge with nowhere for the agent to click it away.
    pending_result = await db.execute(
        select(func.count(OpenHouseHostRequest.id))
        .join(Property, Property.listing_key == OpenHouseHostRequest.listing_key)
        .where(
            OpenHouseHostRequest.listing_owner_agent_id == current_user.id,
            OpenHouseHostRequest.status == "pending",
            Property.home_status.notin_(INACTIVE_STATUSES),
        )
    )
    countered_result = await db.execute(
        select(func.count(OpenHouseHostRequest.id)).where(
            OpenHouseHostRequest.requesting_agent_id == current_user.id,
            OpenHouseHostRequest.status == "countered",
        )
    )
    # These two catch events that don't leave anything outstanding to derive a count from (e.g.
    # the host already accepted/declined your proposed time) - notification read-state instead.
    listing_agent_unseen_result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.agent_id == current_user.id,
            Notification.is_read == False,
            Notification.type == "HOST_REQUEST_COUNTER_RESPONSE",
        )
    )
    host_unseen_result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.agent_id == current_user.id,
            Notification.is_read == False,
            Notification.type.in_(["HOST_REQUEST_APPROVED", "HOST_REQUEST_DECLINED"]),
        )
    )
    # Unseen-notification-based, like the two above, so the badge clears the moment the Team
    # Requests tab is opened (which marks TEAM_BROADCAST_RECEIVED read) rather than only once the
    # agent actually accepts/declines the broadcast.
    incoming_broadcast_unseen_result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.agent_id == current_user.id,
            Notification.is_read == False,
            Notification.type == "TEAM_BROADCAST_RECEIVED",
        )
    )
    return ActionCountsResponse(
        pending_as_listing_agent=pending_result.scalar() or 0,
        countered_as_host=countered_result.scalar() or 0,
        listing_agent_unseen_updates=listing_agent_unseen_result.scalar() or 0,
        host_unseen_updates=host_unseen_result.scalar() or 0,
        pending_incoming_broadcasts=incoming_broadcast_unseen_result.scalar() or 0,
    )


async def get_my_requests(db: AsyncSession, current_user: User) -> List[HostRequestResponse]:
    """The current agent's own outgoing requests to host OTHER agents' listings (Discovery's 'My
    Requests'). An approved request whose date has already passed is dropped - the open house
    already happened, so there's nothing left to act on and the listing agent's host notes
    (surfaced live below) should stop being relevant to this agent."""
    today = date.today()
    stmt = select(OpenHouseHostRequest).where(OpenHouseHostRequest.requesting_agent_id == current_user.id).order_by(OpenHouseHostRequest.created_at.desc())
    result = await db.execute(stmt)
    requests = [r for r in result.scalars().all() if not (r.status == "approved" and r.requested_date < today)]
    if not requests:
        return []
    listing_keys = list({r.listing_key for r in requests})
    prop_result = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
    property_by_key = {p.listing_key: p for p in prop_result.scalars().all()}
    requester_by_id = {current_user.id: current_user}
    note_by_listing_key = await _fetch_notes_by_listing_key(db, listing_keys)
    return [await _hydrate_host_request(db, r, property_by_key, requester_by_id, note_by_listing_key) for r in requests]


async def create_host_request(db: AsyncSession, current_user: User, payload: CreateHostRequestRequest) -> CreateHostRequestResult:
    prop_result = await db.execute(select(Property).where(Property.listing_key == payload.listing_key))
    prop = prop_result.scalar_one_or_none()
    if not prop:
        raise ValueError("This listing could not be found.")

    owner_mls_id = prop.list_agent_mls_id or prop.co_list_agent_mls_id
    listing_owner = None
    if owner_mls_id:
        owner_result = await db.execute(select(User).where(User.mls_id == owner_mls_id))
        listing_owner = owner_result.scalar_one_or_none()
    if not listing_owner:
        # The listing's MLS agent has no OpenHousePal account - nothing to persist or notify.
        # The frontend falls back to the email template for this case.
        return CreateHostRequestResult(on_platform=False, request=None)

    request_id = str(uuid.uuid4())
    request = OpenHouseHostRequest(
        id=request_id,
        listing_key=payload.listing_key,
        listing_owner_agent_id=listing_owner.id,
        requesting_agent_id=current_user.id,
        requested_date=payload.requested_date,
        requested_start_time=payload.requested_start_time,
        requested_end_time=payload.requested_end_time,
        message=payload.message,
        status="pending",
    )
    db.add(request)

    try:
        db.add(Notification(
            agent_id=listing_owner.id,
            type="HOST_REQUEST_RECEIVED",
            reference_type="HOST_REQUEST",
            reference_id=request_id,
            title=f"{_agent_name(current_user)} wants to host an open house",
            message=f"Requested {_format_date(payload.requested_date)} at {prop.street_address}",
            property_id=prop.id,
            property_address=prop.street_address,
            link="/open-houses/my-listings",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    hydrated = await _hydrate_host_request(db, request, {prop.listing_key: prop}, {current_user.id: current_user})
    return CreateHostRequestResult(on_platform=True, request=hydrated)


async def _fill_broadcast_and_decline_siblings(db: AsyncSession, request: OpenHouseHostRequest) -> None:
    """Once one team-broadcast response is approved, close out the rest so the owner doesn't have
    to manually decline every other agent who said they were interested in the same slot."""
    if not request.broadcast_id:
        return

    broadcast_result = await db.execute(select(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.id == request.broadcast_id))
    broadcast = broadcast_result.scalar_one_or_none()
    if broadcast:
        broadcast.status = "filled"

    siblings_result = await db.execute(
        select(OpenHouseHostRequest).where(
            OpenHouseHostRequest.broadcast_id == request.broadcast_id,
            OpenHouseHostRequest.id != request.id,
            OpenHouseHostRequest.status == "pending",
        )
    )
    for sibling in siblings_result.scalars().all():
        sibling.status = "declined"
        sibling.responded_at = datetime.now(timezone.utc)
        try:
            db.add(Notification(
                agent_id=sibling.requesting_agent_id,
                type="HOST_REQUEST_DECLINED",
                reference_type="HOST_REQUEST",
                reference_id=sibling.id,
                title="This open house slot was filled",
                message="Another team member already got this one - keep an eye out for the next broadcast.",
                link="/discovery",
                is_read=False,
            ))
        except Exception:
            pass


async def approve_host_request(db: AsyncSession, current_user: User, request_id: str) -> HostRequestResponse:
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Host request not found.")
    if request.status not in ("pending", "countered"):
        raise ValueError("This request has already been responded to.")

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    # No OpenHouseEvent ("kit") is created here - the host builds their own kit for this date
    # from the Open Houses > Your Opens flow when they're ready. Approving just confirms the
    # date/time and notifies them; it doesn't generate a kit on their behalf.
    request.status = "approved"
    request.responded_at = datetime.now(timezone.utc)

    await _fill_broadcast_and_decline_siblings(db, request)

    notes_result = await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key == request.listing_key))
    note = notes_result.scalar_one_or_none()
    approval_message = f"You're confirmed to host {prop.street_address if prop else 'this listing'} on {_format_date(request.requested_date)}"
    if note and note.notes:
        approval_message += f". Instructions from the listing agent: {note.notes}"

    try:
        db.add(Notification(
            agent_id=request.requesting_agent_id,
            type="HOST_REQUEST_APPROVED",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title="Your open house request was approved!",
            message=approval_message,
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/open-houses",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()
    note_by_listing_key = {request.listing_key: note} if note else {}
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {requester.id: requester} if requester else {}, note_by_listing_key)


async def decline_host_request(db: AsyncSession, current_user: User, request_id: str) -> HostRequestResponse:
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Host request not found.")
    if request.status != "pending":
        raise ValueError("This request has already been responded to.")

    request.status = "declined"
    request.responded_at = datetime.now(timezone.utc)

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    try:
        db.add(Notification(
            agent_id=request.requesting_agent_id,
            type="HOST_REQUEST_DECLINED",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title="Your open house request was declined",
            message=f"The listing agent declined your request for {prop.street_address if prop else 'this listing'}",
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/discovery",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {requester.id: requester} if requester else {})


async def counter_host_request(db: AsyncSession, current_user: User, request_id: str, payload: CounterHostRequestRequest) -> HostRequestResponse:
    """Listing agent proposes a different date/time instead of approving or declining outright."""
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Host request not found.")
    if request.status != "pending":
        raise ValueError("This request has already been responded to.")

    request.status = "countered"
    request.counter_date = payload.counter_date
    request.counter_start_time = payload.counter_start_time
    request.counter_end_time = payload.counter_end_time
    request.counter_message = payload.counter_message
    request.counter_date_2 = payload.counter_date_2
    request.counter_start_time_2 = payload.counter_start_time_2
    request.counter_end_time_2 = payload.counter_end_time_2

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    has_second_option = bool(payload.counter_date_2 and payload.counter_start_time_2 and payload.counter_end_time_2)
    counter_note_suffix = f' Note: "{payload.counter_message.strip()}"' if payload.counter_message and payload.counter_message.strip() else ""
    counter_options_suffix = " Two options were proposed - take a look." if has_second_option else ""

    try:
        db.add(Notification(
            agent_id=request.requesting_agent_id,
            type="HOST_REQUEST_COUNTERED",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title="A different time was proposed",
            message=f"The listing agent for {prop.street_address if prop else 'this listing'} suggested {_format_date(payload.counter_date)} instead.{counter_options_suffix}{counter_note_suffix}",
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/discovery",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {requester.id: requester} if requester else {})


async def respond_to_counter(db: AsyncSession, current_user: User, request_id: str, accept: bool, slot: int = 1) -> HostRequestResponse:
    """The requesting agent accepts or declines the listing agent's proposed alternate time.
    When the listing agent offered two options, `slot` picks which one (1 or 2) the host is
    accepting - ignored when declining, since neither option is being taken."""
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.requesting_agent_id != current_user.id:
        raise ValueError("Host request not found.")
    if request.status != "countered":
        raise ValueError("There's no proposed time to respond to on this request.")
    if slot == 2 and not (request.counter_date_2 and request.counter_start_time_2 and request.counter_end_time_2):
        raise ValueError("There's no second proposed time on this request.")

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    if accept:
        if slot == 2:
            request.requested_date = request.counter_date_2
            request.requested_start_time = request.counter_start_time_2
            request.requested_end_time = request.counter_end_time_2
        else:
            request.requested_date = request.counter_date
            request.requested_start_time = request.counter_start_time
            request.requested_end_time = request.counter_end_time

        # No auto-created kit here either - same reasoning as approve_host_request.
        request.status = "approved"
        notif_title = "New time confirmed"
        # This notification goes to the listing agent, not the host - phrase it from their side.
        notif_message = (
            f"{_agent_name(current_user)} agreed to host {prop.street_address if prop else 'this listing'} on "
            f"{_format_date(request.requested_date)} from {_format_time(request.requested_start_time)} to {_format_time(request.requested_end_time)}"
        )
    else:
        request.status = "declined"
        notif_title = "Proposed time declined"
        notif_message = f"{_agent_name(current_user)} declined the proposed time for {prop.street_address if prop else 'this listing'}"

    request.responded_at = datetime.now(timezone.utc)

    try:
        db.add(Notification(
            agent_id=request.listing_owner_agent_id,
            type="HOST_REQUEST_COUNTER_RESPONSE",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title=notif_title,
            message=notif_message,
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/open-houses/my-listings",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    note_by_listing_key = await _fetch_notes_by_listing_key(db, [request.listing_key])
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {current_user.id: current_user}, note_by_listing_key)


async def search_agents(db: AsyncSession, current_user: User, query: str) -> List[AgentSearchResult]:
    term = query.strip()
    if len(term) < 2:
        return []
    like = f"%{term}%"
    result = await db.execute(
        select(User).where(
            User.id != current_user.id,
            or_(
                User.email.ilike(like),
                User.mls_id.ilike(like),
                (User.first_name + " " + User.last_name).ilike(like),
            ),
        ).limit(10)
    )
    return [
        AgentSearchResult(id=u.id, name=_agent_name(u), brokerage=u.brokerage, mls_id=u.mls_id, email=u.email)
        for u in result.scalars().all()
    ]


async def list_team_members(db: AsyncSession, current_user: User) -> List[TeamMemberResponse]:
    result = await db.execute(
        select(OpenHouseTeamMember, User)
        .join(User, User.id == OpenHouseTeamMember.member_agent_id)
        .where(OpenHouseTeamMember.owner_id == current_user.id)
        .order_by(OpenHouseTeamMember.created_at.desc())
    )
    return [
        TeamMemberResponse(id=member.id, agent_id=user.id, name=_agent_name(user), brokerage=user.brokerage, mls_id=user.mls_id, added_at=member.created_at)
        for member, user in result.all()
    ]


async def add_team_member(db: AsyncSession, current_user: User, payload: AddTeamMemberRequest) -> TeamMemberResponse:
    identifier = payload.identifier.strip()
    if not identifier:
        raise ValueError("Enter an email or MLS ID to add.")

    result = await db.execute(select(User).where(or_(User.email.ilike(identifier), User.mls_id == identifier)))
    agent = result.scalar_one_or_none()
    if not agent:
        raise ValueError("No OpenHousePal agent found with that email or MLS ID.")
    if agent.id == current_user.id:
        raise ValueError("You can't add yourself to your own team.")

    existing_result = await db.execute(
        select(OpenHouseTeamMember).where(OpenHouseTeamMember.owner_id == current_user.id, OpenHouseTeamMember.member_agent_id == agent.id)
    )
    if existing_result.scalar_one_or_none():
        raise ValueError(f"{_agent_name(agent)} is already on your team.")

    member = OpenHouseTeamMember(owner_id=current_user.id, member_agent_id=agent.id)
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return TeamMemberResponse(id=member.id, agent_id=agent.id, name=_agent_name(agent), brokerage=agent.brokerage, mls_id=agent.mls_id, added_at=member.created_at)


async def remove_team_member(db: AsyncSession, current_user: User, member_id: str) -> None:
    result = await db.execute(select(OpenHouseTeamMember).where(OpenHouseTeamMember.id == member_id, OpenHouseTeamMember.owner_id == current_user.id))
    member = result.scalar_one_or_none()
    if not member:
        raise ValueError("Team member not found.")
    await db.delete(member)
    await db.commit()


async def create_broadcast(db: AsyncSession, current_user: User, payload: CreateBroadcastRequest) -> BroadcastResponse:
    prop = await _get_owned_property(db, current_user, payload.listing_key)

    members_result = await db.execute(select(OpenHouseTeamMember).where(OpenHouseTeamMember.owner_id == current_user.id))
    members = members_result.scalars().all()
    if not members:
        raise ValueError("Add agents to your Open House Team before sending a broadcast.")

    broadcast = OpenHouseTeamBroadcast(
        listing_key=payload.listing_key,
        owner_id=current_user.id,
        requested_date=payload.requested_date,
        requested_start_time=payload.requested_start_time,
        requested_end_time=payload.requested_end_time,
        message=payload.message,
    )
    db.add(broadcast)
    await db.flush()

    # Broad info only - date/time window and the listing itself. No visitor/guest data exists yet
    # at this stage anyway, and host notes/instructions are withheld until someone's approved.
    for member in members:
        try:
            db.add(Notification(
                agent_id=member.member_agent_id,
                type="TEAM_BROADCAST_RECEIVED",
                reference_type="TEAM_BROADCAST",
                reference_id=broadcast.id,
                title=f"{_agent_name(current_user)} needs a host",
                message=f"{prop.street_address} - {_format_date(payload.requested_date)}",
                property_id=prop.id,
                property_address=prop.street_address,
                link="/open-houses/my-listings",
                is_read=False,
            ))
        except Exception:
            pass

    await db.commit()
    await db.refresh(broadcast)
    return BroadcastResponse(
        id=broadcast.id, listing_key=broadcast.listing_key, property_address=prop.street_address, property_image=prop.img_src,
        requested_date=broadcast.requested_date, requested_start_time=broadcast.requested_start_time, requested_end_time=broadcast.requested_end_time,
        message=broadcast.message, status=broadcast.status, created_at=broadcast.created_at, interested_requests=[],
    )


async def list_my_broadcasts(db: AsyncSession, current_user: User) -> List[BroadcastResponse]:
    result = await db.execute(
        select(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.owner_id == current_user.id).order_by(OpenHouseTeamBroadcast.created_at.desc())
    )
    broadcasts = result.scalars().all()
    if not broadcasts:
        return []

    listing_keys = list({b.listing_key for b in broadcasts})
    prop_result = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
    property_by_key = {p.listing_key: p for p in prop_result.scalars().all()}

    broadcast_ids = [b.id for b in broadcasts]
    requests_result = await db.execute(
        select(OpenHouseHostRequest).where(OpenHouseHostRequest.broadcast_id.in_(broadcast_ids), OpenHouseHostRequest.status == "pending")
    )
    requests_by_broadcast: Dict[str, List[OpenHouseHostRequest]] = {}
    for req in requests_result.scalars().all():
        requests_by_broadcast.setdefault(req.broadcast_id, []).append(req)

    requester_ids = list({r.requesting_agent_id for reqs in requests_by_broadcast.values() for r in reqs})
    requester_by_id: Dict[str, User] = {}
    if requester_ids:
        requesters_result = await db.execute(select(User).where(User.id.in_(requester_ids)))
        requester_by_id = {u.id: u for u in requesters_result.scalars().all()}

    responses = []
    for broadcast in broadcasts:
        prop = property_by_key.get(broadcast.listing_key)
        interested = requests_by_broadcast.get(broadcast.id, [])
        hydrated_requests = [await _hydrate_host_request(db, r, {broadcast.listing_key: prop} if prop else {}, requester_by_id) for r in interested]
        responses.append(BroadcastResponse(
            id=broadcast.id, listing_key=broadcast.listing_key, property_address=prop.street_address if prop else None, property_image=prop.img_src if prop else None,
            requested_date=broadcast.requested_date, requested_start_time=broadcast.requested_start_time, requested_end_time=broadcast.requested_end_time,
            message=broadcast.message, status=broadcast.status, created_at=broadcast.created_at, interested_requests=hydrated_requests,
        ))
    return responses


async def list_incoming_broadcasts(db: AsyncSession, current_user: User) -> List[IncomingBroadcastResponse]:
    owner_ids_result = await db.execute(select(OpenHouseTeamMember.owner_id).where(OpenHouseTeamMember.member_agent_id == current_user.id))
    owner_ids = [row[0] for row in owner_ids_result.all()]
    if not owner_ids:
        return []

    broadcasts_result = await db.execute(
        select(OpenHouseTeamBroadcast)
        .where(OpenHouseTeamBroadcast.owner_id.in_(owner_ids), OpenHouseTeamBroadcast.status == "open")
        .order_by(OpenHouseTeamBroadcast.created_at.desc())
    )
    broadcasts = broadcasts_result.scalars().all()
    if not broadcasts:
        return []

    listing_keys = list({b.listing_key for b in broadcasts})
    prop_result = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
    property_by_key = {p.listing_key: p for p in prop_result.scalars().all()}

    owners_result = await db.execute(select(User).where(User.id.in_(owner_ids)))
    owner_by_id = {u.id: u for u in owners_result.scalars().all()}

    broadcast_ids = [b.id for b in broadcasts]
    my_responses_result = await db.execute(
        select(OpenHouseHostRequest).where(OpenHouseHostRequest.broadcast_id.in_(broadcast_ids), OpenHouseHostRequest.requesting_agent_id == current_user.id)
    )
    my_status_by_broadcast = {r.broadcast_id: r.status for r in my_responses_result.scalars().all()}

    result = []
    for broadcast in broadcasts:
        if broadcast.id in my_status_by_broadcast:
            continue  # already responded (or already covered) - don't keep prompting
        prop = property_by_key.get(broadcast.listing_key)
        owner = owner_by_id.get(broadcast.owner_id)
        result.append(IncomingBroadcastResponse(
            id=broadcast.id, listing_key=broadcast.listing_key, property_address=prop.street_address if prop else None, property_image=prop.img_src if prop else None,
            owner_agent_name=_agent_name(owner) if owner else "Unknown agent", owner_agent_brokerage=owner.brokerage if owner else None,
            requested_date=broadcast.requested_date, requested_start_time=broadcast.requested_start_time, requested_end_time=broadcast.requested_end_time,
            message=broadcast.message, status=broadcast.status, created_at=broadcast.created_at, my_response_status=None,
        ))
    return result


async def respond_to_broadcast(db: AsyncSession, current_user: User, broadcast_id: str) -> HostRequestResponse:
    """Team member says 'I can host this' - creates a normal pending host request the owner
    approves/declines exactly like a direct ask, so all the existing tooling just works."""
    result = await db.execute(select(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.id == broadcast_id))
    broadcast = result.scalar_one_or_none()
    if not broadcast or broadcast.status != "open":
        raise ValueError("This open house request is no longer available.")

    membership_result = await db.execute(
        select(OpenHouseTeamMember).where(OpenHouseTeamMember.owner_id == broadcast.owner_id, OpenHouseTeamMember.member_agent_id == current_user.id)
    )
    if not membership_result.scalar_one_or_none():
        raise ValueError("You're not on this agent's Open House Team.")

    existing_result = await db.execute(
        select(OpenHouseHostRequest).where(OpenHouseHostRequest.broadcast_id == broadcast_id, OpenHouseHostRequest.requesting_agent_id == current_user.id)
    )
    if existing_result.scalar_one_or_none():
        raise ValueError("You've already responded to this request.")

    request = OpenHouseHostRequest(
        id=str(uuid.uuid4()),
        listing_key=broadcast.listing_key,
        listing_owner_agent_id=broadcast.owner_id,
        requesting_agent_id=current_user.id,
        requested_date=broadcast.requested_date,
        requested_start_time=broadcast.requested_start_time,
        requested_end_time=broadcast.requested_end_time,
        status="pending",
        broadcast_id=broadcast.id,
    )
    db.add(request)

    prop_result = await db.execute(select(Property).where(Property.listing_key == broadcast.listing_key))
    prop = prop_result.scalar_one_or_none()

    try:
        db.add(Notification(
            agent_id=broadcast.owner_id,
            type="TEAM_BROADCAST_RESPONSE",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title=f"{_agent_name(current_user)} can host",
            message=f"{_agent_name(current_user)} is available for {prop.street_address if prop else 'your listing'} on {_format_date(broadcast.requested_date)}",
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/open-houses/my-listings",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    return await _hydrate_host_request(db, request, {broadcast.listing_key: prop} if prop else {}, {current_user.id: current_user})


async def cancel_broadcast(db: AsyncSession, current_user: User, broadcast_id: str) -> None:
    result = await db.execute(select(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.id == broadcast_id, OpenHouseTeamBroadcast.owner_id == current_user.id))
    broadcast = result.scalar_one_or_none()
    if not broadcast:
        raise ValueError("Broadcast not found.")
    broadcast.status = "cancelled"
    await db.commit()


async def get_listing_detail(db: AsyncSession, current_user: User, listing_key: str) -> ListingDetailResponse:
    prop = await _get_owned_property(db, current_user, listing_key)

    # Include deleted kits too - deleting a kit must not erase the listing agent's guest history
    # for it (see _counts_toward_insights). scheduled_opens below stays scoped to active kits only.
    events_result = await db.execute(
        select(OpenHouseEvent).where(OpenHouseEvent.listing_key == listing_key).order_by(OpenHouseEvent.created_at.desc())
    )
    events = events_result.scalars().all()

    hosting_agent_ids = list({e.agent_id for e in events})
    agents_by_id: Dict[str, User] = {}
    if hosting_agent_ids:
        agents_result = await db.execute(select(User).where(User.id.in_(hosting_agent_ids)))
        agents_by_id = {u.id: u for u in agents_result.scalars().all()}

    event_ids = [e.id for e in events]
    visitors_by_event: Dict[str, List[OpenHouseVisitor]] = {}
    if event_ids:
        visitors_result = await db.execute(select(OpenHouseVisitor).where(OpenHouseVisitor.open_house_event_id.in_(event_ids)))
        for visitor in visitors_result.scalars().all():
            visitors_by_event.setdefault(visitor.open_house_event_id, []).append(visitor)

    today = date.today()
    scheduled_opens: List[ScheduledOpenResponse] = []
    history: List[OpenHouseHistoryRow] = []
    total_guests = 0
    total_unrepresented = 0
    total_requested_updates = 0

    total_opens_hosted = 0
    for event in events:
        agent = agents_by_id.get(event.agent_id)
        agent_name = _agent_name(agent) if agent else "Unknown agent"
        visitors = visitors_by_event.get(event.id, [])
        guests = len(visitors)
        unrepresented = sum(1 for v in visitors if (v.has_agent or "").upper() == "NO")
        requested_updates = sum(1 for v in visitors if v.interested_in_similar)

        if not _counts_toward_insights(event, guests, today):
            continue  # deleted kit that was cancelled before ever being used - nothing to show

        total_guests += guests
        total_unrepresented += unrepresented
        total_requested_updates += requested_updates
        total_opens_hosted += 1

        # Same "still upcoming" rule as _find_scheduled_event/_scheduled_open_previews - an
        # evergreen (no fixed date) active kit counts as scheduled too, not just dated ones.
        is_upcoming = not event.is_deleted and (
            (event.is_active and event.scheduled_date is None)
            or (event.scheduled_date is not None and event.scheduled_date >= today)
        )
        if is_upcoming:
            scheduled_opens.append(ScheduledOpenResponse(
                id=event.id,
                scheduled_date=event.scheduled_date,
                scheduled_start_time=event.scheduled_start_time,
                scheduled_end_time=event.scheduled_end_time,
                hosting_agent_id=event.agent_id,
                hosting_agent_name=agent_name,
                hosting_agent_brokerage=agent.brokerage if agent else None,
            ))
        else:
            history.append(OpenHouseHistoryRow(
                id=event.id,
                event_date=event.scheduled_date or (event.created_at.date() if event.created_at else None),
                start_time=event.scheduled_start_time,
                end_time=event.scheduled_end_time,
                hosting_agent_id=event.agent_id,
                hosting_agent_name=agent_name,
                hosting_agent_brokerage=agent.brokerage if agent else None,
                total_guests=guests,
                unrepresented_guests=unrepresented,
                requested_updates=requested_updates,
            ))

    # Soonest date first, not creation order.
    scheduled_opens.sort(key=lambda o: o.scheduled_date or date.max)

    pending_result = await db.execute(
        select(OpenHouseHostRequest).where(OpenHouseHostRequest.listing_key == listing_key, OpenHouseHostRequest.status == "pending").order_by(OpenHouseHostRequest.created_at.desc())
    )
    pending_requests_raw = pending_result.scalars().all()
    requester_by_id: Dict[str, User] = {}
    if pending_requests_raw:
        requester_ids = list({r.requesting_agent_id for r in pending_requests_raw})
        requesters_result = await db.execute(select(User).where(User.id.in_(requester_ids)))
        requester_by_id = {u.id: u for u in requesters_result.scalars().all()}
    pending_requests = [await _hydrate_host_request(db, r, {listing_key: prop}, requester_by_id) for r in pending_requests_raw]

    note_result = await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key == listing_key))
    note = note_result.scalar_one_or_none()

    return ListingDetailResponse(
        listing_key=listing_key,
        address=prop.street_address,
        image=prop.img_src,
        scheduled_opens=scheduled_opens,
        pending_requests=pending_requests,
        history=history,
        key_metrics=KeyMetrics(
            total_guests=total_guests,
            unrepresented_guests=total_unrepresented,
            requested_updates=total_requested_updates,
            total_opens_hosted=total_opens_hosted,
        ),
        notes=ListingHostNoteResponse(
            notes=note.notes if note else None,
            is_priority_override=note.is_priority_override if note else False,
            updated_at=note.updated_at if note else None,
        ),
    )


async def save_listing_notes(db: AsyncSession, current_user: User, listing_key: str, payload: UpdateListingNotesRequest) -> ListingHostNoteResponse:
    await _get_owned_property(db, current_user, listing_key)

    result = await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key == listing_key))
    note = result.scalar_one_or_none()
    if not note:
        note = OpenHouseListingNote(listing_key=listing_key, agent_id=current_user.id)
        db.add(note)

    if payload.notes is not None:
        note.notes = payload.notes
    if payload.is_priority_override is not None:
        note.is_priority_override = payload.is_priority_override

    await db.commit()
    await db.refresh(note)
    return ListingHostNoteResponse(notes=note.notes, is_priority_override=note.is_priority_override, updated_at=note.updated_at)
