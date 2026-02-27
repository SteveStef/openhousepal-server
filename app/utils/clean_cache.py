from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.config.logging import get_logger
from app.services.verification_service import verification_service

logger = get_logger(__name__)

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
