from pydantic import BaseModel, EmailStr
from typing import Optional
from enum import Enum
from datetime import datetime

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

class OpenHouseFormSubmission(BaseModel):
    # Personal info
    full_name: str
    email: EmailStr
    phone: str
    
    # Visit context
    visiting_reason: VisitingReason
    timeframe: Timeframe
    has_agent: HasAgent
    
    # Open house and agent context
    open_house_event_id: str
    agent_id: Optional[str] = None
    
    # Collection preference
    interested_in_similar: bool = False

class OpenHouseFormResponse(BaseModel):
    success: bool
    message: str
    visitor_id: Optional[str] = None
    collection_created: Optional[bool] = False

# Open house management schemas
class OpenHouseCreateRequest(BaseModel):
    address: str
    property_data: dict  # Full PropertyDetailResponse data from Zillow API
    cover_image_url: str
    open_house_event_id: Optional[str] = None  # UUID provided by frontend for consistent QR codes
    qr_code_url: Optional[str] = None  # Optional since backend will generate it

class OpenHouseResponse(BaseModel):
    id: str
    open_house_event_id: str
    address: str
    cover_image_url: str
    qr_code_url: str
    form_url: str
    # Property details for PDF generation
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    price: Optional[int] = None
    created_at: datetime