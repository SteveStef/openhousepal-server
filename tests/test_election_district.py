import asyncio
import os
import sys
from typing import List

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), 'server')))

from app.services.bright_mls_service import bright_mls_service

async def test_election_district():
    """
    Test script to verify if 'ElectionDistrict' contains the township-like data we want.
    """
    print("--- Fetching a sample of active properties to inspect ElectionDistrict ---")
    
    try:
        params = {
            "$filter": "MlsStatus eq 'ACTIVE-BRIGHT'",
            "$top": 20,
            "$select": "ListingKey,FullStreetAddress,City,ElectionDistrict,County,SubdivisionName"
        }
        
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        properties = data.get("value", [])
        
        if properties:
            print(f"Success! Found {len(properties)} properties.")
            for p in properties:
                print(f"\n- Address: {p.get('FullStreetAddress')}")
                print(f"  City: {p.get('City')}")
                print(f"  ElectionDistrict: {p.get('ElectionDistrict')}")
                print(f"  County: {p.get('County')}")
                print(f"  Subdivision: {p.get('SubdivisionName')}")
        else:
            print("No properties found.")
            
    except Exception as e:
        print(f"Query failed: {str(e)}")

    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(test_election_district())
