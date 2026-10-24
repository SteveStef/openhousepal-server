from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, Text, ForeignKey, Table, Index, DateTime, Date, Time, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base
from typing import Optional
import uuid
import os
import enum

class HomeType(str, enum.Enum):
    SINGLE_FAMILY = "SINGLE_FAMILY"
    TOWNHOUSE = "TOWNHOUSE"
    CONDO = "CONDO"
    MULTI_FAMILY = "MULTI_FAMILY"
    LAND = "LAND"
    FARM = "FARM"
    RESIDENTIAL_LEASE = "RESIDENTIAL_LEASE"
    COMMERCIAL = "COMMERCIAL"
    OTHER = "OTHER"


class PlanTier(str, enum.Enum):
    """Five subscription tiers across two tracks. The Bright ladder is BASIC -> PREMIUM -> IDX
    (see PLAN_TIER_RANK in app.utils.auth). NO_MLS and PRO_NO_MLS are a parallel track for
    agents without Bright MLS coverage (manual data entry throughout) - neither has a ladder
    rank and neither gets Bright MLS data. PRO_NO_MLS is NO_MLS plus Design Studio, Blogs and
    the Studio AI Media tool. Feature access is checked by explicit capability set, not a
    single rank comparison - see MLS_ACCESS_TIERS / STUDIO_DESIGN_ACCESS_TIERS /
    STUDIO_MEDIA_ACCESS_TIERS in app.utils.auth."""
    NO_MLS = "NO_MLS"
    PRO_NO_MLS = "PRO_NO_MLS"
    BASIC = "BASIC"
    PREMIUM = "PREMIUM"
    IDX = "IDX"

# Association table for many-to-many relationship between collections and properties
collection_properties = Table(
    'collection_properties',
    Base.metadata,
    Column('collection_id', String, ForeignKey('collections.id'), primary_key=True),
    Column('property_id', String, ForeignKey('properties.id'), primary_key=True),
    Column('added_at', DateTime(timezone=True), nullable=True),  # NULL = initial property (no NEW badge)
    # Moved into the Showcase's Sold/Removed folder (by the buyer or the agent). A home that goes
    # under contract / sold / off the market stays in the list with a notice until someone moves
    # it; a home back on the market drops this (collections_service._sold_folder_ids).
    Column('sold_filed_at', DateTime(timezone=True), nullable=True),
)

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    state = Column(String, nullable=False)  # Agent's state
    brokerage = Column(String, nullable=False)  # Agent's brokerage
    mls_id = Column(String, unique=True, index=True, nullable=True)  # Agent's Bright MLS ID - NULL for agents outside Bright MLS
    license_number = Column(String, nullable=True)  # Real-estate license # - how agents outside Bright MLS identify themselves
    broker_authorized = Column(Boolean, default=False, nullable=False)
    # When True, only accepted OpenHouseTeamMembers may Request to Host this agent's listings.
    # Default False = same-brokerage OpenHousePal agents (plus any accepted team member,
    # regardless of brokerage) may request - see open_house_manager_service._is_eligible_requester.
    restrict_host_requests_to_team = Column(Boolean, default=False, nullable=False, server_default='false')
    photo_url = Column(String, nullable=True)  # Uploaded headshot, served from /uploads
    professional_title = Column(String, nullable=True)  # e.g. "Real Estate Agent" - shown on Agent Hub + public forms
    market = Column(String, nullable=True)  # e.g. "Philly Main Line & Suburbs" - shown on Agent Hub + public forms
    # Per-category notification opt-outs, shape {category: {"email": bool, "push": bool}}. NULL or a
    # missing key = the default (everything on). The in-app bell is never suppressed by this - it
    # only gates the email/push channels. See services/notification_preferences_service.py.
    notification_prefs = Column(JSONB, nullable=True)
    # Client-email + briefing settings (services/email_settings_service.py). NULL = defaults.
    email_settings = Column(JSONB, nullable=True)
    # Center of every QR code: None/'ohp' = OpenHousePal house, 'logo' = Brand Kit logo, 'initials' (IDX only)
    qr_logo = Column(String, nullable=True)
    # Which automatic follow-up rules are switched OFF: {"<rule key>": false}. Missing key = the
    # rule's default (on). See tasks_service.AUTO_FOLLOWUP_RULES.
    followup_rules = Column(JSONB, nullable=True)
    # Google colorId ('1'-'11') for follow-up events on the agent's calendar (None = '5', gold).
    followup_color = Column(String, nullable=True)
    # The one-time setup flow's own bookkeeping: {"last_step", "skipped": [...], "confirmed": [...]}.
    # The answers themselves live where every feature reads them - see setup_flow_service.
    setup_state = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def is_admin(self) -> bool:
        return self.email == os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")

    # PayPal subscription fields
    subscription_id = Column(String, nullable=True, unique=True, index=True)  # PayPal subscription ID (unique to prevent hijacking)
    subscription_status = Column(String, default="TRIAL")  # TRIAL, ACTIVE, SUSPENDED, CANCELLED, EXPIRED
    plan_id = Column(String, nullable=True)  # PayPal plan ID (P-50796747... or P-4KN61644...)
    plan_tier = Column(String, nullable=True)  # NO_MLS, BASIC, PREMIUM, or IDX - see PlanTier enum above
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    # Last signed-in request (admin activity tracker; stamped at most every few minutes).
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    # Paid for by their brokerage on a custom deal (admin "Brokerage groups"). The plan itself is
    # still plan_tier/subscription_status (set by the admin, no PayPal subscription on the agent).
    billing_group_id = Column(String, ForeignKey('billing_groups.id', ondelete='SET NULL'), nullable=True, index=True)
    # The admin-made sign-up code this agent used at checkout (admin "Codes"), if any.
    promo_code_id = Column(String, ForeignKey('promo_codes.id', ondelete='SET NULL'), nullable=True, index=True)
    # When the one-time $500 IDX setup fee was paid or waived (a code, an admin plan). Set once -
    # going onto IDX again later never charges it twice.
    idx_setup_settled_at = Column(DateTime(timezone=True), nullable=True)
    subscription_started_at = Column(DateTime(timezone=True), nullable=True)
    last_billing_date = Column(DateTime(timezone=True), nullable=True)
    next_billing_date = Column(DateTime(timezone=True), nullable=True)  # For grace period after cancellation
    last_paypal_sync = Column(DateTime(timezone=True), nullable=True)  # Track when last synced with PayPal
    # Set the moment an IDX agent agrees to the "everything moves to your IDX link" prompt (see
    # /idx-domain-migration/agree). Gates that one-time modal - null means still needs to see it.
    # Never cleared by a plan downgrade, so a re-upgrade to IDX doesn't re-show it.
    idx_domain_migration_agreed_at = Column(DateTime(timezone=True), nullable=True)
    # IDX: new QR codes and copied links use the agent's own domain (on) or openhousepal.com (off).
    # Both addresses always work - this only picks which one we hand out. Settings -> IDX Domain.
    links_use_own_domain = Column(Boolean, nullable=False, default=True, server_default='true')

    # Opaque secret in the agent's private iCalendar (.ics) feed URL
    # (/calendar/{calendar_feed_token}.ics). Lets Google / Outlook / Apple Calendar subscribe to
    # this agent's OpenHousePal tasks and open houses read-only, with no OAuth. Rotatable from
    # Settings (regenerating invalidates the old URL). NULL until the agent first opens the feed.
    calendar_feed_token = Column(String, nullable=True, unique=True, index=True)

    # --- CRM push (send leads OUT to the agent's real CRM) ------------------------------------
    # OpenHousePal isn't meant to replace FollowUpBoss / Lofty / kvCORE - when on, every new lead
    # is forwarded to whichever of these the agent set: a webhook URL (JSON POST) and/or that
    # CRM's lead-parsing inbox (a formatted plain-text email those systems already know how to
    # read). crm_last_push_* is just the "did the last one work" status shown in Settings.
    crm_push_enabled = Column(Boolean, nullable=False, server_default='false', default=False)
    crm_webhook_url = Column(String, nullable=True)
    crm_parse_email = Column(String, nullable=True)
    # Native Follow Up Boss push (their API key -> POST /v1/events). Server-side only.
    crm_fub_api_key = Column(String, nullable=True)
    # A human-readable heads-up computed from FUB's /v1/identity when the key is saved - e.g.
    # "this is a team account, new leads route to the owner first". NULL = solo / owner / unknown.
    crm_fub_account_note = Column(String, nullable=True)
    crm_last_push_at = Column(DateTime(timezone=True), nullable=True)
    crm_last_push_status = Column(String, nullable=True)

    # --- Portal lead ingestion (pull leads IN from Zillow / Realtor.com / etc.) ---------------
    # Opaque secret in the agent's unique inbound address (leads-<token>@<inbound domain>). The
    # agent points each portal's lead-forwarding at that address; an inbound-email webhook parses
    # the forwarded message into a real Lead. NULL until the agent first opens the Integrations
    # section. Rotatable (regenerating invalidates the old address).
    inbound_lead_token = Column(String, nullable=True, unique=True, index=True)

    # Relationships
    collections = relationship("Collection", back_populates="owner")
    notifications = relationship("Notification", back_populates="agent")
    discovery_preferences = relationship("DiscoveryPreferences", back_populates="user", uselist=False, cascade="all, delete-orphan")
    agent_hub = relationship("AgentHub", back_populates="owner", uselist=False, cascade="all, delete-orphan")
    email_signature = relationship("EmailSignature", back_populates="owner", uselist=False, cascade="all, delete-orphan")
    agent_forms = relationship("AgentForm", back_populates="owner", cascade="all, delete-orphan")
    site = relationship("Site", back_populates="owner", uselist=False, cascade="all, delete-orphan")
    tasks = relationship("AgentTask", back_populates="owner", cascade="all, delete-orphan")

class AgentHub(Base):
    __tablename__ = "agent_hubs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    # Every slug this hub has ever used - so a QR code/link printed before a rebrand (e.g. on a
    # Cheat Sheet already handed out) keeps resolving to this hub instead of 404ing.
    previous_slugs = Column(JSONB, nullable=False, server_default='[]')
    status = Column(String, default="DRAFT", nullable=False)
    # Full page-builder payload (profile, contact, social links, sales/video/tour/blog carousels,
    # layout/visibility flags) - the single source of truth served to public visitors by slug.
    config = Column(JSONB, nullable=False, server_default='{}')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="agent_hub")

class EmailSignature(Base):
    """A condensed, email-safe "mini Agent Hub" the agent installs in Gmail/Outlook/Apple Mail.
    Same page-builder-payload-as-JSONB shape as AgentHub.config (block order, field toggles,
    template, banner settings) - a separate table rather than another AgentHub.config key because
    it has its own install/HTML-generation lifecycle and its own public banner image endpoint,
    unrelated to the Agent Hub's public slug page."""
    __tablename__ = "email_signatures"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)
    config = Column(JSONB, nullable=False, server_default='{}')
    # Opaque id embedded in the banner <img> src of an installed signature (public, unauthenticated
    # endpoint - the whole point is it loads in recipients' inboxes). Not the owner's real user id,
    # same "rotatable opaque token" idiom as calendar_feed_token/inbound_lead_token. NULL until the
    # agent first opens Email Signature settings.
    public_token = Column(String, nullable=True, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="email_signature")


class AgentForm(Base):
    __tablename__ = "agent_forms"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    form_type = Column(String, nullable=False)  # 'buying' | 'selling' | 'home_value'
    slug = Column(String, nullable=False)
    title = Column(String, nullable=False)
    headline = Column(String, nullable=True)  # Friendly on-screen intro heading, distinct from the admin-facing title
    subtitle = Column(Text, nullable=True)
    description = Column(Text, nullable=True)  # Short agent-editable blurb shown under the title in the Forms list; seeded from a default per form_type, then freely editable

    questions = Column(JSONB, nullable=False, default=list)
    success_heading = Column(String, nullable=False)
    success_message = Column(Text, nullable=True)
    accent_color = Column(String, nullable=True)
    show_branding = Column(Boolean, default=True, nullable=False)  # Show agent photo/name/brokerage on the intro screen
    dark_mode = Column(Boolean, default=False, nullable=False)  # Force dark theme for visitors on this form's public page
    # The form's look: 'hub' = whatever the Agent Hub wears (theme, colour/finish, light/dark);
    # 'custom' = its own - look_theme 'classic' (accent_color) or 'luxe' (luxe_metal), dark_mode.
    look_mode = Column(String, default='brand', server_default='hub', nullable=False)  # 'brand' (Brand Kit) | 'custom' | 'hub' (legacy)
    look_theme = Column(String, nullable=True)
    luxe_metal = Column(String, nullable=True)
    redirect_to_agent_hub = Column(Boolean, default=True, nullable=False)  # After submitting, auto-redirect the visitor to the agent's public Agent Hub
    property_photo_carousel = Column(Boolean, default=False, nullable=False)  # Welcome screen cycles through the linked property's listing photos
    background_effects = Column(Boolean, default=True, nullable=False)  # Subtle animated background graphics across the visitor form flow
    welcome_text_effects = Column(Boolean, default=True, nullable=False)  # Entrance animation for the welcome title, subtitle, and start button
    branding_layout = Column(String, default='split', nullable=False)  # Question-page agent/property placement: 'top' | 'bottom' | 'split' (property header, agent footer) | 'split_reverse' | 'integrated' (single boxed card, like Open House sign-in)
    is_enabled = Column(Boolean, default=True, nullable=False)
    order_index = Column(Integer, default=0, nullable=False)
    linked_property = Column(JSONB, nullable=True)  # Snapshot: {listing_key, address, city, state, price, beds, baths, sqft, image_url, photos, status}
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('owner_id', 'slug', name='uq_agent_forms_owner_slug'),
    )

    owner = relationship("User", back_populates="agent_forms")
    submissions = relationship("FormSubmission", back_populates="form", cascade="all, delete-orphan")


