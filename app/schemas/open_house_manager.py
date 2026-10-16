from datetime import date, datetime, time
from typing import List, Literal, Optional

from pydantic import BaseModel

HostRequestStatus = Literal["pending", "approved", "declined", "countered"]
OpenHouseStatus = Literal["needs_host", "scheduled"]
BroadcastStatus = Literal["open", "filled", "cancelled"]


class MyListingSummary(BaseModel):
    open_scheduled: int
    # Listings with no confirmed host (kit or approved request) for the upcoming Fri-Sun window -
    # see _current_weekend_range/_has_weekend_host. Naturally rolls forward to the next weekend
    # once the current one passes, with no stored state.
    needs_host: int
    avg_guests_per_open: float
    high_priority: int


class ActionCountsResponse(BaseModel):
    """Drives the red-dot badges. pending_as_listing_agent/countered_as_host are derived straight
    from live request status, so they clear themselves the instant the underlying request is
    responded to. The *_unseen_updates counts catch informational events that don't leave anything
    outstanding to derive from (e.g. the host just accepted your proposed time) - those are backed
    by notification read-state instead, and clear once the relevant panel is opened."""
    pending_as_listing_agent: int  # requests on my listings still awaiting my approve/decline/counter
    countered_as_host: int  # requests I sent that got countered and are awaiting my accept/decline
    listing_agent_unseen_updates: int  # unseen "host responded to your proposed time" notifications
    host_unseen_updates: int  # unseen "listing agent approved/declined your request" notifications
    pending_incoming_broadcasts: int  # open Team broadcasts ("X needs a host") I haven't responded to yet


class ScheduledOpenResponse(BaseModel):
    id: str
    scheduled_date: Optional[date] = None
    scheduled_start_time: Optional[time] = None
    scheduled_end_time: Optional[time] = None
    hosting_agent_id: str
    hosting_agent_name: str
    hosting_agent_brokerage: Optional[str] = None
    hosting_agent_photo_url: Optional[str] = None
    # False for an approved host request the host hasn't built an actual kit for yet - lets the
    # UI show it as confirmed while flagging that there's no kit to manage yet.
    kit_built: bool = True


class MyListingItem(BaseModel):
    listing_key: str
    image: Optional[str] = None
    # Up to a handful of this listing's own photos (from the MLS media sync) - lets the public
    # Agent Hub's mobile "carousel view" cycle through one listing's real photos when the agent
    # only has the one active listing, instead of sitting on a single static image.
    photos: List[str] = []
    days_on_market: Optional[int] = None
    status: Optional[str] = None
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    living_area: Optional[float] = None
    lot_size_acres: Optional[float] = None
    open_house_status: OpenHouseStatus
    avg_guests_per_open: float
    opens_hosted_count: int
    scheduled_opens_count: int = 0
    scheduled_opens: List[ScheduledOpenResponse] = []
    # A host request that's been approved (directly, or by accepting a proposed time) but the
    # host hasn't built their kit yet - shown separately from scheduled_opens so "we agreed on a
    # time" is visible on the listing agent's side even before a kit exists for it.
    confirmed_pending_kit: List[ScheduledOpenResponse] = []
    # True when nothing (kit or approved request) is scheduled for the upcoming Fri-Sun window -
    # drives MyListingSummary.needs_host. Distinct from open_house_status, which stays "scheduled"
    # as long as anything is ever booked, however far out.
    needs_host_this_weekend: bool = True
    is_high_priority: bool
    pending_request_count: int
    is_manual: bool = False
    # When it may have an open house (open_house_manager_service.hosting_window): "pending" = under
    # contract / off market, none at all; "coming_soon" = Coming Soon, go-live date unknown, none
    # yet; hosts_from = Coming Soon, only on or after this day.
    hosting_blocked: Optional[str] = None
    hosts_from: Optional[date] = None


class MyListingsResponse(BaseModel):
    summary: MyListingSummary
    listings: List[MyListingItem]


class ManualListingCreate(BaseModel):
    address: str
    city: str
    state: str
    zipcode: Optional[str] = None
    price: Optional[float] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    living_area: Optional[float] = None
    image_url: Optional[str] = None


class RefreshListingsResponse(BaseModel):
    status: Literal["started"]


