"""Read-only: print the property-sync checkpoints (SystemSettings) to see whether the closed-
listings sync leg (run_closed_sync) has ever actually run, and how recent its checkpoint is."""
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import SystemSettings
from app.database import DATABASE_URL


async def main():
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SystemSettings).where(SystemSettings.key.in_([
                "last_property_sync_time", "last_closed_property_sync_time", "last_open_house_sync_time",
            ]))
        )
        for s in result.scalars().all():
            print(f"{s.key}: {s.value}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
