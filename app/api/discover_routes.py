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
import os

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
    DaysOnMarket: int
    Url: str
    New: bool
    DistanceFromLandmark: Optional[float] = None

class DiscoveryPreferencesResponse(BaseModel):
    landmark_address: Optional[str] = None
    miles: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    state: Optional[str] = None
    min_bedrooms: Optional[int] = None
    min_bathrooms: Optional[int] = None
    min_square_feet: Optional[int] = None
    min_price: Optional[float] = None
    brokerages: Optional[List[str]] = None
    cities: Optional[List[str]] = None
    townships: Optional[List[str]] = None
    school_districts: Optional[List[str]] = None

class UpdateDiscoveryPreferencesRequest(BaseModel):
    landmark_address: Optional[str] = None
    miles: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    state: Optional[str] = None
    min_bedrooms: Optional[int] = None
    min_bathrooms: Optional[int] = None
    min_square_feet: Optional[int] = None
    min_price: Optional[float] = None
    brokerages: Optional[List[str]] = None
    cities: Optional[List[str]] = None
    townships: Optional[List[str]] = None
    school_districts: Optional[List[str]] = None

class GetDiscoveryResponse(BaseModel):
    success: bool
    data: List[DiscoveryPropertyResponse]
    preferences: Optional[DiscoveryPreferencesResponse] = None

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
            return {"success": True, "data": [], "preferences": None}

        # Build the property query
        prop_stmt = select(Property)
        filters = []

        # Apply state filter if set
        if prefs.state:
            filters.append(Property.state.ilike(prefs.state))

        if prefs.min_bedrooms:
            filters.append(Property.bedrooms >= prefs.min_bedrooms)
        if prefs.min_bathrooms:
            filters.append(Property.bathrooms >= prefs.min_bathrooms)
        if prefs.min_square_feet:
            filters.append(Property.living_area >= prefs.min_square_feet)
        if prefs.min_price:
            filters.append(Property.price >= prefs.min_price)

        # Apply city filters
        if prefs.cities and len(prefs.cities) > 0:
            # Cities are stored as "CITY, STATE"
            city_names = [c.split(',')[0].strip() for c in prefs.cities]
            filters.append(Property.city.in_(city_names))

        # Apply school district filters
        if prefs.school_districts and len(prefs.school_districts) > 0:
            # Districts are stored as "DISTRICT, STATE"
            district_names = [d.split(',')[0].strip() for d in prefs.school_districts]
            filters.append(Property.school_district_name.in_(district_names))

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
            days_on_market = 0
            if p.mls_list_date is not None:
                list_date = p.mls_list_date
                if list_date.tzinfo is None:
                    list_date = list_date.replace(tzinfo=timezone.utc)
                diff = now - list_date
                is_new = diff < timedelta(days=3)
                days_on_market = diff.days

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
                Url=f"{os.getenv("CLIENT_URL") or "https://openhousepal.com"}/property/{p.listing_key}/{current_user.id}",
                ListAgentFullName=str(p.list_agent_full_name) if p.list_agent_full_name is not None else "Unknown",
                ListAgentPreferredPhone=str(p.list_agent_preferred_phone) if p.list_agent_preferred_phone is not None else "N/A",
                ListOfficeName=str(p.list_office_name) if p.list_office_name is not None else "Unknown",
                ListOfficePhone=str(p.list_office_phone) if p.list_office_phone is not None else "N/A",
                ListingAgentEmail=str(p.list_agent_email) if p.list_agent_email is not None else "",
                DaysOnMarket=days_on_market,
                New=is_new,
                DistanceFromLandmark=None
            ))

        return {
            "success": True, 
            "data": response_data,
            "preferences": DiscoveryPreferencesResponse(
                landmark_address=prefs.landmark_address,
                miles=prefs.miles,
                latitude=prefs.latitude,
                longitude=prefs.longitude,
                state=prefs.state,
                min_bedrooms=prefs.min_bedrooms,
                min_bathrooms=prefs.min_bathrooms,
                min_square_feet=prefs.min_square_feet,
                min_price=prefs.min_price,
                brokerages=prefs.brokerages,
                cities=prefs.cities,
                townships=prefs.townships,
                school_districts=prefs.school_districts
            )
        }

    except Exception as e:
        logger.error(f"Failed to get discovery: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while fetching discovery results")

@router.patch("/api/discovery/preferences", response_model=GetDiscoveryResponse, dependencies=[Depends(require_broker_authorization)])
async def update_discovery_preferences(
    request: UpdateDiscoveryPreferencesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan)
):
    """
    Update discovery preferences for the current user.
    """
    try:
        # Get the discovery preferences for this user
        stmt = select(DiscoveryPreferences).where(
            DiscoveryPreferences.user_id == current_user.id
        )
        result = await db.execute(stmt)
        prefs = result.scalar_one_or_none()

        if not prefs:
            # Create if doesn't exist
            prefs = DiscoveryPreferences(user_id=current_user.id)
            db.add(prefs)
        
        # Update fields if provided
        if request.landmark_address is not None:
            prefs.landmark_address = request.landmark_address
        if request.miles is not None:
            prefs.miles = request.miles
        if request.latitude is not None:
            prefs.latitude = request.latitude
        if request.longitude is not None:
            prefs.longitude = request.longitude
        if request.state is not None:
            prefs.state = request.state
        if request.min_bedrooms is not None:
            prefs.min_bedrooms = request.min_bedrooms
        if request.min_bathrooms is not None:
            prefs.min_bathrooms = request.min_bathrooms
        if request.min_square_feet is not None:
            prefs.min_square_feet = request.min_square_feet
        if request.min_price is not None:
            prefs.min_price = request.min_price
        if request.brokerages is not None:
            prefs.brokerages = request.brokerages
        if request.cities is not None:
            prefs.cities = request.cities
        if request.townships is not None:
            prefs.townships = request.townships
        if request.school_districts is not None:
            prefs.school_districts = request.school_districts

        await db.commit()
        await db.refresh(prefs)

        # Return updated state (same as GET)
        return await get_discovery(db=db, current_user=current_user)

    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update discovery preferences: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while updating preferences")