class HostRequestResponse(BaseModel):
    id: str
    listing_key: str
    property_address: Optional[str] = None
    property_image: Optional[str] = None
    listing_owner_agent_id: str
    requesting_agent_id: str
    requesting_agent_name: str
    requesting_agent_brokerage: Optional[str] = None
    requesting_agent_email: Optional[str] = None
    listing_agent_name: Optional[str] = None
    listing_agent_brokerage: Optional[str] = None
    # One "Other time" each way: once used, that side can only say yes or no.
    listing_agent_suggested_time: bool = False
    host_suggested_time: bool = False
    requested_date: date
    requested_start_time: time
    requested_end_time: time
    message: Optional[str] = None
    status: HostRequestStatus
    created_at: datetime
    responded_at: Optional[datetime] = None
    counter_date: Optional[date] = None
    counter_start_time: Optional[time] = None
    counter_end_time: Optional[time] = None
    counter_message: Optional[str] = None
    broadcast_id: Optional[str] = None
    # The listing agent's Host Notes for this property (access instructions, lockbox code, parking,
    # etc.) - live, not a snapshot: always reflects whatever the listing agent has saved most
    # recently, so an edit made after approval still reaches the host on their next fetch.
    listing_host_notes: Optional[str] = None


class CreateHostRequestRequest(BaseModel):
    listing_key: str
    requested_date: date
    requested_start_time: time
    requested_end_time: time
    message: Optional[str] = None
    # Client-generated id shared across multiple slots offered in the same "Request to Host"
    # submission (e.g. the modal's "Offer a Second Open"), so the backend can treat them as one
    # ask - one notification, one badge count - even though each slot is still its own
    # independently approvable/declinable OpenHouseHostRequest row.
    group_id: Optional[str] = None


class CreateHostRequestResult(BaseModel):
    # False when the listing's MLS agent isn't an OpenHousePal user - nothing was created,
    # the frontend should fall back to the email template instead.
    on_platform: bool
    request: Optional[HostRequestResponse] = None


class CancelApprovedOpenRequest(BaseModel):
    """Listing agent revoking an already-approved date - identified by (listing, host, date)
    rather than a request id, since the Scheduled Opens list mixes built-kit previews (whose id is
    the OpenHouseEvent, not the request) with kit-less previews (whose id is the request) - see
    _scheduled_open_previews vs _confirmed_pending_kit_previews."""
    hosting_agent_id: str
    scheduled_date: date
    # A single date can carry more than one distinct open for the same host (rare, but two
    # different approved time slots on the same day both exist) - without these, matching by
    # date alone can hit more than one approved request and the cancel fails outright.
    scheduled_start_time: Optional[time] = None
    scheduled_end_time: Optional[time] = None


class CounterHostRequestRequest(BaseModel):
    counter_date: date
    counter_start_time: time
    counter_end_time: time
    counter_message: Optional[str] = None


class CounterBackRequest(BaseModel):
    """Host proposing yet another time after the listing agent countered - puts the ball back in
    the listing agent's court (status returns to 'pending') instead of only allowing accept/decline
    of the listing agent's single counter."""
    proposed_date: date
    proposed_start_time: time
    proposed_end_time: time
    note: Optional[str] = None


class HostRequestEventResponse(BaseModel):
    id: str
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    action: str
    proposed_date: Optional[date] = None
    proposed_start_time: Optional[time] = None
    proposed_end_time: Optional[time] = None
    note: Optional[str] = None
    created_at: datetime


class AgentSearchResult(BaseModel):
    id: str
    name: str
    brokerage: Optional[str] = None
    mls_id: Optional[str] = None
    email: str
    photo_url: Optional[str] = None


TeamMemberStatus = Literal["pending", "accepted", "declined"]


class TeamMemberResponse(BaseModel):
    id: str
    agent_id: str
    name: str
    brokerage: Optional[str] = None
    mls_id: Optional[str] = None
    status: TeamMemberStatus = "accepted"
    added_at: datetime
    photo_url: Optional[str] = None


class AddTeamMemberRequest(BaseModel):
    identifier: str  # email or MLS ID of the agent to add


class IncomingTeamInviteResponse(BaseModel):
    id: str
    owner_agent_id: str
    owner_agent_name: str
    owner_agent_brokerage: Optional[str] = None
    status: TeamMemberStatus
    created_at: datetime


