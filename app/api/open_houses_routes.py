from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from typing import List, Optional

from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.database import LeadEvent, LeadNote, OpenHouseEvent, OpenHouseHostRequest, OpenHouseListingNote, User, OpenHouseVisitor, OpenHouseSignInView, OpenHouseFormRequest, Notification, ScheduledEmail, AgentHub, Property
from app.services import email_signature_service
from app.services.open_house_manager_service import INACTIVE_STATUSES
from app.utils.auth import get_current_active_user, require_active_plan, require_broker_authorization
from app.schemas.open_house import OpenHouseCreateRequest, OpenHouseResponse, OpenHouseFormSubmission, OpenHouseFormResponse, VisitorResponse, NoteUpdate, QrDarkModeUpdate, ShareGuestsUpdate, GuestSettingsUpdate, OpenHouseSnapshotUpdate, OpenHouseCheatSheetSnapshotUpdate, OpenHouseCheatSheetPhotosUpdate, OpenHouseFormRequestCreate, OpenHouseFormRequestResponse, OpenHouseFormRequestType
from app.schemas.leads import LeadNoteCreate, LeadNoteUpdate
from app.services.open_house_service import OpenHouseService
from app.services.email_service import EmailService
from app.services.leads_service import upsert_lead
from app.api.lead_routes import _load_owned_note, _reload_note, _to_lead_note_out
from app.services.website.site_service import resolve_owner_public_base_url, build_qr_code_image_url
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone

import os
from dotenv import load_dotenv
from app.config.logging import get_logger

logger = get_logger(__name__)

load_dotenv()

router = APIRouter()

