from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid
import secrets
import string

from app.models.database import Collection, Property, User
from app.schemas.collection import Collection as CollectionSchema


class CollectionsService:
    
    @staticmethod
    async def get_user_collections(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Get all collections for a user"""
        try:
            # Query collections for the user including anonymous ones assigned to them
            query = select(Collection).where(
                (Collection.owner_id == user_id) | 
                (Collection.owner_id.is_(None))  # Include anonymous collections for now
            ).order_by(Collection.created_at.desc())
            
            result = await db.execute(query)
            collections = result.scalars().all()
            
            collections_data = []
            for collection in collections:
                # Get property count (mock for now)
                property_count = 0  # Would count actual properties in a real implementation
                
                # Convert to response format  
                collection_data = {
                    "id": collection.id,
                    "name": collection.name,
                    "description": collection.description or "",
                    "visitor_name": collection.visitor_name,
                    "visitor_email": collection.visitor_email,
                    "visitor_phone": collection.visitor_phone,
                    "preferences": collection.preferences or {},
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
            query = select(Collection).where(
                Collection.id == collection_id,
                (Collection.owner_id == user_id) | (Collection.owner_id.is_(None))
            )
            
            result = await db.execute(query)
            collection = result.scalar_one_or_none()
            
            if not collection:
                return None
            
            # Get properties in this collection (mock for now)
            properties = []  # Would load actual properties
            
            return {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description or "",
                "visitor_name": collection.visitor_name,
                "visitor_email": collection.visitor_email,
                "visitor_phone": collection.visitor_phone,
                "preferences": collection.preferences or {},
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
        collection_data: CollectionSchema,
        user_id: str
    ) -> Dict[str, Any]:
        """Create a new collection"""
        try:
            collection = Collection(
                id=str(uuid.uuid4()),
                name=collection_data.name,
                description=collection_data.description,
                owner_id=user_id,
                preferences=collection_data.preferences if hasattr(collection_data, 'preferences') else {},
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
                "preferences": collection.preferences or {},
                "property_count": 0,
                "is_anonymous": False,
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
            
            # Update preferences to store status since there's no direct status column
            preferences = collection.preferences or {}
            preferences['status'] = status
            collection.preferences = preferences
            # Flag the JSON column as modified so SQLAlchemy knows to update it
            flag_modified(collection, 'preferences')
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
            # Query collection with properties and ensure it's public
            query = (
                select(Collection)
                .options(selectinload(Collection.properties))
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
                'status': collection.preferences.get('status', 'ACTIVE') if collection.preferences else 'ACTIVE',
                'preferences': collection.preferences or {},
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