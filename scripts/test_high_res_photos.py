import asyncio
import os
import sys
import json
from dotenv import load_dotenv

# Add the server directory to the path so we can import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.bright_mls_service import BrightMlsService

async def test_high_res_photos():
    load_dotenv()
    
    # Initialize service
    service = BrightMlsService()
    
    # Target property for testing
    listing_key = "803996757732"
    
    print(f"--- Testing High Resolution Photo Fetch for ListingKey: {listing_key} ---")
    
    params = {
        "$filter": f"ResourceRecordKey eq {listing_key} and MediaCategory eq 'Photo'",
        "$select": "ResourceRecordKey,MediaURL,MediaURLHiRes,MediaURLFull",
        "$orderby": "MediaDisplayOrder asc"
    }
    
    try:
        print("Making request to BrightMedia endpoint...")
        data = await service._make_request("BrightMedia", params=params)
        
        photos = data.get("value", [])
        if not photos:
            print("No photos found for this listing.")
            return

        print(f"Found {len(photos)} photos. Analyzing resolutions for all photos:")
        
        for idx, photo in enumerate(photos):
            print(f"\nPhoto {idx + 1}:")
            u_std = photo.get('MediaURL', 'N/A')
            u_hi = photo.get('MediaURLHiRes', 'N/A')
            u_full = photo.get('MediaURLFull', 'N/A')
            
            print(f"  - MediaURL:      {u_std}")
            print(f"  - MediaURLHiRes: {u_hi}")
            print(f"  - MediaURLFull:  {u_full}")
            
            # Highlight the recommended URL
            best_url = u_full if u_full != 'N/A' else (u_hi if u_hi != 'N/A' else u_std)
            print(f"  - Recommended:   {best_url}")

    except Exception as e:
        print(f"Error during test: {e}")

if __name__ == "__main__":
    asyncio.run(test_high_res_photos())