class FormCampaign(Base):
    """A separately-tracked link+QR for a premade form - never a copy of the form itself. "General"
    (the form's default/original link) is NOT a row here; it's represented by campaign_id IS NULL
    on FormViewSession/FormSubmission below, so every existing form's link stays byte-identical
    with zero backfill. Soft-delete only (deleted_at) - a deleted campaign's token stops attributing
    new traffic (falls back to General) but its historical sessions/submissions keep pointing at
    this row forever, so past stats/leads stay visible under its real name."""
    __tablename__ = "form_campaigns"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    form_id = Column(String, ForeignKey('agent_forms.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    token = Column(String, nullable=False)  # opaque, appended as ?c={token} on the form's existing URL
    # Where this campaign's visitors land after submitting. NULL = the form's own "Redirect to Agent
    # Hub" switch; else 'agent_hub' | 'sales' (Sales & Listings page) | 'none' (stay on the
    # thank-you screen) | 'custom' (redirect_url). See forms_service.normalize_campaign_redirect.
    redirect_mode = Column(String(20), nullable=True)
    redirect_url = Column(String(500), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('form_id', 'token', name='uq_form_campaigns_form_token'),
    )


class FormSubmission(Base):
    __tablename__ = "form_submissions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    form_id = Column(String, ForeignKey('agent_forms.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    form_type = Column(String, nullable=False)
    answers = Column(JSONB, nullable=False)  # snapshot: [{questionId, label, type, value}]
    contact_name = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    property_address = Column(String, nullable=True)
    is_reviewed = Column(Boolean, default=False, nullable=False)
    # NULL = General. Inherited from the visitor's FormViewSession at submit time (never
    # re-resolved from a token passed fresh on submit) - see forms_routes.py submit_form.
    campaign_id = Column(String, ForeignKey('form_campaigns.id', ondelete='SET NULL'), nullable=True, index=True)
    # Schedule a Showing only: {listing_key, preferred: [{date, time}], note, confirmed_date,
    # confirmed_time}. The visitor's form link reads it back (by submission id) to show the
    # request as Pending until the agent confirms a time, then Confirmed.
    showing = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    form = relationship("AgentForm", back_populates="submissions")


class FormViewSession(Base):
    __tablename__ = "form_view_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    form_id = Column(String, ForeignKey('agent_forms.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    # Anonymous per-browser-tab id (crypto.randomUUID, held in a ref on the FormRunner instance) -
    # one row per (form, session), created on first view and updated in place as the visitor
    # advances, so per-question drop-off can be computed without an append-only event log.
    session_key = Column(String, nullable=False, index=True)
    furthest_step_index = Column(Integer, default=0, nullable=False)
    furthest_question_id = Column(String, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    submission_id = Column(String, ForeignKey('form_submissions.id', ondelete='SET NULL'), nullable=True)
    # NULL = General. Set once, only when this session row is first created (from the campaign
    # token on that first progress ping) - never touched again for the life of the session. This
    # is the single source of truth a submission's own campaign_id is inherited from.
    campaign_id = Column(String, ForeignKey('form_campaigns.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # ---- Insights (migration e7f4a0c3b2d6) - see forms_service.record_form_progress ----
    # What they've answered so far, {question_id: value} - kept even if they never submit, so the
    # agent can see unfinished responses. Cleared after UNFINISHED_RETENTION_DAYS.
    partial_answers = Column(JSONB(none_as_null=True), nullable=True)
    # Every question they landed on, in order: [{"q": question_id, "s": step, "t": iso}] (capped).
    # Drives per-question drop-off on branching forms and time spent per question.
    path = Column(JSONB(none_as_null=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)        # first real question
    last_activity_at = Column(DateTime(timezone=True), nullable=True)  # when they dropped off, if they did
    source = Column(String, nullable=True)     # qr | link | website - first touch only
    device = Column(String, nullable=True)     # mobile | tablet | desktop
    referrer = Column(String, nullable=True)   # host only, e.g. instagram.com
    contact_name = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)  # agent hid it from Unfinished

    __table_args__ = (
        UniqueConstraint('form_id', 'session_key', name='uq_form_view_sessions_form_session'),
    )


class AgentHubEvent(Base):
    __tablename__ = "agent_hub_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    event_type = Column(String, nullable=False)  # 'visit' | 'qr_scan' | 'form_view' | 'interaction'
    # Anonymous per-browser id (localStorage) sent by the public Agent Hub page - lets the visits/
    # interactions stats count unique visitors instead of raw page-load/click events.
    visitor_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class Lead(Base):
    __tablename__ = "leads"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    name = Column(String, nullable=True)
    email = Column(String, nullable=True, index=True)   # normalized (lowercased/trimmed) for dedup matching
    phone = Column(String, nullable=True, index=True)   # normalized (digits only) for dedup matching

    represented = Column(String, nullable=False, default='unknown')  # 'yes' | 'no' | 'unknown'
    status = Column(String, nullable=False, default='new')           # new | contacted | in_talks | signed
    tags = Column(JSONB, nullable=False, default=list)
    notes = Column(Text, nullable=True)
    last_contacted_at = Column(DateTime(timezone=True), nullable=True)
    hidden_at = Column(DateTime(timezone=True), nullable=True)

    # Set when this lead's matched Collection (showcase) is deleted; cleared if a new one is
    # ever created for them again. Matching is by owner+normalized email/phone, not a hard FK,
    # since most showcases are auto-created from open house sign-in with no LeadEvent link.
    showcase_deleted_at = Column(DateTime(timezone=True), nullable=True)

    # Set to now() every time the owning agent opens this lead's detail page. Compared against the
    # newest LeadEvent.occurred_at (not `updated_at`, which SQLAlchemy bumps on any unrelated field
    # change) to decide whether the lead has unseen activity - see needs_attention in lead_routes.py.
    viewed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")
    events = relationship("LeadEvent", back_populates="lead", cascade="all, delete-orphan", order_by="LeadEvent.occurred_at.desc()")
    lead_notes = relationship("LeadNote", back_populates="lead", cascade="all, delete-orphan", order_by="LeadNote.created_at.desc()")


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id = Column(String, ForeignKey('leads.id', ondelete='CASCADE'), nullable=False, index=True)

    # open_house_visitor | form_submission | property_tour | property_comment | collection_visitor
    source_type = Column(String, nullable=False)
    source_id = Column(String, nullable=True)
    summary = Column(String, nullable=False)  # human-readable, e.g. "Signed in at Open House - 123 Main St"

    property_address = Column(String, nullable=True)
    # All three source FKs are ON DELETE SET NULL: deleting the open house event, showcase, or
    # form a lead came through nulls the pointer here (the lead + its history survive) rather
    # than blocking the delete with an FK violation. See migrations a1c4e8b2d9f3 (collection_id)
    # and f1a4c7e9b2d5 (open_house_event_id, form_id).
    open_house_event_id = Column(String, ForeignKey('open_house_events.id', ondelete='SET NULL'), nullable=True)
    collection_id = Column(String, ForeignKey('collections.id', ondelete='SET NULL'), nullable=True)
    form_id = Column(String, ForeignKey('agent_forms.id', ondelete='SET NULL'), nullable=True)
    raw_snapshot = Column(JSONB, nullable=True)  # form answers / message text, for detail view

    occurred_at = Column(DateTime(timezone=True), nullable=False)

    # Set once the agent has visited the showcase this event points to (see
    # collections_routes.py's mark-seen endpoint) - clears the lead card's "commented"/"tour
    # request" tags the same way PropertyInteraction.agent_viewed_at already clears like/dislike
    # tags, instead of those tags staying on forever with no way to dismiss them.
    agent_viewed_at = Column(DateTime(timezone=True), nullable=True)

    lead = relationship("Lead", back_populates="events")
    open_house_event = relationship("OpenHouseEvent")
    collection = relationship("Collection")
    form = relationship("AgentForm")


class LeadNote(Base):
    __tablename__ = "lead_notes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id = Column(String, ForeignKey('leads.id', ondelete='CASCADE'), nullable=False, index=True)
    author_id = Column(String, ForeignKey('users.id'), nullable=True)
    text = Column(Text, nullable=False)

    # Set when this note is tied to a specific open house (e.g. added from Open House -> Visitors) -
    # shown as a labeled sub-thread on the lead profile. Null for general lead notes.
    open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    lead = relationship("Lead", back_populates="lead_notes")
    author = relationship("User")
    open_house_event = relationship("OpenHouseEvent")


class BillingGroup(Base):
    """A brokerage paying for a group of its agents on a custom deal (invoiced via PayPal) - admin only."""
    __tablename__ = "billing_groups"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    brokerage = Column(String, nullable=True)
    contact_name = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    paypal_reference = Column(String, nullable=True)   # their PayPal invoice / recurring-invoice id
    amount = Column(Float, nullable=True)              # what the brokerage pays per billing_cycle
    billing_cycle = Column(String, nullable=False, server_default='monthly', default='monthly')  # monthly | quarterly | yearly
    seats = Column(Integer, nullable=True)             # agents the deal covers (None = no set number)
    plan_tier = Column(String, nullable=True)          # the plan its agents get
    status = Column(String, nullable=False, server_default='active', default='active')  # active | paused | ended
    started_on = Column(Date, nullable=True)
    next_invoice_on = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class PromoCode(Base):
    """An admin-made sign-up code, reusable until it's switched off, runs out of uses or expires.
    'paypal_plan': checkout swaps in a special PayPal plan (e.g. IDX with no setup fee).
    'billed_by_us': the account switches on with no PayPal at all - OpenHousePal bills them directly
    at custom_price (kept for the admin's records)."""
    __tablename__ = "promo_codes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String, nullable=False, unique=True, index=True)  # stored upper-case
    label = Column(String, nullable=True)                           # e.g. "Smith Team - no setup fee"
    kind = Column(String, nullable=False)                           # paypal_plan | billed_by_us
    plan_tier = Column(String, nullable=False)
    paypal_plan_id = Column(String, nullable=True, index=True)
    trial_days = Column(Integer, nullable=False, server_default='0', default=0)  # read from PayPal when saved
    custom_price = Column(Float, nullable=True)
    billing_cycle = Column(String, nullable=True)                   # monthly | quarterly | yearly
    billing_group_id = Column(String, ForeignKey('billing_groups.id', ondelete='SET NULL'), nullable=True)
    max_uses = Column(Integer, nullable=True)                       # None = unlimited
    uses = Column(Integer, nullable=False, server_default='0', default=0)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default='true', default=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AgentActiveDay(Base):
    """One row per agent per day they used the app (admin activity tracker)."""
    __tablename__ = "agent_active_days"

    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    day = Column(Date, primary_key=True)


class AgentTask(Base):
    """A to-do / follow-up reminder an agent works from the Leads page and the dashboard widget.
    Optionally tied to a lead (so it shows on that lead's profile and can carry a suggested
    outreach message). Some are created automatically - e.g. a next-day "follow up" task the
    moment an open house sign-in creates or updates a lead (auto_source = 'open_house_signin').
    Included in the agent's private .ics calendar feed."""
    __tablename__ = "agent_tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    # Optional link to the lead this follow-up is about. SET NULL so deleting a lead keeps the
    # task (it just loses the shortcut back to a now-gone profile).
    lead_id = Column(String, ForeignKey('leads.id', ondelete='SET NULL'), nullable=True, index=True)

    title = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    # 'call' | 'text' | 'email' | 'showing' | 'general' - drives the icon and the .ics category.
    kind = Column(String, nullable=False, default='general')
    priority = Column(String, nullable=False, default='normal')   # 'low' | 'normal' | 'high'

    due_at = Column(DateTime(timezone=True), nullable=True, index=True)  # NULL = someday / no date
    # A whole-day task (no specific time) - the .ics feed emits it as an all-day VEVENT.
    all_day = Column(Boolean, nullable=False, default=False)
    remind_at = Column(DateTime(timezone=True), nullable=True, index=True)  # when to fire the in-app/notification reminder
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)       # set once that reminder has fired, so it fires once

    completed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # For auto-created tasks: what made it, so we never make a second one for the same trigger.
    # e.g. 'open_house_signin'. NULL for tasks the agent added by hand.
    auto_source = Column(String, nullable=True)
    auto_source_id = Column(String, nullable=True, index=True)  # e.g. the open_house_event_id

    # A ready-to-send draft outreach message (SMS/email body) the agent can copy. For open-house
    # follow-ups this is templated now; a later pass swaps in an AI-written version.
    suggested_message = Column(Text, nullable=True)
    # One short line saying WHY this follow-up exists ("Signed in at your open house - 12 Oak St"),
    # shown on the task card so the agent knows what the touch point was without opening notes.
    context = Column(String, nullable=True)
    # Short secret behind the one-tap "Mark as done" link in the calendar event (/d/<code>).
    done_code = Column(String, nullable=True, unique=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        # One auto task per (owner, source, source id) - the guard behind "don't make a second
        # follow-up if the same guest signs in again at the same open house".
        UniqueConstraint('owner_id', 'auto_source', 'auto_source_id', name='uq_agent_tasks_auto_dedup'),
        Index('ix_agent_tasks_owner_open', 'owner_id', 'completed_at'),
    )

    owner = relationship("User", back_populates="tasks")
    lead = relationship("Lead")


class MailConnection(Base):
    """An agent's own mailbox (Gmail or Outlook) that their GUEST emails are sent from - Showcase
    invites, tour confirmations, Spotlight alerts... - so they arrive from the agent's real address
    instead of OpenHousePal's. One per agent. Agent-only alerts never use it. If sending through it
    fails for any reason the email goes out through OpenHousePal instead (agent_mailbox_service)."""
    __tablename__ = "mail_connections"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    provider = Column(String, nullable=False)  # google | microsoft
    account_email = Column(String, nullable=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime(timezone=True), nullable=True)
    scopes = Column(String, nullable=True)
    # The agent's switch: guest emails go from this mailbox (on) or from OpenHousePal (off).
    enabled = Column(Boolean, nullable=False, default=True, server_default='true')
    # ok | needs_reconnect (the provider refused our token - sending paused, OpenHousePal sends)
    status = Column(String, nullable=False, default='ok', server_default='ok')
    last_error = Column(Text, nullable=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    # Per-day count, so we stay well under Gmail/Outlook sending limits (then OpenHousePal sends).
    sent_day = Column(String, nullable=True)
    sent_count = Column(Integer, nullable=False, default=0, server_default='0')
    total_sent = Column(Integer, nullable=False, default=0, server_default='0')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ClientEmail(Base):
    """One email an agent's client was sent (Showcase invite / digest, tour confirmation, Spotlight
    alert...) - whether it went from the agent's own mailbox or OpenHousePal - with its open and
    click tracking. The id is also the tracking token in the pixel / link addresses
    (client_email_service). Agents see these on the Emails page and in each lead's history."""
    __tablename__ = "client_emails"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    recipient_email = Column(String, nullable=False)   # normalized (lowercased/trimmed)
    template = Column(String, nullable=False)
    subject = Column(String, nullable=False, default='')
    # auto = a recurring automatic update (daily / weekly digest, Spotlight listing alert) - the kind
    # the engagement rules slow down or stop; personal = a reply to something they did or the agent did.
    kind = Column(String, nullable=False, default='personal')
    via = Column(String, nullable=True)                # mailbox | openhousepal
    status = Column(String, nullable=False, default='sending')  # sending | sent | failed
    links = Column(JSONB, nullable=True)               # {"collection_link": "https://..."} - where tracked links go
    error = Column(String, nullable=True)              # why it didn't go through (provider's answer, trimmed)
    # What Mailgun reported after sending (webhooks, client_email_routes.mailgun_events): delivered |
    # delayed | bounced | blocked | spam, with a plain reason. NULL = no word yet / sent from their own mailbox.
    delivery = Column(String(16), nullable=True)
    delivery_note = Column(String(300), nullable=True)
    delivery_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    open_count = Column(Integer, nullable=False, default=0, server_default='0')
    clicked_at = Column(DateTime(timezone=True), nullable=True)
    click_count = Column(Integer, nullable=False, default=0, server_default='0')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index('ix_client_emails_owner_recipient_sent', 'owner_id', 'recipient_email', 'sent_at'),)


class EmailLog(Base):
    """One row per email OpenHousePal sends - agent alerts, client emails (from Mailgun or the agent's
    own mailbox) and account emails - with what Mailgun later reported. Backs the admin Insights
    Emails page. Counts only: no address, no subject, no body (services/email_log_service.py)."""
    __tablename__ = "email_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    template = Column(String(80), nullable=False)
    audience = Column(String(12), nullable=False)         # agent | client | account
    owner_id = Column(String, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)  # the agent it's for / from
    client_email_id = Column(String, nullable=True)       # ClientEmail (opens + clicks) when it's a tracked client email
    via = Column(String(16), nullable=False)              # openhousepal | mailbox
    status = Column(String(12), nullable=False)           # sent | failed | held (dev - never left this computer)
    provider_id = Column(String(255), nullable=True, index=True)  # Mailgun's message id - matches its webhooks
    delivery = Column(String(16), nullable=True)          # delivered | delayed | bounced | blocked | spam
    delivery_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class ClientEmailPref(Base):
    """Per agent + client: whether automatic emails go out daily, weekly or not at all, and why.
    `mode` is the last state the rules worked out (kept so a change can be announced once);
    `manual` is the agent's own choice ("off" stays off; "on" restarts the count)."""
    __tablename__ = "client_email_prefs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    email = Column(String, nullable=False)
    mode = Column(String, nullable=False, default='daily', server_default='daily')  # daily | weekly | off
    reason = Column(Text, nullable=True)
    manual = Column(String, nullable=True)             # None | off
    reset_at = Column(DateTime(timezone=True), nullable=True)   # count unopened emails only after this
    changed_at = Column(DateTime(timezone=True), nullable=True)
    last_weekly_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint('owner_id', 'email', name='uq_client_email_prefs_owner_email'),)


class CalendarConnection(Base):
    """One agent's connection to an external calendar (Google now; `provider` leaves room for
    Microsoft). We hold the OAuth tokens and the id of a dedicated "OpenHousePal" secondary
    calendar we create in their account - the calendar.app.created scope only lets us manage
    events on a calendar we made, never their existing ones."""
    __tablename__ = "calendar_connections"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    provider = Column(String, nullable=False, server_default='google', default='google')
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime(timezone=True), nullable=True)
    calendar_id = Column(String, nullable=True)      # the OHP secondary calendar in their account
    account_email = Column(String, nullable=True)    # which Google account connected
    scopes = Column(String, nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('owner_id', 'provider', name='uq_calendar_connections_owner_provider'),
    )

    owner = relationship("User")
    event_links = relationship("CalendarEventLink", back_populates="connection", cascade="all, delete-orphan")


class CalendarEventLink(Base):
    """Maps one OHP task / scheduled open house to the event we created for it in the connected
    calendar, so an edit or delete updates the right event instead of making duplicates."""
    __tablename__ = "calendar_event_links"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = Column(String, ForeignKey('calendar_connections.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = Column(String, nullable=False)            # 'task' | 'open_house'
    source_id = Column(String, nullable=False)
    external_event_id = Column(String, nullable=False)
    # What we last sent - a sync that would send the same event again skips the call.
    body_hash = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('connection_id', 'kind', 'source_id', name='uq_calendar_event_links_src'),
    )

    connection = relationship("CalendarConnection", back_populates="event_links")


class AgentReview(Base):
    """A client review collected through the agent's public review link. Shown on the Agent Hub.
    When the rating is high the public flow also nudges the reviewer to re-post it on Google /
    Zillow (deep link + their text on the clipboard) - those destinations are configured in
    Settings and stored on AgentHub.config.reviews, not here."""
    __tablename__ = "agent_reviews"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    reviewer_name = Column(String, nullable=False)
    reviewer_email = Column(String, nullable=True)   # optional - lets the agent follow up / thank them
    rating = Column(Integer, nullable=False)         # 1-5
    body = Column(Text, nullable=False)

    source = Column(String, nullable=False, default='direct')      # 'direct' (our form) - room for imports later
    status = Column(String, nullable=False, default='published')   # 'published' | 'hidden'
    # Set once the reviewer confirmed they also posted it on that external site (best-effort, from
    # the public flow's "I posted it" button) - purely informational for the agent.
    posted_google_at = Column(DateTime(timezone=True), nullable=True)
    posted_zillow_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    owner = relationship("User")


class DigestDismissal(Base):
    """An agent manually dismissing one Daily Digest "needs attention" item (see
    ai_insights_service.get_daily_digest) without leaving the Dashboard. item_id is the same
    reason-specific id the digest computes fresh on every request (e.g. "lead-{lead_id}-tour_request"),
    not a foreign key - the underlying record it refers to may itself change or disappear, and the
    dismissal should still hold. A later, DIFFERENT reason for the same lead gets its own item_id and
    is unaffected."""
    __tablename__ = "digest_dismissals"
    __table_args__ = (UniqueConstraint('owner_id', 'item_id', name='uq_digest_dismissals_owner_item'),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    item_id = Column(String, nullable=False)
    dismissed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Collection(Base):
    __tablename__ = "collections"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(String, ForeignKey('users.id'), nullable=False)  # Required - agent who owns the collection
    share_token = Column(String, unique=True, nullable=True)
    short_code = Column(String, unique=True, nullable=True, index=True)  # short personal link /s/<code> (services/showcase_links.py)
    is_public = Column(Boolean, default=True)
    status = Column(String, default="ACTIVE", nullable=False)  # ACTIVE, INACTIVE
    # INACTIVE = asleep: no matching, no emails. 'no_response' (the client ignored 3 weekly emails -
    # wakes by itself when they open their Showcase, services/showcase_sleep.py) | 'agent' | NULL (older rows).
    inactive_reason = Column(String(24), nullable=True)
    inactive_at = Column(DateTime(timezone=True), nullable=True)
    favorited_at = Column(DateTime(timezone=True), nullable=True)  # the agent starred it - sorts first on the Showcases page

    # Notification Settings
    notify_visitor = Column(Boolean, default=True, nullable=False)
    notify_agent = Column(Boolean, default=True, nullable=False)

    # Anonymous visitor info (for open house collections)
    visitor_email = Column(String, nullable=True)
    visitor_name = Column(String, nullable=True)
    visitor_phone = Column(String, nullable=True)
    original_open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)

    # How this showcase came to exist - null for pre-existing rows (manual/open-house creation
    # predates this column); set going forward. 'manual' | 'open_house' | 'form'.
    source = Column(String, nullable=True)
    source_form_submission_id = Column(String, ForeignKey('form_submissions.id', ondelete='SET NULL'), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_synced_at = Column(DateTime(timezone=True), nullable=True)  # Track when properties were last synced
    last_visitor_activity_at = Column(DateTime(timezone=True), nullable=True)  # Track when a visitor actually interacts with the collection
    last_agent_dismissed_at = Column(DateTime(timezone=True), nullable=True)   # Track when the agent last "dismissed" new listings

    # Free-text "ask my agent to refine my search" message a visitor sent from the public showcase
    # page (an alternative to editing the search criteria themselves) - cleared once the agent
    # dismisses it (see collections_routes.py). Single most-recent message only, not a thread.
    refine_request_message = Column(Text, nullable=True)
    refine_request_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    owner = relationship("User", back_populates="collections")
    properties = relationship("Property", secondary=collection_properties, back_populates="collections")
    original_open_house_event = relationship("OpenHouseEvent", foreign_keys=[original_open_house_event_id])
    preferences = relationship("CollectionPreferences", back_populates="collection", uselist=False, cascade="all, delete-orphan")
    property_interactions = relationship("PropertyInteraction", back_populates="collection", cascade="all, delete-orphan")
    property_comments = relationship("PropertyComment", back_populates="collection", cascade="all, delete-orphan")
    property_tours = relationship("PropertyTour", back_populates="collection", cascade="all, delete-orphan")

class PriceHistoryParcel(Base):
    """A parcel whose 2012+ Bright listings the history backfill has pulled (and when)."""
    __tablename__ = "price_history_parcels"

    tax_id = Column(String, primary_key=True)
    checked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    listings_found = Column(Integer, nullable=True)


class PropertyPriceEvent(Base):
    """One price or status change the MLS sync saw on a listing (see property_sync_service
    _upsert_property). Bright only sends a listing's latest change, so this log is what lets the
    price history show every cut - from the day it was added on."""
    __tablename__ = "property_price_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    listing_key = Column(String, nullable=False)
    observed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    kind = Column(String, nullable=False)  # 'listed' | 'price' | 'status'
    price = Column(Float, nullable=True)
    previous_price = Column(Float, nullable=True)
    status = Column(String, nullable=True)
    previous_status = Column(String, nullable=True)

    __table_args__ = (Index('ix_property_price_events_listing_key', 'listing_key', 'observed_at'),)


class PropertyFieldChange(Base):
    """One field of one listing that a sync changed: the old and new value (as text, cut at 500
    characters; a photo gallery as its count). Diffs only - never a copy of the whole record - so
    it is small enough to keep forever and answers "what did this listing say before, and when did
    it change?" for any field (property_sync_service._log_field_changes)."""
    __tablename__ = "property_field_changes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    listing_key = Column(String, nullable=False)
    field = Column(String, nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index('ix_property_field_changes_listing_key', 'listing_key', 'observed_at'),)


class Property(Base):
    __tablename__ = "properties"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    listing_key = Column(String, unique=True, index=True, nullable=False)
    listing_id = Column(String, nullable=True) # MLS Number

    # Basic property info
    street_address = Column(String, nullable=False)
    unparsed_address = Column(String, nullable=True)
    city = Column(String, index=True, nullable=False)
    state = Column(String, index=True, nullable=False)
    zipcode = Column(String, index=True, nullable=True)

    # Property details
    price = Column(Float, index=True, nullable=True)
    bedrooms = Column(Float, index=True, nullable=True)
    bathrooms = Column(Float, index=True, nullable=True)
    living_area = Column(Float, index=True, nullable=True)
    lot_size = Column(Float, nullable=True)
    home_status = Column(String, index=True, nullable=False)

    # Standardized type for application logic
    home_type = Column(String, index=True, nullable=True)

    # Raw MLS data for precise filtering and reference
    mls_property_type = Column(String, index=True, nullable=True) # e.g., 'Residential'
    mls_structure_design_type = Column(String, index=True, nullable=True) # e.g., 'Interior Row/Townhouse'

    # New added
    price_per_square_feet = Column(Float, nullable=True)
    mls_incorporated_city_name = Column(String, nullable=True)
    raw_mls_data = Column(JSONB, nullable=True)

    # Cached nearby-amenities lookup (Google Places) for the Open House Cheat Sheet PDF -
    # persisted rather than in-memory so repeat kit downloads don't re-bill Google.
    nearby_amenities = Column(JSONB, nullable=True)
    nearby_amenities_cached_at = Column(DateTime(timezone=True), nullable=True)

    # Location
    full_address = Column(String, index=True, nullable=True) # Combined address for fast searching
    latitude = Column(Float, index=True, nullable=True)
    longitude = Column(Float, index=True, nullable=True)
    google_rooftop_latitude = Column(Float, nullable=True)
    google_rooftop_longitude = Column(Float, nullable=True)
    google_geocode_location_type = Column(String, nullable=True)
    google_geocoded_at = Column(DateTime(timezone=True), nullable=True)

    img_src = Column(String, nullable=True)

    # Narrative & Media
    description = Column(Text, nullable=True)
    photos = Column(JSONB, nullable=True)  # List of URL strings

    # Listing Agent & Office
    list_agent_full_name = Column(String, nullable=True)
    list_agent_email = Column(String, nullable=True)
    list_office_name = Column(String, index=True, nullable=True)
    list_office_phone = Column(String, nullable=True)
    list_agent_mls_id = Column(String, index=True, nullable=True)  # Matches User.mls_id - lets us find "my listings"
    co_list_agent_mls_id = Column(String, index=True, nullable=True)
    is_manual = Column(Boolean, default=False, nullable=False)  # Agent-entered "My Listings" row (synthetic listing_key) - never touched by the Bright MLS sync

    # Core Structural & Exterior
    architectural_style = Column(JSONB, nullable=True)
    construction_materials = Column(JSONB, nullable=True)
    roof_type = Column(JSONB, nullable=True)
    foundation_details = Column(JSONB, nullable=True)
    structure_type = Column(JSONB, nullable=True)
    levels = Column(JSONB, nullable=True)

    # Interior & Features
    interior_features = Column(JSONB, nullable=True)
    exterior_features = Column(JSONB, nullable=True)
    flooring = Column(JSONB, nullable=True)
    appliances = Column(JSONB, nullable=True)
    fireplaces = Column(Integer, nullable=True)
    fireplace_features = Column(JSONB, nullable=True)
    door_features = Column(JSONB, nullable=True)
    window_features = Column(JSONB, nullable=True)

    # Utilities & Systems
    cooling = Column(JSONB, nullable=True)
    heating = Column(JSONB, nullable=True)
    water_source = Column(JSONB, nullable=True)
    sewer = Column(JSONB, nullable=True)
    utilities = Column(JSONB, nullable=True)

    # Parking
    garage_spaces = Column(Float, nullable=True)
    parking_features = Column(JSONB, nullable=True)
    has_garage = Column(Boolean, nullable=True)

    # Community & HOA
    association_fee = Column(Float, nullable=True)
    association_fee_frequency = Column(String, nullable=True)
    association_amenities = Column(JSONB, nullable=True)
    association_fee_includes = Column(JSONB, nullable=True)
    has_association = Column(Boolean, nullable=True)

    # Lot & Location
    lot_features = Column(JSONB, nullable=True)
    view = Column(JSONB, nullable=True)
    waterfront_features = Column(JSONB, nullable=True)
    has_waterfront_view = Column(Boolean, nullable=True)
    has_view = Column(Boolean, nullable=True)

    # Tax & Financial
    tax_annual_amount = Column(Float, nullable=True)
    tax_year = Column(Integer, nullable=True)

    # Dates
    year_built = Column(Integer, index=True, nullable=True)
    mls_list_date = Column(DateTime(timezone=True), nullable=True)
    price_change_timestamp = Column(DateTime(timezone=True), nullable=True)

    # Education
    elementary_school = Column(String, nullable=True)
    middle_or_junior_school = Column(String, nullable=True)
    high_school = Column(String, nullable=True)
    school_district_name = Column(String, index=True, nullable=True)

    # Neighborhood & Location
    county = Column(String, index=True, nullable=True)
    township = Column(String, index=True, nullable=True)
    subdivision_name = Column(String, index=True, nullable=True)
    directions = Column(Text, nullable=True)
    zoning = Column(String, nullable=True)

    # Financials (More detail)
    tax_assessment_amount = Column(Float, nullable=True)
    assessment_year = Column(Integer, nullable=True)
    possession = Column(JSONB, nullable=True)
    listing_tax_id = Column(String, nullable=True)

    # Detailed Features
    cooling_fuel = Column(JSONB, nullable=True)
    heating_fuel = Column(JSONB, nullable=True)
    above_grade_finished_area = Column(Float, nullable=True)
    below_grade_finished_area = Column(Float, nullable=True)
    basement = Column(JSONB, nullable=True)
    accessibility_features = Column(JSONB, nullable=True)

    # Booleans
    has_basement = Column(Boolean, nullable=True)
    has_central_air = Column(Boolean, nullable=True)
    has_fireplace = Column(Boolean, nullable=True)

    # More Financials
    association_fee_2 = Column(Float, nullable=True)
    association_fee_2_frequency = Column(String, nullable=True)

    # More Agent Info
    list_agent_preferred_phone = Column(String, nullable=True)

    lot_size_acres = Column(Float, nullable=True)
    attached_garage_yn = Column(Boolean, nullable=True)
    new_construction_yn = Column(Boolean, nullable=True)
    senior_community_yn = Column(Boolean, nullable=True)
    pets_allowed = Column(JSONB, nullable=True)

    # Listing Intelligence
    original_list_price = Column(Float, nullable=True)
    days_on_market = Column(Integer, index=True, nullable=True)
    # When the sync last read DaysOnMarket from Bright. Not updated_at: geocoding, the open-house
    # pass and other jobs bump that too, which froze the day count (see utils/listing_dom.py).
    dom_read_at = Column(DateTime(timezone=True), nullable=True)
    cumulative_days_on_market = Column(Integer, nullable=True)

    # Structure
    stories = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    modification_timestamp = Column(DateTime(timezone=True), index=True, nullable=True)

    # MLS-reported open house (Bright's separate BrightOpenHouses resource, not part of the
    # listing record itself) - used as a Discovery fallback badge when no agent has hosted this
    # listing inside OpenHousePal. Synced independently since scheduling an open house doesn't
    # touch the listing's own modification_timestamp.
    mls_open_house_date = Column(Date, nullable=True)
    mls_open_house_start_time = Column(Time, nullable=True)
    mls_open_house_end_time = Column(Time, nullable=True)
    # Every upcoming Bright open house slot (a listing can have several - Sat + Sun, next
    # weekend). The three columns above stay as the soonest one. Shape + merge rules live in
    # app/services/mls_open_houses.py.
    mls_open_houses = Column(JSONB(none_as_null=True), nullable=True)

    # Relationships
    collections = relationship("Collection", secondary=collection_properties, back_populates="properties")

    __table_args__ = (
        Index('ix_properties_city_state', 'city', 'state'),
        Index('ix_properties_listing_tax_id', 'listing_tax_id'),
        Index('ix_properties_lat_long', 'latitude', 'longitude'),
        Index('ix_properties_status_price', 'home_status', 'price'),
        # Agent sale matching (content_adapters._agent_sale_identifier_filters) - migration f6a9d2c4b8e1
        Index('ix_properties_raw_buyer_agent_mls_id', text("(raw_mls_data ->> 'BuyerAgentMlsId')")),
        Index('ix_properties_raw_co_buyer_agent_mls_id', text("(raw_mls_data ->> 'CoBuyerAgentMlsId')")),
        Index('ix_properties_raw_list_agent_mls_id', text("(raw_mls_data ->> 'ListAgentMlsId')")),
        Index('ix_properties_raw_co_list_agent_mls_id', text("(raw_mls_data ->> 'CoListAgentMlsId')")),
        # Address search (ILIKE / pg_trgm) - migration e7b2d4f6a8c1. Declared here so autogenerate
        # never drops it again (that is how the first trigram index disappeared).
        Index('ix_properties_full_address_trgm', 'full_address', postgresql_using='gin',
              postgresql_ops={'full_address': 'gin_trgm_ops'}),
    )


class OpenHouseEvent(Base):
    __tablename__ = "open_house_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    qr_code = Column(String, unique=True, nullable=False)
    agent_id = Column(String, ForeignKey('users.id'), nullable=False)
    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False)  # Soft delete flag
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Track when deleted
    form_url = Column(String, nullable=True)  # Store the form link
    cover_image_url = Column(String, nullable=True)  # Store the selected cover image
    cover_image_position = Column(JSONB, nullable=True)  # {x, y} crop offset (%) for the Sign In sheet cover photo
    cheat_sheet_photos = Column(JSONB, nullable=True)  # Agent-selected photos for the Open House Cheat Sheet PDF
    cheat_sheet_snapshot = Column(JSONB, nullable=True)  # Frozen Cheat Sheet PDF payload created with the kit.

    # Property metadata (replaces property_id relationship)
    address = Column(String, nullable=True)
    abbreviated_address = Column(String, nullable=True)
    house_type = Column(String, nullable=True)
    lot_size = Column(Integer, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    zipcode = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    bedrooms = Column(Float, nullable=True)
    bathrooms = Column(Float, nullable=True)
    living_area = Column(Integer, nullable=True)
    home_status = Column(String, nullable=True)
    listing_key = Column(String, nullable=True, index=True)

    # Property details for PDF generation and collection preferences

    notes = Column(Text, nullable=True)
    similar_properties_snapshot = Column(JSONB, nullable=True) # Full property data snapshot

    # Scheduling - only set when this open house came from an approved Open House Manager host
    # request (a real booked date/time). Self-hosted opens created via the wizard leave these
    # null, same as before - they remain evergreen QR pages with no fixed schedule.
    scheduled_date = Column(Date, nullable=True)
    scheduled_start_time = Column(Time, nullable=True)
    scheduled_end_time = Column(Time, nullable=True)
    # Snapshot of who owns/lists this property, distinct from `agent_id` (the hosting agent) when
    # this open house was hosted by someone else's approved request. Denormalized on purpose (like
    # the address/price fields above) so history stays correct even if the listing later changes hands.
    listing_owner_agent_id = Column(String, ForeignKey('users.id'), nullable=True)
    # Default theme for the on-screen Sign-In Sheet/Kiosk guests see when they scan this open
    # house's QR code (or the agent leaves a tablet running it) - does not affect the printed flyer.
    qr_dark_mode = Column(Boolean, default=True, nullable=False)
    # Hosting-agent-controlled, per kit. Off by default: the listing agent (when different from the
    # host) always sees basic anonymous performance (see get_listing_detail), but never actual guest
    # name/email/phone unless the host explicitly turns this on for this specific open house.
    share_guests_with_listing_agent = Column(Boolean, default=False, nullable=False, server_default='false')
    # Build a Showcase (and its invite email) for each guest who signs in without an agent. On by
    # default; a listing agent sitting their own listing can turn it off so guests aren't sent off
    # to other homes. A guest who scans the Similar Homes sheet's "see more like these" QR asked
    # for other homes, so they still get one.
    auto_showcase = Column(Boolean, default=True, nullable=False, server_default='true')
    # The Similar Homes sheet in the kit + the similar homes on the Guest Pass. Off = neither shows
    # (similar_properties_snapshot is kept, so switching back on restores the picked homes). A
    # listing agent hosting their own listing starts with this and auto_showcase off.
    similar_homes = Column(Boolean, default=True, nullable=False, server_default='true')
    # 'on' | 'no_calendar' (follow-ups made but kept off the agent's calendar) | 'off' (no follow-ups)
    followups_mode = Column(String, default='on', nullable=False, server_default='on')
    # Bright MLS plans: after signing in, the guest's phone opens their Guest Pass for this open
    # house (the home, its Similar Homes + Cheat Sheet as web pages, one-tap disclosure / second
    # tour requests, their Showcase) - app/api/guest_pass_routes.py. On by default.
    guest_pass = Column(Boolean, default=True, nullable=False, server_default='true')
    # Guest Pass footer: the host's Email Signature card (site version) in place of the plain agent
    # card - on by default; only shows when the plan has signatures and one is saved.
    pass_signature = Column(Boolean, default=True, nullable=False, server_default='true')
    # No-MLS Pro plan (no Guest Pass - it has no Similar Homes / Cheat Sheet / Showcases): "hub"
    # sends the guest to the agent's Agent Hub after signing in; NULL / "none" = nothing.
    after_signin = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    agent = relationship("User", foreign_keys=[agent_id])
    listing_owner_agent = relationship("User", foreign_keys=[listing_owner_agent_id])


class OpenHouseVisitor(Base):
    __tablename__ = "open_house_visitors"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    full_name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=False)

    # Visit Information
    has_agent = Column(String, nullable=False)  # YES, NO
    notes = Column(Text, nullable=True)  # legacy single-blob note, superseded by LeadNote via lead_id

    # Open House Context
    open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)
    qr_code = Column(String, nullable=False)
    form_url = Column(String, nullable=True)  # Store the form link

    # Direct link to the matching Lead record, so visitor notes/activity can share the Lead's
    # note history. Backfilled for existing rows via the lead_notes migration.
    lead_id = Column(String, ForeignKey('leads.id', ondelete='SET NULL'), nullable=True, index=True)

    interested_in_similar = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # How the sign-in went (admin Insights, migration c7e9a1b3d5f8): seconds from first tapping the
    # form to pressing Sign in (NULL = not measured), phone | kiosk, and mobile | tablet | desktop.
    signin_seconds = Column(Integer, nullable=True)
    signin_mode = Column(String(12), nullable=True)
    signin_device = Column(String(12), nullable=True)

    # Relationships
    open_house_event = relationship("OpenHouseEvent")
    lead = relationship("Lead")


class OpenHouseSignInView(Base):
    """
    One row per time the public sign-in form is actually shown to someone (page load on a
    guest's own phone, or each fresh instance in Kiosk mode) - as opposed to OpenHouseVisitor,
    which only ever gets a row on a *completed* submission. Together these back the "Completion
    %" stat (started vs. finished), which otherwise has no way to know how many people opened
    the form and abandoned it.
    """
    __tablename__ = "open_house_signin_views"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    # The sign-in this showing turned into (set on submit), and phone | kiosk / mobile | tablet | desktop.
    visitor_id = Column(String, ForeignKey('open_house_visitors.id', ondelete='SET NULL'), nullable=True)
    mode = Column(String(12), nullable=True)
    device = Column(String(12), nullable=True)

    open_house_event = relationship("OpenHouseEvent")


class OpenHouseFormRequest(Base):
    """A "Request Seller Disclosure" or "Schedule a Second Visit" request scanned from the
    Cheat Sheet - property-scoped to the open house it came from (no address entry needed),
    same pattern as OpenHouseVisitor sign-ins."""
    __tablename__ = "open_house_form_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    open_house_event_id = Column(String, ForeignKey('open_house_events.id', ondelete='CASCADE'), nullable=False, index=True)

    form_type = Column(String, nullable=False)  # 'disclosure' | 'second_visit'

    full_name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=False)

    # 'disclosure' requests also ask if they'd like another tour, folding that ask into the
    # same form instead of needing a separate flow.
    wants_another_tour = Column(Boolean, nullable=True)
    another_tour_time = Column(String, nullable=True)

    # 'second_visit' requests just need a preferred time for the next showing, plus the same
    # cross-ask in reverse - whether they'd also like the seller disclosure sent over.
    preferred_visit_time = Column(String, nullable=True)
    wants_disclosure = Column(Boolean, nullable=True)

    lead_id = Column(String, ForeignKey('leads.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Set when the agent marks this handled - "Scheduled" for a second_visit request, "Sent" for a
    # disclosure request. Moves it from the Upcoming to the Past tab in the Forms Results view.
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    open_house_event = relationship("OpenHouseEvent")
    lead = relationship("Lead")


class PropertyInteraction(Base):
    __tablename__ = "property_interactions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = Column(String, ForeignKey('collections.id', ondelete='CASCADE'), nullable=False)
    property_id = Column(String, ForeignKey('properties.id'), nullable=False)

    # Interaction types
    liked = Column(Boolean, default=False)
    disliked = Column(Boolean, default=False)

    # View tracking
    view_count = Column(Integer, default=0)
    last_viewed_at = Column(DateTime(timezone=True), nullable=True)

    # Set only when the AGENT (not a visitor) opens this property from their own showcase
    # management view - clears that property's "NEW" tag there without touching the
    # visitor-facing view_count/last_viewed_at or Collection.last_visitor_activity_at.
    agent_viewed_at = Column(DateTime(timezone=True), nullable=True)

    # The agent's "I recommend this home for you" - set from their Showcase view; the home wears a
    # Recommended tag on both sides and sorts first. Cleared when the client passes on it.
    agent_recommended_at = Column(DateTime(timezone=True), nullable=True)

    # Last time each side actually read this property's comment thread - drives the
    # per-property unread message badge independently for the agent and the visitor.
    agent_comments_read_at = Column(DateTime(timezone=True), nullable=True)
    visitor_comments_read_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    collection = relationship("Collection", back_populates="property_interactions")
    property = relationship("Property")


class PropertyComment(Base):
    __tablename__ = "property_comments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = Column(String, ForeignKey('collections.id', ondelete='CASCADE'), nullable=False)
    property_id = Column(String, ForeignKey('properties.id'), nullable=False)

    # Visitor identification fields
    visitor_name = Column(String, nullable=True)
    visitor_email = Column(String, nullable=True)

    # True when the agent authored this comment (vs. a visitor) - determines which side's
    # unread count/badge it counts toward.
    is_agent = Column(Boolean, default=False, nullable=False)

    content = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    collection = relationship("Collection", back_populates="property_comments")
    property = relationship("Property")


class PropertyTour(Base):
    __tablename__ = "property_tours"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = Column(String, ForeignKey('collections.id', ondelete='CASCADE'), nullable=False)
    property_id = Column(String, ForeignKey('properties.id'), nullable=False)

    # Visitor contact information
    visitor_name = Column(String, nullable=False)
    visitor_email = Column(String, nullable=False)
    visitor_phone = Column(String, nullable=True)

    # Tour scheduling details
    preferred_date = Column(String, nullable=False)
    preferred_time = Column(String, nullable=False)
    preferred_date_2 = Column(String, nullable=True)
    preferred_time_2 = Column(String, nullable=True)
    preferred_date_3 = Column(String, nullable=True)
    preferred_time_3 = Column(String, nullable=True)
    message = Column(Text, nullable=True)

    # Set once the agent has verified the showing through Bright/the listing agent and
    # confirms it to the visitor - only then does it show as an "Upcoming Tour" on their
    # showcase. May differ from the visitor's original preferred slots if a different time
    # was actually agreed on.
    confirmed_date = Column(String, nullable=True)
    confirmed_time = Column(String, nullable=True)

    # Tour flag
    is_completed = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    collection = relationship("Collection", back_populates="property_tours")
    property = relationship("Property")


class CollectionPreferences(Base):
    __tablename__ = "collection_preferences"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = Column(String, ForeignKey('collections.id', ondelete='CASCADE'), nullable=False, unique=True)

    # Property criteria
    min_beds = Column(Integer, nullable=True)
    max_beds = Column(Integer, nullable=True)
    min_baths = Column(Float, nullable=True)
    max_baths = Column(Float, nullable=True)
    min_price = Column(Integer, nullable=True)
    max_price = Column(Integer, nullable=True)
    min_year_built = Column(Integer, nullable=True)
    max_year_built = Column(Integer, nullable=True)

    # Location criteria
    lat = Column(Float, nullable=True)
    long = Column(Float, nullable=True)
    address = Column(String, nullable=True)
    cities = Column(JSONB, nullable=True)
    townships = Column(JSONB, nullable=True)
    school_districts = Column(JSONB, nullable=True)  # New column for school district preferences
    county = Column(String, nullable=True)  # Legacy single-value column, superseded by `counties` below
    counties = Column(JSONB, nullable=True)
    state = Column(String, nullable=True)
    diameter = Column(Float, nullable=True)  # Search diameter in miles (actually used as radius - see property_service)
    # Agent-drawn "Draw on Map" search area - a GeoJSON Polygon/MultiPolygon in WGS84 lng/lat
    # (the same shape google.maps.Polygon.getPath() yields), filtered against Property.latitude/
    # longitude via areas/geography.py's existing ray-casting SQL (no PostGIS - see that module).
    # Mutually exclusive with address/diameter and cities/townships/school_districts, same rule as
    # those three enforce against each other today.
    search_polygon = Column(JSONB, nullable=True)

    # Lot size / living area criteria (acres, square feet)
    min_acres = Column(Float, nullable=True)
    max_acres = Column(Float, nullable=True)
    min_sqft = Column(Float, nullable=True)
    max_sqft = Column(Float, nullable=True)

    # Additional features
    special_features = Column(Text, default="")

    # Home type preferences
    is_town_house = Column(Boolean, nullable=True, default=False)
    is_lot_land = Column(Boolean, nullable=True, default=False)
    is_condo = Column(Boolean, nullable=True, default=False)
    is_multi_family = Column(Boolean, nullable=True, default=False)
    is_single_family = Column(Boolean, nullable=True, default=False)
    is_apartment = Column(Boolean, nullable=True, default=False)
    is_commercial = Column(Boolean, nullable=True, default=False)
    is_farm = Column(Boolean, nullable=True, default=False)

    # Visitor form data
    visiting_reason = Column(String, nullable=True)  # BUYING_SOON, BROWSING, NEIGHBORHOOD, etc.
    has_agent = Column(String, nullable=True)  # YES, NO, LOOKING

    # Match-score comparison ("X% Match" badge, same scoring as the Similar Properties picker).
    # The agent-chosen property to score every match against - independent of, and overrides, the
    # linked OpenHouseEvent's own listing_key (see collections_service.py's original-property
    # resolution) so an agent can swap in a different property, including a sold one, without
    # touching the open house record itself.
    compare_property_listing_key = Column(String, nullable=True)
    show_match_score = Column(Boolean, nullable=False, default=True, server_default='true')

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    collection = relationship("Collection", back_populates="preferences")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    token = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False, nullable=False)

    # Relationship
    user = relationship("User")

class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(String, primary_key=True)  # PayPal event ID
    event_type = Column(String, nullable=False)  # For debugging/monitoring
    processed_at = Column(DateTime(timezone=True), server_default=func.now())


class BundleCode(Base):
    __tablename__ = "bundle_codes"

    code = Column(String, primary_key=True)
    is_used = Column(Boolean, default=False, nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # Notification type and reference
    type = Column(String, nullable=False, index=True)  # OPEN_HOUSE_SIGN_IN, TOUR_REQUEST, PROPERTY_INTERACTION
    reference_type = Column(String, nullable=False)  # VISITOR, TOUR, INTERACTION
    reference_id = Column(String, nullable=False)  # ID of the referenced entity

    # Notification content
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)

    # Denormalized fields for performance (avoid joins when fetching notifications)
    collection_id = Column(String, ForeignKey('collections.id', ondelete='CASCADE'), nullable=True)
    collection_name = Column(String, nullable=True)
    property_id = Column(String, nullable=True)
    property_address = Column(String, nullable=True)
    visitor_name = Column(String, nullable=True)
    link = Column(String, nullable=True)  # Frontend URL path where the event occurred

    # Read status
    is_read = Column(Boolean, default=False, nullable=False, index=True)
    read_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships
    agent = relationship("User", back_populates="notifications")
    collection = relationship("Collection")

class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    subject = Column(String, nullable=False)
    status = Column(String, nullable=False, default='open', index=True)  # 'open' | 'closed'
    # What the agent picked before writing: 'support' (a question), 'bug' (something broke) or
    # 'feature' (an idea). Tickets from before the picker existed read as 'support'.
    category = Column(String, nullable=False, default='support', server_default='support', index=True)
    # Context captured quietly at submission (page they came from, browser/device/screen, urgency) -
    # shown to the admin beside the thread. See support_routes._clean_details for the allowed keys.
    details = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Per-viewer read tracking - a ticket is "unread" for a viewer when updated_at is newer than
    # their own last-read stamp and the newest message wasn't sent by that same viewer.
    agent_last_read_at = Column(DateTime(timezone=True), nullable=True)
    admin_last_read_at = Column(DateTime(timezone=True), nullable=True)

    agent = relationship("User")
    messages = relationship("SupportTicketMessage", backref="ticket", cascade="all, delete-orphan", order_by="SupportTicketMessage.created_at")


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id = Column(String, ForeignKey('support_tickets.id', ondelete='CASCADE'), nullable=False, index=True)

    sender_type = Column(String, nullable=False)  # 'agent' | 'admin'
    body = Column(Text, nullable=False)
    attachments = Column(JSONB, nullable=True)  # screenshot URLs (support-screenshots/ in S3), up to 3

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PublicQuestion(Base):
    """A one-off question from a signed-out homepage visitor ("Ask Us a Question") - just an
    email + message, no account and no threaded conversation (there's no user to notify back, so
    unlike SupportTicket this is a one-way inbox the admin reads and marks handled, not a two-way
    thread). Kept in its own admin tab, separate from agents' own SupportTicket inbox."""
    __tablename__ = "public_questions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SavedManualProperty(Base):
    """An agent's personal library of properties they've typed in by hand (e.g. via the Forms
    'Enter it manually' picker) so they can re-attach the same property elsewhere without
    retyping it. This is intentionally decoupled from Property/is_manual (Open House Manager's
    'My Listings') and Agent Hub's recentSales config array - saving here never creates a real
    listing or sale; the agent still has to add it there deliberately for it to show up as one."""
    __tablename__ = "saved_manual_properties"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    address = Column(String, nullable=False)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    zipcode = Column(String, nullable=True)
    price = Column(Float, nullable=True)
    beds = Column(Float, nullable=True)
    baths = Column(Float, nullable=True)
    sqft = Column(Float, nullable=True)
    image_url = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LoginRateLimit(Base):
    """Sliding-window failed-login counters for /auth/login, keyed by either the attempted
    email or the client IP - see login_rate_limit_service.py for the two very different
    thresholds each scope uses and why. One row per (scope, identifier); attempts/window_started_at
    reset together once the window ages out, same pattern as SignupVerification's rate limit."""
    __tablename__ = "login_rate_limits"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    scope = Column(String, nullable=False)  # 'email' | 'ip'
    identifier = Column(String, nullable=False)  # lowercased email, or client IP string

    attempts = Column(Integer, nullable=False, default=0)
    window_started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('scope', 'identifier', name='uq_login_rate_limit_scope_identifier'),
    )


class ScheduledEmail(Base):
    __tablename__ = "scheduled_emails"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    template_name = Column(String, nullable=False)
    template_variables = Column(JSONB, nullable=False)
    # The agent whose guest this is - lets a guest email go out from the agent's own mailbox
    # (MailConnection) when they've connected one. NULL = not a guest email / sent by OpenHousePal.
    owner_id = Column(String, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)

    # Status tracking
    status = Column(String, default="PENDING", index=True)  # PENDING, SENT, FAILED

    # Scheduling
    scheduled_for = Column(DateTime(timezone=True), nullable=False, index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    # Error tracking
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BlacklistedEmail(Base):
    __tablename__ = "blacklisted_emails"

    email = Column(String, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SignupVerification(Base):
    __tablename__ = "signup_verifications"

    email = Column(String, primary_key=True)
    code = Column(String, nullable=False)
    form_data = Column(JSONB, nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    last_sent_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class SystemSettings(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True)
    value = Column(JSONB, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class SchoolDistrict(Base):
    __tablename__ = "school_districts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, index=True, nullable=False)
    state = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('name', 'state', name='unique_school_district_name_state'),
    )

class NcesAttendanceBoundary(Base):
    __tablename__ = "nces_attendance_boundaries"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    school_id = Column(String, index=True, nullable=False)  # NCES ncessch
    school_name = Column(String, index=True, nullable=False)
    lea_id = Column(String, index=True, nullable=True)
    state = Column(String, index=True, nullable=False)
    grade_low = Column(String, nullable=True)
    grade_high = Column(String, nullable=True)
    level = Column(String, index=True, nullable=True)
    source_name = Column(String, nullable=True)
    defacto = Column(String, nullable=True)
    open_enroll = Column(String, nullable=True)
    multi_boundary = Column(String, nullable=True)
    bbox_xmin = Column(Float, index=True, nullable=False)
    bbox_ymin = Column(Float, index=True, nullable=False)
    bbox_xmax = Column(Float, index=True, nullable=False)
    bbox_ymax = Column(Float, index=True, nullable=False)
    geometry = Column(JSONB, nullable=False)  # Web Mercator polygon rings from the NCES shapefile.
    imported_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('school_id', 'source_name', 'bbox_xmin', 'bbox_ymin', 'bbox_xmax', 'bbox_ymax', name='uq_nces_boundary_shape'),
        Index('ix_nces_boundaries_state_level_bbox', 'state', 'level', 'bbox_xmin', 'bbox_xmax', 'bbox_ymin', 'bbox_ymax'),
    )

class City(Base):
    __tablename__ = "cities"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, index=True, nullable=False)
    state = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('name', 'state', name='unique_city_name_state'),
    )

class Township(Base):
    __tablename__ = "townships"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, index=True, nullable=False)
    state = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('name', 'state', name='unique_township_name_state'),
    )

