from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, insert, delete, text, func, update
from sqlalchemy.orm import selectinload, joinedload, load_only
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
import uuid
import secrets
import string
import os

from app.models.database import (
    Collection, Property, User, PropertyInteraction, 
    PropertyComment, PropertyTour, collection_properties,
    Notification, ScheduledEmail
)
from app.schemas.collection import CollectionCreate
from app.config.logging import get_logger

from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.blacklist_service import BlacklistService

logger = get_logger(__name__)

class CollectionsService:

    @staticmethod
    def _calculate_dom(mls_list_date: Optional[datetime]) -> Optional[int]:
        """Calculates days on market based on now and mls_list_date."""
        if not mls_list_date:
            return None
        
        now = datetime.now(timezone.utc)
        list_date = mls_list_date
        if list_date.tzinfo is None:
            list_date = list_date.replace(tzinfo=timezone.utc)
        
        delta = now - list_date
        return max(0, delta.days)

    @staticmethod
    async def count_active_collections(db: AsyncSession, user_id: str) -> int:
        """Count the number of active collections for a user"""
        try:
            result = await db.execute(
                select(func.count(Collection.id)).where(
                    and_(
                        Collection.owner_id == user_id,
                        Collection.status == 'ACTIVE'
                    )
                )
            )
            return result.scalar() or 0
        except Exception as e:
            logger.error("Failed to count active collections", extra={"error": str(e)})
            return 0

    @staticmethod
    async def should_create_as_active(db: AsyncSession, user_id: str) -> bool:
        """Check if a new collection should be created as active (unlimited)"""
        return True

    @staticmethod
    async def can_activate_collection(db: AsyncSession, user_id: str, collection_id: str) -> bool:
        """Check if a collection can be activated (unlimited)"""
        return True

    @staticmethod
    async def get_user_collections(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Get all collections for a user with total and active property counts"""
        try:
            query = (
                select(Collection)
                .options(
                    selectinload(Collection.preferences),
                    selectinload(Collection.properties),
                    selectinload(Collection.original_open_house_event),
                    selectinload(Collection.property_interactions),
                )
                .where(Collection.owner_id == user_id)
                .order_by(Collection.created_at.desc())
            )

            result = await db.execute(query)
            collections = result.scalars().all()

            collections_data = []
            for collection in collections:
                # Calculate total vs active (non-disliked) properties
                total_count = len(collection.properties) if collection.properties else 0
                
                disliked_property_ids = {
                    interaction.property_id 
                    for interaction in collection.property_interactions 
                    if interaction.disliked
                }
                
                active_count = 0
                new_listings_count = 0
                
                # A property is "new" if mls_list_date > last_agent_dismissed_at (fallback to created_at)
                dismiss_reference = collection.last_agent_dismissed_at or collection.created_at
                if dismiss_reference.tzinfo is None:
                    dismiss_reference = dismiss_reference.replace(tzinfo=timezone.utc)

                if collection.properties:
                    for p in collection.properties:
                        if p.id not in disliked_property_ids:
                            active_count += 1
                        
                        if p.mls_list_date:
                            mls_date = p.mls_list_date
                            if mls_date.tzinfo is None:
                                mls_date = mls_date.replace(tzinfo=timezone.utc)
                            
                            if mls_date > dismiss_reference:
                                new_listings_count += 1

                original_property_data = None
                if collection.original_open_house_event_id:
                    try:
                        original_open_house = collection.original_open_house_event

                        if original_open_house:
                            # Extract property data from OpenHouseEvent metadata
                            original_property_data = {
                                "id": original_open_house.id,
                                "address": original_open_house.address or "Unknown Address",
                                "city": original_open_house.city,
                                "state": original_open_house.state,
                                "zipCode": original_open_house.zipcode,
                                "price": original_open_house.price,
                                "beds": original_open_house.bedrooms,
                                "baths": original_open_house.bathrooms,
                                "squareFeet": original_open_house.lot_size,  # Use lot_size since living_area doesn't exist
                                "propertyType": original_open_house.house_type or "Unknown"
                            }
                    except Exception as e:
                        pass

                preferences_data = {}
                if collection.preferences:
                    preferences_data = {
                        "min_beds": collection.preferences.min_beds,
                        "max_beds": collection.preferences.max_beds,
                        "min_baths": collection.preferences.min_baths,
                        "max_baths": collection.preferences.max_baths,
                        "min_price": collection.preferences.min_price,
                        "max_price": collection.preferences.max_price,
                        "min_year_built": collection.preferences.min_year_built,
                        "max_year_built": collection.preferences.max_year_built,
                        "lat": collection.preferences.lat,
                        "long": collection.preferences.long,
                        "address": collection.preferences.address,  # Add missing address field
                        "cities": collection.preferences.cities,
                        "townships": collection.preferences.townships,
                        "school_districts": collection.preferences.school_districts,
                        "diameter": collection.preferences.diameter,
                        "special_features": collection.preferences.special_features,
                        "visiting_reason": collection.preferences.visiting_reason,
                        "has_agent": collection.preferences.has_agent,

                        "is_town_house": collection.preferences.is_town_house,
                        "is_condo": collection.preferences.is_condo,
                        "is_single_family": collection.preferences.is_single_family,
                        "is_lot_land": collection.preferences.is_lot_land,
                        "is_multi_family": collection.preferences.is_multi_family,
                        "is_apartment": collection.preferences.is_apartment,
                        "is_commercial": collection.preferences.is_commercial,
                        "is_farm": collection.preferences.is_farm
                    }

                # Transform properties for this collection (similar to get_shared_collection)
                properties_data = []
                for prop in collection.properties:
                    property_dict = {
                        'id': prop.id,
                        'ListingKey': prop.listing_key,
                        'FullStreetAddress': prop.street_address or 'Unknown Address',
                        'City': prop.city,
                        'StateOrProvince': prop.state,
                        'PostalCode': prop.zipcode,
                        'ListPrice': prop.price,
                        'BedroomsTotal': prop.bedrooms,
                        'BathroomsTotal': prop.bathrooms,
                        'LivingArea': prop.living_area,
                        'LotSizeSquareFeet': prop.lot_size,
                        'PropertyType': prop.home_type,
                        'YearBuilt': prop.year_built,
                        'DaysOnMarket': CollectionsService._calculate_dom(prop.mls_list_date),
                        'AssociationYN': prop.has_association,
                        'ListPictureURL': prop.img_src,
                        'PublicRemarks': '',
                        'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                        'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                        'MlsStatus': prop.home_status,
                    }
                    properties_data.append(property_dict)

                # Convert to response format
                is_blacklisted = False
                if collection.visitor_email:
                    is_blacklisted = await BlacklistService.is_blacklisted(db, collection.visitor_email)
                    
                collection_data = {
                    "id": collection.id,
                    "name": collection.name,
                    "description": collection.description or "",
                    "status": collection.status or "ACTIVE",
                    "notify_visitor": collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                    "notify_agent": collection.notify_agent if hasattr(collection, 'notify_agent') else True,
                    "is_blacklisted": is_blacklisted,
                    "visitor_name": collection.visitor_name,
                    "visitor_email": collection.visitor_email,
                    "visitor_phone": collection.visitor_phone,
                    "original_property": original_property_data,
                    "preferences": preferences_data,
                    "matchedProperties": properties_data,  # Add actual properties data
                    "property_count": total_count,
                    "active_property_count": active_count,
                    "is_anonymous": collection.owner_id is None,
                    "is_public": bool(collection.is_public) if collection.is_public is not None else False,
                    "share_token": collection.share_token,
                    "created_at": collection.created_at.isoformat(),
                    "updated_at": collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                    "stats": {
                        "totalProperties": total_count,
                        "activeProperties": active_count,
                        "newProperties": new_listings_count,
                        "lastActivity": collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None,
                        "lastAgentDismissedAt": collection.last_agent_dismissed_at.isoformat() if collection.last_agent_dismissed_at else None
                    }
                }
                collections_data.append(collection_data)

            return collections_data

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            raise e

    @staticmethod
    async def dismiss_new_listings(
        db: AsyncSession,
        collection_id: str,
        user_id: str
    ) -> Dict[str, Any]:
        """Updates last_agent_dismissed_at to now for a collection."""
        try:
            query = select(Collection).where(
                and_(
                    Collection.id == collection_id,
                    Collection.owner_id == user_id
                )
            )
            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return {"success": False, "error": "Collection not found"}

            collection.last_agent_dismissed_at = datetime.now(timezone.utc)
            await db.commit()

            return {"success": True}

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "error": str(e)}

    @staticmethod
    async def get_collection_by_id(
        db: AsyncSession, 
        collection_id: str, 
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get a specific collection by ID with total and active property counts"""
        try:
            query = select(Collection).options(
                selectinload(Collection.preferences),
                selectinload(Collection.properties),
                selectinload(Collection.property_interactions)
            ).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return None

            # Calculate total vs active (non-disliked) properties
            total_count = len(collection.properties) if collection.properties else 0
            
            disliked_property_ids = {
                interaction.property_id 
                for interaction in collection.property_interactions 
                if interaction.disliked
            }

            # Transform properties to frontend format (similar to get_shared_collection)
            properties = []
            active_count = 0
            new_listings_count = 0

            # A property is "new" if mls_list_date > last_agent_dismissed_at (fallback to created_at)
            dismiss_reference = collection.last_agent_dismissed_at or collection.created_at
            if dismiss_reference.tzinfo is None:
                dismiss_reference = dismiss_reference.replace(tzinfo=timezone.utc)

            for prop in collection.properties:
                if prop.id not in disliked_property_ids:
                    active_count += 1
                
                if prop.mls_list_date:
                    mls_date = prop.mls_list_date
                    if mls_date.tzinfo is None:
                        mls_date = mls_date.replace(tzinfo=timezone.utc)
                    
                    if mls_date > dismiss_reference:
                        new_listings_count += 1

                property_dict = {
                    'id': prop.id,
                    'ListingKey': prop.listing_key,
                    'FullStreetAddress': prop.street_address or 'Unknown Address',
                    'City': prop.city,
                    'StateOrProvince': prop.state,
                    'PostalCode': prop.zipcode,
                    'ListPrice': prop.price,
                    'BedroomsTotal': prop.bedrooms,
                    'BathroomsTotal': prop.bathrooms,
                    'LivingArea': prop.living_area,
                    'LotSizeSquareFeet': prop.lot_size,
                    'PropertyType': prop.home_type,
                    'YearBuilt': prop.year_built,
                    'DaysOnMarket': CollectionsService._calculate_dom(prop.mls_list_date),
                    'AssociationYN': prop.has_association,
                    'ListPictureURL': prop.img_src,
                    'PublicRemarks': '',
                    'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                    'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                    'MlsStatus': prop.home_status,                }
                properties.append(property_dict)

            # Get preferences data if available
            preferences_data = {}
            if collection.preferences:
                preferences_data = {
                    "min_beds": collection.preferences.min_beds,
                    "max_beds": collection.preferences.max_beds,
                    "min_baths": collection.preferences.min_baths,
                    "max_baths": collection.preferences.max_baths,
                    "min_price": collection.preferences.min_price,
                    "max_price": collection.preferences.max_price,
                    "min_year_built": collection.preferences.min_year_built,
                    "max_year_built": collection.preferences.max_year_built,
                    "lat": collection.preferences.lat,
                    "long": collection.preferences.long,
                    "address": collection.preferences.address,  # Add missing address field
                    "cities": collection.preferences.cities,
                    "townships": collection.preferences.townships,
                    "school_districts": collection.preferences.school_districts,
                    "diameter": collection.preferences.diameter,
                    "special_features": collection.preferences.special_features,
                    "visiting_reason": collection.preferences.visiting_reason,
                    "has_agent": collection.preferences.has_agent,

                    "is_town_house": collection.preferences.is_town_house,
                    "is_condo": collection.preferences.is_condo,
                    "is_single_family": collection.preferences.is_single_family,
                    "is_lot_land": collection.preferences.is_lot_land,
                    "is_multi_family": collection.preferences.is_multi_family,
                    "is_apartment": collection.preferences.is_apartment,
                    "is_commercial": collection.preferences.is_commercial,
                    "is_farm": collection.preferences.is_farm
                }

            is_blacklisted = False
            if collection.visitor_email:
                is_blacklisted = await BlacklistService.is_blacklisted(db, collection.visitor_email)

            return {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description or "",
                "visitor_name": collection.visitor_name,
                "visitor_email": collection.visitor_email,
                "visitor_phone": collection.visitor_phone,
                "preferences": preferences_data,
                "properties": properties,
                "property_count": total_count,
                "active_property_count": active_count,
                "is_anonymous": collection.owner_id is None,
                "is_public": collection.is_public or False,
                "share_token": collection.share_token,
                "status": collection.status,
                "notify_visitor": collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                "notify_agent": collection.notify_agent if hasattr(collection, 'notify_agent') else True,
                "is_blacklisted": is_blacklisted,
                "created_at": collection.created_at.isoformat(),
                "updated_at": collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                "stats": {
                    "totalProperties": total_count,
                    "activeProperties": active_count,
                    "newProperties": new_listings_count,
                    "lastActivity": collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None,
                    "lastAgentDismissedAt": collection.last_agent_dismissed_at.isoformat() if collection.last_agent_dismissed_at else None
                }
            }

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            raise e

    @staticmethod
    async def create_collection(
        db: AsyncSession,
        collection_data: CollectionCreate,
        user_id: str
    ) -> Dict[str, Any]:
        """Create a new collection with intelligent status assignment"""
        try:
            # Determine if this collection should be active or inactive based on current count
            should_be_active = await CollectionsService.should_create_as_active(db, user_id)
            status = 'ACTIVE' if should_be_active else 'INACTIVE'

            active_count = await CollectionsService.count_active_collections(db, user_id)

            # Generate share token for public access
            share_token = CollectionsService.generate_share_token()

            collection = Collection(
                id=str(uuid.uuid4()),
                name=collection_data.name,
                description=collection_data.description,
                owner_id=user_id,
                is_public=collection_data.is_public if collection_data.is_public is not None else True,
                share_token=share_token,
                status=status,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )

            db.add(collection)
            await db.commit()
            await db.refresh(collection)

            try:
                preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection.id)

                if preferences:
                    # Initial population for newly created collections
                    await CollectionsService.repopulate_collection_from_preferences(db, collection.id, commit=True)

            except Exception as e:
                # Collection creation should still succeed even if property population fails
                pass

            return {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description or "",
                "visitor_name": collection.visitor_name,
                "visitor_email": collection.visitor_email,
                "visitor_phone": collection.visitor_phone,
                "preferences": {},  # No preferences relationship created yet
                "property_count": 0,
                "is_anonymous": False,
                "is_public": collection.is_public,
                "share_token": collection.share_token,
                "status": collection.status,
                "notify_visitor": collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                "notify_agent": collection.notify_agent if hasattr(collection, 'notify_agent') else True,
                "created_at": collection.created_at.isoformat(),
                "updated_at": collection.updated_at.isoformat(),
                "stats": {
                    "totalProperties": 0,
                    "activeProperties": 0,
                    "lastActivity": None
                }
            }

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def update_collection_status(
        db: AsyncSession,
        collection_id: str,
        user_id: str,
        status: str
    ) -> bool:
        """Update collection status"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            old_status = collection.status

            # Update the status column directly
            collection.status = status
            collection.updated_at = datetime.now(timezone.utc)

            await db.commit()

            # If reactivating, catch up on missed properties that were listed while inactive
            if status == 'ACTIVE' and old_status != 'ACTIVE':
                try:
                    await CollectionsService.repopulate_collection_from_preferences(db, collection_id, commit=True)
                except Exception as e:
                    logger.error(f"Failed to repopulate collection {collection_id} after reactivation: {e}")
                    # We don't fail the status update if repopulation fails

            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def update_collection_notifications(
        db: AsyncSession,
        collection_id: str,
        user_id: str,
        notify_visitor: bool,
        notify_agent: bool
    ) -> bool:
        """Update collection notification settings"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            # Update the notification columns directly
            collection.notify_visitor = notify_visitor
            collection.notify_agent = notify_agent
            collection.updated_at = datetime.now(timezone.utc)

            await db.commit()
            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def update_shared_collection_notifications(
        db: AsyncSession,
        share_token: str,
        notify_visitor: bool
    ) -> bool:
        """Update collection notification settings via share token (for visitors)"""
        try:
            query = select(Collection).where(
                Collection.share_token == share_token
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            # Update the visitor notification column
            collection.notify_visitor = notify_visitor
            collection.updated_at = datetime.now(timezone.utc)

            await db.commit()
            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    def generate_share_token() -> str:
        """Generate a unique, secure share token"""
        # Generate a 12-character random string with letters and numbers
        alphabet = string.ascii_letters + string.digits
        random_part = ''.join(secrets.choice(alphabet) for _ in range(12))
        return f"coll-{random_part}"

    @staticmethod
    async def toggle_share_status(
        db: AsyncSession,
        collection_id: str,
        user_id: str,
        make_public: bool,
        force_regenerate: bool = False
    ) -> Dict[str, Any]:
        """Toggle collection share status and generate/revoke share token"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return {"success": False, "message": "Collection not found"}

            if make_public:
                # Generate new share token if making public or forced regeneration
                if not collection.share_token or force_regenerate:
                    # Ensure the token is unique
                    while True:
                        new_token = CollectionsService.generate_share_token()
                        # Check if token already exists
                        token_check = await db.execute(
                            select(Collection).where(Collection.share_token == new_token)
                        )
                        if not token_check.scalar_one_or_none():
                            collection.share_token = new_token
                            break

                collection.is_public = True
                share_url = f"/collection/{collection.share_token}"
                message = "Collection is now public and shareable"
            else:
                # Make private but keep the share token for potential future use
                collection.is_public = False
                share_url = None
                message = "Collection is now private"

            collection.updated_at = datetime.now(timezone.utc)
            await db.commit()

            return {
                "success": True,
                "message": message,
                "is_public": collection.is_public,
                "share_token": collection.share_token if collection.is_public else None,
                "share_url": share_url
            }

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def get_shared_collection(
        db: AsyncSession,
        share_token: str
    ) -> Optional[Dict[str, Any]]:
        """Get a shared collection by share token (for anonymous access)"""
        try:
            # Query collection with properties and preferences, ensure it's public
            query = (
                select(Collection)
                .options(
                    selectinload(Collection.properties),
                    joinedload(Collection.preferences)
                )
                .where(
                    and_(
                        Collection.share_token == share_token,
                        Collection.is_public == True
                    )
                )
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                # Let's also check if collection exists but is not public
                debug_query = select(Collection).where(Collection.share_token == share_token)
                debug_result = await db.execute(debug_query)
                debug_collection = debug_result.scalar_one_or_none()
                if debug_collection:
                    pass
                else:
                    pass
                return None

            if collection.preferences:
                pass
            else:
                # Manual query for preferences to debug the issue
                from app.models.database import CollectionPreferences
                prefs_query = select(CollectionPreferences).where(CollectionPreferences.collection_id == collection.id)
                prefs_result = await db.execute(prefs_query)
                manual_prefs = prefs_result.scalar_one_or_none()
                if manual_prefs:
                    # Use the manually loaded preferences
                    collection.preferences = manual_prefs

            # Get all property IDs for batch querying interactions
            property_ids = [prop.id for prop in collection.properties]

            # Initialize lookup dictionaries
            interactions_lookup = {}
            comments_lookup = {}
            tours_lookup = {}
            added_at_lookup = {}

            # Only query if we have properties
            if property_ids:
                # Fetch added_at timestamps for each property from collection_properties
                added_at_query = select(
                    collection_properties.c.property_id,
                    collection_properties.c.added_at
                ).where(
                    and_(
                        collection_properties.c.collection_id == collection.id,
                        collection_properties.c.property_id.in_(property_ids)
                    )
                )
                added_at_result = await db.execute(added_at_query)
                added_at_data = added_at_result.all()

                # Create lookup dictionary for added_at timestamps
                for row in added_at_data:
                    added_at_lookup[row.property_id] = row.added_at

            # Calculate "new" threshold (properties added in last 7 days)
            now = datetime.now(timezone.utc)
            new_threshold_days = int(os.getenv("NEW_PROPERTY_DAYS", "7"))
            new_threshold = now - timedelta(days=new_threshold_days)

            # Only query if we have properties
            if property_ids:
                # Fetch all interactions for this collection in a single query
                interactions_query = select(PropertyInteraction).where(
                    and_(
                        PropertyInteraction.collection_id == collection.id,
                        PropertyInteraction.property_id.in_(property_ids)
                    )
                )

                interactions_result = await db.execute(interactions_query)
                interactions = interactions_result.scalars().all()

                # Create lookup dictionary for interactions by property_id
                for interaction in interactions:
                    interactions_lookup[interaction.property_id] = interaction

                # Fetch all comments for this collection in a single query
                comments_query = select(PropertyComment).where(
                    and_(
                        PropertyComment.collection_id == collection.id,
                        PropertyComment.property_id.in_(property_ids)
                    )
                )

                comments_result = await db.execute(comments_query)
                comments = comments_result.scalars().all()

                # Create lookup dictionary for comments by property_id
                for comment in comments:
                    if comment.property_id not in comments_lookup:
                        comments_lookup[comment.property_id] = []
                    comment_dict = {
                        'id': comment.id,
                        'content': comment.content,
                        'author': comment.visitor_name or 'Anonymous',
                        'createdAt': comment.created_at.isoformat()
                    }
                    comments_lookup[comment.property_id].append(comment_dict)

                # Fetch all tours for this collection in a single query
                tours_query = select(PropertyTour).where(
                    and_(
                        PropertyTour.collection_id == collection.id,
                        PropertyTour.property_id.in_(property_ids)
                    )
                )

                tours_result = await db.execute(tours_query)
                tours = tours_result.scalars().all()

                # Create lookup dictionary for tours by property_id (just track existence)
                for tour in tours:
                    tours_lookup[tour.property_id] = True

            # Transform to response format similar to get_user_collections
            properties_data = []
            for prop in collection.properties:
                property_dict = {
                    'id': prop.id,
                    'ListingKey': prop.listing_key,
                    'FullStreetAddress': prop.street_address or 'Unknown Address',
                    'City': prop.city,
                    'StateOrProvince': prop.state,
                    'PostalCode': prop.zipcode,
                    'ListPrice': prop.price,
                    'BedroomsTotal': prop.bedrooms,
                    'BathroomsTotal': prop.bathrooms,
                    'LivingArea': prop.living_area,
                    'LotSizeSquareFeet': prop.lot_size,
                    'PropertyType': prop.home_type,
                    'YearBuilt': prop.year_built,
                    'DaysOnMarket': CollectionsService._calculate_dom(prop.mls_list_date),
                    'AssociationYN': prop.has_association,
                    'ListPictureURL': prop.img_src,
                    'PublicRemarks': '',
                    'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                    'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                    'MlsStatus': prop.home_status,                    # Real interaction data from database
                    'liked': interactions_lookup[prop.id].liked if prop.id in interactions_lookup else False,
                    'disliked': interactions_lookup[prop.id].disliked if prop.id in interactions_lookup else False,
                    'viewed': prop.id in interactions_lookup,  # True if any interaction exists
                    'viewCount': interactions_lookup[prop.id].view_count if prop.id in interactions_lookup else 0,
                    'lastViewedAt': interactions_lookup[prop.id].last_viewed_at.isoformat() if prop.id in interactions_lookup and interactions_lookup[prop.id].last_viewed_at else None,
                    'comments': comments_lookup.get(prop.id, []),
                    # Tour status
                    'hasTourScheduled': tours_lookup.get(prop.id, False)
                }
                properties_data.append(property_dict)

            # Calculate stats
            total_properties = len(properties_data)
            viewed_properties = sum(1 for prop in properties_data if prop.get('viewCount', 0) > 0)
            liked_properties = sum(1 for prop in properties_data if prop.get('liked', False))

            is_blacklisted = False
            if collection.visitor_email:
                is_blacklisted = await BlacklistService.is_blacklisted(db, collection.visitor_email)

            collection_data = {
                'id': collection.id,
                'name': collection.name,
                'customer': {
                    'firstName': collection.visitor_name.split(' ')[0] if collection.visitor_name else 'Anonymous',
                    'lastName': collection.visitor_name.split(' ')[-1] if collection.visitor_name and ' ' in collection.visitor_name else 'Visitor',
                    'email': collection.visitor_email or 'anonymous@visitor.com',
                    'phone': collection.visitor_phone or 'N/A',
                    'preferredContact': 'EMAIL',
                    'is_blacklisted': is_blacklisted
                },
                'matchedProperties': properties_data,
                'createdAt': collection.created_at.isoformat(),
                'updatedAt': collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                'status': collection.status or 'ACTIVE',
                'notifyVisitor': collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                'preferences': {
                    'min_beds': collection.preferences.min_beds,
                    'max_beds': collection.preferences.max_beds,
                    'min_baths': collection.preferences.min_baths,
                    'max_baths': collection.preferences.max_baths,
                    'min_price': collection.preferences.min_price,
                    'max_price': collection.preferences.max_price,
                    'min_year_built': collection.preferences.min_year_built,
                    'max_year_built': collection.preferences.max_year_built,
                    'lat': collection.preferences.lat,
                    'long': collection.preferences.long,
                    'address': collection.preferences.address,  # Add missing address field
                    'cities': collection.preferences.cities,
                    'townships': collection.preferences.townships,
                    'school_districts': collection.preferences.school_districts,
                    'diameter': collection.preferences.diameter,
                    'special_features': collection.preferences.special_features,
                    'visiting_reason': collection.preferences.visiting_reason,
                    'has_agent': collection.preferences.has_agent,

                    'is_town_house': collection.preferences.is_town_house,
                    'is_condo': collection.preferences.is_condo,
                    'is_single_family': collection.preferences.is_single_family,
                    'is_lot_land': collection.preferences.is_lot_land,
                    'is_multi_family': collection.preferences.is_multi_family,
                    'is_apartment': collection.preferences.is_apartment,
                    'is_commercial': collection.preferences.is_commercial,
                    'is_farm': collection.preferences.is_farm
                } if collection.preferences else {},
                'stats': {
                    'totalProperties': total_properties,
                    'viewedProperties': viewed_properties,
                    'likedProperties': liked_properties,
                    'lastActivity': collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None
                },
                'shareToken': collection.share_token,
                'isPublic': collection.is_public
            }

            return collection_data

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            raise e

    @staticmethod
    async def repopulate_collection_from_preferences(
        db: AsyncSession,
        collection_id: str,
        commit: bool = True
    ) -> Dict[str, Any]:
        """
        Standardized utility to sync a collection's properties with its preferences.
        Performs an EXCLUSIVE refresh:
        1. Queries local database for matching properties.
        2. Identifies 'Protected' properties (liked, commented, toured).
        3. Removes stale links (those that don't match AND aren't protected).
        4. Bulk inserts new matching links.
        """
        # Local import to avoid circular dependency
        from app.services.property_service import property_service

        try:
            # 1. Get current preferences
            preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
            if not preferences:
                return {'success': False, 'error': 'Preferences not found'}

            # 2. Fetch new matching properties from the local database
            matching_properties = await property_service.get_properties_by_preferences(db, preferences)
            new_match_ids = {p.id for p in matching_properties}

            # 3. Identify IDs of properties to PROTECT (those with user interactions)
            # We don't want to remove properties the user has already engaged with
            keep_query = select(collection_properties.c.property_id).where(
                collection_properties.c.collection_id == collection_id
            ).where(
                or_(
                    collection_properties.c.property_id.in_(select(PropertyInteraction.property_id).where(PropertyInteraction.collection_id == collection_id)),
                    collection_properties.c.property_id.in_(select(PropertyComment.property_id).where(PropertyComment.collection_id == collection_id)),
                    collection_properties.c.property_id.in_(select(PropertyTour.property_id).where(PropertyTour.collection_id == collection_id))
                )
            )
            keep_ids_res = await db.execute(keep_query)
            protected_ids = {r[0] for r in keep_ids_res.fetchall()}
            
            # 4. Clear stale links (not a new match AND not protected)
            delete_stmt = delete(collection_properties).where(
                and_(
                    collection_properties.c.collection_id == collection_id,
                    collection_properties.c.property_id.notin_(list(new_match_ids)),
                    collection_properties.c.property_id.notin_(list(protected_ids))
                )
            )
            await db.execute(delete_stmt)
            
            # 5. Identify which new matches need to be inserted 
            # (skip those already linked/protected)
            existing_links_query = select(collection_properties.c.property_id).where(
                collection_properties.c.collection_id == collection_id
            )
            existing_res = await db.execute(existing_links_query)
            already_linked_ids = {r[0] for r in existing_res.fetchall()}
            
            ids_to_insert = new_match_ids - already_linked_ids
            
            # 6. Bulk Insert
            if ids_to_insert:
                insert_data = [
                    {"collection_id": collection_id, "property_id": p_id, "added_at": datetime.now(timezone.utc)}
                    for p_id in ids_to_insert
                ]
                await db.execute(insert(collection_properties), insert_data)

            if commit:
                await db.commit()

            return {
                'success': True,
                'properties_found': len(new_match_ids),
                'new_links_created': len(ids_to_insert),
                'protected_count': len(protected_ids)
            }

        except Exception as e:
            logger.error(f"Failed to repopulate collection {collection_id}: {e}", exc_info=True)
            if commit:
                await db.rollback()
            return {'success': False, 'error': str(e)}

    @staticmethod
    async def delete_collection(
        db: AsyncSession,
        collection_id: str,
        user_id: str
    ) -> bool:
        """Delete a collection and clean up orphaned properties"""
        try:
            # First, get the collection with its properties
            query = select(Collection).options(
                selectinload(Collection.properties)
            ).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False


            # Get all property IDs in this collection
            property_ids_in_collection = [prop.id for prop in collection.properties]

            # Find properties that will become orphaned after this collection is deleted
            orphaned_properties = []

            if property_ids_in_collection:

                for property_id in property_ids_in_collection:
                    # Count how many other collections this property belongs to
                    count_query = text("""
                        SELECT COUNT(*)
                        FROM collection_properties
                        WHERE property_id = :property_id AND collection_id != :collection_id
                    """)

                    count_result = await db.execute(count_query, {
                        "property_id": property_id,
                        "collection_id": collection_id
                    })
                    other_collection_count = count_result.scalar()

                    # If this property has no other collection associations, it will be orphaned
                    if other_collection_count == 0:
                        orphaned_properties.append(property_id)


            # Delete the collection (this will cascade delete the collection_properties relationships)
            await db.delete(collection)

            # Clean up orphaned properties and their dependencies
            if orphaned_properties:

                # Delete PropertyInteractions for orphaned properties
                interactions_result = await db.execute(
                    delete(PropertyInteraction).where(PropertyInteraction.property_id.in_(orphaned_properties))
                )

                # Delete PropertyComments for orphaned properties
                comments_result = await db.execute(
                    delete(PropertyComment).where(PropertyComment.property_id.in_(orphaned_properties))
                )

                # Delete the orphaned properties themselves
                properties_result = await db.execute(
                    delete(Property).where(Property.id.in_(orphaned_properties))
                )

            await db.commit()

            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e


    @staticmethod
    async def get_properties(
        collectionId: str,
        db: AsyncSession
    ):
        try:
            # Get all the properties that have a relation to the collectionId
            query = (
                select(Collection)
                .options(selectinload(Collection.properties))
                .where(Collection.id == collectionId)
            )
            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return []

            property_ids = [prop.id for prop in collection.properties]

            # Fetch added_at timestamps for each property from collection_properties
            added_at_query = select(
                collection_properties.c.property_id,
                collection_properties.c.added_at
            ).where(
                and_(
                    collection_properties.c.collection_id == collectionId,
                    collection_properties.c.property_id.in_(property_ids)
                )
            )
            added_at_result = await db.execute(added_at_query)
            added_at_data = added_at_result.all()

            # Create lookup dictionary for added_at timestamps
            added_at_lookup = {}
            for row in added_at_data:
                added_at_lookup[row.property_id] = row.added_at

            # Calculate "new" threshold (properties added in last 7 days)
            now = datetime.now(timezone.utc)
            new_threshold_days = int(os.getenv("NEW_PROPERTY_DAYS", "7"))
            new_threshold = now - timedelta(days=new_threshold_days)

            interactions_query = select(PropertyInteraction).where(
                and_(
                    PropertyInteraction.collection_id == collectionId,
                    PropertyInteraction.property_id.in_(property_ids)
                )
            )

            interactions_result = await db.execute(interactions_query)
            interactions = interactions_result.scalars().all()

            # Create lookup dictionary for interactions by property_id
            interactions_lookup = {}
            for interaction in interactions:
                interactions_lookup[interaction.property_id] = interaction

            # Fetch all comments for this collection in a single query
            comments_query = select(PropertyComment).where(
                and_(
                    PropertyComment.collection_id == collectionId,
                    PropertyComment.property_id.in_(property_ids)
                )
            ).order_by(PropertyComment.created_at.asc())

            comments_result = await db.execute(comments_query)
            comments = comments_result.scalars().all()

            # Create lookup dictionary for comments by property_id
            comments_lookup = {}
            for comment in comments:
                if comment.property_id not in comments_lookup:
                    comments_lookup[comment.property_id] = []
                comments_lookup[comment.property_id].append({
                    'id': comment.id,
                    'author': comment.visitor_name or 'Anonymous',
                    'content': comment.content,
                    'createdAt': comment.created_at.isoformat()
                })

            # Fetch tour counts for each property in this collection
            tours_query = select(
                PropertyTour.property_id,
                func.count(PropertyTour.id).label('tour_count')
            ).where(
                and_(
                    PropertyTour.collection_id == collectionId,
                    PropertyTour.property_id.in_(property_ids)
                )
            ).group_by(PropertyTour.property_id)

            tours_result = await db.execute(tours_query)
            tours_data = tours_result.all()

            # Create lookup dictionary for tour counts by property_id
            tours_lookup = {}
            for tour_row in tours_data:
                tours_lookup[tour_row.property_id] = tour_row.tour_count

            properties_data = []
            for prop in collection.properties:
                added_at = added_at_lookup.get(prop.id)
                is_new = False
                if added_at:
                    # Make added_at timezone-aware if it isn't already
                    if added_at.tzinfo is None:
                        added_at = added_at.replace(tzinfo=timezone.utc)
                    is_new = added_at >= new_threshold

                property_dict = {
                    'id': prop.id,
                    'ListingKey': prop.listing_key,
                    'FullStreetAddress': prop.street_address or 'Unknown Address',
                    'City': prop.city,
                    'StateOrProvince': prop.state,
                    'PostalCode': prop.zipcode,
                    'ListPrice': prop.price,
                    'BedroomsTotal': prop.bedrooms,
                    'BathroomsTotal': prop.bathrooms,
                    'LivingArea': prop.living_area,
                    'LotSizeSquareFeet': prop.lot_size,
                    'PropertyType': prop.home_type,
                    'YearBuilt': prop.year_built,
                    'DaysOnMarket': CollectionsService._calculate_dom(prop.mls_list_date),
                    'AssociationYN': prop.has_association,
                    'ListPictureURL': prop.img_src,
                    'PublicRemarks': '',
                    'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                    'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                    'MlsStatus': prop.home_status,                    'liked': interactions_lookup[prop.id].liked if prop.id in interactions_lookup else False,
                    'disliked': interactions_lookup[prop.id].disliked if prop.id in interactions_lookup else False,
                    'viewed': prop.id in interactions_lookup,  # True if any interaction exists
                    'viewCount': interactions_lookup[prop.id].view_count if prop.id in interactions_lookup else 0,
                    'lastViewedAt': interactions_lookup[prop.id].last_viewed_at.isoformat() if prop.id in interactions_lookup and interactions_lookup[prop.id].last_viewed_at else None,
                    'comments': comments_lookup.get(prop.id, []),
                    'tourCount': tours_lookup.get(prop.id, 0),
                    'added_at': added_at.isoformat() if added_at else None,
                    'is_new': is_new
                }
                properties_data.append(property_dict)

            return properties_data
        except Exception as e:
            return []


