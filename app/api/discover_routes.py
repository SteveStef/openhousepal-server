from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
from app.models.database import DiscoveryPreferences, User, Property, Brokerage
from dotenv import load_dotenv
from typing import Literal, Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.config.logging import get_logger
from app.utils.auth import get_current_active_user, require_mls_access, require_broker_authorization
from app.utils.geo import get_lat_long_offsets, is_within_distance, haversine_distance, filter_properties_by_radius, distance_miles_sql
from app.utils.listing_dom import resolve_days_on_market
from app.services.open_house_manager_service import hosting_window
import os

logger = get_logger(__name__)

load_dotenv()

router = APIRouter(tags=["discovery"])

class DiscoveryPropertyResponse(BaseModel):
    ListingKey: str
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
    AgentOnPlatform: bool = False
    # Only meaningful when AgentOnPlatform is True - whether the CURRENT viewer specifically is
    # eligible to Request to Host this listing (brokerage match, or an accepted My Team member of
    # the listing agent - see open_house_manager_service._is_eligible_requester). True when
    # AgentOnPlatform is False since eligibility doesn't apply to the email-fallback flow.
    HostRequestEligible: bool = True
    # Only set when AgentOnPlatform is True - their uploaded OpenHousePal headshot, so Discovery
    # can show a real photo next to their name instead of the generic placeholder icon.
    ListAgentPhotoUrl: Optional[str] = None
    # Only set when AgentOnPlatform is True - the owner's own user id, so Discovery's Team filter
    # can match a listing against whichever of the viewer's (possibly several) Open House Teams
    # they've selected, without a second lookup per listing.
    OnPlatformAgentId: Optional[str] = None
    # Coming Soon: "coming_soon" = no go-live date yet (no open houses until it goes active);
    # HostsFrom = the first day an open house is allowed (open_house_manager_service.hosting_window).
    HostingBlocked: Optional[str] = None
    HostsFrom: Optional[str] = None

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
    excluded_agents: Optional[List[str]] = None
    agent_filter_mode: Optional[str] = None

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
    excluded_agents: Optional[List[str]] = None
    agent_filter_mode: Optional[Literal["exclude", "include"]] = None

class GetDiscoveryResponse(BaseModel):
    success: bool
    data: List[DiscoveryPropertyResponse]
    preferences: Optional[DiscoveryPreferencesResponse] = None

