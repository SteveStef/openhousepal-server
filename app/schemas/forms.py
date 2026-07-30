from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing import Optional, List, Literal, Any
from datetime import datetime

QuestionType = Literal['short_text', 'long_text', 'email', 'phone', 'number', 'select', 'date', 'time', 'currency', 'multi_select', 'contact_group', 'statement']
# Question-page agent/property placement: 'top' = both together above the question, 'bottom' = both
# together below the question, 'split' = property header above / agent footer below, 'split_reverse'
# = agent header above / property footer below, 'integrated' = the whole flow (property, branding,
# progress, question) renders as one boxed card, matching the Open House guest sign-in widget.
BrandingLayout = Literal['top', 'bottom', 'split', 'split_reverse', 'integrated']


class QuestionDependsOn(BaseModel):
    question_id: str
    values: List[str]


class ContactGroupField(BaseModel):
    id: str
    type: QuestionType
    label: str
    required: bool = False


class Question(BaseModel):
    id: str
    type: QuestionType
    label: str
    placeholder: Optional[str] = None
    required: bool = False
    options: Optional[List[str]] = None
    locked: bool = False  # Core contact/address field - cannot be deleted
    depends_on: Optional[QuestionDependsOn] = None  # Only shown/validated when the referenced question's answer matches
    section_title: Optional[str] = None  # Shown as a section divider above this question
    unit: Optional[str] = None  # e.g. "square feet" - shown as a suffix on number inputs
    helper_text: Optional[str] = None  # Small clarifying note shown under the input
    fields: Optional[List[ContactGroupField]] = None  # Only used when type == 'contact_group' - multiple sub-fields shown together on one screen


class LinkedProperty(BaseModel):
    listing_key: Optional[str] = None
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    acres: Optional[float] = None
    image_url: Optional[str] = None
    photos: Optional[List[str]] = None
    status: Optional[str] = None


# ---- Authenticated (agent-side) schemas: snake_case ----

class AgentFormResponse(BaseModel):
    id: str
    form_type: str
    slug: str
    title: str
    headline: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    questions: List[Question]
    success_heading: str
    success_message: Optional[str] = None
    accent_color: Optional[str] = None
    show_branding: bool = True
    dark_mode: bool = False
    redirect_to_agent_hub: bool = True
    property_photo_carousel: bool = False
    branding_layout: BrandingLayout = 'split'
    is_enabled: bool
    order_index: int
    linked_property: Optional[LinkedProperty] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AgentFormCreate(BaseModel):
    title: str


class AgentFormUpdate(BaseModel):
    title: Optional[str] = None
    headline: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    questions: Optional[List[Question]] = None
    success_heading: Optional[str] = None
    success_message: Optional[str] = None
    accent_color: Optional[str] = None
    show_branding: Optional[bool] = None
    dark_mode: Optional[bool] = None
    redirect_to_agent_hub: Optional[bool] = None
    property_photo_carousel: Optional[bool] = None
    branding_layout: Optional[BrandingLayout] = None
    linked_property: Optional[LinkedProperty] = None


class FormLayoutItem(BaseModel):
    id: str
    order_index: int
    is_enabled: bool


class AgentFormLayoutUpdate(BaseModel):
    forms: List[FormLayoutItem]


class SlugUpdate(BaseModel):
    slug: str


class SlugCheckResponse(BaseModel):
    slug: str
    available: bool


class FormSubmissionListItem(BaseModel):
    id: str
    form_id: str
    form_type: str
    answers: List[dict]
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    property_address: Optional[str] = None
    is_reviewed: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SubmissionReviewedUpdate(BaseModel):
    is_reviewed: bool


# ---- Public (visitor-facing) schemas: camelCase ----

class PublicFormSummary(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    form_id: str
    form_type: str
    slug: str
    title: str


class PublicAgentFormResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    form_id: str
    form_type: str
    slug: str
    title: str
    headline: Optional[str] = None
    subtitle: Optional[str] = None
    questions: List[Question]
    success_heading: str
    success_message: Optional[str] = None
    accent_color: Optional[str] = None
    show_branding: bool = True
    dark_mode: bool = False
    redirect_to_agent_hub: bool = True
    property_photo_carousel: bool = False
    branding_layout: BrandingLayout = 'split'
    agent_name: str
    agent_brokerage: str
    agent_photo_url: Optional[str] = None
    agent_title: Optional[str] = None
    agent_market: Optional[str] = None
    linked_property: Optional[LinkedProperty] = None


class FormAnswerInput(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    question_id: str
    value: Any = None


class FormSubmissionCreate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    answers: List[FormAnswerInput]


class FormSubmissionResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    success: bool
    message: str
    submission_id: Optional[str] = None
