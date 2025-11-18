from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List, Dict, Any
import asyncio
from datetime import datetime, timezone

from app.models.database import Collection, CollectionPreferences, Property, collection_properties, User
from app.services.zillow_service import ZillowService
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.email_service import EmailService
from app.config.logging import get_logger
import os
from sqlalchemy import func

# Get logger from centralized config
logger = get_logger(__name__)

class PropertySyncService:
    def __init__(self):
        self.zillow_service = ZillowService()
        self.email_service = EmailService()
    
    async def get_active_collections_with_preferences(self, db: AsyncSession) -> List[tuple]:
        """
        Get all active collections that have preferences set
        Eagerly loads the owner relationship for email notifications
        """
        result = await db.execute(
            select(Collection, CollectionPreferences)
            .join(CollectionPreferences)
            .options(selectinload(Collection.owner))
            .where(Collection.status == 'ACTIVE')
        )
        return result.fetchall()
    
    async def property_exists_in_collection(
        self, 
        db: AsyncSession, 
        collection_id: str, 
        zpid: str
    ) -> bool:
        """
        Check if a property (by zpid) already exists in a collection
        """
        result = await db.execute(
            select(Property.id)
            .join(collection_properties)
            .where(
                collection_properties.c.collection_id == collection_id,
                Property.zpid == zpid
            )
        )
        return result.scalar_one_or_none() is not None
    
    async def create_property_from_zillow_data(
        self, 
        db: AsyncSession, 
        property_data: Dict[str, Any]
    ) -> Property:
        """
        Create a new Property record from Zillow data
        """
        # Check if property already exists by zpid
        result = await db.execute(
            select(Property).where(Property.zpid == property_data.get('zpid'))
        )
        existing_property = result.scalar_one_or_none()
        
        if existing_property:
            field_mapping = {
                'address': 'street_address',
                'image_url': 'img_src'
            }

            # Special handling for zpid (string to int conversion)
            zpid_value = property_data.get('zpid')
            if zpid_value and str(zpid_value).isdigit():
                existing_property.zpid = int(zpid_value)
            
            for key, value in property_data.items():
                if value is not None:
                    # Map field name if necessary
                    actual_field = field_mapping.get(key, key)
                    if hasattr(existing_property, actual_field):
                        setattr(existing_property, actual_field, value)
            
            # Note: last_synced field doesn't exist in Property model, using updated_at instead
            # The updated_at field is automatically updated by SQLAlchemy onupdate
            
            await db.commit()
            await db.refresh(existing_property)
            return existing_property
        
        zpid_value = property_data.get('zpid')
        zpid_int = int(zpid_value) if zpid_value and str(zpid_value).isdigit() else None
        
        property_obj = Property(
            zpid=zpid_int,
            street_address=property_data.get('address'),  # ✅ Fixed: address -> street_address
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
            zestimate=property_data.get('zestimate')
        )
        
        db.add(property_obj)
        await db.commit()
        await db.refresh(property_obj)
        return property_obj
    
    async def add_property_to_collection(
        self,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ):
        """
        Add a property to a collection (many-to-many relationship) with timestamp.
        Used for scheduled property sync - properties will show "NEW" badge.
        """
        # Check if relationship already exists
        result = await db.execute(
            select(collection_properties)
            .where(
                collection_properties.c.collection_id == collection_id,
                collection_properties.c.property_id == property_id
            )
        )

        if result.fetchone() is None:
            # Insert new relationship with timestamp
            await db.execute(
                collection_properties.insert().values(
                    collection_id=collection_id,
                    property_id=property_id,
                    added_at=datetime.now(timezone.utc)
                )
            )
            await db.commit()
            logger.info(f"Added property {property_id} to collection {collection_id}")

    async def add_property_to_collection_initial(
        self,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ):
        """
        Add a property to a collection WITHOUT timestamp (for initial population).
        Used when creating a new showcase - properties will NOT show "NEW" badge.
        """
        # Check if relationship already exists
        result = await db.execute(
            select(collection_properties)
            .where(
                collection_properties.c.collection_id == collection_id,
                collection_properties.c.property_id == property_id
            )
        )

        if result.fetchone() is None:
            # Insert new relationship WITHOUT added_at (NULL = no "NEW" badge)
            await db.execute(
                collection_properties.insert().values(
                    collection_id=collection_id,
                    property_id=property_id
                    # No added_at field = NULL in database
                )
            )
            await db.commit()
            logger.info(f"Added initial property {property_id} to collection {collection_id} (no timestamp)")

    async def sync_collection_properties(
        self,
        db: AsyncSession,
        collection: Collection,
        preferences: CollectionPreferences
    ) -> Dict[str, Any]:
        """
        Sync properties for a single collection based on its preferences
        Returns dict with new_properties_count, collection, and total_properties
        """
        logger.info(f"Syncing properties for collection {collection.id}")

        try:
            # Get matching properties from Zillow
            matching_properties = await self.zillow_service.get_matching_properties(preferences)

            new_properties_count = 0

            for property_data in matching_properties:
                zpid = property_data.get('zpid')
                if not zpid:
                    continue

                # Check if property already exists in this collection
                if await self.property_exists_in_collection(db, collection.id, zpid):
                    # Property exists - check for price drop
                    result = await db.execute(
                        select(Property).where(Property.zpid == zpid)
                    )
                    existing_property = result.scalar_one_or_none()

                    if existing_property:
                        old_price = existing_property.price  # OLD price from database
                        new_price = property_data.get('price')  # NEW price from Zillow

                        # Check for price drop
                        if old_price and new_price and new_price < old_price:
                            # Price dropped! Send notification
                            visitor_email = collection.visitor_email
                            visitor_name = collection.visitor_name or "Valued Visitor"
                            collection_name = collection.name

                            # Build collection link
                            frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
                            collection_link = f"{frontend_url}/showcase/{collection.share_token}"

                            # Calculate savings
                            savings = old_price - new_price
                            discount_percent = round((savings / old_price) * 100, 1)

                            if visitor_email:
                                email_service = EmailService()
                                email_service.send_simple_message(
                                    to_email=visitor_email,
                                    subject=f"Price Drop Alert - {collection_name}",
                                    template="price_drop_alert",
                                    template_variables={
                                        "recipient_name": visitor_name,
                                        "collection_name": collection_name,
                                        "collection_link": collection_link,
                                        "property_address": existing_property.street_address,
                                        "property_image": existing_property.img_src,
                                        "old_price": f"${old_price:,}",
                                        "new_price": f"${new_price:,}",
                                        "savings": f"${savings:,}",
                                        "discount_percent": f"{discount_percent}%"
                                    }
                                )
                                logger.info(f"Price drop email sent for property {zpid}: ${old_price:,} → ${new_price:,}")

                    # Update property with new data
                    property_obj = await self.create_property_from_zillow_data(db, property_data)
                    continue  # Don't count as new property

                # Create or update property
                property_obj = await self.create_property_from_zillow_data(db, property_data)

                # Add property to collection
                await self.add_property_to_collection(db, collection.id, property_obj.id)
                new_properties_count += 1

            # Get total property count without lazy loading
            count_result = await db.execute(
                select(func.count()).select_from(collection_properties).where(
                    collection_properties.c.collection_id == collection.id
                )
            )
            total_properties = count_result.scalar()

            logger.info(f"Added {new_properties_count} new properties to collection {collection.id}")
            return {
                'new_properties_count': new_properties_count,
                'collection': collection,
                'total_properties': total_properties
            }

        except Exception as e:
            logger.error(f"Error syncing collection {collection.id}", exc_info=True, extra={"collection_id": collection.id})
            return {
                'new_properties_count': 0,
                'collection': collection,
                'total_properties': 0
            }
    
    async def sync_all_active_collections(self) -> Dict[str, Any]:
        """
        Sync all active collections with their preferences
        This is the main function that should be called by the scheduled task
        """
        logger.info("Starting property sync for all active collections")
        
        sync_results = {
            'started_at': datetime.now(),
            'collections_processed': 0,
            'total_new_properties': 0,
            'errors': [],
            'success': True
        }
        
        # Import here to avoid circular imports and ensure proper async context
        from app.database import AsyncSessionLocal
        
        try:
            async with AsyncSessionLocal() as db:
                try:
                    # Get all active collections with preferences
                    collections_with_preferences = await self.get_active_collections_with_preferences(db)
                    
                    for collection, preferences in collections_with_preferences:
                        try:
                            # Capture attributes before any async operations that might detach the object
                            collection_id = collection.id
                            visitor_email = collection.visitor_email
                            visitor_name = collection.visitor_name or "Valued Visitor"
                            share_token = collection.share_token
                            collection_name = collection.name

                            sync_result = await self.sync_collection_properties(db, collection, preferences)
                            sync_results['collections_processed'] += 1
                            sync_results['total_new_properties'] += sync_result['new_properties_count']

                            # Send email notifications to visitor and agent if new properties were added
                            if sync_result['new_properties_count'] > 0 and visitor_email and share_token:
                                # Get agent info
                                agent_result = await db.execute(
                                    select(User).where(User.id == collection.owner_id)
                                )
                                agent = agent_result.scalar_one_or_none()

                                # Build collection link
                                frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
                                collection_link = f"{frontend_url}/showcase/{share_token}"

                                # Send to visitor
                                self.email_service.send_simple_message(
                                    to_email=visitor_email,
                                    subject=f"New Properties Added to Your Collection - {collection_name}",
                                    template="new_properties_synced",
                                    template_variables={
                                        "recipient_name": visitor_name,
                                        "collection_name": collection_name,
                                        "new_count": sync_result['new_properties_count'],
                                        "total_count": sync_result['total_properties'],
                                        "collection_link": collection_link
                                    }
                                )

                                # Send to agent (same template, different recipient)
                                if agent and agent.email:
                                    self.email_service.send_simple_message(
                                        to_email=agent.email,
                                        subject=f"New Properties Added to {visitor_name}'s Collection",
                                        template="new_properties_synced",
                                        template_variables={
                                            "recipient_name": agent.first_name,
                                            "collection_name": collection_name,
                                            "new_count": sync_result['new_properties_count'],
                                            "total_count": sync_result['total_properties'],
                                            "collection_link": collection_link,
                                            "visitor_name": visitor_name
                                        }
                                    )

                                logger.info(
                                    f"Email notifications sent for collection {collection_id}: "
                                    f"{sync_result['new_properties_count']} new properties"
                                )

                            # Add small delay between collection syncs to be respectful to API
                            await asyncio.sleep(2)

                        except Exception as e:
                            error_msg = f"Failed to sync collection {collection.id}: {str(e)}"
                            logger.error(error_msg)
                            sync_results['errors'].append(error_msg)
                    
                    sync_results['completed_at'] = datetime.now()
                    sync_results['duration_seconds'] = (
                        sync_results['completed_at'] - sync_results['started_at']
                    ).total_seconds()
                    
                    logger.info(
                        f"Property sync completed. Processed {sync_results['collections_processed']} collections, "
                        f"added {sync_results['total_new_properties']} new properties"
                    )
                    
                except Exception as e:
                    error_msg = f"Database error during property sync: {str(e)}"
                    logger.error(error_msg)
                    sync_results['success'] = False
                    sync_results['errors'].append(error_msg)
                    
        except Exception as e:
            error_msg = f"Critical error during property sync: {str(e)}"
            logger.error(error_msg)
            sync_results['success'] = False
            sync_results['errors'].append(error_msg)
        
        return sync_results
    
    async def sync_single_collection(self, collection_id: str) -> Dict[str, Any]:
        """
        Sync a single collection manually (useful for testing or manual triggers)
        """
        logger.info(f"Starting manual sync for collection {collection_id}")
        
        # Import here to avoid circular imports
        from app.database import AsyncSessionLocal
        
        async with AsyncSessionLocal() as db:
            try:
                # Get collection and preferences
                result = await db.execute(
                    select(Collection).where(Collection.id == collection_id)
                )
                collection = result.scalar_one_or_none()
                
                if not collection:
                    return {'success': False, 'error': 'Collection not found'}
                
                preferences = await CollectionPreferencesService.get_preferences_by_collection_id(
                    db, collection_id
                )
                
                if not preferences:
                    return {'success': False, 'error': 'No preferences found for collection'}
                
                # Sync properties
                sync_result = await self.sync_collection_properties(
                    db, collection, preferences
                )

                return {
                    'success': True,
                    'collection_id': collection_id,
                    'new_properties_added': sync_result['new_properties_count'],
                    'synced_at': datetime.now()
                }
                
            except Exception as e:
                logger.error(f"Error syncing single collection {collection_id}", exc_info=True, extra={"collection_id": collection_id})
                return {'success': False, 'error': str(e)}
    
    async def populate_new_collection(self, db: AsyncSession, collection_id: str) -> Dict[str, Any]:
        """
        Immediately populate a newly created collection with properties based on its preferences.
        This is called right after collection creation to provide initial properties.
        Initial properties are added WITHOUT timestamps (no "NEW" badge).
        """
        logger.info(f"Populating new collection {collection_id} with initial properties")

        try:
            # Get collection and preferences
            result = await db.execute(
                select(Collection).where(Collection.id == collection_id)
            )
            collection = result.scalar_one_or_none()

            if not collection:
                return {'success': False, 'error': 'Collection not found'}

            preferences = await CollectionPreferencesService.get_preferences_by_collection_id(
                db, collection_id
            )

            if not preferences:
                logger.warning(f"No preferences found for new collection {collection_id}, skipping property population")
                return {'success': True, 'new_properties_added': 0, 'message': 'No preferences to populate from'}

            # Get matching properties from Zillow
            matching_properties = await self.zillow_service.get_matching_properties(preferences)

            properties_added = 0

            for property_data in matching_properties:
                zpid = property_data.get('zpid')
                if not zpid:
                    continue

                # Check if property already exists in this collection
                if await self.property_exists_in_collection(db, collection.id, zpid):
                    continue

                # Create or update property
                property_obj = await self.create_property_from_zillow_data(db, property_data)

                # Add property WITHOUT timestamp (initial population - no "NEW" badge)
                await self.add_property_to_collection_initial(db, collection.id, property_obj.id)
                properties_added += 1

            logger.info(f"Successfully populated new collection {collection_id} with {properties_added} initial properties (no timestamps)")

            return {
                'success': True,
                'collection_id': collection_id,
                'new_properties_added': properties_added,
                'populated_at': datetime.now()
            }

        except Exception as e:
            logger.error(f"Error populating new collection {collection_id}", exc_info=True, extra={"collection_id": collection_id})
            # Don't raise the exception - collection creation should succeed even if population fails
            return {
                'success': False,
                'collection_id': collection_id,
                'error': str(e),
                'new_properties_added': 0
            }

    async def replace_collection_properties(self, db: AsyncSession, collection_id: str) -> Dict[str, Any]:
        """
        Replace all properties in a collection with new ones based on updated preferences.
        This removes all existing property associations and adds new matching properties.
        """
        logger.info(f"Replacing all properties for collection {collection_id}")

        try:
            # Get collection and preferences
            result = await db.execute(
                select(Collection).where(Collection.id == collection_id)
            )
            collection = result.scalar_one_or_none()

            if not collection:
                return {'success': False, 'error': 'Collection not found'}

            # Get preferences for this collection
            preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)

            if not preferences:
                return {'success': False, 'error': 'No preferences found for collection'}

            # Step 1: Remove all existing property associations for this collection
            await db.execute(
                collection_properties.delete().where(
                    collection_properties.c.collection_id == collection_id
                )
            )
            # Note: Do not commit yet - wait until new properties are successfully fetched
            logger.info(f"Removed all existing property associations for collection {collection_id}")

            # Step 2: Get matching properties from Zillow based on current preferences
            matching_properties = await self.zillow_service.get_matching_properties(preferences)

            properties_added = 0

            # Step 3: Add new matching properties to the collection
            for property_data in matching_properties:
                zpid = property_data.get('zpid')
                if not zpid:
                    continue

                try:
                    # Create or update property in database
                    property_obj = await self.create_property_from_zillow_data(db, property_data)

                    # Add property to collection
                    await self.add_property_to_collection(db, collection_id, property_obj.id)
                    properties_added += 1

                except Exception as e:
                    logger.warning(f"Failed to add property {zpid} to collection {collection_id}: {str(e)}")
                    continue

            # Commit the transaction only after all properties are successfully added
            await db.commit()
            logger.info(f"Successfully replaced properties for collection {collection_id}: {properties_added} properties added")

            return {
                'success': True,
                'collection_id': collection_id,
                'properties_replaced': properties_added,
                'message': f'Collection updated with {properties_added} matching properties'
            }

        except Exception as e:
            logger.error(f"Error replacing properties for collection {collection_id}", exc_info=True, extra={"collection_id": collection_id})
            await db.rollback()
            return {
                'success': False,
                'collection_id': collection_id,
                'error': str(e)
            }
