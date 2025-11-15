from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Dict, Any, Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.database import Property
from app.services.zillow_service import ZillowService
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
            if "zpid" in request.property_data:
                existing_property.zpid = request.property_data["zpid"]
                
        else:
            # Create new property
            new_property = Property(
                id=request.property_id,
                street_address=request.address,
                img_src=request.cover_image_url,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                last_synced=datetime.now(timezone.utc)
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
            if "zpid" in request.property_data:
                new_property.zpid = request.property_data["zpid"]
                
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
            "zpid": property_record.zpid
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
    """Fetch property details from Zillow API without saving to database"""
    zillow_service = ZillowService()
    return await zillow_service.get_property_by_address(request.address)

@router.get("/properties/{property_id}/cache")
async def cache_property_details(
    property_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Cache detailed property information from Zillow API"""
    start_time = datetime.now(timezone.utc)
    try:
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()

        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found")

        # Check if cache is still valid (1 day expiry)
        if property_record.detailed_data_cached and property_record.detailed_data_cached_at:
            from datetime import timedelta
            expiry_time = property_record.detailed_data_cached_at + timedelta(days=1)

            if datetime.now(timezone.utc) < expiry_time and property_record.detailed_property:
                # Validate that cached data is not None/empty before returning
                try:
                    cached_data = property_record.detailed_property
                    if cached_data and isinstance(cached_data, dict) and len(cached_data) > 0:
                        return {
                            "success": True,
                            "message": "Property details already cached and still valid",
                            "cached_at": property_record.detailed_data_cached_at.isoformat(),
                            "expires_at": expiry_time.isoformat(),
                            "property_id": property_id,
                            "from_cache": True,
                            "details": cached_data
                        }
                except Exception:
                    pass  # Fall through to fetch fresh data

        if not property_record.street_address:
            raise HTTPException(status_code=400, detail="Property missing address for Zillow lookup")

        # Fetch from Zillow
        zillow_service = ZillowService()
        details = await zillow_service.get_property_by_address(property_record.street_address, True)

        if not details:
            raise HTTPException(status_code=404, detail="Property details not found on Zillow")

        # Convert Pydantic model to dict
        try:
            if hasattr(details, 'model_dump'):
                details_dict = details.model_dump(mode='json')
            else:
                details_dict = details.dict()
                details_dict = _convert_datetimes_to_strings(details_dict)
        except Exception as e:
            raise HTTPException(status_code=500, detail="Failed to process property details")

        # Update property with cached details
        try:
            property_record.detailed_property = details_dict
            property_record.detailed_data_cached = True
            property_record.detailed_data_cached_at = datetime.now(timezone.utc)
            property_record.updated_at = datetime.now(timezone.utc)

            await db.commit()

            duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            logger.info(
                "Property cache updated successfully",
                extra={
                    "event": "property_cache_updated",
                    "property_id": property_id,
                    "address": property_record.street_address,
                    "duration_ms": round(duration_ms, 2)
                }
            )

            return {
                "success": True,
                "message": "Property details cached successfully",
                "cached_at": property_record.detailed_data_cached_at.isoformat(),
                "property_id": property_id,
                "from_cache": False,
                "details": details_dict
            }
        except Exception as db_error:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to save property details to database: {str(db_error)}")

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

