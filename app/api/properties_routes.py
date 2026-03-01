from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from typing import Dict, Any, Optional, Union, List
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
import os

from app.database import get_db
from app.models.database import Property, User, ScheduledEmail, Notification, Collection, SchoolDistrict
from app.schemas.collection_preferences import CollectionPreferencesBase
from app.services.property_service import property_service
from app.utils.auth import require_broker_authorization, get_current_user_optional
import json
from datetime import datetime, timezone
from typing import Any, Dict
from app.models.property import (
    PropertyDetailResponse, 
    PropertySummaryResponse,
    PropertyLookupRequest,
    SimilarPropertiesRequest
)
from app.config.logging import get_logger

router = APIRouter(prefix="/api/properties", tags=["properties"])
logger = get_logger(__name__)

def _convert_datetimes_to_strings(obj: Any) -> Any:
    """Recursively convert datetime objects to ISO format strings"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: _convert_datetimes_to_strings(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetimes_to_strings(item) for item in obj]
    else:
        return obj

class PropertyStoreRequest(BaseModel):
    property_id: str
    property_data: Dict[str, Any]
    address: str
    cover_image_url: Optional[str] = None

class PropertyResponse(BaseModel):
    id: str
    property_data: Dict[str, Any]
    address: str

class AgentMessageRequest(BaseModel):
    agent_id: str
    property_id: str
    property_address: str
    visitor_name: str
    visitor_contact: str
    message: str

class ScheduleTourRequest(BaseModel):
    agent_id: str
    property_id: str
    property_address: str
    visitor_name: Optional[str] = None
    visitor_contact: Optional[str] = None
    preferred_date: str
    preferred_time: str
    preferred_date_2: Optional[str] = None
    preferred_time_2: Optional[str] = None
    preferred_date_3: Optional[str] = None
    preferred_time_3: Optional[str] = None
    message: Optional[str] = None

@router.post("")
async def store_property(
    request: PropertyStoreRequest,
    db: AsyncSession = Depends(get_db)
):
    """Store property data for later retrieval by ID (Used for manual adds or snapshots)"""
    try:
        # Check if property already exists by ID or ListingKey
        listing_key = str(request.property_data.get("listing_key", ""))
        stmt = select(Property).where(or_(Property.id == request.property_id, Property.listing_key == listing_key))
        result = await db.execute(stmt)
        existing_property = result.scalar_one_or_none()
        
        # Prepare data from property_data (handles both snake_case and camelCase from frontend)
        data = request.property_data
        
        fields = {
            "street_address": request.address,
            "img_src": request.cover_image_url or data.get("imageUrl") or data.get("imgSrc"),
            "price": data.get("price") or data.get("ListPrice"),
            "bedrooms": data.get("beds") or data.get("BedroomsTotal"),
            "bathrooms": data.get("baths") or data.get("BathroomsTotal"),
            "living_area": data.get("sqft") or data.get("LivingArea"),
            "lot_size": data.get("lotSize") or data.get("LotSizeSquareFeet"),
            "year_built": data.get("yearBuilt") or data.get("YearBuilt"),
            "home_type": data.get("propertyType") or data.get("PropertyType"),
            "home_status": data.get("homeStatus") or data.get("MlsStatus") or "ACTIVE",
            "latitude": data.get("latitude") or data.get("Latitude"),
            "longitude": data.get("longitude") or data.get("Longitude"),
            "listing_key": listing_key,
            "description": data.get("description") or data.get("PublicRemarks"),
            "city": data.get("city") or data.get("City"),
            "state": data.get("state") or data.get("StateOrProvince"),
            "zipcode": data.get("zipcode") or data.get("PostalCode"),
            "updated_at": datetime.now(timezone.utc)
        }

        if existing_property:
            for key, value in fields.items():
                if value is not None:
                    setattr(existing_property, key, value)
        else:
            new_property = Property(id=request.property_id, created_at=datetime.now(timezone.utc), **fields)
            db.add(new_property)
        
        await db.commit()
        return {"success": True, "message": "Property stored successfully"}
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to store property: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store property: {str(e)}")

# TODO
@router.post("/message-agent")
async def message_agent(
    request: AgentMessageRequest,
    db: AsyncSession = Depends(get_db)
):
    """Send a message to an agent from a visitor on the property page"""
    try:
        # 1. Find the agent
        stmt = select(User).where(User.id == request.agent_id)
        result = await db.execute(stmt)
        agent = result.scalar_one_or_none()
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
            
        # 2. Schedule the email inquiry
        scheduled_email = ScheduledEmail(
            recipient_email=agent.email,
            subject=f"New Inquiry from {request.visitor_name} for {request.property_address}",
            template_name="similar_property_visitor_message_template",
            template_variables={
                "agent_name": f"{agent.first_name} {agent.last_name}",
                "visitor_name": request.visitor_name,
                "visitor_contact": request.visitor_contact,
                "property_address": request.property_address,
                "message": request.message,
                "today_date": datetime.now().strftime("%m/%d/%Y")
            },
            scheduled_for=datetime.now(timezone.utc)
        )
        
        db.add(scheduled_email)

        # 3. Create a dashboard notification for the agent
        notification = Notification(
            agent_id=agent.id,
            type="PROPERTY_INQUIRY",
            reference_type="PROPERTY",
            reference_id=request.property_id,
            title=f"New Inquiry: {request.visitor_name}",
            message=f"Visitor sent a message about {request.property_address}",
            property_id=request.property_id,
            property_address=request.property_address,
            visitor_name=request.visitor_name,
            is_read=False,
            created_at=datetime.now(timezone.utc)
        )
        db.add(notification)
        
        await db.commit()
        
        return {"success": True, "message": "Message sent to agent successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Failed to send message to agent", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to send message to agent")

# TODO
@router.post("/schedule-tour")
async def schedule_tour(
    request: ScheduleTourRequest,
    db: AsyncSession = Depends(get_db)
):
    """Schedule a tour for a property and notify the agent"""
    try:
        # 1. Find the agent
        stmt = select(User).where(User.id == request.agent_id)
        result = await db.execute(stmt)
        agent = result.scalar_one_or_none()
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
            
        # 2. Format preferred dates for the email
        preferred_dates = f"1. {request.preferred_date} at {request.preferred_time}"
        if request.preferred_date_2 and request.preferred_time_2:
            preferred_dates += f"\n2. {request.preferred_date_2} at {request.preferred_time_2}"
        if request.preferred_date_3 and request.preferred_time_3:
            preferred_dates += f"\n3. {request.preferred_date_3} at {request.preferred_time_3}"

        # 3. Schedule the email notification
        scheduled_email = ScheduledEmail(
            recipient_email=agent.email,
            subject=f"Tour Request from {request.visitor_name or 'Visitor'} for {request.property_address}",
            template_name="similar_property_visitor_tour_request",
            template_variables={
                "agent_name": f"{agent.first_name} {agent.last_name}",
                "visitor_name": request.visitor_name or "Interested Visitor",
                "visitor_contact": request.visitor_contact or "Not provided",
                "property_address": request.property_address,
                "preferred_dates": preferred_dates,
                "today_date": datetime.now().strftime("%m/%d/%Y")
            },
            scheduled_for=datetime.now(timezone.utc)
        )
        db.add(scheduled_email)

        # 4. Create a dashboard notification for the agent
        notification = Notification(
            agent_id=agent.id,
            type="TOUR_REQUEST",
            reference_type="TOUR",
            reference_id=request.property_id,
            title=f"Tour Request: {request.visitor_name or 'Visitor'}",
            message=f"Visitor wants to tour {request.property_address}",
            property_id=request.property_id,
            property_address=request.property_address,
            visitor_name=request.visitor_name,
            is_read=False,
            created_at=datetime.now(timezone.utc)
        )
        db.add(notification)
        
        await db.commit()
        
        return {"success": True, "message": "Tour scheduled and agent notified"}
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Failed to schedule tour", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to schedule tour")
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Failed to schedule tour", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to schedule tour")

@router.get("/school-districts", dependencies=[Depends(require_broker_authorization)])
async def search_school_districts(
    query: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db)
):
    """Autocomplete school districts from the reference table"""
    try:
        stmt = (
            select(SchoolDistrict.name, SchoolDistrict.state)
            .where(SchoolDistrict.name.ilike(f"%{query}%"))
            .order_by(SchoolDistrict.name.asc())
            .limit(10)
        )
        result = await db.execute(stmt)
        districts = result.fetchall()
        
        return {
            "success": True, 
            "results": [f"{d[0]}, {d[1]}" for d in districts]
        }
    except Exception as e:
        logger.error(f"School district autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@router.get("/{property_id}", dependencies=[Depends(require_broker_authorization)])
async def get_property(
    property_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get property data by ID from the local mirror"""
    try:
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()
        
        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found")
        
        return {
            "success": True,
            "property": PropertyDetailResponse.model_validate(property_record)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get property {property_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get property")

@router.post("/lookup", response_model=PropertyDetailResponse, dependencies=[Depends(require_broker_authorization)])
async def get_property_details(
    request: PropertyLookupRequest,
    db: AsyncSession = Depends(get_db)
):
    """Fetch property details from the local mirror using PropertyService"""
    if not request.listing_key and not request.address:
        raise HTTPException(status_code=400, detail="Either listing_key or address must be provided")
        
    try:
        if request.listing_key:
            property_data = await property_service.get_property_by_listing_key(db, request.listing_key)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found in mirror by listing key")
            return property_data
        else:
            property_data = await property_service.get_property_by_address(db, request.address)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found in mirror by address")
            return property_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Mirror lookup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database lookup failed: {str(e)}")

@router.post("/similar", dependencies=[Depends(require_broker_authorization)])
async def get_similar_properties(
    request: SimilarPropertiesRequest,
    db: AsyncSession = Depends(get_db)
):
    """Find matching properties from the local mirror using PropertyService"""
    try:
        # 1. If specific keys are provided, fetch those exactly
        if request.listing_keys:
            logger.info(f"Fetching properties by keys from mirror: {request.listing_keys}")
            results = []
            for key in request.listing_keys:
                p = await property_service.get_property_by_listing_key(db, str(key))
                if p: results.append(p)
            return {"success": True, "properties": results}

        # 2. Discovery Mode (Search by preferences)
        # Fetch the original property to get its exact home_type and price if not provided
        original_property = None
        if request.listing_key:
            original_property = await db.execute(select(Property).where(Property.listing_key == str(request.listing_key)))
            original_property = original_property.scalar_one_or_none()

        # Helper to map string type to booleans
        def get_type_flags(h_type: Optional[str]):
            flags = {
                "is_single_family": False,
                "is_town_house": False,
                "is_condo": False,
                "is_multi_family": False,
                "is_lot_land": False,
                "is_apartment": False
            }
            if h_type == "SINGLE_FAMILY": flags["is_single_family"] = True
            elif h_type == "TOWNHOUSE": flags["is_town_house"] = True
            elif h_type == "CONDO": flags["is_condo"] = True
            elif h_type == "MULTI_FAMILY": flags["is_multi_family"] = True
            elif h_type in ["LAND", "FARM"]: flags["is_lot_land"] = True
            elif h_type == "RESIDENTIAL_LEASE": flags["is_apartment"] = True
            return flags

        if request.lat and request.lng:
            type_flags = get_type_flags(original_property.home_type if original_property else None)
            prefs = CollectionPreferencesBase(
                lat=request.lat,
                long=request.lng,
                diameter=request.radius or 5.0,
                min_price=request.min_price if request.min_price and request.min_price > 0 else None,
                max_price=request.max_price if request.max_price and request.max_price > 0 else None,
                min_beds=request.min_beds if request.min_beds and request.min_beds > 0 else None,
                min_baths=request.min_baths if request.min_baths and request.min_baths > 0 else None,
                **type_flags
            )
        else:
            # Fallback to city/zip based search
            try:
                # Use request values, fallback to original property, fallback to 0
                bedrooms = int(float(request.bedrooms)) if request.bedrooms is not None else (original_property.bedrooms if original_property else 0)
                price = float(request.price) if request.price is not None else (original_property.price if original_property else 0)
            except (ValueError, TypeError):
                bedrooms = 0
                price = 0
            
            # Widen default window to 40% to catch local neighbors like Drummers Ln
            min_price = int(price * 0.6) if price > 0 else None
            max_price = int(price * 1.4) if price > 0 else None
            city_name = request.city.split(',')[0].strip() if request.city else (original_property.city if original_property else None)
            state_name = request.state if request.state else (original_property.state if original_property else "PA")
            
            type_flags = get_type_flags(original_property.home_type if original_property else None)
            prefs = CollectionPreferencesBase(
                cities=[f"{city_name}, {state_name}"] if city_name else [],
                min_price=min_price,
                max_price=max_price,
                min_beds=max(0, bedrooms - 1),
                **type_flags
            )
        
        results = await property_service.get_properties_by_preferences(db, prefs, max_properties=50)
        
        # Filter out the current listing if provided
        if request.listing_key:
            results = [p for p in results if str(p.listing_key) != str(request.listing_key)]
            
        return {"success": True, "properties": results}
    except Exception as e:
        logger.error(f"Mirror search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{property_id}/cache")
async def cache_property_details(
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user_optional)
):
    """
    Returns property details directly from the mirror. 
    Legacy 'cache' naming preserved for frontend compatibility.
    """
    try:
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()

        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found in mirror")

        # In the new architecture, everything is already 'cached' in the mirror
        response_details = PropertyDetailResponse.model_validate(property_record)

        return {
            "success": True,
            "message": "Property details retrieved from mirror",
            "property_id": property_id,
            "from_cache": True,
            "property": response_details.model_dump(by_alias=True, exclude_none=True)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Property mirror retrieval failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve property: {str(e)}")

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        logger.error("Property cache failed", exc_info=True, extra={"property_id": property_id, "error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to cache property details: {str(e)}")

class PropertyAgentResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    property: PropertyDetailResponse
    agent_name: str

@router.get("/agent/{agent_id}/listing/{listing_key}", response_model=PropertyAgentResponse, dependencies=[Depends(require_broker_authorization)])
async def get_property_for_agent(
    agent_id: str,
    listing_key: str,
    db: AsyncSession = Depends(get_db)
):
    """Get property data from mirror by agent and listing key"""
    try:
        from app.services.user_service import UserService
        from app.models.database import Collection
        
        # 1. Try to find as a User ID first
        agent = await UserService.get_user_by_id(db, agent_id)
        
        # 2. If not found, try to find as a Collection ID and get its owner
        if not agent:
            stmt = select(Collection).where(Collection.id == agent_id)
            result = await db.execute(stmt)
            collection = result.scalar_one_or_none()
            if collection:
                agent = await UserService.get_user_by_id(db, collection.owner_id)
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent or Collection not found")
        
        agent_name = f"{agent.first_name} {agent.last_name}"
        
        try:
            property_data = await property_service.get_property_by_listing_key(db, listing_key)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found in mirror")
                
            return PropertyAgentResponse(property=property_data, agent_name=agent_name)
        except Exception as e:
            logger.error(f"Mirror lookup for agent failed: {e}")
            raise HTTPException(status_code=500, detail=f"Database lookup failed: {str(e)}")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get property for agent", extra={"agent_id": agent_id, "listing_key": listing_key, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get property for agent: {str(e)}")
