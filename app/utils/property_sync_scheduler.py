import asyncio
import os
import sys
from datetime import datetime
from typing import Dict, Any
from pathlib import Path

# Add server directory to Python path so script can be run from anywhere
server_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(server_dir))

from app.database import AsyncSessionLocal
from app.services.property_sync_service import PropertySyncService
from app.config.logging import get_logger

logger = get_logger(__name__)


async def sync_all_collections_with_rate_limit() -> Dict[str, Any]:
    """
    Wrapper function that uses existing PropertySyncService with rate limiting
    This will be called by APScheduler every 5 hours
    """
    logger.info("Property sync started", extra={"event": "property_sync_started"})

    # Get configuration from environment
    max_collections_per_sync = int(os.getenv("MAX_COLLECTIONS_PER_SYNC", "0"))  # 0 = no limit

    sync_results = {
        'started_at': datetime.utcnow(),
        'collections_found': 0,
        'collections_processed': 0,
        'collections_skipped': 0,
        'total_new_properties': 0,
        'errors': [],
        'success': True
    }

    try:
        # Use your existing PropertySyncService
        property_sync_service = PropertySyncService()

        async with AsyncSessionLocal() as db:
            # Use your existing method to get active collections with preferences
            collections_with_preferences = await property_sync_service.get_active_collections_with_preferences(db)
            sync_results['collections_found'] = len(collections_with_preferences)

            # Apply max collection limit if set
            if max_collections_per_sync > 0 and len(collections_with_preferences) > max_collections_per_sync:
                collections_with_preferences = collections_with_preferences[:max_collections_per_sync]
                sync_results['collections_skipped'] = sync_results['collections_found'] - len(collections_with_preferences)

            # Process each collection using your existing method
            for i, (collection, preferences) in enumerate(collections_with_preferences, 1):
                try:
                    # Use your existing sync_collection_properties method
                    sync_result = await property_sync_service.sync_collection_properties(
                        db, collection, preferences
                    )

                    sync_results['total_new_properties'] += sync_result['new_properties_count']
                    sync_results['collections_processed'] += 1

                except Exception as e:
                    error_msg = f"Failed to sync collection {collection.name}: {str(e)}"
                    logger.error("Collection sync failed", extra={"collection_name": collection.name, "error": str(e)})
                    sync_results['errors'].append(error_msg)

        sync_results['completed_at'] = datetime.utcnow()
        sync_results['duration_seconds'] = (
            sync_results['completed_at'] - sync_results['started_at']
        ).total_seconds()

        logger.info(
            "Property sync completed",
            extra={
                "event": "property_sync_completed",
                "collections_processed": sync_results['collections_processed'],
                "new_properties_added": sync_results['total_new_properties'],
                "duration_seconds": round(sync_results['duration_seconds'], 2),
                "errors_count": len(sync_results['errors'])
            }
        )

    except Exception as e:
        error_msg = f"Critical error during property sync: {str(e)}"
        logger.error("Property sync critical failure", exc_info=True, extra={"error": error_msg})
        sync_results['success'] = False
        sync_results['errors'].append(error_msg)

    return sync_results


async def scheduled_property_sync():
    """
    Entry point function to be called by APScheduler
    Checks if property sync is enabled before running
    """
    if not os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
        logger.info("Property sync is disabled", extra={"event": "property_sync_disabled"})
        return

    try:
        await sync_all_collections_with_rate_limit()
    except Exception as e:
        logger.error("Scheduled property sync failed", exc_info=True, extra={"error": str(e)})


async def list_all_collections():
    """
    Utility function to list all active collections with preferences
    Useful for debugging and monitoring
    """
    try:
        property_sync_service = PropertySyncService()

        async with AsyncSessionLocal() as db:
            collections = await property_sync_service.get_active_collections_with_preferences(db)


        for i, (collection, preferences) in enumerate(collections, 1):
            if preferences:
                # Display location info
                location = preferences.address if preferences.address else "No location set"

                # Display cities if available (cities is a JSON array)
                if preferences.cities:
                    cities_str = ', '.join(preferences.cities) if isinstance(preferences.cities, list) else str(preferences.cities)

                # Display price range
                min_price = preferences.min_price if preferences.min_price else 0
                max_price = preferences.max_price if preferences.max_price else 0

        return collections

    except Exception as e:
        return []


if __name__ == "__main__":
    """For testing - run this script directly to list collections"""
    asyncio.run(list_all_collections())

