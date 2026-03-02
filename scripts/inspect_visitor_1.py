import asyncio
import os
import sys
import json
from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Collection, CollectionPreferences, Property

async def inspect_visitor_1():
    async with AsyncSessionLocal() as db:
        # 1. Get Collection and Preferences
        stmt = (
            select(Collection)
            .options(selectinload(Collection.preferences))
            .where(Collection.name == 'Stress Test: Test Visitor 1')
        )
        res = await db.execute(stmt)
        col = res.scalar_one_or_none()
        
        if not col:
            print("COLLECTION NOT FOUND")
            return

        print(f"--- COLLECTION: {col.name} ({col.id}) ---")
        prefs = col.preferences
        if prefs:
            pref_dict = {
                "cities": prefs.cities,
                "townships": prefs.townships,
                "school_districts": prefs.school_districts,
                "min_price": prefs.min_price,
                "max_price": prefs.max_price,
                "min_beds": prefs.min_beds,
                "min_baths": prefs.min_baths,
                "is_single_family": prefs.is_single_family,
                "is_condo": prefs.is_condo,
                "is_town_house": prefs.is_town_house,
                "is_multi_family": prefs.is_multi_family,
                "is_lot_land": prefs.is_lot_land,
                "is_apartment": prefs.is_apartment
            }
            print(f"PREFERENCES: {json.dumps(pref_dict, indent=2)}")
        
        # 2. Get details for rejected properties
        rejected_addresses = [
            '1772 FERNDALE AVE #B',
            '1847 HORACE AVE #2ND FL',
            '1815 HORACE AVE #3RD FLR'
        ]
        
        print("\n--- REJECTED PROPERTIES ---")
        for addr in rejected_addresses:
            p_res = await db.execute(select(Property).where(Property.street_address == addr))
            p = p_res.scalar()
            if p:
                print(f"PROP: {p.street_address}")
                print(f"  PRICE: {p.price} | BEDS: {p.bedrooms} | BATHS: {p.bathrooms}")
                print(f"  CITY: {p.city} | TOWNSHIP: {p.township} | SD: {p.school_district_name}")
                print(f"  TYPE: {p.home_type} | STATUS: {p.home_status}")
            else:
                print(f"PROP: {addr} NOT FOUND")

if __name__ == "__main__":
    asyncio.run(inspect_visitor_1())
