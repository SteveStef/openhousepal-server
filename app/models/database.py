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
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    collections = relationship("Collection", back_populates="owner")

class Collection(Base):
    __tablename__ = "collections"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(String, ForeignKey('users.id'), nullable=True)  # Allow null for anonymous collections
    share_token = Column(String, unique=True, nullable=True)
    is_public = Column(Boolean, default=False)
    
    # Anonymous visitor info (for open house collections)
    visitor_email = Column(String, nullable=True)
    visitor_name = Column(String, nullable=True)
    visitor_phone = Column(String, nullable=True)
    original_property_id = Column(String, ForeignKey('properties.id'), nullable=True)
    preferences = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    owner = relationship("User", back_populates="collections")
    properties = relationship("Property", secondary=collection_properties, back_populates="collections")
    original_property = relationship("Property", foreign_keys=[original_property_id])

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
    living_area = Column(Integer, nullable=True)  # Square footage
    lot_size = Column(Integer, nullable=True)
    year_built = Column(Integer, nullable=True)
    home_type = Column(String, nullable=True)
    home_status = Column(String, nullable=True)
    
    # Location
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    
    # Images and media
    img_src = Column(String, nullable=True)
    original_photos = Column(JSON, nullable=True)
    
    # Property features
    has_garage = Column(Boolean, nullable=True)
    has_pool = Column(Boolean, nullable=True)
    has_fireplace = Column(Boolean, nullable=True)
    parking_capacity = Column(Integer, nullable=True)
    
    # Financial info
    tax_assessed_value = Column(Integer, nullable=True)
    property_tax_rate = Column(Float, nullable=True)
    hoa_fee = Column(Float, nullable=True)
    
    # Market info
    days_on_zillow = Column(Integer, nullable=True)
    price_change = Column(Integer, nullable=True)
    date_price_changed = Column(Integer, nullable=True)  # Unix timestamp
    
    # External API data cache
    zillow_data = Column(JSON, nullable=True)  # Store full Zillow API response
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_synced = Column(DateTime(timezone=True), nullable=True)  # When data was last fetched from API
    
    # Relationships
    collections = relationship("Collection", secondary=collection_properties, back_populates="properties")


class OpenHouseEvent(Base):
    __tablename__ = "open_house_events"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    qr_code = Column(String, unique=True, nullable=False)
    property_id = Column(String, ForeignKey('properties.id'), nullable=False)
    agent_id = Column(String, ForeignKey('users.id'), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True)
    form_url = Column(String, nullable=True)  # Store the form link
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    property = relationship("Property")
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
    
    # Property Context
    property_id = Column(String, ForeignKey('properties.id'), nullable=True)
    qr_code = Column(String, nullable=False)
    form_url = Column(String, nullable=True)  # Store the form link
    
    # Preferences
    additional_comments = Column(Text, nullable=True)
    interested_in_similar = Column(Boolean, default=False)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    property = relationship("Property")


class PropertyInteraction(Base):
    __tablename__ = "property_interactions"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = Column(String, ForeignKey('collections.id'), nullable=False)
    property_id = Column(String, ForeignKey('properties.id'), nullable=False)
    user_id = Column(String, ForeignKey('users.id'), nullable=True)  # Null for anonymous interactions
    visitor_email = Column(String, nullable=True)  # For anonymous visitors
    
    # Interaction types
    liked = Column(Boolean, default=False)
    disliked = Column(Boolean, default=False)
    favorited = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    collection = relationship("Collection")
    property = relationship("Property")
    user = relationship("User")
    
    # Ensure one interaction per user/visitor per property in a collection
    __table_args__ = (
        # For authenticated users
        Index('idx_property_interaction_user', 'collection_id', 'property_id', 'user_id', unique=True, 
              postgresql_where=Column('user_id').isnot(None)),
        # For anonymous visitors
        Index('idx_property_interaction_visitor', 'collection_id', 'property_id', 'visitor_email', unique=True,
              postgresql_where=Column('visitor_email').isnot(None)),
    )


class PropertyComment(Base):
    __tablename__ = "property_comments"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = Column(String, ForeignKey('collections.id'), nullable=False)
    property_id = Column(String, ForeignKey('properties.id'), nullable=False)
    user_id = Column(String, ForeignKey('users.id'), nullable=True)  # Null for anonymous comments
    visitor_email = Column(String, nullable=True)  # For anonymous visitors
    visitor_name = Column(String, nullable=True)   # For anonymous visitors
    
    content = Column(Text, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    collection = relationship("Collection")
    property = relationship("Property")
    user = relationship("User")
