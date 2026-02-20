from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, and_
from sqlalchemy.orm import selectinload
from typing import List, Dict, Any, Optional
import asyncio
from datetime import datetime, timezone, timedelta
import os

from app.models.database import Collection, CollectionPreferences, Property, collection_properties, User, PropertyInteraction, PropertyComment, PropertyTour
from app.services.bright_mls_service import BrightMlsService
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.email_service import EmailService
from app.config.logging import get_logger

# Get logger from centralized config
logger = get_logger(__name__)

class PropertySyncService:
    """
    Service for syncing property data between Bright MLS and the local database.
    Updated to work with the robust BrightMlsService implementation.
    """
    def __init__(self):
        self.email_service = EmailService()
    
    async def get_total_active_collections_count(self, db: AsyncSession) -> int:
        """Get total count of active collections that have preferences"""
        query = (
            select(func.count())
            .select_from(Collection)
            .join(CollectionPreferences)
            .where(Collection.status == 'ACTIVE')
        )
        result = await db.execute(query)
        return result.scalar() or 0

    async def get_active_collections_with_preferences(
        self,
        db: AsyncSession,
        max_collections: int = None
    ) -> List[tuple]:
        """Get active collections prioritized by oldest last_synced_at"""
        query = (
            select(Collection, CollectionPreferences)
            .join(CollectionPreferences)
            .options(selectinload(Collection.owner))
            .where(Collection.status == 'ACTIVE')
            .order_by(Collection.last_synced_at.asc().nullsfirst())
        )

        if max_collections and max_collections > 0:
            query = query.limit(max_collections)

        result = await db.execute(query)
        return result.fetchall()
    
    async def property_exists_in_collection(
        self, 
        db: AsyncSession, 
        collection_id: str, 
        listing_key: str
    ) -> bool:
        """Check if a property already exists in a collection"""
        result = await db.execute(
            select(Property.id)
            .join(collection_properties)
            .where(
                collection_properties.c.collection_id == collection_id,
                Property.listing_key == str(listing_key)
            )
        )
        return result.scalar_one_or_none() is not None
    
    async def create_property_from_mls_data(
        self, 
        db: AsyncSession, 
        property_data: Dict[str, Any]
    ) -> Property:
        """
        Upserts a Property record from standardized MLS data.
        Maps the new BrightMlsService keys to database columns.
        """
        listing_key = str(property_data.get('listing_key'))
        
        # Check if property already exists
        result = await db.execute(
            select(Property).where(Property.listing_key == listing_key)
        )
        existing_property = result.scalar_one_or_none()
        
        if existing_property:
            # Map standardized keys to DB columns
            existing_property.street_address = property_data.get('address')
            existing_property.city = property_data.get('city')
            existing_property.state = property_data.get('state')
            existing_property.zipcode = property_data.get('zipcode')
            existing_property.price = property_data.get('price')
            existing_property.bedrooms = property_data.get('bedrooms')
            existing_property.bathrooms = property_data.get('bathrooms')
            existing_property.living_area = property_data.get('living_area')
            existing_property.lot_size = property_data.get('lot_size')
            existing_property.home_type = property_data.get('home_type')
            existing_property.home_status = property_data.get('home_status')
            existing_property.latitude = property_data.get('latitude')
            existing_property.longitude = property_data.get('longitude')
            existing_property.img_src = property_data.get('image_url')
            existing_property.updated_at = datetime.now(timezone.utc)
            
            await db.commit()
            await db.refresh(existing_property)
            return existing_property
        
        # Create new property
        property_obj = Property(
            listing_key=listing_key,
            street_address=property_data.get('address'),
            city=property_data.get('city'),
            state=property_data.get('state'),
            zipcode=property_data.get('zipcode'),
            price=property_data.get('price'),
            bedrooms=property_data.get('bedrooms'),
            bathrooms=property_data.get('bathrooms'),
            living_area=property_data.get('living_area'),
            lot_size=property_data.get('lot_size'),
            home_type=property_data.get('home_type'),
            home_status=property_data.get('home_status'),
            latitude=property_data.get('latitude'),
            longitude=property_data.get('longitude'),
            img_src=property_data.get('image_url'),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        
        db.add(property_obj)
        await db.commit()
        await db.refresh(property_obj)
        return property_obj
    
    async def add_property_to_collection(self, db: AsyncSession, collection_id: str, property_id: str, initial: bool = False):
        """Links a property to a collection with or without a NEW badge timestamp"""
        result = await db.execute(
            select(collection_properties).where(
                collection_properties.c.collection_id == collection_id,
                collection_properties.c.property_id == property_id
            )
        )

        if result.fetchone() is None:
            await db.execute(
                collection_properties.insert().values(
                    collection_id=collection_id,
                    property_id=property_id,
                    added_at=None if initial else datetime.now(timezone.utc)
                )
            )
            await db.commit()

    async def invalidate_collection_property_cache(self, db: AsyncSession, collection_id: str) -> int:
        """Force detail refresh for all properties in a collection"""
        logger.info(f"Cache invalidation requested for collection {collection_id}")
        return 0

    async def sync_collection_properties(
        self,
        db: AsyncSession,
        collection: Collection,
        preferences: CollectionPreferences,
        mls_service: BrightMlsService
    ) -> Dict[str, Any]:
        """Core logic for syncing a single collection"""
        logger.info(f"Syncing collection {collection.id}")

        try:
            matching_properties = await mls_service.get_properties_by_preferences(preferences)
            new_count = 0
            first_new = None

            for prop_data in matching_properties:
                listing_key = str(prop_data.get('listing_key'))
                
                # Check for existing property to detect price drops
                existing_result = await db.execute(
                    select(Property).where(Property.listing_key == listing_key)
                )
                existing_p = existing_result.scalar_one_or_none()

                if existing_p and existing_p.price and prop_data.get('price'):
                    if prop_data['price'] < existing_p.price:
                        # Notify about price drop
                        await self._send_price_drop_notification(collection, existing_p, prop_data['price'])

                # Check if linked to this collection
                if not await self.property_exists_in_collection(db, collection.id, listing_key):
                    property_obj = await self.create_property_from_mls_data(db, prop_data)
                    await self.add_property_to_collection(db, collection.id, property_obj.id)
                    new_count += 1
                    if new_count == 1:
                        first_new = prop_data
                else:
                    # Just update the data
                    await self.create_property_from_mls_data(db, prop_data)

            collection.last_synced_at = datetime.now(timezone.utc)
            await db.commit()

            return {
                'new_count': new_count,
                'first_new': first_new,
                'total': len(matching_properties)
            }

        except Exception as e:
            logger.error(f"Sync failed for {collection.id}: {e}", exc_info=True)
            return {'new_count': 0, 'first_new': None, 'total': 0}

    async def _send_price_drop_notification(self, collection, property_obj, new_price):
        """Helper to send price drop emails"""
        if not collection.visitor_email: return
        
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
        self.email_service.send_simple_message(
            to_email=collection.visitor_email,
            subject=f"Price Drop! - {collection.name}",
            template="price_drop_alert",
            template_variables={
                "recipient_name": collection.visitor_name or "Valued Visitor",
                "collection_name": collection.name,
                "collection_link": f"{frontend_url}/showcase/{collection.share_token}",
                "property_address": property_obj.street_address,
                "property_image": property_obj.img_src,
                "old_price": f"${property_obj.price:,}",
                "new_price": f"${new_price:,}",
                "savings": f"${(property_obj.price - new_price):,}"
            }
        )

    async def sync_all_active_collections(self) -> Dict[str, Any]:
        """Scheduled task entry point"""
        from app.database import AsyncSessionLocal
        mls_service = BrightMlsService()
        results = {'processed': 0, 'new_props': 0}
        
        try:
            async with AsyncSessionLocal() as db:
                collections = await self.get_active_collections_with_preferences(db)
                for col, pref in collections:
                    res = await self.sync_collection_properties(db, col, pref, mls_service)
                    results['processed'] += 1
                    results['new_props'] += res['new_count']
                    await asyncio.sleep(0.2)
            return results
        finally:
            await mls_service.close()

    async def populate_new_collection(self, db: AsyncSession, collection_id: str) -> Dict[str, Any]:
        """Initial population for newly created collections"""
        mls_service = BrightMlsService()
        try:
            result = await db.execute(select(Collection).where(Collection.id == collection_id))
            collection = result.scalar_one_or_none()
            if not collection: return {'success': False, 'error': 'Not found'}

            preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
            if not preferences: return {'success': True, 'new_properties_added': 0}

            matching = await mls_service.get_properties_by_preferences(preferences)
            for prop_data in matching:
                if not await self.property_exists_in_collection(db, collection.id, prop_data['listing_key']):
                    p_obj = await self.create_property_from_mls_data(db, prop_data)
                    await self.add_property_to_collection(db, collection.id, p_obj.id, initial=True)

            return {'success': True, 'new_properties_added': len(matching)}
        finally:
            await mls_service.close()

    async def replace_collection_properties(self, db: AsyncSession, collection_id: str, preferences: Optional[CollectionPreferences] = None) -> Dict[str, Any]:
        """Re-populate collection after preference change"""
        mls_service = BrightMlsService()
        try:
            if not preferences:
                preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
            if not preferences: return {'success': False, 'error': 'No preferences'}

            matching = await mls_service.get_properties_by_preferences(preferences)
            
            # Clear existing associations
            await db.execute(collection_properties.delete().where(collection_properties.c.collection_id == collection_id))
            
            for prop_data in matching:
                p_obj = await self.create_property_from_mls_data(db, prop_data)
                await self.add_property_to_collection(db, collection_id, p_obj.id, initial=True)

            return {'success': True, 'properties_replaced': len(matching)}
        finally:
            await mls_service.close()
