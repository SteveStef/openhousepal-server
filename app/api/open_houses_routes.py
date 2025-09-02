from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.models.database import OpenHouseEvent, User
from app.utils.auth import get_current_active_user
from app.schemas.open_house import OpenHouseCreateRequest, OpenHouseResponse, OpenHouseFormSubmission, OpenHouseFormResponse
from app.services.open_house_service import OpenHouseService
import urllib.parse
import uuid
from datetime import datetime, timedelta

import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

@router.post("/api/open-houses", response_model=OpenHouseResponse)
async def create_open_house(
    request: OpenHouseCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new open house record with property metadata"""
    try:
        open_house_id = request.open_house_event_id or str(uuid.uuid4())
        form_url = f"/open-house/{open_house_id}"
        qr_code_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(os.getenv("CLIENT_URL") + form_url)}"
        
        property_data = request.property_data
        address_data = property_data.get('address', {})
        is_nested_address = isinstance(address_data, dict)
        
        street_address = address_data.get('streetAddress') if is_nested_address else address_data
        city = address_data.get('city') if is_nested_address else property_data.get('city')
        state = address_data.get('state') if is_nested_address else property_data.get('state')
        zipcode = address_data.get('zipcode') if is_nested_address else property_data.get('zipCode')
        
        abbreviated_addr = property_data.get('abbreviatedAddress')
        if not abbreviated_addr and is_nested_address:
            abbreviated_addr = f"{street_address}, {city}, {state}"
        elif not abbreviated_addr:
            abbreviated_addr = street_address
        
        open_house = OpenHouseEvent(
            id=open_house_id,
            agent_id=current_user.id,
            qr_code=qr_code_url,
            form_url=form_url,
            cover_image_url=request.cover_image_url,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(hours=2),
            
            # Property metadata from request - use extracted string fields
            address=street_address,
            abbreviated_address=abbreviated_addr,
            image_src=request.cover_image_url,
            house_type=property_data.get('homeType'),
            latitude=property_data.get('latitude'),
            longitude=property_data.get('longitude'),
            city=city,
            state=state,
            zipcode=zipcode,
            bedrooms=property_data.get('bedrooms'),
            bathrooms=property_data.get('bathrooms'),
            living_area=property_data.get('livingArea'),
            price=property_data.get('price'),
            lot_size=property_data.get('lotSize'),
            year_built=property_data.get('yearBuilt'),
            home_status=property_data.get('homeStatus')
        )
        
        db.add(open_house)
        await db.commit()
        await db.refresh(open_house)
        
        return OpenHouseResponse(
            id=open_house.id,
            open_house_event_id=open_house.id,
            address=open_house.address,
            cover_image_url=open_house.cover_image_url,
            qr_code_url=open_house.qr_code,
            form_url=open_house.form_url,
            bedrooms=open_house.bedrooms,
            bathrooms=open_house.bathrooms,
            living_area=open_house.living_area,
            price=open_house.price,
            created_at=open_house.created_at
        )
        
    except Exception as e:
        print(f"Error creating open house: {e}")
        print(f"Property data that caused error: {property_data}")
        
        if "Error binding parameter" in str(e):
            print("Database parameter binding error - likely trying to store complex object in simple field")
            raise HTTPException(status_code=500, detail="Invalid property data structure for database storage")
        
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
            response_list.append(OpenHouseResponse(
                id=oh.id,
                open_house_event_id=oh.id,
                address=oh.address or "Unknown Address",
                cover_image_url=oh.cover_image_url or oh.image_src or "",
                qr_code_url=oh.qr_code,
                form_url=oh.form_url or f"/open-house/{oh.id}",
                bedrooms=oh.bedrooms,
                bathrooms=oh.bathrooms,
                living_area=oh.living_area,
                price=oh.price,
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
        
        # If user is interested in similar properties, create a collection and fetch properties immediately
        collection_result = {"success": False, "properties_added": 0}
        if form_data.interested_in_similar and form_data.open_house_event_id:
            collection_result = await OpenHouseService.create_collection_for_visitor(
                db, visitor, form_data
            )
        
        # Customize message based on collection creation result
        message = "Thank you for visiting! We'll be in touch soon."
        if collection_result["success"] and collection_result["properties_added"] > 0:
            message = f"Thank you for visiting! We've found {collection_result['properties_added']} similar properties for you. We'll be in touch soon with your personalized collection."
        elif collection_result["success"]:
            message = "Thank you for visiting! We've created a personalized collection for you and will be in touch soon with matching properties."
        
        return OpenHouseFormResponse(
            success=True,
            message=message,
            visitor_id=visitor.id,
            collection_created=collection_result["success"]
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


@router.get("/open-house/property/{open_house_event_id}")
async def get_property_by_qr(
    open_house_event_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get property information by open house event ID
    """
    try:
        property_data = await OpenHouseService.get_property_by_qr_code(db, open_house_event_id)
        
        if not property_data:
            raise HTTPException(
                status_code=404,
                detail="Property not found for this open house event ID"
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
