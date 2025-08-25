from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from datetime import datetime
from typing import Optional, Dict, Any

from app.models.database import Property, Collection, OpenHouseVisitor, User
from app.schemas.open_house import OpenHouseFormSubmission


class OpenHouseService:
    
    @staticmethod
    async def create_visitor(
        db: AsyncSession, 
        form_data: OpenHouseFormSubmission
    ) -> OpenHouseVisitor:
        """Create a new open house visitor record"""
        
        
        # Create visitor record
        visitor = OpenHouseVisitor(
            full_name=form_data.full_name,
            email=form_data.email,
            phone=form_data.phone,
            visiting_reason=form_data.visiting_reason if isinstance(form_data.visiting_reason, str) else form_data.visiting_reason.value,
            timeframe=form_data.timeframe if isinstance(form_data.timeframe, str) else form_data.timeframe.value,
            has_agent=form_data.has_agent if isinstance(form_data.has_agent, str) else form_data.has_agent.value,
            property_id=form_data.property_id,
            qr_code="",  # Will be empty since we're not tracking QR codes for this form
            additional_comments=form_data.additional_comments,
            interested_in_similar=form_data.interested_in_similar,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
    ) -> bool:
        """Create a collection for the visitor with smart filters based on the visited property"""
        
        if not form_data.property_id:
            return False
            
        try:
            # Get the original property to create smart filters
            visited_property = await OpenHouseService.get_property_by_id(db, str(form_data.property_id))
            
            if not visited_property:
                print(f"Could not find property {form_data.property_id} to create filters")
                return False
            
            # Generate smart filters based on the visited property (non-async method)
            smart_filters = OpenHouseService.generate_smart_filters(visited_property, form_data)
            
            collection = Collection(
                owner_id=form_data.agent_id if form_data.agent_id else None,  # Link to agent if provided
                name=visited_property.get('address', 'Unknown Property'),
                description=f"Properties similar to {visited_property.get('address', 'the visited property')} based on your preferences",
                visitor_email=visitor.email,
                visitor_name=visitor.full_name,
                visitor_phone=visitor.phone,
                original_property_id=form_data.property_id,
                preferences={
                    "visiting_reason": form_data.visiting_reason if isinstance(form_data.visiting_reason, str) else form_data.visiting_reason.value,
                    "timeframe": form_data.timeframe if isinstance(form_data.timeframe, str) else form_data.timeframe.value,
                    "additional_comments": form_data.additional_comments,
                    "smart_filters": smart_filters,
                    "agent_id": form_data.agent_id  # Store agent_id in preferences for tracking
                },
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            
            db.add(collection)
            await db.commit()
            await db.refresh(collection)
            
            print(f"Created collection {collection.id} with smart filters: {smart_filters}")
            return True
            
        except Exception as e:
            print(f"Error creating collection for visitor: {e}")
            await db.rollback()
            return False
    
    @staticmethod
    def generate_smart_filters(visited_property: Dict[str, Any], form_data: OpenHouseFormSubmission) -> Dict[str, Any]:
        """Generate intelligent filters based on the property they visited"""
        filters = {}
        
        # Price range filter (+/- 20% of visited property)
        if visited_property.get("price"):
            base_price = visited_property["price"]
            price_min = int(base_price * 0.8)  # -20%
            price_max = int(base_price * 1.2)  # +20%
            filters["price_range"] = {
                "min": price_min,
                "max": price_max,
                "base_price": base_price
            }
        
        # Bedroom range (+/- 1 bedroom)
        if visited_property.get("beds"):
            base_beds = visited_property["beds"]
            filters["bedrooms"] = {
                "min": max(1, base_beds - 1),
                "max": base_beds + 1,
                "preferred": base_beds
            }
        
        # Bathroom range (+/- 0.5 bathroom)
        if visited_property.get("baths"):
            base_baths = visited_property["baths"]
            filters["bathrooms"] = {
                "min": max(1, base_baths - 0.5),
                "max": base_baths + 1,
                "preferred": base_baths
            }
        
        # Square footage range (+/- 20%)
        if visited_property.get("squareFeet"):
            base_sqft = visited_property["squareFeet"]
            filters["square_feet"] = {
                "min": int(base_sqft * 0.8),
                "max": int(base_sqft * 1.2),
                "preferred": base_sqft
            }
        
        # Location preferences (same city/state)
        if visited_property.get("city"):
            filters["location"] = {
                "preferred_city": visited_property["city"],
                "preferred_state": visited_property.get("state"),
                "radius_miles": 15  # 15-mile radius from the visited property
            }
        
        # Property type preference
        if visited_property.get("propertyType"):
            filters["property_type"] = {
                "preferred": visited_property["propertyType"],
                "alternatives": ["Single Family", "Townhouse", "Condo"]  # Common alternatives
            }
        
        # Additional preferences from form data
        if form_data.additional_comments:
            # Parse common keywords from comments
            comments_lower = form_data.additional_comments.lower()
            features = []
            
            if "pool" in comments_lower:
                features.append("pool")
            if "garage" in comments_lower:
                features.append("garage")
            if "modern" in comments_lower or "updated" in comments_lower:
                features.append("modern_kitchen")
            if "yard" in comments_lower or "garden" in comments_lower:
                features.append("large_yard")
            if "fireplace" in comments_lower:
                features.append("fireplace")
            
            if features:
                filters["desired_features"] = features
        
        # Timeframe urgency affects how strict the filters are
        if form_data.timeframe in ["IMMEDIATELY", "1_3_MONTHS"]:
            filters["urgency"] = "high"
            # More flexible filters for urgent buyers
            if "price_range" in filters:
                filters["price_range"]["max"] = int(filters["price_range"]["max"] * 1.1)  # +10% more flexibility
        elif form_data.timeframe in ["OVER_YEAR", "NOT_SURE"]:
            filters["urgency"] = "low"
            # Stricter filters for casual browsers
            if "price_range" in filters:
                filters["price_range"]["max"] = int(filters["price_range"]["max"] * 0.95)  # -5% stricter
        
        return filters
    
    @staticmethod
    async def get_property_by_id(db: AsyncSession, property_id: str) -> Optional[Dict[str, Any]]:
        """Get property details by ID from database"""
        try:
            stmt = select(Property).where(Property.id == property_id)
            result = await db.execute(stmt)
            property_record = result.scalar_one_or_none()
            
            if not property_record:
                return None
            
            # Extract data from both stored fields and zillow_data JSON
            property_data = property_record.zillow_data or {}
            
            return {
                "id": property_record.id,
                "address": property_record.street_address,
                "city": property_data.get("city") or property_record.city,
                "state": property_data.get("state") or property_record.state,
                "zipCode": property_data.get("zipCode") or property_record.zipcode,
                "price": property_record.price or property_data.get("price"),
                "beds": property_record.bedrooms or property_data.get("beds"),
                "baths": property_record.bathrooms or property_data.get("baths"),
                "squareFeet": property_record.living_area or property_data.get("sqft"),
                "lotSize": property_record.lot_size or property_data.get("lotSize"),
                "propertyType": property_record.home_type or property_data.get("propertyType"),
                "description": property_data.get("description", "Beautiful property")
            }
            
        except Exception as e:
            print(f"Error fetching property by ID: {e}")
            return None
    
    @staticmethod
    async def get_property_by_qr_code(
        db: AsyncSession,
        qr_code: str
    ) -> Optional[Dict[str, Any]]:
        """Get property information by QR code"""
        
        # For now, we'll use a simple lookup. In a real system, you'd have a mapping
        # table between QR codes and properties
        try:
            # This is a mock implementation - in reality you'd have:
            # 1. A QR code mapping table
            # 2. Or store QR codes directly in the Property table
            # 3. Or use the QR code as a unique identifier
            
            # For demonstration, let's assume QR code maps to a property ID or address
            # You would replace this with actual QR code to property mapping logic
            
            # Example: If QR code is "QR123", it might map to property ID 1
            # This is just mock logic - implement according to your QR code strategy
            
            property_data = {
                "id": 1,
                "address": "123 Main Street, West Chester, PA 19380",
                "city": "West Chester",
                "state": "PA",
                "zipCode": "19380", 
                "price": 1200000,
                "beds": 5,
                "baths": 3.5,
                "squareFeet": 5000,
                "lotSize": 1.0,
                "propertyType": "Single Family",
                "description": "Beautiful colonial home in desirable West Chester location"
            }
            
            return property_data
            
        except Exception as e:
            print(f"Error fetching property by QR code: {e}")
            return None
