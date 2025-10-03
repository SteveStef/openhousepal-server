from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete, text, func
from sqlalchemy.orm import selectinload, joinedload, load_only
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid
import secrets
import string
import os

from app.models.database import Collection, Property, User, PropertyInteraction, PropertyComment
from app.schemas.collection import CollectionCreate

from app.services.property_sync_service import PropertySyncService
from app.services.collection_preferences_service import CollectionPreferencesService

class CollectionsService:

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
            print(f"Error counting active collections: {e}")
            return 0

    @staticmethod
    async def should_create_as_active(db: AsyncSession, user_id: str) -> bool:
        """Check if a new collection should be created as active (under the limit)"""
        max_active = int(os.getenv("MAX_ACTIVE_COLLECTIONS_PER_USER", "10"))
        active_count = await CollectionsService.count_active_collections(db, user_id)
        return active_count < max_active

    @staticmethod
    async def can_activate_collection(db: AsyncSession, user_id: str, collection_id: str) -> bool:
        """Check if a collection can be activated without exceeding the limit"""
        max_active = int(os.getenv("MAX_ACTIVE_COLLECTIONS_PER_USER", "10"))

        # Get current collection status
        current_collection = await db.execute(
            select(Collection.status).where(
                and_(
                    Collection.id == collection_id,
                    Collection.owner_id == user_id
                )
            )
        )
        current_status = current_collection.scalar()

        # If already active, allow the "activation" (no change)
        if current_status == 'ACTIVE':
            return True

        # If inactive, check if activating would exceed limit
        active_count = await CollectionsService.count_active_collections(db, user_id)
        return active_count < max_active

    @staticmethod
    async def get_user_collections(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Get all collections for a user"""
        try:
            query = (
                select(Collection)
                .options(
                    selectinload(Collection.preferences),
                    selectinload(Collection.properties),
                    selectinload(Collection.original_open_house_event),  # no load_only here
                )
                .where(Collection.owner_id == user_id)
                .order_by(Collection.created_at.desc())
            )

            result = await db.execute(query)
            collections = result.scalars().all()

            collections_data = []
            for collection in collections:
                property_count = len(collection.properties) if collection.properties else 0
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
                        print(f"Warning: Could not fetch original property data for collection {collection.id}: {e}")

                preferences_data = {}
                if collection.preferences:
                    preferences_data = {
                        "min_beds": collection.preferences.min_beds,
                        "max_beds": collection.preferences.max_beds,
                        "min_baths": collection.preferences.min_baths,
                        "max_baths": collection.preferences.max_baths,
                        "min_price": collection.preferences.min_price,
                        "max_price": collection.preferences.max_price,
                        "lat": collection.preferences.lat,
                        "long": collection.preferences.long,
                        "address": collection.preferences.address,  # Add missing address field
                        "cities": collection.preferences.cities,
                        "townships": collection.preferences.townships,
                        "diameter": collection.preferences.diameter,
                        "special_features": collection.preferences.special_features,
                        "timeframe": collection.preferences.timeframe,
                        "visiting_reason": collection.preferences.visiting_reason,
                        "has_agent": collection.preferences.has_agent,

                        "is_town_house": collection.preferences.is_town_house,
                        "is_condo": collection.preferences.is_condo,
                        "is_single_family": collection.preferences.is_single_family,
                        "is_lot_land": collection.preferences.is_lot_land,
                        "is_multi_family": collection.preferences.is_multi_family,
                        "is_apartment": collection.preferences.is_apartment
                    }

                # Transform properties for this collection (similar to get_shared_collection)
                properties_data = []
                for prop in collection.properties:
                    property_dict = {
                        'id': prop.id,
                        'address': prop.street_address or 'Unknown Address',
                        'city': prop.city,
                        'state': prop.state,
                        'zipCode': prop.zipcode,
                        'price': prop.price,
                        'beds': prop.bedrooms,
                        'baths': prop.bathrooms,
                        'squareFeet': prop.living_area,
                        'lotSize': prop.lot_size,
                        'propertyType': prop.home_type,
                        'imageUrl': prop.img_src,
                        'description': '',
                        'listingUpdated': prop.updated_at.isoformat() if prop.updated_at else None,
                        'status': prop.home_status,
                    }
                    properties_data.append(property_dict)

                # Convert to response format  
                collection_data = {
                    "id": collection.id,
                    "name": collection.name,
                    "description": collection.description or "",
                    "visitor_name": collection.visitor_name,
                    "visitor_email": collection.visitor_email,
                    "visitor_phone": collection.visitor_phone,
                    "original_property": original_property_data,
                    "preferences": preferences_data,
                    "matchedProperties": properties_data,  # Add actual properties data
                    "property_count": property_count,
                    "is_anonymous": collection.owner_id is None,
                    "is_public": bool(collection.is_public) if collection.is_public is not None else False,
                    "share_token": collection.share_token,
                    "created_at": collection.created_at.isoformat(),
                    "updated_at": collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat()
                }
                collections_data.append(collection_data)

            return collections_data

        except Exception as e:
            print(f"Error fetching user collections: {e}")
            raise e

    @staticmethod
    async def get_collection_by_id(
        db: AsyncSession, 
        collection_id: str, 
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get a specific collection by ID"""
        try:
            query = select(Collection).options(
                selectinload(Collection.preferences),
                selectinload(Collection.properties)
            ).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return None

            # Transform properties to frontend format (similar to get_shared_collection)
            properties = []
            for prop in collection.properties:
                property_dict = {
                    'address': prop.street_address or 'Unknown Address',
                    'city': prop.city,
                    'state': prop.state,
                    'zipCode': prop.zipcode,
                    'price': prop.price,
                    'beds': prop.bedrooms,
                    'baths': prop.bathrooms,
                    'squareFeet': prop.living_area,
                    'lotSize': prop.lot_size,
                    'propertyType': prop.home_type,
                    'imageUrl': prop.img_src,
                    'description': '',
                    'listingUpdated': prop.updated_at.isoformat() if prop.updated_at else None,
                    'status': prop.home_status,
                }
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
                    "lat": collection.preferences.lat,
                    "long": collection.preferences.long,
                    "address": collection.preferences.address,  # Add missing address field
                    "cities": collection.preferences.cities,
                    "townships": collection.preferences.townships,
                    "diameter": collection.preferences.diameter,
                    "special_features": collection.preferences.special_features,
                    "timeframe": collection.preferences.timeframe,
                    "visiting_reason": collection.preferences.visiting_reason,
                    "has_agent": collection.preferences.has_agent,

                    "is_town_house": collection.preferences.is_town_house,
                    "is_condo": collection.preferences.is_condo,
                    "is_single_family": collection.preferences.is_single_family,
                    "is_lot_land": collection.preferences.is_lot_land,
                    "is_multi_family": collection.preferences.is_multi_family,
                    "is_apartment": collection.preferences.is_apartment
                }

            return {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description or "",
                "visitor_name": collection.visitor_name,
                "visitor_email": collection.visitor_email,
                "visitor_phone": collection.visitor_phone,
                "preferences": preferences_data,
                "properties": properties,
                "property_count": len(properties),
                "is_anonymous": collection.owner_id is None,
                "is_public": collection.is_public or False,
                "share_token": collection.share_token,
                "created_at": collection.created_at.isoformat(),
                "updated_at": collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat()
            }

        except Exception as e:
            print(f"Error fetching collection by ID: {e}")
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
            print(f"[CREATE_COLLECTION] User {user_id} has {active_count} active collections, creating new collection as {status}")

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
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )

            db.add(collection)
            await db.commit()
            await db.refresh(collection)

            try:
                preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection.id)

                if preferences:
                    sync_service = PropertySyncService()
                    result = await sync_service.populate_new_collection(db, collection.id)

                    if result['success']:
                        print(f"Successfully populated collection {collection.id} with {result['new_properties_added']} properties")
                    else:
                        print(f"Warning: Failed to populate collection {collection.id} with properties: {result.get('error', 'Unknown error')}")

            except Exception as e:
                print(f"Warning: Failed to populate collection {collection.id} with properties: {e}")
                # Collection creation should still succeed even if property population fails

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
                "created_at": collection.created_at.isoformat(),
                "updated_at": collection.updated_at.isoformat()
            }

        except Exception as e:
            print(f"Error creating collection: {e}")
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

            # Update the status column directly
            collection.status = status
            collection.updated_at = datetime.utcnow()

            await db.commit()
            return True

        except Exception as e:
            print(f"Error updating collection status: {e}")
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

            collection.updated_at = datetime.utcnow()
            await db.commit()

            return {
                "success": True,
                "message": message,
                "is_public": collection.is_public,
                "share_token": collection.share_token if collection.is_public else None,
                "share_url": share_url
            }

        except Exception as e:
            print(f"Error toggling collection share status: {e}")
            await db.rollback()
            raise e

    @staticmethod
    async def get_shared_collection(
        db: AsyncSession,
        share_token: str
    ) -> Optional[Dict[str, Any]]:
        """Get a shared collection by share token (for anonymous access)"""
        try:
            print(f"[DEBUG SERVICE] Looking for collection with share_token: {share_token}")
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
                print(f"[DEBUG SERVICE] No collection found for token: {share_token}")
                # Let's also check if collection exists but is not public
                debug_query = select(Collection).where(Collection.share_token == share_token)
                debug_result = await db.execute(debug_query)
                debug_collection = debug_result.scalar_one_or_none()
                if debug_collection:
                    print(f"[DEBUG SERVICE] Collection exists but is_public={debug_collection.is_public}")
                else:
                    print(f"[DEBUG SERVICE] No collection exists with this token at all")
                return None

            print(f"[DEBUG SERVICE] Found collection: {collection.id} - {collection.name} - public: {collection.is_public}")
            print(f"[DEBUG SERVICE] Preferences loaded: {collection.preferences is not None}")
            if collection.preferences:
                print(f"[DEBUG SERVICE] Timeframe: {collection.preferences.timeframe}, Visiting reason: {collection.preferences.visiting_reason}")
            else:
                # Manual query for preferences to debug the issue
                from app.models.database import CollectionPreferences
                prefs_query = select(CollectionPreferences).where(CollectionPreferences.collection_id == collection.id)
                prefs_result = await db.execute(prefs_query)
                manual_prefs = prefs_result.scalar_one_or_none()
                print(f"[DEBUG SERVICE] Manual preferences query result: {manual_prefs is not None}")
                if manual_prefs:
                    print(f"[DEBUG SERVICE] Manual - Timeframe: {manual_prefs.timeframe}, Visiting reason: {manual_prefs.visiting_reason}")
                    # Use the manually loaded preferences
                    collection.preferences = manual_prefs

            # Get all property IDs for batch querying interactions
            property_ids = [prop.id for prop in collection.properties]

            # Initialize lookup dictionaries
            interactions_lookup = {}
            comments_lookup = {}

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

            # Transform to response format similar to get_user_collections
            properties_data = []
            for prop in collection.properties:
                property_dict = {
                    'id': prop.id,
                    'address': prop.street_address or 'Unknown Address',
                    'city': prop.city,
                    'state': prop.state,
                    'zipCode': prop.zipcode,
                    'price': prop.price,
                    'beds': prop.bedrooms,
                    'baths': prop.bathrooms,
                    'squareFeet': prop.living_area,
                    'lotSize': prop.lot_size,
                    'propertyType': prop.home_type,
                    'imageUrl': prop.img_src,
                    'description': '',
                    'listingUpdated': prop.updated_at.isoformat() if prop.updated_at else None,
                    'status': prop.home_status,
                    # Real interaction data from database
                    'liked': interactions_lookup[prop.id].liked if prop.id in interactions_lookup else False,
                    'disliked': interactions_lookup[prop.id].disliked if prop.id in interactions_lookup else False,
                    'favorited': interactions_lookup[prop.id].favorited if prop.id in interactions_lookup else False,
                    'viewed': prop.id in interactions_lookup,  # True if any interaction exists
                    'comments': comments_lookup.get(prop.id, [])
                }
                properties_data.append(property_dict)

            # Calculate stats
            total_properties = len(properties_data)

            collection_data = {
                'id': collection.id,
                'name': collection.name,
                'customer': {
                    'firstName': collection.visitor_name.split(' ')[0] if collection.visitor_name else 'Anonymous',
                    'lastName': collection.visitor_name.split(' ')[-1] if collection.visitor_name and ' ' in collection.visitor_name else 'Visitor',
                    'email': collection.visitor_email or 'anonymous@visitor.com',
                    'phone': collection.visitor_phone or 'N/A',
                    'preferredContact': 'EMAIL'
                },
                'matchedProperties': properties_data,
                'createdAt': collection.created_at.isoformat(),
                'updatedAt': collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                'status': collection.status or 'ACTIVE',
                'preferences': {
                    'min_beds': collection.preferences.min_beds,
                    'max_beds': collection.preferences.max_beds,
                    'min_baths': collection.preferences.min_baths,
                    'max_baths': collection.preferences.max_baths,
                    'min_price': collection.preferences.min_price,
                    'max_price': collection.preferences.max_price,
                    'lat': collection.preferences.lat,
                    'long': collection.preferences.long,
                    'address': collection.preferences.address,  # Add missing address field
                    'cities': collection.preferences.cities,
                    'townships': collection.preferences.townships,
                    'diameter': collection.preferences.diameter,
                    'special_features': collection.preferences.special_features,
                    'timeframe': collection.preferences.timeframe,
                    'visiting_reason': collection.preferences.visiting_reason,
                    'has_agent': collection.preferences.has_agent,

                    'is_town_house': collection.preferences.is_town_house,
                    'is_condo': collection.preferences.is_condo,
                    'is_single_family': collection.preferences.is_single_family,
                    'is_lot_land': collection.preferences.is_lot_land,
                    'is_multi_family': collection.preferences.is_multi_family,
                    'is_apartment': collection.preferences.is_apartment
                } if collection.preferences else {},
                'stats': {
                    'totalProperties': total_properties,
                    'viewedProperties': 0,
                    'likedProperties': 0,
                    'lastActivity': collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat()
                },
                'shareToken': collection.share_token,
                'isPublic': collection.is_public
            }

            return collection_data

        except Exception as e:
            print(f"Error getting shared collection: {e}")
            raise e

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

            print(f"[DELETE_COLLECTION] Deleting collection {collection_id} with {len(collection.properties)} properties")

            # Get all property IDs in this collection
            property_ids_in_collection = [prop.id for prop in collection.properties]

            # Find properties that will become orphaned after this collection is deleted
            orphaned_properties = []

            if property_ids_in_collection:
                print(f"[DELETE_COLLECTION] Checking {len(property_ids_in_collection)} properties for orphaned status")

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

                print(f"[DELETE_COLLECTION] Found {len(orphaned_properties)} properties that will become orphaned")

            # Delete the collection (this will cascade delete the collection_properties relationships)
            await db.delete(collection)

            # Clean up orphaned properties and their dependencies
            if orphaned_properties:
                print(f"[DELETE_COLLECTION] Cleaning up {len(orphaned_properties)} orphaned properties")

                # Delete PropertyInteractions for orphaned properties
                interactions_result = await db.execute(
                    delete(PropertyInteraction).where(PropertyInteraction.property_id.in_(orphaned_properties))
                )
                print(f"[DELETE_COLLECTION] Deleted {interactions_result.rowcount} property interactions")

                # Delete PropertyComments for orphaned properties
                comments_result = await db.execute(
                    delete(PropertyComment).where(PropertyComment.property_id.in_(orphaned_properties))
                )
                print(f"[DELETE_COLLECTION] Deleted {comments_result.rowcount} property comments")

                # Delete the orphaned properties themselves
                properties_result = await db.execute(
                    delete(Property).where(Property.id.in_(orphaned_properties))
                )
                print(f"[DELETE_COLLECTION] Deleted {properties_result.rowcount} orphaned properties")

            await db.commit()
            print(f"[DELETE_COLLECTION] Successfully deleted collection {collection_id}")

            return True

        except Exception as e:
            print(f"Error deleting collection: {e}")
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
                print(f"[DEBUG SERVICE] No collection found with ID: {collectionId}")
                return []

            property_ids = [prop.id for prop in collection.properties]
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

            properties_data = []
            for prop in collection.properties:
                property_dict = {
                    'id': prop.id,
                    'zpid': prop.zpid,
                    'address': prop.street_address or 'Unknown Address',
                    'city': prop.city,
                    'state': prop.state,
                    'zipCode': prop.zipcode,
                    'price': prop.price,
                    'beds': prop.bedrooms,
                    'baths': prop.bathrooms,
                    'squareFeet': prop.living_area,
                    'lotSize': prop.lot_size,
                    'propertyType': prop.home_type,
                    'imageUrl': prop.img_src,
                    'description': '',
                    'listingUpdated': prop.updated_at.isoformat() if prop.updated_at else None,
                    'status': prop.home_status,
                    'liked': interactions_lookup[prop.id].liked if prop.id in interactions_lookup else False,
                    'disliked': interactions_lookup[prop.id].disliked if prop.id in interactions_lookup else False,
                    'favorited': interactions_lookup[prop.id].favorited if prop.id in interactions_lookup else False,
                    'viewed': prop.id in interactions_lookup,  # True if any interaction exists
                    'comments': comments_lookup.get(prop.id, [])
                }
                properties_data.append(property_dict)

            return properties_data
        except Exception as e:
            print(e)
            return []


