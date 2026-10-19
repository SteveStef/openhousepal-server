from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing import Optional, List, Literal, Any
from datetime import datetime

QuestionType = Literal['short_text', 'long_text', 'email', 'phone', 'number', 'select', 'date', 'time', 'currency', 'multi_select', 'contact_group', 'statement', 'address_autocomplete', 'city_multiselect', 'school_district_multiselect', 'county_multiselect']
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
    options: Optional[List[str]] = None  # 'select' sub-fields (e.g. State)
    searchable: Optional[bool] = None  # 'select' sub-fields with many options - typeable/filterable combobox


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
    searchable: Optional[bool] = None  # 'select' questions with many options render as a typeable/filterable combobox instead of a button grid
    # 'statement' questions only - a dead end (e.g. a disqualifying answer) with no Continue/Submit
    # button, since there's nothing meaningful left to submit and showing the form's normal success
    # screen afterward would contradict what this statement just told the visitor.
    terminal: Optional[bool] = None


class LinkedProperty(BaseModel):
    listing_key: Optional[str] = None
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    close_price: Optional[float] = None  # recorded sale price once closed - Just Sold designs label it "Sold for"
    close_date: Optional[str] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    acres: Optional[float] = None
    image_url: Optional[str] = None
    photos: Optional[List[str]] = None
    status: Optional[str] = None
    description: Optional[str] = None
    year_built: Optional[int] = None
    property_type: Optional[str] = None
    lot_size: Optional[float] = None


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
    look_mode: str = 'hub'
    look_theme: Optional[str] = None
    luxe_metal: Optional[str] = None
    redirect_to_agent_hub: bool = True
    property_photo_carousel: bool = False
    background_effects: bool = True
    welcome_text_effects: bool = True
    branding_layout: BrandingLayout = 'split'
    is_enabled: bool
    order_index: int
    linked_property: Optional[LinkedProperty] = None
    # Count of active (non-deleted) named Campaigns beyond General - NOT a total-including-General
    # count. 0 means General only (the card shows no campaign meta-line at all); set on the ORM
    # object as a transient attribute in get_my_forms before serialization, since it has no real
    # column. See forms_routes.py CAMPAIGN_ELIGIBLE_FORM_TYPES / get_my_forms.
    additional_campaign_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AgentFormCreate(BaseModel):
    title: str


class FormCampaignResponse(BaseModel):
    """id is None only for the synthesized General entry (there is no FormCampaign row for
    General - see database.py's FormCampaign docstring). token is likewise None for General,
    since its link is the form's plain URL with no ?c= param at all."""
    id: Optional[str] = None
    name: str
    token: Optional[str] = None
    is_general: bool = False
    description: Optional[str] = None
    views: int = 0
    leads: int = 0
    deleted_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    redirect_mode: Optional[str] = None  # None = same as the form
    redirect_url: Optional[str] = None


class FormCampaignCreate(BaseModel):
    name: str
    redirect_mode: Optional[str] = None
    redirect_url: Optional[str] = None


class FormCampaignUpdate(BaseModel):
    """Changes where a campaign's visitors go after submitting. redirect_mode None / 'default'
    puts it back on the form's own setting."""
    redirect_mode: Optional[str] = None
    redirect_url: Optional[str] = None


class CampaignLookupResponse(BaseModel):
    """Resolves a bare campaign_id back to its owning form - used by Design Studio, which stores
    only the campaign_id (a design isn't otherwise scoped to one form) and needs the form's slug
    and title to build the tracking link and show which form a design's campaign belongs to."""
    id: str
    name: str
    token: str
    form_id: str
    form_slug: str
    form_title: str
    redirect_mode: Optional[str] = None
    redirect_url: Optional[str] = None


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
    look_mode: Optional[str] = None
    look_theme: Optional[str] = None
    luxe_metal: Optional[str] = None
    redirect_to_agent_hub: Optional[bool] = None
    property_photo_carousel: Optional[bool] = None
    background_effects: Optional[bool] = None
    welcome_text_effects: Optional[bool] = None
    branding_layout: Optional[BrandingLayout] = None
    linked_property: Optional[LinkedProperty] = None


