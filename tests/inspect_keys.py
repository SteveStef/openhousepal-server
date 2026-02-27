import asyncio
import os
import sys
import json

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.bright_mls_service import bright_mls_service

async def inspect_keys():
    """
    Fetch a single Residential property and print all its keys to find the correct field name.
    """
    print("Fetching a single Residential property to inspect available fields...")
    
    try:
        # Fetch one property without $select to see all fields
        params = {
            "$filter": "PropertyType eq 'Residential' and MlsStatus eq 'ACTIVE-BRIGHT'",
            "$top": 1
        }
        
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        
        if data.get("value"):
            item = data["value"][0]
            keys = sorted(list(item.keys()))
            
            print(f"Total keys found: {len(keys)}")
            print("\nAvailable keys (first 50):")
            for k in keys[:50]:
                print(f" - {k}")
                
            # Specifically look for keys containing 'Type' or 'Sub' or 'Style'
            interesting_keys = [k for k in keys if any(x in k.lower() for x in ['type', 'sub', 'style', 'design'])]
            print("\nInteresting keys (Type/Sub/Style/Design):")
            for k in interesting_keys:
                print(f" - {k}: {item.get(k)}")
                
            # Search for 'Townhouse' in the values of this property just in case
            for k, v in item.items():
                if v and "townhouse" in str(v).lower():
                    print(f"\nFOUND 'Townhouse' in field '{k}': {v}")

        else:
            print("No properties found.")

    except Exception as e:
        print(f"Inspection failed: {str(e)}")

    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(inspect_keys())
