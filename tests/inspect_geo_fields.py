import asyncio
import os
import sys
import json

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), 'server')))

from app.services.bright_mls_service import bright_mls_service

async def discover_township_field():
    """
    Fetches samples and looks for township-like data in MLSAreaMajor, CityRegion, etc.
    """
    print("--- Fetching properties to inspect geo fields ---")
    
    try:
        params = {
            "$filter": "MlsStatus eq 'ACTIVE-BRIGHT'",
            "$top": 5,
            "$select": "ListingKey,FullStreetAddress,City,County,MLSAreaMajor,MLSAreaMinor,SubdivisionName"
        }
        
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        properties = data.get("value", [])
        
        for p in properties:
            print(f"\n- Address: {p.get('FullStreetAddress')}")
            print(f"  City: {p.get('City')}")
            print(f"  County: {p.get('County')}")
            print(f"  MLSAreaMajor: {p.get('MLSAreaMajor')}")
            print(f"  MLSAreaMinor: {p.get('MLSAreaMinor')}")
            print(f"  Subdivision: {p.get('SubdivisionName')}")
            
    except Exception as e:
        print(f"Discovery failed: {str(e)}")

    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(discover_township_field())
