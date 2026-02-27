import asyncio
import os
import sys
from typing import List

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.bright_mls_service import bright_mls_service

async def test_property_types():
    """
    Test script to verify supported PropertyType values in Bright MLS.
    Queries each known type and attempts to discover new ones.
    """
    known_types = [
        "Residential",
        "Multi-Family",
        "Residential Lease",
        "Land",
        "Farm",
        "Commercial Sale",
        "Commercial Lease",
        "Business Opportunity",
        "Industrial"
    ]
    
    print(f"{'Property Type':<25} | {'Count':<10} | {'Status'}")
    print("-" * 50)
    
    for prop_type in known_types:
        try:
            # Query the count for this property type
            params = {
                "$filter": f"PropertyType eq '{prop_type}' and MlsStatus eq 'ACTIVE-BRIGHT'",
                "$count": "true",
                "$top": 0
            }
            
            data = await bright_mls_service._make_request("BrightProperties", params=params)
            count = data.get("@odata.count", 0)
            status = "✅ Found" if count > 0 else "❌ No active listings"
            
            print(f"{prop_type:<25} | {count:<10} | {status}")
            
        except Exception as e:
            print(f"{prop_type:<25} | {'Error':<10} | ⚠️ {str(e)}")

    print("" + "="*50)
    print("Attempting to discover other unique PropertyType values...")
    print("="*50)

    try:
        # Fetch a sample of recent active listings to see what types are present
        params = {
            "$filter": "MlsStatus eq 'ACTIVE-BRIGHT'",
            "$select": "PropertyType",
            "$top": 500
        }
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        
        found_types = set()
        for item in data.get("value", []):
            ptype = item.get("PropertyType")
            if ptype:
                found_types.add(ptype)
        
        print(f"Unique PropertyType values found in a sample of 500 active listings:")
        for ptype in sorted(list(found_types)):
            is_known = " (Known)" if ptype in known_types else " (NEW!)"
            print(f" - {ptype}{is_known}")
            
    except Exception as e:
        print(f"Discovery failed: {str(e)}")

    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(test_property_types())
