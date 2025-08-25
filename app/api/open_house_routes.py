from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.database import get_db
from app.schemas.open_house import OpenHouseFormSubmission, OpenHouseFormResponse
from app.models.database import Property, OpenHouseVisitor
from app.services.open_house_service import OpenHouseService

router = APIRouter(prefix="/open-house", tags=["open-house"])


@router.post("/submit", response_model=OpenHouseFormResponse)
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        print(f"Open house form submission error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process form submission"
        )


@router.get("/property/{qr_code}")
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
                status_code=status.HTTP_404_NOT_FOUND,
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
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch property information"
        )
