import asyncio
import json
import os
import sys
from datetime import datetime

# Add the current directory to sys.path to allow importing from 'app'
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.bright_mls_service import BrightMlsService

async def query_bright_property():
    listing_key = "804613057266"
    print(f"Querying Bright MLS for ListingKey: {listing_key}")
    
    service = BrightMlsService()
    try:
        # We use _make_request directly to get full details without select
        # The endpoint for properties is "BrightProperties"
        # We filter by ListingKey
        params = {
            "$filter": f"ListingKey eq {listing_key}"
        }

        
        print(f"Requesting BrightProperties with filter: {params['$filter']}")
        data = await service._make_request("BrightProperties", params=params)
        
        properties = data.get("value", [])
        if properties:
            prop = properties[0]
            output_file = "tmp2.json"
            with open(output_file, "w") as f:
                json.dump(prop, f, indent=4)
            print(f"Successfully saved property to {output_file}")
            
            # Also fetch media if possible
            print(f"Fetching media for {listing_key}...")
            media_data = await service.get_media_for_properties([listing_key])
            if media_data:
                print(f"Found {len(media_data.get(listing_key, []))} photos.")
                # We could append photos to the json but user asked for "full property details no select"
                # which usually refers to the BrightProperties fields.
        else:
            print(f"Property with ListingKey {listing_key} not found in Bright MLS.")
            print(f"Full response: {data}")
            
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        await service.close()

if __name__ == "__main__":
    asyncio.run(query_bright_property())
