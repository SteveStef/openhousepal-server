from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class ContactPreference(str, Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    TEXT = "TEXT"


class VisitingReason(str, Enum):
    BUYING_SOON = "BUYING_SOON"
    BROWSING = "BROWSING"
    NEIGHBORHOOD = "NEIGHBORHOOD"
    INVESTMENT = "INVESTMENT"
    CURIOUS = "CURIOUS"
    OTHER = "OTHER"


class Timeframe(str, Enum):
    IMMEDIATELY = "IMMEDIATELY"
    ONE_TO_THREE_MONTHS = "1_3_MONTHS"
    THREE_TO_SIX_MONTHS = "3_6_MONTHS"
    SIX_TO_TWELVE_MONTHS = "6_12_MONTHS"
    OVER_YEAR = "OVER_YEAR"
    NOT_SURE = "NOT_SURE"


class HasAgent(str, Enum):
    YES = "YES"
    NO = "NO"
    LOOKING = "LOOKING"

          # full_name: formData.fullName,
          # email: formData.email,
          # phone: formData.phone,
          # visiting_reason: formData.visitingReason,
          # timeframe: formData.timeframe,
          # has_agent: formData.hasAgent,
          # property_id: propertyId,
          # agent_id: agentId, // Include agent ID for proper collection assignment
          # additional_comments: formData.additionalComments,
          # interested_in_similar: formData.interestedInSimilar,

class OpenHouseFormSubmission(BaseModel):
    # Contact Information
    full_name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., max_length=254)
    phone: str = Field(..., max_length=20)
    
    # Visit Information
    visiting_reason: VisitingReason
    timeframe: Timeframe
    has_agent: HasAgent
    
    # Property Context
    property_id: Optional[str] = None
    agent_id: Optional[str] = None
    
    additional_comments: Optional[str] = Field(None, max_length=1000)
    interested_in_similar: bool = False
    
    class Config:
        use_enum_values = True



class OpenHouseFormResponse(BaseModel):
    success: bool
    message: str
    visitor_id: Optional[str] = None
    collection_created: bool = False
