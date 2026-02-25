import asyncio
import os
import sys
import json
from pathlib import Path

# Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.services.bright_mls_service import bright_mls_service
from dotenv import load_dotenv

# Load environment variables from server/.env
load_dotenv(root_dir / ".env")

async def test_coming_soon():
    print("🚀 Testing for Coming Soon properties...")
    
    if not os.getenv("BRIGHT_MLS_CLIENT"):
        print("❌ ERROR: BRIGHT_MLS_CLIENT not found in .env file.")
        return

    # Try a few common status names for Coming Soon in Bright MLS
    statuses_to_try = ["COMING SOON", "COMING-SOON", "COMING SOON-BRIGHT", "COMING-SOON-BRIGHT"]
    
    for status in statuses_to_try:
        try:
            params = {
                "$filter": f"MlsStatus eq '{status}'",
                "$top": 3,
                "$count": "true",
                "$select": "ListingKey,ListingId,MlsStatus,FullStreetAddress,ListPrice,DaysOnComingSoon"
            }
            
            print(f"--- Trying MlsStatus: '{status}' ---")
            data = await bright_mls_service._make_request("BrightProperties", params=params)
            
            count = data.get("@odata.count", 0)
            value = data.get("value", [])
            
            if count > 0:
                print(f"✅ SUCCESS: Found {count} properties with status '{status}'")
                print("Sample items:")
                for item in value:
                    print(f"  - {item.get('FullStreetAddress')} (${item.get('ListPrice'):,}) [Key: {item.get('ListingKey')}]")
                # If we found it, no need to try others
                return
            else:
                print(f"ℹ️ No properties found for '{status}'")

        except Exception as e:
            print(f"⚠️ Error trying '{status}': {e}")
            
    print("--- Fallback: Checking unique MlsStatus values from recent properties ---")
    try:
        # Fetch 100 recent properties to see what statuses are actually in use
        params = {
            "$top": 100,
            "$select": "MlsStatus",
            "$orderby": "ModificationTimestamp desc"
        }
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        statuses = set(item.get("MlsStatus") for item in data.get("value", []) if item.get("MlsStatus"))
        print(f"Unique statuses found in last 100 items: {statuses}")
        
        # Also try filtering by DaysOnComingSoon
        params = {
            "$filter": "DaysOnComingSoon gt 0",
            "$top": 5,
            "$select": "ListingKey,FullStreetAddress,MlsStatus,DaysOnComingSoon"
        }
        print("--- Trying filter: DaysOnComingSoon gt 0 ---")
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        if data.get("value"):
            print(f"Found {len(data['value'])} items with DaysOnComingSoon > 0")
            for item in data['value']:
                print(f"  - {item.get('FullStreetAddress')} | Status: {item.get('MlsStatus')} | Days: {item.get('DaysOnComingSoon')}")
    except Exception as e:
        print(f"❌ Fallback failed: {e}")

if __name__ == "__main__":
    async def main():
        await test_coming_soon()
        await bright_mls_service.close()

    asyncio.run(main())
