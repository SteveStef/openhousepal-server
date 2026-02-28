import asyncio
import os
import sys
from typing import List

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.bright_mls_service import bright_mls_service

async def test_township_query():
    """
    Test script to verify if we can query by 'Township' using the service pattern.
    """
    # Try a known PA township where this field is common
    township_name = "RADNOR"
    print(f"--- Querying for active properties in Township: {township_name} ---")
    
    try:
        params = {
            "$filter": f"MlsStatus eq 'ACTIVE-BRIGHT' and Township eq '{township_name}'",
            "$top": 5,
            "$select": "ListingKey,FullStreetAddress,City,Township,CountyOrProvince,SchoolDistrictName"
        }
        
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        properties = data.get("value", [])
        
        if properties:
            print(f"Success! Found {len(properties)} properties.")
            for p in properties:
                print(f"\n- Address: {p.get('FullStreetAddress')}")
                print(f"  City: {p.get('City')}")
                print(f"  Township: {p.get('Township')}")
                print(f"  County: {p.get('CountyOrProvince')}")
                print(f"  School District: {p.get('SchoolDistrictName')}")
        else:
            print(f"No properties found for Township: {township_name}")
            
    except Exception as e:
        print(f"Query failed: {str(e)}")

    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(test_township_query())
