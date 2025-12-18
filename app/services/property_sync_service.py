from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List, Dict, Any
import asyncio
from datetime import datetime, timezone

from app.models.database import Collection, CollectionPreferences, Property, collection_properties, User
from app.services.zillow_working_service import ZillowWorkingService
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.email_service import EmailService
from app.config.logging import get_logger
import os
from sqlalchemy import func

# Get logger from centralized config
logger = get_logger(__name__)

class PropertySyncService:
    def __init__(self):
        self.zillow_service = ZillowWorkingService()
        self.email_service = EmailService()
    
    async def get_active_collections_with_preferences(
        self,
        db: AsyncSession,
        max_collections: int = None
    ) -> List[tuple]:
        """
        Get active collections that have preferences set, ordered by last_synced_at (oldest first)
        Eagerly loads the owner relationship for email notifications

        Args:
            db: Database session
            max_collections: Maximum number of collections to return (for batch processing)

        Returns:
            List of (Collection, CollectionPreferences) tuples, ordered by last_synced_at
        """
        query = (
            select(Collection, CollectionPreferences)
            .join(CollectionPreferences)
            .options(selectinload(Collection.owner))
            .where(Collection.status == 'ACTIVE')
            .order_by(Collection.last_synced_at.asc().nullsfirst())  # NULL = never synced (highest priority)
        )

        if max_collections and max_collections > 0:
            query = query.limit(max_collections)

        result = await db.execute(query)
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
            # Verbose logging disabled - use summary logs instead
            # logger.info(f"Added property {property_id} to collection {collection_id}")

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

    async def invalidate_collection_property_cache(
        self,
        db: AsyncSession,
        collection_id: str
    ) -> int:
        """
        Invalidate cached property data for all properties in a collection.
        Returns count of properties with invalidated cache.
        """
        from sqlalchemy import update

        # Get all property IDs in this collection
        result = await db.execute(
            select(collection_properties.c.property_id)
            .where(collection_properties.c.collection_id == collection_id)
        )
        property_ids = [row[0] for row in result.fetchall()]

        if not property_ids:
            return 0

        # Invalidate cache for all these properties
        stmt = update(Property).where(
            Property.id.in_(property_ids)
        ).values(
            detailed_property=None,
            detailed_data_cached=False,
            detailed_data_cached_at=None
        )

        update_result = await db.execute(stmt)
        await db.commit()

        logger.info(f"Invalidated cache for {update_result.rowcount} properties in collection {collection_id}")
        return update_result.rowcount

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
            first_new_property = None

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

                            # Get agent info
                            agent_result = await db.execute(
                                select(User).where(User.id == collection.owner_id)
                            )
                            agent = agent_result.scalar_one_or_none()
                            agent_name = f"{agent.first_name or ''} {agent.last_name or ''}".strip() if agent else ""
                            agent_email = agent.email if agent else ""
                            agent_phone = ""  # User model doesn't have phone field

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
                                        "discount_percent": f"{discount_percent}%",
                                        "agent_name": agent_name,
                                        "agent_email": agent_email,
                                        "agent_phone": agent_phone
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

                # Track the first new property for email template
                if new_properties_count == 1:
                    first_new_property = {
                        'address': property_obj.street_address,
                        'beds': property_obj.bedrooms,
                        'baths': property_obj.bathrooms,
                        'price': property_obj.price,
                        'sqft': property_obj.living_area,
                        'image': property_obj.img_src
                    }

            # Get total property count without lazy loading
            count_result = await db.execute(
                select(func.count()).select_from(collection_properties).where(
                    collection_properties.c.collection_id == collection.id
                )
            )
            total_properties = count_result.scalar()

            logger.info(f"Added {new_properties_count} new properties to collection {collection.id}")

            # Invalidate cache for all properties in this collection
            await self.invalidate_collection_property_cache(db, collection.id)

            # Update last_synced_at timestamp
            collection.last_synced_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(collection)

            return {
                'new_properties_count': new_properties_count,
                'collection': collection,
                'total_properties': total_properties,
                'first_new_property': first_new_property if new_properties_count > 0 else None
            }

        except Exception as e:
            logger.error(f"Error syncing collection {collection.id}", exc_info=True, extra={"collection_id": collection.id})

            # Update last_synced_at even on failure to prevent this collection from blocking others
            try:
                collection.last_synced_at = datetime.now(timezone.utc)
                await db.commit()
                await db.refresh(collection)
            except Exception as commit_error:
                logger.error(f"Failed to update last_synced_at for collection {collection.id}", exc_info=True)

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
                                collection_link_agent = f"{frontend_url}/showcase?showcase={collection_id}"

                                # Extract agent info for email
                                agent_name = f"{agent.first_name or ''} {agent.last_name or ''}".strip() if agent else ""
                                agent_email = agent.email if agent else ""
                                agent_phone = ""  # User model doesn't have phone field

                                # Extract first property details for email
                                first_prop = sync_result.get('first_new_property') or {}
                                property_address = first_prop.get('address', '')
                                property_beds = first_prop.get('beds', '')
                                property_baths = first_prop.get('baths', '')
                                property_price = f"${first_prop['price']:,}" if first_prop.get('price') else ''
                                property_sqft = f"{first_prop['sqft']:,}" if first_prop.get('sqft') else ''
                                property_image = first_prop.get('image', '')

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
                                        "collection_link": collection_link,
                                        "agent_name": agent_name,
                                        "agent_email": agent_email,
                                        "agent_phone": agent_phone,
                                        "property_address": property_address,
                                        "property_beds": property_beds,
                                        "property_baths": property_baths,
                                        "property_price": property_price,
                                        "property_sqft": property_sqft,
                                        "property_image": property_image
                                    }
                                )

                                # Send to agent (different template)
                                if agent and agent.email:
                                    self.email_service.send_simple_message(
                                        to_email=agent.email,
                                        subject=f"New Properties Added to {visitor_name}'s Collection",
                                        template="new_properties_synced_agent",
                                        template_variables={
                                            "recipient_name": agent.first_name,
                                            "collection_name": collection_name,
                                            "new_count": sync_result['new_properties_count'],
                                            "total_count": sync_result['total_properties'],
                                            "collection_link": collection_link,
                                            "visitor_name": visitor_name,
                                            "property_address": property_address,
                                            "property_beds": property_beds,
                                            "property_baths": property_baths,
                                            "property_price": property_price,
                                            "property_sqft": property_sqft,
                                            "property_image": property_image
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
        Note: Does NOT commit - caller must handle commit for atomic updates with preferences.
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

            # Step 1: Get matching properties from Zillow FIRST (validate before deleting)
            matching_properties = await self.zillow_service.get_matching_properties(preferences)

            # Step 2: Validate that properties were found - fail fast if none match
            if not matching_properties or len(matching_properties) == 0:
                logger.warning(f"No matching properties found for collection {collection_id}")
                return {
                    'success': False,
                    'error': 'No properties match the updated preferences',
                    'properties_replaced': 0
                }

            # Step 3: Now safe to delete existing properties (we have new ones to replace with)
            await db.execute(
                collection_properties.delete().where(
                    collection_properties.c.collection_id == collection_id
                )
            )
            # Verbose logging disabled - use summary logs instead
            # logger.info(f"Removed all existing property associations for collection {collection_id}")

            properties_added = 0

            # Step 4: Add new matching properties to the collection
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

            # CRITICAL: Do NOT commit here - let the caller handle commit
            # This ensures atomic updates with preferences
            logger.info(f"Successfully prepared properties for collection {collection_id}: {properties_added} properties ready to commit")

            return {
                'success': True,
                'collection_id': collection_id,
                'properties_replaced': properties_added,
                'message': f'Collection prepared with {properties_added} matching properties'
            }

        except Exception as e:
            logger.error(f"Error replacing properties for collection {collection_id}", exc_info=True, extra={"collection_id": collection_id})
            await db.rollback()
            return {
                'success': False,
                'collection_id': collection_id,
                'error': str(e)
            }
