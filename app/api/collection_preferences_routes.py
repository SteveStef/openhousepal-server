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

router = APIRouter(prefix="/collection-preferences", tags=["collection-preferences"])

@router.post("/", response_model=CollectionPreferences, status_code=status.HTTP_201_CREATED)
async def create_preferences(
    preferences_data: CollectionPreferencesCreate,
    db: AsyncSession = Depends(get_db)
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
    db: AsyncSession = Depends(get_db)
):
    """Get preferences for a collection"""
    preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
    return preferences

@router.put("/collection/{collection_id}", response_model=CollectionPreferences)
async def update_preferences(
    collection_id: str,
    preferences_update: CollectionPreferencesUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update collection preferences"""
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
    db: AsyncSession = Depends(get_db)
):
    """Delete collection preferences"""
    success = await CollectionPreferencesService.delete_preferences(db, collection_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found for this collection"
        )

@router.post("/auto-generate/{collection_id}", response_model=CollectionPreferences)
async def auto_generate_preferences(
    collection_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Auto-generate preferences based on collection's original property"""
    preferences = await CollectionPreferencesService.auto_generate_preferences(db, collection_id)
    if not preferences:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot auto-generate preferences: collection or original property not found"
        )
    return preferences