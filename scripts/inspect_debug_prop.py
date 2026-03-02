import asyncio
import os
import sys
from sqlalchemy import select, func

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property

async def debug_props():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(func.count(Property.id)))
        count = res.scalar()
        print(f"TOTAL PROPERTIES: {count}")
        
        if count > 0:
            sample = await db.execute(select(Property).limit(5))
            for p in sample.scalars().all():
                print(f"PROP: {p.street_address} | CITY: {p.city} | PRICE: {p.price} | BEDS: {p.bedrooms} | TYPE: {p.home_type} | STATUS: {p.home_status}")

if __name__ == "__main__":
    asyncio.run(debug_props())
