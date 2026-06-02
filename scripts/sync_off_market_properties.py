import asyncio
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any
from sqlalchemy import select, update

# Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.database import AsyncSessionLocal
from app.models.database import Property
from app.services.bright_mls_service import bright_mls_service
from dotenv import load_dotenv

load_dotenv(root_dir / ".env")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("sync_off_market")

async def get_all_active_mls_keys() -> set:
    """Helper to fetch all active ListingKeys from Bright MLS efficiently."""
    active_keys = set()
    page_size = 1000
    skip = 0
    
    params = {
        "$filter": "MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT')",
        "$select": "ListingKey",
        "$top": page_size
    }
    
    while True:
        params["$skip"] = skip
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        batch = data.get("value", [])
        
        if not batch:
            break
            
        for item in batch:
            active_keys.add(str(item["ListingKey"]))
        
        if len(batch) < page_size:
            break
        skip += page_size
        
    return active_keys

async def cleanup_off_market_properties() -> Dict[str, Any]:
    """
    The Sanity Check (Audit).
    Reconciles local 'ACTIVE' properties with the Bright MLS Source of Truth.
    Identifies 'Ghost' records and marks them as OFF_MARKET-BRIGHT.
    """
    logger.info("Starting Property Sanity Check (Audit)...")
    stats = {"total_checked": 0, "ghosts_found": 0, "updated": 0, "errors": 0}

    try:
        # 1. Get Local Active Inventory
        async with AsyncSessionLocal() as db:
            stmt = select(Property.listing_key).where(
                Property.home_status.in_(['ACTIVE-BRIGHT', 'COMING SOON-BRIGHT'])
            )
            res = await db.execute(stmt)
            local_keys = set(str(row[0]) for row in res.all())
        
        stats["total_checked"] = len(local_keys)
        logger.info(f"Found {len(local_keys)} properties marked as active in local database.")

        if not local_keys:
            logger.info("No active properties found in local database. Skipping audit.")
            return stats

        # 2. Fetch Source of Truth from MLS (Efficiently)
        mls_active_keys = await get_all_active_mls_keys()
        logger.info(f"Found {len(mls_active_keys)} currently active properties in Bright MLS.")

        # 3. Find Ghosts (Local ACTIVE but missing from MLS)
        ghost_keys = local_keys - mls_active_keys
        stats["ghosts_found"] = len(ghost_keys)

        if not ghost_keys:
            logger.info("Perfect Sync: No ghost records detected.")
            return stats

        logger.warning(f"Detected {len(ghost_keys)} ghost records! Marking them as OFF_MARKET-BRIGHT.")

        # 4. Batch Update to OFF_MARKET
        ghost_list = list(ghost_keys)
        batch_size = 500
        for i in range(0, len(ghost_list), batch_size):
            batch = ghost_list[i : i + batch_size]
            try:
                async with AsyncSessionLocal() as db:
                    stmt = (
                        update(Property)
                        .where(Property.listing_key.in_(batch))
                        .values(
                            home_status='OFF_MARKET-BRIGHT',
                            updated_at=datetime.now(timezone.utc)
                        )
                    )
                    await db.execute(stmt)
                    await db.commit()
                    stats["updated"] += len(batch)
                    logger.info(f"  ...marked {stats['updated']}/{len(ghost_keys)} ghosts as off-market")
            except Exception as e:
                logger.error(f"Failed to update batch of ghost properties: {e}")
                stats["errors"] += 1

    except Exception as e:
        logger.error(f"Critical error during sanity check: {e}", exc_info=True)
        stats["errors"] += 1

    logger.info(f"Sanity Check Complete: {stats}")
    return stats

async def main():
    logger.info("🚀 Starting Off-Market Property Cleanup...")
    try:
        stats = await cleanup_off_market_properties()
        logger.info(f"✅ Cleanup Summary: {stats}")
    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")
    finally:
        await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(main())
