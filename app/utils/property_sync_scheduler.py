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

from app.services.property_sync_service import PropertySyncService
from app.config.logging import get_logger

logger = get_logger(__name__)

async def run_global_property_sync() -> Dict[str, Any]:
    """
    Triggers the high-efficiency global incremental property sync.
    Fetches all changes from Bright MLS and propagates to collections.
    """
    logger.info("Global property sync triggered", extra={"event": "global_sync_started"})

    sync_results = {
        'started_at': datetime.now(timezone.utc),
        'properties_updated': 0,
        'notifications_sent': 0,
        'errors': 0,
        'success': True
    }

    try:
        property_sync_service = PropertySyncService()
        
        # Execute the new global sync logic
        results = await property_sync_service.run_global_sync()
        
        sync_results['properties_updated'] = results.get('updated', 0)
        sync_results['notifications_sent'] = results.get('notifications_sent', 0)
        sync_results['errors'] = results.get('errors', 0)

        sync_results['completed_at'] = datetime.now(timezone.utc)
        sync_results['duration_seconds'] = (
            sync_results['completed_at'] - sync_results['started_at']
        ).total_seconds()

        await discord_message(sync_results)

        logger.info(
            "Global property sync completed",
            extra={
                "event": "global_sync_completed",
                "properties_updated": sync_results['properties_updated'],
                "notifications_sent": sync_results['notifications_sent'],
                "duration_seconds": round(sync_results['duration_seconds'], 2)
            }
        )

    except Exception as e:
        logger.error("Global property sync critical failure", exc_info=True, extra={"error": str(e)})
        sync_results['success'] = False

    return sync_results


async def scheduled_property_sync():
    """
    Entry point function to be called by APScheduler.
    """
    if not os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
        logger.info("Property sync is disabled", extra={"event": "property_sync_disabled"})
        return

    try:
        await run_global_property_sync()
    except Exception as e:
        logger.error("Scheduled property sync failed", exc_info=True, extra={"error": str(e)})


async def discord_message(sync_results):
    discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")

    try:
        if not discord_webhook_url:
            return

        emoji = "🚀" if sync_results['success'] else "❌"
        status = "Success" if sync_results['success'] else "Failed"

        message_lines = [
            f"{emoji} **Global Property Sync {status}**",
            f"",
            f"**Properties Updated:** {sync_results['properties_updated']}",
            f"**Notifications Sent:** {sync_results['notifications_sent']}",
            f"**Duration:** {sync_results['duration_seconds']:.1f}s",
        ]

        if sync_results['errors'] > 0:
            message_lines.append(f"**Error Batches:** {sync_results['errors']}")

        content = "\n".join(message_lines)

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(discord_webhook_url, json={"content": content})

    except Exception as e:
        logger.error(f"Failed to send Discord webhook: {str(e)}")


if __name__ == "__main__":
    """For testing"""
    asyncio.run(run_global_property_sync())
