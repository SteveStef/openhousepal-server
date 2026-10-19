from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func, case
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.database import get_db
from app.models.database import Property, User, ScheduledEmail, Notification, SchoolDistrict, Brokerage, City, Township
from app.schemas.collection_preferences import CollectionPreferencesBase
from app.services.property_service import property_service
from app.utils.auth import require_admin_user, require_basic_plan, require_mls_access, require_broker_authorization, get_current_user_optional
from app.utils.normalization import MAJOR_BRANDS
from datetime import datetime, timezone
from typing import Any, Dict
from app.models.property import (
    PropertyDetailResponse,
    PropertyLookupRequest,
    SimilarPropertiesRequest,
    NearbyAmenitiesResponse,
)
from app.services import nearby_amenities_service
from app.services.nces_school_assignment_service import resolve_school_assignment
from app.config.logging import get_logger

router = APIRouter(prefix="/api/properties", tags=["properties"])
logger = get_logger(__name__)

def _convert_datetimes_to_strings(obj: Any) -> Any:
    """Recursively convert datetime objects to ISO format strings"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: _convert_datetimes_to_strings(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetimes_to_strings(item) for item in obj]
    else:
        return obj

class PropertyStoreRequest(BaseModel):
    property_id: str
    property_data: Dict[str, Any]
    address: str
    cover_image_url: Optional[str] = None

class PropertyResponse(BaseModel):
    id: str
    property_data: Dict[str, Any]
    address: str

class AgentMessageRequest(BaseModel):
    agent_id: str
    property_id: str
    property_address: str
    visitor_name: str
    visitor_contact: str
    # Optional mobile number from its own box - so a lead who gave an email can still get a text.
    visitor_phone: Optional[str] = None
    message: str
    # Where the visitor came from ({src, oh, ref} - see services/visit_source.py).
    came_from: Optional[Dict[str, Any]] = None

class ScheduleTourRequest(BaseModel):
    agent_id: str
    property_id: str
    property_address: str
    visitor_name: Optional[str] = None
    visitor_contact: Optional[str] = None
    visitor_phone: Optional[str] = None
    preferred_date: str
    preferred_time: str
    preferred_date_2: Optional[str] = None
    preferred_time_2: Optional[str] = None
    preferred_date_3: Optional[str] = None
    preferred_time_3: Optional[str] = None
    message: Optional[str] = None
    came_from: Optional[Dict[str, Any]] = None

@router.post("")
async def store_property(
    request: PropertyStoreRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan)
):
    """Store property data for later retrieval by ID (Used for manual adds or snapshots)"""
    try:
        # Check if property already exists by ID or ListingKey
        listing_key = str(request.property_data.get("listing_key", "") or "")
        # Only match on listing_key when there IS one - a manual (No-MLS) property has "", and
        # matching "" against "" would grab some OTHER agent's manual property row (or blow up
        # with multiple matches) instead of creating this one.
        match = Property.id == request.property_id
        if listing_key:
            match = or_(match, Property.listing_key == listing_key)
        stmt = select(Property).where(match)
        result = await db.execute(stmt)
        existing_property = result.scalar_one_or_none()

        # Prepare data from property_data (handles both snake_case and camelCase from frontend)
        data = request.property_data

        fields = {
            "street_address": request.address,
            "listing_id": data.get("listing_id") or data.get("ListingId"),
            "img_src": request.cover_image_url or data.get("imageUrl") or data.get("imgSrc"),
            "price": data.get("price") or data.get("ListPrice"),
            "bedrooms": data.get("beds") or data.get("BedroomsTotal"),
            "bathrooms": data.get("baths") or data.get("BathroomsTotal"),
            "living_area": data.get("sqft") or data.get("LivingArea"),
            "lot_size": data.get("lotSize") or data.get("LotSizeSquareFeet"),
            "year_built": data.get("yearBuilt") or data.get("YearBuilt"),
            "home_type": data.get("propertyType") or data.get("PropertyType"),
            "home_status": data.get("homeStatus") or data.get("MlsStatus") or "ACTIVE",
            "latitude": data.get("latitude") or data.get("Latitude"),
            "longitude": data.get("longitude") or data.get("Longitude"),
            "listing_key": listing_key,
            "description": data.get("description") or data.get("PublicRemarks"),
            "city": data.get("city") or data.get("City"),
            "state": data.get("state") or data.get("StateOrProvince"),
            "zipcode": data.get("zipcode") or data.get("PostalCode"),
            "updated_at": datetime.now(timezone.utc)
        }

        if existing_property:
            for key, value in fields.items():
                if value is not None:
                    setattr(existing_property, key, value)
        else:
            new_property = Property(id=request.property_id, created_at=datetime.now(timezone.utc), **fields)
            db.add(new_property)

        await db.commit()
        return {"success": True, "message": "Property stored successfully"}

    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to store property: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store property: {str(e)}")

async def _capture_listing_page_lead(db: AsyncSession, agent: User, property_obj, request, *, tour: bool,
                                     email: Optional[str], phone: Optional[str]):
    """The listing page's "Message agent" / "Schedule a tour" forms (where a Similar Homes sheet's
    per-home QR lands) make a real lead + follow-up, like every other form - with which home and
    where they came from (e.g. that open house's sheet). Never fatal: the email + notification
    already went out."""
    try:
        from app.services.leads_service import upsert_lead
        from app.services.visit_source import clean_came_from
        listing_key = (property_obj.listing_key if property_obj is not None else None) or request.property_id
        from app.services.tasks_service import display_address
        street = (property_obj.street_address if property_obj is not None else None) or (request.property_address or "").split(",")[0]
        street = display_address(street)[0] or street  # "8008 GRAY HAVEN RD" -> "8008 Gray Haven Rd"
        snapshot = {"listing_key": listing_key, "message": (request.message or "").strip() or None}
        if tour:
            for n, (d, t) in enumerate([(request.preferred_date, request.preferred_time),
                                        (request.preferred_date_2, request.preferred_time_2),
                                        (request.preferred_date_3, request.preferred_time_3)], start=1):
                if d or t:
                    snapshot[f"preferred_date_{n}"], snapshot[f"preferred_time_{n}"] = d, t
        if request.came_from is not None:
            snapshot["came_from"] = clean_came_from(request.came_from)
        return await upsert_lead(
            db, owner_id=agent.id, name=(request.visitor_name or "").strip() or None, email=email, phone=phone,
            source_type="property_page_tour_request" if tour else "property_page_inquiry",
            source_id=listing_key,
            summary=f"Requested a tour of {street}" if tour else f"Sent you a message about {street}",
            occurred_at=datetime.now(timezone.utc), property_address=request.property_address,
            raw_snapshot={k: v for k, v in snapshot.items() if v is not None},
        )
    except Exception:
        logger.warning("listing-page lead capture failed", exc_info=True)
        return None


async def _point_alert_at_lead(db: AsyncSession, notification, lead) -> None:
    """The bell / Activity row opens the person's lead (where the message and follow-up are), not
    the public listing page they were on."""
    if lead is not None and getattr(lead, "id", None):
        notification.link = f"/leads?lead={lead.id}"
        await db.commit()


# TODO
@router.post("/message-agent")
async def message_agent_route(
    request: AgentMessageRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db)
):
    """The listing page's "Message the agent" form."""
    # Public, unauthenticated, and it emails the agent - same per-IP spam limit as every other
    # public form (this one had none, so anyone could flood any agent's inbox).
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(http_request)
    return await message_agent(request, db)


async def message_agent(request: AgentMessageRequest, db: AsyncSession):
    """Send a message to an agent from a visitor - the shared path behind the listing page form,
    the IDX site's "Ask a question" and the Guest Pass question (each caller does its own spam
    check before calling this)."""
    try:
        # 1. Find the agent and property
        agent_stmt = select(User).where(User.id == request.agent_id)
        agent_result = await db.execute(agent_stmt)
        agent = agent_result.scalar_one_or_none()

        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        # The public pages pass the MLS listing key (the URL's id); older callers pass Property.id.
        property_stmt = select(Property).where(or_(Property.id == request.property_id, Property.listing_key == request.property_id)).limit(1)
        property_result = await db.execute(property_stmt)
        property_obj = property_result.scalars().first()

        # Create full address string and get image
        if property_obj:
            full_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()
            property_image = property_obj.img_src
        else:
            full_address = request.property_address
            property_image = None

        # Handle visitor contact (split into email/phone)
        visitor_email = None
        visitor_phone = None
        contact = (request.visitor_contact or "").strip()
        if "@" in contact:
            visitor_email = contact
        else:
            visitor_phone = contact
        if (request.visitor_phone or "").strip() and not visitor_phone:
            visitor_phone = request.visitor_phone.strip()

        # 2. Schedule the email inquiry
        scheduled_email = ScheduledEmail(
            recipient_email=agent.email,
            subject=f"New Inquiry from {request.visitor_name} for {request.property_address}",
            template_name="similar_property_visitor_message",
            template_variables={
                "agent_name": f"{agent.first_name} {agent.last_name}",
                "first_name": agent.first_name or "",   # "Hi Ava," 
                "visitor_name": request.visitor_name,
                "visitor_email": visitor_email or "",
                "visitor_phone": visitor_phone or "",
                "property_address": full_address,
                "property_image": property_image,
                "message": request.message,
                "today_date": datetime.now(timezone.utc).strftime("%m/%d/%Y")
            },
            scheduled_for=datetime.now(timezone.utc)
        )

        db.add(scheduled_email)

        # 3. Create a dashboard notification for the agent
        notification = Notification(
            agent_id=agent.id,
            type="PROPERTY_INQUIRY",
            reference_type="PROPERTY",
            reference_id=request.property_id,
            title=f"New Inquiry: {request.visitor_name}",
            message=f"Visitor sent a message about {request.property_address}",
            property_id=request.property_id,
            property_address=request.property_address,
            visitor_name=request.visitor_name,
            # This route's own public-facing listing page - the one the visitor was actually on
            # when they sent the message. Was missing entirely before (property_obj.listing_key,
            # not request.property_id - that route param is a listing_key despite its folder name).
            link=f"/property/{property_obj.listing_key}/{agent.id}" if property_obj else None,
            is_read=False,
            created_at=datetime.now(timezone.utc)
        )
        db.add(notification)

        await db.commit()
        lead = await _capture_listing_page_lead(db, agent, property_obj, request, tour=False, email=visitor_email, phone=visitor_phone)
        await _point_alert_at_lead(db, notification, lead)

        return {"success": True, "message": "Message sent to agent successfully"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Failed to send message to agent", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to send message to agent")

# TODO
def _format_tour_datetime(date_str: str, time_str: str) -> str:
    """Format YYYY-MM-DD and HH:MM to M/D/YYYY h:mmam/pm"""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        # Format: 2/2/2026 1:15pm
        return dt.strftime("%-m/%-d/%Y %-I:%M%p").lower()
    except Exception:
        return f"{date_str} {time_str}"

@router.get("/agent/{agent_id}/busy")
async def agent_busy_times(agent_id: str, db: AsyncSession = Depends(get_db)):
    """The agent's busy blocks for the next 8 days (start/end only) from their connected Google /
    Outlook calendar, so the showing window can cross those times out. [] when none connected."""
    from app.services import calendar_sync_service
    return {"busy": await calendar_sync_service.public_busy(db, agent_id)}


@router.post("/schedule-tour")
async def schedule_tour_route(
    request: ScheduleTourRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db)
):
    """The listing page's tour request form."""
    # Public, unauthenticated, and it emails the agent - same per-IP spam limit as every other
    # public form (this one had none, so anyone could flood any agent's inbox).
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(http_request)
    return await schedule_tour(request, db)