class TeamMembershipResponse(BaseModel):
    """One Open House Team the current agent is an accepted member of - an agent can be on any
    number of these at once (there's no limit on how many teams you can join), so this is always
    a list. Powers Discovery's Team filter."""
    owner_id: str
    owner_name: str
    owner_brokerage: Optional[str] = None


class TrustedHostResponse(BaseModel):
    """An agent who has previously been approved to host for the current listing agent - powers
    the 'Hosted for you before' quick-pick when starting a new direct request."""
    agent_id: str
    name: str
    brokerage: Optional[str] = None
    mls_id: Optional[str] = None
    times_hosted: int
    last_hosted_at: datetime
    photo_url: Optional[str] = None


class UpdateRestrictionRequest(BaseModel):
    restrict_host_requests_to_team: bool


class CreateBroadcastRequest(BaseModel):
    listing_key: str
    requested_date: date
    # Required unless is_anytime is set - the listing agent doesn't care what time, so nothing to
    # collect until whoever accepts picks one.
    requested_start_time: Optional[time] = None
    requested_end_time: Optional[time] = None
    is_anytime: bool = False
    message: Optional[str] = None
    # When set, sends this as a direct 1:1 ask to one specific agent (doesn't need to be a My Team
    # member) instead of fanning out to the whole team.
    target_agent_id: Optional[str] = None


class RespondToBroadcastRequest(BaseModel):
    """Omit proposed_* to accept exactly as requested (instant claim, first to do so wins).
    Include a differing proposed_* to counter instead - creates a normal pending request for the
    owner to review, exactly like a direct ask. For an Anytime broadcast, proposed_start_time/
    proposed_end_time are required (there's no time to default to) - the same date + a time is
    still treated as an instant claim, not a counter, since the owner already said any time works."""
    proposed_date: Optional[date] = None
    proposed_start_time: Optional[time] = None
    proposed_end_time: Optional[time] = None
    note: Optional[str] = None


class BroadcastResponse(BaseModel):
    id: str
    listing_key: str
    property_address: Optional[str] = None
    property_image: Optional[str] = None
    requested_date: date
    requested_start_time: Optional[time] = None
    requested_end_time: Optional[time] = None
    is_anytime: bool = False
    message: Optional[str] = None
    status: BroadcastStatus
    created_at: datetime
    interested_requests: List[HostRequestResponse] = []
    is_direct: bool = False
    target_agent_name: Optional[str] = None


class IncomingBroadcastResponse(BaseModel):
    id: str
    listing_key: str
    property_address: Optional[str] = None
    property_image: Optional[str] = None
    owner_agent_name: str
    owner_agent_brokerage: Optional[str] = None
    requested_date: date
    requested_start_time: Optional[time] = None
    requested_end_time: Optional[time] = None
    is_anytime: bool = False
    message: Optional[str] = None
    status: BroadcastStatus
    created_at: datetime
    my_response_status: Optional[HostRequestStatus] = None
    is_direct: bool = False


class OpenHouseHistoryRow(BaseModel):
    id: str
    event_date: Optional[date] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    hosting_agent_id: str
    hosting_agent_name: str
    hosting_agent_brokerage: Optional[str] = None
    total_guests: int
    unrepresented_guests: int
    requested_updates: int
    # Whether the hosting agent has opted in to sharing actual guest name/email/phone for this
    # specific open house - always False when the listing agent is the one who hosted it (nothing
    # to "share" from yourself). See GET /events/{id}/guests, gated on this being True.
    share_guests_enabled: bool = False


class KeyMetrics(BaseModel):
    total_guests: int
    unrepresented_guests: int
    requested_updates: int
    total_opens_hosted: int


class ListingHostNoteResponse(BaseModel):
    notes: Optional[str] = None
    is_priority_override: bool = False
    updated_at: Optional[datetime] = None


class UpdateListingNotesRequest(BaseModel):
    notes: Optional[str] = None
    is_priority_override: Optional[bool] = None


class ListingDetailResponse(BaseModel):
    listing_key: str
    address: Optional[str] = None
    image: Optional[str] = None
    scheduled_opens: List[ScheduledOpenResponse]
    # Approved requests with no kit built yet - see MyListingItem.confirmed_pending_kit.
    confirmed_pending_kit: List[ScheduledOpenResponse] = []
    pending_requests: List[HostRequestResponse]
    history: List[OpenHouseHistoryRow]
    key_metrics: KeyMetrics
    notes: ListingHostNoteResponse
