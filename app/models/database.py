from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Table, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
from typing import Optional
import uuid

# Association table for many-to-many relationship between collections and properties
collection_properties = Table(
    'collection_properties',
    Base.metadata,
    Column('collection_id', String, ForeignKey('collections.id'), primary_key=True),
    Column('property_id', String, ForeignKey('properties.id'), primary_key=True)
)

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    state = Column(String, nullable=True)  # Agent's state
    brokerage = Column(String, nullable=True)  # Agent's brokerage
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    collections = relationship("Collection", back_populates="owner")

class Collection(Base):
    __tablename__ = "collections"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(String, ForeignKey('users.id'), nullable=True)  # Required - agent who owns the collection
    share_token = Column(String, unique=True, nullable=True)
    is_public = Column(Boolean, default=True)
    status = Column(String, default="ACTIVE")  # ACTIVE, INACTIVE
    
    # Anonymous visitor info (for open house collections)
    visitor_email = Column(String, nullable=True)
    visitor_name = Column(String, nullable=True)
    visitor_phone = Column(String, nullable=True)
    original_open_house_event_id = Column(String, ForeignKey('open_house_events.id'), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    owner = relationship("User", back_populates="collections")
    properties = relationship("Property", secondary=collection_properties, back_populates="collections")
    original_open_house_event = relationship("OpenHouseEvent", foreign_keys=[original_open_house_event_id])
    preferences = relationship("CollectionPreferences", back_populates="collection", uselist=False, cascade="all, delete-orphan")
    property_interactions = relationship("PropertyInteraction", back_populates="collection", cascade="all, delete-orphan")
    property_comments = relationship("PropertyComment", back_populates="collection", cascade="all, delete-orphan")

class Property(Base):
    __tablename__ = "properties"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    zpid = Column(Integer, unique=True, index=True, nullable=True)  # Zillow Property ID
    
    # Basic property info
    street_address = Column(String, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    zipcode = Column(String, nullable=True)
    country = Column(String, default="US")
    
    # Property details
    price = Column(Integer, nullable=True)
    zestimate = Column(Integer, nullable=True)
    bedrooms = Column(Integer, nullable=True)
    bathrooms = Column(Float, nullable=True)
    living_area = Column(Integer, nullable=True)
    lot_size = Column(Integer, nullable=True)
    home_type = Column(String, nullable=True)
    home_status = Column(String, nullable=True)
    
    # Location
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    
    img_src = Column(String, nullable=True)
    
    # Property details caching
    detailed_property= Column(JSON, nullable=True)  # Store detailed Zillow API response
    detailed_data_cached = Column(Boolean, default=False)
    detailed_data_cached_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    collections = relationship("Collection", secondary=collection_properties, back_populates="properties")


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
    
    # Property metadata (replaces property_id relationship)
    address = Column(String, nullable=True)
    abbreviated_address = Column(String, nullable=True)
    house_type = Column(String, nullable=True)
    lot_size = Column(Integer, nullable=True)
    
    # Location data for collection searching
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    zipcode = Column(String, nullable=True)
    
    # Property details for PDF generation and collection preferences
    bedrooms = Column(Integer, nullable=True)
    bathrooms = Column(Float, nullable=True)
    price = Column(Integer, nullable=True)
    home_status = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    agent = relationship("User")


class OpenHouseVisitor(Base):
    __tablename__ = "open_house_visitors"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    full_name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    
    # Visit Information
    visiting_reason = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    has_agent = Column(String, nullable=False)  # YES, NO, LOOKING
    
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
    favorited = Column(Boolean, default=False)
    
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
    
    # Location criteria
    lat = Column(Float, nullable=True)
    long = Column(Float, nullable=True)
    address = Column(String, nullable=True)
    cities = Column(JSON, nullable=True)
    townships = Column(JSON, nullable=True)
    diameter = Column(Float, default=2.0)  # Search diameter in miles
    
    # Additional features
    special_features = Column(Text, default="")
    
    # Home type preferences
    is_town_house = Column(Boolean, nullable=True, default=False)
    is_lot_land = Column(Boolean, nullable=True, default=False)
    is_condo = Column(Boolean, nullable=True, default=False)
    is_multi_family = Column(Boolean, nullable=True, default=False)
    is_single_family = Column(Boolean, nullable=True, default=False)
    is_apartment = Column(Boolean, nullable=True, default=False)
    
    # Visitor form data
    timeframe = Column(String, nullable=True)  # IMMEDIATELY, 1_3_MONTHS, 3_6_MONTHS, etc.
    visiting_reason = Column(String, nullable=True)  # BUYING_SOON, BROWSING, NEIGHBORHOOD, etc.
    has_agent = Column(String, nullable=True)  # YES, NO, LOOKING
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    collection = relationship("Collection", back_populates="preferences")