# is this route authed for people that have trial, premium, or basic
@router.post("/api/open-houses", response_model=OpenHouseResponse, dependencies=[Depends(require_broker_authorization)])
async def create_open_house(
    request: OpenHouseCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Create a new open house record with property metadata"""
    try:
        open_house_id = request.open_house_event_id or str(uuid.uuid4())
        form_url = f"/open-house/{open_house_id}"
        base_url = await resolve_owner_public_base_url(db, current_user)
        qr_code_url = build_qr_code_image_url(base_url, form_url)

        property_data = request.property_data

        # Support both new RESO PascalCase and old camelCase/nested structures
        address_data = property_data.get('address') if isinstance(property_data.get('address'), dict) else {}
        is_nested_address = isinstance(address_data, dict)

        street_address = (
            property_data.get('FullStreetAddress') or
            address_data.get('streetAddress') or
            (address_data if not is_nested_address else None) or
            request.address
        )

        city = (
            property_data.get('City') or
            address_data.get('city') or
            property_data.get('city')
        )

        state = (
            property_data.get('StateOrProvince') or
            address_data.get('state') or
            property_data.get('state')
        )

        zipcode = (
            property_data.get('PostalCode') or
            address_data.get('zipcode') or
            property_data.get('zipCode') or
            property_data.get('zipcode')
        )

        abbreviated_addr = property_data.get('abbreviatedAddress')
        if not abbreviated_addr:
            abbreviated_addr = street_address

        # Helper to safely cast to float/int
        def safe_num(val):
            try:
                if val is None: return None
                return float(val)
            except (ValueError, TypeError):
                return None

        listing_key_value = str(property_data.get('ListingKey') or property_data.get('listing_key') or '')

        # Kits are evergreen: one kit per agent per home, reused for every open they hold there (no
        # reprinting). Approved Open House Manager dates live on the requests themselves - the
        # listing agent's insights for each hosted window come from the sign-ins on that date
        # (open_house_manager_service._hosted_windows), so nothing is stamped onto the kit.
        scheduled_date = scheduled_start_time = scheduled_end_time = None

        defaults = await OpenHouseService.guest_defaults(db, current_user, listing_key_value or None)
        guest_choices = {
            "auto_showcase": defaults["auto_showcase"] if request.auto_showcase is None else request.auto_showcase,
            "similar_homes": defaults["similar_homes"] if request.similar_homes is None else request.similar_homes,
            **({"guest_pass": request.guest_pass} if request.guest_pass is not None else {}),
        }

        open_house = OpenHouseEvent(
            id=open_house_id,
            agent_id=current_user.id,
            qr_code=qr_code_url,
            form_url=form_url,
            cover_image_url=request.cover_image_url,
            cover_image_position=request.cover_image_position,
            # Sign-in screen theme from the wizard's Review step; omitted = model default (dark).
            **({"qr_dark_mode": request.qr_dark_mode} if request.qr_dark_mode is not None else {}),

            # Property metadata
            address=street_address,
            abbreviated_address=abbreviated_addr,
            house_type=property_data.get('home_type') or property_data.get('HomeType') or property_data.get('PropertyType') or property_data.get('homeType'),
            latitude=safe_num(property_data.get('Latitude') or property_data.get('latitude')),
            longitude=safe_num(property_data.get('Longitude') or property_data.get('longitude')),
            lot_size=safe_num(property_data.get('LotSizeSquareFeet') or property_data.get('lot_size') or property_data.get('lotSize')),
            city=city,
            state=state,
            zipcode=zipcode,
            bedrooms=safe_num(property_data.get('BedroomsTotal') or property_data.get('bedrooms')),
            bathrooms=safe_num(property_data.get('BathroomsTotal') or property_data.get('bathrooms')),
            living_area=safe_num(property_data.get('LivingArea') or property_data.get('livingArea') or property_data.get('living_area')),
            price=safe_num(property_data.get('ListPrice') or property_data.get('price')),
            home_status=property_data.get('MlsStatus') or property_data.get('homeStatus'),
            listing_key=listing_key_value,
            similar_properties_snapshot=request.similar_properties_snapshot,
            cheat_sheet_photos=request.cheat_sheet_photos,
            cheat_sheet_snapshot=request.cheat_sheet_snapshot,
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
            **guest_choices,
        )

        db.add(open_house)
        await db.commit()
        await db.refresh(open_house)

        # Mirror to the agent's connected calendar (no-op unless they connected Google + this
        # open house has a real scheduled date). Best-effort, own session, never blocks.
        if open_house.scheduled_date:
            from app.services import calendar_sync_service
            calendar_sync_service.sync_open_house_bg(open_house.agent_id, open_house.id)

        return OpenHouseResponse(
            id=open_house.id,
            open_house_event_id=open_house.id,
            agent_id=open_house.agent_id,
            address=open_house.address,
            cover_image_url=open_house.cover_image_url,
            cover_image_position=open_house.cover_image_position,
            qr_code_url=open_house.qr_code,
            form_url=open_house.form_url,
            bedrooms=open_house.bedrooms,
            bathrooms=open_house.bathrooms,
            living_area=open_house.living_area,
            price=open_house.price,
            lot_size=open_house.lot_size,
            city=open_house.city,
            state=open_house.state,
            zipcode=open_house.zipcode,
            latitude=open_house.latitude,
            longitude=open_house.longitude,
            listing_key=open_house.listing_key,
            similar_properties_snapshot=open_house.similar_properties_snapshot,
            cheat_sheet_photos=open_house.cheat_sheet_photos,
            cheat_sheet_snapshot=open_house.cheat_sheet_snapshot,
            qr_dark_mode=open_house.qr_dark_mode,
            share_guests_with_listing_agent=open_house.share_guests_with_listing_agent,
            auto_showcase=open_house.auto_showcase,
            similar_homes=open_house.similar_homes,
            guest_pass=open_house.guest_pass,
            visitor_count=0,
            created_at=open_house.created_at
        )

    except Exception as e:
        logger.error("creating open house failed", extra={"error": str(e)})

        if "Error binding parameter" in str(e):
            raise HTTPException(status_code=500, detail="Invalid property data structure for database storage")

        raise HTTPException(status_code=500, detail="Failed to create open house")

@router.get("/api/open-houses", response_model=List[OpenHouseResponse], dependencies=[Depends(require_broker_authorization)])
async def get_open_houses(
    listing_key: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Get all active (non-deleted) open houses for the current agent, optionally filtered to one listing (e.g. for Design Studio's open-house auto-populate)."""
    try:
        visitor_counts_subq = (
            select(
                OpenHouseVisitor.open_house_event_id.label("open_house_event_id"),
                func.count(OpenHouseVisitor.id).label("visitor_count"),
            )
            .group_by(OpenHouseVisitor.open_house_event_id)
            .subquery()
        )

        conditions = [
            OpenHouseEvent.agent_id == current_user.id,
            OpenHouseEvent.is_deleted == False  # Only show active listings
        ]
        if listing_key:
            conditions.append(OpenHouseEvent.listing_key == listing_key)

        stmt = select(OpenHouseEvent, func.coalesce(visitor_counts_subq.c.visitor_count, 0)).outerjoin(
            visitor_counts_subq,
            visitor_counts_subq.c.open_house_event_id == OpenHouseEvent.id,
        ).where(
            and_(*conditions)
        ).order_by(OpenHouseEvent.created_at.desc())

        result = await db.execute(stmt)
        open_houses = result.all()

        # The listing agent's standing host notes, but only for opens whose scheduled date is still
        # in the future - once the open is over the access info isn't useful anymore, so it's
        # dropped and the "Notes & Info" affordance reverts to a plain "Notes".
        today = date.today()
        upcoming_scheduled_keys = {
            oh.listing_key
            for oh, _ in open_houses
            if oh.listing_key and oh.scheduled_date and oh.scheduled_date >= today
        }
        host_notes_by_key: dict[str, str] = {}
        if upcoming_scheduled_keys:
            notes_result = await db.execute(
                select(OpenHouseListingNote.listing_key, OpenHouseListingNote.notes).where(
                    OpenHouseListingNote.listing_key.in_(upcoming_scheduled_keys)
                )
            )
            host_notes_by_key = {k: n for k, n in notes_result.all() if n and n.strip()}

        # Helper to safely cast to int
        def safe_int(val):
            try:
                if val is None: return None
                return int(float(val))
            except (ValueError, TypeError):
                return None

        spotlights = await OpenHouseService.listing_spotlights(db, current_user, [oh for oh, _ in open_houses])
        hub = await OpenHouseService.agent_hub_link(db, current_user)
        showcases_available = _showcases_available(current_user)
        guest_pass_available = _guest_pass_available(current_user)

        response_list = []
        for oh, visitor_count in open_houses:
            response_list.append(OpenHouseResponse(
                id=oh.id,
                open_house_event_id=oh.id,
                agent_id=oh.agent_id,
                address=oh.address or "Unknown Address",
                cover_image_url=oh.cover_image_url or "",
                cover_image_position=oh.cover_image_position,
                qr_code_url=oh.qr_code or "",
                form_url=oh.form_url or f"/open-house/{oh.id}",
                bedrooms=oh.bedrooms,
                bathrooms=oh.bathrooms,
                living_area=oh.living_area,
                price=oh.price,
                lot_size=oh.lot_size,
                city=oh.city,
                state=oh.state,
                zipcode=oh.zipcode,
                latitude=oh.latitude,
                longitude=oh.longitude,
                listing_key=oh.listing_key,
                notes=oh.notes,
                similar_properties_snapshot=oh.similar_properties_snapshot,
                cheat_sheet_photos=oh.cheat_sheet_photos,
                cheat_sheet_snapshot=oh.cheat_sheet_snapshot,
                qr_dark_mode=oh.qr_dark_mode,
                share_guests_with_listing_agent=oh.share_guests_with_listing_agent,
                auto_showcase=oh.auto_showcase,
                similar_homes=oh.similar_homes,
                after_signin=oh.after_signin,
                guest_pass=oh.guest_pass,
                pass_signature=oh.pass_signature,
                guest_pass_available=guest_pass_available,
                pass_signature_available=email_signature_service.signature_plan_allowed(current_user),
                **_spotlight_fields(spotlights.get(oh.id), oh),
                hub_available=hub["available"],
                hub_ready=hub["ready"],
                showcases_available=showcases_available,
                visitor_count=visitor_count or 0,
                created_at=oh.created_at,
                scheduled_date=oh.scheduled_date,
                scheduled_start_time=oh.scheduled_start_time,
                scheduled_end_time=oh.scheduled_end_time,
                listing_host_notes=host_notes_by_key.get(oh.listing_key) if oh.scheduled_date and oh.scheduled_date >= today else None,
            ))

        return response_list

    except Exception as e:
        logger.error("fetching open houses failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch open houses")


@router.get("/api/open-houses/guest-defaults")
async def get_guest_defaults(
    listing_key: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """What the kit wizard starts Showcases + Similar Homes at for this home: both off when the
    agent is its listing (or co-listing) agent, both on otherwise. Same rule the create route uses
    when the wizard doesn't say."""
    return await OpenHouseService.guest_defaults(db, current_user, listing_key)


@router.get("/api/open-houses/missed-showcases")
async def get_missed_showcases(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """The Basic agent's "you'd be getting these right now" backlog: guests without an agent who
    signed in at their open houses (last 90 days) and would have gotten a personalized Showcase +
    invite on Premium. `upgrade_eligible` is only true on the Bright ladder below Premium - the
    No-MLS track has no Showcases to upgrade into, so it's never pitched there.

    Safety net: if the agent is ALREADY on a Showcase plan and still has a backlog (e.g. a plan
    change path that didn't trigger the backfill), this kicks the backfill off."""
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    from app.services.open_house_service import schedule_showcase_backfill

    items = await OpenHouseService.missed_showcases(db, current_user.id)
    has_showcases = owner_has_access(current_user, SHOWCASE_ACCESS_TIERS)
    if has_showcases and items:
        schedule_showcase_backfill(current_user.id)
    return {
        "count": len(items),
        "recent": items[:5],
        "window_days": OpenHouseService.MISSED_SHOWCASE_WINDOW_DAYS,
        "upgrade_eligible": current_user.plan_tier == "BASIC",
        "backfilling": bool(has_showcases and items),
    }


@router.get("/api/open-houses/form-requests", response_model=List[OpenHouseFormRequestResponse])
async def get_open_house_form_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """
    All "Request Seller Disclosure" / "Schedule a Second Visit" requests across the current
    agent's open houses, newest first - feeds both the Forms tab's Open House section and the
    Open Houses tab's own results panel.

    Registered BEFORE the /{open_house_id} route below on purpose: FastAPI/Starlette matches GET
    routes in registration order, and "form-requests" would otherwise match that route's
    {open_house_id} path parameter first, 404ing here with "Open house not found" instead of ever
    reaching this handler. That's exactly what was happening - this endpoint was unreachable, so
    every "Respond to this request" surface (Open Houses page, lead detail) silently rendered
    nothing instead of the real disclosure/second-visit response UI.
    """
    try:
        # Still-open requests are a bounded backlog (the agent clears them), but completed ones
        # pile up forever - capped the same way /tasks caps its "done" side, so this payload (and
        # the Past tab it feeds) can't grow unbounded for an agent years into using this.
        base = (
            select(OpenHouseFormRequest, OpenHouseEvent.address, OpenHouseEvent.city, OpenHouseEvent.cover_image_url)
            .join(OpenHouseEvent, OpenHouseFormRequest.open_house_event_id == OpenHouseEvent.id)
            .where(OpenHouseEvent.agent_id == current_user.id)
        )
        open_rows = (await db.execute(
            base.where(OpenHouseFormRequest.completed_at.is_(None))
            .order_by(OpenHouseFormRequest.created_at.desc())
        )).all()
        completed_rows = (await db.execute(
            base.where(OpenHouseFormRequest.completed_at.isnot(None))
            .order_by(OpenHouseFormRequest.completed_at.desc())
            .limit(50)
        )).all()
        rows = open_rows + completed_rows

        return [
            OpenHouseFormRequestResponse(
                id=req.id,
                open_house_event_id=req.open_house_event_id,
                form_type=req.form_type,
                full_name=req.full_name,
                email=req.email,
                phone=req.phone,
                wants_another_tour=req.wants_another_tour,
                another_tour_time=req.another_tour_time,
                preferred_visit_time=req.preferred_visit_time,
                wants_disclosure=req.wants_disclosure,
                created_at=req.created_at,
                completed_at=req.completed_at,
                # Street + city (no state/zip - keeps the card compact while still being the "full" address).
                property_address=f"{address}, {city}" if address and city else address,
                property_image=cover_image_url,
            )
            for req, address, city, cover_image_url in rows
        ]

    except Exception as e:
        logger.error("fetching open house form requests failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch open house form requests")


@router.patch("/api/open-houses/form-requests/{request_id}/complete", response_model=OpenHouseFormRequestResponse)
async def complete_open_house_form_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """
    Mark a "Request Seller Disclosure" / "Schedule a Second Visit" request as handled - "Sent" for
    a disclosure, "Scheduled" for a second visit. Moves it from the Upcoming to the Past tab in the
    Forms Results view.
    """
    try:
        result = await db.execute(
            select(OpenHouseFormRequest, OpenHouseEvent.address, OpenHouseEvent.city, OpenHouseEvent.cover_image_url)
            .join(OpenHouseEvent, OpenHouseFormRequest.open_house_event_id == OpenHouseEvent.id)
            .where(OpenHouseFormRequest.id == request_id, OpenHouseEvent.agent_id == current_user.id)
        )
        row = result.first()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        req, address, city, cover_image_url = row

        req.completed_at = datetime.now(timezone.utc)
        db.add(req)

        # Clear the matching lead's "needs a response" flag too - this request is also tracked as
        # a LeadEvent (source_id = this request's id, set when the request was created), separately
        # from this table, and had no way to resolve before this: has_second_visit_request/
        # has_disclosure_request stayed true forever once triggered, with nothing here ever
        # clearing it even after the agent marked it Sent/Scheduled.
        if req.lead_id:
            event_result = await db.execute(
                select(LeadEvent).where(LeadEvent.source_id == req.id, LeadEvent.lead_id == req.lead_id)
            )
            lead_event = event_result.scalar_one_or_none()
            if lead_event and lead_event.agent_viewed_at is None:
                lead_event.agent_viewed_at = datetime.now(timezone.utc)
                db.add(lead_event)

        await db.commit()
        await db.refresh(req)
        # ...and its follow-up ("Send the disclosure to Jane" / "Tour 12 Oak St") is done too - checked
        # off and off the calendar, as if the agent ticked it (user, 10/15).
        from app.services.tasks_service import complete_request_followups
        await complete_request_followups(db, current_user.id, source_ids=(req.id,),
                                         rules={"disclosure_request", "second_visit_request"})
        await db.refresh(req)

        return OpenHouseFormRequestResponse(
            id=req.id,
            open_house_event_id=req.open_house_event_id,
            form_type=req.form_type,
            full_name=req.full_name,
            email=req.email,
            phone=req.phone,
            wants_another_tour=req.wants_another_tour,
            another_tour_time=req.another_tour_time,
            preferred_visit_time=req.preferred_visit_time,
            wants_disclosure=req.wants_disclosure,
            created_at=req.created_at,
            completed_at=req.completed_at,
            property_address=f"{address}, {city}" if address and city else address,
            property_image=cover_image_url,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("completing open house form request failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update request")


@router.get("/api/open-houses/{open_house_id}", response_model=OpenHouseResponse)
async def get_open_house(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Single open house's property details - backs the Visitor Insights page header photo/address."""
    visitor_count_result = await db.execute(
        select(func.count(OpenHouseVisitor.id)).where(OpenHouseVisitor.open_house_event_id == open_house_id)
    )
    visitor_count = visitor_count_result.scalar() or 0

    result = await db.execute(
        select(OpenHouseEvent).where(
            and_(OpenHouseEvent.id == open_house_id, OpenHouseEvent.agent_id == current_user.id)
        )
    )
    oh = result.scalar_one_or_none()
    if not oh:
        raise HTTPException(status_code=404, detail="Open house not found")

    return OpenHouseResponse(
        id=oh.id,
        open_house_event_id=oh.id,
        agent_id=oh.agent_id,
        address=oh.address or "Unknown Address",
        cover_image_url=oh.cover_image_url or "",
        cover_image_position=oh.cover_image_position,
        qr_code_url=oh.qr_code or "",
        form_url=oh.form_url or f"/open-house/{oh.id}",
        bedrooms=oh.bedrooms,
        bathrooms=oh.bathrooms,
        living_area=oh.living_area,
        price=oh.price,
        lot_size=oh.lot_size,
        city=oh.city,
        state=oh.state,
        zipcode=oh.zipcode,
        latitude=oh.latitude,
        longitude=oh.longitude,
        listing_key=oh.listing_key,
        notes=oh.notes,
        similar_properties_snapshot=oh.similar_properties_snapshot,
        cheat_sheet_photos=oh.cheat_sheet_photos,
        cheat_sheet_snapshot=oh.cheat_sheet_snapshot,
        qr_dark_mode=oh.qr_dark_mode,
        share_guests_with_listing_agent=oh.share_guests_with_listing_agent,
        auto_showcase=oh.auto_showcase,
        similar_homes=oh.similar_homes,
        after_signin=oh.after_signin,
        guest_pass=oh.guest_pass,
        pass_signature=oh.pass_signature,
        guest_pass_available=_guest_pass_available(current_user),
        pass_signature_available=email_signature_service.signature_plan_allowed(current_user),
        **_spotlight_fields((await OpenHouseService.listing_spotlights(db, current_user, [oh])).get(oh.id), oh),
        hub_available=(hub := await OpenHouseService.agent_hub_link(db, current_user))["available"],
        hub_ready=hub["ready"],
        showcases_available=_showcases_available(current_user),
        visitor_count=visitor_count,
        created_at=oh.created_at,
    )


@router.delete("/api/open-houses/{open_house_id}", dependencies=[Depends(require_broker_authorization)])
async def delete_open_house(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
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
        open_house.deleted_at = datetime.now(timezone.utc)

        await db.commit()

        # Pull it off the agent's connected calendar too (no-op if not synced).
        from app.services import calendar_sync_service
        calendar_sync_service.unsync_open_house_bg(current_user.id, open_house_id)

        return {
            "success": True,
            "message": "Open house removed from your listings. QR links, similar property pages, collections, and visitor data are preserved.",
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
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Submit open house visitor form
    """
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    if form_data.open_house_event_id:
        await _assert_open_house_owner_live(db, form_data.open_house_event_id)

    # Idempotency / de-dup. The kiosk retries a sign-in that failed to reach the server (offline);
    # if the ORIGINAL request actually landed but its response was lost, the retry would otherwise
    # create a second visitor + a second auto-showcase + a second notification. Treat a repeat of
    # the same (open house, email) inside a short window as the same sign-in and return the
    # existing one. A genuine re-visit hours later, or the same person at a different open house,
    # still goes through.
    if form_data.open_house_event_id and form_data.email:
        _dupe_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        _existing = await db.execute(
            select(OpenHouseVisitor)
            .where(
                OpenHouseVisitor.open_house_event_id == form_data.open_house_event_id,
                func.lower(OpenHouseVisitor.email) == str(form_data.email).lower(),
                OpenHouseVisitor.created_at >= _dupe_cutoff,
            )
            .order_by(OpenHouseVisitor.created_at.desc())
        )
        _dupe = _existing.scalars().first()
        # Similar Homes QR: a guest who said "I have an agent" when they signed in, and now says
        # they don't, goes through again - their newer answer wins (Showcase + follow-up).
        if _dupe and (form_data.intent or "").lower() == "similar" and _dupe.has_agent == "YES" and form_data.has_agent.value != "YES":
            _dupe = None
        if _dupe:
            logger.info("Duplicate open-house sign-in ignored", extra={"visitor_id": _dupe.id, "email": form_data.email})
            return OpenHouseFormResponse(
                success=True,
                message="Thank you for visiting! We'll be in touch soon.",
                visitor_id=_dupe.id,
                collection_created=bool(getattr(_dupe, "lead_id", None)),
                guest_pass_url=await _guest_pass_url(db, _dupe),
            )

    try:
        # Create visitor record and handle collection creation
        visitor = await OpenHouseService.create_visitor(db, form_data)

        open_house_info = await OpenHouseService.get_open_house_event_by_id(db, form_data.open_house_event_id) if form_data.open_house_event_id else None
        from_similar_qr = (form_data.intent or "").lower() == "similar"
        # Someone with an agent scanning the Similar Homes QR: the page tells them kindly we can't
        # help with their search, and the host isn't bothered - no lead, alert or wrap-up email.
        quiet = from_similar_qr and form_data.has_agent.value == "YES"
        if open_house_info and open_house_info.get("agent_id") and not quiet:
            lead = await upsert_lead(
                db,
                owner_id=open_house_info["agent_id"],
                name=visitor.full_name,
                email=visitor.email,
                phone=visitor.phone,
                source_type="open_house_visitor",
                source_id=visitor.id,
                summary=(f"Scanned \"Want to see more homes like these?\" on the Similar Homes sheet and signed in - {open_house_info.get('address') or 'Open House'}" if from_similar_qr
                         else f"Signed in at open house - {open_house_info.get('address') or 'Open House'}"),
                occurred_at=visitor.created_at,
                represented='yes' if form_data.has_agent.value == 'YES' else 'no',
                override_represented=from_similar_qr,
                property_address=open_house_info.get('address'),
                open_house_event_id=form_data.open_house_event_id,
            )
            if lead:
                visitor.lead_id = lead.id
                db.add(visitor)
                await db.commit()
                # Drop a next-day "follow up with this guest" task on the agent's list +
                # calendar. Idempotent per (agent, open house) and never fatal. Not for a guest
                # who said they already have an agent - the agent can't represent them.
                if form_data.has_agent.value != "YES":
                    try:
                        from app.services.tasks_service import create_open_house_followup
                        await create_open_house_followup(
                            db,
                            owner_id=open_house_info["agent_id"],
                            lead=lead,
                            signin_id=visitor.id,
                            property_address=open_house_info.get("address"),
                            occurred_at=visitor.created_at,
                            commit=True,
                            rule="similar_homes_qr" if from_similar_qr else "open_house_signin",
                        )
                    except Exception:
                        logger.exception("open house follow-up task creation failed (non-fatal)")

        # Anyone who doesn't already have an agent automatically gets a showcase (and the
        # follow-up email below) - no separate "interested in updates" opt-in anymore, across
        # every sign-in path (kiosk, QR code, sign-in sheet) since they all submit through here.
        collection_result = {"success": False, "properties_added": 0}
        showcases_on = bool(open_house_info) and (
            from_similar_qr or (await db.get(OpenHouseEvent, form_data.open_house_event_id)).auto_showcase is not False
        )
        if form_data.open_house_event_id and form_data.has_agent.value != "YES" and showcases_on:
            # Builds the showcase AND schedules the guest's invite email (1 hour out) - the same
            # helper backfill_missed_showcases() uses when a Basic agent upgrades.
            collection_result = await OpenHouseService.create_showcase_and_invite(
                db, visitor, form_data, send_after=timedelta(hours=1)
            )
            # The Showcase's homes are in now - re-push this guest's follow-up so its calendar alert
            # carries the real pitch (home count, best match), not the empty just-created Showcase.
            if collection_result.get("success") and visitor.lead_id and open_house_info:
                from app.services.tasks_service import resync_lead_followups
                resync_lead_followups(open_house_info["agent_id"], visitor.lead_id, delay=20)

        # Customize message based on collection creation result
        message = "Thank you for visiting! We'll be in touch soon."
        if collection_result["success"] and collection_result["properties_added"] > 0:
            message = f"Thank you for visiting! We've found {collection_result['properties_added']} similar properties for you. We'll be in touch soon with your personalized collection."
        elif collection_result["success"]:
            message = "Thank you for visiting! We've created a personalized collection for you and will be in touch soon with matching properties."

        # Create notification for agent
        if form_data.open_house_event_id and not quiet:
            try:
                # Get the open house event to find the agent_id
                open_house_query = select(OpenHouseEvent).where(OpenHouseEvent.id == form_data.open_house_event_id)
                open_house_result = await db.execute(open_house_query)
                open_house_event = open_house_result.scalar_one_or_none()

                if open_house_event:
                    property_data_for_notif = await OpenHouseService.get_property_by_qr_code(db, form_data.open_house_event_id)

                    # Build link for notification
                    notification_link = None
                    if collection_result.get('success') and collection_result.get('collection_id'):
                        notification_link = f"/showcases?showcase={collection_result.get('collection_id')}"
                    else:
                        # Fallback to visitor list if no collection
                        notification_link = f"/open-houses/visitors/{form_data.open_house_event_id}"

                    # A Basic agent's unrepresented guest is exactly who a Showcase is for - say so
                    # right when it matters (they get it automatically on upgrade, see
                    # backfill_missed_showcases). Only on the Bright ladder: No-MLS has no Showcases.
                    notif_message = "Signed in at your open house"
                    if collection_result.get("reason") == "basic_plan":
                        hosting_agent = await db.get(User, open_house_event.agent_id)
                        if hosting_agent and hosting_agent.plan_tier == "BASIC":
                            notif_message = ("Signed in without an agent - on Premium they'd already have their own "
                                             "Showcase. Upgrade and they'll get it automatically.")
                            notification_link = "/upgrade-required?feature=Showcases&tier=PREMIUM"

                    notification = Notification(
                        agent_id=open_house_event.agent_id,
                        type="OPEN_HOUSE_SIGN_IN",
                        reference_type="VISITOR",
                        reference_id=visitor.id,
                        title=f"New Open House Visitor: {visitor.full_name}",
                        message=notif_message,
                        collection_id=collection_result.get('collection_id') if collection_result.get('success') else None,
                        collection_name=collection_result.get('collection_id') if collection_result.get('success') else None,
                        property_address=property_data_for_notif.get('address') if property_data_for_notif else None,
                        visitor_name=visitor.full_name,
                        link=notification_link,
                        is_read=False,
                        created_at=datetime.now(timezone.utc)
                    )

                    db.add(notification)
                    await db.commit()
                    # No email per sign-in - the host gets ONE wrap-up 2 hours after the last guest
                    # signs in (agent_roundup_service); the bell above still alerts live.
                    from app.services.agent_roundup_service import queue_sign_in
                    queue_sign_in(db, await db.get(User, open_house_event.agent_id), open_house_event, visitor,
                                  notif_message if notif_message != "Signed in at your open house" else None,
                                  notification_link)
                    await db.commit()
            except Exception as e:
                logger.error("creating notification failed", extra={"error": str(e)})
                # Don't fail the whole request if notification creation fails

        return OpenHouseFormResponse(
            success=True,
            message=message,
            visitor_id=visitor.id,
            collection_created=collection_result["success"],
            guest_pass_url=await _guest_pass_url(db, visitor),
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


async def _guest_pass_url(db: AsyncSession, visitor: OpenHouseVisitor) -> Optional[str]:
    """The guest's Guest Pass link (same host as the sign-in page), when this open house gives one."""
    try:
        if not visitor or not visitor.open_house_event_id:
            return None
        oh = await db.get(OpenHouseEvent, visitor.open_house_event_id)
        host = await db.get(User, oh.agent_id) if oh else None
        if not OpenHouseService.guest_pass_on(oh, host):
            return None
        from app.services.guest_pass_service import pass_path
        return pass_path(visitor)
    except Exception:
        logger.exception("building guest pass link failed (non-fatal)")
        return None


OPEN_HOUSE_INACTIVE_DETAIL = "This open house sign-in isn't active right now."


async def _assert_open_house_owner_live(db: AsyncSession, open_house_event_id: str) -> None:
    """Sign-in QR codes / kiosks only work while the hosting agent's subscription is live (every
    plan includes sign-in, so this is a subscription check, not a tier check). Agents are warned
    about this before cancelling (Settings > Billing)."""
    from app.utils.auth import owner_has_access
    owner = (await db.execute(
        select(User).join(OpenHouseEvent, OpenHouseEvent.agent_id == User.id).where(OpenHouseEvent.id == open_house_event_id)
    )).scalar_one_or_none()
    if owner is not None and not owner_has_access(owner):
        raise HTTPException(status_code=404, detail=OPEN_HOUSE_INACTIVE_DETAIL)


@router.get("/open-house/property/{open_house_event_id}")
async def get_property_by_qr(
    open_house_event_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get property information by open house event ID
    """
    try:
        await _assert_open_house_owner_live(db, open_house_event_id)
        property_data = await OpenHouseService.get_property_by_qr_code(db, open_house_event_id)

        if not property_data:
            raise HTTPException(
                status_code=404,
                detail="This sign-in link isn't working. It may have been typed wrong - please scan the QR code at the open house again."
            )

        # The open house event itself only stores a single cover image - pull the full photo
        # gallery from the local MLS mirror (by listing key) for the Sign-In Sheet's rotating
        # display. Falls back to just the cover image for manually-added/off-market open houses
        # with no matching listing_key.
        photos: list = []
        if property_data.get("listingKey"):
            photo_result = await db.execute(
                select(Property.photos).where(Property.listing_key == str(property_data["listingKey"]))
            )
            photos = photo_result.scalar_one_or_none() or []
        if not photos and property_data.get("imageSrc"):
            photos = [property_data["imageSrc"]]
        property_data["photos"] = photos

        theme = {"accentColor": "#C9A24D", "darkMode": False}
        qr_dark_mode = True
        oh_result = await db.execute(select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_event_id))
        open_house_event = oh_result.scalar_one_or_none()
        if open_house_event:
            qr_dark_mode = bool(open_house_event.qr_dark_mode)
            property_data["similarHomes"] = open_house_event.similar_homes is not False
            # Where the guest's phone goes once they sign in (the host's Spotlight for this home,
            # their Agent Hub, or nowhere) - the sign-in page sends them there.
            target = await OpenHouseService.after_signin_target(db, open_house_event, await db.get(User, open_house_event.agent_id))
            if target:
                property_data["afterSignin"] = target
            hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == open_house_event.agent_id))
            hub = hub_result.scalar_one_or_none()
            configured_theme = (hub.config or {}).get("openHouseRequestTheme") if hub else None
            if configured_theme:
                theme = {**theme, **configured_theme}

            # For the Sign-In Sheet's agent branding block, screen-only stand-in for the "hosted
            # by" line a printed flyer would carry.
            agent_result = await db.execute(select(User).where(User.id == open_house_event.agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent:
                property_data["agentName"] = f"{agent.first_name or ''} {agent.last_name or ''}".strip()
                property_data["agentBrokerage"] = agent.brokerage or ""
                property_data["agentPhotoUrl"] = agent.photo_url or None

        return {
            "success": True,
            "property": property_data,
            "theme": theme,
            "qrDarkMode": qr_dark_mode,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching property by QR code failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch property information"
        )


@router.post("/open-house/{open_house_event_id}/track-view")
async def track_signin_view(
    open_house_event_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Fired once each time the public sign-in form is actually shown to someone (a guest's own
    phone loading the page, or a fresh instance in Kiosk mode) - separate from OpenHouseVisitor,
    which only gets a row on a completed submission. Backs the "Completion %" stat (started vs.
    finished). Best-effort/fire-and-forget from the frontend, so failures here should never
    surface to a guest - a missed view just slightly undercounts a stat, nothing more.
    """
    try:
        oh_result = await db.execute(select(OpenHouseEvent.id).where(OpenHouseEvent.id == open_house_event_id))
        if not oh_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Open house not found")

        db.add(OpenHouseSignInView(open_house_event_id=open_house_event_id))
        await db.commit()
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("tracking sign-in view failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to track sign-in view")


@router.post("/open-house/{open_house_event_id}/request", response_model=OpenHouseFormRequestResponse)
async def submit_open_house_form_request(
    open_house_event_id: str,
    request_data: OpenHouseFormRequestCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Submit a "Request Seller Disclosure" or "Schedule a Second Visit" request, scoped to a
    specific open house event (public endpoint, scanned from the Cheat Sheet QR codes) - no
    address entry needed, same pattern as the open house sign-in form.
    """
    try:
        oh_result = await db.execute(select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_event_id))
        open_house_event = oh_result.scalar_one_or_none()
        if not open_house_event:
            raise HTTPException(status_code=404, detail="Open house not found")

        current_time = datetime.now(timezone.utc)
        form_request = OpenHouseFormRequest(
            open_house_event_id=open_house_event_id,
            form_type=request_data.form_type.value,
            full_name=request_data.full_name,
            email=request_data.email,
            phone=request_data.phone,
            wants_another_tour=request_data.wants_another_tour,
            another_tour_time=request_data.another_tour_time,
            preferred_visit_time=request_data.preferred_visit_time,
            wants_disclosure=request_data.wants_disclosure,
            created_at=current_time,
        )
        db.add(form_request)
        await db.commit()
        await db.refresh(form_request)

        property_address = open_house_event.address

        # Surface the specifics (preferred visit time, or the follow-up tour ask) on the lead's
        # activity timeline, not just the bare "requested X" summary.
        request_answers = []
        if request_data.form_type == OpenHouseFormRequestType.DISCLOSURE:
            summary = f"Requested seller disclosure - {property_address or 'Open House'}"
            notif_title = f"Disclosure Request: {request_data.full_name}"
            notif_message = "Requested the seller disclosure at your open house"
            if request_data.wants_another_tour:
                request_answers.append({"label": "Wants another tour", "value": "Yes"})
                if request_data.another_tour_time:
                    request_answers.append({"label": "Preferred time", "value": request_data.another_tour_time})
        else:
            summary = f"Requested a second visit - {property_address or 'Open House'}"
            notif_title = f"Second Visit Request: {request_data.full_name}"
            notif_message = "Requested a second visit to your open house"
            if request_data.preferred_visit_time:
                request_answers.append({"label": "Preferred visit time", "value": request_data.preferred_visit_time})
            if request_data.wants_disclosure:
                request_answers.append({"label": "Also wants seller disclosure", "value": "Yes"})

        lead = await upsert_lead(
            db,
            owner_id=open_house_event.agent_id,
            name=request_data.full_name,
            email=request_data.email,
            phone=request_data.phone,
            source_type=f"open_house_{request_data.form_type.value}",
            source_id=form_request.id,
            summary=summary,
            occurred_at=current_time,
            property_address=property_address,
            open_house_event_id=open_house_event_id,
            raw_snapshot={"answers": request_answers} if request_answers else None,
        )
        if lead:
            form_request.lead_id = lead.id
            db.add(form_request)
            await db.commit()
            await db.refresh(form_request)
            # One form, two asks: "also send me the disclosure" on the second-visit form, or "I'd
            # like another tour" on the disclosure form, gets its own follow-up too - so neither the
            # disclosure nor the showing can slip through behind the other.
            extra = None
            if request_data.form_type != OpenHouseFormRequestType.DISCLOSURE and request_data.wants_disclosure:
                extra = ("open_house_disclosure", f"Requested seller disclosure - {property_address or 'Open House'}", None)
            elif request_data.form_type == OpenHouseFormRequestType.DISCLOSURE and request_data.wants_another_tour:
                extra = ("open_house_second_visit", f"Requested a second visit - {property_address or 'Open House'}",
                         [{"label": "Preferred visit time", "value": request_data.another_tour_time}] if request_data.another_tour_time else None)
            if extra:
                try:
                    await upsert_lead(
                        db, owner_id=open_house_event.agent_id, name=request_data.full_name, email=request_data.email,
                        phone=request_data.phone, source_type=extra[0], source_id=form_request.id, summary=extra[1],
                        occurred_at=current_time, property_address=property_address,
                        open_house_event_id=open_house_event_id, raw_snapshot={"answers": extra[2]} if extra[2] else None,
                    )
                except Exception:
                    logger.exception("second ask on an open house request failed (non-fatal)")

        try:
            notification = Notification(
                agent_id=open_house_event.agent_id,
                type=f"OPEN_HOUSE_{request_data.form_type.value.upper()}",
                reference_type="OPEN_HOUSE_FORM_REQUEST",
                reference_id=form_request.id,
                title=notif_title,
                message=notif_message,
                property_address=property_address,
                visitor_name=request_data.full_name,
                # Takes the agent straight to where they act on this (mark Scheduled/Sent), not
                # the lead profile - the Open Houses page (OpenHouseFormRequestsPanel) is the
                # actual response surface, not Forms ("/studio/forms?tab=results" was a dead link;
                # that page has no results tab at all).
                link="/open-houses",
                is_read=False,
                created_at=current_time,
            )
            db.add(notification)
            await db.commit()
            from app.services.agent_email_service import absolute, email_agent
            is_disclosure = request_data.form_type == OpenHouseFormRequestType.DISCLOSURE
            asked_for = "the seller disclosure" if is_disclosure else "a second tour"
            email_agent(await db.get(User, open_house_event.agent_id), f"OPEN_HOUSE_{request_data.form_type.value.upper()}", "open_house_form_request",
                        f"{request_data.full_name} requested {asked_for}",
                        {"visitor_name": request_data.full_name, "request_label": asked_for,
                         "property_address": property_address, "visitor_email": request_data.email, "visitor_phone": request_data.phone,
                         "details": "; ".join(f"{a['label']}: {a['value']}" for a in request_answers) or None,
                         "link": absolute("/open-houses")})
        except Exception as e:
            logger.error("creating open house form request notification failed", extra={"error": str(e)})

        return OpenHouseFormRequestResponse(
            id=form_request.id,
            open_house_event_id=form_request.open_house_event_id,
            form_type=form_request.form_type,
            full_name=form_request.full_name,
            email=form_request.email,
            phone=form_request.phone,
            wants_another_tour=form_request.wants_another_tour,
            another_tour_time=form_request.another_tour_time,
            preferred_visit_time=form_request.preferred_visit_time,
            wants_disclosure=form_request.wants_disclosure,
            created_at=form_request.created_at,
            property_address=property_address,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("submitting open house form request failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to submit request")


@router.get("/api/open-houses/{open_house_id}/visitors", response_model=List[VisitorResponse])
async def get_open_house_visitors(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
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
        ).order_by(OpenHouseVisitor.created_at.asc())

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
                notes=visitor.notes,
                lead_id=visitor.lead_id,
                created_at=visitor.created_at
            )
            for visitor in visitors
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching visitors failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch visitors")

@router.put("/api/open-houses/{open_house_id}/note")
async def update_open_house_note(
    open_house_id: str,
    note_update: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Update the note for a specific open house event"""
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

        open_house.notes = note_update.notes
        await db.commit()

        return {"success": True, "message": "Note updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating open house note failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update note")

@router.put("/api/open-houses/{open_house_id}/qr-dark-mode")
async def update_qr_dark_mode(
    open_house_id: str,
    payload: QrDarkModeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Set the default theme (light/dark) for the on-screen Sign-In Sheet/Kiosk this open house's
    QR code leads to - toggled from the Documents window, applies the next time that page loads."""
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

        open_house.qr_dark_mode = payload.qr_dark_mode
        await db.commit()

        return {"success": True, "qr_dark_mode": open_house.qr_dark_mode}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating QR dark mode failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update QR dark mode")

@router.put("/api/open-houses/{open_house_id}/share-guests")
async def update_share_guests(
    open_house_id: str,
    payload: ShareGuestsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Hosting-agent-only toggle: whether the listing agent for this specific open house can see
    actual guest name/email/phone (GET /api/open-house-manager/events/{id}/guests), on top of the
    basic anonymous counts they always see. Off by default - see OpenHouseEvent.share_guests_with_listing_agent."""
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

        open_house.share_guests_with_listing_agent = payload.share_guests_with_listing_agent
        await db.commit()

        return {"success": True, "share_guests_with_listing_agent": open_house.share_guests_with_listing_agent}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating guest sharing failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update guest sharing")

def _spotlight_fields(info: Optional[dict], oh: OpenHouseEvent) -> dict:
    """own_spotlight_url / can_make_spotlight / spotlight_draft_id for one open house, from
    OpenHouseService.listing_spotlights (absent = not their own listing, or not on IDX). "Make"
    needs a real MLS listing to pre-pick, so a manually-entered home never offers it."""
    if not info:
        return {}
    return {"own_spotlight_url": info["url"], "spotlight_draft_id": info["draft_id"],
            "can_make_spotlight": not info["url"] and bool(oh.listing_key)}


def _guest_pass_available(user: User) -> bool:
    from app.utils.auth import owner_has_access, MLS_ACCESS_TIERS
    return owner_has_access(user, MLS_ACCESS_TIERS)


def _showcases_available(user: User) -> bool:
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    return owner_has_access(user, SHOWCASE_ACCESS_TIERS)


@router.put("/api/open-houses/{open_house_id}/guest-settings")
async def update_guest_settings(
    open_house_id: str,
    payload: GuestSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Per open house: auto-build Showcases for guests without an agent, the Similar Homes sheet +
    Guest Pass similar homes, the Guest Pass itself and its footer, and (No-MLS Pro) where the
    guest's phone goes after signing in. Any field may be sent alone."""
    open_house = (await db.execute(
        select(OpenHouseEvent).where(and_(OpenHouseEvent.id == open_house_id, OpenHouseEvent.agent_id == current_user.id))
    )).scalar_one_or_none()
    if not open_house:
        raise HTTPException(status_code=404, detail="Open house not found")
    if payload.auto_showcase is not None:
        open_house.auto_showcase = payload.auto_showcase
    if payload.similar_homes is not None:
        open_house.similar_homes = payload.similar_homes
    if payload.after_signin is not None:
        open_house.after_signin = payload.after_signin
    if payload.guest_pass is not None:
        open_house.guest_pass = payload.guest_pass
    if payload.pass_signature is not None:
        open_house.pass_signature = payload.pass_signature
    await db.commit()
    return {"success": True, "auto_showcase": open_house.auto_showcase, "similar_homes": open_house.similar_homes,
            "after_signin": open_house.after_signin, "guest_pass": open_house.guest_pass, "pass_signature": open_house.pass_signature}


@router.put("/api/visitors/{visitor_id}/note")
async def update_visitor_note(
    visitor_id: str,
    note_update: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Update the note for a specific visitor"""
    try:
        # First ensure the visitor belongs to an open house owned by the current user
        stmt = select(OpenHouseVisitor).join(OpenHouseEvent).where(
            and_(
                OpenHouseVisitor.id == visitor_id,
                OpenHouseEvent.agent_id == current_user.id
            )
        )
        result = await db.execute(stmt)
        visitor = result.scalar_one_or_none()

        if not visitor:
            raise HTTPException(status_code=404, detail="Visitor not found or access denied")

        visitor.notes = note_update.notes
        await db.commit()

        return {"success": True, "message": "Visitor note updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating visitor note failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update visitor note")


async def _load_owned_visitor(db: AsyncSession, visitor_id: str, owner_id: str) -> OpenHouseVisitor:
    stmt = select(OpenHouseVisitor).join(OpenHouseEvent).where(
        and_(OpenHouseVisitor.id == visitor_id, OpenHouseEvent.agent_id == owner_id)
    )
    result = await db.execute(stmt)
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found or access denied")
    return visitor


@router.get("/api/visitors/{visitor_id}/notes")
async def list_visitor_notes(
    visitor_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """
    Notes for a visitor are the SAME LeadNote rows shown on their matching Leads profile - this
    just filters that shared history down to the visitor's linked lead, so edits on either page
    are always looking at the same records.
    """
    visitor = await _load_owned_visitor(db, visitor_id, current_user.id)
    if not visitor.lead_id:
        return []
    result = await db.execute(
        select(LeadNote)
        .options(selectinload(LeadNote.author), selectinload(LeadNote.open_house_event))
        .where(LeadNote.lead_id == visitor.lead_id)
        .order_by(LeadNote.created_at.desc())
    )
    notes = result.scalars().all()
    return [_to_lead_note_out(n) for n in notes]


@router.post("/api/visitors/{visitor_id}/notes")
async def create_visitor_note(
    visitor_id: str,
    body: LeadNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    visitor = await _load_owned_visitor(db, visitor_id, current_user.id)
    if not visitor.lead_id:
        raise HTTPException(status_code=400, detail="This visitor isn't linked to a lead record yet, so a shared note can't be added.")
    note = LeadNote(
        lead_id=visitor.lead_id,
        author_id=current_user.id,
        text=body.text,
        open_house_event_id=body.open_house_event_id or visitor.open_house_event_id,
    )
    db.add(note)
    await db.commit()
    note = await _reload_note(db, note.id)
    out = _to_lead_note_out(note)
    await _note_changed(db, current_user.id, visitor)
    return out


async def _note_changed(db: AsyncSession, owner_id: str, visitor: Optional[OpenHouseVisitor]) -> None:
    """A note on an open house guest drives their follow-up: shows on the unrepresented guest's
    event, or (guest with an agent) makes / refreshes the heads-up one. Never fails the note."""
    if visitor is None:
        return
    from app.services.tasks_service import on_visitor_note
    await on_visitor_note(db, owner_id, visitor)


async def _visitor_for_note(db: AsyncSession, note: LeadNote, owner_id: str) -> Optional[OpenHouseVisitor]:
    stmt = (select(OpenHouseVisitor).join(OpenHouseEvent)
            .where(OpenHouseVisitor.lead_id == note.lead_id, OpenHouseEvent.agent_id == owner_id)
            .order_by(OpenHouseVisitor.created_at.desc()))
    if note.open_house_event_id:
        stmt = stmt.where(OpenHouseVisitor.open_house_event_id == note.open_house_event_id)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


@router.patch("/api/visitors/notes/{note_id}")
async def update_visitor_note_thread(
    note_id: str,
    body: LeadNoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    # Same shared lead_notes table as the Leads page - reuse its ownership check so both
    # surfaces enforce the identical permission rule against the same record.
    note = await _load_owned_note(db, note_id, current_user.id)
    note.text = body.text
    db.add(note)
    await db.commit()
    note = await _reload_note(db, note_id)
    out = _to_lead_note_out(note)
    await _note_changed(db, current_user.id, await _visitor_for_note(db, note, current_user.id))
    return out


@router.delete("/api/visitors/notes/{note_id}")
async def delete_visitor_note_thread(
    note_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    note = await _load_owned_note(db, note_id, current_user.id)
    visitor = await _visitor_for_note(db, note, current_user.id)
    await db.delete(note)
    await db.commit()
    await _note_changed(db, current_user.id, visitor)
    return {"success": True}

@router.patch("/api/open-houses/{open_house_id}/snapshot", dependencies=[Depends(require_broker_authorization)])
async def update_open_house_snapshot(
    open_house_id: str,
    update_data: OpenHouseSnapshotUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Update the similar properties snapshot for a specific open house"""
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

        open_house.similar_properties_snapshot = update_data.similar_properties_snapshot
        await db.commit()

        return {"success": True, "message": "Snapshot updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating open house snapshot failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update snapshot")


@router.post("/api/open-houses/{open_house_id}/snapshot/refresh", dependencies=[Depends(require_broker_authorization)])
async def refresh_open_house_snapshot(
    open_house_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Re-checks every property currently in the 'Similar Homes' snapshot against our local MLS
    mirror: refreshes its price if it's changed, backfills lot size/acres, school district, and
    year built for older snapshots that predate those fields being captured, and drops the
    property entirely if it's no longer active (sold/withdrawn/expired/off-market) - so a Property
    Recommendations PDF prepared ahead of time doesn't quietly go stale before the open house
    happens. Reads the already-synced Property table (no live Bright MLS call), so it's cheap
    enough for the agent to click as often as they want."""
    try:
        stmt = select(OpenHouseEvent).where(
            and_(OpenHouseEvent.id == open_house_id, OpenHouseEvent.agent_id == current_user.id)
        )
        result = await db.execute(stmt)
        open_house = result.scalar_one_or_none()
        if not open_house:
            raise HTTPException(status_code=404, detail="Open house not found")

        snapshot = open_house.similar_properties_snapshot or []
        listing_keys = list({
            str(item.get("ListingKey") or item.get("listing_key") or "")
            for item in snapshot
            if item.get("ListingKey") or item.get("listing_key")
        })

        props_by_key = {}
        if listing_keys:
            props_result = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
            for prop in props_result.scalars().all():
                props_by_key[prop.listing_key] = prop

        updated_snapshot = []
        removed_count = 0
        updated_count = 0
        for item in snapshot:
            key = str(item.get("ListingKey") or item.get("listing_key") or "")
            prop = props_by_key.get(key)
            if not prop or (prop.home_status or "").upper() in INACTIVE_STATUSES:
                removed_count += 1
                continue

            changed = False
            if prop.price is not None and item.get("ListPrice") != prop.price:
                item = {**item, "ListPrice": prop.price}
                changed = True

            # Backfill lot size/acres, school district, and year built for snapshots captured
            # before these fields were tracked (see the frontend's normalizeSimilarProperty, added
            # after these older snapshots) - they have none of these keys at all, so the Similar
            # Homes PDF's Acres/Built/School District stats have nothing to show for them. Only
            # fills in items that are actually missing a given field - never overwrites a real
            # value already on the item with the mirror's.
            if not (item.get("LotSizeAcres") or item.get("acres")) and prop.lot_size:
                acres = round(prop.lot_size / 43560, 2)
                item = {
                    **item,
                    "LotSizeSquareFeet": prop.lot_size,
                    "lotSizeSquareFeet": prop.lot_size,
                    "LotSizeAcres": acres,
                    "lotSizeAcres": acres,
                    "acres": acres,
                }
                changed = True

            if not item.get("SchoolDistrictName") and prop.school_district_name:
                item = {**item, "SchoolDistrictName": prop.school_district_name}
                changed = True

            if not item.get("YearBuilt") and prop.year_built:
                item = {**item, "YearBuilt": prop.year_built}
                changed = True

            if changed:
                updated_count += 1
            updated_snapshot.append(item)

        open_house.similar_properties_snapshot = updated_snapshot
        await db.commit()

        return {
            "success": True,
            "similarPropertiesSnapshot": updated_snapshot,
            "removedCount": removed_count,
            "updatedCount": updated_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("refreshing open house snapshot failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to refresh snapshot")

@router.patch("/api/open-houses/{open_house_id}/cheat-sheet-snapshot", dependencies=[Depends(require_broker_authorization)])
async def update_open_house_cheat_sheet_snapshot(
    open_house_id: str,
    update_data: OpenHouseCheatSheetSnapshotUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Freeze the Cheat Sheet payload for an open house kit."""
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

        open_house.cheat_sheet_snapshot = update_data.cheat_sheet_snapshot
        await db.commit()

        return {"success": True, "message": "Cheat Sheet snapshot saved successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating open house cheat sheet snapshot failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update Cheat Sheet snapshot")


@router.patch("/api/open-houses/{open_house_id}/cheat-sheet-photos", dependencies=[Depends(require_broker_authorization)])
async def update_open_house_cheat_sheet_photos(
    open_house_id: str,
    update_data: OpenHouseCheatSheetPhotosUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan)
):
    """Updates which photos (and crop position) show on the Cheat Sheet, then clears the frozen
    snapshot so the next preview rebuilds it fresh from these photos instead of serving stale
    ones - same "edit re-derives, doesn't hand-patch the frozen copy" approach as Similar Homes."""
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

        open_house.cheat_sheet_photos = update_data.cheat_sheet_photos
        open_house.cheat_sheet_snapshot = None
        await db.commit()

        return {"success": True, "message": "Cheat Sheet photos updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating open house cheat sheet photos failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to update Cheat Sheet photos")
