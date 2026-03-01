import asyncio
import os
import sys
from sqlalchemy import select

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.database import AsyncSessionLocal
from app.models.database import Property

async def find_quotes():
    async with AsyncSessionLocal() as db:
        stmt = select(Property.street_address, Property.school_district_name).where(
            Property.school_district_name.ilike("%'%")
        )
        res = await db.execute(stmt)
        rows = res.fetchall()
        print(f"🔍 Found {len(rows)} properties with quotes in district name:")
        for r in rows:
            print(f"🏠 {r[0]} | District: {r[1]}")

if __name__ == "__main__":
    asyncio.run(find_quotes())
