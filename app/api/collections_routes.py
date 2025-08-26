from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.schemas.collection import CollectionCreate, CollectionResponse
from app.schemas.property_interactions import (
    PropertyInteractionUpdate,
    PropertyCommentCreate,
    PropertyInteractionResponse,
    PropertyCommentResponse,
    PropertyInteractionStats,
    PropertyInteractionSummary
)
from app.services.collections_service import CollectionsService
from app.services.property_interactions_service import PropertyInteractionsService
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.open_house_service import OpenHouseService
from app.utils.auth import get_current_active_user
from app.models.database import User

router = APIRouter(prefix="/collections", tags=["collections"])


class CreateFromAddressRequest(BaseModel):
    name: str
    address: str
    visitor_name: str
    visitor_email: str
    visitor_phone: str
    visiting_reason: str
    timeframe: str
    has_agent: str
    additional_comments: str = ""
    interested_in_similar: bool = False


class UpdateStatusRequest(BaseModel):
    status: str


class ShareToggleRequest(BaseModel):
    make_public: bool
    force_regenerate: bool = False


@router.get("/", response_model=List[CollectionResponse])
async def get_all_collections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get all collections for the authenticated user
    """
    try:
        collections = await CollectionsService.get_user_collections(db, current_user.id)
        return collections
        
    except Exception as e:
        print(f"Error fetching collections: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collections"
        )


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
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
        print(f"Error fetching collection: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collection"
        )


@router.post("/", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(
    collection_data: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Create a new collection
    """
    try:
        collection = await CollectionsService.create_collection(db, collection_data, current_user.id)
        
        # Auto-generate preferences if collection has an original property
        if collection.get("original_property_id"):
            try:
                await CollectionPreferencesService.auto_generate_preferences(db, collection["id"])
            except Exception as e:
                print(f"Warning: Failed to auto-generate preferences for collection {collection['id']}: {e}")
        
        return collection
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        print(f"Error creating collection: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create collection"
        )


