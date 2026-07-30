from datetime import date, datetime, time
from typing import List, Literal, Optional

from pydantic import BaseModel

HostRequestStatus = Literal["pending", "approved", "declined", "countered"]
OpenHouseStatus = Literal["needs_host", "scheduled"]
BroadcastStatus = Literal["open", "filled", "cancelled"]


class MyListingSummary(BaseModel):
    open_scheduled: int
    needs_host: int
    avg_guests_per_open: float
    high_priority: int


class MyListingItem(BaseModel):
    listing_key: str
    image: Optional[str] = None
    days_on_market: Optional[int] = None
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    living_area: Optional[float] = None
    open_house_status: OpenHouseStatus
    avg_guests_per_open: float
    opens_hosted_count: int
    is_high_priority: bool
    pending_request_count: int
    is_manual: bool = False


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
    broadcast_id: Optional[str] = None


class CreateHostRequestRequest(BaseModel):
    listing_key: str
    requested_date: date
    requested_start_time: time
    requested_end_time: time
    message: Optional[str] = None


class CreateHostRequestResult(BaseModel):
    # False when the listing's MLS agent isn't an OpenHousePal user - nothing was created,
    # the frontend should fall back to the email template instead.
    on_platform: bool
    request: Optional[HostRequestResponse] = None


class CounterHostRequestRequest(BaseModel):
    counter_date: date
    counter_start_time: time
    counter_end_time: time


class AgentSearchResult(BaseModel):
    id: str
    name: str
    brokerage: Optional[str] = None
    mls_id: Optional[str] = None
    email: str


class TeamMemberResponse(BaseModel):
    id: str
    agent_id: str
    name: str
    brokerage: Optional[str] = None
    mls_id: Optional[str] = None
    added_at: datetime


class AddTeamMemberRequest(BaseModel):
    identifier: str  # email or MLS ID of the agent to add


class CreateBroadcastRequest(BaseModel):
    listing_key: str
    requested_date: date
    requested_start_time: time
    requested_end_time: time
    message: Optional[str] = None


class BroadcastResponse(BaseModel):
    id: str
    listing_key: str
    property_address: Optional[str] = None
    property_image: Optional[str] = None
    requested_date: date
    requested_start_time: time
    requested_end_time: time
    message: Optional[str] = None
    status: BroadcastStatus
    created_at: datetime
    interested_requests: List[HostRequestResponse] = []


class IncomingBroadcastResponse(BaseModel):
    id: str
    listing_key: str
    property_address: Optional[str] = None
    property_image: Optional[str] = None
    owner_agent_name: str
    owner_agent_brokerage: Optional[str] = None
    requested_date: date
    requested_start_time: time
    requested_end_time: time
    message: Optional[str] = None
    status: BroadcastStatus
    created_at: datetime
    my_response_status: Optional[HostRequestStatus] = None


class ScheduledOpenResponse(BaseModel):
    id: str
    scheduled_date: Optional[date] = None
    scheduled_start_time: Optional[time] = None
    scheduled_end_time: Optional[time] = None
    hosting_agent_id: str
    hosting_agent_name: str
    hosting_agent_brokerage: Optional[str] = None


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
    pending_requests: List[HostRequestResponse]
    history: List[OpenHouseHistoryRow]
    key_metrics: KeyMetrics
    notes: ListingHostNoteResponse
