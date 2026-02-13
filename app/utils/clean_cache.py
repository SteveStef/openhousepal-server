from datetime import datetime, timedelta, timezone
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import PropertyDetails
from app.database import AsyncSessionLocal
from app.config.logging import get_logger
from app.services.verification_service import verification_service
import os

logger = get_logger(__name__)

async def cleanup_expired_property_cache():
    """Remove expired property cache data from PropertyDetails table"""
    cache_expiry_hours = int(os.getenv("CACHE_EXPIRY_HOURS", 3))
    logger.info("Starting property cache cleanup", extra={"cache_expiry_hours": cache_expiry_hours})

    try:
        async with AsyncSessionLocal() as db:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=cache_expiry_hours)

            # Now we delete the entire record from PropertyDetails when it expires
            stmt = delete(PropertyDetails).where(
                PropertyDetails.updated_at < cutoff_time
            )

            result = await db.execute(stmt)
            await db.commit()

            logger.info("Property cache cleanup completed", extra={"entries_cleaned": result.rowcount})
            return result.rowcount

    except Exception as e:
        logger.error("Property cache cleanup failed", exc_info=True)
        raise

async def cleanup_expired_signup_verifications():
    """Remove expired signup verification entries from the database"""
    logger.info("Starting signup verification cleanup")
    try:
        async with AsyncSessionLocal() as db:
            count = await verification_service.cleanup_expired(db)
            return count
    except Exception as e:
        logger.error("Signup verification cleanup failed", exc_info=True)
        raise