class Brokerage(Base):
    __tablename__ = "brokerages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, index=True, nullable=False)
    parent_name = Column(String, index=True, nullable=True)
    state = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('name', 'state', name='unique_brokerage_name_state'),
    )

class DiscoveryPreferences(Base):
    __tablename__ = "discovery_preferences"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)

    landmark_address = Column(String, nullable=True)
    miles = Column(Float, nullable=True)
    latitude = Column(Float, index=True, nullable=True)
    longitude = Column(Float, index=True, nullable=True)
    state = Column(String, nullable=True)

    # Criteria
    min_bedrooms = Column(Integer, nullable=True)
    min_bathrooms = Column(Integer, nullable=True)
    min_square_feet = Column(Integer, nullable=True)
    min_price = Column(Float, nullable=True)

    # Array of strings
    brokerages = Column(JSONB, nullable=True)
    cities = Column(JSONB, nullable=True)
    townships = Column(JSONB, nullable=True)
    school_districts = Column(JSONB, nullable=True)
    excluded_agents = Column(JSONB, nullable=True)  # Listing agent full names to hide from results (opposite of brokerages)
    agent_filter_mode = Column(String, nullable=True)  # 'exclude' (default) hides excluded_agents; 'include' shows ONLY excluded_agents

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationship
    user = relationship("User", back_populates="discovery_preferences")


