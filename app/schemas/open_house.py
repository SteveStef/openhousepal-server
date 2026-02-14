from pydantic import BaseModel, EmailStr, ConfigDict
from pydantic.alias_generators import to_camel
from typing import Optional, List, Union, Dict, Any
from enum import Enum
from datetime import datetime

class HasAgent(str, Enum):
    YES = "YES"
    NO = "NO"

class OpenHouseFormSubmission(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    # Personal info
    full_name: str
    email: EmailStr
    phone: str

    # Visit context
    has_agent: HasAgent

    # Open house and agent context
    open_house_event_id: str
    agent_id: Optional[str] = None

    # Collection preference
    interested_in_similar: bool = False

class OpenHouseFormResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    success: bool
    message: str
    visitor_id: Optional[str] = None
    collection_created: Optional[bool] = False

# Open house management schemas
class OpenHouseCreateRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    address: str
    property_data: dict  # Full PropertyDetailResponse data from Zillow API
    cover_image_url: str
    open_house_event_id: Optional[str] = None  # UUID provided by frontend for consistent QR codes
    qr_code_url: Optional[str] = None  # Optional since backend will generate it
    similar_properties_snapshot: Optional[List[Dict[str, Any]]] = None

class OpenHouseResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    id: str
    open_house_event_id: str
    agent_id: Optional[str] = None
    address: str
    cover_image_url: str
    qr_code_url: str
    form_url: str
    # Property details for PDF generation
    bedrooms: Optional[Union[int, float]] = None
    bathrooms: Optional[Union[int, float]] = None
    living_area: Optional[Union[int, float]] = None
    price: Optional[Union[int, float]] = None
    city: Optional[str] = None
    notes: Optional[str] = None
    similar_properties_snapshot: Optional[List[Dict[str, Any]]] = None
    created_at: datetime

class VisitorResponse(BaseModel):
    id: str
    full_name: str
    email: str
    phone: str
    has_agent: str
    interested_in_similar: bool
    notes: Optional[str] = None
    created_at: datetime

class NoteUpdate(BaseModel):
    notes: str