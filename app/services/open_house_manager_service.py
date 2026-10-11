import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

import os
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import s3_service, storage_ledger_service
from app.services.mls_open_houses import upcoming_mls_open_houses
from app.utils.listing_dom import property_days_on_market

logger = logging.getLogger(__name__)

from app.models.database import (
    Notification,
    OpenHouseEvent,
    OpenHouseHostRequest,
    OpenHouseHostRequestEvent,
    OpenHouseListingNote,
    OpenHouseTeamBroadcast,
    OpenHouseTeamMember,
    OpenHouseVisitor,
    Property,
    SystemSettings,
    User,
)
from app.schemas.open_house import VisitorResponse
from app.schemas.open_house_manager import (
    ActionCountsResponse,
    AddTeamMemberRequest,
    AgentSearchResult,
    BroadcastResponse,
    CancelApprovedOpenRequest,
    CounterBackRequest,
    CounterHostRequestRequest,
    CreateBroadcastRequest,
    CreateHostRequestRequest,
    CreateHostRequestResult,
    HostRequestEventResponse,
    HostRequestResponse,
    IncomingBroadcastResponse,
    IncomingTeamInviteResponse,
    KeyMetrics,
    ListingDetailResponse,
    ListingHostNoteResponse,
    ManualListingCreate,
    MyListingItem,
    MyListingsResponse,
    MyListingSummary,
    OpenHouseHistoryRow,
    RespondToBroadcastRequest,
    ScheduledOpenResponse,
    TeamMemberResponse,
    TeamMembershipResponse,
    TrustedHostResponse,
    UpdateListingNotesRequest,
    UpdateRestrictionRequest,
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


def _send_email_safely(recipient, subject: str, template: str, variables: dict, notification_type: Optional[str] = None) -> None:
    """Best-effort email alongside the in-app Notification for hosting-request events. `recipient`
    is the agent's User: the email follows their Settings -> Notifications switch for this kind of
    alert (it used to go out even with "Open house host requests" / "Team & broadcasts" turned off)
    and is sent in the background like every other notification email (it used to block the request
    on Mailgun). A bare address is still accepted and always sent. Never raises."""
    try:
        from app.services.agent_email_service import email_agent, send
        if isinstance(recipient, str) or recipient is None:
            send(recipient, template, subject, variables)
        else:
            email_agent(recipient, notification_type, template, subject, variables)
    except Exception:
        pass


def _format_date(d: date) -> str:
    # %-d (no leading zero) is a glibc-only strftime extension - not supported on Windows.
    return f"{d.strftime('%a, %b')} {d.day}"


def _format_time(t) -> str:
    if not t:
        return ""
    hour12 = t.hour % 12 or 12
    period = "PM" if t.hour >= 12 else "AM"
    return f"{hour12}:{t.minute:02d} {period}"


def _calendar_host_open(host_id: str, request_id: str, remove: bool = False) -> None:
    """Put (or take) an approved open the host is running for another agent on/off the host's
    connected calendar. Background, never fatal - the approval itself already happened."""
    try:
        from app.services import calendar_sync_service
        (calendar_sync_service.unsync_host_open_bg if remove else calendar_sync_service.sync_host_open_bg)(host_id, request_id)
    except Exception:
        pass


def _format_slot(d: date, start=None, end=None) -> str:
    """"Sat, Oct 10, 1:00 PM - 3:00 PM" - an alert has to say WHEN, not just which day (a counter
    on the same day only changes the time, so the date alone reads as if nothing changed)."""
    if start and end:
        return f"{_format_date(d)}, {_format_time(start)} - {_format_time(end)}"
    if start:
        return f"{_format_date(d)}, {_format_time(start)}"
    return _format_date(d)


def _format_times(start=None, end=None) -> str:
    return f"{_format_time(start)} - {_format_time(end)}" if start and end else ""


def _owns_listing(user: User, prop: Property) -> bool:
    from app.utils.plan_ids import listing_owner_key
    return listing_owner_key(user) in {prop.list_agent_mls_id, prop.co_list_agent_mls_id}


async def _get_owned_property(db: AsyncSession, current_user: User, listing_key: str) -> Property:
    result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    prop = result.scalar_one_or_none()
    if not prop or not _owns_listing(current_user, prop):
        raise ValueError("Listing not found or not owned by this agent.")
    return prop


async def _atomic_status_update(db: AsyncSession, request_id: str, where_extra, allowed_statuses: tuple, values: dict) -> bool:
    """Atomically transitions an OpenHouseHostRequest only if it's still in one of
    allowed_statuses (and matches where_extra, e.g. the caller's ownership) - the single guard
    against every "acted on twice" race: two concurrent approvals, a counter response after the
    request was already closed elsewhere, etc. Postgres re-checks an UPDATE's WHERE clause against
    the committed row under row-level locking, so only one concurrent caller's WHERE can still
    match once the other's write lands - the loser's rowcount comes back 0. Returns True if this
    call was the one that made the change."""
    stmt = OpenHouseHostRequest.__table__.update().where(
        OpenHouseHostRequest.id == request_id,
        OpenHouseHostRequest.status.in_(allowed_statuses),
        *where_extra,
    ).values(**values)
    result = await db.execute(stmt)
    return result.rowcount == 1


async def _log_request_event(
    db: AsyncSession, request_id: str, actor_id: Optional[str], action: str,
    proposed_date=None, proposed_start_time=None, proposed_end_time=None, note: Optional[str] = None,
) -> None:
    db.add(OpenHouseHostRequestEvent(
        request_id=request_id, actor_id=actor_id, action=action,
        proposed_date=proposed_date, proposed_start_time=proposed_start_time, proposed_end_time=proposed_end_time,
        note=note,
    ))


async def _is_accepted_team_member(db: AsyncSession, owner_id: str, member_agent_id: str) -> bool:
    result = await db.execute(
        select(OpenHouseTeamMember.id).where(
            OpenHouseTeamMember.owner_id == owner_id,
            OpenHouseTeamMember.member_agent_id == member_agent_id,
            OpenHouseTeamMember.status == "accepted",
        )
    )
    return result.scalar_one_or_none() is not None


async def _is_eligible_requester(db: AsyncSession, listing_owner: User, requesting_agent: User) -> bool:
    """Default (restrict_host_requests_to_team=False): same-brokerage agents may request, and an
    accepted My Team member may always request regardless of brokerage (being hand-picked
    overrides a brokerage mismatch). Restricted (True): only an accepted My Team member may
    request - same-brokerage alone is no longer enough."""
    is_team_member = await _is_accepted_team_member(db, listing_owner.id, requesting_agent.id)
    if listing_owner.restrict_host_requests_to_team:
        return is_team_member
    same_brokerage = bool(listing_owner.brokerage) and bool(requesting_agent.brokerage) and (
        listing_owner.brokerage.strip().lower() == requesting_agent.brokerage.strip().lower()
    )
    return same_brokerage or is_team_member


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


def _current_weekend_range(today: date) -> tuple[date, date]:
    """The Fri-Sun of today's calendar week (Mon-Sun), regardless of what day today is - so
    Monday through Sunday all point at the same weekend. This is what makes "needs a host"
    naturally roll forward: the instant Monday of the next week arrives, this window advances to
    the next Fri-Sun with no stored state or reset job needed."""
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    sunday = monday + timedelta(days=6)
    return friday, sunday


def _has_weekend_host(previews: List[ScheduledOpenResponse], friday: date, sunday: date) -> bool:
    return any(p.scheduled_date is not None and friday <= p.scheduled_date <= sunday for p in previews)


def _filter_authorized_events(events: List[OpenHouseEvent], listing_owner_id: str, approved_requester_ids: set) -> List[OpenHouseEvent]:
    """A kit only belongs on the LISTING side of Open House Manager (insights, scheduled opens,
    history) if whoever hosted it actually had a claim on this listing - either they're the listing
    owner hosting their own property, or they had an approved OpenHouseHostRequest for it. Anyone
    else can freely build a kit for any address (the wizard doesn't require ownership), but that
    kit is theirs alone - it must never surface on, or feed insights/visitor stats into, a listing
    they were never approved to host."""
    allowed = {listing_owner_id} | approved_requester_ids
    return [e for e in events if e.agent_id in allowed]


_EASTERN = ZoneInfo("America/New_York")


def _signin_day(visitor: OpenHouseVisitor) -> Optional[date]:
    """The (Eastern) day a guest signed in - which open they came to."""
    if not visitor.created_at:
        return None
    ts = visitor.created_at if visitor.created_at.tzinfo else visitor.created_at.replace(tzinfo=timezone.utc)
    return ts.astimezone(_EASTERN).date()


def _window_over(d: date, end_time, now_et: datetime) -> bool:
    """An approved open has happened once its day is past - or it's today and its end time passed."""
    return d < now_et.date() or (d == now_et.date() and end_time is not None and now_et.time() >= end_time)


def _hosted_windows(owner_id: str, listing_events: List[OpenHouseEvent], approved_requests: List[OpenHouseHostRequest],
                    visitors_by_event: Dict[str, List[OpenHouseVisitor]]) -> List[dict]:
    """Every open that actually happened at one listing, for the listing agent's insights.

    Kits are evergreen - one per agent per home, reused for every open they hold there - so the
    unit here is the hosted WINDOW, not the kit:
      * each approved host request whose time has passed = one open, with the guests who signed
        in at that host's kit(s) for this home on that day (any other day's sign-ins at their kit
        were never part of a window the listing agent approved, so they don't count);
      * each day the listing agent's own kit drew sign-ins = one of their own opens.
    Rows: {kit, agent_id, date, start, end, visitors}."""
    now_et = datetime.now(_EASTERN)
    kits_by_agent: Dict[str, List[OpenHouseEvent]] = {}
    for e in listing_events:
        kits_by_agent.setdefault(e.agent_id, []).append(e)

    def that_day(agent_id: str, day: date):
        kit, out = None, []
        for e in kits_by_agent.get(agent_id, []):
            vs = [v for v in visitors_by_event.get(e.id, []) if _signin_day(v) == day]
            if vs and kit is None:
                kit = e
            out += vs
        return kit or next(iter(kits_by_agent.get(agent_id, [])), None), out

    rows: List[dict] = []
    seen = set()
    for r in sorted(approved_requests, key=lambda r: (r.requested_date, r.requested_start_time or datetime.min.time())):
        if r.requesting_agent_id == owner_id or not _window_over(r.requested_date, r.requested_end_time, now_et):
            continue
        key = (r.requesting_agent_id, r.requested_date)
        if key in seen:
            continue
        seen.add(key)
        kit, vs = that_day(*key)
        rows.append({"kit": kit, "request": r, "agent_id": r.requesting_agent_id, "date": r.requested_date,
                     "start": r.requested_start_time, "end": r.requested_end_time, "visitors": vs})
    own_days = sorted({d for e in kits_by_agent.get(owner_id, []) for v in visitors_by_event.get(e.id, []) if (d := _signin_day(v))})
    for day in own_days:
        kit, vs = that_day(owner_id, day)
        same_day = kit is not None and kit.scheduled_date == day
        rows.append({"kit": kit, "request": None, "agent_id": owner_id, "date": day,
                     "start": kit.scheduled_start_time if same_day else None, "end": kit.scheduled_end_time if same_day else None, "visitors": vs})
    rows.sort(key=lambda w: (w["date"], w["start"] or datetime.min.time()), reverse=True)
    return rows


def _counts_toward_insights(event: OpenHouseEvent, visitor_count: int, today: date) -> bool:
    """Whether a kit's guest data should count as a "hosted" open, for opens_hosted_count/
    total_opens_hosted. A dated kit only counts once its date has actually passed - having a kit
    built for Saturday doesn't mean Saturday's open already happened, no matter how long ago the
    kit itself was created. An evergreen (no fixed date) kit has no "occurred" date to check
    against, so it counts once it's actually drawn a real sign-in - merely existing unused isn't
    hosting. Deleting a kit is just the host tidying up their list and must never erase a guest
    history that's already real, so a past-dated or already-visited kit keeps counting after
    deletion too."""
    if event.scheduled_date is not None:
        return event.scheduled_date < today
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
            hosting_agent_photo_url=agent.photo_url if agent else None,
        ))
    # Soonest date first; evergreen (no fixed date) opens sort after every dated one.
    previews.sort(key=lambda p: (p.scheduled_date is None, p.scheduled_date or date.max))
    return previews


