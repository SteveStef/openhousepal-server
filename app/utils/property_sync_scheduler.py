import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Any
from pathlib import Path
import httpx

# Add server directory to Python path so script can be run from anywhere
server_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(server_dir))

from app.database import AsyncSessionLocal
from app.services.property_sync_service import PropertySyncService
from app.config.logging import get_logger

logger = get_logger(__name__)


async def sync_all_collections_with_rate_limit() -> Dict[str, Any]:
    """
    Batch-based property sync that processes N oldest collections per run.
    Uses last_synced_at to determine which collections need syncing.
    Called by APScheduler every X minutes (default: 60).
    """
    logger.info("Property sync started", extra={"event": "property_sync_started"})

    # Get configuration from environment
    max_collections_per_sync = int(os.getenv("MAX_COLLECTIONS_PER_SYNC", "10"))  # Default: 10 collections per run

    sync_results = {
        'started_at': datetime.now(timezone.utc),
        'collections_found': 0,
        'collections_processed': 0,
        'total_new_properties': 0,
        'errors': [],
        'success': True
    }

    try:
        # Use existing PropertySyncService
        property_sync_service = PropertySyncService()

        async with AsyncSessionLocal() as db:
            # Get N oldest collections (ordered by last_synced_at, NULL first)
            # This automatically limits to max_collections_per_sync
            collections_with_preferences = await property_sync_service.get_active_collections_with_preferences(
                db,
                max_collections=max_collections_per_sync
            )
            sync_results['collections_found'] = len(collections_with_preferences)

            # Process each collection
            for i, (collection, preferences) in enumerate(collections_with_preferences, 1):
                try:
                    logger.info(
                        f"Syncing collection {i}/{len(collections_with_preferences)}",
                        extra={
                            "collection_id": collection.id,
                            "collection_name": collection.name,
                            "last_synced_at": collection.last_synced_at.isoformat() if collection.last_synced_at else "Never"
                        }
                    )

                    # Sync collection (this also updates last_synced_at)
                    sync_result = await property_sync_service.sync_collection_properties(
                        db, collection, preferences
                    )

                    sync_results['total_new_properties'] += sync_result['new_properties_count']
                    sync_results['collections_processed'] += 1

                except Exception as e:
                    error_msg = f"Failed to sync collection {collection.name}: {str(e)}"
                    logger.error("Collection sync failed", extra={"collection_name": collection.name, "error": str(e)})
                    sync_results['errors'].append(error_msg)

        sync_results['completed_at'] = datetime.now(timezone.utc)
        sync_results['duration_seconds'] = (
            sync_results['completed_at'] - sync_results['started_at']
        ).total_seconds()

        await discord_message(sync_results)

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


async def discord_message(sync_results):
    discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")

    try:
        if not discord_webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL not configured, skipping Discord notification")
            return

        emoji = "✅" if sync_results['success'] and not sync_results['errors'] else "⚠️" if sync_results['errors'] else "❌"
        status = "Completed successfully" if sync_results['success'] and not sync_results['errors'] else "Completed with errors" if sync_results['errors'] else "Failed"

        message_lines = [
            f"{emoji} **Property Sync {status}**",
            f"",
            f"**Collections Processed:** {sync_results['collections_processed']}",
            f"**New Properties Added:** {sync_results['total_new_properties']}",
            f"**Duration:** {sync_results['duration_seconds']:.1f}s",
        ]

        # Add errors if any
        if sync_results['errors']:
            message_lines.append(f"")
            message_lines.append(f"**Errors ({len(sync_results['errors'])}):**")
            for error in sync_results['errors'][:5]:  # Limit to first 5 errors
                message_lines.append(f"• {error}")
            if len(sync_results['errors']) > 5:
                message_lines.append(f"• ... and {len(sync_results['errors']) - 5} more errors")

        # Build message and send (THIS IS OUTSIDE THE ERROR LOOP)
        content = "\n".join(message_lines)

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                discord_webhook_url,
                json={"content": content}
            )

            if response.status_code in [200, 204]:
                logger.info("Discord webhook sent successfully")
            else:
                logger.error(f"Discord webhook failed: {response.status_code} - {response.text}")

    except Exception as e:
        logger.error(f"Failed to send Discord webhook: {str(e)}", exc_info=True)


if __name__ == "__main__":
    """For testing - run this script directly to list collections"""
    asyncio.run(sync_all_collections_with_rate_limit())

