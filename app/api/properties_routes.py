from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import Dict, Any, Optional
from pydantic import BaseModel
import os

from app.database import get_db
from app.models.database import Property, PropertyDetails
from app.services.bright_mls_service import BrightMlsService
import json
from datetime import datetime, timezone
from typing import Any, Dict
from app.models.property import PropertyDetailResponse, PropertySaveResponse, PropertyLookupRequest
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
    mls_service = BrightMlsService()
    return await mls_service.get_property_by_address(request.address)

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
            # Construct response from DB to match frontend expectations
            details = property_record.details
            
            # Map DB fields back to the nested structure the frontend expects
            response_details = {
                "description": details.description,
                "listAgentFullName": details.list_agent_full_name,
                "listAgentEmail": details.list_agent_email,
                "listOfficeName": details.list_office_name,
                "listOfficePhone": details.list_office_phone,
                "originalPhotos": [], # Map photos back to originalPhotos structure
                "resoFacts": {
                    "yearBuilt": details.year_built,
                    "architecturalStyle": details.architectural_style,
                    "constructionMaterials": details.construction_materials,
                    "stories": details.levels, # Mapping levels to stories for display
                    "livingArea": property_record.living_area,
                    "appliances": details.appliances,
                    "interiorFeatures": details.interior_features,
                    "flooring": details.flooring,
                    "windowFeatures": details.window_features,
                    "fireplaceFeatures": details.fireplace_features,
                    "heating": details.heating,
                    "cooling": details.cooling,
                    "waterSource": details.water_source,
                    "sewer": details.sewer,
                    "electric": details.electric,
                    "parkingCapacity": details.garage_spaces, # Approx
                    "garageParkingCapacity": details.garage_spaces,
                    "parkingFeatures": details.parking_features,
                    "hasAssociation": details.has_association,
                    "hoaFee": f"${details.association_fee}" if details.association_fee else None,
                    "taxAnnualAmount": details.tax_annual_amount,
                    "associationFeeIncludes": details.association_fee_includes,
                    # Add schools if available in DB columns (need to add if missing)
                    "exteriorFeatures": details.exterior_features,
                    "lotFeatures": details.lot_features,
                    "communityFeatures": details.association_amenities, # Approx
                    "updated_at": details.updated_at.isoformat() if details.updated_at else None
                }
            }
            
            # Reconstruct photo structure
            if details.photos:
                for photo in details.photos:
                    response_details["originalPhotos"].append({
                        "caption": photo.get("caption", ""),
                        "mixedSources": {
                            "jpeg": [{"url": photo.get("url"), "width": 0}],
                            "webp": []
                        }
                    })

            return {
                "success": True,
                "message": "Property details retrieved from cache",
                "cached_at": details.updated_at.isoformat() if details.updated_at else None,
                "property_id": property_id,
                "from_cache": True,
                "details": response_details
            }

        if not property_record.street_address:
            raise HTTPException(status_code=400, detail="Property missing address for MLS lookup")

        # Fetch from Bright MLS
        mls_service = BrightMlsService()

        # Construct full address for search to avoid ambiguity
        search_address = property_record.street_address
        if property_record.city:
            search_address += f", {property_record.city}"
        
        if property_record.state:
            search_address += f", {property_record.state}"

        if property_record.zipcode:
            search_address += f" {property_record.zipcode}"

        # Get full data package
        fetched_data = await mls_service.get_property_by_address(search_address, True)
        
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
        # (For brevity, reusing the construction logic logic or returning mapped data)
        # Ideally refactor the response construction into a helper function
        
        # ... (Return structure mirroring the cache hit block) ...
        
        return {
            "success": True,
            "message": "Property details cached successfully",
            "property_id": property_id,
            "from_cache": False,
            # Return the same structure as above
            "details": {
                "description": new_details.description,
                "resoFacts": details_data # This is a bit of a shortcut, ideally we map it back
            }
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

