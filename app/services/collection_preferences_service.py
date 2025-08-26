from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import math

from app.models.database import CollectionPreferences, Collection, Property
from app.schemas.collection_preferences import CollectionPreferencesCreate, CollectionPreferencesUpdate

class CollectionPreferencesService:
    
    @staticmethod
    async def create_preferences(db: AsyncSession, preferences_data: CollectionPreferencesCreate) -> CollectionPreferences:
        """Create collection preferences"""
        db_preferences = CollectionPreferences(**preferences_data.dict())
        
        db.add(db_preferences)
        await db.commit()
        await db.refresh(db_preferences)
        return db_preferences
    
    @staticmethod
    async def get_preferences_by_collection_id(db: AsyncSession, collection_id: str) -> Optional[CollectionPreferences]:
        """Get preferences for a collection"""
        result = await db.execute(
            select(CollectionPreferences).where(CollectionPreferences.collection_id == collection_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def update_preferences(db: AsyncSession, collection_id: str, preferences_update: CollectionPreferencesUpdate) -> Optional[CollectionPreferences]:
        """Update collection preferences"""
        # Get existing preferences
        preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
        if not preferences:
            return None
        
        # Update fields that are not None
        update_data = preferences_update.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(preferences, field, value)
        
        await db.commit()
        await db.refresh(preferences)
        return preferences
    
    @staticmethod
    async def delete_preferences(db: AsyncSession, collection_id: str) -> bool:
        """Delete collection preferences"""
        preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
        if not preferences:
            return False
        
        await db.delete(preferences)
        await db.commit()
        return True
    
    @staticmethod
    async def auto_generate_preferences(db: AsyncSession, collection_id: str, form_data: Optional[object] = None) -> Optional[CollectionPreferences]:
        """Auto-generate preferences based on the original property of a collection"""
        # Get collection with original property
        result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = result.scalar_one_or_none()
        
        if not collection or not collection.original_property_id:
            return None
        
        # Get the original property
        result = await db.execute(
            select(Property).where(Property.id == collection.original_property_id)
        )
        original_property = result.scalar_one_or_none()
        
        if not original_property:
            return None
        
        # Calculate preferences based on original property and form data
        preferences_data_dict = {
            "collection_id": collection_id,
            "min_beds": max(1, (original_property.bedrooms or 3) - 1),
            "max_beds": (original_property.bedrooms or 3) + 1,
            "min_baths": max(1.0, (original_property.bathrooms or 2.5) - 0.5),
            "max_baths": (original_property.bathrooms or 2.5) + 0.5,
            "min_price": int((original_property.price or 1000000) * 0.8),  # 20% less
            "max_price": int((original_property.price or 1000000) * 1.2),  # 20% more
            "lat": original_property.latitude,
            "long": original_property.longitude,
            "diameter": 2.0,  # Default 2 miles diameter
            "special_features": ""
        }
        
        # Add visitor form data if provided
        if form_data:
            preferences_data_dict.update({
                "timeframe": form_data.timeframe if isinstance(form_data.timeframe, str) else form_data.timeframe.value,
                "visiting_reason": form_data.visiting_reason if isinstance(form_data.visiting_reason, str) else form_data.visiting_reason.value,
                "has_agent": form_data.has_agent if isinstance(form_data.has_agent, str) else form_data.has_agent.value
            })
        
        preferences_data = CollectionPreferencesCreate(**preferences_data_dict)
        
        # Check if preferences already exist
        existing = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
        if existing:
            # Update existing preferences
            update_data = CollectionPreferencesUpdate(**preferences_data.dict(exclude={'collection_id'}))
            return await CollectionPreferencesService.update_preferences(db, collection_id, update_data)
        else:
            # Create new preferences
            return await CollectionPreferencesService.create_preferences(db, preferences_data)