@router.post("/create-from-address", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection_from_address(
    request: CreateFromAddressRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Create a new collection by looking up property data from address
    """
    try:
        # First, fetch property data from the address using Zillow API
        import httpx
        import os
        
        RAPID_API_KEY = os.getenv("RAPID_API_KEY")
        if not RAPID_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Property lookup service not configured"
            )
        
        headers = {
            'x-rapidapi-key': RAPID_API_KEY,
            'x-rapidapi-host': "zillow56.p.rapidapi.com"
        }
        
        params = {"address": request.address}
        url = "https://zillow56.p.rapidapi.com/search_address"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params, timeout=30.0)
            if response.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Property not found for the provided address"
                )
            elif response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to fetch property data"
                )
            
            property_data = response.json()
        
        # Create OpenHouseFormSubmission-like object to reuse existing collection creation logic
        from app.schemas.open_house import OpenHouseFormSubmission
        
        # Extract property ID from Zillow response
        property_id = str(property_data.get("zpid") or property_data.get("id", ""))
        if not property_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Property ID not found in API response"
            )
        
        # Store property data in database for collection creation
        from app.models.database import Property
        from sqlalchemy import select
        
        stmt = select(Property).where(Property.id == property_id)
        result = await db.execute(stmt)
        existing_property = result.scalar_one_or_none()
        
        if existing_property:
            # Update existing property
            existing_property.street_address = request.address
            existing_property.zillow_data = property_data
            existing_property.updated_at = datetime.utcnow()
        else:
            # Create new property
            property_record = Property(
                id=property_id,
                street_address=request.address,
                zillow_data=property_data,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(property_record)
        
        await db.commit()
        
        # Create open house form submission
        from app.schemas.open_house import OpenHouseFormSubmission
        form_submission = OpenHouseFormSubmission(
            full_name=request.visitor_name,
            email=request.visitor_email,
            phone=request.visitor_phone,
            visiting_reason=request.visiting_reason,
            timeframe=request.timeframe,
            has_agent=request.has_agent,
            interested_in_similar=request.interested_in_similar,
            property_id=property_id,
            agent_id=current_user.id
        )
        
        # Create visitor record and collection using the open house service
        visitor = await OpenHouseService.create_visitor(db, form_submission)
        collection_created = await OpenHouseService.create_collection_for_visitor(db, visitor, form_submission)
        
        if not collection_created:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create collection"
            )
        
        # Get the created collection to return
        collections = await CollectionsService.get_user_collections(db, current_user.id)
        
        # Return the most recently created collection
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
        print(f"Error creating collection from address: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create collection"
        )


@router.patch("/{collection_id}/status")
async def update_collection_status(
    collection_id: str,
    request: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
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
        print(f"Error updating collection status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update collection status"
        )


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
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
        print(f"Error deleting collection: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete collection"
        )


@router.post("/{collection_id}/properties/{property_id}/interact")
async def update_property_interaction(
    collection_id: str,
    property_id: str,
    interaction_data: PropertyInteractionUpdate,
    db: AsyncSession = Depends(get_db)
):
    """
    Update user interaction with a property within a collection (like, dislike, favorite)
    Supports both authenticated users and anonymous visitors
    """
    try:
        # Try to get current user, but don't require authentication for shared collections
        current_user = None
        try:
            from fastapi import Request
            from app.utils.auth import get_current_active_user
            # This is a simplified approach - in practice you'd need request context
            pass  # For now, handle via visitor_email
        except:
            pass  # Anonymous access allowed
        
        # Handle alternative frontend format
        if interaction_data.interaction_type and interaction_data.value is not None:
            if interaction_data.interaction_type == 'like':
                interaction_data.liked = interaction_data.value
            elif interaction_data.interaction_type == 'dislike':
                interaction_data.disliked = interaction_data.value
            elif interaction_data.interaction_type == 'favorite':
                interaction_data.favorited = interaction_data.value
        
        # For shared collections, use visitor_email identification
        if interaction_data.visitor_email:
            interaction = await PropertyInteractionsService.update_property_interaction(
                db, collection_id, property_id, 
                visitor_email=interaction_data.visitor_email, 
                interaction_data=interaction_data
            )
        else:
            # This would be for authenticated users - for now require visitor_email
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="visitor_email is required for property interactions"
            )
        
        return {
            "success": True,
            "interaction": interaction
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating property interaction: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update property interaction"
        )


@router.post("/{collection_id}/properties/{property_id}/comments")
async def add_property_comment(
    collection_id: str,
    property_id: str,
    comment_data: PropertyCommentCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Add a comment to a property within a collection
    Supports both authenticated users and anonymous visitors
    """
    try:
        # Handle alternative frontend format
        if comment_data.comment and not comment_data.content:
            comment_data.content = comment_data.comment
        
        # For shared collections, use visitor_email identification
        if comment_data.visitor_email:
            comment = await PropertyInteractionsService.add_property_comment(
                db, collection_id, property_id, comment_data,
                visitor_email=comment_data.visitor_email,
                visitor_name=comment_data.visitor_name
            )
        else:
            # This would be for authenticated users - for now require visitor_email
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="visitor_email is required for property comments"
            )
        
        return {
            "success": True,
            "comment": comment
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error adding property comment: {e}")
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
        print(f"Error getting property comments: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get property comments"
        )


@router.get("/{collection_id}/properties/{property_id}/interactions")
async def get_property_interaction_summary(
    collection_id: str,
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get complete interaction summary for a property within a collection
    """
    try:
        summary = await PropertyInteractionsService.get_property_interaction_summary(
            db, collection_id, property_id, user_id=current_user.id
        )
        
        return summary
        
    except Exception as e:
        print(f"Error getting property interaction summary: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get property interaction summary"
        )


@router.patch("/{collection_id}/share")
async def toggle_collection_share(
    collection_id: str,
    request: ShareToggleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
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
        print(f"Error toggling collection share: {e}")
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
        print(f"[DEBUG] Getting shared collection for token: {share_token}")
        collection_data = await CollectionsService.get_shared_collection(
            db, share_token
        )
        
        if not collection_data:
            print(f"[DEBUG] No collection found for token: {share_token}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Collection not found or not available for sharing"
            )
        
        print(f"[DEBUG] Found collection: {collection_data.get('id')} - {collection_data.get('name')}")
        return collection_data
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error getting shared collection: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get shared collection"
        )