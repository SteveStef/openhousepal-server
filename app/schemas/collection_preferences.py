from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class CollectionPreferencesBase(BaseModel):
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    diameter: float = 2.0
    special_features: str = ""
    
    # Visitor form data
    timeframe: Optional[str] = None
    visiting_reason: Optional[str] = None
    has_agent: Optional[str] = None

class CollectionPreferencesCreate(CollectionPreferencesBase):
    collection_id: str

class CollectionPreferencesUpdate(BaseModel):
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    diameter: Optional[float] = None
    special_features: Optional[str] = None
    
    # Visitor form data
    timeframe: Optional[str] = None
    visiting_reason: Optional[str] = None
    has_agent: Optional[str] = None

class CollectionPreferences(CollectionPreferencesBase):
    id: str
    collection_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True