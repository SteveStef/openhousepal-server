from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from typing import Optional, Dict, Any

from app.models.database import Property, OpenHouseVisitor, Collection, collection_properties, OpenHouseEvent, User
from app.schemas.open_house import OpenHouseFormSubmission
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.collections_service import CollectionsService
from app.services.bright_mls_service import BrightMlsService
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

            # Check if agent already has 50 active collections
            should_be_active = await CollectionsService.should_create_as_active(db, agent_id)
            collection_status = 'ACTIVE' if should_be_active else 'INACTIVE'

            # Create collection
            collection = Collection(
                owner_id=visited_open_house.get('agent_id'),  # Use agent_id from the open house event
                name=visited_open_house.get('address', 'Unknown Property'),
                description=f"Properties similar to {visited_open_house.get('address', 'the visited property')} based on {visitor.full_name}'s preferences",
                visitor_email=visitor.email,
                visitor_name=visitor.full_name,
                visitor_phone=visitor.phone,
                original_open_house_event_id=form_data.open_house_event_id,
                share_token=CollectionsService.generate_share_token(),
                status=collection_status,
                created_at=datetime.utcnow()
            )
            
            db.add(collection)
            await db.commit()
            await db.refresh(collection)
            
            # Auto-generate preferences based on the original property and form data
            try:
                preferences = await CollectionPreferencesService.auto_generate_preferences(db, collection.id, form_data)
                
                if preferences:
                    
                    # Immediately fetch and populate properties using BrightMlsService
                    properties_added = await OpenHouseService._populate_collection_with_properties(
                        db, collection, preferences
                    )
                    return {"success": True, "properties_added": properties_added, "collection_id": collection.id, "share_token": collection.share_token}
                else:
                    return {"success": True, "properties_added": 0, "collection_id": collection.id, "share_token": collection.share_token}

            except Exception as e:
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
        """Populate collection with properties from Bright MLS API"""
        mls_service = BrightMlsService()
        try:
            # Get matching properties from Bright MLS using the new method name
            matching_properties = await mls_service.get_properties_by_preferences(preferences)
            
            properties_added = 0
            
            for property_data in matching_properties:
                listing_key = property_data.get('listing_key')
                if not listing_key:
                    continue
                
                if await OpenHouseService._property_exists_in_collection(db, collection.id, listing_key):
                    continue

                property_obj = await OpenHouseService._create_property_from_mls_data(db, property_data)
                await OpenHouseService._add_property_to_collection(db, collection.id, property_obj.id)
                properties_added += 1
            
            return properties_added
            
        except Exception as e:
            logger.error(f"populating collection {collection.id} with Bright MLS properties failed", extra={"error": str(e)})
            return 0
        finally:
            await mls_service.close()
    
    @staticmethod
    async def _property_exists_in_collection(db: AsyncSession, collection_id: str, listing_key: str) -> bool:
        """Check if a property (by listing_key) already exists in a collection"""
        # listing_key is a string in the new service
        result = await db.execute(
            select(Property.id)
            .join(collection_properties)
            .where(
                collection_properties.c.collection_id == collection_id,
                Property.listing_key == str(listing_key)
            )
        )
        return result.scalar_one_or_none() is not None
    
    @staticmethod
    async def _create_property_from_mls_data(db: AsyncSession, property_data: Dict[str, Any]) -> Property:
        """Create a new Property record from MLS data"""
        # listing_key is already standardized as a string in property_data
        listing_key = str(property_data.get('listing_key'))
        
        # Check if property already exists by listing_key
        result = await db.execute(
            select(Property).where(Property.listing_key == listing_key)
        )
        existing_property = result.scalar_one_or_none()
        
        if existing_property:
            # Update existing property with latest data
            field_mapping = {
                'address': 'street_address',
                'image_url': 'img_src',
                'days_on_market': 'days_on_zillow', # Keep for compatibility if needed
                'last_updated': 'updated_at' # Map to updated_at
            }
            
            # Update listing_key if needed (should match)
            if listing_key:
                 existing_property.listing_key = listing_key
            
            for key, value in property_data.items():
                if value is not None:
                    # Map field name if necessary
                    actual_field = field_mapping.get(key, key)
                    if hasattr(existing_property, actual_field):
                        setattr(existing_property, actual_field, value)
            
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
            zestimate=property_data.get('zestimate'),
        )
        
        db.add(property_obj)
        await db.commit()
        await db.refresh(property_obj)
        return property_obj
    
    @staticmethod 
    async def _add_property_to_collection(db: AsyncSession, collection_id: str, property_id: str):
        """Add a property to a collection (many-to-many relationship)"""
        # Check if relationship already exists
        result = await db.execute(
            select(collection_properties)
            .where(
                collection_properties.c.collection_id == collection_id,
                collection_properties.c.property_id == property_id
            )
        )
        
        if result.fetchone() is None:
            # Insert new relationship
            await db.execute(
                collection_properties.insert().values(
                    collection_id=collection_id,
                    property_id=property_id
                )
            )
            await db.commit()
    
    @staticmethod
    async def get_open_house_event_by_id(db: AsyncSession, open_house_event_id: str) -> Optional[dict]:
        """Get open house event details by ID from database"""
        try:
            stmt = select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_event_id)
            result = await db.execute(stmt)
            open_house_record = result.scalar_one_or_none()
            
            if not open_house_record:
                return None
            
            # Use open house event metadata fields
            return {
                "id": open_house_record.id,
                "agent_id": open_house_record.agent_id,  # Include agent_id for collection ownership
                "address": open_house_record.address,
                "city": open_house_record.city,
                "state": open_house_record.state,
                "zipCode": open_house_record.zipcode,
                "price": open_house_record.price,
                "beds": open_house_record.bedrooms,
                "baths": open_house_record.bathrooms,
                "squareFeet": None,  # Column dropped from database
                "lotSize": None,  # Column dropped from database
                "propertyType": open_house_record.house_type,
                "description": "Beautiful property",  # Default description
                "latitude": open_house_record.latitude,
                "longitude": open_house_record.longitude,
                "yearBuilt": None,  # Column dropped from database
                "homeStatus": open_house_record.home_status,
                "imageSrc": open_house_record.cover_image_url
            }
            
        except Exception as e:
            logger.error("fetching open house event by ID failed", extra={"error": str(e)})
            return None

    @staticmethod
    async def get_property_by_qr_code(db: AsyncSession, qr_code: str) -> Optional[dict]:
        """Get property information by open house event ID from OpenHouseEvent metadata"""
        try:
            # Find OpenHouseEvent by ID (the parameter is actually the open house event ID)
            stmt = select(OpenHouseEvent).where(OpenHouseEvent.id == qr_code)
            result = await db.execute(stmt)
            open_house_record = result.scalar_one_or_none()
            
            if not open_house_record:
                return None
            
            # Return property data from OpenHouseEvent metadata
            return {
                "id": open_house_record.id,
                "address": open_house_record.address,
                "city": open_house_record.city,
                "state": open_house_record.state,
                "zipCode": open_house_record.zipcode,
                "price": open_house_record.price,
                "beds": open_house_record.bedrooms,
                "baths": open_house_record.bathrooms,
                "squareFeet": None,  # Column dropped from database
                "lotSize": None,  # Column dropped from database
                "propertyType": open_house_record.house_type,
                "description": "Beautiful property",  # Default description
                "latitude": open_house_record.latitude,
                "longitude": open_house_record.longitude,
                "yearBuilt": None,  # Column dropped from database
                "homeStatus": open_house_record.home_status,
                "imageSrc": open_house_record.cover_image_url
            }
            
        except Exception as e:
            logger.error("fetching property by QR code failed", extra={"error": str(e)})
            return None
