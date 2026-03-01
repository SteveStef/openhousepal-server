import asyncio
import os
import sys
from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, Collection, CollectionPreferences

async def inspect_test_showcase():
    async with AsyncSessionLocal() as db:
        # Get the 'test' collection
        stmt = (
            select(Collection)
            .options(
                selectinload(Collection.preferences),
                selectinload(Collection.properties)
            )
            .where(Collection.name == 'test')
        )
        res = await db.execute(stmt)
        col = res.scalar_one_or_none()

        if not col:
            print("❌ 'test' collection not found.")
            return

        print(f"📊 Showcase: '{col.name}'")
        prefs = col.preferences
        print(f"   Preferences:")
        print(f"      Cities: {prefs.cities}")
        print(f"      Townships: {prefs.townships}")
        print(f"      School Districts: {prefs.school_districts}")
        print(f"      Price: ${prefs.min_price} - ${prefs.max_price}")
        print(f"      Beds: {prefs.min_beds} - {prefs.max_beds}")
        print(f"      Baths: {prefs.min_baths} - {prefs.max_baths}")
        print(f"      Year Built: {prefs.min_year_built} - {prefs.max_year_built}")
        print(f"      Address: {prefs.address} (Radius: {prefs.diameter})")
        print(f"      Lat/Long: {prefs.lat}, {prefs.long}")

        print(f"\n   Existing Properties in Collection (Sample):")
        for prop in col.properties[:5]:
            print(f"      - '{prop.street_address}'")
            print(f"        City: {prop.city}, State: {prop.state}, Township: {prop.township}")
            print(f"        School District: {prop.school_district_name}")
            print(f"        Price: ${prop.price}, Beds: {prop.bedrooms}, Baths: {prop.bathrooms}")
            print(f"        Year Built: {prop.year_built}")
            print(f"        Lat/Long: {prop.latitude}, {prop.longitude}")
            print("-" * 30)

if __name__ == "__main__":
    asyncio.run(inspect_test_showcase())
