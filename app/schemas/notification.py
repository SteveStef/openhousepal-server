from pydantic import BaseModel
from datetime import datetime
from typing import Dict, List, Optional


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


class NotificationChannelPrefs(BaseModel):
    email: bool
    push: bool


class NotificationPreferencesCategory(BaseModel):
    id: str
    label: str
    help: str


class NotificationPreferencesResponse(BaseModel):
    """The resolved matrix (every category, every channel present) plus the row metadata the
    Settings screen renders, and whether push is a live channel yet."""
    categories: List[NotificationPreferencesCategory]
    preferences: Dict[str, NotificationChannelPrefs]
    push_available: bool = False


class NotificationPreferencesUpdate(BaseModel):
    """Partial is fine - any category/channel left out keeps its current (or default) value."""
    preferences: Dict[str, Dict[str, bool]]
