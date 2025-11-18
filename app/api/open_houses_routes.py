from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.models.database import OpenHouseEvent, User, OpenHouseVisitor, Notification
from app.utils.auth import get_current_active_user, require_basic_plan
from app.schemas.open_house import OpenHouseCreateRequest, OpenHouseResponse, OpenHouseFormSubmission, OpenHouseFormResponse, VisitorResponse
from app.services.open_house_service import OpenHouseService
from app.services.email_service import EmailService
import urllib.parse
import uuid
from datetime import datetime, timedelta

import os
from dotenv import load_dotenv
from app.config.logging import get_logger

logger = get_logger(__name__)

load_dotenv()

router = APIRouter()

# is this route authed for people that have trial, premium, or basic
@router.post("/api/open-houses", response_model=OpenHouseResponse)
async def create_open_house(
    request: OpenHouseCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan)
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
            
            # Property metadata from request - use extracted string fields
            address=street_address,
            abbreviated_address=abbreviated_addr,
            house_type=property_data.get('homeType'),
            latitude=property_data.get('latitude'),
            longitude=property_data.get('longitude'),
            lot_size=property_data.get('lot_size'),
            city=city,
            state=state,
            zipcode=zipcode,
            bedrooms=property_data.get('bedrooms'),
            bathrooms=property_data.get('bathrooms'),
            price=property_data.get('price'),
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
            living_area=open_house.lot_size,
            price=open_house.price,
            created_at=open_house.created_at
        )
        
    except Exception as e:
        logger.error("creating open house failed", extra={"error": str(e)})
        
        if "Error binding parameter" in str(e):
            raise HTTPException(status_code=500, detail="Invalid property data structure for database storage")
        
        raise HTTPException(status_code=500, detail="Failed to create open house")

