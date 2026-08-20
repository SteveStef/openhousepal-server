from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, Text, ForeignKey, Table, Index, DateTime, Date, Time, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
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

# Association table for many-to-many relationship between collections and properties
collection_properties = Table(
    'collection_properties',
    Base.metadata,
    Column('collection_id', String, ForeignKey('collections.id'), primary_key=True),
    Column('property_id', String, ForeignKey('properties.id'), primary_key=True),
    Column('added_at', DateTime(timezone=True), nullable=True)  # NULL = initial property (no NEW badge)
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
    mls_id = Column(String, unique=True, index=True, nullable=False)  # Agent's MLS ID
    broker_authorized = Column(Boolean, default=False, nullable=False)
    photo_url = Column(String, nullable=True)  # Uploaded headshot, served from /uploads
    professional_title = Column(String, nullable=True)  # e.g. "Real Estate Agent" - shown on Agent Hub + public forms
    market = Column(String, nullable=True)  # e.g. "Philly Main Line & Suburbs" - shown on Agent Hub + public forms
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def is_admin(self) -> bool:
        return self.email == os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")

    # PayPal subscription fields
    subscription_id = Column(String, nullable=True, unique=True, index=True)  # PayPal subscription ID (unique to prevent hijacking)
    subscription_status = Column(String, default="TRIAL")  # TRIAL, ACTIVE, SUSPENDED, CANCELLED, EXPIRED
    plan_id = Column(String, nullable=True)  # PayPal plan ID (P-50796747... or P-4KN61644...)
    plan_tier = Column(String, nullable=True)  # BASIC or PREMIUM
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    subscription_started_at = Column(DateTime(timezone=True), nullable=True)
    last_billing_date = Column(DateTime(timezone=True), nullable=True)
    next_billing_date = Column(DateTime(timezone=True), nullable=True)  # For grace period after cancellation
    last_paypal_sync = Column(DateTime(timezone=True), nullable=True)  # Track when last synced with PayPal

    # Relationships
    collections = relationship("Collection", back_populates="owner")
    notifications = relationship("Notification", back_populates="agent")
    discovery_preferences = relationship("DiscoveryPreferences", back_populates="user", uselist=False, cascade="all, delete-orphan")
    agent_hub = relationship("AgentHub", back_populates="owner", uselist=False, cascade="all, delete-orphan")
    agent_forms = relationship("AgentForm", back_populates="owner", cascade="all, delete-orphan")
    site = relationship("Site", back_populates="owner", uselist=False, cascade="all, delete-orphan")

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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

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
    open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)
    collection_id = Column(String, ForeignKey('collections.id', ondelete='SET NULL'), nullable=True)
    form_id = Column(String, ForeignKey('agent_forms.id'), nullable=True)
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
    is_public = Column(Boolean, default=True)
    status = Column(String, default="ACTIVE", nullable=False)  # ACTIVE, INACTIVE
    
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

    # Relationships
    collections = relationship("Collection", secondary=collection_properties, back_populates="properties")

    __table_args__ = (
        Index('ix_properties_city_state', 'city', 'state'),
        Index('ix_properties_lat_long', 'latitude', 'longitude'),
        Index('ix_properties_status_price', 'home_status', 'price'),
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

class ScheduledEmail(Base):
    __tablename__ = "scheduled_emails"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    template_name = Column(String, nullable=False)
    template_variables = Column(JSONB, nullable=False)
    
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


class OpenHouseTeamMember(Base):
    __tablename__ = "open_house_team_members"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    member_agent_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    requested_start_time = Column(Time, nullable=False)
    requested_end_time = Column(Time, nullable=False)
    message = Column(Text, nullable=True)

    status = Column(String, default="open", nullable=False, index=True)  # open, filled, cancelled

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    owner = relationship("User", foreign_keys=[owner_id])


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
    template_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    status = Column(String, default="DRAFT", nullable=False)  # DRAFT, READY, EXPORTED, PUBLISHED, ARCHIVED

    linked_property = Column(JSONB, nullable=True)  # snapshot, same shape as AgentForm.linked_property
    blocks = Column(JSONB, nullable=False, default=list)  # ordered list of structured content blocks
    branding_mode = Column(String, default="personal", nullable=False)  # personal | team | brokerage | none

    campaign_id = Column(String, nullable=True, index=True)  # groups co-generated campaign assets

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


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

    # draft_data and published_snapshot share one shape ({"pages": [...], "theme": {...}}) so
    # publishing is a cheap copy, not a transform. The public renderer only ever reads
    # published_snapshot; the editor only ever reads/writes draft_data. See site_service.py.
    draft_data = Column(JSONB, nullable=False, default=dict)
    published_snapshot = Column(JSONB, nullable=False, default=dict)

    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="site")
    domains = relationship("SiteDomain", back_populates="site", cascade="all, delete-orphan")


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

    # Public share link - opaque slug, distinct from the DB id so a shared link can't be used to
    # probe/enumerate reports. Nullable until the agent first shares. The agent keeps editing the
    # same report after sharing (no snapshot/freeze), so the link always reflects the latest state.
    share_slug = Column(String, unique=True, index=True, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User")


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

