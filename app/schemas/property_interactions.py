from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class PropertyInteractionUpdate(BaseModel):
    """Schema for updating property interactions (likes, dislikes, favorites)"""
    liked: Optional[bool] = None
    disliked: Optional[bool] = None
    favorited: Optional[bool] = None
    
    # Alternative format for frontend compatibility
    interaction_type: Optional[str] = None  # 'like', 'dislike', 'favorite'
    value: Optional[bool] = None


class PropertyCommentCreate(BaseModel):
    """Schema for creating a new property comment"""
    content: str
    
    # Alternative format for frontend compatibility
    comment: Optional[str] = None  # Alternative field name


class PropertyInteractionResponse(BaseModel):
    """Response schema for property interaction"""
    id: str
    collection_id: str
    property_id: str
    liked: bool
    disliked: bool
    favorited: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PropertyCommentResponse(BaseModel):
    """Response schema for property comment"""
    id: str
    collection_id: str
    property_id: str
    content: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PropertyInteractionStats(BaseModel):
    """Statistics for property interactions within a collection"""
    property_id: str
    likes: int
    dislikes: int
    favorites: int
    comments: int


class PropertyInteractionSummary(BaseModel):
    """Summary response for property interactions"""
    stats: PropertyInteractionStats
    comments: List[PropertyCommentResponse]