class OpenHouseHostRequest(Base):
    __tablename__ = "open_house_host_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    listing_key = Column(String, nullable=False, index=True)

    listing_owner_agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    requesting_agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # Client-generated id shared across multiple slots offered in one "Request to Host" submission
    # (e.g. "Offer a Second Open") - null for a plain single-slot request. Lets notifications/badge
    # counts treat those slots as one ask while each row here still stays independently
    # approvable/declinable - see open_house_manager_service.create_host_request/get_action_counts.
    group_id = Column(String, nullable=True, index=True)

    requested_date = Column(Date, nullable=False)
    requested_start_time = Column(Time, nullable=False)
    requested_end_time = Column(Time, nullable=False)
    message = Column(Text, nullable=True)

    status = Column(String, default="pending", nullable=False, index=True)  # pending, approved, declined, countered

    # Set once approved - links back to the OpenHouseEvent that got created for this request
    open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)

    # Set when the listing agent proposes a different date/time instead of approving as-is
    counter_date = Column(Date, nullable=True)
    counter_start_time = Column(Time, nullable=True)
    counter_end_time = Column(Time, nullable=True)
    counter_message = Column(Text, nullable=True)

    # Optional second alternate time, offered alongside the first - the host picks whichever works
    counter_date_2 = Column(Date, nullable=True)
    counter_start_time_2 = Column(Time, nullable=True)
    counter_end_time_2 = Column(Time, nullable=True)

    # Set when this request was created from a team broadcast response, rather than a direct ask
    broadcast_id = Column(String, ForeignKey('open_house_team_broadcasts.id', ondelete='SET NULL'), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    responded_at = Column(DateTime(timezone=True), nullable=True)

    listing_owner_agent = relationship("User", foreign_keys=[listing_owner_agent_id])
    requesting_agent = relationship("User", foreign_keys=[requesting_agent_id])
    open_house_event = relationship("OpenHouseEvent")


class OpenHouseHostRequestEvent(Base):
    """Append-only audit trail for one OpenHouseHostRequest - one row per action taken on it
    (requested, countered, counter-accepted/declined, approved, declined, a plain note). Powers
    the request's visible history/thread. The parent row's own fields (status, counter_*,
    requested_*) remain the fast-path "current state" used everywhere else - this table only
    exists so the full back-and-forth is never lost."""
    __tablename__ = "open_house_host_request_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id = Column(String, ForeignKey('open_house_host_requests.id', ondelete='CASCADE'), nullable=False, index=True)
    actor_id = Column(String, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    # requested | countered | counter_accepted | counter_declined | approved | declined | note
    action = Column(String, nullable=False)
    proposed_date = Column(Date, nullable=True)
    proposed_start_time = Column(Time, nullable=True)
    proposed_end_time = Column(Time, nullable=True)
    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    request = relationship("OpenHouseHostRequest")
    actor = relationship("User", foreign_keys=[actor_id])


class OpenHouseTeamMember(Base):
    __tablename__ = "open_house_team_members"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    member_agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    # pending -> accepted|declined. New invites start pending and need the invited agent's
    # acceptance; existing rows created before this column existed are grandfathered in as
    # 'accepted' via the column's server_default, so already-established teams keep working.
    status = Column(String, default="accepted", nullable=False, server_default="accepted", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    responded_at = Column(DateTime(timezone=True), nullable=True)

    owner = relationship("User", foreign_keys=[owner_id])
    member_agent = relationship("User", foreign_keys=[member_agent_id])

    __table_args__ = (
        UniqueConstraint('owner_id', 'member_agent_id', name='unique_team_owner_member'),
    )


class OpenHouseTeamBroadcast(Base):
    __tablename__ = "open_house_team_broadcasts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    listing_key = Column(String, nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    requested_date = Column(Date, nullable=False)
    # Null when is_anytime is set - the owner doesn't care what time, so nothing to store until
    # whoever accepts picks one (see respond_to_broadcast).
    requested_start_time = Column(Time, nullable=True)
    requested_end_time = Column(Time, nullable=True)
    message = Column(Text, nullable=True)
    is_anytime = Column(Boolean, default=False, nullable=False)

    status = Column(String, default="open", nullable=False, index=True)  # open, filled, cancelled

    # When set, this is a direct 1:1 ask to one specific OpenHousePal agent (Manager > Manage Hosts
    # "Direct Request") rather than a fan-out to the whole My Team - reuses the exact same
    # open/filled/cancelled + atomic-claim-or-counter machinery as a team broadcast (see
    # respond_to_broadcast) instead of a separate flow, and doesn't require the target to be a
    # team member. Null = a normal team-wide broadcast.
    target_agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    owner = relationship("User", foreign_keys=[owner_id])
    target_agent = relationship("User", foreign_keys=[target_agent_id])


class OpenHouseListingNote(Base):
    __tablename__ = "open_house_listing_notes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    listing_key = Column(String, nullable=False, unique=True, index=True)
    agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    notes = Column(Text, nullable=True)
    # "Agent-defined priority" override - lets the listing agent manually flag a listing as
    # high-priority regardless of what the computed priority logic says.
    is_priority_override = Column(Boolean, default=False, nullable=False)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    agent = relationship("User")


# ---------------------------------------------------------------------------
# Studio: shared Brand Kit, Design Studio, and Blogs
# ---------------------------------------------------------------------------

class StudioBrandKit(Base):
    __tablename__ = "studio_brand_kits"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)

    primary_color = Column(String, nullable=False, default="#111827")
    secondary_color = Column(String, nullable=False, default="#C9A24D")
    accent_color = Column(String, nullable=False, default="#816937")
    heading_font = Column(String, nullable=False, default="serif")
    body_font = Column(String, nullable=False, default="sans")

    personal_logo_url = Column(String, nullable=True)
    team_logo_url = Column(String, nullable=True)
    brokerage_logo_url = Column(String, nullable=True)
    disclaimer_text = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


class DesignProject(Base):
    __tablename__ = "design_projects"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    design_type = Column(String, nullable=False)  # e.g. 'just_listed_flyer', 'instagram_post'
    style_id = Column(String, nullable=False)  # visual style, e.g. 'modern_luxury' - renamed from template_id
    template_id = Column(String, nullable=True)  # layout/template variant chosen at creation (Situation+Format+Template)
    situation = Column(String, nullable=True, index=True)  # e.g. 'just_listed', 'open_house' - set once at creation
    color_mode = Column(String, nullable=True)  # 'light' | 'dark' | 'brand' - independent of style_id
    name = Column(String, nullable=False)
    status = Column(String, default="DRAFT", nullable=False)  # DRAFT, READY, EXPORTED, PUBLISHED, ARCHIVED

    linked_property = Column(JSONB, nullable=True)  # snapshot, same shape as AgentForm.linked_property
    blocks = Column(JSONB, nullable=False, default=list)  # ordered list of structured content blocks
    branding_mode = Column(String, default="personal", nullable=False)  # personal | team | brokerage | none

    campaign_id = Column(String, ForeignKey('form_campaigns.id', ondelete='SET NULL'), nullable=True, index=True)  # the real Form Campaign this design's QR/link tracks, if any

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


class ListingBlurb(Base):
    """The short "about this home" lines a Studio design prints (door hanger, flyer, postcard...),
    one per agent per listing. Written once by AI from the MLS remarks (cheap model, ~0.1 cent),
    then every later design for that home reuses it - and an agent's own edit replaces it, so the
    next design brings their wording over. `key` = the MLS listing key, else the normalized address."""
    __tablename__ = "listing_blurbs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    key = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    source = Column(String, nullable=False, default="ai")  # ai | agent (edited by the agent)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (UniqueConstraint('owner_id', 'key', name='uq_listing_blurbs_owner_key'),)


class StudioMediaAsset(Base):
    """One AI Property Media generation (or regeneration) - Studio's AutoReel-style tool. The
    source photo is only ever referenced by URL and never mutated; each run is a new row. All
    versions produced from the same (source photo + action) share `group_id`, which drives the
    history rail; `parent_asset_id` records the exact version a regenerate came from.

    Image actions (twilight/lights_on/virtual_staging/enhance) resolve synchronously via OpenAI
    gpt-image-2 and land as status="succeeded". `animate` is async: the row is created
    status="processing" with a Runway `provider_job_id`, then flipped by poll_asset() once
    Runway finishes. See app/services/studio_media_service.py."""
    __tablename__ = "studio_media_assets"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    group_id = Column(String, nullable=False, index=True)  # all versions from one (source photo + action)
    parent_asset_id = Column(String, ForeignKey('studio_media_assets.id', ondelete='SET NULL'), nullable=True)

    kind = Column(String, nullable=False)  # 'image' | 'video'
    action = Column(String, nullable=False)  # 'twilight' | 'lights_on' | 'virtual_staging' | 'enhance' | 'animate'
    status = Column(String, default="processing", nullable=False)  # 'processing' | 'succeeded' | 'failed'

    source_photo_url = Column(String, nullable=False)  # untouched original (MLS URL or uploaded studio-media/...)
    source_kind = Column(String, nullable=False)  # 'listing' | 'upload'
    linked_property = Column(JSONB, nullable=True)  # snapshot, same shape as DesignProject.linked_property; null for non-MLS tiers
    options = Column(JSONB, nullable=True)  # {room_type, style} / {enhancements: [...]} / {motion}

    provider = Column(String, nullable=True)  # 'openai' | 'runway'
    provider_model = Column(String, nullable=True)  # 'gpt-image-2' | 'gen4.5'
    provider_job_id = Column(String, nullable=True)  # Runway task id (video only)

    result_url = Column(String, nullable=True)  # S3 URL of the generated image/video
    error_message = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)  # exact prompt sent (audit/debug, mirrors cma_ai_service)
    prompt_version = Column(String, nullable=True)
    cost_usd = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


class StorageItem(Base):
    """One row per media file an agent uploads or generates across the whole app - AI Media,
    Design Studio, Blogs, brand logos, manual listing photos, agent/sale photos. The single
    source of truth for an agent's total stored bytes (see storage_ledger_service). Keyed by
    S3 URL so a delete anywhere can release it by URL."""
    __tablename__ = "storage_items"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    category = Column(String, nullable=False)  # ai_media_image | ai_media_video | ai_media_upload | design | blog | brand_logo | listing_photo | agent_photo | agent_sale_photo
    s3_url = Column(String, nullable=False, unique=True, index=True)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Blog(Base):
    __tablename__ = "blogs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    slug = Column(String, nullable=False)
    status = Column(String, default="DRAFT", nullable=False, index=True)  # DRAFT, SCHEDULED, PUBLISHED, UNPUBLISHED, ARCHIVED

    title = Column(String, nullable=False)
    subtitle = Column(String, nullable=True)
    cover_image_url = Column(String, nullable=True)
    category = Column(String, nullable=True)
    tags = Column(JSONB, nullable=False, default=list)
    theme = Column(String, nullable=True)  # the article's look: luxe (default when null), editorial, modern, midnight, luxe_plus (Luxe - IDX only)

    blocks = Column(JSONB, nullable=False, default=list)  # ordered list of structured content blocks
    linked_properties = Column(JSONB, nullable=False, default=list)  # list of property snapshots

    seo_title = Column(String, nullable=True)
    meta_description = Column(Text, nullable=True)
    focus_keyword = Column(String, nullable=True)
    social_title = Column(String, nullable=True)
    social_description = Column(Text, nullable=True)
    social_image_url = Column(String, nullable=True)
    is_indexed = Column(Boolean, default=True, nullable=False)

    agent_hub_visible = Column(Boolean, default=True, nullable=False)
    agent_hub_featured = Column(Boolean, default=False, nullable=False)
    idx_visible = Column(Boolean, default=True, nullable=False, server_default='true')  # shows on the agent's IDX website
    luxe_metal = Column(String, nullable=True)  # Luxe theme finish: gold (default) / silver / bronze
    accent_color = Column(String, nullable=True)  # Classic theme accent: the Agent Hub's Classic choices (null = Original gold)
    linked_areas = Column(JSONB, nullable=False, default=list, server_default='[]')  # IDX neighborhood pages the article links (area slugs) - see services/blog_neighborhoods.py
    use_signature_card = Column(Boolean, default=True, nullable=False, server_default='true')  # end the article with the agent's Email Signature card (when their plan has it) instead of the small contact card
    ai_review = Column(JSONB, nullable=True)  # AI pipeline's Verified report (ai_blog_pipeline_service) - private to the agent

    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('owner_id', 'slug', name='uq_blogs_owner_slug'),
    )

    owner = relationship("User")
    events = relationship("BlogEvent", back_populates="blog", cascade="all, delete-orphan")