def _confirmed_pending_kit_previews(
    approved_requests: List[OpenHouseHostRequest], events: List[OpenHouseEvent], agents_by_id: Dict[str, User]
) -> tuple[List[ScheduledOpenResponse], List[ScheduledOpenResponse]]:
    """Splits approved-but-not-this-exact-date requests into (covered_by_existing_kit, truly_kit_less).

    A host reuses the same physical kit (QR code / sign-in page) for every open they hold at one
    address - they don't rebuild it per date. So once a host has ANY active kit for this listing,
    every other date they're confirmed to host there is already covered by it and needs no action;
    only a host with zero kits at all for this listing has something to actually build. The exact
    date a kit's own scheduled_date names still shows under scheduled_opens as before - this only
    covers the OTHER approved dates for the same host+listing."""
    today = date.today()
    # Any date - one kit covers every date this host is confirmed for at this address.
    kit_exists_any_date = {(e.listing_key, e.agent_id) for e in events}
    # Exact slot (date + time) - that slot's own kit already appears in scheduled_opens, don't
    # double-list it. Keyed on time too, not just date - the same host can have two distinct
    # approved opens on the same day (different time windows), and matching by date alone would
    # silently drop the one that isn't the kit's own time.
    kit_exists_this_slot = {(e.listing_key, e.agent_id, e.scheduled_date, e.scheduled_start_time, e.scheduled_end_time) for e in events}

    covered: List[ScheduledOpenResponse] = []
    kit_less: List[ScheduledOpenResponse] = []
    for req in approved_requests:
        if req.requested_date < today:
            continue
        agent = agents_by_id.get(req.requesting_agent_id)
        preview = ScheduledOpenResponse(
            id=req.id,
            scheduled_date=req.requested_date,
            scheduled_start_time=req.requested_start_time,
            scheduled_end_time=req.requested_end_time,
            hosting_agent_id=req.requesting_agent_id,
            hosting_agent_name=_agent_name(agent) if agent else "Unknown agent",
            hosting_agent_brokerage=agent.brokerage if agent else None,
            hosting_agent_photo_url=agent.photo_url if agent else None,
            kit_built=(req.listing_key, req.requesting_agent_id) in kit_exists_any_date,
        )
        (covered if preview.kit_built else kit_less).append(preview)

    covered.sort(key=lambda p: p.scheduled_date or date.max)
    kit_less.sort(key=lambda p: p.scheduled_date or date.max)
    return covered, kit_less


# Sentinel hosting_agent_id for a Bright-sourced open with no OpenHousePal host - never a real
# user id, so nothing (approval checks, the cancel-open button, etc) mistakes it for one.
BRIGHT_MLS_AGENT_ID = "BRIGHT-MLS"


def _bright_mls_synthetic_opens(prop: Property, existing: List[ScheduledOpenResponse]) -> List[ScheduledOpenResponse]:
    """A listing agent who posted their open houses straight into Bright (not through
    OpenHousePal) still has real dates/times on file there. Surface every upcoming one as a
    scheduled open on the listing owner's own Manager view too - not just Discovery's
    cross-listing status endpoint (get_listing_open_house_statuses already does this for other
    agents browsing Discovery) - so they show on the calendar and block those dates the same way
    a real OpenHousePal-booked date does, instead of the listing agent's own dashboard having no
    record of them at all. Labeled BRIGHT_MLS_AGENT_ID/"BRIGHT-MLS" since no OpenHousePal agent
    actually did the hosting. A date already covered by a real in-app kit/request is skipped."""
    covered_dates = {e.scheduled_date for e in existing}
    opens: List[ScheduledOpenResponse] = []
    for slot in upcoming_mls_open_houses(prop):
        if slot["date"] in covered_dates:
            continue
        opens.append(ScheduledOpenResponse(
            id=f"bright-mls-{prop.listing_key}-{slot['date']}-{slot['start'].strftime('%H%M') if slot['start'] else 'x'}",
            scheduled_date=slot["date"],
            scheduled_start_time=slot["start"],
            scheduled_end_time=slot["end"],
            hosting_agent_id=BRIGHT_MLS_AGENT_ID,
            hosting_agent_name="BRIGHT-MLS",
            hosting_agent_brokerage=None,
            hosting_agent_photo_url=None,
            kit_built=True,  # no real host, so no "Kit Not Built Yet" badge makes sense here
        ))
    return opens


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
        select(Property.listing_key, Property.mls_open_houses, Property.mls_open_house_date, Property.mls_open_house_start_time, Property.mls_open_house_end_time)
        .where(Property.listing_key.in_(listing_keys), or_(Property.mls_open_house_date >= today, Property.mls_open_houses.is_not(None)))
    )
    # Every upcoming Bright slot per listing, soonest first (listings with none are left out).
    mls_open_houses = {row.listing_key: slots for row in mls_result.all() if (slots := upcoming_mls_open_houses(row, today))}

    # Approved host requests don't always have a kit built yet (see _confirmed_pending_kit_previews
    # for the Manager's own equivalent) - a date is genuinely "already scheduled" for Discovery's
    # purposes the moment the listing agent approves it, not only once the hosting agent gets
    # around to building the kit for it.
    requests_result = await db.execute(
        select(OpenHouseHostRequest.listing_key, OpenHouseHostRequest.requesting_agent_id, OpenHouseHostRequest.requested_date, OpenHouseHostRequest.requested_start_time, OpenHouseHostRequest.requested_end_time)
        .where(OpenHouseHostRequest.listing_key.in_(listing_keys), OpenHouseHostRequest.status == "approved", OpenHouseHostRequest.requested_date >= today)
    )
    requests_by_listing: Dict[str, List] = {}
    approved_requester_ids_by_listing: Dict[str, set] = {}
    for row in requests_result.all():
        requests_by_listing.setdefault(row.listing_key, []).append(row)
        approved_requester_ids_by_listing.setdefault(row.listing_key, set()).add(row.requesting_agent_id)

    # A kit only belongs to a listing if whoever built it actually has a current claim on it -
    # the listing's own on-platform agent, or someone with a currently-approved request (see
    # _filter_authorized_events, Manager's own equivalent of this same rule). Without this, a kit
    # left over from an approval that was since cancelled - the kit itself isn't auto-deleted, see
    # cancel_approved_open - would keep showing here as "scheduled" even after the listing agent
    # revoked it.
    props_result = await db.execute(
        select(Property.listing_key, Property.list_agent_mls_id, Property.co_list_agent_mls_id).where(Property.listing_key.in_(listing_keys))
    )
    prop_rows = props_result.all()
    mls_ids = {r.list_agent_mls_id for r in prop_rows if r.list_agent_mls_id} | {r.co_list_agent_mls_id for r in prop_rows if r.co_list_agent_mls_id}
    owner_user_id_by_mls_id: Dict[str, str] = {}
    if mls_ids:
        owners_result = await db.execute(select(User.id, User.mls_id).where(User.mls_id.in_(mls_ids)))
        owner_user_id_by_mls_id = {row.mls_id: row.id for row in owners_result.all()}
    listing_owner_ids: Dict[str, set] = {}
    for r in prop_rows:
        owners = {owner_user_id_by_mls_id[mid] for mid in (r.list_agent_mls_id, r.co_list_agent_mls_id) if mid and mid in owner_user_id_by_mls_id}
        if owners:
            listing_owner_ids[r.listing_key] = owners

    result: Dict[str, dict] = {}
    for key in listing_keys:
        allowed_agent_ids = listing_owner_ids.get(key, set()) | approved_requester_ids_by_listing.get(key, set())
        listing_events = [e for e in events_by_listing.get(key, []) if e.agent_id in allowed_agent_ids]
        owner_ids_here = listing_owner_ids.get(key, set())
        # Only the listing agent's own evergreen kit means "open any time" - a host's evergreen kit
        # is just their reusable kit; their actual dates are the approved requests.
        matched = _find_scheduled_event([e for e in listing_events if e.agent_id in owner_ids_here or e.scheduled_date is not None])
        mls_slots = mls_open_houses.get(key) or []
        # Every upcoming open for this listing, merged from every source (in-app kits, approved
        # requests awaiting a kit, and Bright's own reported date) and deduped by the full
        # date+time slot rather than just the date - the same day can genuinely have more than one
        # distinct open (different times, different hosts), and the caller (Discovery's "Already
        # Scheduled" panel) shouldn't collapse those down to one or only see the first.
        all_upcoming: Dict[tuple, dict] = {}
        for e in listing_events:
            if e.scheduled_date is not None and e.scheduled_date >= today:
                slot = (e.scheduled_date, e.scheduled_start_time, e.scheduled_end_time)
                all_upcoming[slot] = {"scheduled_date": e.scheduled_date, "scheduled_start_time": e.scheduled_start_time, "scheduled_end_time": e.scheduled_end_time, "hosting_agent_id": e.agent_id}
        for r in requests_by_listing.get(key, []):
            slot = (r.requested_date, r.requested_start_time, r.requested_end_time)
            all_upcoming.setdefault(slot, {"scheduled_date": r.requested_date, "scheduled_start_time": r.requested_start_time, "scheduled_end_time": r.requested_end_time, "hosting_agent_id": r.requesting_agent_id})
        for m in mls_slots:
            all_upcoming.setdefault((m["date"], m["start"], m["end"]), {
                "scheduled_date": m["date"],
                "scheduled_start_time": m["start"],
                "scheduled_end_time": m["end"],
                "hosting_agent_id": BRIGHT_MLS_AGENT_ID,
            })
        all_upcoming = sorted(all_upcoming.values(), key=lambda o: (o["scheduled_date"], o["scheduled_start_time"] or datetime.min.time()))

        # A real dated in-app kit is the most authoritative - someone is actively hosting on a
        # specific date, so show that over anything else.
        if matched is not None and matched.scheduled_date is not None:
            result[key] = {
                "status": "scheduled",
                "scheduled_date": matched.scheduled_date,
                "scheduled_start_time": matched.scheduled_start_time,
                "scheduled_end_time": matched.scheduled_end_time,
                "scheduled_opens": all_upcoming,
            }
            continue

        # Otherwise, Bright's own real date beats a vague evergreen (dateless) in-app kit - a
        # dateless self-hosted kit for this listing shouldn't shadow a genuine scheduled date just
        # because it happened to exist. Only fall through to "Self-Guided/Anytime" once neither a
        # dated in-app kit nor a real Bright date exists.
        if mls_slots:
            result[key] = {
                "status": "scheduled",
                "scheduled_date": mls_slots[0]["date"],
                "scheduled_start_time": mls_slots[0]["start"],
                "scheduled_end_time": mls_slots[0]["end"],
                "scheduled_opens": all_upcoming,
            }
        elif matched is not None:
            result[key] = {
                "status": "scheduled",
                "scheduled_date": None,
                "scheduled_start_time": None,
                "scheduled_end_time": None,
                "scheduled_opens": all_upcoming,
            }
        elif requests_by_listing.get(key):
            # An approved request with no kit built yet still means a host is genuinely confirmed
            # for a specific date - this shouldn't show as "Needs Host" just because the host
            # hasn't gotten around to building their kit (see _confirmed_pending_kit_previews for
            # Manager's own equivalent of this same "confirmed but kit-less" case).
            earliest = min(requests_by_listing[key], key=lambda r: (r.requested_date, r.requested_start_time or datetime.min.time()))
            result[key] = {
                "status": "scheduled",
                "scheduled_date": earliest.requested_date,
                "scheduled_start_time": earliest.requested_start_time,
                "scheduled_end_time": earliest.requested_end_time,
                "scheduled_opens": all_upcoming,
            }
        else:
            result[key] = {"status": "needs_host", "scheduled_opens": []}
    return result


