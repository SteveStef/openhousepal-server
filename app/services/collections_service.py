from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid
import secrets
import string

from app.models.database import Collection, Property, User
from app.schemas.collection import CollectionCreate


class CollectionsService:
    
    @staticmethod
    async def get_user_collections(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Get all collections for a user"""
        try:
            # Query collections for the user including anonymous ones assigned to them
            # Include the preferences relationship to avoid lazy loading issues
            query = select(Collection).options(
                selectinload(Collection.preferences)
            ).where(
                (Collection.owner_id == user_id) | 
                (Collection.owner_id.is_(None))  # Include anonymous collections for now
            ).order_by(Collection.created_at.desc())
            
            result = await db.execute(query)
            collections = result.scalars().all()
            
            collections_data = []
            for collection in collections:
                # Get property count (mock for now)
                property_count = 0  # Would count actual properties in a real implementation
                
                # Get original property data if available
                original_property_data = None
                if collection.original_property_id:
                    try:
                        property_query = select(Property).where(Property.id == str(collection.original_property_id))
                        property_result = await db.execute(property_query)
                        original_property = property_result.scalar_one_or_none()
                        
                        if original_property:
                            # Extract property data from both stored fields and zillow_data JSON
                            zillow_data = original_property.zillow_data or {}
                            original_property_data = {
                                "id": original_property.id,
                                "address": original_property.street_address or "Unknown Address",
                                "city": zillow_data.get("city") or original_property.city,
                                "state": zillow_data.get("state") or original_property.state,
                                "zipCode": zillow_data.get("zipCode") or original_property.zipcode,
                                "price": original_property.price or zillow_data.get("price"),
                                "beds": original_property.bedrooms or zillow_data.get("beds"),
                                "baths": original_property.bathrooms or zillow_data.get("baths"),
                                "squareFeet": original_property.living_area or zillow_data.get("sqft"),
                                "propertyType": original_property.home_type or zillow_data.get("propertyType") or "Unknown"
                            }
                    except Exception as e:
                        print(f"Warning: Could not fetch original property data for collection {collection.id}: {e}")
                
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
                        "diameter": collection.preferences.diameter,
                        "special_features": collection.preferences.special_features,
                        "timeframe": collection.preferences.timeframe,
                        "visiting_reason": collection.preferences.visiting_reason,
                        "has_agent": collection.preferences.has_agent
                    }

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
                selectinload(Collection.preferences)
            ).where(
                Collection.id == collection_id,
                (Collection.owner_id == user_id) | (Collection.owner_id.is_(None))
            )
            
            result = await db.execute(query)
            collection = result.scalar_one_or_none()
            
            if not collection:
                return None
            
            # Get properties in this collection (mock for now)
            properties = []  # Would load actual properties
            
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
                    "diameter": collection.preferences.diameter,
                    "special_features": collection.preferences.special_features
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
        """Create a new collection"""
        try:
            collection = Collection(
                id=str(uuid.uuid4()),
                name=collection_data.name,
                description=collection_data.description,
                owner_id=user_id,
                is_public=collection_data.is_public,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            
            db.add(collection)
            await db.commit()
            await db.refresh(collection)
            
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
                (Collection.owner_id == user_id) | (Collection.owner_id.is_(None))
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
                (Collection.owner_id == user_id) | (Collection.owner_id.is_(None))
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
            from sqlalchemy.orm import joinedload
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
                    'images': prop.original_photos or [],
                    'description': '',
                    'listingUpdated': prop.updated_at.isoformat() if prop.updated_at else None,
                    'status': prop.home_status,
                    'yearBuilt': prop.year_built,
                    'taxes': prop.tax_assessed_value,
                    'hoaFees': prop.hoa_fee,
                    'daysOnMarket': prop.days_on_zillow,
                    'county': '',
                    # Initialize interaction states - these will be populated by frontend
                    'liked': False,
                    'disliked': False,
                    'favorited': False,
                    'viewed': False,
                    'comments': []
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
                    'diameter': collection.preferences.diameter,
                    'special_features': collection.preferences.special_features,
                    'timeframe': collection.preferences.timeframe,
                    'visiting_reason': collection.preferences.visiting_reason,
                    'has_agent': collection.preferences.has_agent
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
        """Delete a collection"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )
            
            result = await db.execute(query)
            collection = result.scalar_one_or_none()
            
            if not collection:
                return False
            
            await db.delete(collection)
            await db.commit()
            
            return True
            
        except Exception as e:
            print(f"Error deleting collection: {e}")
            await db.rollback()
            raise e