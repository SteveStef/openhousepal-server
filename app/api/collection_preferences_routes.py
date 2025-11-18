from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.database import get_db
from app.schemas.collection_preferences import (
    CollectionPreferences,
    CollectionPreferencesCreate,
    CollectionPreferencesUpdate
)
from app.services.collection_preferences_service import CollectionPreferencesService
from app.utils.auth import require_premium_plan, get_current_user_optional
from app.models.database import User

router = APIRouter(prefix="/collection-preferences", tags=["collection-preferences"])

@router.post("/", response_model=CollectionPreferences, status_code=status.HTTP_201_CREATED)
async def create_preferences(
    preferences_data: CollectionPreferencesCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """Create collection preferences"""
    try:
        preferences = await CollectionPreferencesService.create_preferences(db, preferences_data)
        return preferences
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create preferences: {str(e)}"
        )

@router.get("/collection/{collection_id}", response_model=Optional[CollectionPreferences])
async def get_preferences_by_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Get preferences for a collection (accessible to owner or visitors of shared collections)"""
    # Check if user can view preferences (owns collection OR collection is publicly shared)
    user_id = current_user.id if current_user else None
    can_view = await CollectionPreferencesService.can_view_preferences(db, collection_id, user_id)

    if not can_view:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Collection not found or not publicly shared."
        )

    preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
    return preferences

@router.put("/collection/{collection_id}", response_model=CollectionPreferences)
async def update_preferences(
    collection_id: str,
    preferences_update: CollectionPreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """Update collection preferences (owner/agent only)"""
    preferences = await CollectionPreferencesService.update_preferences(db, collection_id, preferences_update)
    if not preferences:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found for this collection"
        )
    return preferences

@router.delete("/collection/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preferences(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """Delete collection preferences (owner/agent only)"""
    success = await CollectionPreferencesService.delete_preferences(db, collection_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found for this collection"
        )

@router.post("/auto-generate/{collection_id}", response_model=CollectionPreferences)
async def auto_generate_preferences(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """Auto-generate preferences based on collection's original property (owner/agent only)"""
    preferences = await CollectionPreferencesService.auto_generate_preferences(db, collection_id)
    if not preferences:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot auto-generate preferences: collection or original property not found"
        )
    return preferences