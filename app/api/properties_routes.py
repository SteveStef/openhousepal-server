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

try:
    from ..models.property import PropertyDetailResponse, PropertySaveResponse, PropertyLookupRequest
except ImportError:
    from models.property import PropertyDetailResponse, PropertySaveResponse, PropertyLookupRequest

router = APIRouter()

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