class BlogEvent(Base):
    __tablename__ = "blog_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    blog_id = Column(String, ForeignKey('blogs.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    event_type = Column(String, nullable=False)  # view, link_click, property_click, contact_click, video_play, qr_scan, lead_submit
    meta = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    blog = relationship("Blog", back_populates="events")


# ---------------------------------------------------------------------------
# Website Builder: multi-tenant agent sites and custom domains. A separate,
# standalone system from Agent Hub (AgentHub above) - no shared code, no
# migration path between them.
# ---------------------------------------------------------------------------

class Site(Base):
    __tablename__ = "sites"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False, index=True)

    # The <slug> in <slug>.openhousepal.com - a global (not per-owner) namespace since it's a
    # real DNS name, unlike e.g. Blog.slug which only needs to be unique per owner.
    subdomain_slug = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, nullable=False, default="DRAFT")  # DRAFT | PUBLISHED - has this site ever gone live, not content freshness
    # The agent's own on/off switch (Settings -> IDX Website; asked right after an IDX purchase).
    # Off = the public IDX site isn't served at all and every generated link falls back to
    # openhousepal.com - for agents who already have a website of their own. Independent of
    # `status` so turning it back on restores the exact published site.
    is_enabled = Column(Boolean, nullable=False, default=True, server_default='true')

    # draft_data and published_snapshot share one shape ({"pages": [...], "theme": {...}}) so
    # publishing is a cheap copy, not a transform. The public renderer only ever reads
    # published_snapshot; the editor only ever reads/writes draft_data. See site_service.py.
    draft_data = Column(JSONB, nullable=False, default=dict)
    published_snapshot = Column(JSONB, nullable=False, default=dict)
    # Getting the site into Google/Bing (google_search_service): our Search Console code
    # (auto_code), the agent's own pasted code (manual_code), the host it was done for, and
    # verified_at / sitemap_submitted_at / indexnow_pinged_at / last_error. Live - no publish needed.
    google_search = Column(JSONB, nullable=False, default=dict, server_default='{}')

    published_at = Column(DateTime(timezone=True), nullable=True)
    # The old-links scan of the site that used to live on the agent's own domain (old_links_service):
    # {domain, status: running|done|failed, started_at, finished_at, found, matched, attempts, error}.
    old_links_scan = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="site")
    domains = relationship("SiteDomain", back_populates="site", cascade="all, delete-orphan")


