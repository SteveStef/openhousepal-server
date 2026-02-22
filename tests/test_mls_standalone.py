import asyncio
import os
import sys
from pathlib import Path

# Fix ModuleNotFoundError: Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.services.bright_mls_service import bright_mls_service
from dotenv import load_dotenv

# Load environment variables from server/.env
load_dotenv(root_dir / ".env")

async def test_member_id(mls_id: str = "3235372"):
    print(f"🚀 Testing Bright MLS Member ID: {mls_id}...\n")
    if not os.getenv("BRIGHT_MLS_CLIENT"):
        print("❌ ERROR: BRIGHT_MLS_CLIENT not found in .env file.")
        return

    try:
        print("--- Calling BrightMembers API ---")
        exists = await bright_mls_service.bright_mls_id_exists(mls_id)
        print(exists)

    except Exception as e:
        print(f"\n❌ API Error: {e}")
    finally:
        print("\n🏁 Check complete.")

async def test_media_fetching(listing_key: str):
    print(f"🚀 Testing Media Fetching for ListingKey: {listing_key}...\n")
    if not os.getenv("BRIGHT_MLS_CLIENT"):
        print("❌ ERROR: BRIGHT_MLS_CLIENT not found in .env file.")
        return

    try:
        print(f"--- Calling _fetch_all_media for {listing_key} ---")
        images = await bright_mls_service._fetch_all_media(listing_key)
        
        if images:
            print(f"✅ SUCCESS: Found {len(images)} images")
            for i, url in enumerate(images[:3]): # Show first 3
                print(f"  [{i+1}] {url[:80]}...")
        else:
            print("❌ FAILURE: No images returned. Checking raw response structure...")
            # Let's do a more generic search to see what's available
            raw_data = await bright_mls_service._make_request("BrightMedia", params={
                "$filter": f"ResourceRecordKey eq {listing_key}",
                "$top": 5
            })
            print(f"Raw response for ResourceRecordKey={listing_key}:")
            import json
            print(json.dumps(raw_data, indent=2))

    except Exception as e:
        print(f"\n❌ Media Fetch Error: {e}")
    finally:
        print("\n🏁 Media check complete.")

if __name__ == "__main__":
    # You can change the listing_key here to test different properties
    target_key = "804149848668" 
    
    async def main():
        # await test_member_id("3372275")
        await test_media_fetching(target_key)
        await bright_mls_service.close()

    asyncio.run(main())
