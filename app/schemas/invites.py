"""Invites (Forms & Invites -> Invites) - request bodies. Responses are plain dicts built by
invites_service.serialize_* so the agent view and the public page can share one shape."""
from datetime import date
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

InviteType = Literal['open_house', 'brokers_open', 'client_event', 'custom']
InviteAudience = Literal['clients', 'agents']
InviteDesign = Literal['classic', 'garden', 'midnight', 'modern']
RsvpResponse = Literal['yes', 'maybe', 'no']


class InviteQuestion(BaseModel):
    """The forms question shape (AgentForm.questions) - plus `role`, which tells the RSVP what a
    pre-made question means: 'guests' (how many are coming), 'represented' (working with an agent?),
    'brokerage'. Custom questions have no role."""
    id: str = Field(max_length=64)
    type: Literal['short_text', 'long_text', 'select', 'multi_select', 'number', 'phone', 'email', 'date']
    label: str = Field(max_length=200)
    options: Optional[List[str]] = None
    required: bool = False
    placeholder: Optional[str] = Field(default=None, max_length=200)
    helper_text: Optional[str] = Field(default=None, max_length=300)
    role: Optional[Literal['guests', 'represented', 'brokerage']] = None


class InviteLinkedProperty(BaseModel):
    listing_key: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    image: Optional[str] = None
    photos: Optional[List[str]] = None
    no_cover: Optional[bool] = None  # "No photo" picked - the home's photos still show on the page


class InviteDay(BaseModel):
    """One more day of a several-day invite (the first day is event_date / start_time / end_time)."""
    date: date
    start: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    end: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')


class CoverPosition(BaseModel):
    x: float = Field(default=50, ge=0, le=100)
    y: float = Field(default=50, ge=0, le=100)


class InviteSave(BaseModel):
    invite_type: InviteType
    audience: Optional[InviteAudience] = None
    title: str = Field(min_length=1, max_length=140)
    # The flyer's line 2 (round 8): None = automatic (town / place), '' = no line 2, else exactly as typed.
    subtitle: Optional[str] = Field(default=None, max_length=160)
    message: Optional[str] = Field(default=None, max_length=2000)
    event_date: Optional[date] = None
    start_time: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    end_time: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    place_name: Optional[str] = Field(default=None, max_length=140)
    address: Optional[str] = Field(default=None, max_length=300)
    linked_property: Optional[InviteLinkedProperty] = None
    cover_image_url: Optional[str] = Field(default=None, max_length=1000)
    design: InviteDesign = 'classic'
    accent_color: Optional[str] = Field(default=None, max_length=9)
    questions: List[InviteQuestion] = Field(default_factory=list, max_length=12)
    extra_dates: List[InviteDay] = Field(default_factory=list, max_length=6)
    motion: Literal['normal', 'luxe'] = 'normal'
    luxe_metal: Optional[Literal['gold', 'silver', 'bronze']] = None
    cover_position: Optional[CoverPosition] = None
    open_house_event_id: Optional[str] = Field(default=None, max_length=64)


class InviteStatusUpdate(BaseModel):
    status: Literal['live', 'closed']


class InviteRecipientIn(BaseModel):
    email: str = Field(max_length=200)
    name: Optional[str] = Field(default=None, max_length=140)


class InviteSendRequest(BaseModel):
    recipients: List[InviteRecipientIn] = Field(min_length=1, max_length=100)


class RsvpAnswerIn(BaseModel):
    questionId: str = Field(max_length=64)
    value: Any = None


class RsvpSubmit(BaseModel):
    response: RsvpResponse
    name: str = Field(min_length=1, max_length=140)
    email: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=40)
    answers: List[RsvpAnswerIn] = Field(default_factory=list, max_length=20)
    # Coming back to change an answer: the private token from their browser / confirmation email.
    token: Optional[str] = Field(default=None, max_length=48)
    # 10/24: which day(s) of a several-day invite; "Are you a real estate agent?" / "Are you working
    # with an agent?"; "Can't make it" -> "Still interested in the home?" + "Keep me updated".
    days: List[date] = Field(default_factory=list, max_length=7)
    is_agent: Optional[bool] = None
    has_agent: Optional[bool] = None
    still_interested: Optional[bool] = None
    keep_updated: Optional[bool] = None


class InvitePublicRequest(BaseModel):
    """A guest's one-tap ask from the invitation page - the Guest Pass's three: the seller
    disclosure, a second (private) tour, or "keep me updated" on the home. Their RSVP token fills
    in who they are; without one they type their name + email / phone in the sheet."""
    kind: Literal['disclosure', 'second_tour', 'updates']
    token: Optional[str] = Field(default=None, max_length=48)
    name: Optional[str] = Field(default=None, max_length=140)
    email: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=40)
    times: Optional[str] = Field(default=None, max_length=200)  # second tour: "Sat, Nov 8 - Afternoon"
    is_agent: Optional[bool] = None
    has_agent: Optional[bool] = None