async def get_my_listings(db: AsyncSession, current_user: User) -> MyListingsResponse:
    from app.utils.plan_ids import listing_owner_key
    owner_key = listing_owner_key(current_user)  # MLS ID, or "user:<id>" for agents outside Bright

    prop_result = await db.execute(
        select(Property).where(
            or_(Property.list_agent_mls_id == owner_key, Property.co_list_agent_mls_id == owner_key),
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
    weekend_friday, weekend_sunday = _current_weekend_range(today)

    for prop in properties:
        # Only kits hosted by the listing owner themselves or an agent with an approved request
        # for THIS listing count here - anyone else's kit for this address (built without ever
        # being approved) must never surface as "scheduled" or feed this listing's insights.
        approved_requester_ids = {r.requesting_agent_id for r in approved_requests_by_listing.get(prop.listing_key, [])}
        listing_events = _filter_authorized_events(events_by_listing.get(prop.listing_key, []), current_user.id, approved_requester_ids)
        active_listing_events = _filter_authorized_events(active_events_by_listing.get(prop.listing_key, []), current_user.id, approved_requester_ids)
        windows = _hosted_windows(current_user.id, listing_events, approved_requests_by_listing.get(prop.listing_key, []), visitors_by_event)
        listing_guest_count = sum(len(w["visitors"]) for w in windows)
        opens_hosted_count = len(windows)
        avg_guests = (listing_guest_count / opens_hosted_count) if opens_hosted_count else 0.0

        total_guests_all += listing_guest_count
        total_opens_all += opens_hosted_count

        # A host's evergreen kit isn't "scheduled" forever - their upcoming dates come from the
        # approved requests (covered/confirmed_pending_kit below). Only the listing agent's own
        # kits speak for themselves.
        own_active_events = [e for e in active_listing_events if e.agent_id == current_user.id]
        upcoming_approved = [r for r in approved_requests_by_listing.get(prop.listing_key, []) if r.requested_date >= today]
        open_house_status = "scheduled" if (upcoming_approved or _find_scheduled_event(own_active_events)) else "needs_host"
        scheduled_opens = _scheduled_open_previews(own_active_events, agents_by_id)
        covered_by_existing_kit, confirmed_pending_kit = _confirmed_pending_kit_previews(
            approved_requests_by_listing.get(prop.listing_key, []), active_listing_events, agents_by_id
        )
        scheduled_opens = scheduled_opens + covered_by_existing_kit
        scheduled_opens = scheduled_opens + _bright_mls_synthetic_opens(prop, scheduled_opens + confirmed_pending_kit)
        scheduled_opens = sorted(scheduled_opens, key=lambda p: (p.scheduled_date or date.max, p.scheduled_start_time or datetime.min.time()))
        note = notes_by_listing.get(prop.listing_key)
        manual_priority = bool(note and note.is_priority_override)
        # High priority = this listing has proven it draws guests (a past open had at least one
        # guest sign in) and still needs a host lined up - worth prioritizing over an unproven listing.
        computed_priority = open_house_status == "needs_host" and listing_guest_count > 0
        needs_host_this_weekend = not _has_weekend_host(scheduled_opens + confirmed_pending_kit, weekend_friday, weekend_sunday)

        listings.append(MyListingItem(
            listing_key=prop.listing_key,
            image=prop.img_src,
            photos=prop.photos if isinstance(prop.photos, list) and prop.photos else ([prop.img_src] if prop.img_src else []),
            days_on_market=property_days_on_market(prop),
            status=prop.home_status,
            address=prop.street_address,
            city=prop.city,
            state=prop.state,
            zipcode=prop.zipcode,
            price=prop.price,
            bedrooms=prop.bedrooms,
            bathrooms=prop.bathrooms,
            living_area=prop.living_area,
            lot_size_acres=prop.lot_size_acres,
            open_house_status=open_house_status,
            avg_guests_per_open=round(avg_guests, 1),
            opens_hosted_count=opens_hosted_count,
            scheduled_opens_count=len(scheduled_opens),
            scheduled_opens=scheduled_opens,
            confirmed_pending_kit=confirmed_pending_kit,
            needs_host_this_weekend=needs_host_this_weekend,
            is_high_priority=manual_priority or computed_priority,
            pending_request_count=pending_counts.get(prop.listing_key, 0),
            is_manual=prop.is_manual,
        ))

    summary = MyListingSummary(
        open_scheduled=sum(1 for l in listings if l.open_house_status == "scheduled"),
        needs_host=sum(1 for l in listings if l.needs_host_this_weekend),
        avg_guests_per_open=round((total_guests_all / total_opens_all) if total_opens_all else 0.0, 1),
        high_priority=sum(1 for l in listings if l.is_high_priority),
    )
    return MyListingsResponse(summary=summary, listings=listings)


async def create_manual_listing(db: AsyncSession, current_user: User, payload: ManualListingCreate) -> MyListingItem:
    from app.utils.plan_ids import listing_owner_key

    # image_url must be a URL this app itself generated via the manual-listing-photo upload
    # endpoint (which puts the actual bytes in S3) - never a base64 data URI or an arbitrary
    # external URL smuggled straight into the JSON body. This is the only gate keeping raw image
    # bytes out of Postgres, since this schema field is otherwise just an unconstrained string.
    if payload.image_url and not payload.image_url.startswith(f"https://{s3_service.PUBLIC_URL_HOST}/"):
        raise ValueError("Photo must be uploaded first.")

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
        list_agent_mls_id=listing_owner_key(current_user),
        is_manual=True,
        days_on_market=0,
    )
    db.add(prop)
    await db.commit()
    await db.refresh(prop)

    return MyListingItem(
        listing_key=prop.listing_key,
        image=prop.img_src,
        days_on_market=property_days_on_market(prop),
        address=prop.street_address,
        city=prop.city,
        state=prop.state,
        zipcode=prop.zipcode,
        price=prop.price,
        bedrooms=prop.bedrooms,
        bathrooms=prop.bathrooms,
        living_area=prop.living_area,
        lot_size_acres=prop.lot_size_acres,
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
    photo_url = prop.img_src
    await db.delete(prop)
    await db.commit()

    s3_service.delete_by_url(photo_url)
    await storage_ledger_service.release(db, photo_url)


async def _hydrate_host_request(
    db: AsyncSession,
    req: OpenHouseHostRequest,
    property_by_key: Dict[str, Property],
    requester_by_id: Dict[str, User],
    note_by_listing_key: Optional[Dict[str, OpenHouseListingNote]] = None,
) -> HostRequestResponse:
    prop = property_by_key.get(req.listing_key)
    requester = requester_by_id.get(req.requesting_agent_id)
    # Access instructions/lockbox info only ever reach the host once they're actually confirmed -
    # a pending, countered, or declined request must never carry them, even though the frontend
    # also chooses not to render this field pre-approval.
    note = (note_by_listing_key or {}).get(req.listing_key) if req.status == "approved" else None
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
    # Distinct on coalesce(group_id, id) so multiple slots offered in one "Request to Host"
    # submission (see create_host_request's group_id) count as a single pending item, matching the
    # single notification they generate, instead of one per slot.
    pending_result = await db.execute(
        select(func.count(func.distinct(func.coalesce(OpenHouseHostRequest.group_id, OpenHouseHostRequest.id))))
        .join(Property, Property.listing_key == OpenHouseHostRequest.listing_key)
        .where(
            OpenHouseHostRequest.listing_owner_agent_id == current_user.id,
            OpenHouseHostRequest.status == "pending",
            Property.home_status.notin_(INACTIVE_STATUSES),
        )
    )
    countered_result = await db.execute(
        select(func.count(func.distinct(func.coalesce(OpenHouseHostRequest.group_id, OpenHouseHostRequest.id)))).where(
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
    # Unseen-notification-based, like the two above, so the badge clears the moment My Requests is
    # opened (which marks these types read) rather than only once the agent actually responds.
    # Covers everything that shows up in that mailbox waiting for a first response: team-wide
    # broadcasts, direct 1:1 asks, and My Team invites.
    incoming_broadcast_unseen_result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.agent_id == current_user.id,
            Notification.is_read == False,
            Notification.type.in_(["TEAM_BROADCAST_RECEIVED", "DIRECT_HOST_REQUEST_RECEIVED", "TEAM_INVITE_RECEIVED"]),
        )
    )
    # ...but never more than what's actually still open in that mailbox: a broadcast someone else
    # filled, the owner closed, or a home that sold leaves its notification unread - it used to keep
    # the badge (and Dash's "2 to respond to") lit with nothing there to respond to.
    incoming_unseen = incoming_broadcast_unseen_result.scalar() or 0
    incoming_waiting = 0
    if incoming_unseen:
        try:
            still_open = len(await list_incoming_broadcasts(db, current_user)) + len(await list_incoming_team_invites(db, current_user))
            incoming_waiting = min(incoming_unseen, still_open)
        except Exception:  # noqa: BLE001 - a badge is never worth breaking the counts
            logger.warning("incoming broadcast recount failed", exc_info=True)
            incoming_waiting = incoming_unseen
    return ActionCountsResponse(
        pending_as_listing_agent=pending_result.scalar() or 0,
        countered_as_host=countered_result.scalar() or 0,
        listing_agent_unseen_updates=listing_agent_unseen_result.scalar() or 0,
        host_unseen_updates=host_unseen_result.scalar() or 0,
        pending_incoming_broadcasts=incoming_waiting,
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


async def _date_already_booked(db: AsyncSession, listing_key: str, target_date: date, exclude_request_id: Optional[str] = None) -> bool:
    """Whether some OTHER request already has an approved date for this listing on target_date -
    the actual double-booking guard. Checked before a request is created, before it's approved,
    and before a negotiated time is accepted, so a listing can never end up with two confirmed
    hosts for the same date.

    Also checks for a real built kit (OpenHouseEvent) on that date - a host who already built a
    kit for this listing/date never went through the approved-request path for that specific date
    (they may have been approved for a different date, or built it directly), so relying on
    OpenHouseHostRequest alone missed this case and let a broadcast get claimed for a date that
    already had a real, live open house on the calendar."""
    stmt = select(OpenHouseHostRequest.id).where(
        OpenHouseHostRequest.listing_key == listing_key,
        OpenHouseHostRequest.status == "approved",
        OpenHouseHostRequest.requested_date == target_date,
    )
    if exclude_request_id:
        stmt = stmt.where(OpenHouseHostRequest.id != exclude_request_id)
    result = await db.execute(stmt.limit(1))
    if result.scalar_one_or_none() is not None:
        return True

    event_result = await db.execute(
        select(OpenHouseEvent.agent_id).where(
            OpenHouseEvent.listing_key == listing_key,
            OpenHouseEvent.is_deleted.is_(False),
            OpenHouseEvent.is_active.is_(True),
            OpenHouseEvent.scheduled_date == target_date,
        )
    )
    event_agent_ids = {row[0] for row in event_result.all()}
    if not event_agent_ids:
        return False

    # A kit only actually conflicts if whoever built it still has a legitimate claim on this
    # listing - same rule _filter_authorized_events uses to decide what's even shown as scheduled.
    # Without this, a kit left behind after its approval was later declined/cancelled - already
    # invisible everywhere else in the app - would silently and permanently block that date, with
    # no visible entry anywhere for the listing agent to cancel and free it back up.
    prop_result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    prop = prop_result.scalar_one_or_none()
    if not prop:
        return False

    approved_result = await db.execute(
        select(OpenHouseHostRequest.requesting_agent_id).where(
            OpenHouseHostRequest.listing_key == listing_key,
            OpenHouseHostRequest.status == "approved",
            OpenHouseHostRequest.requesting_agent_id.in_(event_agent_ids),
        )
    )
    approved_agent_ids = {row[0] for row in approved_result.all()}

    agents_result = await db.execute(select(User).where(User.id.in_(event_agent_ids)))
    agents_by_id = {u.id: u for u in agents_result.scalars().all()}
    return any(
        agent_id in approved_agent_ids or ((agent := agents_by_id.get(agent_id)) and _owns_listing(agent, prop))
        for agent_id in event_agent_ids
    )


async def _agent_already_hosting_that_day(db: AsyncSession, agent_id: str, target_date: date, exclude_request_id: Optional[str] = None) -> bool:
    """One agent can't physically host two different open houses on the same day, even across
    different listings - checked alongside _date_already_booked's per-listing check."""
    stmt = select(OpenHouseHostRequest.id).where(
        OpenHouseHostRequest.requesting_agent_id == agent_id,
        OpenHouseHostRequest.status == "approved",
        OpenHouseHostRequest.requested_date == target_date,
    )
    if exclude_request_id:
        stmt = stmt.where(OpenHouseHostRequest.id != exclude_request_id)
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


async def _decline_same_date_conflicts(db: AsyncSession, request: OpenHouseHostRequest) -> None:
    """Once a request is approved for a date, any other still-open ask (pending or mid-negotiation)
    for that same listing+date can never succeed - _date_already_booked blocks it at accept time
    regardless - so auto-decline them now instead of leaving them stranded in limbo, and let their
    requester know why."""
    result = await db.execute(
        select(OpenHouseHostRequest).where(
            OpenHouseHostRequest.listing_key == request.listing_key,
            OpenHouseHostRequest.requested_date == request.requested_date,
            OpenHouseHostRequest.id != request.id,
            OpenHouseHostRequest.status.in_(["pending", "countered"]),
        )
    )
    for conflict in result.scalars().all():
        conflict.status = "declined"
        conflict.responded_at = datetime.now(timezone.utc)
        if conflict.requesting_agent_id == request.requesting_agent_id:
            # Their own other offered time for the same day - they're the one confirmed, so close it
            # without telling them "another agent was confirmed" (their approval alert says it all).
            await _log_request_event(db, conflict.id, request.listing_owner_agent_id, "declined", note="Closed - you were confirmed for another time that day.")
            continue
        await _log_request_event(db, conflict.id, request.listing_owner_agent_id, "declined", note="Another agent was already confirmed for this date.")
        try:
            db.add(Notification(
                agent_id=conflict.requesting_agent_id,
                type="HOST_REQUEST_DECLINED",
                reference_type="HOST_REQUEST",
                reference_id=conflict.id,
                title="This date was already booked",
                message="Another agent was confirmed to host this listing on that date first.",
                link="/discovery?myRequests=1",
                is_read=False,
            ))
        except Exception:
            pass


async def create_host_request(db: AsyncSession, current_user: User, payload: CreateHostRequestRequest) -> CreateHostRequestResult:
    prop_result = await db.execute(select(Property).where(Property.listing_key == payload.listing_key))
    prop = prop_result.scalar_one_or_none()
    if not prop:
        raise ValueError("This listing could not be found.")

    owner_mls_id = prop.list_agent_mls_id or prop.co_list_agent_mls_id
    listing_owner = None
    if owner_mls_id and owner_mls_id.startswith("user:"):  # hand-entered by an agent outside Bright
        listing_owner = (await db.execute(select(User).where(User.id == owner_mls_id[5:]))).scalar_one_or_none()
    elif owner_mls_id:
        owner_result = await db.execute(select(User).where(User.mls_id == owner_mls_id))
        listing_owner = owner_result.scalar_one_or_none()
    if not listing_owner:
        # The listing's MLS agent has no OpenHousePal account - nothing to persist or notify.
        # The frontend falls back to the email template for this case.
        return CreateHostRequestResult(on_platform=False, request=None)

    if not await _is_eligible_requester(db, listing_owner, current_user):
        # Enforced server-side, not just hidden in Discovery's UI - see _is_eligible_requester.
        raise ValueError("This agent isn't accepting host requests for this listing.")

    if await _date_already_booked(db, payload.listing_key, payload.requested_date):
        raise ValueError(f"{prop.street_address} already has a confirmed host for {_format_date(payload.requested_date)}. Pick a different date.")

    if await _agent_already_hosting_that_day(db, current_user.id, payload.requested_date):
        raise ValueError(f"You're already confirmed to host another open house on {_format_date(payload.requested_date)}. One open per day.")

    request_id = str(uuid.uuid4())
    request = OpenHouseHostRequest(
        id=request_id,
        listing_key=payload.listing_key,
        listing_owner_agent_id=listing_owner.id,
        requesting_agent_id=current_user.id,
        group_id=payload.group_id,
        requested_date=payload.requested_date,
        requested_start_time=payload.requested_start_time,
        requested_end_time=payload.requested_end_time,
        message=payload.message,
        status="pending",
    )
    db.add(request)
    await _log_request_event(
        db, request_id, current_user.id, "requested",
        payload.requested_date, payload.requested_start_time, payload.requested_end_time, payload.message,
    )

    try:
        # Multiple slots offered in one submission (payload.group_id shared across them) fold into
        # a single notification instead of one per slot - each slot is still its own row/still
        # independently approvable, this only affects what the listing agent sees as "new".
        existing_notif = None
        if payload.group_id:
            existing_result = await db.execute(
                select(Notification).where(
                    Notification.agent_id == listing_owner.id,
                    Notification.reference_type == "HOST_REQUEST_GROUP",
                    Notification.reference_id == payload.group_id,
                )
            )
            existing_notif = existing_result.scalar_one_or_none()

        if existing_notif:
            sibling_result = await db.execute(
                select(OpenHouseHostRequest.requested_date, OpenHouseHostRequest.requested_start_time, OpenHouseHostRequest.requested_end_time)
                .where(OpenHouseHostRequest.group_id == payload.group_id, OpenHouseHostRequest.id != request_id)
                .order_by(OpenHouseHostRequest.created_at)
            )
            slots = [(d, a, b) for d, a, b in sibling_result.all()] + [(payload.requested_date, payload.requested_start_time, payload.requested_end_time)]
            # Same day -> "Sat, Oct 10, 12:00 PM - 2:00 PM or 2:00 PM - 4:00 PM"; different days -> each in full.
            if len({d for d, _, _ in slots}) == 1:
                label = f"{_format_date(slots[0][0])}, " + " or ".join(_format_times(a, b) or "any time" for _, a, b in slots)
            else:
                label = " or ".join(_format_slot(d, a, b) for d, a, b in slots)
            existing_notif.message = f"Requested {label} at {prop.street_address}"
            existing_notif.is_read = False
        else:
            db.add(Notification(
                agent_id=listing_owner.id,
                type="HOST_REQUEST_RECEIVED",
                reference_type="HOST_REQUEST_GROUP" if payload.group_id else "HOST_REQUEST",
                reference_id=payload.group_id or request_id,
                title=f"{_agent_name(current_user)} wants to host an open house",
                message=f"Requested {_format_slot(payload.requested_date, payload.requested_start_time, payload.requested_end_time)} at {prop.street_address}",
                property_id=prop.id,
                property_address=prop.street_address,
                # Deep-links straight to this listing's Manage Hosts modal (not just the page) - see
                # the ?listingKey handler in frontend/app/open-houses/my-listings/page.tsx.
                link=f"/open-houses/my-listings?listingKey={payload.listing_key}",
                is_read=False,
            ))
    except Exception:
        existing_notif = None
    # One email per ask: a second time offered in the same ask only updates the alert above.
    if not existing_notif:
        _send_email_safely(
            listing_owner, f"{_agent_name(current_user)} wants to host {prop.street_address}", "host_request_received",
            {"agent_name": listing_owner.first_name, "requester_name": _agent_name(current_user), "property_address": prop.street_address,
             "requested_date": _format_date(payload.requested_date), "requested_time": f"{_format_time(payload.requested_start_time)} - {_format_time(payload.requested_end_time)}"}, notification_type="HOST_REQUEST_RECEIVED")

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
                link="/discovery?myRequests=1",
                is_read=False,
            ))
        except Exception:
            pass


async def approve_host_request(db: AsyncSession, current_user: User, request_id: str) -> HostRequestResponse:
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Host request not found.")

    if await _date_already_booked(db, request.listing_key, request.requested_date, exclude_request_id=request_id):
        raise ValueError("This listing already has a confirmed host for that date.")

    if await _agent_already_hosting_that_day(db, request.requesting_agent_id, request.requested_date, exclude_request_id=request_id):
        raise ValueError("This agent is already confirmed to host another open house that day.")

    claimed = await _atomic_status_update(
        db, request_id, (OpenHouseHostRequest.listing_owner_agent_id == current_user.id,), ("pending", "countered"),
        {"status": "approved", "responded_at": datetime.now(timezone.utc)},
    )
    if not claimed:
        raise ValueError("This request has already been responded to.")
    await db.refresh(request)

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    # No OpenHouseEvent ("kit") is created here - the host builds their own kit for this date
    # from the Open Houses > Your Opens flow when they're ready. Approving just confirms the
    # date/time and notifies them; it doesn't generate a kit on their behalf.
    await _fill_broadcast_and_decline_siblings(db, request)
    await _decline_same_date_conflicts(db, request)
    await _log_request_event(db, request.id, current_user.id, "approved", request.requested_date, request.requested_start_time, request.requested_end_time)
    _calendar_host_open(request.requesting_agent_id, request.id)

    notes_result = await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key == request.listing_key))
    note = notes_result.scalar_one_or_none()
    approval_message = f"You're confirmed to host {prop.street_address if prop else 'this listing'} on {_format_slot(request.requested_date, request.requested_start_time, request.requested_end_time)}"
    if note and note.notes:
        approval_message += f". Instructions from the listing agent: {note.notes}"

    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()

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
            # Straight to building the kit for this exact open (Opens page "You're confirmed to host").
            link=f"/open-houses?build={request.id}",
            is_read=False,
        ))
    except Exception:
        pass
    if requester:
        _send_email_safely(
            requester, "Your open house request was approved!", "host_request_approved",
            {"agent_name": requester.first_name, "property_address": prop.street_address if prop else "the listing",
             "confirmed_date": _format_date(request.requested_date),
             "confirmed_time": f"{_format_time(request.requested_start_time)} - {_format_time(request.requested_end_time)}",
             "listing_host_notes": note.notes if note and note.notes else ""}, notification_type="HOST_REQUEST_APPROVED")

    await db.commit()
    await db.refresh(request)
    note_by_listing_key = {request.listing_key: note} if note else {}
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {requester.id: requester} if requester else {}, note_by_listing_key)


