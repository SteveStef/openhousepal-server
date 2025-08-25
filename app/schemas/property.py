from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

class PropertyBase(BaseModel):
    zpid: Optional[int] = None
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    country: str = "US"

class PropertyCreate(PropertyBase):
    price: Optional[int] = None
    zestimate: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    lot_size: Optional[int] = None
    year_built: Optional[int] = None
    home_type: Optional[str] = None
    home_status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    img_src: Optional[str] = None
    zillow_data: Optional[Dict[str, Any]] = None

class PropertyUpdate(BaseModel):
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[int] = None
    zestimate: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    lot_size: Optional[int] = None
    year_built: Optional[int] = None
    home_type: Optional[str] = None
    home_status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    img_src: Optional[str] = None

class PropertySummary(PropertyBase):
    id: str
    price: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    img_src: Optional[str] = None
    
    class Config:
        from_attributes = True

class Property(PropertyBase):
    id: str
    price: Optional[int] = None
    zestimate: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    lot_size: Optional[int] = None
    year_built: Optional[int] = None
    home_type: Optional[str] = None
    home_status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    img_src: Optional[str] = None
    original_photos: Optional[Dict[str, Any]] = None
    has_garage: Optional[bool] = None
    has_pool: Optional[bool] = None
    has_fireplace: Optional[bool] = None
    parking_capacity: Optional[int] = None
    tax_assessed_value: Optional[int] = None
    property_tax_rate: Optional[float] = None
    hoa_fee: Optional[float] = None
    days_on_zillow: Optional[int] = None
    price_change: Optional[int] = None
    date_price_changed: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    last_synced: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class AddPropertyToCollection(BaseModel):
    collection_id: str
    property_id: Optional[str] = None  # If property already exists in DB
    zpid: Optional[int] = None  # If we need to fetch from Zillow first

