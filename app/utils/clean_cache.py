from datetime import datetime, timedelta
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import Property
from app.database import AsyncSessionLocal
from app.config.logging import get_logger
import os

logger = get_logger(__name__)

async def cleanup_expired_property_cache():
    """Remove expired property cache data"""
    cache_expiry_hours = int(os.getenv("CACHE_EXPIRY_HOURS", 3))
    logger.info("Starting property cache cleanup", extra={"cache_expiry_hours": cache_expiry_hours})

    try:
        async with AsyncSessionLocal() as db:
            cutoff_time = datetime.utcnow() - timedelta(hours=cache_expiry_hours)

            stmt = update(Property).where(
                Property.detailed_data_cached_at < cutoff_time
            ).values(
                detailed_property=None,
                detailed_data_cached=False,
                detailed_data_cached_at=None
            )

            result = await db.execute(stmt)
            await db.commit()

            logger.info("Property cache cleanup completed", extra={"entries_cleaned": result.rowcount})
            return result.rowcount

    except Exception as e:
        logger.error("Property cache cleanup failed", exc_info=True)
        raise