async def schedule_tour(request: ScheduleTourRequest, db: AsyncSession):
    """Schedule a tour for a property and notify the agent (shared by any caller that already did
    its own spam check)."""
    try:
        # 1. Find the agent and property
        agent_stmt = select(User).where(User.id == request.agent_id)
        agent_result = await db.execute(agent_stmt)
        agent = agent_result.scalar_one_or_none()

        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        # The public pages pass the MLS listing key (the URL's id); older callers pass Property.id.
        property_stmt = select(Property).where(or_(Property.id == request.property_id, Property.listing_key == request.property_id)).limit(1)
        property_result = await db.execute(property_stmt)
        property_obj = property_result.scalars().first()

        # Create full address string and get image
        if property_obj:
            full_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()
            property_image = property_obj.img_src
        else:
            full_address = request.property_address
            property_image = None

        # 2. Format preferred dates for the email (M/D/YYYY h:mmam/pm)
        dates = []
        if request.preferred_date and request.preferred_time:
            dates.append(f"1. {_format_tour_datetime(request.preferred_date, request.preferred_time)}")
        if request.preferred_date_2 and request.preferred_time_2:
            dates.append(f"2. {_format_tour_datetime(request.preferred_date_2, request.preferred_time_2)}")
        if request.preferred_date_3 and request.preferred_time_3:
            dates.append(f"3. {_format_tour_datetime(request.preferred_date_3, request.preferred_time_3)}")

        preferred_dates_str = "\n".join(dates) if dates else "No specific dates provided"

        # 3. Handle visitor contact (split into email/phone)
        visitor_email = None
        visitor_phone = None
        contact = (request.visitor_contact or "").strip()
        if "@" in contact:
            visitor_email = contact
        else:
            visitor_phone = contact
        if (request.visitor_phone or "").strip() and not visitor_phone:
            visitor_phone = request.visitor_phone.strip()

        # 4. Schedule the email notification
        scheduled_email = ScheduledEmail(
            recipient_email=agent.email,
            subject=f"Tour Request from {request.visitor_name or 'Visitor'} for {request.property_address}",
            template_name="similar_property_visitor_tour_request",
            template_variables={
                "agent_name": f"{agent.first_name} {agent.last_name}",
                "first_name": agent.first_name or "",   # "Hi Ava," 
                "message": request.message or "I'm interested in touring this property!",
                "preferred_dates": preferred_dates_str,
                "property_address": full_address,
                "property_image": property_image,
                "today_date": datetime.now(timezone.utc).strftime("%m/%d/%Y"),
                "visitor_email": visitor_email or "",
                "visitor_name": request.visitor_name or "Interested Visitor",
                "visitor_phone": visitor_phone or ""
            },
            scheduled_for=datetime.now(timezone.utc)
        )
        db.add(scheduled_email)

        # 4. Create a dashboard notification for the agent
        notification = Notification(
            agent_id=agent.id,
            type="TOUR_REQUEST",
            reference_type="TOUR",
            reference_id=request.property_id,
            title=f"Tour Request: {request.visitor_name or 'Visitor'}",
            message=f"Visitor wants to tour {request.property_address}",
            property_id=request.property_id,
            property_address=request.property_address,
            visitor_name=request.visitor_name,
            link=f"/property/{property_obj.listing_key}/{agent.id}" if property_obj else None,
            is_read=False,
            created_at=datetime.now(timezone.utc)
        )
        db.add(notification)

        await db.commit()
        lead = await _capture_listing_page_lead(db, agent, property_obj, request, tour=True, email=visitor_email, phone=visitor_phone)
        await _point_alert_at_lead(db, notification, lead)

        return {"success": True, "message": "Tour scheduled and agent notified"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Failed to schedule tour", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to schedule tour")

