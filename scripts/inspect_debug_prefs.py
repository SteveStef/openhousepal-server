import asyncio
import os
import sys
from sqlalchemy import select

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Collection, CollectionPreferences

async def inspect_prefs():
    async with AsyncSessionLocal() as db:
        stmt = select(Collection, CollectionPreferences).join(CollectionPreferences).where(Collection.name == '107 MAHOGANY LN')
        result = await db.execute(stmt)
        row = result.fetchone()
        if row:
            col, prefs = row
            print(f"Collection: {col.name}")
            print(f"Cities: {prefs.cities}")
            print(f"Townships: {prefs.townships}")
            print(f"Beds: {prefs.min_beds} - {prefs.max_beds}")
            print(f"Price: {prefs.min_price} - {prefs.max_price}")
            print(f"Types: SF:{prefs.is_single_family}, TH:{prefs.is_town_house}, C:{prefs.is_condo}")
        else:
            print("Collection not found.")

if __name__ == "__main__":
    asyncio.run(inspect_prefs())
