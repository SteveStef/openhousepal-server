from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from typing import Optional

from app.models.database import Property, Collection
from app.schemas.property_visit import PropertyVisitFormSubmission
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.collections_service import CollectionsService
from app.config.logging import get_logger

logger = get_logger(__name__)


class PropertyVisitService:
    
    @staticmethod
    async def create_collection_from_visit(
        db: AsyncSession, 
        form_data: PropertyVisitFormSubmission
    ) -> Optional[str]:
        """Create a collection directly from a property visit form submission"""
        
        if not form_data.interested_in_similar or not form_data.property_id:
            return None

        # Require agent_id for collection creation since owner_id cannot be NULL
        if not form_data.agent_id:
            return None

        try:
            # Get the original property to create smart filters
            visited_property = await PropertyVisitService.get_property_by_id(db, form_data.property_id)
            
            if not visited_property:
                return None
            
            # Create collection directly
            collection = Collection(
                owner_id=form_data.agent_id,  # Agent ID is required and validated above
                name=visited_property.get('address', 'Unknown Property'),
                description=f"Properties similar to {visited_property.get('address', 'the visited property')} based on your preferences",
                visitor_email=form_data.email,
                visitor_name=form_data.full_name,
                visitor_phone=form_data.phone,
                original_open_house_event_id=form_data.property_id,
                share_token=CollectionsService.generate_share_token(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            
            db.add(collection)
            await db.commit()
            await db.refresh(collection)
            
            # Auto-generate preferences based on the original property and form data
            try:
                await CollectionPreferencesService.auto_generate_preferences(db, collection.id, form_data)
            except Exception as e:
                # Collection creation should still succeed even if preferences fail
                pass

            return collection.id
            
        except Exception as e:
            logger.error("creating collection from property visit failed", extra={"error": str(e)})
            await db.rollback()
            return None
    
    @staticmethod
    async def get_property_by_id(db: AsyncSession, property_id: str) -> Optional[dict]:
        """Get property details by ID from database"""
        try:
            stmt = select(Property).where(Property.id == property_id)
            result = await db.execute(stmt)
            property_record = result.scalar_one_or_none()
            
            if not property_record:
                return None
            
            # Use structured database fields directly (zillow_data field was removed)
            return {
                "id": property_record.id,
                "address": property_record.street_address,
                "city": property_record.city,
                "state": property_record.state,
                "zipCode": property_record.zipcode,
                "price": property_record.price,
                "beds": property_record.bedrooms,
                "baths": property_record.bathrooms,
                "squareFeet": property_record.living_area,
                "lotSize": property_record.lot_size,
                "propertyType": property_record.home_type,
                "description": "Beautiful property"  # Default description since zillow_data was removed
            }
            
        except Exception as e:
            logger.error("fetching property by ID failed", extra={"error": str(e)})
            return None