@router.get("/address", dependencies=[Depends(require_broker_authorization), Depends(require_mls_access)])
async def search_properties_by_address(
    query: str = Query(..., min_length=2),
    active_only: bool = Query(False),
    exclude_rentals: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_broker_authorization),
):
    """Internal address search using Trigram similarity for typo-tolerant autocomplete.
    active_only/exclude_rentals are opt-in (default off) - this endpoint is shared by several
    pickers (CMA comps, Studio, forms) that legitimately need closed/sold or rental listings; only
    the open house "Generate Kit" address search passes both true, since an open house can only be
    hosted for an active for-sale listing."""
    try:
        # 1. Use the % operator (similarity) to filter using the Trigram index
        # 2. Use func.similarity to rank the results so the best match is first
        clean_status = func.upper(func.replace(func.coalesce(Property.home_status, ""), "-BRIGHT", ""))
        status_priority = case(
            (clean_status.in_(["ACTIVE", "COMING SOON"]), 0),
            (clean_status.ilike("PENDING%"), 1),
            (clean_status.ilike("%UNDER CONTRACT%"), 1),
            (clean_status.ilike("%CONTRACT%"), 1),
            (clean_status.ilike("CLOSED%"), 2),
            (clean_status.ilike("SOLD%"), 2),
            else_=3,
        )
        starts_with_query = case(
            (Property.full_address.ilike(f"{query}%"), 0),
            else_=1,
        )
        stmt = (
            select(
                Property.full_address, Property.latitude, Property.longitude, Property.img_src,
                Property.photos, Property.home_status, Property.raw_mls_data, Property.price,
                Property.google_rooftop_latitude, Property.google_rooftop_longitude,
                Property.google_geocode_location_type,
            )
            .where(
                or_(
                    Property.full_address.ilike(f"%{query}%"),
                    Property.full_address.op("%")(query) # The similarity operator
                )
            )
        )
        if active_only:
            # For-sale homes: Active and Coming Soon (an agent preps the kit for a Coming Soon
            # listing's first open house - user, 10/16). Never Pending/Under Contract, Closed or
            # Off Market.
            stmt = stmt.where(clean_status.in_(["ACTIVE", "COMING SOON"]))
        if exclude_rentals:
            stmt = stmt.where(Property.mls_property_type.notilike("%Lease%"))
        # The address they typed comes first - "252 Highland Ave" must find 252 HIGHLAND AVE, WAYNE
        # (off-market - the usual seller CMA subject) ahead of a fuzzy for-sale 221 Highland Ave in
        # another state. For-sale-first only breaks ties after that.
        stmt = stmt.order_by(starts_with_query, status_priority, func.similarity(Property.full_address, query).desc()).limit(15)
        result = await db.execute(stmt)
        records = []
        seen_addresses = set()
        for row in result.fetchall():
            address_key = (row[0] or "").strip().upper()
            if address_key in seen_addresses:
                continue
            seen_addresses.add(address_key)
            records.append(row)
            if len(records) >= 5:
                break
        logger.info(
            "Address autocomplete results",
            extra={
                "query": query,
                "count": len(records),
                "first_has_image": bool(records[0][3] or ((records[0][4] or [None])[0] if isinstance(records[0][4], list) and records[0][4] else None) or ((records[0][6] or {}).get("ListPictureURL") if isinstance(records[0][6], dict) else None)) if records else False,
                "first_status": records[0][5] if records else None,
            },
        )

        payload = {
            "success": True,
            "results": [
                {
                    "address": r[0],
                    "lat": r[8] or r[1],
                    "lng": r[9] or r[2],
                    "brightLat": r[1],
                    "brightLng": r[2],
                    "googleRooftopLatitude": r[8],
                    "googleRooftopLongitude": r[9],
                    "googleGeocodeLocationType": r[10],
                    "image": image_url,
                    "imgSrc": image_url,
                    "ListPictureURL": image_url,
                    "status": status_value,
                    "homeStatus": status_value,
                    "MlsStatus": status_value,
                    "price": price_value,
                    "ListPrice": price_value,
                } for r in records
                for image_url, status_value, price_value in [(
                    r[3] or ((r[4] or [None])[0] if isinstance(r[4], list) and r[4] else None) or ((r[6] or {}).get("ListPictureURL") if isinstance(r[6], dict) else None),
                    r[5] or ((r[6] or {}).get("MlsStatus") if isinstance(r[6], dict) else None),
                    r[7] or ((r[6] or {}).get("ListPrice") if isinstance(r[6], dict) else None),
                )]
            ]
        }
        # CMA accuracy test only: a live arm for this agent shows its sold test home as it read on
        # the as-of date (Active, its asking price then). No arm -> unchanged.
        from app.services import cma_backtest_arms as _arms
        for _arm in await _arms.live_arms(db, current_user.id):
            _street = _arm.address_key.split("|")[0] if _arm.address_key else None
            payload["results"] = [_arms.mask_suggestion(x, _arm) if _street and _arms._street_of(x.get("address")) == _street else x
                                  for x in payload["results"]]
        return payload
    except Exception as e:
        logger.error(f"Address search failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@router.get("/school-districts", dependencies=[Depends(require_broker_authorization)])
async def search_school_districts(
    query: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db)
):
    """Autocomplete school districts from the reference table"""
    try:
        stmt = (
            select(SchoolDistrict.name, SchoolDistrict.state)
            .where(SchoolDistrict.name.ilike(f"%{query}%"))
            .order_by(SchoolDistrict.name.asc())
            .limit(10)
        )
        result = await db.execute(stmt)
        districts = result.fetchall()

        return {
            "success": True,
            "results": [f"{d[0]}, {d[1]}" for d in districts]
        }
    except Exception as e:
        logger.error(f"School district autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/cities", dependencies=[Depends(require_broker_authorization)])