async def decline_host_request(db: AsyncSession, current_user: User, request_id: str) -> HostRequestResponse:
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Host request not found.")

    claimed = await _atomic_status_update(
        db, request_id, (OpenHouseHostRequest.listing_owner_agent_id == current_user.id,), ("pending",),
        {"status": "declined", "responded_at": datetime.now(timezone.utc)},
    )
    if not claimed:
        raise ValueError("This request has already been responded to.")
    await db.refresh(request)
    await _log_request_event(db, request.id, current_user.id, "declined")

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
            link="/discovery?myRequests=1",
            is_read=False,
        ))
    except Exception:
        pass

    await db.commit()
    await db.refresh(request)
    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {requester.id: requester} if requester else {})


async def cancel_approved_open(db: AsyncSession, current_user: User, listing_key: str, payload: CancelApprovedOpenRequest) -> None:
    """Listing agent revokes a date they'd already approved - e.g. the host backed out, or plans
    changed. Only touches the approval itself (frees the date/agent up for the double-booking and
    one-host-one-day guards again); doesn't delete any kit the host may have already built for it,
    since that's the host's own content and a listing agent shouldn't be able to unilaterally wipe
    it - it just becomes unauthorized for this listing's insights the same way any other
    non-approved agent's kit already is (see _filter_authorized_events)."""
    conditions = [
        OpenHouseHostRequest.listing_key == listing_key,
        OpenHouseHostRequest.requesting_agent_id == payload.hosting_agent_id,
        OpenHouseHostRequest.requested_date == payload.scheduled_date,
        OpenHouseHostRequest.status == "approved",
    ]
    # Same host can be approved for more than one time slot on the same date - narrow by time too
    # when given, so this doesn't hit multiple rows and blow up on scalar_one_or_none().
    if payload.scheduled_start_time is not None:
        conditions.append(OpenHouseHostRequest.requested_start_time == payload.scheduled_start_time)
    if payload.scheduled_end_time is not None:
        conditions.append(OpenHouseHostRequest.requested_end_time == payload.scheduled_end_time)
    result = await db.execute(select(OpenHouseHostRequest).where(*conditions).order_by(OpenHouseHostRequest.created_at.desc()))
    matches = result.scalars().all()
    request = matches[0] if matches else None
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Approved open house not found.")

    claimed = await _atomic_status_update(
        db, request.id, (OpenHouseHostRequest.listing_owner_agent_id == current_user.id,), ("approved",),
        {"status": "declined", "responded_at": datetime.now(timezone.utc)},
    )
    if not claimed:
        raise ValueError("This open house has already changed status.")
    await _log_request_event(db, request.id, current_user.id, "declined", note="Cancelled by the listing agent.")
    _calendar_host_open(request.requesting_agent_id, request.id, remove=True)

    # A request accepted off a team broadcast leaves that broadcast marked "filled" - without this,
    # cancelling the approval here leaves Sent Requests showing a stale "filled" broadcast for an
    # open house that no longer exists.
    if request.broadcast_id:
        await db.execute(
            update(OpenHouseTeamBroadcast)
            .where(OpenHouseTeamBroadcast.id == request.broadcast_id, OpenHouseTeamBroadcast.status == "filled")
            .values(status="cancelled")
        )

    # The kit itself is left alone (see docstring above) but this exact date on it is no longer a
    # real booking - left untouched, a dated kit is still treated as "scheduled for that date"
    # (_scheduled_open_previews) the moment this same host becomes authorized again for this
    # listing (e.g. approved for some other date later - see _filter_authorized_events), silently
    # resurrecting a stale phantom booking that duplicates whatever real date gets approved next.
    # Clearing just the date/time turns it back into an ordinary reusable, undated kit.
    await db.execute(
        update(OpenHouseEvent)
        .where(
            OpenHouseEvent.listing_key == listing_key,
            OpenHouseEvent.agent_id == request.requesting_agent_id,
            OpenHouseEvent.scheduled_date == request.requested_date,
            OpenHouseEvent.is_deleted.is_(False),
        )
        .values(scheduled_date=None, scheduled_start_time=None, scheduled_end_time=None)
    )

    prop_result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    prop = prop_result.scalar_one_or_none()

    try:
        db.add(Notification(
            agent_id=request.requesting_agent_id,
            type="HOST_REQUEST_DECLINED",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title="An open house you were hosting was cancelled",
            message=f"The listing agent cancelled your {_format_date(payload.scheduled_date)} open house at {prop.street_address if prop else 'this listing'}.",
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/discovery?myRequests=1",
            is_read=False,
        ))
    except Exception:
        pass

    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()
    if requester:
        _send_email_safely(
            requester, "An open house you were hosting was cancelled", "host_request_declined",
            {"agent_name": requester.first_name, "property_address": prop.street_address if prop else "the listing"}, notification_type="HOST_REQUEST_DECLINED")

    await db.commit()