class SiteRedirect(Base):
    """An address from the site that used to live on the agent's own domain (found in the Internet
    Archive / its old sitemap - Google still lists it) -> the page here that does the same job.
    Written by old_links_service's scan; the agent can change the target or turn one off on the
    Domain tab. source_path is normalized (lowercase, decoded, no trailing slash or query)."""
    __tablename__ = "site_redirects"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    site_id = Column(String, ForeignKey('sites.id', ondelete='CASCADE'), nullable=False, index=True)
    source_path = Column(String, nullable=False)
    target_path = Column(String, nullable=True)  # null = no good match found (agent can pick one)
    confidence = Column(Float, nullable=False, default=0.0)
    match_reason = Column(String, nullable=True)  # short plain-English why ("Same blog post", "Town page")
    is_enabled = Column(Boolean, nullable=False, default=True, server_default='true')
    agent_approved = Column(Boolean, nullable=False, default=False, server_default='false')  # the agent picked/confirmed it - a rescan never changes it
    last_seen_at = Column(DateTime(timezone=True), nullable=True)  # newest Archive capture of the old page
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('site_id', 'source_path', name='uq_site_redirects_site_source'),
    )


class SiteDomain(Base):
    __tablename__ = "site_domains"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    site_id = Column(String, ForeignKey('sites.id', ondelete='CASCADE'), nullable=False, index=True)
    # Denormalized alongside site_id (same pattern as BlogEvent.owner_id) so ownership checks
    # don't need a join back through Site.
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # Lowercase, no scheme, no trailing dot, e.g. "peterstefanatos.com". Apex and "www." are
    # separate rows, auto-linked as a pair (see domain_verification_service) once either one
    # verifies - DNS control of one implies control of the other's zone.
    hostname = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, nullable=False, default="PENDING")  # PENDING -> VERIFIED -> ACTIVE, or FAILED
    verification_token = Column(String, nullable=True)  # DNS TXT record value (secondary/manual verification path)
    is_primary = Column(Boolean, nullable=False, default=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)  # last DNS verification attempt, pass or fail
    last_error = Column(String, nullable=True)  # short, agent-safe reason for FAILED - never a raw resolver/ACME error

    # Set when this row is the non-primary half of an apex/www pair - it 301-redirects to the
    # target row's hostname instead of serving content directly. Self-referential, so deleting
    # the target clears this via ondelete=SET NULL rather than cascading.
    redirect_target_domain_id = Column(String, ForeignKey('site_domains.id', ondelete='SET NULL'), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    site = relationship("Site", back_populates="domains")
    redirect_target = relationship("SiteDomain", remote_side=[id])


class PurchasedDomain(Base):
    """A domain an agent bought inside OpenHousePal (domain_purchase_service). Registered at
    Name.com under our reseller account, in the agent's name; paid with a yearly PayPal
    subscription (first year = the setup fee, renewals = the yearly cycle). Its website hookup is
    ordinary site_domains rows, so everything after the purchase is the normal custom-domain path."""
    __tablename__ = "purchased_domains"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    domain_name = Column(String, nullable=False, index=True)  # bare, lowercase: janesmithhomes.com
    # CHECKOUT (waiting on PayPal) -> REGISTERING -> ACTIVE; FAILED (refunded) / ABANDONED
    # (never paid); ACTIVE ones that stop renewing keep status ACTIVE with auto_renew False
    # until expires_at, then EXPIRED. TRANSFERRED = moved to another company.
    status = Column(String, nullable=False, default="CHECKOUT", index=True)
    price_usd = Column(Float, nullable=False)          # what the agent paid for year one
    renewal_price_usd = Column(Float, nullable=False)  # what the agent pays each later year
    cost_usd = Column(Float, nullable=True)            # what Name.com charged us
    paypal_subscription_id = Column(String, nullable=True, unique=True, index=True)
    namecom_order_id = Column(String, nullable=True)
    auto_renew = Column(Boolean, nullable=False, default=True)
    registered_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_renewed_at = Column(DateTime(timezone=True), nullable=True)
    registrant = Column(JSONB, nullable=True)  # the contact we registered it with (agent's own)
    last_error = Column(String, nullable=True)  # short, agent-safe
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ---------------------------------------------------------------------------
# Single Property Websites: one standalone cinematic marketing site per listing,
# many per owner (unlike Site above, which is one whole-agent website). Publishes at its own
# <slug>.OHP_BASE_DOMAIN subdomain - see domain_resolution_service.resolve_property_hostname -
# and, once an owner has a verified custom domain on their Site, under a /listings/<slug> path
# there too. Deliberately reuses Site's draft/published-snapshot split (site_service.publish_site)
# rather than inventing a different publish model.
# ---------------------------------------------------------------------------

class PropertyWebsite(Base):
    __tablename__ = "property_websites"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # The real listing this was generated from, when it came from Bright MLS / My Listings - null
    # for a manually-entered property (mirrors AgentForm.linked_property's own manual-entry case).
    # Never re-joined against Property at render time: property_snapshot below is the single
    # source of truth a published site reads, so it keeps working even after the listing sells,
    # expires, or is removed from sync.
    listing_key = Column(String, nullable=True, index=True)

    template_id = Column(String, nullable=False, default="cinematic_luxury")

    # The <slug> in <slug>.OHP_BASE_DOMAIN - a global namespace shared with Site.subdomain_slug
    # (see property_website_service._unique_property_slug, which checks both tables), since both
    # are real DNS names under the same base domain and must never collide.
    slug = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, nullable=False, default="DRAFT")  # DRAFT | PUBLISHED

    # Wide snapshot of the source property at creation time - address/price/beds/baths/features/
    # photos/agent info/etc, far wider than the slim LinkedProperty shape used elsewhere, since the
    # cinematic template shows categorized features, school district, county, etc. Frozen at
    # creation; an agent can refresh it manually later if the underlying listing changed.
    property_snapshot = Column(JSONB, nullable=False, default=dict)

    # draft_data / published_snapshot share one shape (hero image, photo order, description
    # override, featured amenities, video asset id, floor plans, documents, agents, open house
    # overrides, colors, section visibility) - same cheap-copy-to-publish split as Site above.
    draft_data = Column(JSONB, nullable=False, default=dict)
    published_snapshot = Column(JSONB, nullable=False, default=dict)

    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")

    __table_args__ = (
        Index('ix_property_websites_owner_id_status', 'owner_id', 'status'),
    )


