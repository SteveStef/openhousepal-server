from pydantic import BaseModel
from datetime import datetime


class PublicQuestionCreate(BaseModel):
    email: str
    message: str


class PublicQuestionResponse(BaseModel):
    id: str
    email: str
    message: str
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PublicQuestionUnreadCountResponse(BaseModel):
    unread_count: int
