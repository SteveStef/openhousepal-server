from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.models.database import Property, OpenHouseEvent, User
from app.utils.auth import get_current_active_user
from app.schemas.open_house import OpenHouseCreateRequest, OpenHouseResponse, OpenHouseFormSubmission, OpenHouseFormResponse
from app.services.open_house_service import OpenHouseService

router = APIRouter()

@router.post("/api/open-houses", response_model=OpenHouseResponse)
async def create_open_house(
    request: OpenHouseCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new open house record"""
    try:
        # Store or update property data first
        stmt = select(Property).where(Property.id == request.property_id)
        result = await db.execute(stmt)
        existing_property = result.scalar_one_or_none()
        
        if existing_property:
            # Update existing property with new data
            existing_property.street_address = request.address
            existing_property.zillow_data = request.property_data
            existing_property.updated_at = datetime.utcnow()
        else:
            # Create new property record
            property_record = Property(
                id=request.property_id,
                street_address=request.address,
                zillow_data=request.property_data,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(property_record)
        
        # Create the open house record
        form_url = f"/open-house/{current_user.id}/{request.property_id}"
        
        open_house = OpenHouseEvent(
            property_id=request.property_id,
            agent_id=current_user.id,
            qr_code=request.qr_code_url,
            form_url=form_url,
            cover_image_url=request.cover_image_url,
            start_time=datetime.utcnow(),  # Default values for now
            end_time=datetime.utcnow(),    # These could be made configurable
            is_active=True,
            created_at=datetime.utcnow()
        )
        
        db.add(open_house)
        await db.commit()
        await db.refresh(open_house)
        
        return OpenHouseResponse(
            id=open_house.id,
            property_id=open_house.property_id,
            address=request.address,
            cover_image_url=request.cover_image_url,
            qr_code_url=request.qr_code_url,
            form_url=form_url,
            created_at=open_house.created_at
        )
        
    except Exception as e:
        print(f"Error creating open house: {e}")
        raise HTTPException(status_code=500, detail="Failed to create open house")

@router.get("/api/open-houses", response_model=List[OpenHouseResponse])
async def get_open_houses(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all open houses for the current agent"""
    try:
        stmt = select(OpenHouseEvent).where(
            OpenHouseEvent.agent_id == current_user.id
        ).order_by(OpenHouseEvent.created_at.desc())
        
        result = await db.execute(stmt)
        open_houses = result.scalars().all()
        
        response_list = []
        for oh in open_houses:
            # Get property details
            prop_stmt = select(Property).where(Property.id == oh.property_id)
            prop_result = await db.execute(prop_stmt)
            property_record = prop_result.scalar_one_or_none()
            
            if property_record:
                # Use stored cover_image_url, with fallback to zillow_data if needed
                cover_image = oh.cover_image_url
                if not cover_image:
                    # Fallback to zillow_data photos if no stored cover image
                    zillow_data = property_record.zillow_data or {}
                    original_photos = zillow_data.get('originalPhotos', [])
                    if isinstance(original_photos, list) and len(original_photos) > 0:
                        cover_image = original_photos[0].get('url', '')
                
                response_list.append(OpenHouseResponse(
                    id=oh.id,
                    property_id=oh.property_id,
                    address=property_record.street_address or "Unknown Address",
                    cover_image_url=cover_image or "",
                    qr_code_url=oh.qr_code,
                    form_url=oh.form_url or f"/open-house/{current_user.id}/{oh.property_id}",
                    created_at=oh.created_at
                ))
        
        return response_list
        
    except Exception as e:
        print(f"Error fetching open houses: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch open houses")

@router.delete("/api/open-houses/{open_house_id}")
async def delete_open_house(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Delete an open house record"""
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
        
        return {"success": True, "message": "Open house deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting open house: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete open house")


@router.post("/open-house/submit", response_model=OpenHouseFormResponse)
async def submit_open_house_form(
    form_data: OpenHouseFormSubmission,
    db: AsyncSession = Depends(get_db)
):
    """
    Submit open house visitor form
    """
    try:
        # Create visitor record and handle collection creation
        visitor = await OpenHouseService.create_visitor(db, form_data)
        
        # If user is interested in similar properties, create a collection
        collection_created = False
        if form_data.interested_in_similar and form_data.property_id:
            collection_created = await OpenHouseService.create_collection_for_visitor(
                db, visitor, form_data
            )
        
        return OpenHouseFormResponse(
            success=True,
            message="Thank you for visiting! We'll be in touch soon.",
            visitor_id=visitor.id,
            collection_created=collection_created
        )
        
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )
    except Exception as e:
        print(f"Open house form submission error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to process form submission"
        )


@router.get("/open-house/property/{qr_code}")
async def get_property_by_qr(
    qr_code: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get property information by QR code
    """
    try:
        property_data = await OpenHouseService.get_property_by_qr_code(db, qr_code)
        
        if not property_data:
            raise HTTPException(
                status_code=404,
                detail="Property not found for this QR code"
            )
            
        return {
            "success": True,
            "property": property_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching property by QR code: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch property information"
        )