from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

LeadStatus = Literal['new', 'contacted', 'in_talks', 'signed', 'hidden', 'qualified', 'converted', 'archived']
LeadRepresented = Literal['yes', 'no', 'unknown']
LeadSourceType = Literal[
    'open_house_visitor', 'form_submission', 'property_tour', 'property_comment', 'collection_visitor',
    'open_house_second_visit', 'open_house_disclosure',
]
LeadSourceFilter = Literal['all', 'open_house', 'form', 'showcase', 'website', 'tour']
LeadSort = Literal['unrepresented_first', 'newest', 'oldest', 'name']


class LeadEventOut(BaseModel):
    id: str
    source_type: LeadSourceType
    summary: str
    property_address: Optional[str] = None
    open_house_event_id: Optional[str] = None
    collection_id: Optional[str] = None
    form_id: Optional[str] = None
    raw_snapshot: Optional[Dict[str, Any]] = None
    occurred_at: datetime

    class Config:
        from_attributes = True


class LeadPropertyOut(BaseModel):
    image: Optional[str] = None
    city: Optional[str] = None
    price: Optional[float] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None


class LeadOpenHouseOut(BaseModel):
    id: str
    address: Optional[str] = None
    image: Optional[str] = None
    city: Optional[str] = None
    visitor_count: int = 0
    visitorCount: int = 0
    is_deleted: bool = False
    created_at: datetime


class LeadOut(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    represented: LeadRepresented
    status: LeadStatus
    tags: List[str] = []
    notes: Optional[str] = None
    primary_source: LeadSourceFilter
    source_count: int
    last_event_summary: Optional[str] = None
    last_event_source_type: Optional[LeadSourceType] = None
    # Engagement-only timestamp (excludes capture-time events like sign-in/form-submit - see
    # _last_activity_at in lead_routes.py) - intentionally None for a lead with no real engagement
    # yet. Used for the Leads tab's "Active: ..." badge and default sort, NOT for display next to
    # last_event_summary/last_event_source_type above (which describe ANY most-recent event,
    # including capture-time ones) - pairing those with this field left the Dashboard Activity feed
    # showing a real event description with no timestamp next to it. Use last_event_occurred_at for
    # that instead.
    last_event_at: Optional[datetime] = None
    # The most recent event's own timestamp, unconditionally - always set whenever any event exists,
    # regardless of event type. This is what actually pairs with last_event_summary/
    # last_event_source_type above.
    last_event_occurred_at: Optional[datetime] = None
    property_address: Optional[str] = None
    property: Optional[LeadPropertyOut] = None
    # The form's title (e.g. "What's My Home Worth?") when the most recent event is a form
    # submission - lets the Dash Leads card show what form this lead came from in its picture
    # space instead of a generic icon when there's no property photo to show there either.
    form_title: Optional[str] = None
    open_houses: List[LeadOpenHouseOut] = []
    showcase_deleted: bool = False
    showcase_new_matches: int = 0
    has_second_visit_request: bool = False
    has_disclosure_request: bool = False
    has_tour_request: bool = False
    has_commented: bool = False
    has_liked_property: bool = False
    has_disliked_property: bool = False
    needs_attention: bool = False
    # The showcase (Collection) this lead's pending attention item lives in, if any - lets the
    # frontend deep-link straight to /showcases?showcase={id} instead of a generic lead page.
    # Picks the first matched collection since most leads have exactly one showcase.
    attention_collection_id: Optional[str] = None
    created_at: datetime
    last_contacted_at: Optional[datetime] = None
    hidden_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LeadDetailOut(LeadOut):
    events: List[LeadEventOut] = []


class LeadUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[LeadStatus] = None
    represented: Optional[LeadRepresented] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None
    mark_contacted: bool = False


class LeadNoteOut(BaseModel):
    id: str
    lead_id: str
    author_id: Optional[str] = None
    author_name: Optional[str] = None
    text: str
    open_house_event_id: Optional[str] = None
    open_house_address: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LeadNoteCreate(BaseModel):
    text: str
    open_house_event_id: Optional[str] = None


class LeadHideRequest(BaseModel):
    lead_id: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    open_house_event_ids: List[str] = []


class LeadNoteUpdate(BaseModel):
    text: str