async def search_cities(
    query: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db)
):
    """Autocomplete cities from the reference table"""
    try:
        stmt = (
            select(City.name, City.state)
            .where(City.name.ilike(f"%{query}%"))
            .order_by(City.name.asc())
            .limit(10)
        )

        result = await db.execute(stmt)
        cities = result.fetchall()

        return {
            "success": True,
            "results": [f"{c[0]}, {c[1]}" for c in cities]
        }
    except Exception as e:
        logger.error(f"City autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@router.get("/townships", dependencies=[Depends(require_broker_authorization)])
async def search_townships(
    query: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db)
):
    """Autocomplete cities from the reference table"""
    try:
        stmt = (
            select(Township.name, Township.state)
            .where(Township.name.ilike(f"%{query}%"))
            .order_by(Township.name.asc())
            .limit(10)
        )

        result = await db.execute(stmt)
        townships = result.fetchall()

        return {
            "success": True,
            "results": [f"{t[0]}, {t[1]}" for t in townships]
        }
    except Exception as e:
        logger.error(f"City autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/counties", dependencies=[Depends(require_broker_authorization)])
async def search_counties(
    query: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db)
):
    """Autocomplete counties - see search_counties_public below (same reference-free lookup
    straight off the indexed Property.county column, just behind the broker-auth gate the other
    authenticated autocomplete endpoints use)."""
    try:
        stmt = (
            select(Property.county)
            .where(Property.county.isnot(None), Property.county.ilike(f"%{query}%"))
            .distinct()
            .order_by(Property.county.asc())
            .limit(10)
        )
        result = await db.execute(stmt)
        counties = result.scalars().all()

        return {
            "success": True,
            "results": list(counties)
        }
    except Exception as e:
        logger.error(f"County autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/area-shapes", dependencies=[Depends(require_broker_authorization)])
