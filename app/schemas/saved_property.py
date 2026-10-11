from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class SavedManualPropertyCreate(BaseModel):
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    image_url: Optional[str] = None


class SavedManualPropertyResponse(BaseModel):
    id: str
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    image_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