class PropertyWebsiteView(Base):
    """One row per public visit to a published Spotlight site - backs the visitor stats shown
    on the Listing Spotlight Pages list (unique visitors + total visits, per site and overall).

    visitor_id is the soft per-browser `ohp_vid` cookie (see public_nearby_shared.visitor_device_id),
    so "unique visitors" counts distinct browsers, not raw page loads. The agent viewing their own
    published site is kept out entirely - the frontend beacon never fires when the page was opened
    from Studio (an owner_ref marker persisted in that browser), so no owner row is ever written.
    Rows are deduped to roughly one per (site, visitor) per 30 min in property_website_service.record_view,
    so total_views reads as visits, not refreshes.
    """
    __tablename__ = "property_website_views"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    property_website_id = Column(String, ForeignKey('property_websites.id', ondelete='CASCADE'), nullable=False, index=True)
    visitor_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index('ix_property_website_views_site_visitor', 'property_website_id', 'visitor_id'),
    )


# ---------------------------------------------------------------------------
# Areas / Market Data: shared, tenant-free geography records (Villanova PA,
# Gladwyne PA, ...) researched once and reused across every agent who serves
# that town, rather than re-researched per agent. See
# app/services/areas/area_enrichment_service.py for how `facts` gets filled in
# and why every fact carries its own source/provenance instead of a bare value.
# ---------------------------------------------------------------------------

class MarketArea(Base):
    __tablename__ = "market_areas"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)  # e.g. "Villanova", or "Rittenhouse Square" for a neighborhood
    state = Column(String, nullable=False)  # 2-letter
    slug = Column(String, unique=True, index=True, nullable=False)  # e.g. "villanova-pa"
    # The real, DB-enforced deduplication identity - state + area_type + a deterministic local
    # normalization of name (+ normalized parent_area_name for neighborhoods). See
    # app.services.areas.canonical_key.compute_canonical_key - the UNIQUE constraint below on
    # this column (not on raw name+state) is what actually guarantees "Villanova"/"Villanova PA"/
    # "Villanova, Pennsylvania" can never become two rows, including under real concurrency (see
    # area_enrichment_service.get_or_create_market_area's INSERT ... ON CONFLICT).
    canonical_key = Column(String, nullable=False)
    county = Column(String, nullable=True)
    municipality_type = Column(String, nullable=True)  # e.g. "township" | "borough" | "CDP"
    # 'city' (default - a real MLS-recognized town/city, added via the city/town autocomplete) or
    # 'neighborhood' (a named market/neighborhood/region with no formal municipal boundary of its
    # own, added via "Add Market Area / Neighborhood" - e.g. Rittenhouse Square, Main Line,
    # Fishtown). Drives which listing-inventory geographic radius applies (see
    # content_adapters.get_area_inventory) and how the page frames the area (a neighborhood shows
    # its parent_area_name; a city doesn't need to).
    area_type = Column(String, nullable=False, server_default="city")
    # For area_type='neighborhood' only - the resolved containing city/town, e.g. "Philadelphia"
    # for Rittenhouse Square. Resolved once at creation time (see geocoding.resolve_named_place),
    # never guessed - null if resolution couldn't determine a parent.
    parent_area_name = Column(String, nullable=True)
    zip_codes = Column(JSONB, nullable=False, server_default='[]')

    # Drives every enrichment lookup (Places/NCES/Census) - set once by geocoding.geocode_place.
    centroid_lat = Column(Float, nullable=True)
    centroid_lng = Column(Float, nullable=True)
    # Shared definition used to scope live website inventory for non-city market areas.
    # Shape today is either:
    # {"type": "bounds", "bounds": {"min_lat", "max_lat", "min_lng", "max_lng"}, "source": ...}
    # or {"type": "radius", "center": {"lat", "lng"}, "radius_miles": n, "source": ...}.
    # Kept on MarketArea (not AgentMarketArea) so one canonical neighborhood definition is reused
    # by every agent and can later be upgraded to a true boundary without per-agent duplication.
    geography = Column(JSONB, nullable=False, server_default='{}')

    # Structured, source-tagged facts (school_districts, transportation, parks, downtown_areas,
    # amenities, photos, demographics, market_stats) - see area_enrichment_service for the exact
    # shape. Never AI-authored; a field the source can't confidently supply is omitted, not guessed.
    facts = Column(JSONB, nullable=False, server_default='{}')
    # The one AI-generated field on this whole record, deliberately isolated from `facts` -
    # {summary, source: "ai_generated", model, generated_at, grounded_fact_keys}. Written once by
    # area_enrichment_service from the already-verified facts, never touched by public requests.
    narrative = Column(JSONB, nullable=True)

    enrichment_status = Column(String, nullable=False, default="PENDING")  # PENDING | RESEARCHING | ENRICHED | PARTIAL | FAILED
    last_enriched_at = Column(DateTime(timezone=True), nullable=True)
    # Distinct from last_enriched_at (which moves on every enrich_area run, full or partial) - a
    # future lightweight re-verification pass (e.g. re-checking a business still exists) can bump
    # this alone without re-running the whole research pipeline. Null until that exists.
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    # Schema/pipeline version this row's `facts` were produced under - lets a future pipeline
    # change tell "researched, but under the old shape/prompts" apart from "confirmed current"
    # instead of treating every non-null `facts` blob as equally trustworthy forever.
    research_version = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        # NOT (name, state) - that would incorrectly forbid two genuinely different canonical
        # geographies that happen to share a literal name+state (e.g. two differently-parented
        # neighborhoods both named "Oak Grove" in the same state, or a city and a neighborhood
        # sharing a name) - canonical_key is the real identity; see compute_canonical_key.
        UniqueConstraint('canonical_key', name='uq_market_areas_canonical_key'),
    )

    agents = relationship("AgentMarketArea", back_populates="area", cascade="all, delete-orphan")


class SpotlightAlertSubscription(Base):
    """A guest who asked a Listing Spotlight page to "tell me about price changes and open
    houses". One row per (site, email). The daily check (spotlight_alerts_service) compares the
    live price / upcoming open houses with what this guest was last told and emails only real
    changes. `token` is the one-click unsubscribe key; unsubscribed rows are kept (never re-added
    by a repeat sign-up without the guest asking again)."""
    __tablename__ = "spotlight_alert_subscriptions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    property_website_id = Column(String, ForeignKey('property_websites.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    lead_id = Column(String, nullable=True)
    email = Column(String, nullable=False)
    name = Column(String, nullable=True)
    token = Column(String, nullable=False, unique=True, default=lambda: uuid.uuid4().hex)
    last_price = Column(Float, nullable=True)
    last_state = Column(JSONB, nullable=True)  # {"status", "open_houses": [ids]} as last told to this guest
    last_notified_at = Column(DateTime(timezone=True), nullable=True)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index('ux_spotlight_alert_site_email', 'property_website_id', 'email', unique=True),
    )


