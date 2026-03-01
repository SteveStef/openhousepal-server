import asyncio
import os
import sys
from sqlalchemy import select

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property

async def inspect_property():
    async with AsyncSessionLocal() as db:
        stmt = select(Property).where(Property.street_address.ilike('%107 Mahogany Ln%'))
        result = await db.execute(stmt)
        props = result.scalars().all()
        if not props:
            print("Property not found.")
            return
            
        for p in props:
            print(f"Address: {p.street_address}")
            print(f"City: '{p.city}'")
            print(f"State: '{p.state}'")
            print(f"Price: {p.price}")
            print(f"Type: {p.home_type}")
            print(f"Beds: {p.bedrooms}")
            print("-" * 20)

if __name__ == "__main__":
    asyncio.run(inspect_property())
