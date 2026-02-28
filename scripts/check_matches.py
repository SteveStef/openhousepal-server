import asyncio
import math
import os
import sys
from sqlalchemy import select, and_, or_

# Add server directory to path
sys.path.append(os.path.join(os.getcwd(), 'server'))

from app.database import AsyncSessionLocal
from app.models.database import Property

async def check_matching_properties():
    # Preferences data
    lat = 38.4209573
    lon = -77.4076189
    diameter = 5
    max_price = 323880
    
    radius_miles = float(diameter) / 2.0
    lat_offset = radius_miles / 69.1
    cos_lat = math.cos(math.radians(lat))
    long_offset = radius_miles / (69.1 * cos_lat)
    
    lat_min, lat_max = lat - lat_offset, lat + lat_offset
    lon_min, lon_max = lon - long_offset, lon + long_offset

    print(f"Checking for COMMERCIAL properties:")
    print(f"- Lat range: {lat_min} to {lat_max}")
    print(f"- Lon range: {lon_min} to {lon_max}")
    print(f"- Max Price: ${max_price}")
    
    async with AsyncSessionLocal() as db:
        # 1. Broad check: Any commercial properties at all?
        all_comm_stmt = select(Property).where(Property.home_type == 'COMMERCIAL')
        res = await db.execute(all_comm_stmt)
        all_comm = res.scalars().all()
        print(f"\nTotal COMMERCIAL properties in DB: {len(all_comm)}")
        for p in all_comm:
            print(f"  - {p.street_address} ({p.city}, {p.state}) | Price: ${p.price} | Status: {p.home_status} | Lat: {p.latitude} | Lon: {p.longitude}")

        # 2. Specific check: Matching the full query
        stmt = select(Property).where(and_(
            Property.home_type == 'COMMERCIAL',
            Property.latitude >= lat_min,
            Property.latitude <= lat_max,
            Property.longitude >= lon_min,
            Property.longitude <= lon_max,
            Property.price <= max_price,
            or_(Property.home_status.ilike('ACTIVE%'), Property.home_status.ilike('COMING SOON%'))
        ))
        
        res = await db.execute(stmt)
        matches = res.scalars().all()
        print(f"\nProperties matching ALL criteria: {len(matches)}")

if __name__ == "__main__":
    asyncio.run(check_matching_properties())
