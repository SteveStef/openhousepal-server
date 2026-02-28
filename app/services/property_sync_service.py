from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, and_, or_
from sqlalchemy.orm import selectinload, joinedload
from typing import List, Dict, Any, Optional
import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
import os

from app.models.database import Collection, CollectionPreferences, Property, collection_properties, User, PropertyInteraction, PropertyComment, PropertyTour, ScheduledEmail, Notification
from app.services.bright_mls_service import bright_mls_service
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.email_service import EmailService
from app.config.logging import get_logger
from app.database import AsyncSessionLocal

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
        property_data: Dict[str, Any],
        commit: bool = True
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
            
            if commit:
                await db.commit()
                await db.refresh(existing_property)
            else:
                await db.flush()
                
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
        if commit:
            await db.commit()
            await db.refresh(property_obj)
        else:
            await db.flush()
            
        return property_obj
    
    async def add_property_to_collection(self, db: AsyncSession, collection_id: str, property_id: str, initial: bool = False, commit: bool = True):
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
            if commit:
                await db.commit()
            else:
                await db.flush()

    async def replace_collection_properties(
        self,
        db: AsyncSession,
        collection_id: str,
        preferences: CollectionPreferences
    ) -> Dict[str, Any]:
        """
        Forcefully replaces collection properties based on new preferences.
        Preserves properties that have user interactions (likes, comments, tours).
        Does NOT commit, allowing for atomic operations in the caller.
        """
        logger.info(f"Replacing properties for collection {collection_id} based on updated preferences")
        
        try:
            # 1. Fetch new matching properties from MLS using Smart Discovery if radius is provided
            matching_properties = await bright_mls_service.get_properties_by_preferences(preferences)
            
            # 2. Identify IDs of properties to KEEP (those with user interactions)
            # Find property IDs in THIS collection linked to interactions, comments, or tours
            keep_query = select(Property.id).join(collection_properties).where(
                collection_properties.c.collection_id == collection_id
            ).where(
                or_(
                    Property.id.in_(select(PropertyInteraction.property_id).where(PropertyInteraction.collection_id == collection_id)),
                    Property.id.in_(select(PropertyComment.property_id).where(PropertyComment.collection_id == collection_id)),
                    Property.id.in_(select(PropertyTour.property_id).where(PropertyTour.collection_id == collection_id))
                )
            )
            keep_ids_res = await db.execute(keep_query)
            keep_ids = [r[0] for r in keep_ids_res.fetchall()]
            
            # 3. Clear non-kept property links
            delete_stmt = collection_properties.delete().where(
                and_(
                    collection_properties.c.collection_id == collection_id,
                    collection_properties.c.property_id.notin_(keep_ids)
                )
            )
            await db.execute(delete_stmt)
            
            # 4. Link the new matching properties
            properties_replaced = 0
            for prop_data in matching_properties:
                # Upsert property record (without committing)
                property_obj = await self.create_property_from_mls_data(db, prop_data, commit=False)
                
                # Link to collection (without committing)
                # Check if already linked (e.g., if it was in the "keep" set)
                is_linked = await self.property_exists_in_collection(db, collection_id, prop_data['listing_key'])
                if not is_linked:
                    await self.add_property_to_collection(db, collection_id, property_obj.id, initial=True, commit=False)
                    properties_replaced += 1
            
            return {
                'success': True,
                'properties_replaced': properties_replaced,
                'total_new': len(matching_properties)
            }
            
        except Exception as e:
            logger.error(f"Replace properties failed for collection {collection_id}: {str(e)}", exc_info=True)
            return {'success': False, 'error': str(e), 'properties_replaced': 0}

    async def sync_collection_properties(
        self,
        db: AsyncSession,
        collection: Collection,
        preferences: CollectionPreferences
    ) -> Dict[str, Any]:
        """
        Refactored core logic for syncing a single collection.
        Optimized with pre-fetch and batched notifications.
        Now skips price drops for disliked properties.
        """
        logger.info(f"Syncing collection {collection.id} ({collection.name})")

        try:
            # 1. PRE-FETCH OPTIMIZATION (Get all current props + dislike status for this showcase)
            existing_data = await self._get_properties_for_collection(db, collection.id)
            property_map = {str(p.listing_key): {"obj": p, "disliked": bool(disliked)} for p, disliked in existing_data}
            
            # 2. Tracks all changes found during THIS sync run
            changes = {
                "new_properties": [],
                "price_drops": []
            }

            # 3. Call MLS Service
            matching_properties = await bright_mls_service.get_properties_by_preferences(preferences)
            
            for prop_data in matching_properties:
                listing_key = str(prop_data.get('listing_key'))
                
                # Check for existing property to detect price drops
                existing_entry = property_map.get(listing_key)
                existing_p = existing_entry["obj"] if existing_entry else None
                is_disliked = existing_entry["disliked"] if existing_entry else False

                # 4. PRICE DROP DETECTION (Broadcast logic)
                # Skip price drop notifications if the user has already disliked this property
                if existing_p and not is_disliked and existing_p.price and prop_data.get('price'):
                    if prop_data['price'] < existing_p.price:
                        # Add to THIS collection's batch
                        changes["price_drops"].append(prop_data)
                        # Notify ALL OTHER active showcases containing this property
                        await self._broadcast_price_drop(db, prop_data, existing_p.price, exclude_collection_id=collection.id)

                # 5. NEW PROPERTY DETECTION
                if listing_key not in property_map:
                    # Link to this collection
                    property_obj = await self.create_property_from_mls_data(db, prop_data)
                    await self.add_property_to_collection(db, collection.id, property_obj.id)
                    changes["new_properties"].append(prop_data)
                else:
                    # Just update the existing property data (e.g. status, image, price)
                    await self.create_property_from_mls_data(db, prop_data)

            # 6. Schedule Combined Notifications (One email for Agent, one for Visitor)
            if changes["new_properties"] or changes["price_drops"]:
                await self._schedule_combined_notification(db, collection, changes)

            # 7. OFF-MARKET CLEANUP (Optimization: Mark missing props as OFF_MARKET)
            found_listing_keys = [str(p.get('listing_key')) for p in matching_properties]
            if found_listing_keys:
                # Find props in THIS collection that are currently marked as FOR_SALE but were NOT in the MLS results
                missing_props_query = (
                    select(Property)
                    .join(collection_properties)
                    .where(
                        and_(
                            collection_properties.c.collection_id == collection.id,
                            Property.home_status == 'FOR_SALE',
                            Property.listing_key.notin_(found_listing_keys)
                        )
                    )
                )
                missing_props = (await db.execute(missing_props_query)).scalars().all()
                
                for p in missing_props:
                    logger.info(f"Marking property {p.listing_key} as OFF_MARKET (missing from MLS search)")
                    p.home_status = 'OFF_MARKET'
                    p.updated_at = datetime.now(timezone.utc)

            # Update last_synced timestamp
            collection.last_synced_at = datetime.now(timezone.utc)
            await db.commit()

            return {
                'new_count': len(changes["new_properties"]),
                'drop_count': len(changes["price_drops"]),
                'off_market_count': len(missing_props) if found_listing_keys else 0,
                'total': len(matching_properties)
            }

        except Exception as e:
            logger.error(f"Sync failed for collection {collection.id}: {str(e)}", exc_info=True)
            await db.rollback()
            return {'new_count': 0, 'drop_count': 0, 'total': 0}

    async def _get_properties_for_collection(self, db: AsyncSession, collection_id: str) -> List[tuple]:
        """Fetch all properties and their dislike status linked to a collection in ONE query."""
        query = (
            select(Property, PropertyInteraction.disliked)
            .join(collection_properties)
            .outerjoin(PropertyInteraction, and_(
                PropertyInteraction.collection_id == collection_id,
                PropertyInteraction.property_id == Property.id
            ))
            .where(collection_properties.c.collection_id == collection_id)
        )
        result = await db.execute(query)
        return result.all()

    async def _broadcast_price_drop(self, db: AsyncSession, prop_data: Dict[str, Any], old_price: float, exclude_collection_id: str):
        """
        Notify ALL other active collections that contain this property about the price drop.
        EXCLUDES collections where the property has been disliked.
        """
        listing_key = str(prop_data['listing_key'])
        
        # 1. Find all active collections containing this property that HAVEN'T disliked it
        query = (
            select(Collection)
            .join(collection_properties)
            .join(Property)
            .outerjoin(PropertyInteraction, and_(
                PropertyInteraction.collection_id == Collection.id,
                PropertyInteraction.property_id == Property.id
            ))
            .options(selectinload(Collection.owner))
            .where(
                and_(
                    Property.listing_key == listing_key,
                    Collection.status == 'ACTIVE',
                    Collection.id != exclude_collection_id,
                    # Only notify if no interaction exists OR if it's not disliked
                    or_(
                        PropertyInteraction.id == None,
                        PropertyInteraction.disliked == False
                    )
                )
            )
        )
        result = await db.execute(query)
        target_collections = result.scalars().all()

        for col in target_collections:
            # Schedule a price drop email for this collection
            changes = {"new_properties": [], "price_drops": [prop_data]}
            await self._schedule_combined_notification(db, col, changes, is_broadcast=True)

    async def _schedule_combined_notification(self, db: AsyncSession, collection: Collection, changes: Dict[str, Any], is_broadcast: bool = False):
        """
        Schedules a single combined notification for the Agent and Visitor.
        Also creates an in-app Notification for the agent.
        """
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
        new_count = len(changes["new_properties"])
        drop_count = len(changes["price_drops"])
        
        if new_count == 0 and drop_count == 0:
            return

        # Use first featured property for the thumbnail
        featured = changes["new_properties"][0] if new_count > 0 else changes["price_drops"][0]
        
        # Get total property count for the showcase
        total_count_query = select(func.count()).select_from(collection_properties).where(collection_properties.c.collection_id == collection.id)
        total_count_res = await db.execute(total_count_query)
        total_count = total_count_res.scalar() or 0

        # Template shared variables
        common_vars = {
            "collection_name": collection.name,
            "visitor_name": collection.visitor_name or "Valued Visitor",
            "collection_link": f"{frontend_url}/showcase/{collection.share_token}",
            "new_count": new_count,
            "drop_count": drop_count,
            "total_count": total_count,
            "property_address": featured.get('address'),
            "property_image": featured.get('image_url'),
            "property_price": f"${featured.get('price', 0):,}",
            "property_beds": featured.get('bedrooms'),
            "property_baths": featured.get('bathrooms'),
            "property_sqft": featured.get('living_area'),
            "today_date": datetime.now().strftime("%m/%d/%Y")
        }

        # 1. Schedule for Visitor
        if collection.visitor_email:
            template = "price_drop_alert" if (is_broadcast or (drop_count > 0 and new_count == 0)) else "new_properties_synced"
            
            # If we have both, we prefer the 'new_properties_synced' template but could make a 'showcase_update' one later
            
            visitor_vars = {
                **common_vars,
                "recipient_name": collection.visitor_name or "Valued Visitor",
                "agent_name": f"{collection.owner.first_name} {collection.owner.last_name}" if collection.owner else "Your Agent",
                "agent_email": collection.owner.email if collection.owner else "",
                "agent_phone": getattr(collection.owner, 'phone', "") if collection.owner else ""
            }

            db.add(ScheduledEmail(
                id=str(uuid.uuid4()) if not hasattr(ScheduledEmail, 'id') else None, # Let DB handle if default is set
                recipient_email=collection.visitor_email,
                subject=f"Updates for your showcase: {collection.name}",
                template_name=template,
                template_variables=visitor_vars,
                status="PENDING",
                scheduled_for=datetime.now(timezone.utc)
            ))

        # 2. Schedule for Agent
        if collection.owner and collection.owner.email:
            agent_vars = {
                **common_vars,
                "recipient_name": collection.owner.first_name,
                "visitor_name": collection.visitor_name or "An anonymous visitor"
            }

            db.add(ScheduledEmail(
                recipient_email=collection.owner.email,
                subject=f"Showcase Updated: {collection.visitor_name or 'Visitor'} - {collection.name}",
                template_name="new_properties_synced_agent",
                template_variables=agent_vars,
                status="PENDING",
                scheduled_for=datetime.now(timezone.utc)
            ))

            # 3. Create In-App Notification for Agent
            try:
                # Build summary message
                parts = []
                if new_count > 0: parts.append(f"{new_count} new property{'ies' if new_count > 1 else ''}")
                if drop_count > 0: parts.append(f"{drop_count} price drop{'s' if drop_count > 1 else ''}")
                summary = " and ".join(parts)
                
                # Fetch internal property ID for linking
                prop_id = None
                featured_key = str(featured.get('listing_key'))
                prop_res = await db.execute(select(Property.id).where(Property.listing_key == featured_key))
                prop_id = prop_res.scalar()

                notification = Notification(
                    agent_id=collection.owner_id,
                    type="PROPERTY_SYNC_UPDATE",
                    reference_type="VISITOR",
                    reference_id=collection.id,
                    title=f"Showcase Updated: {collection.visitor_name or 'Visitor'}",
                    message=f"Found {summary} for {collection.name}.",
                    collection_id=collection.id,
                    collection_name=collection.name,
                    property_id=prop_id,
                    property_address=featured.get('address'),
                    visitor_name=collection.visitor_name,
                    link=f"/showcases?showcase={collection.id}" + (f"&property={prop_id}" if prop_id else ""),
                    is_read=False,
                    created_at=datetime.now(timezone.utc)
                )
                db.add(notification)
            except Exception as e:
                logger.error(f"Failed to create agent notification: {e}")

    async def populate_new_collection(self, db: AsyncSession, collection_id: str) -> Dict[str, Any]:
        """Initial population for newly created collections using standardized utility"""
        from app.services.collections_service import CollectionsService
        try:
            result = await CollectionsService.repopulate_collection_from_preferences(
                db, collection_id, commit=True
            )
            return {
                'success': result['success'],
                'new_properties_added': result.get('new_links_created', 0),
                'error': result.get('error')
            }
        except Exception as e:
            logger.error(f"Failed to populate collection {collection_id}: {e}")
            return {'success': False, 'error': str(e)}

