from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import Dict, Any, Optional, Union, List
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
import os

from app.database import get_db
from app.models.database import Property, PropertyDetails
from app.services.bright_mls_service import BrightMlsService
import json
from datetime import datetime, timezone
from typing import Any, Dict
from app.models.property import (
    PropertyDetailResponse, 
    PropertySaveResponse, 
    PropertyLookupRequest,
    ResoFacts,
    OriginalPhoto,
    MixedSources,
    ImageSource
)
from app.config.logging import get_logger

router = APIRouter()
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

@router.post("/api/properties")
async def store_property(
    request: PropertyStoreRequest,
    db: AsyncSession = Depends(get_db)
):
    """Store property data for later retrieval by ID"""
    try:
        # Check if property already exists
        stmt = select(Property).where(Property.id == request.property_id)
        result = await db.execute(stmt)
        existing_property = result.scalar_one_or_none()
        
        if existing_property:
            # Update existing property
            existing_property.street_address = request.address
            existing_property.updated_at = datetime.now(timezone.utc)
            existing_property.last_synced = datetime.now(timezone.utc)
            
            # Update cover image if provided
            if request.cover_image_url:
                existing_property.img_src = request.cover_image_url
            
            # Update specific fields from property data
            if "price" in request.property_data:
                existing_property.price = request.property_data["price"]
            if "beds" in request.property_data:
                existing_property.bedrooms = request.property_data["beds"]
            if "baths" in request.property_data:
                existing_property.bathrooms = request.property_data["baths"]
            if "sqft" in request.property_data:
                existing_property.living_area = request.property_data["sqft"]
            if "lotSize" in request.property_data:
                existing_property.lot_size = request.property_data["lotSize"]
            if "yearBuilt" in request.property_data:
                existing_property.year_built = request.property_data["yearBuilt"]
            if "propertyType" in request.property_data:
                existing_property.home_type = request.property_data["propertyType"]
            if "latitude" in request.property_data:
                existing_property.latitude = request.property_data["latitude"]
            if "longitude" in request.property_data:
                existing_property.longitude = request.property_data["longitude"]
            if "listing_key" in request.property_data:
                existing_property.listing_key = request.property_data["listing_key"]
                
        else:
            # Create new property
            new_property = Property(
                id=request.property_id,
                street_address=request.address,
                img_src=request.cover_image_url,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            
            # Set specific fields from property data
            if "price" in request.property_data:
                new_property.price = request.property_data["price"]
            if "beds" in request.property_data:
                new_property.bedrooms = request.property_data["beds"]
            if "baths" in request.property_data:
                new_property.bathrooms = request.property_data["baths"]
            if "sqft" in request.property_data:
                new_property.living_area = request.property_data["sqft"]
            if "lotSize" in request.property_data:
                new_property.lot_size = request.property_data["lotSize"]
            if "yearBuilt" in request.property_data:
                new_property.year_built = request.property_data["yearBuilt"]
            if "propertyType" in request.property_data:
                new_property.home_type = request.property_data["propertyType"]
            if "latitude" in request.property_data:
                new_property.latitude = request.property_data["latitude"]
            if "longitude" in request.property_data:
                new_property.longitude = request.property_data["longitude"]
            if "listing_key" in request.property_data:
                new_property.listing_key = request.property_data["listing_key"]
                
            db.add(new_property)
        
        await db.commit()
        
        return {"success": True, "message": "Property stored successfully"}
        
    except Exception as e:
        await db.rollback()
        logger.error("Failed to store property", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to store property")

@router.get("/api/properties/{property_id}")
async def get_property(
    property_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get property data by ID"""
    try:
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()
        
        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found")
        
        # Build property data from database fields
        property_data = {
            "id": property_record.id,
            "address": property_record.street_address,
            "price": property_record.price,
            "beds": property_record.bedrooms,
            "baths": property_record.bathrooms,
            "sqft": property_record.living_area,
            "lotSize": property_record.lot_size,
            "yearBuilt": property_record.year_built,
            "propertyType": property_record.home_type,
            "latitude": property_record.latitude,
            "longitude": property_record.longitude,
            "listing_key": property_record.listing_key
        }
        
        return PropertyResponse(
            id=property_record.id,
            property_data=property_data,
            address=property_record.street_address or ""
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get property", extra={"property_id": property_id, "error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to get property")

@router.post("/api/property", response_model=PropertyDetailResponse)
async def get_property_details(
    request: PropertyLookupRequest
):
    """Fetch property details from Bright MLS API without saving to database"""
    if not request.listing_key and not request.address:
        raise HTTPException(status_code=400, detail="Either listing_key or address must be provided")
        
    mls_service = BrightMlsService()
    return await mls_service.get_property_by_address(
        address=request.address, 
        listing_key=request.listing_key
    )

class SimilarPropertiesRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    listing_key: Optional[Union[str, int]] = None
    listing_keys: Optional[List[Union[str, int]]] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[Union[float, int]] = None
    bedrooms: Optional[Union[float, int]] = None

@router.post("/api/properties/similar")
async def get_similar_properties(
    request: SimilarPropertiesRequest
):
    """Find similar properties or fetch curated properties by keys"""
    mls_service = BrightMlsService()
    
    # If specific keys are provided, fetch those exactly (Curation Mode)
    if request.listing_keys:
        logger.info(f"Fetching curated properties by keys: {request.listing_keys}")
        results = await mls_service.get_properties_by_keys([str(k) for k in request.listing_keys])
        return {"success": True, "properties": results}

    # Otherwise, perform a similarity search (Discovery Mode)
    logger.info(f"Finding similar properties for: {request.listing_key} in {request.zipcode or request.city}")
    mls_service = BrightMlsService()
    
    # Create a mock preference object for the search service
    from app.schemas.collection_preferences import CollectionPreferencesBase
    
    # Safely handle numeric conversions
    try:
        bedrooms = int(float(request.bedrooms)) if request.bedrooms is not None else 0
        price = float(request.price) if request.price is not None else 0
    except (ValueError, TypeError):
        bedrooms = 0
        price = 0
    
    # Calculate a price range (+/- 20%)
    min_price = int(price * 0.8) if price > 0 else None
    max_price = int(price * 1.2) if price > 0 else None
    
    # We use the existing get_matching_properties logic but scoped to the zip/city
    # Use the first word of city if it contains commas
    city_name = request.city.split(',')[0].strip() if request.city else None
    
    prefs = CollectionPreferencesBase(
        cities=[f"{city_name}, {request.state}"] if city_name and request.state else [],
        min_price=min_price,
        max_price=max_price,
        min_beds=max(0, bedrooms - 1),
        max_beds=bedrooms + 1
    )
    
    logger.info(f"Similar Search Filters: City={city_name}, Price={min_price}-{max_price}, Beds={prefs.min_beds}-{prefs.max_beds}")
    
    results = await mls_service.get_matching_properties(prefs, max_properties=12)
    
    # Filter out the original property if it's in the results
    if request.listing_key:
        results = [p for p in results if str(p.get('listing_key')) != str(request.listing_key)]
        
    # Convert results to camelCase for frontend compatibility
    camel_results = []
    for p in results:
        camel_p = {}
        for k, v in p.items():
            # Convert snake_case key to camelCase
            components = k.split('_')
            camel_key = components[0] + ''.join(x.title() for x in components[1:])
            camel_p[camel_key] = v
            
        # Ensure image_url is also available as imageUrl and coverImageUrl
        if 'imageUrl' in camel_p:
            camel_p['coverImageUrl'] = camel_p['imageUrl']
            
        camel_results.append(camel_p)
        
    return {"success": True, "properties": camel_results}

@router.get("/properties/{property_id}/cache")
async def cache_property_details(
    property_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Cache detailed property information from Bright MLS"""
    start_time = datetime.now(timezone.utc)
    try:
        # Eager load the details relationship
        stmt = select(Property).options(selectinload(Property.details)).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()

        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found")

        # Check if details already exist and are fresh (e.g., < 24 hours old)
        # For now, we'll just check if they exist to avoid re-fetching on every load
        if property_record.details:
            # Use Pydantic model for standardized serialization
            details = property_record.details
            
            # Construct PropertyDetailResponse structure
            # Bridge the gap between Property and PropertyDetails models
            reso_facts = ResoFacts.model_validate(details)
            # Add living area from the main property record
            reso_facts.living_area = property_record.living_area
            
            # Map garage spaces to parking capacity fields
            reso_facts.parking_capacity = details.garage_spaces
            reso_facts.garage_parking_capacity = details.garage_spaces
            
            # Map association fee to hoa fee for frontend compatibility
            if details.association_fee:
                reso_facts.hoa_fee = f"${details.association_fee}"
            
            response_details = PropertyDetailResponse.model_validate(property_record)
            response_details.days_on_market = details.days_on_market
            response_details.year_built = details.year_built
            response_details.description = details.description
            response_details.standard_status = details.standard_status
            response_details.list_office_name = details.list_office_name
            response_details.list_office_phone = details.list_office_phone
            response_details.list_agent_full_name = details.list_agent_full_name
            response_details.list_agent_email = details.list_agent_email
            response_details.original_photos = [
                OriginalPhoto(
                    caption=p.get("caption", ""),
                    mixed_sources=MixedSources(
                        jpeg=[ImageSource(url=p.get("url"), width=0)],
                        webp=[]
                    )
                ) for p in (details.photos or [])
            ]
            response_details.reso_facts = reso_facts

            return {
                "success": True,
                "message": "Property details retrieved from cache",
                "cached_at": details.updated_at.isoformat() if details.updated_at else None,
                "property_id": property_id,
                "from_cache": True,
                "details": response_details.model_dump(by_alias=True, exclude_none=True)
            }

        if not property_record.street_address and not property_record.listing_key:
            raise HTTPException(status_code=400, detail="Property missing address or listing key for MLS lookup")

        # Fetch from Bright MLS
        mls_service = BrightMlsService()

        # Construct full address for search to avoid ambiguity
        search_address = property_record.street_address
        if search_address and property_record.city:
            search_address += f", {property_record.city}"
        
        if search_address and property_record.state:
            search_address += f", {property_record.state}"

        if search_address and property_record.zipcode:
            search_address += f" {property_record.zipcode}"

        # Get full data package
        fetched_data = await mls_service.get_property_by_address(
            search_address, 
            details=True, 
            listing_key=property_record.listing_key
        )
        
        if not fetched_data or 'details' not in fetched_data:
             raise HTTPException(status_code=404, detail="Property details not found on MLS")

        details_data = fetched_data['details']

        # Create new PropertyDetails record
        new_details = PropertyDetails(
            property_id=property_id,
            updated_at=datetime.now(timezone.utc)
        )
        
        # Populate the rest of the fields using a loop or direct mapping
        for key, value in details_data.items():
            if hasattr(new_details, key):
                # Handle special timestamp fields that might be strings
                if key in ['modification_timestamp'] and isinstance(value, str) and value:
                    try:
                        # Attempt to parse ISO string to datetime
                        from dateutil.parser import parse
                        value = parse(value)
                    except Exception:
                        value = None # Or keep as string if column allows, but TZDateTime doesn't
                
                setattr(new_details, key, value)

        db.add(new_details)
        await db.commit()
        await db.refresh(new_details)

        duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        
        # Construct response similar to cached version
        response_details = PropertyDetailResponse.model_validate(property_record)
        response_details.days_on_market = new_details.days_on_market
        response_details.year_built = new_details.year_built
        response_details.description = new_details.description
        response_details.standard_status = new_details.standard_status
        response_details.list_office_name = new_details.list_office_name
        response_details.list_office_phone = new_details.list_office_phone
        response_details.list_agent_full_name = new_details.list_agent_full_name
        response_details.list_agent_email = new_details.list_agent_email
        response_details.reso_facts = ResoFacts.model_validate(new_details)
        response_details.reso_facts.living_area = property_record.living_area
        
        # Map garage spaces to parking capacity fields
        response_details.reso_facts.parking_capacity = new_details.garage_spaces
        response_details.reso_facts.garage_parking_capacity = new_details.garage_spaces
        
        # Map association fee to hoa fee for frontend compatibility
        if new_details.association_fee:
            response_details.reso_facts.hoa_fee = f"${new_details.association_fee}"
        
        response_details.original_photos = [
            OriginalPhoto(
                caption=p.get("caption", ""),
                mixed_sources=MixedSources(
                    jpeg=[ImageSource(url=p.get("url"), width=0)],
                    webp=[]
                )
            ) for p in (new_details.photos or [])
        ]
        
        return {
            "success": True,
            "message": "Property details cached successfully",
            "property_id": property_id,
            "from_cache": False,
            "details": response_details.model_dump(by_alias=True, exclude_none=True)
        }

    except HTTPException:
        try:
            await db.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(
            "Property cache failed",
            exc_info=True,
            extra={
                "event": "property_cache_failed",
                "property_id": property_id,
                "error": str(e)
            }
        )
        try:
            await db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to cache property details: {str(e)}")

class PropertyAgentResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    property: PropertyDetailResponse
    agent_name: str

@router.get("/properties/agent/{agent_id}/listing/{listing_key}", response_model=PropertyAgentResponse)
async def get_property_for_agent(
    agent_id: str,
    listing_key: str,
    db: AsyncSession = Depends(get_db)
):
    """Get property data and agent name by agent ID and listing key"""
    try:
        # Get agent details
        from app.services.user_service import UserService
        agent = await UserService.get_user_by_id(db, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        agent_name = f"{agent.first_name} {agent.last_name}"
        
        # Get property details
        mls_service = BrightMlsService()
        property_data = await mls_service.get_property_by_address("", details=True, listing_key=listing_key)
        
        if not property_data:
            raise HTTPException(status_code=404, detail="Property not found")
            
        return PropertyAgentResponse(
            property=property_data,
            agent_name=agent_name
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get property for agent", extra={"agent_id": agent_id, "listing_key": listing_key, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get property for agent: {str(e)}")

