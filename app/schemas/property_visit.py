from pydantic import BaseModel, EmailStr
from typing import Optional
from enum import Enum

class VisitingReason(str, Enum):
    BUYING_SOON = "BUYING_SOON"
    BROWSING = "BROWSING"
    NEIGHBORHOOD = "NEIGHBORHOOD"
    INVESTMENT = "INVESTMENT"
    CURIOUS = "CURIOUS"
    OTHER = "OTHER"

class HasAgent(str, Enum):
    YES = "YES"
    NO = "NO"
    LOOKING = "LOOKING"

class PropertyVisitFormSubmission(BaseModel):
    # Personal info
    full_name: str
    email: EmailStr
    phone: str

    # Visit context
    visiting_reason: VisitingReason
    has_agent: HasAgent

    # Property and agent context
    property_id: str
    agent_id: Optional[str] = None

    # Collection preference
    interested_in_similar: bool = False

class PropertyVisitFormResponse(BaseModel):
    success: bool
    message: str
    collection_id: Optional[str] = None