@router.get("/api/open-houses", response_model=List[OpenHouseResponse])
async def get_open_houses(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all active (non-deleted) open houses for the current agent"""
    try:
        stmt = select(OpenHouseEvent).where(
            and_(
                OpenHouseEvent.agent_id == current_user.id,
                OpenHouseEvent.is_deleted == False  # Only show active listings
            )
        ).order_by(OpenHouseEvent.created_at.desc())

        result = await db.execute(stmt)
        open_houses = result.scalars().all()
        
        response_list = []
        for oh in open_houses:
            response_list.append(OpenHouseResponse(
                id=oh.id,
                open_house_event_id=oh.id,
                address=oh.address or "Unknown Address",
                cover_image_url=oh.cover_image_url or "",
                qr_code_url=oh.qr_code,
                form_url=oh.form_url or f"/open-house/{oh.id}",
                bedrooms=oh.bedrooms,
                bathrooms=oh.bathrooms,
                living_area=oh.lot_size,  # Column dropped from database
                price=oh.price,
                created_at=oh.created_at
            ))
        
        return response_list
        
    except Exception as e:
        logger.error("fetching open houses failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch open houses")

@router.delete("/api/open-houses/{open_house_id}")
async def delete_open_house(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Soft delete an open house record (preserves data integrity with collections)"""
    try:
        stmt = select(OpenHouseEvent).where(
            and_(
                OpenHouseEvent.id == open_house_id,
                OpenHouseEvent.agent_id == current_user.id,
                OpenHouseEvent.is_deleted == False  # Only allow deleting non-deleted records
            )
        )
        result = await db.execute(stmt)
        open_house = result.scalar_one_or_none()

        if not open_house:
            raise HTTPException(status_code=404, detail="Open house not found")

        # Soft delete: mark as deleted and set timestamp
        open_house.is_deleted = True
        open_house.deleted_at = datetime.utcnow()

        await db.commit()

        return {
            "success": True,
            "message": "Open house removed from your listings. All related collections and visitor data are preserved.",
            "deleted_at": open_house.deleted_at
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("soft deleting open house failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to remove open house")


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

        # If user is interested in similar properties and doesn't already have an agent, create a collection
        collection_result = {"success": False, "properties_added": 0}
        if form_data.interested_in_similar and form_data.open_house_event_id and form_data.has_agent.value != "YES":
            collection_result = await OpenHouseService.create_collection_for_visitor(
                db, visitor, form_data
            )
        
        # Customize message based on collection creation result
        message = "Thank you for visiting! We'll be in touch soon."
        if collection_result["success"] and collection_result["properties_added"] > 0:
            message = f"Thank you for visiting! We've found {collection_result['properties_added']} similar properties for you. We'll be in touch soon with your personalized collection."
        elif collection_result["success"]:
            message = "Thank you for visiting! We've created a personalized collection for you and will be in touch soon with matching properties."

        # Send visitor confirmation email with showcase link
        if collection_result["success"] and collection_result.get('share_token'):
            property_data = await OpenHouseService.get_property_by_qr_code(db, form_data.open_house_event_id)
            if property_data:
                email_service = EmailService()
                frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
                showcase_link = f"{frontend_url}/showcase/{collection_result['share_token']}"

                email_service.send_simple_message(
                    to_email=visitor.email,
                    subject=f"Your Personalized Property Collection - {property_data.get('address', 'Open House')}",
                    template="visitor_confirmation",
                    template_variables={
                        "visitor_name": visitor.full_name,
                        "property_address": property_data.get('address', 'the property'),
                        "showcase_link": showcase_link,
                        "properties_count": collection_result.get('properties_added', 0)
                    }
                )

        # Create notification for agent
        if form_data.open_house_event_id:
            try:
                # Get the open house event to find the agent_id
                open_house_query = select(OpenHouseEvent).where(OpenHouseEvent.id == form_data.open_house_event_id)
                open_house_result = await db.execute(open_house_query)
                open_house_event = open_house_result.scalar_one_or_none()

                if open_house_event:
                    property_data_for_notif = await OpenHouseService.get_property_by_qr_code(db, form_data.open_house_event_id)

                    notification = Notification(
                        agent_id=open_house_event.agent_id,
                        type="OPEN_HOUSE_SIGN_IN",
                        reference_type="VISITOR",
                        reference_id=visitor.id,
                        title=f"New Open House Visitor: {visitor.full_name}",
                        message="Signed in at your open house" + (f" - {property_data_for_notif.get('address')}" if property_data_for_notif else ""),
                        collection_id=collection_result.get('collection_id') if collection_result.get('success') else None,
                        collection_name=collection_result.get('collection_id') if collection_result.get('success') else None,
                        property_address=property_data_for_notif.get('address') if property_data_for_notif else None,
                        visitor_name=visitor.full_name,
                        is_read=False,
                        created_at=datetime.utcnow()
                    )

                    db.add(notification)
                    await db.commit()
            except Exception as e:
                logger.error("creating notification failed", extra={"error": str(e)})
                # Don't fail the whole request if notification creation fails

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
        logger.error("fetching property by QR code failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch property information"
        )

@router.get("/api/open-houses/{open_house_id}/visitors", response_model=List[VisitorResponse])
async def get_open_house_visitors(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all visitors for a specific open house (must be owned by current agent)"""
    try:
        # First verify the open house exists and belongs to the current user
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

        # Get all visitors for this open house
        visitor_stmt = select(OpenHouseVisitor).where(
            OpenHouseVisitor.open_house_event_id == open_house_id
        ).order_by(OpenHouseVisitor.created_at.desc())

        visitor_result = await db.execute(visitor_stmt)
        visitors = visitor_result.scalars().all()

        return [
            VisitorResponse(
                id=visitor.id,
                full_name=visitor.full_name,
                email=visitor.email,
                phone=visitor.phone,
                has_agent=visitor.has_agent,
                interested_in_similar=visitor.interested_in_similar,
                created_at=visitor.created_at
            )
            for visitor in visitors
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching visitors failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch visitors")