class FormLayoutItem(BaseModel):
    id: str
    order_index: int
    is_enabled: bool


class FormLookUpdate(BaseModel):
    """One look for every form at once (setup flow / "Use on all my forms")."""
    look_mode: str = 'hub'
    look_theme: Optional[str] = None
    accent_color: Optional[str] = None
    luxe_metal: Optional[str] = None
    dark_mode: Optional[bool] = None


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
    # Resolved regardless of the campaign's deleted_at, so a deleted campaign's real name still
    # shows on old submissions instead of going blank. None/omitted for General - quiet by default,
    # same as the card's meta-line. Set as a transient attribute in get_form_submissions.
    campaign_name: Optional[str] = None
    # Schedule a Showing only - see FormSubmission.showing.
    showing: Optional[dict] = None
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


class ExampleProperty(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    address: str
    city: Optional[str] = None
    price: Optional[float] = None
    image: Optional[str] = None


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
    # What the visitor sees, already resolved (the Agent Hub's look when the form matches it):
    # {theme: 'classic'|'luxe', accent: '#hex', metal: 'gold'|'silver'|'bronze', dark: bool}.
    look: Optional[dict] = None
    redirect_to_agent_hub: bool = True
    # Set only when the link's campaign overrides the form: 'agent_hub' | 'sales' | 'none' | 'custom'
    # (after_submit_url holds the custom link). The page builds the hub/sales path for its own host.
    after_submit_mode: Optional[str] = None
    after_submit_url: Optional[str] = None
    property_photo_carousel: bool = False
    background_effects: bool = True
    welcome_text_effects: bool = True
    branding_layout: BrandingLayout = 'split'
    agent_name: str
    agent_brokerage: str
    agent_photo_url: Optional[str] = None
    agent_title: Optional[str] = None
    agent_market: Optional[str] = None
    linked_property: Optional[LinkedProperty] = None
    # A few of the agent's real recent sales - only populated for showcase_request forms, to
    # illustrate the welcome-screen preview with actual homes instead of placeholder tiles.
    example_properties: Optional[List[ExampleProperty]] = None


class FormAnswerInput(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    question_id: str
    value: Any = None


class FormSubmissionCreate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    answers: List[FormAnswerInput]
    # Both optional and both new - used only for campaign attribution (see forms_service.py's
    # resolve_submission_campaign_id). session_key lets the submission inherit the campaign its
    # FormViewSession was already attributed to; campaign_token is a fallback used only when no
    # session row exists at all. Neither affects validation/persistence of the answers themselves.
    session_key: Optional[str] = None
    campaign_token: Optional[str] = None


class FormSubmissionResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    success: bool
    message: str
    submission_id: Optional[str] = None
    # Set only for showcase_request submissions that successfully auto-built a Showcase - the
    # public form page uses this to send the visitor straight there instead of just a thank-you screen.
    redirect_url: Optional[str] = None


class FormProgressUpdate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    session_key: str
    step_index: int
    question_id: Optional[str] = None
    completed: bool = False
    # Only consulted when this ping creates a brand-new FormViewSession (first view) - resolved
    # once via forms_service.resolve_campaign_id and never touched again for that session's life.
    campaign_token: Optional[str] = None
    # Insights - all optional so older cached pages keep working. `answers` = what the visitor has
    # typed/picked so far ({question_id: value}), merged into the session even if they never
    # submit. source/device/referrer are first-touch only.
    answers: Optional[dict] = None
    source: Optional[str] = None      # qr | link | website
    device: Optional[str] = None      # mobile | tablet | desktop
    referrer: Optional[str] = None    # host of document.referrer
