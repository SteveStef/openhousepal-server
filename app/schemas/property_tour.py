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
    confirmed_date: Optional[str] = None
    confirmed_time: Optional[str] = None
    is_completed: bool
    created_at: datetime
    updated_at: datetime
    # Set only on the confirm response: 'added' (pushed to the agent's Google Calendar),
    # 'not_connected' (no calendar linked - prompt them to connect one), or 'error' (calendar is
    # linked but the push failed - see the connection's last_error). None everywhere else.
    calendar_status: Optional[str] = None

    class Config:
        from_attributes = True


class PropertyTourCompletionUpdate(BaseModel):
    """Schema for updating tour completion status"""
    is_completed: bool


class PropertyTourConfirmUpdate(BaseModel):
    """Schema for confirming a tour request with the agreed date/time"""
    confirmed_date: str
    confirmed_time: str
