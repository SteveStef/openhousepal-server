from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from app.models.database import DiscoveryPreferences, User, Property, Brokerage
from dotenv import load_dotenv
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.config.logging import get_logger
from app.utils.auth import get_current_active_user, require_basic_plan, require_broker_authorization
from app.utils.geo import get_lat_long_offsets, is_within_distance, haversine_distance

logger = get_logger(__name__)

load_dotenv()

router = APIRouter(tags=["discovery"])

class DiscoveryPropertyResponse(BaseModel):
    Street: str
    City: str
    State: str
    Zipcode: str
    Price: str
    Image: str
    Beds: int
    Baths: int
    SquareFeet: int
    ListAgentFullName: str
    ListAgentPreferredPhone: str
    ListOfficeName: str
    ListOfficePhone: str
    ListingAgentEmail: str
    New: bool
    DistanceFromLandmark: Optional[float] = None

class GetDiscoveryResponse(BaseModel):
    success: bool
    data: List[DiscoveryPropertyResponse]

@router.get("/api/discovery", response_model=GetDiscoveryResponse, dependencies=[Depends(require_broker_authorization)])
async def get_discovery(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan)
):
    """
    Retrieve properties based on user's discovery preferences.
    """
    try:
        # Get the discovery preferences for this user
        stmt = select(DiscoveryPreferences).where(
            DiscoveryPreferences.user_id == current_user.id
        )
        result = await db.execute(stmt)
        prefs = result.scalar_one_or_none()

        if not prefs:
            return {"success": True, "data": []}

        # Build the property query
        prop_stmt = select(Property)
        filters = []

        # Apply ONLY brokerage filter
        if prefs.brokerages and len(prefs.brokerages) > 0:
            # 1. Find all specific office names that belong to these parent brokerages
            brokerage_stmt = select(Brokerage.name).where(
                or_(
                    Brokerage.parent_name.in_(prefs.brokerages),
                    Brokerage.name.in_(prefs.brokerages)
                )
            )
            brokerage_result = await db.execute(brokerage_stmt)
            specific_office_names = brokerage_result.scalars().all()
            
            # 2. Combine with user's specific strings
            unique_names = list(set(specific_office_names))
            filters.append(Property.list_office_name.in_(unique_names))

        # Add filters if any (only brokerage in this case)
        if filters:
            prop_stmt = prop_stmt.where(and_(*filters))

        # Limit results
        prop_stmt = prop_stmt.limit(50)
        
        prop_result = await db.execute(prop_stmt)
        properties = prop_result.scalars().all()

        # Map to response format
        now = datetime.now(timezone.utc)
        response_data = []
        
        for p in properties:
            # Check if it's "new" (e.g., listed within last 3 days)
            is_new = False
            if p.mls_list_date is not None:
                list_date = p.mls_list_date
                if list_date.tzinfo is None:
                    list_date = list_date.replace(tzinfo=timezone.utc)
                is_new = (now - list_date) < timedelta(days=3)

            response_data.append(DiscoveryPropertyResponse(
                Street=str(p.street_address),
                City=str(p.city),
                State=str(p.state),
                Zipcode=str(p.zipcode) if p.zipcode is not None else "",
                Price=f"${int(p.price):,}" if p.price is not None else "$0",
                Image=str(p.img_src) if p.img_src is not None else "",
                Beds=int(p.bedrooms) if p.bedrooms is not None else 0,
                Baths=int(p.bathrooms) if p.bathrooms is not None else 0,
                SquareFeet=int(p.living_area) if p.living_area is not None else 0,
                ListAgentFullName=str(p.list_agent_full_name) if p.list_agent_full_name is not None else "Unknown",
                ListAgentPreferredPhone=str(p.list_agent_preferred_phone) if p.list_agent_preferred_phone is not None else "N/A",
                ListOfficeName=str(p.list_office_name) if p.list_office_name is not None else "Unknown",
                ListOfficePhone=str(p.list_office_phone) if p.list_office_phone is not None else "N/A",
                ListingAgentEmail=str(p.list_agent_email) if p.list_agent_email is not None else "",
                New=is_new,
                DistanceFromLandmark=None
            ))

        return {"success": True, "data": response_data}

    except Exception as e:
        logger.error(f"Failed to get discovery: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while fetching discovery results")




