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


class InviteSave(BaseModel):
    invite_type: InviteType
    audience: Optional[InviteAudience] = None
    title: str = Field(min_length=1, max_length=140)
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
