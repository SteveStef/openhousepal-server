from sqlalchemy import Column, Integer, String, Float, Boolean, Text, ForeignKey, Table, Index, DateTime, Date, Time, UniqueConstraint
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

class AgentHub(Base):
    __tablename__ = "agent_hubs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
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

class AgentHubEvent(Base):
    __tablename__ = "agent_hub_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    event_type = Column(String, nullable=False)  # 'visit' | 'qr_scan'
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_synced_at = Column(DateTime(timezone=True), nullable=True)  # Track when properties were last synced
    last_visitor_activity_at = Column(DateTime(timezone=True), nullable=True)  # Track when a visitor actually interacts with the collection
    last_agent_dismissed_at = Column(DateTime(timezone=True), nullable=True)   # Track when the agent last "dismissed" new listings

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
    cheat_sheet_photos = Column(JSONB, nullable=True)  # Agent-selected photos for the Open House Cheat Sheet PDF
    
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
    notes = Column(Text, nullable=True)

    # Open House Context
    open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)
    qr_code = Column(String, nullable=False)
    form_url = Column(String, nullable=True)  # Store the form link

    interested_in_similar = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    open_house_event = relationship("OpenHouseEvent")


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
    diameter = Column(Float, nullable=True)  # Search diameter in miles

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

