from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class CollectionBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = False

class CollectionCreate(CollectionBase):
    pass

class CollectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None

class Collection(CollectionBase):
    id: str
    owner_id: str
    share_token: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class CollectionWithProperties(Collection):
    properties: List["PropertySummary"] = []

class ShareCollectionRequest(BaseModel):
    collection_id: str
    
class ShareCollectionResponse(BaseModel):
    share_token: str
    share_url: str

class CollectionResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str = "ACTIVE"
    visitor_name: Optional[str] = None
    visitor_email: Optional[str] = None
    visitor_phone: Optional[str] = None
    original_property: Optional[dict] = None
    preferences: dict = {}
    property_count: int = 0
    is_anonymous: bool = False
    is_public: bool = False
    share_token: Optional[str] = None
    created_at: str
    updated_at: str

# Forward reference for PropertySummary will be resolved when property schemas are imported