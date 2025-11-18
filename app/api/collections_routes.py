from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, timezone
import httpx
import os

from app.database import get_db
from app.schemas.collection import CollectionCreate, CollectionResponse
from app.schemas.collection_preferences import CollectionPreferencesCreate, CollectionPreferencesUpdate
from app.schemas.property_interactions import (
    PropertyInteractionUpdate,
    PropertyCommentCreate,
    PropertyInteractionResponse,
    PropertyCommentResponse,
    PropertyInteractionStats,
    PropertyInteractionSummary
)
from app.schemas.property_tour import (
    PropertyTourCreate,
    PropertyTourResponse,
    PropertyTourStatusUpdate
)
from app.services.collections_service import CollectionsService
from app.services.property_interactions_service import PropertyInteractionsService
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.property_sync_service import PropertySyncService
from app.services.zillow_service import ZillowService
from app.services.property_tour_service import PropertyTourService
from app.utils.auth import get_current_active_user, get_current_user_optional, require_premium_plan
from app.models.database import User, Collection
from sqlalchemy import select
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
    address: str
    diameter: float = 0

    # Home type preferences
    is_town_house: Optional[bool] = False
    is_lot_land: Optional[bool] = False
    is_condo: Optional[bool] = False
    is_multi_family: Optional[bool] = False
    is_single_family: Optional[bool] = False
    is_apartment: Optional[bool] = False


class UpdateStatusRequest(BaseModel):
    status: str


class ShareToggleRequest(BaseModel):
    make_public: bool
    force_regenerate: bool = False


@router.get("/", response_model=List[CollectionResponse])
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


@router.get("/{collection_id}", response_model=CollectionResponse)
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


@router.post("/", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
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


@router.post("/create-manually", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
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
        zillow_service = ZillowService()
        latitude = 0
        longitude = 0
        if len(request.address) > 0:
            try:
                property_details = await zillow_service.get_property_by_address(request.address)
                latitude = property_details.latitude
                longitude = property_details.longitude

                if not latitude or not longitude:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
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
            description=f"Properties for {request.visitor_name} near {request.address}",
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
            cities=request.cities,
            townships=request.townships,
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
            special_features=request.additional_comments,
            visiting_reason=request.visiting_reason,
            has_agent=request.has_agent
        )
        
        await CollectionPreferencesService.create_preferences(db, preferences_data)
        
        # Populate collection with properties immediately after creation
        try:
            sync_service = PropertySyncService()
            population_result = await sync_service.populate_new_collection(db, collection.id)
            
            if population_result['success']:
                pass
            else:
                pass
        except Exception as e:
            # Continue - collection creation should succeed even if population fails
            pass

        collections = await CollectionsService.get_user_collections(db, current_user.id)
        
        if collections:
            latest_collection = max(collections, key=lambda x: x['created_at'])
            return latest_collection
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Collection created but could not retrieve"
            )
        
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

        # Check active collection limit when trying to activate
        if request.status == 'ACTIVE':
            can_activate = await CollectionsService.can_activate_collection(
                db, current_user.id, collection_id
            )
            if not can_activate:
                active_count = await CollectionsService.count_active_collections(db, current_user.id)
                max_active = int(os.getenv("MAX_ACTIVE_COLLECTIONS_PER_USER", "10"))
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot activate collection. You have reached the maximum of {max_active} active collections (currently {active_count}). Please deactivate another collection first."
                )

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
    Update user interaction with a property within a collection (like, dislike, favorite)
    For both authenticated users (agents) and anonymous visitors (shared collections)
    """
    try:
        if interaction_data.interaction_type and interaction_data.value is not None:
            if interaction_data.interaction_type == 'like':
                interaction_data.liked = interaction_data.value
            elif interaction_data.interaction_type == 'dislike':
                interaction_data.disliked = interaction_data.value
            elif interaction_data.interaction_type == 'favorite':
                interaction_data.favorited = interaction_data.value

        interaction = await PropertyInteractionsService.create_property_interaction(
            db, collection_id, property_id, interaction_data,
            user_id=current_user.id if current_user else None
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
            db, collection_id, property_id
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
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Add a comment to a property within a collection
    Supports both authenticated users and anonymous visitors
    """
    try:
        # Handle alternative frontend format
        if comment_data.comment and not comment_data.content:
            comment_data.content = comment_data.comment
        
        # Create anonymous comment - no user identification required
        comment = await PropertyInteractionsService.add_property_comment(
            db, collection_id, property_id, comment_data,
            user_id=current_user.id if current_user else None
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
    db: AsyncSession = Depends(get_db)
):
    """
    Get all comments for a property within a collection
    Public endpoint for shared collections
    """
    try:
        comments = await PropertyInteractionsService.get_property_comments(
            db, collection_id, property_id
        )
        
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
    Get complete interaction summary for a property within a collection
    """
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


@router.get("/shared/{share_token}")
async def get_shared_collection(
    share_token: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a shared collection by share token (no authentication required)
    """
    try:
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

@router.get("/{collectionId}/properties")
async def get_properties_from_collection(
    collectionId: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    try:
        return await CollectionsService.get_properties(
            collectionId,
            db
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

        # Perform atomic update of preferences and properties
        result = await CollectionPreferencesService.update_preferences_and_refresh_properties(
            db, collection_id, preferences_update
        )

        if result['success']:
            return {
                "success": True,
                "message": result['message'],
                "preferences_updated": result['preferences_updated'],
                "properties_refreshed": result['properties_refreshed'],
                "properties_count": result['properties_count'],
                "collection_id": collection_id
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
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Schedule a tour for a property in a collection (public endpoint for visitors)
    """
    try:
        tour = await PropertyTourService.create_tour_request(
            db, collection_id, property_id, tour_data,
            user_id=current_user.id if current_user else None
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


@router.patch("/tours/{tour_id}/status", response_model=PropertyTourResponse)
async def update_tour_status(
    tour_id: str,
    status_update: PropertyTourStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_premium_plan)
):
    """
    Update the status of a tour request (agent only)
    """
    try:
        tour = await PropertyTourService.update_tour_status(
            db, tour_id, status_update, current_user.id
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
        logger.error("updating tour status failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update tour status"
        )
