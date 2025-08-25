from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.models.database import Property, OpenHouseEvent, User
from app.auth.dependencies import get_current_user

router = APIRouter()

class OpenHouseCreateRequest(BaseModel):
    property_id: str
    property_data: Dict[str, Any]
    address: str
    cover_image_url: str
    qr_code_url: str

class OpenHouseResponse(BaseModel):
    id: str
    property_id: str
    address: str
    cover_image_url: str
    qr_code_url: str
    form_url: str
    created_at: datetime
    is_active: bool

@router.post("/api/open-houses")
async def create_open_house(
    request: OpenHouseCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new open house event"""
    try:
        # Get or create property first
        stmt = select(Property).where(Property.id == request.property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()
        
        if not property_record:
            # Create property if it doesn't exist
            property_record = Property(
                id=request.property_id,
                street_address=request.address,
                zillow_data=request.property_data,
                img_src=request.cover_image_url,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_synced=datetime.utcnow()
            )
            
            # Set property fields from data
            if "price" in request.property_data:
                property_record.price = request.property_data["price"]
            if "beds" in request.property_data:
                property_record.bedrooms = request.property_data["beds"]
            if "baths" in request.property_data:
                property_record.bathrooms = request.property_data["baths"]
            if "sqft" in request.property_data:
                property_record.living_area = request.property_data["sqft"]
            if "lotSize" in request.property_data:
                property_record.lot_size = request.property_data["lotSize"]
            if "yearBuilt" in request.property_data:
                property_record.year_built = request.property_data["yearBuilt"]
            if "propertyType" in request.property_data:
                property_record.home_type = request.property_data["propertyType"]
            if "latitude" in request.property_data:
                property_record.latitude = request.property_data["latitude"]
            if "longitude" in request.property_data:
                property_record.longitude = request.property_data["longitude"]
            if "zpid" in request.property_data:
                property_record.zpid = request.property_data["zpid"]
            
            db.add(property_record)
        
        # Generate form URL
        form_url = f"/open-house/{current_user.id}/{request.property_id}"
        
        # Create open house event
        open_house = OpenHouseEvent(
            property_id=request.property_id,
            agent_id=current_user.id,
            qr_code=request.qr_code_url,
            start_time=datetime.utcnow(),  # For now, set to current time
            end_time=datetime.utcnow(),    # For now, set to current time
            is_active=True,
            form_url=form_url,
            created_at=datetime.utcnow()
        )
        
        db.add(open_house)
        await db.commit()
        await db.refresh(open_house)
        
        return {
            "id": open_house.id,
            "property_id": open_house.property_id,
            "address": request.address,
            "cover_image_url": request.cover_image_url,
            "qr_code_url": request.qr_code_url,
            "form_url": open_house.form_url,
            "created_at": open_house.created_at,
            "is_active": open_house.is_active
        }
        
    except Exception as e:
        await db.rollback()
        print(f"Error creating open house: {e}")
        raise HTTPException(status_code=500, detail="Failed to create open house")

@router.get("/api/open-houses")
async def get_agent_open_houses(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all open houses for the current agent"""
    try:
        stmt = (
            select(OpenHouseEvent, Property)
            .join(Property, OpenHouseEvent.property_id == Property.id)
            .where(OpenHouseEvent.agent_id == current_user.id)
            .order_by(OpenHouseEvent.created_at.desc())
        )
        result = await db.execute(stmt)
        open_houses = result.fetchall()
        
        response = []
        for open_house_event, property_record in open_houses:
            response.append({
                "id": open_house_event.id,
                "property_id": open_house_event.property_id,
                "address": property_record.street_address,
                "cover_image_url": property_record.img_src,
                "qr_code_url": open_house_event.qr_code,
                "form_url": open_house_event.form_url,
                "created_at": open_house_event.created_at,
                "is_active": open_house_event.is_active
            })
        
        return {"open_houses": response}
        
    except Exception as e:
        print(f"Error getting open houses: {e}")
        raise HTTPException(status_code=500, detail="Failed to get open houses")

@router.delete("/api/open-houses/{open_house_id}")
async def delete_open_house(
    open_house_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete an open house event"""
    try:
        stmt = select(OpenHouseEvent).where(
            and_(
                OpenHouseEvent.id == open_house_id,
                OpenHouseEvent.agent_id == current_user.id
            )
        )
        result = await db.execute(stmt)
        open_house = result.scalar_one_or_none()
        
        if not open_house:
            raise HTTPException(status_code=404, detail="Open house not found")
        
        await db.delete(open_house)
        await db.commit()
        
        return {"message": "Open house deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        print(f"Error deleting open house: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete open house")
