import asyncio
import os
import sys
from sqlalchemy import select

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property

async def check_prop():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Property).where(Property.street_address == '284 IVEN AVE #1B-270-1B'))
        p = res.scalar()
        if p:
            print(f"PRICE: {p.price} | BEDS: {p.bedrooms} | CITY: {p.city} | TOWNSHIP: {p.township} | TYPE: {p.home_type}")
        else:
            print("NOT FOUND")

if __name__ == "__main__":
    asyncio.run(check_prop())
