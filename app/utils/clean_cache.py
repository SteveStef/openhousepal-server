from datetime import datetime, timedelta
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import Property
from app.database import AsyncSessionLocal
import os

print(os.getenv("CACHE_EXPIRY_DAYS"))
async def cleanup_expired_property_cache():
    """Remove expired property cache data older than 7 days"""
    print(f"[CLEANUP] Starting cache cleanup at {datetime.utcnow()}")
    try:
        async with AsyncSessionLocal() as db:
            cutoff_time = datetime.utcnow() - timedelta(days=int(os.getenv("CACHE_EXPIRY_DAYS", 7)))

            stmt = update(Property).where(
                Property.detailed_data_cached_at < cutoff_time
            ).values(
                detailed_property=None,
                detailed_data_cached=False,
                detailed_data_cached_at=None
            )

            result = await db.execute(stmt)
            await db.commit()

            print(f"[CLEANUP] Successfully cleaned up {result.rowcount} expired cache entries")
            return result.rowcount

    except Exception as e:
        print(f"[CLEANUP] Error during cache cleanup: {str(e)}")
        raise
