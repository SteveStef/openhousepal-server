
import asyncio
import os
import sys
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import SystemSettings
from app.database import DATABASE_URL

SYNC_KEY = "last_property_sync_time"

async def update_sync_time(date_str: str):
    """
    Updates the 'last_property_sync_time' in SystemSettings.
    Expected format: YYYY-MM-DDTHH:MM:SSZ
    """
    try:
        # Validate format
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
        dt = dt.replace(tzinfo=timezone.utc)
        formatted_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        print("❌ Invalid date format. Please use: YYYY-MM-DDTHH:MM:SSZ (e.g., 2026-03-31T12:00:00Z)")
        return

    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as db:
        async with db.begin():
            stmt = select(SystemSettings).where(SystemSettings.key == SYNC_KEY)
            result = await db.execute(stmt)
            setting = result.scalar_one_or_none()

            val = {"timestamp": formatted_date}
            if setting:
                setting.value = val
                print(f"✅ Updated existing sync time to: {formatted_date}")
            else:
                db.add(SystemSettings(key=SYNC_KEY, value=val))
                print(f"✅ Created new sync time entry: {formatted_date}")

    await engine.dispose()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/update_sync_time.py YYYY-MM-DDTHH:MM:SSZ")
        print("Example: python3 scripts/update_sync_time.py 2026-03-31T00:00:00Z")
    else:
        asyncio.run(update_sync_time(sys.argv[1]))
