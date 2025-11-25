from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class NotificationCreate(BaseModel):
    """Schema for creating a new notification"""
    agent_id: str
    type: str  # OPEN_HOUSE_SIGN_IN, TOUR_REQUEST, PROPERTY_INTERACTION
    reference_type: str  # VISITOR, TOUR, INTERACTION
    reference_id: str
    title: str
    message: str
    collection_id: Optional[str] = None
    collection_name: Optional[str] = None
    property_id: Optional[str] = None
    property_address: Optional[str] = None
    visitor_name: Optional[str] = None
    link: Optional[str] = None


class NotificationResponse(BaseModel):
    """Response schema for notification"""
    id: str
    agent_id: str
    type: str
    reference_type: str
    reference_id: str
    title: str
    message: str
    collection_id: Optional[str] = None
    collection_name: Optional[str] = None
    property_id: Optional[str] = None
    property_address: Optional[str] = None
    visitor_name: Optional[str] = None
    link: Optional[str] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationUnreadCountResponse(BaseModel):
    """Response schema for unread notification count"""
    unread_count: int
