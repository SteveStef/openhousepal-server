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
    visitor_name: Optional[str] = None
    visitor_email: Optional[str] = None
    visitor_phone: Optional[str] = None


class PropertyTourResponse(BaseModel):
    """Response schema for property tour"""
    id: str
    collection_id: str
    property_id: str
    visitor_name: str
    visitor_email: str
    visitor_phone: Optional[str] = None
    preferred_date: str
    preferred_time: str
    preferred_date_2: Optional[str] = None
    preferred_time_2: Optional[str] = None
    preferred_date_3: Optional[str] = None
    preferred_time_3: Optional[str] = None
    message: Optional[str] = None
    is_completed: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PropertyTourCompletionUpdate(BaseModel):
    """Schema for updating tour completion status"""
    is_completed: bool