async def counter_host_request(db: AsyncSession, current_user: User, request_id: str, payload: CounterHostRequestRequest) -> HostRequestResponse:
    """Listing agent proposes a different date/time instead of approving or declining outright."""
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.listing_owner_agent_id != current_user.id:
        raise ValueError("Host request not found.")

    claimed = await _atomic_status_update(
        db, request_id, (OpenHouseHostRequest.listing_owner_agent_id == current_user.id,), ("pending",),
        {
            "status": "countered",
            "counter_date": payload.counter_date, "counter_start_time": payload.counter_start_time, "counter_end_time": payload.counter_end_time,
            "counter_message": payload.counter_message,
        },
    )
    if not claimed:
        raise ValueError("This request has already been responded to.")
    await db.refresh(request)
    await _log_request_event(db, request.id, current_user.id, "countered", payload.counter_date, payload.counter_start_time, payload.counter_end_time, payload.counter_message)

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    counter_note_suffix = f' Note: "{payload.counter_message.strip()}"' if payload.counter_message and payload.counter_message.strip() else ""

    requester_result = await db.execute(select(User).where(User.id == request.requesting_agent_id))
    requester = requester_result.scalar_one_or_none()

    try:
        db.add(Notification(
            agent_id=request.requesting_agent_id,
            type="HOST_REQUEST_COUNTERED",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title="A different time was proposed",
            message=f"The listing agent for {prop.street_address if prop else 'this listing'} suggested {_format_slot(payload.counter_date, payload.counter_start_time, payload.counter_end_time)} instead.{counter_note_suffix}",
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link="/discovery?myRequests=1",
            is_read=False,
        ))
    except Exception:
        pass
    if requester:
        _send_email_safely(
            requester, "A different open house time was proposed", "host_request_countered",
            {"agent_name": requester.first_name, "property_address": prop.street_address if prop else "the listing",
             "proposed_date": _format_date(payload.counter_date),
             "proposed_time": f"{_format_time(payload.counter_start_time)} - {_format_time(payload.counter_end_time)}",
             "note": payload.counter_message or ""}, notification_type="HOST_REQUEST_COUNTERED")

    await db.commit()
    await db.refresh(request)
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {requester.id: requester} if requester else {})


async def counter_back_host_request(db: AsyncSession, current_user: User, request_id: str, payload: CounterBackRequest) -> HostRequestResponse:
    """Host proposes yet another time after the listing agent countered, instead of only being
    able to accept/decline that single counter - puts the ball back in the listing agent's court
    (status returns to 'pending', same meaning it already has everywhere else) so the two sides
    can keep negotiating instead of being capped at one round."""
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.requesting_agent_id != current_user.id:
        raise ValueError("Host request not found.")

    claimed = await _atomic_status_update(
        db, request_id, (OpenHouseHostRequest.requesting_agent_id == current_user.id,), ("countered",),
        {
            "status": "pending",
            "requested_date": payload.proposed_date, "requested_start_time": payload.proposed_start_time, "requested_end_time": payload.proposed_end_time,
            "message": payload.note,
            "counter_date": None, "counter_start_time": None, "counter_end_time": None, "counter_message": None,
        },
    )
    if not claimed:
        raise ValueError("There's no proposed time to respond to on this request.")
    await db.refresh(request)
    await _log_request_event(db, request.id, current_user.id, "countered", payload.proposed_date, payload.proposed_start_time, payload.proposed_end_time, payload.note)

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    owner_result = await db.execute(select(User).where(User.id == request.listing_owner_agent_id))
    owner = owner_result.scalar_one_or_none()

    try:
        db.add(Notification(
            agent_id=request.listing_owner_agent_id,
            type="HOST_REQUEST_COUNTERED",
            reference_type="HOST_REQUEST",
            reference_id=request.id,
            title=f"{_agent_name(current_user)} proposed a different time",
            message=f"{_agent_name(current_user)} suggested {_format_slot(payload.proposed_date, payload.proposed_start_time, payload.proposed_end_time)} for {prop.street_address if prop else 'this listing'} instead",
            property_id=prop.id if prop else None,
            property_address=prop.street_address if prop else None,
            link=f"/open-houses/my-listings?listingKey={request.listing_key}",
            is_read=False,
        ))
    except Exception:
        pass
    if owner:
        _send_email_safely(
            owner, f"{_agent_name(current_user)} proposed a different open house time", "host_request_countered",
            {"agent_name": owner.first_name, "property_address": prop.street_address if prop else "your listing",
             "proposed_date": _format_date(payload.proposed_date),
             "proposed_time": f"{_format_time(payload.proposed_start_time)} - {_format_time(payload.proposed_end_time)}",
             "note": payload.note or ""}, notification_type="HOST_REQUEST_COUNTERED")

    await db.commit()
    await db.refresh(request)
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {current_user.id: current_user})


async def respond_to_counter(db: AsyncSession, current_user: User, request_id: str, accept: bool) -> HostRequestResponse:
    """The requesting agent accepts or declines the listing agent's proposed alternate time."""
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or request.requesting_agent_id != current_user.id:
        raise ValueError("Host request not found.")
    if request.status != "countered":
        raise ValueError("There's no proposed time to respond to on this request.")

    if accept:
        new_date, new_start, new_end = (request.counter_date, request.counter_start_time, request.counter_end_time)
        if await _date_already_booked(db, request.listing_key, new_date, exclude_request_id=request_id):
            raise ValueError("Someone else was already confirmed for that date while you were negotiating. Ask the listing agent to propose a different time.")
        if await _agent_already_hosting_that_day(db, request.requesting_agent_id, new_date, exclude_request_id=request_id):
            raise ValueError("You're already confirmed to host another open house that day. One open per day.")
        # No auto-created kit here either - same reasoning as approve_host_request.
        values = {
            "status": "approved", "responded_at": datetime.now(timezone.utc),
            "requested_date": new_date, "requested_start_time": new_start, "requested_end_time": new_end,
        }
    else:
        values = {"status": "declined", "responded_at": datetime.now(timezone.utc)}

    claimed = await _atomic_status_update(
        db, request_id, (OpenHouseHostRequest.requesting_agent_id == current_user.id,), ("countered",), values,
    )
    if not claimed:
        raise ValueError("There's no proposed time to respond to on this request.")
    await db.refresh(request)
    if accept:
        await _decline_same_date_conflicts(db, request)
    await _log_request_event(
        db, request.id, current_user.id, "counter_accepted" if accept else "counter_declined",
        request.requested_date if accept else None, request.requested_start_time if accept else None, request.requested_end_time if accept else None,
    )

    prop_result = await db.execute(select(Property).where(Property.listing_key == request.listing_key))
    prop = prop_result.scalar_one_or_none()

    if accept:
        notif_title = "New time confirmed"
        # This notification goes to the listing agent, not the host - phrase it from their side.
        notif_message = (
            f"{_agent_name(current_user)} agreed to host {prop.street_address if prop else 'this listing'} on "
            f"{_format_date(request.requested_date)} from {_format_time(request.requested_start_time)} to {_format_time(request.requested_end_time)}"
        )
    else:
        notif_title = "Proposed time declined"
        notif_message = f"{_agent_name(current_user)} declined the proposed time for {prop.street_address if prop else 'this listing'}"

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
            # The request is already resolved (approved/declined) by this point, so it won't show
            # under Manage Hosts' pending list anymore - Host Requests' "Recent Updates" feed is
            # where this exact notification actually surfaces.
            link="/open-houses/my-listings?panel=host-requests",
            is_read=False,
        ))
    except Exception:
        pass
    if accept:
        # Their open house now has a confirmed host - same email as a claimed team broadcast.
        _calendar_host_open(request.requesting_agent_id, request.id)
        owner = await db.get(User, request.listing_owner_agent_id)
        if owner:
            _send_email_safely(
                owner, f"{_agent_name(current_user)} is hosting your open house", "team_broadcast_claimed",
                {"agent_name": owner.first_name, "host_name": _agent_name(current_user), "property_address": prop.street_address if prop else "your listing",
                 "confirmed_date": _format_slot(request.requested_date, request.requested_start_time, request.requested_end_time)}, notification_type="TEAM_BROADCAST_CLAIMED")

    await db.commit()
    await db.refresh(request)
    note_by_listing_key = await _fetch_notes_by_listing_key(db, [request.listing_key])
    return await _hydrate_host_request(db, request, {request.listing_key: prop} if prop else {}, {current_user.id: current_user}, note_by_listing_key)


