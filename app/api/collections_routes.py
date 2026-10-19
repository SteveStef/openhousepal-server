from fastapi import APIRouter, HTTPException, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, List, Optional
from pydantic import BaseModel
from datetime import datetime, timezone

from app.database import get_db
from app.schemas.collection import CollectionCreate, CollectionResponse
from app.schemas.collection_preferences import CollectionPreferencesBase, CollectionPreferencesCreate, CollectionPreferencesUpdate, ShowcaseSelfServePreferencesUpdate
from app.models.property import PropertySummaryResponse
from app.schemas.property_interactions import (
    PropertyInteractionUpdate,
    PropertyCommentCreate,
)
from app.schemas.property_tour import (
    PropertyTourCreate,
    PropertyTourResponse,
    PropertyTourCompletionUpdate,
    PropertyTourConfirmUpdate
)
from app.services.collections_service import CollectionsService
from app.services.property_interactions_service import PropertyInteractionsService
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.property_service import property_service
from app.services.property_tour_service import PropertyTourService
from app.services.blacklist_service import BlacklistService
from app.services.leads_service import upsert_lead
from app.utils.auth import get_current_active_user, get_current_user_optional, require_premium_plan, require_broker_authorization
from app.models.database import User, Collection, Notification
from sqlalchemy import select, update, and_
from app.services.showcase_links import share_match
from sqlalchemy.orm import joinedload
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/collections", tags=["collections"])


class CreateCollectionWithPreferencesRequest(BaseModel):
    # Collection info
    name: str
    
    # Customer info
    visitor_name: str
    visitor_email: str
    visitor_phone: str
    visiting_reason: str
    has_agent: str
    additional_comments: str = ""
    
    # Preferences
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_year_built: Optional[int] = None
    max_year_built: Optional[int] = None
    cities: Optional[list[str]] = None
    townships: Optional[list[str]] = None
    school_districts: Optional[list[str]] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    diameter: float = 0

    # Home type preferences
    is_town_house: Optional[bool] = False
    is_lot_land: Optional[bool] = False
    is_condo: Optional[bool] = False
    is_multi_family: Optional[bool] = False
    is_single_family: Optional[bool] = False
    is_apartment: Optional[bool] = False
    is_commercial: Optional[bool] = False
    is_farm: Optional[bool] = False

    # The rest of the map editor's search (the create flow ends in the same editor as Edit).
    # All optional - older callers that don't send them get the previous behaviour.
    counties: Optional[list[str]] = None
    search_polygon: Optional[dict[str, Any]] = None
    min_sqft: Optional[float] = None
    max_sqft: Optional[float] = None
    min_acres: Optional[float] = None
    max_acres: Optional[float] = None
    special_features: Optional[str] = None
    compare_property_listing_key: Optional[str] = None
    show_match_score: bool = True
    # "Email it to them" in the agent's create flow: their link + a few homes, right away.
    email_client: bool = False


class UpdateStatusRequest(BaseModel):
    status: str


class NotificationSettingsRequest(BaseModel):
    notify_visitor: bool
    notify_agent: bool


class NotificationSettingsSharedRequest(BaseModel):
    notify_visitor: bool


class UnsubscribeRequest(BaseModel):
    email: str


class ShareToggleRequest(BaseModel):
    make_public: bool
    force_regenerate: bool = False


async def _owner_id_if_owner(db: AsyncSession, collection_id: str, user: Optional[User]) -> Optional[str]:
    """The public Showcase actions (like, view, comment, tour) treat the caller as the agent only
    when they OWN this Showcase. Anyone else - even someone signed in to their own OpenHousePal
    account in the same browser - is a visitor, so their actions alert the agent and log on the lead
    like any guest's (a signed-in non-owner used to be silently skipped, or counted as the agent)."""
    if user is None:
        return None
    owner_id = (await db.execute(select(Collection.owner_id).where(Collection.id == collection_id))).scalar_one_or_none()
    return user.id if owner_id == user.id else None


