from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Dict, Any, Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.database import Property
from app.services.zillow_service import ZillowService
import json
from datetime import datetime
from typing import Any, Dict
from app.models.property import PropertyDetailResponse, PropertySaveResponse, PropertyLookupRequest

router = APIRouter()

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
            existing_property.updated_at = datetime.utcnow()
            existing_property.last_synced = datetime.utcnow()
            
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
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_synced=datetime.utcnow()
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
        print(f"Error storing property: {e}")
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
        print(f"Error getting property: {e}")
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
    print(f"[CACHE] Starting cache request for property_id: {property_id}")
    try:
        print(f"[CACHE] Querying database for property: {property_id}")
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()
        
        if not property_record:
            print(f"[CACHE] Property not found: {property_id}")
            raise HTTPException(status_code=404, detail="Property not found")
            
        print(f"[CACHE] Found property: {property_record.street_address}")
        
        # Check if cache is still valid (1 day expiry)
        if property_record.detailed_data_cached and property_record.detailed_data_cached_at:
            print(f"[CACHE] Checking cache validity...")
            from datetime import timedelta
            expiry_time = property_record.detailed_data_cached_at + timedelta(days=1)
            
            if datetime.utcnow() < expiry_time and property_record.detailed_property:
                print(f"[CACHE] Cache is valid, returning cached data")
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
                    else:
                        print(f"[CACHE] Cached data is invalid/empty, will fetch fresh data")
                except Exception as cache_error:
                    print(f"[CACHE] Error reading cached data: {cache_error}, will fetch fresh data")
            else:
                print(f"[CACHE] Cache expired or missing, fetching fresh data")
        
        if not property_record.street_address:
            print(f"[CACHE] No address found for property")
            raise HTTPException(status_code=400, detail="Property missing address for Zillow lookup")
        
        print(f"[CACHE] Calling Zillow API for address: {property_record.street_address}")
        zillow_service = ZillowService()
        details = await zillow_service.get_property_by_address(property_record.street_address, True)
        
        if not details:
            print(f"[CACHE] No details returned from Zillow API")
            raise HTTPException(status_code=404, detail="Property details not found on Zillow")
        
        print(f"[CACHE] Received details from Zillow, type: {type(details)}")
        
        # Convert ZillowPropertyDetailResponse Pydantic model to dict
        print(f"[CACHE] Converting details to dict...")
        try:
            # Use model_dump with mode='json' to handle datetime serialization
            if hasattr(details, 'model_dump'):
                details_dict = details.model_dump(mode='json')
            else:
                details_dict = details.dict()
                # Manual datetime conversion for older pydantic versions
                details_dict = _convert_datetimes_to_strings(details_dict)
            print(f"[CACHE] Converted to dict with {len(details_dict)} keys")
        except Exception as e:
            print(f"[CACHE] Error converting details to dict: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to process property details")
        
        # Update property with cached details in a transaction
        print(f"[CACHE] Saving to database...")
        try:
            property_record.detailed_property = details_dict
            property_record.detailed_data_cached = True
            property_record.detailed_data_cached_at = datetime.utcnow()
            property_record.updated_at = datetime.utcnow()
            
            await db.commit()
            print(f"[CACHE] Successfully committed to database")
            
            return {
                "success": True,
                "message": "Property details cached successfully",
                "cached_at": property_record.detailed_data_cached_at.isoformat(),
                "property_id": property_id,
                "from_cache": False,
                "details": details_dict
            }
        except Exception as db_error:
            print(f"[CACHE] Database error: {str(db_error)}")
            await db.rollback()
            print(f"[CACHE] Rolled back transaction due to database error")
            raise HTTPException(status_code=500, detail=f"Failed to save property details to database: {str(db_error)}")
        
    except HTTPException as http_ex:
        print(f"[CACHE] HTTP Exception: {http_ex.status_code} - {http_ex.detail}")
        # Ensure rollback on HTTP exceptions too
        try:
            await db.rollback()
            print(f"[CACHE] Rolled back transaction due to HTTP exception")
        except Exception as rollback_error:
            print(f"[CACHE] Warning: Failed to rollback after HTTP exception: {rollback_error}")
        raise
    except Exception as e:
        print(f"[CACHE] Unexpected error: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[CACHE] Traceback: {traceback.format_exc()}")
        # Ensure rollback on any unexpected exception
        try:
            await db.rollback()
            print(f"[CACHE] Rolled back transaction due to unexpected error")
        except Exception as rollback_error:
            print(f"[CACHE] Warning: Failed to rollback after unexpected error: {rollback_error}")
        raise HTTPException(status_code=500, detail=f"Failed to cache property details: {str(e)}")