async def get_request_history(db: AsyncSession, current_user: User, request_id: str) -> List[HostRequestEventResponse]:
    """The visible thread/history for one host request - who proposed/responded what, when.
    Either party to the request may read it; nobody else can."""
    result = await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == request_id))
    request = result.scalar_one_or_none()
    if not request or current_user.id not in (request.listing_owner_agent_id, request.requesting_agent_id):
        raise ValueError("Host request not found.")

    events_result = await db.execute(
        select(OpenHouseHostRequestEvent).where(OpenHouseHostRequestEvent.request_id == request_id).order_by(OpenHouseHostRequestEvent.created_at.asc())
    )
    events = events_result.scalars().all()
    actor_ids = list({e.actor_id for e in events if e.actor_id})
    actors_by_id: Dict[str, User] = {}
    if actor_ids:
        actors_result = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors_by_id = {u.id: u for u in actors_result.scalars().all()}

    return [
        HostRequestEventResponse(
            id=e.id, actor_id=e.actor_id, actor_name=_agent_name(actors_by_id[e.actor_id]) if e.actor_id in actors_by_id else None,
            action=e.action, proposed_date=e.proposed_date, proposed_start_time=e.proposed_start_time, proposed_end_time=e.proposed_end_time,
            note=e.note, created_at=e.created_at,
        )
        for e in events
    ]


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
        AgentSearchResult(id=u.id, name=_agent_name(u), brokerage=u.brokerage, mls_id=u.mls_id, email=u.email, photo_url=u.photo_url)
        for u in result.scalars().all()
    ]


async def list_team_members(db: AsyncSession, current_user: User) -> List[TeamMemberResponse]:
    """Everyone the owner has invited, pending or accepted (declined invites are dropped
    entirely - re-inviting is what a declined agent's owner does next, not staring at a 'declined'
    row). Eligibility checks elsewhere (_is_accepted_team_member) only ever count accepted rows."""
    result = await db.execute(
        select(OpenHouseTeamMember, User)
        .join(User, User.id == OpenHouseTeamMember.member_agent_id)
        .where(OpenHouseTeamMember.owner_id == current_user.id, OpenHouseTeamMember.status != "declined")
        .order_by(OpenHouseTeamMember.created_at.desc())
    )
    return [
        TeamMemberResponse(id=member.id, agent_id=user.id, name=_agent_name(user), brokerage=user.brokerage, mls_id=user.mls_id, status=member.status, added_at=member.created_at, photo_url=user.photo_url)
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
    existing = existing_result.scalar_one_or_none()
    if existing and existing.status in ("pending", "accepted"):
        raise ValueError(f"{_agent_name(agent)} is already on your team." if existing.status == "accepted" else f"{_agent_name(agent)} already has a pending invite.")

    if existing and existing.status == "declined":
        # Re-inviting someone who declined before - reuse the row instead of hitting the unique
        # constraint, reset it back to a fresh pending invite.
        member = existing
        member.status = "pending"
        member.responded_at = None
    else:
        member = OpenHouseTeamMember(owner_id=current_user.id, member_agent_id=agent.id, status="pending")
        db.add(member)
    await db.flush()  # populate member.id before it's referenced below

    try:
        db.add(Notification(
            agent_id=agent.id,
            type="TEAM_INVITE_RECEIVED",
            reference_type="TEAM_MEMBER",
            reference_id=member.id,
            title=f"{_agent_name(current_user)} invited you to their Open House Team",
            message=f"{_agent_name(current_user)} wants you on their team of trusted hosts.",
            link="/discovery?myRequests=1",
            is_read=False,
        ))
    except Exception:
        pass
    _send_email_safely(
        agent, f"{_agent_name(current_user)} invited you to their Open House Team", "team_invite_received",
        {"agent_name": agent.first_name, "owner_name": _agent_name(current_user), "owner_brokerage": current_user.brokerage or ""}, notification_type="TEAM_INVITE_RECEIVED")

    await db.commit()
    await db.refresh(member)
    return TeamMemberResponse(id=member.id, agent_id=agent.id, name=_agent_name(agent), brokerage=agent.brokerage, mls_id=agent.mls_id, status=member.status, added_at=member.created_at, photo_url=agent.photo_url)


async def remove_team_member(db: AsyncSession, current_user: User, member_id: str) -> None:
    result = await db.execute(select(OpenHouseTeamMember).where(OpenHouseTeamMember.id == member_id, OpenHouseTeamMember.owner_id == current_user.id))
    member = result.scalar_one_or_none()
    if not member:
        raise ValueError("Team member not found.")
    await db.delete(member)
    await db.commit()


async def list_incoming_team_invites(db: AsyncSession, current_user: User) -> List[IncomingTeamInviteResponse]:
    result = await db.execute(
        select(OpenHouseTeamMember, User)
        .join(User, User.id == OpenHouseTeamMember.owner_id)
        .where(OpenHouseTeamMember.member_agent_id == current_user.id, OpenHouseTeamMember.status == "pending")
        .order_by(OpenHouseTeamMember.created_at.desc())
    )
    return [
        IncomingTeamInviteResponse(id=member.id, owner_agent_id=owner.id, owner_agent_name=_agent_name(owner), owner_agent_brokerage=owner.brokerage, status=member.status, created_at=member.created_at)
        for member, owner in result.all()
    ]


async def list_my_team_memberships(db: AsyncSession, current_user: User) -> List[TeamMembershipResponse]:
    """Every other agent's Open House Team the current agent is an accepted member of - there's no
    cap on how many teams one agent can belong to (the uniqueness constraint on
    OpenHouseTeamMember is per owner+member pair, not per member), so this is always a list.
    Powers Discovery's Team filter, letting an agent narrow results to one, several, or all of
    the teams they're on."""
    result = await db.execute(
        select(OpenHouseTeamMember, User)
        .join(User, User.id == OpenHouseTeamMember.owner_id)
        .where(OpenHouseTeamMember.member_agent_id == current_user.id, OpenHouseTeamMember.status == "accepted")
        .order_by(User.first_name, User.last_name)
    )
    return [
        TeamMembershipResponse(owner_id=owner.id, owner_name=_agent_name(owner), owner_brokerage=owner.brokerage)
        for _, owner in result.all()
    ]


async def respond_to_team_invite(db: AsyncSession, current_user: User, member_id: str, accept: bool) -> IncomingTeamInviteResponse:
    result = await db.execute(select(OpenHouseTeamMember).where(OpenHouseTeamMember.id == member_id, OpenHouseTeamMember.member_agent_id == current_user.id))
    member = result.scalar_one_or_none()
    if not member:
        raise ValueError("Team invite not found.")

    claim = await db.execute(
        OpenHouseTeamMember.__table__.update()
        .where(OpenHouseTeamMember.id == member_id, OpenHouseTeamMember.member_agent_id == current_user.id, OpenHouseTeamMember.status == "pending")
        .values(status="accepted" if accept else "declined", responded_at=datetime.now(timezone.utc))
    )
    if claim.rowcount != 1:
        raise ValueError("This invite has already been responded to.")
    await db.refresh(member)

    owner_result = await db.execute(select(User).where(User.id == member.owner_id))
    owner = owner_result.scalar_one_or_none()
    try:
        db.add(Notification(
            agent_id=member.owner_id,
            type="TEAM_INVITE_ACCEPTED" if accept else "TEAM_INVITE_DECLINED",
            reference_type="TEAM_MEMBER",
            reference_id=member.id,
            title=f"{_agent_name(current_user)} {'accepted' if accept else 'declined'} your team invite",
            message=f"{_agent_name(current_user)} is {'now' if accept else 'not'} on your Open House Team.",
            link="/open-houses/my-listings?panel=team",
            is_read=False,
        ))
    except Exception:
        pass
    if accept and owner:
        from app.services.agent_email_service import absolute, email_agent
        email_agent(owner, "TEAM_INVITE_ACCEPTED", "team_invite_accepted", f"{_agent_name(current_user)} joined your Open House Team",
                    {"member_name": _agent_name(current_user), "member_brokerage": current_user.brokerage or "",
                     "link": absolute("/open-houses/my-listings?panel=team")})

    await db.commit()
    await db.refresh(member)
    return IncomingTeamInviteResponse(id=member.id, owner_agent_id=member.owner_id, owner_agent_name=_agent_name(owner) if owner else "Unknown agent", owner_agent_brokerage=owner.brokerage if owner else None, status=member.status, created_at=member.created_at)


async def update_restriction(db: AsyncSession, current_user: User, payload: UpdateRestrictionRequest) -> None:
    current_user.restrict_host_requests_to_team = payload.restrict_host_requests_to_team
    db.add(current_user)
    await db.commit()


async def get_trusted_hosts(db: AsyncSession, current_user: User) -> List[TrustedHostResponse]:
    """Agents who've ever been approved to host for this listing agent, across any of their
    listings - powers the 'Hosted for you before' quick-pick. Looked up from the request's audit
    log (any 'approved' or 'counter_accepted' event) rather than the request's current status - a
    request that was later cancelled, or whose kit got deleted, must not erase trust that was
    already established once the listing agent approved them."""
    result = await db.execute(
        select(
            OpenHouseHostRequest.requesting_agent_id,
            func.count(func.distinct(OpenHouseHostRequest.id)).label("times_hosted"),
            func.max(OpenHouseHostRequestEvent.created_at).label("last_hosted_at"),
        )
        .join(OpenHouseHostRequestEvent, OpenHouseHostRequestEvent.request_id == OpenHouseHostRequest.id)
        .where(
            OpenHouseHostRequest.listing_owner_agent_id == current_user.id,
            OpenHouseHostRequestEvent.action.in_(["approved", "counter_accepted"]),
        )
        .group_by(OpenHouseHostRequest.requesting_agent_id)
        .order_by(func.count(func.distinct(OpenHouseHostRequest.id)).desc(), func.max(OpenHouseHostRequestEvent.created_at).desc())
    )
    rows = result.all()
    if not rows:
        return []
    agent_ids = [r.requesting_agent_id for r in rows]
    agents_result = await db.execute(select(User).where(User.id.in_(agent_ids)))
    agents_by_id = {u.id: u for u in agents_result.scalars().all()}
    return [
        TrustedHostResponse(
            agent_id=r.requesting_agent_id, name=_agent_name(agents_by_id[r.requesting_agent_id]) if r.requesting_agent_id in agents_by_id else "Unknown agent",
            brokerage=agents_by_id[r.requesting_agent_id].brokerage if r.requesting_agent_id in agents_by_id else None,
            mls_id=agents_by_id[r.requesting_agent_id].mls_id if r.requesting_agent_id in agents_by_id else None,
            photo_url=agents_by_id[r.requesting_agent_id].photo_url if r.requesting_agent_id in agents_by_id else None,
            times_hosted=r.times_hosted, last_hosted_at=r.last_hosted_at,
        )
        for r in rows if r.last_hosted_at is not None
    ]


async def create_broadcast(db: AsyncSession, current_user: User, payload: CreateBroadcastRequest) -> BroadcastResponse:
    """Team-wide (payload.target_agent_id unset) or a direct 1:1 ask to one specific agent (set) -
    same OpenHouseTeamBroadcast row either way, same open/filled/cancelled lifecycle and the same
    atomic-claim-or-counter response path (respond_to_broadcast), just a different recipient set."""
    prop = await _get_owned_property(db, current_user, payload.listing_key)

    if not payload.is_anytime and (not payload.requested_start_time or not payload.requested_end_time):
        raise ValueError("Please choose a start and end time, or mark this as Anytime.")

    # Idempotency guard: a double-click / duplicate submit of the exact same ask (same listing,
    # date, time window, and recipient) while an identical one is still open would otherwise create
    # two live broadcasts - and since each can independently be claimed, that's how the same agent
    # ends up hosting the same date twice with two different accepted times. Reuse the existing one
    # instead of creating a near-identical sibling.
    existing_result = await db.execute(
        select(OpenHouseTeamBroadcast).where(
            OpenHouseTeamBroadcast.owner_id == current_user.id,
            OpenHouseTeamBroadcast.listing_key == payload.listing_key,
            OpenHouseTeamBroadcast.status == "open",
            OpenHouseTeamBroadcast.requested_date == payload.requested_date,
            OpenHouseTeamBroadcast.is_anytime == payload.is_anytime,
            OpenHouseTeamBroadcast.requested_start_time == (None if payload.is_anytime else payload.requested_start_time),
            OpenHouseTeamBroadcast.requested_end_time == (None if payload.is_anytime else payload.requested_end_time),
            OpenHouseTeamBroadcast.target_agent_id == payload.target_agent_id,
        )
    )
    existing_broadcast = existing_result.scalar_one_or_none()
    if existing_broadcast:
        prop_for_existing = prop
        target_for_existing: Optional[User] = None
        if existing_broadcast.target_agent_id:
            target_result = await db.execute(select(User).where(User.id == existing_broadcast.target_agent_id))
            target_for_existing = target_result.scalar_one_or_none()
        return BroadcastResponse(
            id=existing_broadcast.id, listing_key=existing_broadcast.listing_key, property_address=prop_for_existing.street_address, property_image=prop_for_existing.img_src,
            requested_date=existing_broadcast.requested_date, requested_start_time=existing_broadcast.requested_start_time, requested_end_time=existing_broadcast.requested_end_time,
            is_anytime=existing_broadcast.is_anytime,
            message=existing_broadcast.message, status=existing_broadcast.status, created_at=existing_broadcast.created_at, interested_requests=[],
            is_direct=bool(existing_broadcast.target_agent_id), target_agent_name=_agent_name(target_for_existing) if target_for_existing else None,
        )

    if await _date_already_booked(db, payload.listing_key, payload.requested_date):
        raise ValueError("This listing already has a confirmed host for that date - pick another date.")

    target_agent: Optional[User] = None
    recipients: List[tuple[str, Optional[str], str]] = []  # (agent_id, email, first_name)
    if payload.target_agent_id:
        target_result = await db.execute(select(User).where(User.id == payload.target_agent_id))
        target_agent = target_result.scalar_one_or_none()
        if not target_agent or target_agent.id == current_user.id:
            raise ValueError("That agent couldn't be found.")
        recipients = [(target_agent.id, target_agent.email, target_agent.first_name)]
    else:
        members_result = await db.execute(
            select(OpenHouseTeamMember).where(OpenHouseTeamMember.owner_id == current_user.id, OpenHouseTeamMember.status == "accepted")
        )
        members = members_result.scalars().all()
        if not members:
            raise ValueError("Add agents to your Open House Team before sending a broadcast.")
        member_ids = [m.member_agent_id for m in members]
        member_users_result = await db.execute(select(User).where(User.id.in_(member_ids)))
        member_users_by_id = {u.id: u for u in member_users_result.scalars().all()}
        recipients = [
            (m.member_agent_id, member_users_by_id[m.member_agent_id].email, member_users_by_id[m.member_agent_id].first_name)
            for m in members if m.member_agent_id in member_users_by_id
        ]

    broadcast = OpenHouseTeamBroadcast(
        listing_key=payload.listing_key,
        owner_id=current_user.id,
        requested_date=payload.requested_date,
        requested_start_time=None if payload.is_anytime else payload.requested_start_time,
        requested_end_time=None if payload.is_anytime else payload.requested_end_time,
        is_anytime=payload.is_anytime,
        message=payload.message,
        target_agent_id=target_agent.id if target_agent else None,
    )
    db.add(broadcast)
    await db.flush()

    requested_time_label = "Anytime" if payload.is_anytime else f"{_format_time(payload.requested_start_time)} - {_format_time(payload.requested_end_time)}"

    # Distinct from HOST_REQUEST_RECEIVED (which always means "someone wants to host MY listing" -
    # the opposite direction) - this is "someone wants ME to host THEIR listing," same direction as
    # a team broadcast, just addressed to one agent instead of the whole team.
    notif_type = "DIRECT_HOST_REQUEST_RECEIVED" if target_agent else "TEAM_BROADCAST_RECEIVED"
    notif_title = f"{_agent_name(current_user)} wants you to host an open house" if target_agent else f"{_agent_name(current_user)} needs a host"
    # Broad info only - date/time window and the listing itself. No visitor/guest data exists yet
    # at this stage anyway, and host notes/instructions are withheld until someone's approved.
    for agent_id, email, first_name in recipients:
        try:
            db.add(Notification(
                agent_id=agent_id,
                type=notif_type,
                reference_type="TEAM_BROADCAST",
                reference_id=broadcast.id,
                title=notif_title,
                message=f"{prop.street_address} - {_format_date(payload.requested_date)}",
                property_id=prop.id,
                property_address=prop.street_address,
                link="/discovery?myRequests=1",
                is_read=False,
            ))
        except Exception:
            pass
        _send_email_safely(
            (await db.get(User, agent_id)) or email, notif_title, "direct_host_request" if target_agent else "team_broadcast_received",
            {"agent_name": first_name, "owner_name": _agent_name(current_user), "property_address": prop.street_address,
             "requested_date": _format_date(payload.requested_date),
             "requested_time": requested_time_label}, notification_type="HOST_REQUEST" if target_agent else "TEAM_BROADCAST_RECEIVED")

    await db.commit()
    await db.refresh(broadcast)
    return BroadcastResponse(
        id=broadcast.id, listing_key=broadcast.listing_key, property_address=prop.street_address, property_image=prop.img_src,
        requested_date=broadcast.requested_date, requested_start_time=broadcast.requested_start_time, requested_end_time=broadcast.requested_end_time,
        is_anytime=broadcast.is_anytime,
        message=broadcast.message, status=broadcast.status, created_at=broadcast.created_at, interested_requests=[],
        is_direct=bool(target_agent), target_agent_name=_agent_name(target_agent) if target_agent else None,
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
        target = None
        if broadcast.target_agent_id:
            target_result = await db.execute(select(User).where(User.id == broadcast.target_agent_id))
            target = target_result.scalar_one_or_none()
        responses.append(BroadcastResponse(
            id=broadcast.id, listing_key=broadcast.listing_key, property_address=prop.street_address if prop else None, property_image=prop.img_src if prop else None,
            requested_date=broadcast.requested_date, requested_start_time=broadcast.requested_start_time, requested_end_time=broadcast.requested_end_time,
            is_anytime=broadcast.is_anytime,
            message=broadcast.message, status=broadcast.status, created_at=broadcast.created_at, interested_requests=hydrated_requests,
            is_direct=bool(broadcast.target_agent_id), target_agent_name=_agent_name(target) if target else None,
        ))
    return responses


async def list_incoming_broadcasts(db: AsyncSession, current_user: User) -> List[IncomingBroadcastResponse]:
    owner_ids_result = await db.execute(
        select(OpenHouseTeamMember.owner_id).where(OpenHouseTeamMember.member_agent_id == current_user.id, OpenHouseTeamMember.status == "accepted")
    )
    team_owner_ids = [row[0] for row in owner_ids_result.all()]

    broadcasts_result = await db.execute(
        select(OpenHouseTeamBroadcast)
        .where(
            OpenHouseTeamBroadcast.status == "open",
            or_(
                # Team-wide asks from teams I'm on - a direct 1:1 ask to someone else stays theirs.
                and_(OpenHouseTeamBroadcast.owner_id.in_(team_owner_ids), OpenHouseTeamBroadcast.target_agent_id.is_(None)) if team_owner_ids else False,
                OpenHouseTeamBroadcast.target_agent_id == current_user.id,
            ),
        )
        .order_by(OpenHouseTeamBroadcast.created_at.desc())
    )
    broadcasts = broadcasts_result.scalars().all()
    if not broadcasts:
        return []

    listing_keys = list({b.listing_key for b in broadcasts})
    prop_result = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
    property_by_key = {p.listing_key: p for p in prop_result.scalars().all()}

    owner_ids = list({b.owner_id for b in broadcasts})
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
            is_direct=bool(broadcast.target_agent_id),
            id=broadcast.id, listing_key=broadcast.listing_key, property_address=prop.street_address if prop else None, property_image=prop.img_src if prop else None,
            owner_agent_name=_agent_name(owner) if owner else "Unknown agent", owner_agent_brokerage=owner.brokerage if owner else None,
            requested_date=broadcast.requested_date, requested_start_time=broadcast.requested_start_time, requested_end_time=broadcast.requested_end_time,
            is_anytime=broadcast.is_anytime,
            message=broadcast.message, status=broadcast.status, created_at=broadcast.created_at, my_response_status=None,
        ))
    return result