@router.post("/unsubscribe")
async def unsubscribe_visitor(
    request: Optional[UnsubscribeRequest] = None,
    email: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Adds the visitor's email to the blacklist to prevent future emails.
    Used by the /unsubscribe page (the link in every buyer email's footer), which sends the address
    as ?email=... - this used to require a JSON body only, so the page's Confirm button always
    failed (422) and nobody could unsubscribe from the footer link. Body still accepted.
    """
    address = ((request.email if request else None) or email or "").strip()
    if "@" not in address:
        raise HTTPException(status_code=400, detail="That unsubscribe link is missing an email address.")
    try:
        if await BlacklistService.blacklist_email(db, address):
            from app.services.leads_service import log_unsubscribe
            await log_unsubscribe(db, address)
        return {"success": True, "message": f"Successfully unsubscribed {address}"}
    except Exception as e:
        logger.error(f"Error unsubscribing visitor {address}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not unsubscribe right now. Please try again.")


@router.post("/unsubscribe/one-click")
async def unsubscribe_one_click(
    email: str, 
    db: AsyncSession = Depends(get_db)
):
    """
    RFC 8058 compliant one-click unsubscribe.
    Gmail/Yahoo send a POST to this URL with the email in the query string.
    """
    try:
        if await BlacklistService.blacklist_email(db, email):
            from app.services.leads_service import log_unsubscribe
            await log_unsubscribe(db, email)
        return {"success": True}
    except Exception as e:
        logger.error(f"One-click unsubscribe failed for {email}: {str(e)}")
        # Return 200 anyway to satisfy mail servers, but log the error
        return {"success": False}


@router.get("/", response_model=List[CollectionResponse], dependencies=[Depends(require_broker_authorization)])
async def get_all_collections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Get all collections for the authenticated user
    """
    try:
        collections = await CollectionsService.get_user_collections(db, current_user.id)
        return collections
        
    except Exception as e:
        logger.error("fetching collections failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collections"
        )


class PreviewMatchesResponse(BaseModel):
    properties: List[PropertySummaryResponse]
    count: int


@router.post("/preview-matches", response_model=PreviewMatchesResponse, dependencies=[Depends(require_broker_authorization)])
async def preview_matches(
    preferences: CollectionPreferencesBase,
    db: AsyncSession = Depends(get_db),
):
    """
    Live, unsaved preview of what a set of search preferences would match - the map-first
    Showcase editor calls this on every criteria change so the map/grid update instantly without
    writing anything to the database. Deliberately calls the exact same PropertyService function
    the real save path (update_preferences_and_refresh_properties) and the visitor self-serve path
    both use, so the preview is never out of sync with what Save actually produces - no second
    search implementation.
    """
    try:
        properties = await property_service.get_properties_by_preferences(db, preferences, max_properties=200)
        count = await property_service.get_properties_count_by_preferences(db, preferences)
        return PreviewMatchesResponse(properties=properties, count=count)
    except Exception as e:
        logger.error("previewing showcase matches failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to preview matching properties"
        )


@router.get("/short/{code}")
async def resolve_short_link(code: str, db: AsyncSession = Depends(get_db)):
    """/s/<code> -> the Showcase it stands for (public: the short link IS the share link)."""
    row = (await db.execute(select(Collection.share_token).where(Collection.short_code == code.lower()))).first()
    if not row or not row[0]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That link isn't active.")
    return {"share_token": row[0]}


async def _home_ids(db: AsyncSession, property_id: Optional[str], listing_key: Optional[str]) -> List[str]:
    """The Property rows a home page stands for (its id, and/or every row with its MLS number)."""
    from app.models.database import Property
    ids: set = set()
    if property_id:
        if (await db.execute(select(Property.id).where(Property.id == property_id))).first():
            ids.add(property_id)
    if listing_key:
        ids |= {r[0] for r in (await db.execute(select(Property.id).where(Property.listing_key == listing_key))).all()}
    return list(ids)


@router.get("/membership", dependencies=[Depends(require_broker_authorization)])
async def showcase_membership(
    property_id: Optional[str] = Query(None),
    listing_key: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """A home's page, agent side (user, 10/18): which of the agent's Showcases already have this
    home, and the rest - so the bar under the photos can list them and "Add to a Showcase" can
    offer the others (the ones that have it shown greyed out)."""
    from sqlalchemy import func
    from app.models.database import PropertyInteraction, collection_properties
    ids = await _home_ids(db, property_id, listing_key)
    cols = (await db.execute(select(Collection).where(Collection.owner_id == current_user.id)
                             .order_by(Collection.updated_at.desc().nullslast(), Collection.created_at.desc()))).scalars().all()
    if not cols:
        return {"home_ids": ids, "showcases": []}
    col_ids = [c.id for c in cols]
    counts = dict((await db.execute(select(collection_properties.c.collection_id, func.count())
                                   .where(collection_properties.c.collection_id.in_(col_ids))
                                   .group_by(collection_properties.c.collection_id))).all())
    having = {r[0] for r in (await db.execute(select(collection_properties.c.collection_id).where(
        collection_properties.c.collection_id.in_(col_ids), collection_properties.c.property_id.in_(ids)))).all()} if ids else set()
    picked = {r[0] for r in (await db.execute(select(PropertyInteraction.collection_id).where(
        PropertyInteraction.collection_id.in_(col_ids), PropertyInteraction.property_id.in_(ids),
        PropertyInteraction.agent_recommended_at.is_not(None)))).all()} if ids else set()
    return {
        "home_ids": ids,
        "showcases": [{
            "id": c.id, "name": c.name, "client": c.visitor_name, "status": c.status,
            "homes": int(counts.get(c.id, 0)), "contains": c.id in having, "recommended": c.id in picked,
        } for c in cols],
    }


class AddHomeBody(BaseModel):
    property_id: Optional[str] = None
    listing_key: Optional[str] = None


@router.post("/{collection_id}/add-home", dependencies=[Depends(require_broker_authorization)])
async def add_home_to_showcase(
    collection_id: str,
    body: AddHomeBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The agent puts one home into one of their client's Showcases from that home's page. It's
    marked as the agent's pick (Recommended) - which also keeps it there when the Showcase refreshes
    from its search, even if the home doesn't match it - and counts as new for the client."""
    from sqlalchemy import insert
    from app.models.database import PropertyInteraction, collection_properties
    coll = (await db.execute(select(Collection).where(Collection.id == collection_id))).scalar_one_or_none()
    if not coll or coll.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found")
    ids = await _home_ids(db, body.property_id, body.listing_key)
    if not ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This home isn't in the listings yet.")
    already = {r[0] for r in (await db.execute(select(collection_properties.c.property_id).where(
        collection_properties.c.collection_id == collection_id, collection_properties.c.property_id.in_(ids)))).all()}
    home_id = next(iter(already)) if already else (body.property_id if body.property_id in ids else ids[0])
    now = datetime.now(timezone.utc)
    if not already:
        await db.execute(insert(collection_properties).values(collection_id=collection_id, property_id=home_id, added_at=now))
    row = (await db.execute(select(PropertyInteraction).where(
        and_(PropertyInteraction.collection_id == collection_id, PropertyInteraction.property_id == home_id)))).scalar_one_or_none()
    if row is None:
        row = PropertyInteraction(collection_id=collection_id, property_id=home_id, liked=False, disliked=False, created_at=now, updated_at=now)
        db.add(row)
    row.disliked = False
    row.agent_recommended_at = row.agent_recommended_at or now
    coll.updated_at = now
    await db.commit()
    return {"success": True, "added": not already, "property_id": home_id}


@router.get("/refine-requests/count")
async def get_pending_refine_requests_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Drives the Showcases nav badge - how many showcases have a visitor's refine-search
    message still sitting unaddressed. Registered ahead of /{collection_id} so "refine-requests"
    doesn't get swallowed as a collection_id path param."""
    count = await CollectionsService.count_pending_refine_requests(db, str(current_user.id))
    return {"count": count}


@router.get("/inbox")
async def get_showcases_inbox(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """The Showcases page header's Tour requests + Activity inboxes (registered ahead of
    /{collection_id} so "inbox" isn't read as a collection id)."""
    return await CollectionsService.get_inbox(db, str(current_user.id))


@router.post("/inbox/read")
async def mark_showcases_inbox_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    await CollectionsService.mark_inbox_read(db, str(current_user.id))
    return {"success": True}


@router.get("/{collection_id}/delete-preview")
async def get_collection_delete_preview(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """What else goes when this Showcase is deleted - shown in the delete confirmation."""
    preview = await CollectionsService.delete_preview(db, collection_id, str(current_user.id))
    if preview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return preview


@router.get("/{collection_id}", response_model=CollectionResponse, dependencies=[Depends(require_broker_authorization)])
async def get_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Get a specific collection by ID
    """
    try:
        collection = await CollectionsService.get_collection_by_id(db, collection_id, current_user.id)
        
        if not collection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found"
            )
            
        return collection
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching collection failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collection"
        )


@router.post("/", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_broker_authorization)])
async def create_collection(
    collection_data: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Create a new collection
    """
    try:
        collection = await CollectionsService.create_collection(db, collection_data, current_user.id)
        
        # Auto-generate preferences if collection has an original open house event
        if collection.get("original_open_house_event_id"):
            try:
                await CollectionPreferencesService.auto_generate_preferences(db, collection["id"])
            except Exception as e:
                pass
        
        return collection
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error("creating collection failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create collection"
        )


@router.post("/create-manually", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_broker_authorization)])
async def create_collection_with_preferences(
    request: CreateCollectionWithPreferencesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    try:
        '''
            If address is used, then make a requests like before to get lat and long of the property
            If City or Township is used, make a zillow request per city/township
        '''
        latitude = request.lat if request.lat is not None else 0
        longitude = request.long if request.long is not None else 0
        
        try:
            # Only perform MLS lookup if coordinates weren't provided by frontend and address exists
            if latitude == 0 and longitude == 0 and request.address and len(request.address) > 0:
                try:
                    # Try to fetch property details from mirror
                    property_details = await property_service.get_property_by_address(db, request.address)
                    if property_details:
                        latitude = property_details.latitude
                        longitude = property_details.longitude

                    if not latitude or not longitude:
                        raise HTTPException(
                            status_code=400,
                            detail="Could not determine coordinates for the provided address"
                        )
                except HTTPException as e:
                    if e.status_code == 404:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Property not found for the provided address"
                        )
                    else:
                        raise e
                except Exception as e:
                    logger.error("looking up address coordinates failed", extra={"error": str(e)})
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to lookup property coordinates"
                    )
            
            # Generate share token for public access
            share_token = CollectionsService.generate_share_token()

            # Determine if this collection should be active or inactive based on current count
            should_be_active = await CollectionsService.should_create_as_active(db, current_user.id)
            collection_status = 'ACTIVE' if should_be_active else 'INACTIVE'

            active_count = await CollectionsService.count_active_collections(db, current_user.id)

            # Create collection directly with auto-generated share token
            collection = Collection(
                owner_id=current_user.id,
                name=request.name,
                description=f"Properties for {request.visitor_name} near {request.address}" if request.address else f"Properties for {request.visitor_name}",
                visitor_email=request.visitor_email,
                visitor_name=request.visitor_name,
                visitor_phone=request.visitor_phone,
                share_token=share_token,
                is_public=True,  # Default to public with share link
                status=collection_status,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            
            db.add(collection)
            await db.commit()
            await db.refresh(collection)

            matched_lead = await upsert_lead(
                db,
                owner_id=current_user.id,
                name=request.visitor_name,
                email=request.visitor_email,
                phone=request.visitor_phone,
                source_type="collection_visitor",
                source_id=collection.id,
                summary=f"Showcase created - {collection.name}",
                occurred_at=collection.created_at,
                represented='yes' if request.has_agent == 'YES' else ('no' if request.has_agent == 'NO' else None),
                property_address=request.address,
                collection_id=collection.id,
            )
            if matched_lead and matched_lead.showcase_deleted_at:
                matched_lead.showcase_deleted_at = None
                db.add(matched_lead)
                await db.commit()

            # Create collection preferences with looked up coordinates
            preferences_data = CollectionPreferencesCreate(
                collection_id=collection.id,
                min_beds=request.min_beds,
                max_beds=request.max_beds,
                min_baths=request.min_baths,
                max_baths=request.max_baths,
                min_price=request.min_price,
                max_price=request.max_price,
                min_year_built=request.min_year_built,
                max_year_built=request.max_year_built,
                # These are list fields on CollectionPreferencesCreate - a caller that omits
                # them sends None, which fails validation with a 500. Coerce to [].
                cities=request.cities or [],
                townships=request.townships or [],
                school_districts=request.school_districts or [],
                lat=latitude,
                long=longitude,
                address=request.address,  # Add missing address field
                diameter=request.diameter,
                is_town_house=request.is_town_house,
                is_lot_land=request.is_lot_land,
                is_condo=request.is_condo,
                is_multi_family=request.is_multi_family,
                is_single_family=request.is_single_family,
                is_apartment=request.is_apartment,
                is_commercial=request.is_commercial,
                is_farm=request.is_farm,
                special_features=request.special_features if request.special_features is not None else request.additional_comments,
                visiting_reason=request.visiting_reason,
                has_agent=request.has_agent,
                counties=request.counties or [],
                search_polygon=request.search_polygon,
                min_sqft=request.min_sqft,
                max_sqft=request.max_sqft,
                min_acres=request.min_acres,
                max_acres=request.max_acres,
                compare_property_listing_key=request.compare_property_listing_key,
                show_match_score=request.show_match_score,
            )
            
            await CollectionPreferencesService.create_preferences(db, preferences_data)
            
            # Populate collection with properties immediately after creation. Collection creation
            # must still succeed even if population fails - but don't swallow it silently: log
            # loudly (an empty showcase is a visible-to-the-agent quality issue, not a crash).
            try:
                population_result = await CollectionsService.repopulate_collection_from_preferences(db, collection.id, commit=True)
                if not population_result.get('success'):
                    logger.warning(
                        "Showcase %s populated 0 properties (population reported failure): %s",
                        collection.id, population_result,
                    )
            except Exception as e:
                logger.error(
                    "Showcase %s population raised - collection kept but may be empty: %s",
                    collection.id, e, exc_info=True,
                )

            if request.email_client:
                from app.services.showcase_digest_service import queue_showcase_ready
                await queue_showcase_ready(db, collection.id, from_agent=True)

            collections = await CollectionsService.get_user_collections(db, current_user.id)
            
            if collections:
                latest_collection = max(collections, key=lambda x: x['created_at'])
                return latest_collection
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Collection created but could not retrieve"
                )
        except Exception as e:
            logger.error(f"Error in create_collection_with_preferences: {e}")
            raise
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("creating collection from address failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create collection"
        )


@router.patch("/{collection_id}/status")
async def update_collection_status(
    collection_id: str,
    request: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Update the status of a collection (ACTIVE/INACTIVE)
    """
    try:
        # Validate status value
        valid_statuses = ['ACTIVE', 'INACTIVE']
        if request.status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
            )

        # Update collection status (no limit check)
        success = await CollectionsService.update_collection_status(
            db, collection_id, current_user.id, request.status
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found or access denied"
            )
            
        return {"message": "Collection status updated successfully", "status": request.status}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating collection status failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update collection status"
        )


@router.patch("/{collection_id}/notifications")
async def update_collection_notifications(
    collection_id: str,
    request: NotificationSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Update the notification settings of a collection
    """
    try:
        success = await CollectionsService.update_collection_notifications(
            db, collection_id, current_user.id, request.notify_visitor, request.notify_agent
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found or access denied"
            )
            
        return {
            "success": True,
            "message": "Collection notifications updated successfully", 
            "notify_visitor": request.notify_visitor,
            "notify_agent": request.notify_agent
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating collection notifications failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update collection notifications"
        )


@router.patch("/{collection_id}/dismiss")
async def dismiss_new_listings(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    result = await CollectionsService.dismiss_new_listings(db, collection_id, str(current_user.id))
    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if result.get("error") == "Collection not found" else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error", "Failed to dismiss listings")
        )
    return {"success": True}


@router.post("/{collection_id}/mark-seen")
async def mark_collection_seen(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Called once when the agent opens a showcase (e.g. clicking into it from a Leads/Dashboard
    alert) - clears every pending like/dislike/comment/tour-request tag on it at once."""
    result = await CollectionsService.mark_collection_seen(db, collection_id, str(current_user.id))
    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if result.get("error") == "Collection not found" else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error", "Failed to mark collection seen")
        )
    return {"success": True}


class RefineRequestIn(BaseModel):
    message: str


@router.post("/{collection_id}/refine-request")
async def submit_refine_request(
    collection_id: str,
    payload: RefineRequestIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Visitor-facing: send the agent a free-text message asking them to change the search,
    instead of (or alongside) editing the criteria themselves. No auth - same public access as
    the other visitor showcase endpoints (interact, comments, view)."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)  # bots can't flood the agent's leads/inbox
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message is required")
    if len(message) > 2000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message is too long")

    result = await CollectionsService.submit_refine_request(db, collection_id, message)
    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if result.get("error") == "Collection not found" else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error", "Failed to send message")
        )
    return {"success": True}


@router.post("/{collection_id}/refine-request/dismiss")
async def dismiss_refine_request(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    result = await CollectionsService.dismiss_refine_request(db, collection_id, str(current_user.id))
    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if result.get("error") == "Collection not found" else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error", "Failed to dismiss message")
        )
    return {"success": True}


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Delete a collection
    """
    try:
        success = await CollectionsService.delete_collection(db, collection_id, current_user.id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found"
            )
            
        return {"message": "Collection deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("deleting collection failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete collection"
        )


@router.post("/{collection_id}/properties/{property_id}/interact")
async def update_property_interaction(
    collection_id: str,
    property_id: str,
    interaction_data: PropertyInteractionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Update user interaction with a property within a collection (like, dislike)
    For both authenticated users (agents) and anonymous visitors (shared collections)
    """
    try:
        if interaction_data.interaction_type and interaction_data.value is not None:
            if interaction_data.interaction_type == 'like':
                interaction_data.liked = interaction_data.value
            elif interaction_data.interaction_type == 'dislike':
                interaction_data.disliked = interaction_data.value

        interaction = await PropertyInteractionsService.create_property_interaction(
            db, collection_id, property_id, interaction_data,
            user_id=await _owner_id_if_owner(db, collection_id, current_user)
        )
        
        return {
            "success": True,
            "interaction": interaction
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating property interaction failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update property interaction"
        )


class RecommendUpdate(BaseModel):
    recommended: bool


@router.put("/{collection_id}/properties/{property_id}/recommend")
async def recommend_property(
    collection_id: str,
    property_id: str,
    body: RecommendUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The agent's "I recommend this home for you" on one home in a client's Showcase (or taking it
    back). The home shows a Recommended tag on both sides and sorts to the top."""
    from app.models.database import PropertyInteraction
    coll = (await db.execute(select(Collection).where(Collection.id == collection_id))).scalar_one_or_none()
    if not coll or coll.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found")
    row = (await db.execute(select(PropertyInteraction).where(
        and_(PropertyInteraction.collection_id == collection_id, PropertyInteraction.property_id == property_id)))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        if not body.recommended:
            return {"success": True, "recommended": False}
        row = PropertyInteraction(collection_id=collection_id, property_id=property_id, liked=False, disliked=False, created_at=now, updated_at=now)
        db.add(row)
    # Recommending a home the client passed on puts it back in their Matches.
    if body.recommended and row.disliked:
        row.disliked = False
    row.agent_recommended_at = now if body.recommended else None
    await db.commit()
    return {"success": True, "recommended": body.recommended}


@router.post("/{collection_id}/properties/{property_id}/view")
async def track_property_view(
    collection_id: str,
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Track a property view by incrementing view_count and updating last_viewed_at.
    For both authenticated users (agents) and anonymous visitors (shared collections)
    """
    try:
        interaction = await PropertyInteractionsService.track_property_view(
            db, collection_id, property_id,
            user_id=await _owner_id_if_owner(db, collection_id, current_user)
        )

        return {
            "success": True,
            "interaction": interaction
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("tracking property view failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to track property view"
        )


@router.post("/{collection_id}/properties/{property_id}/comments")
async def add_property_comment(
    collection_id: str,
    property_id: str,
    comment_data: PropertyCommentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    owner_id = await _owner_id_if_owner(db, collection_id, current_user)
    if owner_id is None:
        from app.services.public_rate_limit import check_public_submit
        check_public_submit(request)  # bots can't flood the agent's leads/inbox

    """
    Add a comment to a property within a collection
    Supports both authenticated users and anonymous visitors
    """
    try:
        # Handle alternative frontend format
        if comment_data.comment and not comment_data.content:
            comment_data.content = comment_data.comment
        comment_data.content = (comment_data.content or '').strip()
        if not comment_data.content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Write a comment first.")
        if len(comment_data.content) > 4000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That comment is too long.")
        
        # Create anonymous comment - no user identification required
        comment = await PropertyInteractionsService.add_property_comment(
            db, collection_id, property_id, comment_data,
            user_id=owner_id
        )

        return {
            "success": True,
            "comment": comment
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("adding property comment failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add property comment"
        )


@router.get("/{collection_id}/properties/{property_id}/comments")
async def get_property_comments(
    collection_id: str,
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Get all comments for a property within a collection
    Public endpoint for shared collections - also used by the agent's own showcase view,
    which is how we tell the two apart below.
    """
    try:
        comments = await PropertyInteractionsService.get_property_comments(
            db, collection_id, property_id
        )

        if current_user:
            # Only the agent actually reading this property's comments clears its "New
            # Comment" signal and per-property unread badge - a guest hitting this same
            # public endpoint on their own showcase must not clear the agent's notification.
            collection_result = await db.execute(
                select(Collection).where(Collection.id == collection_id)
            )
            collection_obj = collection_result.scalar_one_or_none()
            if collection_obj and collection_obj.owner_id == current_user.id:
                await db.execute(
                    update(Notification)
                    .where(
                        Notification.agent_id == current_user.id,
                        Notification.collection_id == collection_id,
                        Notification.property_id == property_id,
                        Notification.type == 'PROPERTY_COMMENT',
                        Notification.is_read.is_(False),
                    )
                    .values(is_read=True, read_at=datetime.now(timezone.utc))
                )
                await db.commit()
                await PropertyInteractionsService.mark_comments_read(db, collection_id, property_id, is_agent=True)
        else:
            # Anonymous caller = the visitor reading their own showcase's comment thread.
            await PropertyInteractionsService.mark_comments_read(db, collection_id, property_id, is_agent=False)

        return comments

    except Exception as e:
        logger.error("getting property comments failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get property comments"
        )


@router.get("/{collection_id}/properties/{property_id}/interactions")
async def get_property_interaction_summary(
    collection_id: str,
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Get complete interaction summary for a property within a collection (its agent only)
    """
    if await _owner_id_if_owner(db, collection_id, current_user) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    try:
        summary = await PropertyInteractionsService.get_property_interaction_summary(
            db, collection_id, property_id
        )
        
        return summary
        
    except Exception as e:
        logger.error("getting property interaction summary failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get property interaction summary"
        )


@router.patch("/{collection_id}/share")
async def toggle_collection_share(
    collection_id: str,
    request: ShareToggleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Toggle collection share status (public/private) and manage share token
    """
    try:
        result = await CollectionsService.toggle_share_status(
            db, collection_id, current_user.id, request.make_public, request.force_regenerate
        )
        
        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=result["message"]
            )
        
        return {
            "success": True,
            "message": result["message"],
            "is_public": result["is_public"],
            "share_token": result["share_token"],
            "share_url": result["share_url"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("toggling collection share failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update collection sharing settings"
        )


SHOWCASE_PAUSED_DETAIL = "showcase_paused"


async def _assert_showcase_owner_active(db: AsyncSession, share_token: str) -> None:
    """A client's showcase link only works while its agent is on a live Showcase plan. After a
    downgrade to Basic or an ended subscription the page shows a friendly "paused - contact your
    agent" state (410 + SHOWCASE_PAUSED_DETAIL) instead of the homes; the data is kept, so it
    comes straight back if the agent upgrades/resubscribes."""
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    owner = (await db.execute(
        select(User).join(Collection, Collection.owner_id == User.id).where(share_match(share_token))
    )).scalar_one_or_none()
    if owner is not None and not owner_has_access(owner, SHOWCASE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=SHOWCASE_PAUSED_DETAIL)


@router.get("/shared/{share_token}")
async def get_shared_collection(
    share_token: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a shared collection by share token (no authentication required)
    """
    try:
        await _assert_showcase_owner_active(db, share_token)
        collection_data = await CollectionsService.get_shared_collection(
            db, share_token
        )
        
        if not collection_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found or not available for sharing"
            )
        
        return collection_data
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get shared collection"
        )


async def _notify_search_changed(db: AsyncSession, collection_obj: Collection, changed: list) -> None:
    """Bell notification for the agent when a client changes their own Showcase search. A burst of
    saves (they tweak price, then beds...) folds into the one unread notification from the last
    two hours instead of stacking up. The daily briefing email lists these too. Never raises."""
    try:
        from datetime import timedelta
        from sqlalchemy import select as _select
        now = datetime.now(timezone.utc)
        who = collection_obj.visitor_name or "Your client"
        message = " · ".join(f"{r['label']}: {r['value']}" for r in changed)[:500] or "They updated their search."
        recent = (await db.execute(_select(Notification).where(
            Notification.agent_id == collection_obj.owner_id, Notification.collection_id == collection_obj.id,
            Notification.type == "SHOWCASE_SEARCH_CHANGED", Notification.is_read.is_(False),
            Notification.created_at >= now - timedelta(hours=2)).limit(1))).scalar_one_or_none()
        if recent is not None:
            recent.message = message
            recent.created_at = now
        else:
            db.add(Notification(
                agent_id=collection_obj.owner_id, type="SHOWCASE_SEARCH_CHANGED", reference_type="COLLECTION",
                reference_id=collection_obj.id, title=f"{who} changed their Showcase search", message=message,
                collection_id=collection_obj.id, collection_name=collection_obj.name, visitor_name=collection_obj.visitor_name,
                link=f"/showcases?showcase={collection_obj.id}", is_read=False, created_at=now,
            ))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not notify search change: {e}")
        await db.rollback()


def _self_serve_payload(payload: dict) -> dict:
    """The rules for a Showcase visitor's own search change (save and live preview alike):
    - building it around a home makes that home the one the match % is measured against;
    - remove_home: no home at all any more - just their filters, no match %;
    - they can't draw, so a new address or new towns replace an area their agent drew on the map
      (a school district narrows it instead - districts narrow every area)."""
    payload = dict(payload)
    remove_home = payload.pop('remove_home', None)
    if not payload.get('compare_property_listing_key'):
        payload.pop('compare_property_listing_key', None)
    payload.pop('show_match_score', None)
    if payload.get('compare_property_listing_key'):
        payload['show_match_score'] = True
    elif remove_home:
        payload['show_match_score'] = False
        payload['compare_property_listing_key'] = None
    if payload.get('address') or payload.get('cities'):
        payload['search_polygon'] = None
    # Areas add up (a circle OR a township OR a county), so a buyer who picks "within N miles"
    # would otherwise still get every home in a township / county their agent had set - far
    # outside the circle they see. Their distance search replaces those.
    if payload.get('address'):
        payload.update(townships=[], counties=[], county=None)
    return payload


def _describe_search_change(payload: dict) -> list:
    """A client's own Showcase search edit -> [{"label", "value"}] rows for their lead's activity."""
    def money(v):
        return f"${v:,.0f}" if isinstance(v, (int, float)) else None

    def span(lo, hi, unit=""):
        if lo and hi:
            return f"{lo}-{hi}{unit}"
        if lo:
            return f"{lo}+{unit}"
        if hi:
            return f"Up to {hi}{unit}"
        return "Any"
    rows = []
    if payload.get("compare_property_listing_key"):
        rows.append({"label": "Built around", "value": str(payload.get("address") or "A different home")})
    elif payload.get("show_match_score") is False:
        rows.append({"label": "Built around", "value": "No home - just their filters"})
    if "min_price" in payload or "max_price" in payload:
        rows.append({"label": "Price", "value": span(money(payload.get("min_price")), money(payload.get("max_price")))})
    if "min_beds" in payload or "max_beds" in payload:
        rows.append({"label": "Bedrooms", "value": span(payload.get("min_beds"), payload.get("max_beds"))})
    if "min_baths" in payload or "max_baths" in payload:
        rows.append({"label": "Bathrooms", "value": span(payload.get("min_baths"), payload.get("max_baths"))})
    if payload.get("address"):
        rows.append({"label": "Near", "value": str(payload["address"])})
    if payload.get("diameter"):
        rows.append({"label": "Within", "value": f"{payload['diameter']:g} miles"})
    if "cities" in payload:
        rows.append({"label": "Towns", "value": ", ".join(payload.get("cities") or []) or "Any"})
    if "school_districts" in payload:
        rows.append({"label": "School districts", "value": ", ".join(payload.get("school_districts") or []) or "Any"})
    return rows


@router.put("/shared/{share_token}/update-preferences-and-refresh")
async def update_shared_collection_preferences(
    share_token: str,
    preferences_update: ShowcaseSelfServePreferencesUpdate,
    db: AsyncSession = Depends(get_db)
):
    """
    Let a showcase visitor tune their own basic search criteria (address, radius,
    price, beds/baths) and re-run the real property match - no authentication
    required, scoped to a public/active collection by its share token only (same
    resolution rule as get_shared_collection above, not owner auth).
    """
    try:
        await _assert_showcase_owner_active(db, share_token)
        result = await db.execute(
            select(Collection).options(joinedload(Collection.preferences)).where(
                and_(
                    share_match(share_token),
                    Collection.is_public == True,
                    Collection.status == "ACTIVE",
                )
            )
        )
        collection_obj = result.unique().scalar_one_or_none()

        if not collection_obj or not collection_obj.preferences:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Showcase not found or not available"
            )

        # Don't let a visitor wipe the location filter to nothing - check the
        # resulting state (payload field if set, else the existing value), not just
        # the payload alone, since a partial update (e.g. price-only) shouldn't be
        # rejected just because it didn't also resend the address/radius.
        #
        # Address itself is never required. A showcase can be entirely area-based (agent
        # picked cities/townships/school districts, no address at all - see
        # EditPreferencesModal's mutually-exclusive address-vs-area modes on the agent
        # side), and a visitor refining price/beds/baths on one of those shouldn't be
        # forced to invent an address just to save. Mirrors the agent's own rule exactly
        # (EditPreferencesModal.validateLocationPreferences): a radius is only required
        # alongside an address that's actually present, and SOME location method (address
        # or area filters) must exist - never let an edit leave the showcase with neither.
        payload = _self_serve_payload(preferences_update.dict(exclude_unset=True))
        effective_diameter = payload.get('diameter', collection_obj.preferences.diameter)
        effective_address = payload.get('address', collection_obj.preferences.address)
        # The resulting areas (what they're sending, else what's saved) - switching from "near the
        # home" to towns / school districts sends the new areas with an empty address. An area the
        # agent drew on the map counts too (the visitor can't draw, but can keep it).
        has_area_filters = any(
            payload.get(k, getattr(collection_obj.preferences, k, None))
            for k in ('cities', 'townships', 'school_districts', 'counties', 'search_polygon')
        )
        if effective_address and (not effective_diameter or effective_diameter <= 0):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A search radius is required when searching near an address"
            )
        if not effective_address and not has_area_filters:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This showcase needs either a search address or an area to search"
            )

        update_result = await CollectionPreferencesService.update_preferences_and_refresh_properties(
            db, collection_obj.id, CollectionPreferencesUpdate(**payload)
        )

        if not update_result['success']:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=update_result.get('error', 'Failed to update your search criteria')
            )

        # On their lead's activity: what they changed it to, in words.
        from app.services.leads_service import log_showcase_activity
        changed = _describe_search_change(payload)
        await log_showcase_activity(db, collection_obj.id, "showcase_search_changed", "Changed their Showcase search",
                                    raw_snapshot={"answers": changed} if changed else None)
        await _notify_search_changed(db, collection_obj, changed)

        collection_data = await CollectionsService.get_shared_collection(db, share_token)
        if not collection_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Showcase not found or not available"
            )
        return collection_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating shared collection preferences failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update your search criteria"
        )


@router.post("/shared/{share_token}/preview-matches")
async def preview_shared_collection_matches(
    share_token: str,
    preferences_update: ShowcaseSelfServePreferencesUpdate,
    db: AsyncSession = Depends(get_db)
):
    """The visitor's search windows, live: how many homes their unsaved changes would match, and
    where they are (for the dots on the distance picker). Nothing is saved - the same search the
    real update runs, on their saved criteria with these changes laid over them."""
    result = await db.execute(
        select(Collection).options(joinedload(Collection.preferences)).where(
            and_(share_match(share_token), Collection.is_public == True, Collection.status == "ACTIVE")
        )
    )
    collection_obj = result.unique().scalar_one_or_none()
    if not collection_obj or not collection_obj.preferences:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found or not available")
    try:
        saved = CollectionPreferencesBase.model_validate(collection_obj.preferences, from_attributes=True).model_dump()
        merged = CollectionPreferencesBase(**{**saved, **_self_serve_payload(preferences_update.model_dump(exclude_unset=True))})
        properties = await property_service.get_properties_by_preferences(db, merged, max_properties=200)
        count = await property_service.get_properties_count_by_preferences(db, merged)
        return {
            "count": count,
            "points": [
                {"id": p.listing_key, "lat": p.latitude, "lng": p.longitude}
                for p in properties if p.latitude is not None and p.longitude is not None
            ],
        }
    except Exception as e:
        logger.error("previewing shared showcase matches failed", extra={"error": str(e)})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to preview matches")


@router.patch("/shared/{share_token}/notifications")
async def update_shared_collection_notifications(
    share_token: str,
    request: NotificationSettingsSharedRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Update the visitor notification settings of a collection via share token
    """
    try:
        success = await CollectionsService.update_shared_collection_notifications(
            db, share_token, request.notify_visitor
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found"
            )
            
        return {
            "success": True,
            "message": "Notification settings updated successfully", 
            "notify_visitor": request.notify_visitor
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating shared collection notifications failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update notification settings"
        )


@router.get("/{collectionId}/properties")
async def get_properties_from_collection(
    collectionId: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """The agent's own Showcase homes (their Showcases page). Guests load homes through
    /shared/{share_token}, which honours a switched-off link and a paused plan - this used to answer
    anyone with the id, signed in or not."""
    if await _owner_id_if_owner(db, collectionId, current_user) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    try:
        return await CollectionsService.get_properties(
            collectionId,
            db,
            viewer_user_id=current_user.id
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get properties from collection"
        )

@router.put("/{collection_id}/update-preferences-and-refresh")
async def update_preferences_and_refresh(
    collection_id: str,
    preferences_update: CollectionPreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Atomically update collection preferences and refresh properties.
    Only commits changes if Zillow API succeeds. If Zillow fails, no changes are made.
    This prevents the collection from being left empty when property fetching fails.
    """
    try:
        # Verify user owns the collection
        collection = await db.execute(
            select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == current_user.id
            )
        )
        collection_obj = collection.scalar_one_or_none()

        if not collection_obj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found or access denied"
            )

        from app.services.showcase_digest_service import queue_search_changed, showcase_home_ids
        before_ids = await showcase_home_ids(db, collection_id)

        # Perform atomic update of preferences and properties
        result = await CollectionPreferencesService.update_preferences_and_refresh_properties(
            db, collection_id, preferences_update
        )

        if result['success']:
            # Their client hears about it in the 4:30 email: what the search is now, homes added / gone.
            await queue_search_changed(db, collection_id, before_ids)
            # They just saw what their own change brought in - no NEW for it on their Showcases page.
            from sqlalchemy import update as _update
            await db.execute(_update(Collection).where(Collection.id == collection_id).values(last_agent_dismissed_at=datetime.now(timezone.utc)))
            await db.commit()
            return {
                "success": True,
                "message": result['message'],
                "preferences_updated": result['preferences_updated'],
                "properties_refreshed": result['properties_refreshed'],
                "properties_count": result['properties_count'],
                "collection_id": collection_id,
                "preferences": result.get('preferences')
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.get('error', 'Failed to update preferences and refresh properties')
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating preferences and refreshing properties failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update preferences and refresh properties"
        )


@router.post("/{collection_id}/properties/{property_id}/schedule-tour", response_model=PropertyTourResponse)
async def schedule_property_tour(
    collection_id: str,
    property_id: str,
    tour_data: PropertyTourCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    owner_id = await _owner_id_if_owner(db, collection_id, current_user)
    if owner_id is None:
        from app.services.public_rate_limit import check_public_submit
        check_public_submit(request)  # bots can't flood the agent's leads/inbox

    """
    Schedule a tour for a property in a collection (public endpoint for visitors)
    """
    try:
        tour = await PropertyTourService.create_tour_request(
            db, collection_id, property_id, tour_data,
            user_id=owner_id
        )
        return tour

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error("scheduling property tour failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to schedule property tour"
        )


@router.get("/{collection_id}/busy")
async def showcase_agent_busy_times(collection_id: str, db: AsyncSession = Depends(get_db)):
    """The Showcase agent's busy blocks for the next 8 days (start/end only) - see
    calendar_sync_service.public_busy."""
    from app.models.database import Collection
    from app.services import calendar_sync_service
    owner_id = (await db.execute(select(Collection.owner_id).where(Collection.id == collection_id))).scalar_one_or_none()
    if not owner_id:
        return {"busy": []}
    return {"busy": await calendar_sync_service.public_busy(db, owner_id)}


@router.get("/{collection_id}/tours", response_model=List[PropertyTourResponse])
async def get_collection_tours(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Get all tour requests for a collection (agent only)
    """
    try:
        # Verify user owns the collection
        collection = await db.execute(
            select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == current_user.id
            )
        )
        collection_obj = collection.scalar_one_or_none()

        if not collection_obj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found or access denied"
            )

        # Actually viewing the tours list is what clears the tour-requested signal (not just
        # opening the showcase) - so the badge on the View Tours button / dashboard card
        # persists until the agent has a chance to see it.
        await db.execute(
            update(Notification)
            .where(
                Notification.agent_id == current_user.id,
                Notification.collection_id == collection_id,
                Notification.type == 'TOUR_REQUEST',
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=datetime.now(timezone.utc))
        )
        await db.commit()

        tours = await PropertyTourService.get_collection_tours(db, collection_id)
        return tours

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching collection tours failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collection tours"
        )


@router.get("/{collection_id}/tours/shared", response_model=List[PropertyTourResponse])
async def get_shared_collection_tours(
    collection_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get tour requests for a collection (public endpoint for the visitor's own showcase) - lets
    the "Your Upcoming Tours" section show confirmed tours without requiring the agent's login.
    """
    try:
        collection = await db.execute(
            select(Collection).where(
                Collection.id == collection_id,
                Collection.is_public == True
            )
        )
        if not collection.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found"
            )

        tours = await PropertyTourService.get_collection_tours(db, collection_id)
        return tours

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching shared collection tours failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collection tours"
        )


@router.patch("/tours/{tour_id}/completion", response_model=PropertyTourResponse)
async def update_tour_completion(
    tour_id: str,
    completion_update: PropertyTourCompletionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Update the completion status of a tour request (agent only)
    """
    try:
        tour = await PropertyTourService.update_tour_completion(
            db, tour_id, completion_update, current_user.id
        )

        if not tour:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tour request not found"
            )

        return tour

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating tour completion failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update tour completion status"
        )


@router.patch("/tours/{tour_id}/confirm", response_model=PropertyTourResponse)
async def confirm_tour_request(
    tour_id: str,
    confirm_update: PropertyTourConfirmUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Confirm a tour request with the agreed date/time (agent only) - this is what surfaces it
    to the visitor as an "Upcoming Tour" on their showcase.
    """
    try:
        tour = await PropertyTourService.confirm_tour(
            db, tour_id, confirm_update, current_user.id
        )

        if not tour:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tour request not found"
            )

        return tour

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("confirming tour request failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to confirm tour request"
        )


@router.patch("/tours/{tour_id}/unconfirm", response_model=PropertyTourResponse)
async def unconfirm_tour_request(
    tour_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Clear a tour request's confirmation (agent only).
    """
    try:
        tour = await PropertyTourService.unconfirm_tour(db, tour_id, current_user.id)

        if not tour:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tour request not found"
            )

        return tour

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("unconfirming tour request failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unconfirm tour request"
        )
