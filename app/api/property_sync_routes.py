from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any

from app.database import get_db
from app.services.property_sync_service import PropertySyncService

router = APIRouter(prefix="/property-sync", tags=["property-sync"])

@router.post("/sync-all")
async def sync_all_collections(background_tasks: BackgroundTasks):
    """
    Manually trigger property sync for all active collections
    This runs as a background task to avoid request timeouts
    """
    sync_service = PropertySyncService()
    
    # Add the sync task to background tasks
    background_tasks.add_task(sync_service.sync_all_active_collections)
    
    return {
        "message": "Property sync started for all active collections",
        "status": "started"
    }

@router.post("/sync-collection/{collection_id}")
async def sync_single_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Manually trigger property sync for a specific collection
    """
    sync_service = PropertySyncService()
    
    try:
        result = await sync_service.sync_single_collection(collection_id)
        
        if result['success']:
            return {
                "message": f"Successfully synced collection {collection_id}",
                "new_properties_added": result['new_properties_added'],
                "synced_at": result['synced_at']
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result['error']
            )
            
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync collection: {str(e)}"
        )

@router.get("/status")
async def get_sync_status():
    """
    Get the status of the property sync system
    """
    # This could be expanded to show last sync time, active sync jobs, etc.
    return {
        "message": "Property sync system is operational",
        "zillow_api_configured": bool(PropertySyncService().zillow_service.api_key),
        "status": "ready"
    }