async def respond_to_broadcast(db: AsyncSession, current_user: User, broadcast_id: str, payload: RespondToBroadcastRequest) -> HostRequestResponse:
    """Team member responds to a team-wide broadcast. Omitting proposed_* means 'I'll host exactly
    as requested' - claimed atomically (see below) so the first member to do so gets the open
    house immediately, no owner approval step needed since the terms already match what they
    asked for. A differing proposed_* is a counter instead: creates a normal pending host request
    the owner reviews like any direct ask - approving it already closes the broadcast for everyone
    else via _fill_broadcast_and_decline_siblings, so no separate state machine is needed there."""
    result = await db.execute(select(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.id == broadcast_id))
    broadcast = result.scalar_one_or_none()
    if not broadcast or broadcast.status != "open":
        raise ValueError("This open house request is no longer available.")

    is_direct_target = broadcast.target_agent_id == current_user.id
    if broadcast.target_agent_id and not is_direct_target:
        raise ValueError("This open house was offered to another agent.")
    if not is_direct_target and not await _is_accepted_team_member(db, broadcast.owner_id, current_user.id):
        raise ValueError("You're not on this agent's Open House Team.")

    existing_result = await db.execute(
        select(OpenHouseHostRequest).where(OpenHouseHostRequest.broadcast_id == broadcast_id, OpenHouseHostRequest.requesting_agent_id == current_user.id)
    )
    if existing_result.scalar_one_or_none():
        raise ValueError("You've already responded to this request.")

    if broadcast.is_anytime:
        if not payload.proposed_start_time or not payload.proposed_end_time:
            raise ValueError("Please choose a start and end time for this open house.")
        # No time to match against - any time offered for the same date is an instant claim just
        # like a normal exact-match accept, not a counter needing the owner's review, since the
        # owner already said any time works. A different date still counts as a counter.
        is_exact_match = not payload.proposed_date or payload.proposed_date == broadcast.requested_date
        final_start_time = payload.proposed_start_time
        final_end_time = payload.proposed_end_time
    else:
        is_exact_match = not (payload.proposed_date or payload.proposed_start_time or payload.proposed_end_time) or (
            payload.proposed_date == broadcast.requested_date
            and payload.proposed_start_time == broadcast.requested_start_time
            and payload.proposed_end_time == broadcast.requested_end_time
        )
        final_start_time = broadcast.requested_start_time
        final_end_time = broadcast.requested_end_time

    prop_result = await db.execute(select(Property).where(Property.listing_key == broadcast.listing_key))
    prop = prop_result.scalar_one_or_none()
    request_id = str(uuid.uuid4())

    if is_exact_match:
        # Same double-booking guard the manual approve path already applies (_date_already_booked's
        # own docstring says this should be checked "before a negotiated time is accepted" - this
        # instant-claim path was the one place that never actually did, which let an Anytime
        # broadcast get claimed for a date the listing (or the claiming agent) already had a real
        # open house on, producing two live entries for the same date.
        if await _date_already_booked(db, broadcast.listing_key, broadcast.requested_date):
            raise ValueError("This listing already has a confirmed host for that date.")
        if await _agent_already_hosting_that_day(db, current_user.id, broadcast.requested_date):
            raise ValueError("You're already confirmed to host another open house that day.")

        # Atomic claim: only the caller whose UPDATE actually flips 'open' -> 'filled' wins - a
        # second, near-simultaneous accept sees rowcount 0 and is rejected outright, so two agents
        # can never both be assigned the same broadcast (Postgres re-checks this UPDATE's WHERE
        # against the committed row under row-level locking, so a concurrent second caller's WHERE
        # no longer matches once the first commits).
        claim = await db.execute(
            OpenHouseTeamBroadcast.__table__.update()
            .where(OpenHouseTeamBroadcast.id == broadcast_id, OpenHouseTeamBroadcast.status == "open")
            .values(status="filled")
        )
        if claim.rowcount != 1:
            raise ValueError("This slot was just taken by another team member.")

        request = OpenHouseHostRequest(
            id=request_id,
            listing_key=broadcast.listing_key,
            listing_owner_agent_id=broadcast.owner_id,
            requesting_agent_id=current_user.id,
            requested_date=broadcast.requested_date,
            requested_start_time=final_start_time,
            requested_end_time=final_end_time,
            status="approved",
            responded_at=datetime.now(timezone.utc),
            broadcast_id=broadcast.id,
        )
        db.add(request)
        # The listing agent's original ask (and their note, if they sent one with it) - without
        # this, a request born from a direct/team broadcast had no record of what was actually
        # asked for or why, only the responding agent's own side of it. Left as the broadcast's own
        # (possibly Anytime/null) time here on purpose - this row is a record of what was ORIGINALLY
        # asked, not the time that ended up getting picked.
        await _log_request_event(db, request_id, broadcast.owner_id, "note", broadcast.requested_date, broadcast.requested_start_time, broadcast.requested_end_time, broadcast.message)
        await _log_request_event(db, request_id, current_user.id, "requested", broadcast.requested_date, final_start_time, final_end_time)
        await _log_request_event(db, request_id, current_user.id, "approved", broadcast.requested_date, final_start_time, final_end_time)
        await _fill_broadcast_and_decline_siblings(db, request)
        _calendar_host_open(current_user.id, request_id)

        try:
            db.add(Notification(
                agent_id=broadcast.owner_id,
                type="TEAM_BROADCAST_CLAIMED",
                reference_type="HOST_REQUEST",
                reference_id=request_id,
                title=f"{_agent_name(current_user)} is hosting!",
                message=f"{_agent_name(current_user)} claimed {prop.street_address if prop else 'your listing'} for {_format_slot(broadcast.requested_date, final_start_time, final_end_time)} - all set.",
                property_id=prop.id if prop else None,
                property_address=prop.street_address if prop else None,
                link="/open-houses/my-listings?panel=team",
                is_read=False,
            ))
        except Exception:
            pass
        owner_result = await db.execute(select(User).where(User.id == broadcast.owner_id))
        owner = owner_result.scalar_one_or_none()
        if owner:
            _send_email_safely(
                owner, f"{_agent_name(current_user)} is hosting your open house", "team_broadcast_claimed",
                {"agent_name": owner.first_name, "host_name": _agent_name(current_user), "property_address": prop.street_address if prop else "your listing",
                 "confirmed_date": _format_slot(broadcast.requested_date, final_start_time, final_end_time)}, notification_type="TEAM_BROADCAST_CLAIMED")
    else:
        request = OpenHouseHostRequest(
            id=request_id,
            listing_key=broadcast.listing_key,
            listing_owner_agent_id=broadcast.owner_id,
            requesting_agent_id=current_user.id,
            requested_date=payload.proposed_date,
            requested_start_time=payload.proposed_start_time,
            requested_end_time=payload.proposed_end_time,
            message=payload.note,
            status="pending",
            broadcast_id=broadcast.id,
        )
        db.add(request)
        # Same as the exact-match branch above - record the listing agent's original ask/note
        # first, before the responding agent's own counter-proposal.
        await _log_request_event(db, request_id, broadcast.owner_id, "note", broadcast.requested_date, broadcast.requested_start_time, broadcast.requested_end_time, broadcast.message)
        await _log_request_event(db, request_id, current_user.id, "requested", payload.proposed_date, payload.proposed_start_time, payload.proposed_end_time, payload.note)

        try:
            db.add(Notification(
                agent_id=broadcast.owner_id,
                type="TEAM_BROADCAST_RESPONSE",
                reference_type="HOST_REQUEST",
                reference_id=request_id,
                title=f"{_agent_name(current_user)} proposed a different time",
                message=f"{_agent_name(current_user)} suggested {_format_slot(payload.proposed_date, payload.proposed_start_time, payload.proposed_end_time)} for {prop.street_address if prop else 'your listing'} instead",
                property_id=prop.id if prop else None,
                property_address=prop.street_address if prop else None,
                link="/open-houses/my-listings?panel=team",
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

    approved_result = await db.execute(
        select(OpenHouseHostRequest).where(
            OpenHouseHostRequest.listing_key == listing_key,
            OpenHouseHostRequest.status == "approved",
        )
    )
    approved_requests = approved_result.scalars().all()

    # Only kits hosted by the listing owner or an agent approved to host THIS listing belong here -
    # see _filter_authorized_events. Anyone else's kit for this address never surfaces on the
    # listing owner's side, whether or not it's since been deleted.
    approved_requester_ids = {r.requesting_agent_id for r in approved_requests}
    events = _filter_authorized_events(events, current_user.id, approved_requester_ids)
    active_events = [e for e in events if not e.is_deleted]

    hosting_agent_ids = {e.agent_id for e in events} | approved_requester_ids
    agents_by_id: Dict[str, User] = {}
    if hosting_agent_ids:
        agents_result = await db.execute(select(User).where(User.id.in_(hosting_agent_ids)))
        agents_by_id = {u.id: u for u in agents_result.scalars().all()}

    covered_by_existing_kit, confirmed_pending_kit = _confirmed_pending_kit_previews(approved_requests, active_events, agents_by_id)

    event_ids = [e.id for e in events]
    visitors_by_event: Dict[str, List[OpenHouseVisitor]] = {}
    if event_ids:
        visitors_result = await db.execute(select(OpenHouseVisitor).where(OpenHouseVisitor.open_house_event_id.in_(event_ids)))
        for visitor in visitors_result.scalars().all():
            visitors_by_event.setdefault(visitor.open_house_event_id, []).append(visitor)

    today = date.today()
    # Upcoming: the listing agent's own kits (an evergreen one reads "anytime"); every host's dates
    # come from their approved requests below (covered_by_existing_kit / confirmed_pending_kit).
    scheduled_opens: List[ScheduledOpenResponse] = []
    for event in active_events:
        if event.agent_id != current_user.id:
            continue
        if (event.is_active and event.scheduled_date is None) or (event.scheduled_date is not None and event.scheduled_date >= today):
            agent = agents_by_id.get(event.agent_id)
            scheduled_opens.append(ScheduledOpenResponse(
                id=event.id,
                scheduled_date=event.scheduled_date,
                scheduled_start_time=event.scheduled_start_time,
                scheduled_end_time=event.scheduled_end_time,
                hosting_agent_id=event.agent_id,
                hosting_agent_name=_agent_name(agent) if agent else "Unknown agent",
                hosting_agent_brokerage=agent.brokerage if agent else None,
                hosting_agent_photo_url=agent.photo_url if agent else None,
            ))

    # History + insights: one row per hosted window (see _hosted_windows) - a host reuses one kit
    # for every date, so each row is that date's guests, not the kit's lifetime total.
    history: List[OpenHouseHistoryRow] = []
    total_guests = total_unrepresented = total_requested_updates = 0
    windows = _hosted_windows(current_user.id, events, approved_requests, visitors_by_event)
    for w in windows:
        agent = agents_by_id.get(w["agent_id"])
        visitors = w["visitors"]
        unrepresented = sum(1 for v in visitors if (v.has_agent or "").upper() == "NO")
        requested_updates = sum(1 for v in visitors if v.interested_in_similar)
        total_guests += len(visitors)
        total_unrepresented += unrepresented
        total_requested_updates += requested_updates
        kit = w["kit"]
        history.append(OpenHouseHistoryRow(
            id=kit.id if kit else w["request"].id,
            event_date=w["date"],
            start_time=w["start"],
            end_time=w["end"],
            hosting_agent_id=w["agent_id"],
            hosting_agent_name=_agent_name(agent) if agent else "Unknown agent",
            hosting_agent_brokerage=agent.brokerage if agent else None,
            total_guests=len(visitors),
            unrepresented_guests=unrepresented,
            requested_updates=requested_updates,
            share_guests_enabled=bool(kit and kit.share_guests_with_listing_agent) and w["agent_id"] != current_user.id,
        ))
    total_opens_hosted = len(windows)

    # Soonest date first, not creation order. Includes dates covered by this same kit that don't
    # match the kit's own scheduled_date - see _confirmed_pending_kit_previews.
    scheduled_opens = scheduled_opens + covered_by_existing_kit
    scheduled_opens = scheduled_opens + _bright_mls_synthetic_opens(prop, scheduled_opens + confirmed_pending_kit)
    scheduled_opens = sorted(scheduled_opens, key=lambda o: (o.scheduled_date or date.max, o.scheduled_start_time or datetime.min.time()))

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
        confirmed_pending_kit=confirmed_pending_kit,
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
    if payload.notes is not None:
        upcoming = (await db.execute(select(OpenHouseHostRequest).where(
            OpenHouseHostRequest.listing_key == listing_key, OpenHouseHostRequest.status == "approved",
            OpenHouseHostRequest.requested_date >= date.today()))).scalars().all()
        for r in upcoming:
            _calendar_host_open(r.requesting_agent_id, r.id)
    return ListingHostNoteResponse(notes=note.notes, is_priority_override=note.is_priority_override, updated_at=note.updated_at)


async def get_shared_guests(db: AsyncSession, current_user: User, event_id: str, window_date: Optional[date] = None) -> List[VisitorResponse]:
    """Actual guest name/email/phone for one open house, visible to the LISTING agent only when
    the hosting agent has explicitly opted in for that specific kit - basic anonymous counts
    (get_listing_detail's key_metrics/history) are always visible regardless; this is the opt-in
    identity layer on top. 403s (via ValueError, mapped to 404 by the route) for anyone else,
    including the host themselves (they already see their own guests via the existing
    /api/open-houses/{id}/visitors endpoint)."""
    result = await db.execute(select(OpenHouseEvent).where(OpenHouseEvent.id == event_id))
    event = result.scalar_one_or_none()
    if not event or not event.listing_key:
        raise ValueError("Guest details aren't available for this open house.")

    # Same ownership check get_listing_detail uses (Property MLS-id match) - OpenHouseEvent's own
    # listing_owner_agent_id column is never populated anywhere in this codebase, so it can't be
    # used as a gate here.
    prop_result = await db.execute(select(Property).where(Property.listing_key == event.listing_key))
    prop = prop_result.scalar_one_or_none()
    if not prop or not _owns_listing(current_user, prop) or not event.share_guests_with_listing_agent:
        raise ValueError("Guest details aren't available for this open house.")

    # One evergreen kit covers every date this host holds here - so a history row asks for its own
    # day only (window_date), across all of this host's kits for the home.
    kit_ids = [event_id]
    if window_date is not None:
        kit_ids = (await db.execute(select(OpenHouseEvent.id).where(
            OpenHouseEvent.listing_key == event.listing_key, OpenHouseEvent.agent_id == event.agent_id))).scalars().all()
    visitors_result = await db.execute(
        select(OpenHouseVisitor).where(OpenHouseVisitor.open_house_event_id.in_(kit_ids)).order_by(OpenHouseVisitor.created_at.asc())
    )
    visitors = [v for v in visitors_result.scalars().all() if window_date is None or _signin_day(v) == window_date]
    return [
        VisitorResponse(
            id=v.id, full_name=v.full_name, email=v.email, phone=v.phone, has_agent=v.has_agent,
            interested_in_similar=v.interested_in_similar, notes=None, lead_id=None, created_at=v.created_at,
        )
        for v in visitors
    ]