@router.get("/api/discovery", response_model=GetDiscoveryResponse, dependencies=[Depends(require_broker_authorization)])
async def get_discovery(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access)
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

        # 1. Location Precedence: Radius
        # Bounding box is a cheap, index-friendly pre-filter; the exact circle is enforced right
        # after it with a haversine WHERE, so the LIMIT further down caps listings that are
        # genuinely inside the radius rather than an arbitrary nearest-ish slice of the box taken
        # before the circle was checked (which made a wider radius surface nothing new once the box
        # already held more than the cap).
        is_radius_search = prefs.latitude is not None and prefs.longitude is not None and prefs.miles
        radius_distance_expr = None
        if is_radius_search:
            lat_off, lon_off = get_lat_long_offsets(prefs.latitude, prefs.miles)
            filters.append(and_(
                Property.latitude >= float(prefs.latitude) - lat_off,
                Property.latitude <= float(prefs.latitude) + lat_off,
                Property.longitude >= float(prefs.longitude) - lon_off,
                Property.longitude <= float(prefs.longitude) + lon_off
            ))
            radius_distance_expr = distance_miles_sql(
                Property.latitude, Property.longitude, float(prefs.latitude), float(prefs.longitude)
            )
            filters.append(radius_distance_expr <= float(prefs.miles))

        # 2. Base Integrity Filters
        # Only search for ACTIVE or COMING SOON listings
        filters.append(or_(
            Property.home_status == 'ACTIVE-BRIGHT',
            Property.home_status == 'COMING SOON-BRIGHT'
        ))

        # Agent-entered "My Listings" rows are not real MLS inventory and must never appear in
        # the Discovery marketplace as an open house another agent could request to host.
        filters.append(Property.is_manual.is_(False))

        # Exclude LAND
        filters.append(Property.home_type != 'LAND')

        # Exclude rentals - Discovery is for agents looking to host an open house on a home for
        # sale, not a lease listing.
        filters.append(Property.home_type != 'RESIDENTIAL_LEASE')
        # ...or commercial space (shops, offices) - not a home anyone holds an open house on.
        filters.append(or_(Property.home_type.is_(None), Property.home_type != 'COMMERCIAL'))

        # Some rentals still slip through mis-tagged as a for-sale PropertyType on Bright's end
        # (listing agent data-entry error) - no genuine home-for-sale lists for under $50k, so this
        # catches what the home_type check above misses.
        filters.append(Property.price >= 50000)

        # 3. Inclusion Filters (These refine the radius search if it exists)
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

        # Apply brokerage filter
        if prefs.brokerages and len(prefs.brokerages) > 0:
            brokerage_stmt = select(Brokerage.name).where(
                or_(
                    Brokerage.parent_name.in_(prefs.brokerages),
                    Brokerage.name.in_(prefs.brokerages)
                )
            )
            brokerage_result = await db.execute(brokerage_stmt)
            specific_office_names = brokerage_result.scalars().all()
            unique_names = list(set(specific_office_names))
            filters.append(Property.list_office_name.in_(unique_names))

        # Agent filter - same picked list, two opposite modes: 'exclude' hides those agents'
        # listings (default), 'include' flips it to show ONLY those agents' listings.
        if prefs.excluded_agents and len(prefs.excluded_agents) > 0:
            if prefs.agent_filter_mode == 'include':
                filters.append(Property.list_agent_full_name.in_(prefs.excluded_agents))
            else:
                filters.append(or_(
                    Property.list_agent_full_name.notin_(prefs.excluded_agents),
                    Property.list_agent_full_name.is_(None)
                ))

        # Apply all filters
        if filters:
            prop_stmt = prop_stmt.where(and_(*filters))

        # Nearest-first by true great-circle distance, before the cap. Combined with the haversine
        # WHERE above this means the LIMIT keeps the closest listings that are actually in the
        # circle - so widening the radius genuinely pulls in the next ring of listings once the
        # nearer ones are exhausted, instead of the cap staying full of rows inside the old radius.
        if is_radius_search and radius_distance_expr is not None:
            prop_stmt = prop_stmt.order_by(radius_distance_expr.asc(), Property.listing_key)
        else:
            # Without a radius the cap used to take ANY 50 rows (no order) - a different set on every
            # load, and the newest listings could be missing. Newest first, same as the page sorts.
            prop_stmt = prop_stmt.order_by(Property.mls_list_date.desc().nullslast(), Property.listing_key)

        # Limit results (a touch higher for a radius search - a few rows can still be trimmed by the
        # float-parity safety net below).
        prop_stmt = prop_stmt.limit(250 if is_radius_search else 50)
        
        prop_result = await db.execute(prop_stmt)
        properties = prop_result.scalars().all()

        # 4. Circular filter is already applied in SQL (haversine WHERE above); this is now just a
        #    float-parity safety net (Postgres vs. Python) and drops any row whose coords went NULL.
        if is_radius_search:
            properties = filter_properties_by_radius(
                properties, 
                float(prefs.latitude), 
                float(prefs.longitude), 
                float(prefs.miles)
            )

        # A relisted home can still have its old listing marked active in the feed (Bright doesn't
        # always expire it) - show each house once, as its newest listing, so a host never asks
        # about a stale listing whose agent isn't the current one.
        from datetime import datetime as _dt, timezone as _tz
        from app.utils.same_home import dedupe_same_homes
        _floor = _dt.min.replace(tzinfo=_tz.utc)
        _newest_first = sorted(properties, key=lambda p: (p.mls_list_date or _floor, p.updated_at or _floor), reverse=True)
        _keep = {id(p) for p in dedupe_same_homes(_newest_first)}
        properties = [p for p in properties if id(p) in _keep]

        # Bulk-check which of these listings' agents already have an OpenHousePal account, so the
        # frontend can skip straight to an email template for the ones who don't - matches the
        # same "My Listings" ownership check (list_agent_mls_id / co_list_agent_mls_id -> User.mls_id).
        mls_ids = {p.list_agent_mls_id for p in properties if p.list_agent_mls_id} | \
                  {p.co_list_agent_mls_id for p in properties if p.co_list_agent_mls_id}
        known_mls_ids = set()
        owner_by_mls_id: dict = {}
        if mls_ids:
            known_result = await db.execute(select(User).where(User.mls_id.in_(mls_ids)))
            owner_by_mls_id = {u.mls_id: u for u in known_result.scalars().all()}
            known_mls_ids = set(owner_by_mls_id.keys())

        # Batch-resolve which of those on-platform owners the current agent is an accepted My
        # Team member of, so per-listing eligibility (brokerage match OR team member) is a single
        # extra query instead of one per row - see open_house_manager_service._is_eligible_requester,
        # which this mirrors for display purposes (create_host_request re-enforces it server-side).
        owner_ids = {u.id for u in owner_by_mls_id.values()}
        team_owner_ids = set()
        if owner_ids:
            from app.models.database import OpenHouseTeamMember
            team_result = await db.execute(
                select(OpenHouseTeamMember.owner_id).where(
                    OpenHouseTeamMember.owner_id.in_(owner_ids),
                    OpenHouseTeamMember.member_agent_id == current_user.id,
                    OpenHouseTeamMember.status == "accepted",
                )
            )
            team_owner_ids = {row[0] for row in team_result.all()}

        def _host_request_eligible(owner: Optional[User]) -> bool:
            if owner is None:
                return True
            if owner.id in team_owner_ids:
                return True
            if owner.restrict_host_requests_to_team:
                return False
            return bool(owner.brokerage) and bool(current_user.brokerage) and owner.brokerage.strip().lower() == current_user.brokerage.strip().lower()

        # Map to response format
        response_data = []

        for p in properties:
            # Check if it's "new" (e.g., listed within last 3 days)
            resolved_dom = resolve_days_on_market(
                p.days_on_market, p.home_status, p.mls_list_date,
            )
            days_on_market = resolved_dom if resolved_dom is not None else 0
            is_new = resolved_dom is not None and resolved_dom < 3

            # Calculate precise distance if landmark exists
            dist = None
            if is_radius_search and p.latitude and p.longitude:
                dist = haversine_distance(prefs.latitude, prefs.longitude, p.latitude, p.longitude)

            on_platform_owner = owner_by_mls_id.get(p.list_agent_mls_id) or owner_by_mls_id.get(p.co_list_agent_mls_id)
            hosting_blocked, hosts_from = hosting_window(p.home_status, p.raw_mls_data)

            response_data.append(DiscoveryPropertyResponse(
                ListingKey=str(p.listing_key),
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
                DistanceFromLandmark=dist,
                AgentOnPlatform=bool(
                    (p.list_agent_mls_id and p.list_agent_mls_id in known_mls_ids) or
                    (p.co_list_agent_mls_id and p.co_list_agent_mls_id in known_mls_ids)
                ),
                HostRequestEligible=_host_request_eligible(on_platform_owner),
                ListAgentPhotoUrl=on_platform_owner.photo_url if on_platform_owner else None,
                OnPlatformAgentId=on_platform_owner.id if on_platform_owner else None,
                HostingBlocked=hosting_blocked,
                HostsFrom=hosts_from.isoformat() if hosts_from else None,
            ))

        # Sort by distance if applicable
        if is_radius_search:
            response_data.sort(key=lambda x: x.DistanceFromLandmark if x.DistanceFromLandmark is not None else 999999)
        else:
            # Fallback sort by DOM (newest first)
            response_data.sort(key=lambda x: x.DaysOnMarket)

        # Truncate final results
        response_data = response_data[:50]

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
                school_districts=prefs.school_districts,
                excluded_agents=prefs.excluded_agents,
                agent_filter_mode=prefs.agent_filter_mode
            )
        }

    except Exception as e:
        logger.error(f"Failed to get discovery: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while fetching discovery results")

@router.patch("/api/discovery/preferences", response_model=GetDiscoveryResponse, dependencies=[Depends(require_broker_authorization)])
async def update_discovery_preferences(
    request: UpdateDiscoveryPreferencesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access)
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
        update_data = request.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(prefs, key, value)

        await db.commit()
        await db.refresh(prefs)

        # Return updated state (same as GET)
        return await get_discovery(db=db, current_user=current_user)

    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update discovery preferences: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while updating preferences")

