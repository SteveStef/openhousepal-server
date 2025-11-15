from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.database import get_db
from app.schemas.property_visit import PropertyVisitFormSubmission, PropertyVisitFormResponse
from app.services.property_visit_service import PropertyVisitService

router = APIRouter(prefix="/property-visit", tags=["property-visit"])


@router.post("/submit", response_model=PropertyVisitFormResponse)
async def submit_property_visit_form(
    form_data: PropertyVisitFormSubmission,
    db: AsyncSession = Depends(get_db)
):
    """
    Submit property visit form and create collection if requested
    """
    try:
        # Create collection directly if user is interested in similar properties
        collection_id = None
        if form_data.interested_in_similar and form_data.property_id:
            collection_id = await PropertyVisitService.create_collection_from_visit(db, form_data)
        
        message = "Thank you for visiting!"
        if collection_id:
            message += " We've created a personalized collection of similar properties for you."
        
        return PropertyVisitFormResponse(
            success=True,
            message=message,
            collection_id=collection_id
        )
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process form submission"
        )


