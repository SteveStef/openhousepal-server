from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional, Dict, Any
import math

from app.models.database import CollectionPreferences, Collection, Property, OpenHouseEvent
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
    async def can_view_preferences(db: AsyncSession, collection_id: str, user_id: Optional[str] = None) -> bool:
        """Check if a user can view preferences (owns collection OR collection is publicly shared)"""
        result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = result.scalar_one_or_none()

        if not collection:
            return False

        # User owns the collection
        if user_id and collection.owner_id == user_id:
            return True

        # Collection is publicly shared
        if collection.is_public:
            return True

        return False
    
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
        """Auto-generate preferences based on the original open house event of a collection"""
        # Get collection with original open house event
        result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = result.scalar_one_or_none()
        
        if not collection or not collection.original_open_house_event_id:
            return None
        
        # Get the original open house event
        result = await db.execute(
            select(OpenHouseEvent).where(OpenHouseEvent.id == collection.original_open_house_event_id)
        )
        original_open_house = result.scalar_one_or_none()
        
        if not original_open_house:
            return None
        
        # Calculate preferences based on original open house event metadata and form data
        single_family = original_open_house.house_type == "SINGLE_FAMILY"

        preferences_data_dict = {
            "collection_id": collection_id,
            "min_beds": max(1, (original_open_house.bedrooms or 3) - 1),
            "max_beds": 0, # (original_open_house.bedrooms or 3) + 1,
            "min_baths": 0, # max(1.0, (original_open_house.bathrooms or 2.5) - 0.5),
            "max_baths": 0, # (original_open_house.bathrooms or 2.5) + 0.5,
            "min_price": int((original_open_house.price or 1000000) * 0.8),  # 20% less
            "max_price": int((original_open_house.price or 1000000) * 1.2),  # 20% more
            "lat": original_open_house.latitude,
            "long": original_open_house.longitude,
            "address": original_open_house.address,  # Store the original address
            "diameter": 6,
            "special_features": "",

            "is_town_house": not single_family,
            "is_condo": not single_family,
            "is_single_family": single_family,

            "is_lot_land": False,
            "is_multi_family": False,
            "is_apartment": False,
        }
        
        # Add visitor form data if provided
        if form_data:
            preferences_data_dict.update({
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

    @staticmethod
    async def update_preferences_and_refresh_properties(
        db: AsyncSession,
        collection_id: str,
        preferences_update: CollectionPreferencesUpdate
    ) -> Dict[str, Any]:
        """
        Atomically update preferences and refresh properties.
        Only commits if Zillow API succeeds. If Zillow fails, rolls back everything.
        """
        from app.services.property_sync_service import PropertySyncService
        from app.config.logging import get_logger

        logger = get_logger(__name__)

        try:
            # Get existing preferences
            preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
            if not preferences:
                return {
                    'success': False,
                    'error': 'Preferences not found for this collection'
                }

            # Update preferences fields in memory (not committed yet)
            update_data = preferences_update.dict(exclude_unset=True)
            for field, value in update_data.items():
                setattr(preferences, field, value)

            # Don't commit yet - this ensures atomicity
            # Either both preferences and properties update, or neither does

            # Now attempt to refresh properties with the updated preferences
            # This will fetch from Zillow and prepare properties (but NOT commit)
            sync_service = PropertySyncService()
            result = await sync_service.replace_collection_properties(db, collection_id)

            if not result['success']:
                # Zillow failed or no properties found - rollback preference changes too
                await db.rollback()
                logger.error(f"Failed to refresh properties, rolling back preference changes: {result.get('error')}")
                return {
                    'success': False,
                    'error': f"Failed to update: {result.get('error')}",
                    'preferences_updated': False,
                    'properties_refreshed': False
                }

            # Commit both preferences and properties atomically
            await db.commit()

            # Success! Both preferences and properties were updated and committed
            await db.refresh(preferences)
            logger.info(f"Successfully updated preferences and refreshed properties for collection {collection_id}")

            return {
                'success': True,
                'message': f"Updated preferences and refreshed {result['properties_replaced']} properties",
                'preferences_updated': True,
                'properties_refreshed': True,
                'properties_count': result['properties_replaced'],
                'preferences': preferences
            }

        except Exception as e:
            logger.error(f"Error in atomic preferences/properties update for collection {collection_id}", exc_info=True)
            await db.rollback()
            return {
                'success': False,
                'error': str(e),
                'preferences_updated': False,
                'properties_refreshed': False
            }
