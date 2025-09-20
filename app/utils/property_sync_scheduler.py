import asyncio
import os
from datetime import datetime
from typing import Dict, Any

from app.database import AsyncSessionLocal
from app.services.property_sync_service import PropertySyncService


async def sync_all_collections_with_rate_limit() -> Dict[str, Any]:
    """
    Wrapper function that uses existing PropertySyncService with rate limiting
    This will be called by APScheduler every 5 hours
    """
    print(f"[PROPERTY_SYNC] Starting property sync at {datetime.utcnow()}")

    # Get configuration from environment
    rate_limit_delay = float(os.getenv("API_RATE_LIMIT_DELAY_SECONDS", "0.5"))  # 0.5s = 2 req/sec
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

            print(f"[PROPERTY_SYNC] Found {len(collections_with_preferences)} active collections with preferences")

            # Apply max collection limit if set
            if max_collections_per_sync > 0 and len(collections_with_preferences) > max_collections_per_sync:
                print(f"[PROPERTY_SYNC] Limiting to {max_collections_per_sync} collections per sync")
                collections_with_preferences = collections_with_preferences[:max_collections_per_sync]
                sync_results['collections_skipped'] = sync_results['collections_found'] - len(collections_with_preferences)

            # Process each collection using your existing method
            for i, (collection, preferences) in enumerate(collections_with_preferences, 1):
                try:
                    print(f"[PROPERTY_SYNC] Processing collection {i}/{len(collections_with_preferences)}: {collection.name}")

                    # Use your existing sync_collection_properties method
                    new_properties = await property_sync_service.sync_collection_properties(
                        db, collection, preferences
                    )

                    sync_results['total_new_properties'] += new_properties
                    sync_results['collections_processed'] += 1

                    print(f"[PROPERTY_SYNC] Added {new_properties} new properties to collection '{collection.name}'")

                    # Rate limiting: wait between collections to respect API limits
                    if i < len(collections_with_preferences):  # Don't wait after the last collection
                        print(f"[PROPERTY_SYNC] Rate limiting: waiting {rate_limit_delay}s before next collection")
                        await asyncio.sleep(rate_limit_delay)

                except Exception as e:
                    error_msg = f"Failed to sync collection {collection.name}: {str(e)}"
                    print(f"[PROPERTY_SYNC] ERROR: {error_msg}")
                    sync_results['errors'].append(error_msg)

        sync_results['completed_at'] = datetime.utcnow()
        sync_results['duration_seconds'] = (
            sync_results['completed_at'] - sync_results['started_at']
        ).total_seconds()

        print(f"[PROPERTY_SYNC] Completed! Processed {sync_results['collections_processed']} collections, "
              f"added {sync_results['total_new_properties']} new properties in "
              f"{sync_results['duration_seconds']:.1f}s")

    except Exception as e:
        error_msg = f"Critical error during property sync: {str(e)}"
        print(f"[PROPERTY_SYNC] CRITICAL ERROR: {error_msg}")
        sync_results['success'] = False
        sync_results['errors'].append(error_msg)

    return sync_results


async def scheduled_property_sync():
    """
    Entry point function to be called by APScheduler
    Checks if property sync is enabled before running
    """
    if not os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
        print("[PROPERTY_SYNC] Property sync is disabled (PROPERTY_SYNC_ENABLED=false)")
        return

    try:
        result = await sync_all_collections_with_rate_limit()

        if result['success']:
            print(f"[PROPERTY_SYNC] ✅ Sync completed successfully - "
                  f"{result['collections_processed']} collections, "
                  f"{result['total_new_properties']} new properties")
        else:
            print(f"[PROPERTY_SYNC] ❌ Sync completed with errors: {result['errors']}")

    except Exception as e:
        print(f"[PROPERTY_SYNC] 💥 Critical error in scheduled sync: {str(e)}")


async def list_all_collections():
    """
    Utility function to list all active collections with preferences
    Useful for debugging and monitoring
    """
    try:
        property_sync_service = PropertySyncService()

        async with AsyncSessionLocal() as db:
            collections = await property_sync_service.get_active_collections_with_preferences(db)

        print(f"\n📋 Found {len(collections)} active collections with preferences:")
        print("-" * 70)

        for i, (collection, preferences) in enumerate(collections, 1):
            print(f"{i:2d}. {collection.name:<35} | Status: {collection.status}")
            if preferences:
                location = f"{preferences.city}, {preferences.state}" if preferences.city and preferences.state else "No location set"
                print(f"     └─ Location: {location}")
                print(f"     └─ Price range: ${preferences.min_price:,} - ${preferences.max_price:,}")

        print("-" * 70)
        print(f"Total: {len(collections)} collections ready for sync")
        return collections

    except Exception as e:
        print(f"❌ Error listing collections: {str(e)}")
        return []


if __name__ == "__main__":
    """For testing - run this script directly to list collections"""
    print("🔍 Testing property sync scheduler...")
    asyncio.run(list_all_collections())