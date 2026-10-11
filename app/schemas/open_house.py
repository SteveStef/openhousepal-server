from pydantic import BaseModel, EmailStr, ConfigDict, field_validator
from pydantic.alias_generators import to_camel
from typing import Literal, Optional, List, Union, Dict, Any
from enum import Enum
from datetime import datetime, date, time

class HasAgent(str, Enum):
    YES = "YES"
    NO = "NO"

class OpenHouseFormSubmission(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    # Personal info
    full_name: str
    email: EmailStr
    phone: str

    # Visit context
    has_agent: HasAgent

    # Open house and agent context
    open_house_event_id: str
    agent_id: Optional[str] = None

    # Collection preference
    interested_in_similar: bool = False
    # "similar" when the guest came in through a Similar Homes handout's QR (?intent=similar)
    intent: Optional[str] = None

class OpenHouseFormResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    success: bool
    message: str
    visitor_id: Optional[str] = None
    collection_created: Optional[bool] = False
    # Where to send the guest's phone next: their Guest Pass (path on the same host). None = stay.
    guest_pass_url: Optional[str] = None

# Open house management schemas
class OpenHouseCreateRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    address: str
    property_data: dict  # Full PropertyDetailResponse data from Zillow API
    cover_image_url: str
    cover_image_position: Optional[Dict[str, Any]] = None  # {x, y} crop offset (%) chosen in the drag preview
    open_house_event_id: Optional[str] = None  # UUID provided by frontend for consistent QR codes
    qr_code_url: Optional[str] = None  # Optional since backend will generate it
    similar_properties_snapshot: Optional[List[Dict[str, Any]]] = None
    cheat_sheet_photos: Optional[List[Any]] = None
    cheat_sheet_snapshot: Optional[Dict[str, Any]] = None
    # Sign-in screen theme picked in the wizard's Review step (None = keep the model default).
    qr_dark_mode: Optional[bool] = None

    @field_validator("address", "cover_image_url", "open_house_event_id", "qr_code_url", mode="before")
    @classmethod
    def coerce_string_fields(cls, value):
        if value is None:
            return value
        if isinstance(value, dict):
            for key in ("url", "id", "value", "address"):
                if value.get(key):
                    return str(value[key])
        return str(value)

class OpenHouseResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    id: str
    open_house_event_id: str
    agent_id: Optional[str] = None
    address: str
    cover_image_url: str
    cover_image_position: Optional[Dict[str, Any]] = None
    qr_code_url: str
    form_url: str
    # Property details for PDF generation
    bedrooms: Optional[Union[int, float]] = None
    bathrooms: Optional[Union[int, float]] = None
    living_area: Optional[Union[int, float]] = None
    price: Optional[Union[int, float]] = None
    lot_size: Optional[Union[int, float]] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    listing_key: Optional[str] = None
    notes: Optional[str] = None
    similar_properties_snapshot: Optional[List[Dict[str, Any]]] = None
    cheat_sheet_photos: Optional[List[Any]] = None
    cheat_sheet_snapshot: Optional[Dict[str, Any]] = None
    qr_dark_mode: bool = True
    share_guests_with_listing_agent: bool = False
    auto_showcase: bool = True
    after_signin: Optional[str] = None
    guest_pass: bool = True
    pass_signature: bool = True
    # The plan includes the Guest Pass (Bright MLS plans with a live subscription).
    guest_pass_available: bool = False
    # The plan includes Email Signature (IDX / No-MLS Pro) - shows the Guest Pass footer switch.
    pass_signature_available: bool = False
    # The host's own published Spotlight for this home - only when the open house is at their own
    # listing (see OpenHouseService.listing_spotlights). None = nothing to send guests to.
    own_spotlight_url: Optional[str] = None
    # Their own listing, IDX plan, no published Spotlight yet -> offer "Make Spotlight" (or "Finish
    # Spotlight" when spotlight_draft_id is set).
    can_make_spotlight: bool = False
    spotlight_draft_id: Optional[str] = None
    # The plan includes an Agent Hub / it's set up enough to send guests to.
    hub_available: bool = False
    hub_ready: bool = False
    # The agent's plan includes Showcases (Premium / IDX) - the "Guest Showcases" switch only shows then.
    showcases_available: bool = False
    visitor_count: int = 0
    created_at: datetime
    # Only populated for opens booked through an approved Open House Manager host request -
    # self-hosted/wizard opens are evergreen QR pages with no fixed schedule.
    scheduled_date: Optional[date] = None
    scheduled_start_time: Optional[time] = None
    scheduled_end_time: Optional[time] = None
    # The listing agent's standing host notes for this listing (access info, lockbox code,
    # parking, seller preferences...), surfaced to a guest host ONLY while their scheduled open
    # is still upcoming - it's transient info that stops being relevant once the open is over.
    listing_host_notes: Optional[str] = None

class VisitorResponse(BaseModel):
    id: str
    full_name: str
    email: str
    phone: str
    has_agent: str
    interested_in_similar: bool
    notes: Optional[str] = None
    lead_id: Optional[str] = None
    created_at: datetime

class NoteUpdate(BaseModel):
    notes: str

class QrDarkModeUpdate(BaseModel):
    qr_dark_mode: bool

class ShareGuestsUpdate(BaseModel):
    share_guests_with_listing_agent: bool

class GuestSettingsUpdate(BaseModel):
    auto_showcase: Optional[bool] = None
    guest_pass: Optional[bool] = None
    pass_signature: Optional[bool] = None
    after_signin: Optional[Literal["hub", "none"]] = None

class OpenHouseSnapshotUpdate(BaseModel):
    similar_properties_snapshot: List[Dict[str, Any]]

class OpenHouseCheatSheetSnapshotUpdate(BaseModel):
    cheat_sheet_snapshot: Dict[str, Any]

class OpenHouseCheatSheetPhotosUpdate(BaseModel):
    cheat_sheet_photos: List[Dict[str, Any]]


class OpenHouseFormRequestType(str, Enum):
    DISCLOSURE = "disclosure"
    SECOND_VISIT = "second_visit"


class OpenHouseFormRequestCreate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    form_type: OpenHouseFormRequestType
    full_name: str
    email: EmailStr
    phone: str
    wants_another_tour: Optional[bool] = None
    another_tour_time: Optional[str] = None
    preferred_visit_time: Optional[str] = None
    wants_disclosure: Optional[bool] = None


class OpenHouseFormRequestResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    id: str
    open_house_event_id: str
    form_type: str
    full_name: str
    email: str
    phone: str
    wants_another_tour: Optional[bool] = None
    another_tour_time: Optional[str] = None
    preferred_visit_time: Optional[str] = None
    wants_disclosure: Optional[bool] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    # Denormalized for display in the Forms/Open House results lists, without a join round-trip.
    property_address: Optional[str] = None
    property_image: Optional[str] = None
