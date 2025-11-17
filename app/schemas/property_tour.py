from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional


class PropertyTourCreate(BaseModel):
    """Schema for creating a new property tour request"""
    preferred_date: str
    preferred_time: str
    preferred_date_2: Optional[str] = None
    preferred_time_2: Optional[str] = None
    preferred_date_3: Optional[str] = None
    preferred_time_3: Optional[str] = None
    message: Optional[str] = None


class PropertyTourResponse(BaseModel):
    """Response schema for property tour"""
    id: str
    collection_id: str
    property_id: str
    visitor_name: str
    visitor_email: str
    visitor_phone: str
    preferred_date: str
    preferred_time: str
    preferred_date_2: Optional[str] = None
    preferred_time_2: Optional[str] = None
    preferred_date_3: Optional[str] = None
    preferred_time_3: Optional[str] = None
    message: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PropertyTourStatusUpdate(BaseModel):
    """Schema for updating tour status"""
    status: str  # PENDING, CONFIRMED, CANCELLED