class AgentMarketArea(Base):
    """One agent's claim that they serve a given MarketArea - drives which community pages
    (/villanova-pa) exist on that agent's site. Never owns or duplicates the area's data."""
    __tablename__ = "agent_market_areas"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    area_id = Column(String, ForeignKey('market_areas.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('owner_id', 'area_id', name='uq_agent_market_areas_owner_area'),
    )

    owner = relationship("User")
    area = relationship("MarketArea", back_populates="agents")


class CmaReport(Base):
    """A Comparative Market Analysis. Deliberately JSONB-heavy (same shape as DesignProject.blocks
    / Blog.blocks) rather than a fully normalized comparable-properties table - the subject and
    each comparable carry a lot of agent-editable, evolving fields (quality ratings, strength,
    commentary, ordering) that don't need relational querying on their own, only as part of the
    whole report. The agent owns and edits the whole document as one unit."""
    __tablename__ = "cma_reports"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    client_name = Column(String, nullable=True)
    mode = Column(String, default="listing", nullable=False)  # listing | buyer
    status = Column(String, default="DRAFT", nullable=False, index=True)  # DRAFT, READY, SHARED

    subject = Column(JSONB, nullable=False, default=dict)  # subject property snapshot, agent-editable
    comparables = Column(JSONB, nullable=False, default=list)  # ordered list of comp snapshots + ratings/strength/commentary
    pricing = Column(JSONB, nullable=False, default=dict)  # value range, pricing commentary, buyer-offer fields
    market_context = Column(JSONB, nullable=False, default=dict)
    improvements = Column(JSONB, nullable=False, default=list)
    presentation = Column(JSONB, nullable=False, default=dict)  # hidden sections/comps, cover photo override

    # Bookkeeping for the last AI-generated analysis (see cma_ai_service.py) - model, cost,
    # input_fingerprint (lets a re-open detect "nothing changed" and skip re-billing), which
    # images were sent. The AI's actual output lives inline on each comparable
    # (`ai_commentary`) and on `pricing` (`ai_estimate`) above, following the same
    # never-overwrite-the-agent's-own-work pattern as everything else on this report - this
    # column is metadata only, not a duplicate copy of the content.
    ai_analysis = Column(JSONB, nullable=True)

    # In-flight "Create AI Analysis" / recompute tracking - lets a GET (list or detail) tell the
    # difference between "no analysis yet" and "an analysis is running right now in another
    # request", so a client that navigated away mid-generation and comes back (or never left, on
    # the list page) can show the generating state again instead of looking idle. Set at the start
    # of the request, cleared at the end - see cma_service.mark_generation_started/_finished.
    generation_status = Column(String, nullable=True, index=True)  # None | 'generating' | 'complete' | 'failed'
    generation_started_at = Column(DateTime(timezone=True), nullable=True)
    # Why the last AI run didn't finish, in plain words (a usage limit, an error) - shown on the page.
    generation_error = Column(String, nullable=True)

    # Public share link - opaque slug, distinct from the DB id so a shared link can't be used to
    # probe/enumerate reports. Nullable until the agent first shares. The agent keeps editing the
    # same report after sharing (no snapshot/freeze), so the link always reflects the latest state.
    share_slug = Column(String, unique=True, index=True, nullable=True)
    # Web version of the shared report ends with the agent's Email Signature card (plans with
    # signatures, when one is saved) - the PDF keeps its own footer. On by default.
    use_signature_card = Column(Boolean, default=True, nullable=False, server_default='true')
    # Made by the admin CMA accuracy test (cma_backtest_service), never by an agent: excluded from
    # every report list, never shareable, removed in bulk from the admin page.
    is_backtest = Column(Boolean, default=False, nullable=False, server_default='false', index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


class CmaBacktestBatch(Base):
    """One admin CMA accuracy test: real past sales re-run through the normal CMA flow as of the day
    before each went under contract (see cma_backtest_service / cma_time_machine). `items` holds one
    entry per home - its hidden truth, the as-of facts used, the generated report id and the
    scoreboard numbers - updated in place as the queue works through them."""
    __tablename__ = "cma_backtest_batches"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    status = Column(String, nullable=False, default="queued")  # queued | running | done | stopped
    settings = Column(JSONB, nullable=False, default=dict)
    items = Column(JSONB, nullable=False, default=list)
    estimated_cost_usd = Column(Float, nullable=True)
    actual_cost_usd = Column(Float, nullable=True)
    error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class CmaBacktestArm(Base):
    """'The next CMA this test agent builds for this sold listing is an accuracy-test run as of
    facts["as_of"].' Created only by the admin test runner; the server's address search, listing
    lookup, comp search and AI generation honour it for that one owner + listing (see
    cma_backtest_arms). `facts` holds the as-of view AND the hidden truth (for the leak scan only)."""
    __tablename__ = "cma_backtest_arms"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    batch_id = Column(String, ForeignKey('cma_backtest_batches.id', ondelete='CASCADE'), nullable=True)
    item_index = Column(Integer, nullable=True)
    listing_key = Column(String, nullable=False)
    address_key = Column(String, nullable=True)
    facts = Column(JSONB, nullable=False, default=dict)
    dry_run = Column(Boolean, nullable=False, default=False, server_default='false')
    report_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class SellerNetSheet(Base):
    """Studio -> Tools -> Financial Tools -> Seller Net Sheet. Same JSONB-document shape as
    CmaReport - line items (mortgage payoffs, closing costs, repairs) are agent-editable freeform
    lists rather than a rigid column per possible cost, since which costs apply varies by deal."""
    __tablename__ = "seller_net_sheets"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    client_name = Column(String, nullable=True)
    status = Column(String, default="DRAFT", nullable=False, index=True)  # DRAFT, READY, SHARED

    property = Column(JSONB, nullable=False, default=dict)  # subject property snapshot, agent-editable
    scenarios = Column(JSONB, nullable=False, default=list)  # [{id, label, sale_price}]
    mortgage_payoffs = Column(JSONB, nullable=False, default=list)  # [{id, label, amount}]
    compensation = Column(JSONB, nullable=False, default=dict)  # listing/buyer side pct+flat
    closing_costs = Column(JSONB, nullable=False, default=list)  # [{id, label, amount}]
    tax_proration = Column(JSONB, nullable=False, default=dict)  # {amount}
    repairs = Column(JSONB, nullable=False, default=list)  # [{id, label, amount}]
    notes = Column(String, nullable=True)
    disclaimer = Column(String, nullable=True)

    share_slug = Column(String, unique=True, index=True, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


class AIConversation(Base):
    """A chat thread between an agent and the AI Assistant. See ai_orchestrator_service.py."""
    __tablename__ = "ai_conversations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    title = Column(String, nullable=True)  # auto-generated from the first user message
    archived_at = Column(DateTime(timezone=True), nullable=True)  # soft-delete - DELETE route sets this, never destroys rows

    # Auto-compaction (see ai_compaction_service.py). `summary` folds in everything with
    # created_at <= summary_covers_through so history replay doesn't grow unbounded; raw
    # messages after that point still get replayed as-is.
    summary = Column(Text, nullable=True)
    summary_covers_through = Column(DateTime(timezone=True), nullable=True)
    compaction_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_message_at = Column(DateTime(timezone=True), nullable=True)

    owner = relationship("User")
    messages = relationship("AIMessage", back_populates="conversation", cascade="all, delete-orphan", order_by="AIMessage.created_at.asc()")


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey('ai_conversations.id', ondelete='CASCADE'), nullable=False, index=True)

    role = Column(String, nullable=False)  # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    # Which tools/records were used to produce an assistant reply - drives the "why" transparency
    # links in the UI. Never contains raw tool_use ids or model-internal state, only
    # [{tool, record_type, record_id, label}, ...].
    tool_calls = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversation = relationship("AIConversation", back_populates="messages")


class AIUsageEvent(Base):
    """One row per AI request attempt (allowed or blocked) - the security/audit log and the
    source of truth for burst/daily/monthly/cost-ceiling enforcement in ai_rate_limit_service.py.
    Never stores message content - that lives only in AIMessage."""
    __tablename__ = "ai_usage_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    conversation_id = Column(String, ForeignKey('ai_conversations.id', ondelete='SET NULL'), nullable=True)

    model = Column(String, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Float, nullable=True)
    tool_call_count = Column(Integer, nullable=False, default=0)
    tool_names = Column(JSONB, nullable=True)  # list of tool names called this turn, e.g. ["search_leads"] - metadata only, never the question/answer text

    blocked = Column(Boolean, nullable=False, default=False)
    block_reason = Column(String, nullable=True)  # e.g. 'burst_limit' | 'daily_limit' | 'monthly_limit' | 'cost_ceiling' | 'input_too_long' | 'timeout' | 'iteration_limit'

    # Which AI feature spent this money - 'chat' (Claude), 'tts' (Read Aloud), or 'realtime'
    # (Talk to OHP). Each has its own independent monthly cost ceiling (ai_config_service.py),
    # so this is what lets ai_rate_limit_service.monthly_cost_so_far() check one without being
    # tripped up by spend from the others, while the admin dashboard still sums across all of
    # them for a per-agent total.
    event_type = Column(String, nullable=False, default="chat", server_default="chat")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    owner = relationship("User")


class UsageBudgetCounter(Base):
    """Persistent, atomically-incremented spend/count counter for external metered APIs -
    the shared mechanism behind every day/month budget cap that must survive a backend
    restart and stay correct across multiple worker processes (see usage_budget_service.py).

    One row per (bucket, period_type, period_key) - e.g. bucket='places_search',
    period_type='day', period_key='2026-08-16'. `bucket` also covers cross-feature
    aggregate budgets that aren't tied to one external provider call, e.g.
    'chat_global'/'area_research_global' (one feature, all users combined) and
    'platform_emergency' (every paid AI/API feature combined) - see usage_budget_service's
    RESERVATION_BUCKETS-style callers in ai_rate_limit_service.py, ai_realtime_service.py,
    and area_enrichment_service.py.

    cost_micros is an integer (1,000,000 micros = $1.00), not a float, so repeated
    increments across a busy day never accumulate floating-point drift in a number a
    circuit breaker is actively comparing against a hard limit."""
    __tablename__ = "usage_budget_counters"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    bucket = Column(String, nullable=False)
    period_type = Column(String, nullable=False)  # 'day' | 'month'
    period_key = Column(String, nullable=False)  # '2026-08-16' | '2026-08'
    count = Column(Integer, nullable=False, default=0, server_default="0")
    cost_micros = Column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("bucket", "period_type", "period_key", name="uq_usage_budget_counter"),
    )


class ExploreNearbyCache(Base):
    """Persistent cache for the public Explore Nearby feature (nearby_amenities_service.py) -
    the actual lever for cutting Google Places spend on it, not just a rate limit. The same
    property/area + category combination (or the same specific place's rating/review count) gets
    asked about by many different visitors over the following days, and none of that data changes
    fast enough to justify a fresh paid call every single time.

    cache_key is either "search:{lat}:{lng}:{category}:{radius}" (lat/lng rounded to the same
    ~11m precision already used for in-flight request de-duplication - close enough that a cache
    hit still returns accurate-enough distances for the exact requesting property) or
    "details:{place_id}". payload is the full serialized response, returned as-is on a hit.

    TTL is enforced by the caller comparing fetched_at against
    ai_config_service's explore_nearby_cache_ttl_days - not encoded here, so it can be tuned
    without a schema change. A stale row is simply overwritten (upsert on cache_key) the next
    time that key is requested past its TTL, rather than swept/deleted separately."""
    __tablename__ = "explore_nearby_cache"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    cache_key = Column(String, nullable=False, unique=True, index=True)
    payload = Column(JSONB, nullable=False)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SharedKnowledge(Base):
    """Researched, sourced facts that aren't about one town - e.g. scope "state:PA" holds
    Pennsylvania-wide facts (realty transfer tax, PHFA programs, disclosure rules, the latest
    Freddie Mac rate). Researched once, reused by every agent's articles (and any other product)
    for free until each item's topic shelf life runs out - see town_knowledge_service. Town-level
    facts live on MarketArea.facts["evidence_bank"] instead."""
    __tablename__ = "shared_knowledge"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    scope = Column(String, nullable=False, unique=True, index=True)
    items = Column(JSONB, nullable=False, default=list)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AIRealtimeSession(Base):
    """One row per Talk to OHP (OpenAI Realtime) voice session. The browser connects directly
    to OpenAI over WebRTC using an ephemeral client secret minted by ai_realtime_service.py, so
    this backend is not in the live audio path - this row (concurrent-session counting, running
    cost, heartbeats) is how usage limits and tenant isolation are still enforced entirely
    server-side despite that. See ai_realtime_service.py for the full session lifecycle."""
    __tablename__ = "ai_realtime_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_heartbeat_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    end_reason = Column(String, nullable=True)  # 'client_ended' | 'max_duration' | 'idle_timeout' | 'cost_ceiling' | 'abandoned'

    tool_call_count = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Float, nullable=False, default=0.0)

    owner = relationship("User")


class BuyerCashNeeded(Base):
    """Studio -> Tools -> Financial Tools -> Buyer Cash Needed. Same JSONB-document shape as
    CmaReport/SellerNetSheet."""
    __tablename__ = "buyer_cash_needed_sheets"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    client_name = Column(String, nullable=True)
    status = Column(String, default="DRAFT", nullable=False, index=True)  # DRAFT, READY, SHARED

    property = Column(JSONB, nullable=False, default=dict)  # subject property snapshot, agent-editable
    purchase_price = Column(JSONB, nullable=False, default=dict)  # {amount}
    down_payment_scenarios = Column(JSONB, nullable=False, default=list)  # [{id, label, percent}]
    loan_type = Column(String, default="conventional", nullable=False)
    closing_costs = Column(JSONB, nullable=False, default=list)  # [{id, label, amount}]
    prepaid_expenses = Column(JSONB, nullable=False, default=list)  # [{id, label, amount}]
    inspections = Column(JSONB, nullable=False, default=list)  # [{id, label, amount, enabled}]
    earnest_money = Column(JSONB, nullable=False, default=dict)  # {amount}
    credits = Column(JSONB, nullable=False, default=dict)  # {seller_credit, lender_credit}
    monthly_context = Column(JSONB, nullable=False, default=dict)  # optional secondary P&I estimate
    notes = Column(String, nullable=True)
    disclaimer = Column(String, nullable=True)

    share_slug = Column(String, unique=True, index=True, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")



# ---------------------------------------------------------------------------------------------
# Invites (Forms & Invites -> Invites): an agent's invitation to an open house, a broker's open,
# a client event / dinner, or anything custom - a designed invitation page at /i/<code> (or on the
# agent's own IDX domain) where guests RSVP. Client RSVPs become Leads (source_type 'invite_rsvp');
# agent RSVPs (broker's opens) never do. See app/services/invites_service.py.
# ---------------------------------------------------------------------------------------------
class EventInvite(Base):
    __tablename__ = "event_invites"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    code = Column(String(16), nullable=False, unique=True, index=True)  # the short public link: /i/<code>
    invite_type = Column(String(24), nullable=False)  # open_house | brokers_open | client_event | custom
    audience = Column(String(12), nullable=False, default='clients')  # clients (RSVPs -> Leads) | agents (never Leads)
    title = Column(String(140), nullable=False)
    message = Column(Text, nullable=True)  # the host's note on the invitation
    event_date = Column(Date, nullable=True)
    start_time = Column(String(5), nullable=True)  # 'HH:MM' Eastern wall clock (like tours / open houses)
    end_time = Column(String(5), nullable=True)
    place_name = Column(String(140), nullable=True)  # "The Smith residence", "Vetri Cucina"
    address = Column(String(300), nullable=True)
    linked_property = Column(JSONB, nullable=True)  # snapshot: {listing_key, address, city, state, zipcode, price, beds, baths, sqft, image, photos}
    cover_image_url = Column(String(1000), nullable=True)
    design = Column(String(24), nullable=False, default='classic')  # classic | garden | midnight | modern
    accent_color = Column(String(9), nullable=True)
    questions = Column(JSONB, nullable=False, default=list)  # same shape as AgentForm.questions (+ optional 'role')
    status = Column(String(12), nullable=False, default='live')  # live | closed (RSVPs closed, page still opens)
    view_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    rsvps = relationship("EventInviteRsvp", back_populates="invite", cascade="all, delete-orphan", passive_deletes=True)


class EventInviteRsvp(Base):
    __tablename__ = "event_invite_rsvps"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    invite_id = Column(String, ForeignKey('event_invites.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    # The guest's private key to come back and change their answer (kept in their browser and in
    # their confirmation email's link) - never shown to anyone else.
    token = Column(String(48), nullable=False, unique=True, index=True)
    response = Column(String(8), nullable=False)  # yes | maybe | no
    name = Column(String(140), nullable=True)
    email = Column(String(200), nullable=True)
    phone = Column(String(40), nullable=True)
    guest_count = Column(Integer, nullable=False, default=1)  # people coming, them included (0 when 'no')
    answers = Column(JSONB, nullable=False, default=list)  # [{questionId, label, type, value}]
    lead_id = Column(String, ForeignKey('leads.id', ondelete='SET NULL'), nullable=True)
    is_seen = Column(Boolean, nullable=False, default=False)  # the agent has looked at it
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    invite = relationship("EventInvite", back_populates="rsvps")


class EventInviteRecipient(Base):
    """Someone the agent emailed the invitation to (Share -> Email a list), so "Resend" can nudge
    only the people who haven't answered yet."""
    __tablename__ = "event_invite_recipients"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    invite_id = Column(String, ForeignKey('event_invites.id', ondelete='CASCADE'), nullable=False, index=True)
    email = Column(String(200), nullable=False)
    name = Column(String(140), nullable=True)
    send_count = Column(Integer, nullable=False, default=1)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('invite_id', 'email', name='uq_event_invite_recipients_invite_email'),
    )
