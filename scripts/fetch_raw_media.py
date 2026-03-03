import asyncio
import os
import sys
import json
from dotenv import load_dotenv

# Add the server directory to the path so we can import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.bright_mls_service import BrightMlsService

async def fetch_raw_media():
    load_dotenv()
    
    # Initialize service
    service = BrightMlsService()
    
    # Use the key you provided earlier or set a default
    listing_key = os.getenv("TEST_LISTING_KEY", "803996757732")
    
    print(f"--- Fetching Default Media Response (No $select) for ListingKey: {listing_key} ---")
    
    # Removing $select entirely to see the default API behavior
    params = {
        "$filter": f"ResourceRecordKey eq {listing_key} and MediaCategory eq 'Photo'",
        "$orderby": "MediaDisplayOrder asc"
    }
    
    try:
        print("Connecting to Bright MLS...")
        data = await service._make_request("BrightMedia", params=params)
        
        raw_value = data.get("value", [])
        
        if not raw_value:
            print("No media found for this listing.")
            return

        output_file = "raw_media_output_default.json"
        with open(output_file, "w") as f:
            json.dump(raw_value, f, indent=4)
            
        print(f"SUCCESS: Default media data saved to {output_file}")
        print(f"Found {len(raw_value)} media items.")
        
        # Print the keys of the first item to see what fields were returned
        if len(raw_value) > 0:
            print("\nDefault fields returned by API:")
            print(", ".join(raw_value[0].keys()))

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(fetch_raw_media())
