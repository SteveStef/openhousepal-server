from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime
from typing import Optional, Dict, Any

from app.models.database import Property, OpenHouseVisitor, Collection, collection_properties, OpenHouseEvent, User, CollectionPreferences
from app.schemas.open_house import OpenHouseFormSubmission
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.collections_service import CollectionsService
from app.services.property_service import property_service
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema
from app.config.logging import get_logger

logger = get_logger(__name__)

class OpenHouseService:

    @staticmethod
    async def create_visitor(db: AsyncSession, form_data: OpenHouseFormSubmission) -> OpenHouseVisitor:
        """Create a visitor record from open house form submission"""

        visitor = OpenHouseVisitor(
            full_name=form_data.full_name,
            email=form_data.email,
            phone=form_data.phone,
            has_agent=form_data.has_agent.value,
            open_house_event_id=form_data.open_house_event_id,
            qr_code="",  # Will be updated by the calling code
            interested_in_similar=form_data.interested_in_similar,
            created_at=datetime.utcnow()
        )

        db.add(visitor)
        await db.commit()
        await db.refresh(visitor)
        return visitor
    
    @staticmethod
    async def create_collection_for_visitor(
        db: AsyncSession, 
        visitor: OpenHouseVisitor, 
        form_data: OpenHouseFormSubmission
    ) -> Dict[str, Any]:
        """Create a collection for a visitor and immediately populate it with matching properties"""
        
        if not form_data.interested_in_similar or not form_data.open_house_event_id:
            return {"success": False, "properties_added": 0}
            
        try:
            # Get the original open house event to create smart filters
            visited_open_house = await OpenHouseService.get_open_house_event_by_id(db, form_data.open_house_event_id)
            
            if not visited_open_house:
                return {"success": False, "properties_added": 0}

            # Check if agent has PREMIUM plan (collections are a Premium-only feature)
            agent_id = visited_open_house.get('agent_id')
            agent_query = select(User).where(User.id == agent_id)
            agent_result = await db.execute(agent_query)
            agent = agent_result.scalar_one_or_none()

            if not agent:
                return {"success": False, "properties_added": 0, "reason": "agent_not_found"}

            # Only create collections for PREMIUM plan agents
            if agent.plan_tier != "PREMIUM":
                return {"success": False, "properties_added": 0, "reason": "basic_plan"}

            # Create collection (always ACTIVE, no limit)
            collection = Collection(
                owner_id=visited_open_house.get('agent_id'),  # Use agent_id from the open house event
                name=visited_open_house.get('address', 'Unknown Property'),
                description=f"Properties similar to {visited_open_house.get('address', 'the visited property')} based on {visitor.full_name}'s preferences",
                visitor_email=visitor.email,
                visitor_name=visitor.full_name,
                visitor_phone=visitor.phone,
                original_open_house_event_id=form_data.open_house_event_id,
                share_token=CollectionsService.generate_share_token(),
                status='ACTIVE',
                created_at=datetime.utcnow()
            )
            
            db.add(collection)
            await db.commit()
            await db.refresh(collection)
            
            # Auto-generate preferences based on the original property and form data
            try:
                preferences_model = await CollectionPreferencesService.auto_generate_preferences(db, collection.id, form_data)
                
                if preferences_model:
                    # Convert model to schema for the population service
                    preferences_schema = CollectionPreferencesSchema.model_validate(preferences_model)
                    
                    # Immediately fetch and populate properties
                    properties_added = await OpenHouseService._populate_collection_with_properties(
                        db, collection, preferences_schema
                    )
                    return {"success": True, "properties_added": properties_added, "collection_id": collection.id, "share_token": collection.share_token}
                else:
                    logger.warning(f"Failed to generate preferences for collection {collection.id}")
                    return {"success": True, "properties_added": 0, "collection_id": collection.id, "share_token": collection.share_token}

            except Exception as e:
                logger.error(f"Preference generation or population failed for collection {collection.id}: {str(e)}", exc_info=True)
                # Collection creation should still succeed even if preferences fail
                return {"success": True, "properties_added": 0, "collection_id": collection.id, "share_token": collection.share_token}
            
        except Exception as e:
            logger.error("creating collection for visitor failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "properties_added": 0}
    
    @staticmethod
    async def _populate_collection_with_properties(
        db: AsyncSession,
        collection: Collection,
        preferences: CollectionPreferencesSchema
    ) -> int:
        """
        Populate collection with properties from local mirror.
        Optimized with 'Smart Discovery' iterative radius tuning.
        """
        try:
            # 1. SMART DISCOVERY (Iterative Radius Tuning)
            current_radius = 6.0
            
            # Initial count check
            count = await property_service.get_properties_count_by_preferences(db, preferences)
            logger.info(f"Smart Discovery: Initial count at 6 miles for collection {collection.id} is {count}")

            if count > 30:
                for r in [3.0, 1.5]:
                    preferences.diameter = r
                    count = await property_service.get_properties_count_by_preferences(db, preferences)
                    current_radius = r
                    if count < 30:
                        break
            elif count < 3:
                # Try expanding the search
                for r in [12.0, 20.0]:
                    preferences.diameter = r
                    count = await property_service.get_properties_count_by_preferences(db, preferences)
                    current_radius = r
                    if count >= 3:
                        break

            # 2. SAVE SMART RADIUS
            await db.execute(
                update(CollectionPreferences)
                .where(CollectionPreferences.collection_id == collection.id)
                .values(diameter=current_radius)
            )
            await db.commit()

            # 3. FINAL POPULATE using standardized utility
            from app.services.collections_service import CollectionsService
            population_result = await CollectionsService.repopulate_collection_from_preferences(
                db, collection.id, commit=True
            )
            
            return population_result.get('properties_found', 0)
            
        except Exception as e:
            logger.error(f"Smart Discovery population failed: {e}", exc_info=True)
            return 0
    
    @staticmethod
    async def get_open_house_event_by_id(db: AsyncSession, open_house_event_id: str) -> Optional[dict]:
        """Get open house event details by ID from database"""
        try:
            stmt = select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_event_id)
            result = await db.execute(stmt)
            open_house_record = result.scalar_one_or_none()
            
            if not open_house_record:
                return None
            
            return {
                "id": open_house_record.id,
                "agent_id": open_house_record.agent_id,
                "address": open_house_record.address,
                "abbreviated_address": open_house_record.abbreviated_address,
                "city": open_house_record.city,
                "state": open_house_record.state,
                "zipCode": open_house_record.zipcode,
                "price": open_house_record.price,
                "beds": open_house_record.bedrooms,
                "baths": open_house_record.bathrooms,
                "squareFeet": open_house_record.living_area,
                "lotSize": open_house_record.lot_size,
                "propertyType": open_house_record.house_type,
                "homeStatus": open_house_record.home_status,
                "imageSrc": open_house_record.cover_image_url,
                "listingKey": open_house_record.listing_key,
                "created_at": open_house_record.created_at
            }
            
        except Exception as e:
            logger.error(f"Failed to fetch open house event: {e}")
            return None

    @staticmethod
    async def get_property_by_qr_code(db: AsyncSession, qr_code: str) -> Optional[dict]:
        """Get property information by open house event ID"""
        return await OpenHouseService.get_open_house_event_by_id(db, qr_code)
