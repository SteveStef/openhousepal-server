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

async def run_global_property_sync(max_pages: int | None = None) -> Dict[str, Any]:
    """
    Triggers the high-efficiency global incremental property sync.
    Fetches all changes from Bright MLS and propagates to collections.
    """
    logger.info("Global property sync triggered", extra={"event": "global_sync_started", "max_pages": max_pages})

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
        results = await property_sync_service.run_global_sync(max_pages=max_pages)
        
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


async def run_open_house_sync(max_pages: int | None = None) -> Dict[str, Any]:
    """
    Triggers the incremental sync of Bright's open house schedule (BrightOpenHouses) into
    Property.mls_open_house_* fields - the Discovery date/time fallback for listings nobody has
    hosted inside OpenHousePal.
    """
    logger.info("Open house sync triggered", extra={"event": "open_house_sync_started", "max_pages": max_pages})

    sync_results = {
        'started_at': datetime.now(timezone.utc),
        'listings_updated': 0,
        'errors': 0,
        'success': True
    }

    try:
        property_sync_service = PropertySyncService()
        results = await property_sync_service.run_open_house_sync(max_pages=max_pages)

        sync_results['listings_updated'] = results.get('updated', 0)
        sync_results['errors'] = results.get('errors', 0)
        sync_results['completed_at'] = datetime.now(timezone.utc)
        sync_results['duration_seconds'] = (
            sync_results['completed_at'] - sync_results['started_at']
        ).total_seconds()

        logger.info(
            "Open house sync completed",
            extra={
                "event": "open_house_sync_completed",
                "listings_updated": sync_results['listings_updated'],
                "duration_seconds": round(sync_results['duration_seconds'], 2)
            }
        )
    except Exception as e:
        logger.error("Open house sync critical failure", exc_info=True, extra={"error": str(e)})
        sync_results['success'] = False

    return sync_results


async def scheduled_open_house_sync():
    """
    Entry point function to be called by APScheduler. Gated by the same flag as the main
    property sync - open house scheduling is disabled in whatever environment property sync is.
    """
    if not os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
        return

    try:
        await run_open_house_sync()
    except Exception as e:
        logger.error("Scheduled open house sync failed", exc_info=True, extra={"error": str(e)})


async def run_closed_property_sync(max_pages: int | None = None) -> Dict[str, Any]:
    """
    Triggers the incremental sync of CLOSED-BRIGHT listings into the local mirror, carrying the
    verified ClosePrice / CloseDate the general modified-since feed leaves null. Feeds the CMA
    comparable engine. Deep multi-year per-market history is scripts/pull_sold_comps.py's job;
    this only keeps the recent tail fresh.
    """
    logger.info("Closed-listings sync triggered", extra={"event": "closed_sync_started", "max_pages": max_pages})

    sync_results = {
        'started_at': datetime.now(timezone.utc),
        'inserted': 0,
        'updated': 0,
        'errors': 0,
        'success': True,
    }

    try:
        property_sync_service = PropertySyncService()
        results = await property_sync_service.run_closed_sync(max_pages=max_pages)

        sync_results['inserted'] = results.get('inserted', 0)
        sync_results['updated'] = results.get('updated', 0)
        sync_results['errors'] = results.get('errors', 0)
        sync_results['completed_at'] = datetime.now(timezone.utc)
        sync_results['duration_seconds'] = (
            sync_results['completed_at'] - sync_results['started_at']
        ).total_seconds()

        logger.info(
            "Closed-listings sync completed",
            extra={
                "event": "closed_sync_completed",
                "inserted": sync_results['inserted'],
                "updated": sync_results['updated'],
                "duration_seconds": round(sync_results['duration_seconds'], 2),
            },
        )
    except Exception as e:
        logger.error("Closed-listings sync critical failure", exc_info=True, extra={"error": str(e)})
        sync_results['success'] = False

    return sync_results


async def scheduled_closed_sync():
    """
    Entry point for APScheduler. Gated by the same flag as the main property sync. Capped at 40
    pages (8,000 listings) per run so a cold checkpoint can't turn one tick into an unbounded
    crawl - progress is checkpointed, so the next tick continues.
    """
    if not os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
        return

    try:
        await run_closed_property_sync(max_pages=40)
    except Exception as e:
        logger.error("Scheduled closed-listings sync failed", exc_info=True, extra={"error": str(e)})


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


async def scheduled_bright_sync():
    """The one 5-minute Bright cycle: what changed since last time, in order - listings (and
    deletions), then open house dates, then sold prices. Each step keeps its own checkpoint, so a
    quiet five minutes is three small requests. Run one after another so they never overlap; the
    scheduler skips a tick if the previous cycle is still going."""
    if not os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
        logger.info("Property sync is disabled", extra={"event": "property_sync_disabled"})
        return
    for step, run in (("listings", run_global_property_sync), ("open houses", run_open_house_sync),
                      ("sold prices", lambda: run_closed_property_sync(max_pages=40))):
        try:
            await run()
        except Exception as e:  # one step failing never stops the others
            logger.error(f"Bright sync step failed: {step}", exc_info=True, extra={"error": str(e)})