async def get_area_shapes(
    cities: List[str] = Query(default=[]),
    school_districts: List[str] = Query(default=[]),
    counties: List[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """The real lines of the towns / school districts / counties a Showcase searches, for the
    Showcase editor map (areas/boundary_service - Census lines, cached per place for 180 days).
    A name that can't be matched is simply left out; the search itself never depends on this."""
    import httpx
    from app.services.areas import boundary_service

    shapes: list[dict] = []
    async with httpx.AsyncClient(headers={"User-Agent": "OpenHousePal/1.0"}) as client:
        for kind, values, resolve in (("town", cities, boundary_service.town_shape),
                                      ("school_district", school_districts, boundary_service.district_shape),
                                      ("county", counties, boundary_service.county_shape)):
            for value in values[:12]:
                v = value
                if kind == "county" and "," not in v:
                    # Counties are stored bare ("Montgomery") - take the state its homes are in.
                    st = (await db.execute(
                        select(Property.state, func.count()).where(func.lower(Property.county) == v.lower())
                        .group_by(Property.state).order_by(func.count().desc()).limit(1)
                    )).first()
                    v = f"{value}, {st[0]}" if st else value
                try:
                    shape = await resolve(db, client, v)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    logger.info(f"Area shape lookup failed for {kind} {value}: {exc}")
                    shape = None
                if shape:
                    shapes.append({**shape, "kind": kind, "query": value})
    return {"shapes": shapes}


@router.get("/area-shapes/public", dependencies=[])
async def get_area_shapes_public(
    cities: List[str] = Query(default=[]),
    school_districts: List[str] = Query(default=[]),
    counties: List[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """The same lines for a CLIENT editing their own search (EditShowcaseCriteriaModal) - public
    Census data, cached per place; capped lower than the agent's."""
    return await get_area_shapes(cities=cities[:6], school_districts=school_districts[:6], counties=counties[:4], db=db)


@router.get("/home-areas/public", dependencies=[])
async def get_home_areas_public(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    db: AsyncSession = Depends(get_db),
):
    """One home's surroundings for a property map: its township / borough / city, the ones around
    it, and its school district (areas/boundary_service.home_areas - free Census lines, saved once
    and shared). Empty lists when the Census can't be reached - the map still shows the home."""
    import httpx
    from app.services.areas import boundary_service
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "OpenHousePal/1.0"}) as client:
            return await boundary_service.home_areas(db, client, round(lat, 5), round(lng, 5))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info(f"Home areas lookup failed for {lat},{lng}: {exc}")
        return {"municipality": None, "neighbors": [], "school_districts": []}


@router.get("/address/public", dependencies=[])
async def search_properties_by_address_public(
    query: str = Query(..., min_length=2, max_length=200),
    db: AsyncSession = Depends(get_db)
):
    """Unauthenticated listing search-by-address for public-facing forms (the showcase page's
    "compare to a different property" picker in EditShowcaseCriteriaModal) - same Trigram
    similarity lookup as /address, minus the broker-auth gate, and includes listing_key/city/
    beds/baths/sqft in one shot since the visitor selects straight from these results (no
    follow-up authenticated /lookup call available to them)."""
    try:
        clean_status = func.upper(func.replace(func.coalesce(Property.home_status, ""), "-BRIGHT", ""))
        status_priority = case(
            (clean_status.in_(["ACTIVE", "COMING SOON"]), 0),
            (clean_status.ilike("PENDING%"), 1),
            (clean_status.ilike("%UNDER CONTRACT%"), 1),
            (clean_status.ilike("%CONTRACT%"), 1),
            (clean_status.ilike("CLOSED%"), 2),
            (clean_status.ilike("SOLD%"), 2),
            else_=3,
        )
        starts_with_query = case(
            (Property.full_address.ilike(f"{query}%"), 0),
            else_=1,
        )
        stmt = (
            select(
                Property.listing_key, Property.full_address, Property.city, Property.latitude, Property.longitude,
                Property.img_src, Property.home_status, Property.price, Property.bedrooms, Property.bathrooms,
                Property.living_area,
            )
            .where(
                or_(
                    Property.full_address.ilike(f"%{query}%"),
                    Property.full_address.op("%")(query)
                ),
                # Visitor-facing picker - never surface an agent's manual "My Listings" rows.
                Property.is_manual.is_(False),
            )
            .order_by(status_priority, starts_with_query, func.similarity(Property.full_address, query).desc())
            .limit(15)
        )
        result = await db.execute(stmt)
        records = []
        seen_addresses = set()
        for row in result.fetchall():
            address_key = (row[1] or "").strip().upper()
            if address_key in seen_addresses:
                continue
            seen_addresses.add(address_key)
            records.append(row)
            if len(records) >= 5:
                break

        return {
            "success": True,
            "results": [
                {
                    "listingKey": r[0],
                    "address": r[1],
                    "city": r[2],
                    "lat": r[3],
                    "lng": r[4],
                    "image": r[5],
                    "status": r[6],
                    "price": r[7],
                    "beds": r[8],
                    "baths": r[9],
                    "squareFeet": r[10],
                } for r in records
            ]
        }
    except Exception as e:
        logger.error(f"Public address search failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/cities/public", dependencies=[])
async def search_cities_public(
    query: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db)
):
    """Unauthenticated city autocomplete for public-facing forms (e.g. the Setup My Own Showcase
    form) - same reference-table lookup as /cities, just without the broker-auth gate since
    visitors filling out a public form aren't logged in."""
    try:
        stmt = (
            select(City.name, City.state)
            .where(City.name.ilike(f"%{query}%"))
            .order_by(City.name.asc())
            .limit(10)
        )
        result = await db.execute(stmt)
        cities = result.fetchall()

        return {
            "success": True,
            "results": [f"{c[0]}, {c[1]}" for c in cities]
        }
    except Exception as e:
        logger.error(f"Public city autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/addresses/public", dependencies=[])
async def search_listing_addresses_public(
    query: str = Query(..., min_length=2, max_length=120),
    form_id: Optional[str] = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    """Address suggestions for a public form's "which home?" question (Schedule a Showing): any
    on-market MLS listing - buyers ask to see other agents' listings all the time - with the form
    owner's own listings first. Only a suggestion list: the visitor can still type anything."""
    from app.models.database import AgentForm
    from app.services.agent_hub_live import INACTIVE_STATUSES
    from app.services.tasks_service import display_address
    q = " ".join(query.split())
    try:
        from app.models.database import AgentMarketArea, MarketArea
        mls_id, towns, states = None, set(), set()
        if form_id:
            form = await db.get(AgentForm, form_id)
            owner = await db.get(User, form.owner_id) if form else None
            mls_id = ((owner.mls_id or "").strip() or None) if owner else None
            if owner:
                for name, state in (await db.execute(
                    select(MarketArea.name, MarketArea.state).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
                    .where(AgentMarketArea.owner_id == owner.id)
                )).all():
                    towns.add((name or "").strip().lower())
                    if state:
                        states.add(state.strip().upper())
        like = f"%{q}%"
        match = [
            or_(Property.street_address.ilike(like), Property.full_address.ilike(like), Property.unparsed_address.ilike(like)),
            Property.home_status.notin_(INACTIVE_STATUSES),
        ]
        # The agent's own market first (Bright spans several states), then everywhere else.
        rows = []
        if states:
            rows = list((await db.execute(select(Property).where(*match, func.upper(Property.state).in_(states)).limit(60))).scalars().all())
        if len(rows) < 8:
            seen_keys = {r.listing_key for r in rows}
            rows += [r for r in (await db.execute(select(Property).where(*match).limit(60))).scalars().all() if r.listing_key not in seen_keys]
        ql = q.lower()

        def rank(p: Property):
            mine = bool(mls_id) and mls_id in (p.list_agent_mls_id, p.co_list_agent_mls_id)
            local = (p.city or "").strip().lower() in towns
            in_state = (p.state or "").strip().upper() in states
            starts = (p.street_address or "").lower().startswith(ql)
            return (0 if mine else 1 if local else 2 if in_state else 3, 0 if starts else 1, (p.street_address or ""))

        out, seen = [], set()
        for p in sorted(rows, key=rank):
            full, _ = display_address(p.street_address or p.unparsed_address, p.city, p.state)
            if not full:
                continue
            full = f"{full} {p.zipcode}".strip() if p.zipcode else full
            if full.lower() in seen:
                continue
            seen.add(full.lower())
            photos = p.photos if isinstance(p.photos, list) else []
            out.append({
                "address": full,
                "price": p.price,
                "status": p.home_status,
                "photo": p.img_src or (photos[0] if photos else None),
                "mine": bool(mls_id) and mls_id in (p.list_agent_mls_id, p.co_list_agent_mls_id),
                "listing_key": p.listing_key,
                # For the showing flow's home card (street on top, town + facts below).
                "street": display_address(p.street_address or p.unparsed_address)[0].split(",")[0],
                "city": (p.city or "").title() or None, "state": p.state, "zipcode": p.zipcode,
                "beds": p.bedrooms, "baths": p.bathrooms, "sqft": p.living_area,
                # where it is - the IDX Showcase builder draws its "around this home" circle from it
                "lat": float(p.latitude) if p.latitude is not None else None, "lng": float(p.longitude) if p.longitude is not None else None,
            })
            if len(out) >= 8:
                break
        return {"success": True, "results": out}
    except Exception as e:
        logger.error(f"Public address autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/school-districts/public", dependencies=[])
async def search_school_districts_public(
    query: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db)
):
    """Unauthenticated school-district autocomplete for public-facing forms - see
    search_cities_public."""
    try:
        stmt = (
            select(SchoolDistrict.name, SchoolDistrict.state)
            .where(SchoolDistrict.name.ilike(f"%{query}%"))
            .order_by(SchoolDistrict.name.asc())
            .limit(10)
        )
        result = await db.execute(stmt)
        districts = result.fetchall()

        return {
            "success": True,
            "results": [f"{d[0]}, {d[1]}" for d in districts]
        }
    except Exception as e:
        logger.error(f"Public school district autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/counties/public", dependencies=[])
async def search_counties_public(
    query: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db)
):
    """Unauthenticated county autocomplete for public-facing forms - see search_cities_public.
    There's no dedicated county reference table, so this pulls distinct values straight off the
    (indexed) Property.county column instead."""
    try:
        stmt = (
            select(Property.county)
            .where(Property.county.isnot(None), Property.county.ilike(f"%{query}%"))
            .distinct()
            .order_by(Property.county.asc())
            .limit(10)
        )
        result = await db.execute(stmt)
        counties = result.scalars().all()

        return {
            "success": True,
            "results": list(counties)
        }
    except Exception as e:
        logger.error(f"Public county autocomplete failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@router.get("/brokerages", dependencies=[])
async def search_brokerages(
        query: str = Query(..., min_length=1),
        db: AsyncSession = Depends(get_db)
):
    '''Autocomplete for brokerage input with priority for major brands'''
    try:
        # 1. Standardize the incoming query for symbol-agnostic matching
        # e.g., "RE/MAX" -> "REMAX", "C 21" -> "C21"
        clean_query = query.replace("/", "").replace("-", "").replace(" ", "").replace(".", "")

        # 2. Create a priority order: major brands first
        priority = case(
            {brand: 0 for brand in MAJOR_BRANDS},
            value=Brokerage.parent_name,
            else_=1
        )

        # 3. Create a "clean" version of the column in SQL to match against
        # We strip '/', '-', ' ', and '.'
        db_clean_name = func.replace(
            func.replace(
                func.replace(
                    func.replace(Brokerage.parent_name, '/', ''),
                    '-', ''
                ),
                ' ', ''
            ),
            '.', ''
        )

        # NOTE: For SELECT DISTINCT, all ORDER BY expressions must appear in the SELECT list
        stmt = (
            select(Brokerage.parent_name, priority.label("priority"))
            .where(
                or_(
                    Brokerage.parent_name.ilike(f"{query}%"),
                    db_clean_name.ilike(f"{clean_query}%")
                )
            )
            .distinct()
            .order_by(priority, Brokerage.parent_name.asc())
            .limit(10)
        )

        results = await db.execute(stmt)
        # Fetch the results and extract only the parent_name (the first column)
        brokerages = [row[0] for row in results.fetchall()]

        return {
            "success": True,
            "results": brokerages
        }

    except Exception as e:
        logger.error(f"Brokerages autocomplete failed {e}")
        raise HTTPException(status_code=500, detail="Search Failed")

@router.get("/{property_id}")
async def get_property(
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_broker_authorization),
):
    """Get property data by ID from the local mirror"""
    try:
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()

        # No-MLS plans may read back the manual properties they entered themselves (no
        # listing_key), but never a Bright MLS record from the mirror.
        from app.utils.auth import tier_has_mls
        if not property_record or (property_record.listing_key and not tier_has_mls(current_user.plan_tier)):
            raise HTTPException(status_code=404, detail="Property not found")

        return {
            "success": True,
            "property": PropertyDetailResponse.model_validate(property_record)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get property {property_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get property")

@router.post("/lookup", response_model=PropertyDetailResponse, dependencies=[Depends(require_broker_authorization), Depends(require_mls_access)])
async def get_property_details(
    request: PropertyLookupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_broker_authorization),
):
    """Fetch property details from the local mirror using PropertyService"""
    if not request.listing_key and not request.address:
        raise HTTPException(status_code=400, detail="Either listing_key or address must be provided")

    # CMA accuracy test only (see cma_backtest_arms): the armed sold home, as it read on the as-of date.
    from app.services import cma_backtest_arms as _arms
    _arm = await _arms.find_arm(db, current_user.id, listing_key=request.listing_key, address=request.address)
    if _arm:
        _detail = await property_service.get_property_by_listing_key(db, _arm.listing_key)
        if _detail:
            return _arms.mask_lookup(_detail, _arm)

    try:
        if request.listing_key:
            property_data = await property_service.get_property_by_listing_key(db, request.listing_key)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found in database by listing key")
            return property_data
        else:
            property_data = await property_service.get_property_by_address(db, request.address)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found in database by address")
            return property_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Mirror lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database lookup failed. Please verify the address format.")

@router.get("/{listing_key}/nearby-amenities", response_model=NearbyAmenitiesResponse, dependencies=[Depends(require_broker_authorization), Depends(require_mls_access)])
async def get_nearby_amenities(
    listing_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_optional),
):
    """Schools/grocery/medical care/parks/airports near a listing, for the Open House Cheat Sheet PDF.
    Backed by Google Places and cached on the Property row - see nearby_amenities_service for the
    graceful-degrade behavior when GOOGLE_PLACES_API_KEY isn't configured."""
    result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    property_record = result.scalar_one_or_none()
    if not property_record:
        raise HTTPException(status_code=404, detail="Listing not found in database.")

    return await nearby_amenities_service.get_nearby_amenities(
        db, property_record, owner_id=(str(current_user.id) if current_user else None),
    )


