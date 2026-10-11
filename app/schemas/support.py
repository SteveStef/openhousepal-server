from pydantic import BaseModel
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


# 'support' = a question for the team, 'bug' = something broke, 'feature' = an idea.
TicketCategory = Literal['support', 'bug', 'feature']


class TicketCreate(BaseModel):
    subject: str
    message: str
    category: TicketCategory = 'support'
    # Quiet context from the browser (page they came from, device, urgency...) - whitelisted and
    # trimmed server-side, see support_routes._clean_details.
    details: Optional[Dict[str, Any]] = None
    # Screenshot URLs returned by POST /support/screenshots.
    attachments: Optional[List[str]] = None


class MessageCreate(BaseModel):
    body: str


class MessageResponse(BaseModel):
    id: str
    sender_type: str
    body: str
    attachments: Optional[List[str]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TicketDetail(BaseModel):
    id: str
    subject: str
    status: str
    category: str = 'support'
    details: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
    messages: List[MessageResponse]

    class Config:
        from_attributes = True


class TicketListItem(BaseModel):
    id: str
    subject: str
    status: str
    category: str = 'support'
    created_at: datetime
    updated_at: datetime
    unread: bool

    class Config:
        from_attributes = True


class AdminTicketListItem(TicketListItem):
    agent_id: str
    agent_name: str
    agent_email: str


class TicketStatusUpdate(BaseModel):
    status: str  # 'open' | 'closed'


class TicketUnreadCountResponse(BaseModel):
    unread_count: int


class ScreenshotUploadResponse(BaseModel):
    url: str
