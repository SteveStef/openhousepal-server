from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

LeadStatus = Literal['new', 'contacted', 'in_talks', 'signed', 'hidden', 'qualified', 'converted', 'archived']
LeadRepresented = Literal['yes', 'no', 'unknown']
LeadSourceType = Literal[
    'open_house_visitor', 'form_submission', 'property_tour', 'property_comment', 'collection_visitor',
    'open_house_second_visit', 'open_house_disclosure',
    'property_website_inquiry', 'property_website_tour_request',
    'portal_lead', 'form_partial',
    'property_page_inquiry', 'property_page_tour_request',
    'blog_inquiry',
    # Done inside their Showcase / elsewhere - logged by leads_service.log_activity.
    'showcase_like', 'showcase_dislike', 'showcase_view', 'showcase_refine', 'showcase_search_changed',
    'showcase_emails_off', 'showcase_emails_on', 'email_unsubscribed', 'review_left',
    # Email tracking + engagement rules - client_email_service.
    'email_opened', 'email_clicked', 'email_weekly', 'email_paused', 'email_resumed',
]
LeadSourceFilter = Literal['all', 'open_house', 'form', 'showcase', 'website', 'tour']
# 'activity' = needs-you first, then most recent activity (the default). 'unrepresented_first' is
# the old default, still accepted.
LeadSort = Literal['activity', 'unrepresented_first', 'newest', 'oldest', 'name', 'warmth']
# Where the person first came in (their first touch point) - the Leads page's "Came in from" filter.
LeadOriginGroup = Literal['open_house', 'showcase', 'forms', 'listings', 'website', 'portal', 'review', 'other']


class LeadMomentOut(BaseModel):
    """One moment on a lead - how they came in, or the latest thing they did."""
    type: str
    label: str
    at: datetime
    group: Optional[str] = None
    # Where they were before reaching out ("Scanned the QR under 22 Birch Ln at your open house...").
    came_from: Optional[str] = None


class LeadTodoOut(BaseModel):
    """Something waiting on the agent for this lead: an open follow-up, or a request they haven't
    handled (disclosure, showing, comment, search change...)."""
    kind: str  # followup | disclosure | second_visit | showing | comment | refine | likes
    label: str
    href: Optional[str] = None
    due_at: Optional[datetime] = None
    all_day: bool = False
    bucket: Optional[str] = None  # follow-ups: overdue | today | upcoming | someday
    task_id: Optional[str] = None
    # False = worth a look, not a must-do (they liked homes).
    urgent: bool = True


class LeadShowcaseOut(BaseModel):
    id: str
    name: Optional[str] = None
    share_token: Optional[str] = None
    home_count: int = 0
    liked: int = 0
    disliked: int = 0
    new_matches: int = 0
    last_active_at: Optional[datetime] = None
    emails_on: bool = True
    refine_message: Optional[str] = None


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
    # The record it came from (form request / submission / visitor / tour / comment id).
    source_id: Optional[str] = None
    # Where this touch point is handled - {href, label, external?} (lead_routes._event_link).
    link: Optional[Dict[str, Any]] = None
    # Where they came from before reaching out (a Similar Homes QR at an open house, the Agent Hub...).
    came_from: Optional[str] = None
    # The agent already dealt with it (opened / marked done) - "Waiting on you" points at one that isn't.
    handled: bool = False

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
    # The newest still-open disclosure / second-visit request - deep links to exactly that one
    # on the Open Houses page (/open-houses?request=<id>).
    pending_request_id: Optional[str] = None
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
    # How they first came in, and the latest thing they did (any kind).
    origin: Optional[LeadMomentOut] = None
    origin_group: LeadOriginGroup = 'other'
    latest: Optional[LeadMomentOut] = None
    # What's waiting on the agent (open follow-ups first, then unhandled requests).
    todos: List[LeadTodoOut] = []
    needs_you: bool = False
    # They unsubscribed from all emails (Showcase updates no longer reach them).
    unsubscribed: bool = False
    # Emails we sent them + whether automatic ones go daily / weekly / not at all, and why
    # (client_email_service.people). None = never emailed.
    email_engagement: Optional[Dict[str, Any]] = None
    # How active they are right now (lead_warmth.warmth): 0-100, recent activity counts most.
    warmth: int = 0
    warmth_level: str = 'cold'
    # Much busier than their usual week (lead_warmth) - the moment to reach out.
    heating_up: bool = False
    warmth_reason: Optional[str] = None

    class Config:
        from_attributes = True


class LeadDetailOut(LeadOut):
    events: List[LeadEventOut] = []
    showcases: List[LeadShowcaseOut] = []


class LeadHandleRequest(BaseModel):
    """Mark one kind of waiting request handled (see LeadTodoOut.kind)."""
    kind: str


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