@router.get("/{listing_key}/school-assignment-debug", dependencies=[Depends(require_admin_user)])
async def get_school_assignment_debug(
    listing_key: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    property_record = result.scalar_one_or_none()
    if not property_record:
        raise HTTPException(status_code=404, detail="Listing not found in database.")

    assignment = await resolve_school_assignment(db, property_record)
    return assignment.to_debug_dict()


@router.post("/similar", dependencies=[Depends(require_broker_authorization), Depends(require_mls_access)])
async def get_similar_properties(
    request: SimilarPropertiesRequest,
    db: AsyncSession = Depends(get_db)
):
    """Find matching properties from the local mirror using PropertyService"""
    try:
        # Discovery Mode (Search by preferences)
        # Fetch the original property to get its exact home_type and price if not provided
        original_property = None
        if request.listing_key:
            original_property = await db.execute(select(Property).where(Property.listing_key == str(request.listing_key)))
            original_property = original_property.scalar_one_or_none()

        # Helper to map string type to booleans
        def get_type_flags(h_type: Optional[str]):
            flags = {
                "is_single_family": False,
                "is_town_house": False,
                "is_condo": False,
                "is_multi_family": False,
                "is_lot_land": False,
                "is_apartment": False
            }
            if h_type == "SINGLE_FAMILY": flags["is_single_family"] = True
            elif h_type == "TOWNHOUSE": flags["is_town_house"] = True
            elif h_type == "CONDO": flags["is_condo"] = True
            elif h_type == "MULTI_FAMILY": flags["is_multi_family"] = True
            elif h_type in ["LAND", "FARM"]: flags["is_lot_land"] = True
            elif h_type == "RESIDENTIAL_LEASE": flags["is_apartment"] = True
            return flags

        # 1. Base preferences from request, then fallback to original property
        min_price = request.min_price if request.min_price and request.min_price > 0 else None
        max_price = request.max_price if request.max_price and request.max_price > 0 else None
        min_beds = request.min_beds if request.min_beds and request.min_beds > 0 else None
        min_baths = request.min_baths if request.min_baths and request.min_baths > 0 else None
        min_acres = request.min_acres if request.min_acres and request.min_acres > 0 else None

        # Determine coordinates (request or original property)
        lat = request.lat if request.lat else (original_property.latitude if original_property else None)
        long = request.lng if request.lng else (original_property.longitude if original_property else None)

        if not lat or not long:
            logger.warning(f"No coordinates found for similar property search. ListingKey: {request.listing_key}")
            raise HTTPException(status_code=400, detail="Location coordinates are required to find similar properties.")

        type_flags = get_type_flags(original_property.home_type if original_property else None)
        prefs = CollectionPreferencesBase(
            lat=float(lat),
            long=float(long),
            diameter=request.radius or 5.0,
            min_price=min_price,
            max_price=max_price,
            min_beds=min_beds,
            min_baths=min_baths,
            **type_flags
        )

        results = await property_service.get_properties_by_preferences(db, prefs, max_properties=100)

        if request.listing_key:
            results = [p for p in results if str(p.listing_key) != str(request.listing_key)]
        if min_acres:
            min_lot_size = float(min_acres) * 43560
            results = [
                p for p in results
                if (
                    getattr(p, 'LotSizeSquareFeet', None)
                    or getattr(p, 'lot_size', None)
                    or ((getattr(p, 'LotSizeAcres', None) or getattr(p, 'lot_size_acres', None) or 0) * 43560)
                    or 0
                ) >= min_lot_size
            ]

        serialized_results = []
        for p in results:
            item = (
                p.model_dump(by_alias=True, exclude_none=True)
                if hasattr(p, "model_dump")
                else p.dict(by_alias=True, exclude_none=True)
                if hasattr(p, "dict")
                else dict(p)
                if isinstance(p, dict)
                else p
            )

            if isinstance(item, dict):
                acres = (
                    item.get("LotSizeAcres")
                    or item.get("lotSizeAcres")
                    or item.get("lot_size_acres")
                    or item.get("acres")
                )
                square_feet = (
                    item.get("LotSizeSquareFeet")
                    or item.get("lotSizeSquareFeet")
                    or item.get("lot_size")
                    or item.get("lotSize")
                )
                if not acres and square_feet:
                    acres = round(float(square_feet) / 43560, 2)

                if acres:
                    item["acres"] = acres
                    item["LotSizeAcres"] = acres
                    item["lotSizeAcres"] = acres

            serialized_results.append(item)

        return {"success": True, "properties": serialized_results}
    except Exception as e:
        logger.error(f"Mirror search failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed. Please try different filters.")

@router.get("/{property_id}/cache")
async def cache_property_details(
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user_optional)
):
    """
    Returns property details directly from the mirror.
    Legacy 'cache' naming preserved for frontend compatibility.
    """
    try:
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        property_record = result.scalar_one_or_none()

        if not property_record:
            raise HTTPException(status_code=404, detail="Property not found in database")

        # In the new architecture, everything is already 'cached' in the mirror
        response_details = PropertyDetailResponse.model_validate(property_record)

        return {
            "success": True,
            "message": "Property details retrieved from database",
            "property_id": property_id,
            "from_cache": True,
            "property": response_details.model_dump(by_alias=True, exclude_none=True)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Property mirror retrieval failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve property details.")

class PropertyAgentResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    property: PropertyDetailResponse
    agent_name: str
    # The agent's public card on the Listing Page (the same details their Agent Hub shows).
    agent_photo_url: Optional[str] = None
    agent_brokerage: Optional[str] = None
    # The agent's own published Listing Spotlight for this home (respects their "always use the
    # standard page" override) - the Listing Page hands the visitor to it instead.
    spotlight_url: Optional[str] = None
    # Coming Soon: the first day it may be shown (Bright "ExpectedOnMarketDate").
    expected_on_market_date: Optional[str] = None
    # Every listing this home has had + logged price changes, newest first (price_history_service).
    price_history: List[dict] = []

async def _price_history(db: AsyncSession, listing_key: str) -> list:
    try:
        from app.services.price_history_service import get_history
        return await get_history(db, listing_key)
    except Exception:  # noqa: BLE001 - the page still works without it
        logger.warning("price history lookup failed", exc_info=True, extra={"listing_key": listing_key})
        return []


@router.get("/price-history/{listing_key}", dependencies=[])
async def get_listing_price_history(listing_key: str, db: AsyncSession = Depends(get_db)):
    """Public: a home's price history (Showcases, Guest Pass). Same display rules as the pages -
    a withdrawn/expired/off-market or internet-opted-out listing has none to show."""
    from app.services.website.content_adapters import _IDX_NOT_SHOWN_STATUSES
    row = (await db.execute(
        select(Property.home_status, Property.raw_mls_data["InternetEntireListingDisplayYN"].astext).where(Property.listing_key == str(listing_key))
    )).first()
    if not row or row[0] in _IDX_NOT_SHOWN_STATUSES or row[1] == "false":
        raise HTTPException(status_code=404, detail="No price history for this home.")
    return {"price_history": await _price_history(db, listing_key)}


@router.get("/agent/{agent_id}/listing/{listing_key}", response_model=PropertyAgentResponse)
async def get_property_for_agent(
    agent_id: str,
    listing_key: str,
    db: AsyncSession = Depends(get_db)
):
    """Get property data from mirror by agent and listing key"""
    try:
        from app.services.user_service import UserService
        from app.models.database import Collection

        # 1. Try to find as a User ID first
        agent = await UserService.get_user_by_id(db, agent_id)

        # 2. If not found, try to find as a Collection ID and get its owner
        if not agent:
            stmt = select(Collection).where(Collection.id == agent_id)
            result = await db.execute(stmt)
            collection = result.scalar_one_or_none()
            if collection:
                agent = await UserService.get_user_by_id(db, collection.owner_id)

        if not agent:
            raise HTTPException(status_code=404, detail="Agent or Collection not found")

        agent_name = f"{agent.first_name} {agent.last_name}"

        try:
            property_data = await property_service.get_property_by_listing_key(db, listing_key)
            if not property_data:
                raise HTTPException(status_code=404, detail="Property not found in database")
            # Bright IDX rules, same as the IDX website: a withdrawn / expired / canceled /
            # off-market listing, or one whose seller kept it off the internet, isn't shown on a
            # public page. Sold and under-contract homes still are.
            from app.services.website.content_adapters import _IDX_NOT_SHOWN_STATUSES
            opted_out, expected_on = (await db.execute(
                select(Property.raw_mls_data["InternetEntireListingDisplayYN"].astext, Property.raw_mls_data["ExpectedOnMarketDate"].astext)
                .where(Property.listing_key == str(listing_key))
            )).first() or (None, None)
            if getattr(property_data, "home_status", None) in _IDX_NOT_SHOWN_STATUSES or opted_out == "false":
                raise HTTPException(status_code=404, detail="This home is no longer on the market.")

            spotlight_url = None
            try:
                from app.services.website import content_adapters
                spotlight_url = (await content_adapters.attach_spotlight_urls(db, agent, [{"listing_key": listing_key}]))[0].get("spotlight_url")
            except Exception:  # noqa: BLE001 - the page still works without it
                logger.warning("listing page spotlight lookup failed", exc_info=True)
            return PropertyAgentResponse(
                property=property_data, agent_name=agent_name,
                agent_photo_url=agent.photo_url or None,
                agent_brokerage=agent.brokerage or None,
                spotlight_url=spotlight_url,
                expected_on_market_date=expected_on,
                price_history=await _price_history(db, listing_key),
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Mirror lookup for agent failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Database lookup failed.")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get property for agent", exc_info=True, extra={"agent_id": agent_id, "listing_key": listing_key})
        raise HTTPException(status_code=500, detail="Failed to get property details.")

