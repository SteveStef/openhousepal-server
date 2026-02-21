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
from app.services.bright_mls_service import bright_mls_service
from app.utils.auth import require_broker_authorization
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
                existing_property.listing_key = str(request.property_data["listing_key"])
                
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
                new_property.listing_key = str(request.property_data["listing_key"])
                
            db.add(new_property)
        
        await db.commit()
        
        return {"success": True, "message": "Property stored successfully"}
        
    except Exception as e:
        await db.rollback()
        logger.error("Failed to store property", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to store property")

@router.get("/api/properties/{property_id}", dependencies=[Depends(require_broker_authorization)])
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

@router.post("/api/property", response_model=PropertyDetailResponse, dependencies=[Depends(require_broker_authorization)])
async def get_property_details(
    request: PropertyLookupRequest
):
    """Fetch property details from Bright MLS API without saving to database"""
    if not request.listing_key and not request.address:
        raise HTTPException(status_code=400, detail="Either listing_key or address must be provided")
        
    try:
        if request.listing_key:
            property_data = await bright_mls_service.get_property_by_id(request.listing_key)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found by listing key")
            return property_data
        else:
            return await bright_mls_service.get_property_by_address(address=request.address)
    except Exception as e:
        logger.error(f"MLS search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class SimilarPropertiesRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    listing_key: Optional[Union[str, int]] = None
    listing_keys: Optional[List[Union[str, int]]] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[Union[float, int]] = None
    bedrooms: Optional[Union[float, int]] = None

@router.post("/api/properties/similar", dependencies=[Depends(require_broker_authorization)])
async def get_similar_properties(
    request: SimilarPropertiesRequest
):
    """Find similar properties or fetch curated properties by keys"""
    try:
        # If specific keys are provided, fetch those exactly (Curation Mode)
        if request.listing_keys:
            logger.info(f"Fetching curated properties by keys: {request.listing_keys}")
            results = await bright_mls_service.get_properties_by_keys([str(k) for k in request.listing_keys])
            return {"success": True, "properties": results}

        # Otherwise, perform a similarity search (Discovery Mode)
        logger.info(f"Finding similar properties for: {request.listing_key} in {request.zipcode or request.city}")
        
        from app.schemas.collection_preferences import CollectionPreferencesBase
        
        try:
            bedrooms = int(float(request.bedrooms)) if request.bedrooms is not None else 0
            price = float(request.price) if request.price is not None else 0
        except (ValueError, TypeError):
            bedrooms = 0
            price = 0
        
        min_price = int(price * 0.8) if price > 0 else None
        max_price = int(price * 1.2) if price > 0 else None
        
        city_name = request.city.split(',')[0].strip() if request.city else None
        
        prefs = CollectionPreferencesBase(
            cities=[f"{city_name}, {request.state}"] if city_name and request.state else [],
            min_price=min_price,
            max_price=max_price,
            min_beds=max(0, bedrooms - 1),
            max_beds=bedrooms + 1
        )
        
        results = await bright_mls_service.get_properties_by_preferences(prefs, max_properties=12)
        
        if request.listing_key:
            results = [p for p in results if str(p.get('listing_key')) != str(request.listing_key)]
            
        camel_results = []
        for p in results:
            camel_p = {}
            for k, v in p.items():
                components = k.split('_')
                camel_key = components[0] + ''.join(x.title() for x in components[1:])
                camel_p[camel_key] = v
            if 'imageUrl' in camel_p:
                camel_p['coverImageUrl'] = camel_p['imageUrl']
            camel_results.append(camel_p)
            
        return {"success": True, "properties": camel_results}
    except Exception as e:
        logger.error(f"Similar properties search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/properties/{property_id}/cache", dependencies=[Depends(require_broker_authorization)])
async def cache_property_details(
    property_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Cache detailed property information from Bright MLS"""
    try:
        # Eager load the details relationship
        stmt = select(Property).options(selectinload(Property.details)).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()

        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found")

        # 1. Return from Cache if exists
        if property_record.details:
            details = property_record.details
            
            # Construct standard response from DB
            reso_facts = ResoFacts.model_validate(details)
            reso_facts.living_area = property_record.living_area
            reso_facts.parking_capacity = details.garage_spaces
            reso_facts.garage_parking_capacity = details.garage_spaces
            reso_facts.standard_status = details.standard_status
            reso_facts.home_status = property_record.home_status
            reso_facts.days_on_market = details.days_on_market
            
            if details.association_fee:
                reso_facts.hoa_fee = f"${details.association_fee}"
            
            response_details = PropertyDetailResponse.model_validate(property_record)
            response_details.days_on_market = details.days_on_market
            response_details.year_built = details.year_built
            response_details.description = details.description
            response_details.standard_status = details.standard_status
            response_details.home_status = property_record.home_status
            response_details.list_office_name = details.list_office_name
            response_details.list_office_phone = details.list_office_phone
            response_details.list_agent_full_name = details.list_agent_full_name
            response_details.list_agent_email = details.list_agent_email
            response_details.listing_key = property_record.listing_key
            
            # Standardize photos from DB
            response_details.photos = [
                OriginalPhoto(
                    caption=p.get("caption", ""),
                    url=p.get("url"),
                    mixed_sources=MixedSources(
                        jpeg=p.get("mixedSources", {}).get("jpeg", []),
                        webp=p.get("mixedSources", {}).get("webp", [])
                    )
                ) for p in (details.photos or [])
            ]
            response_details.reso_facts = reso_facts

            # Return CLEAN structure frontend expects
            return {
                "success": True,
                "message": "Property details retrieved from cache",
                "cached_at": details.updated_at.isoformat() if details.updated_at else None,
                "property_id": property_id,
                "from_cache": True,
                "property": response_details.model_dump(by_alias=True, exclude_none=True)
            }

        # 2. Not cached - Fetch from MLS
        if not property_record.street_address and not property_record.listing_key:
            raise HTTPException(status_code=400, detail="Property missing address or listing key for MLS lookup")

        try:
            if property_record.listing_key:
                fetched_data = await bright_mls_service.get_property_by_id(property_record.listing_key)
            else:
                fetched_data = await bright_mls_service.get_property_by_address(property_record.street_address)
            
            if not fetched_data:
                 raise HTTPException(status_code=404, detail="Property not found on MLS")

            # Extract facts
            facts_src = fetched_data.get('reso_facts', {})

            # Create new PropertyDetails record
            new_details = PropertyDetails(
                property_id=property_id,
                updated_at=datetime.now(timezone.utc)
            )
            
            mapping = {
                "description": "description",
                "interiorFeatures": "interior_features",
                "flooring": "flooring",
                "appliances": "appliances",
                "fireplaces": "fireplaces",
                "levels": "levels",
                "architecturalStyle": "architectural_style",
                "constructionMaterials": "construction_materials",
                "roofType": "roof_type",
                "structureType": "structure_type",
                "cooling": "cooling",
                "heating": "heating",
                "waterSource": "water_source",
                "sewer": "sewer",
                "garageSpaces": "garage_spaces",
                "parkingFeatures": "parking_features",
                "hasGarage": "has_garage",
                "associationFee": "association_fee",
                "associationFeeFrequency": "association_fee_frequency",
                "associationAmenities": "association_amenities",
                "hasAssociation": "has_association",
                "schoolDistrictName": "school_district_name",
                "elementarySchool": "elementary_school",
                "highSchool": "high_school",
                "county": "county",
                "zoning": "zoning",
                "taxAnnualAmount": "tax_annual_amount"
            }

            for src_key, db_col in mapping.items():
                if hasattr(new_details, db_col) and src_key in facts_src:
                    setattr(new_details, db_col, facts_src[src_key])

            # Standardized photo saving
            new_details.photos = fetched_data.get('photos', [])
            new_details.days_on_market = fetched_data.get('days_on_market')
            new_details.year_built = fetched_data.get('year_built')
            new_details.standard_status = fetched_data.get('home_status')
            new_details.list_office_name = fetched_data.get('list_office_name')
            new_details.list_agent_full_name = fetched_data.get('list_agent_full_name')
            new_details.list_agent_email = fetched_data.get('list_agent_email')
            new_details.list_office_phone = fetched_data.get('list_office_phone')

            db.add(new_details)
            await db.commit()
            await db.refresh(new_details)

            # Build standardized response
            response_details = PropertyDetailResponse.model_validate(fetched_data)
            
            return {
                "success": True,
                "message": "Property details cached successfully",
                "property_id": property_id,
                "from_cache": False,
                "property": response_details.model_dump(by_alias=True, exclude_none=True)
            }
        except Exception as e:
            logger.error(f"Failed to fetch MLS data for cache: {e}")
            raise

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

@router.get("/properties/agent/{agent_id}/listing/{listing_key}", response_model=PropertyAgentResponse, dependencies=[Depends(require_broker_authorization)])
async def get_property_for_agent(
    agent_id: str,
    listing_key: str,
    db: AsyncSession = Depends(get_db)
):
    """Get property data and agent name by agent ID and listing key"""
    try:
        from app.services.user_service import UserService
        agent = await UserService.get_user_by_id(db, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        agent_name = f"{agent.first_name} {agent.last_name}"
        
        try:
            property_data = await bright_mls_service.get_property_by_id(listing_key)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found")
                
            return PropertyAgentResponse(property=property_data, agent_name=agent_name)
        except Exception as e:
            logger.error(f"MLS ID search for agent failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get property for agent", extra={"agent_id": agent_id, "listing_key": listing_key, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get property for agent: {str(e)}")
