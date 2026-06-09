import asyncio
import os
import sys
from datetime import datetime, timezone

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.services.bright_mls_service import BrightMlsService
from app.utils.mls_mapper import parse_dt

async def test_deletion_timestamp_logic():
    print("\n--- Testing Bright MLS Deletion Timestamp Logic ---")
    
    service = BrightMlsService()
    try:
        # 1. Fetch one recent deletion from Bright MLS
        print("Fetching a recent deletion from Bright MLS...")
        params = {
            "$top": 1,
            "$orderby": "DeletionTimestamp desc",
            "$select": "DeleteKey,DeletionTimestamp,TableName"
        }
        
        data = await service._make_request("Deletion", params=params)
        values = data.get("value", [])
        
        if not values:
            print("❌ No deletion records found to test with.")
            return
            
        deletion = values[0]
        delete_key = deletion.get("DeleteKey")
        raw_ts = deletion.get("DeletionTimestamp")
        
        print(f"✅ Found Deletion Record:")
        print(f"   DeleteKey:         {delete_key}")
        print(f"   Raw Timestamp:     {raw_ts}")
        
        # 2. Parse the timestamp
        parsed_ts = parse_dt(raw_ts)
        print(f"   Parsed Datetime:   {parsed_ts} (Type: {type(parsed_ts)})")
        
        if not parsed_ts:
            print("❌ Failed to parse the timestamp using mls_mapper.parse_dt")
            return

        # 3. Simulate a Comparison
        # Case A: Local modification is OLDER than deletion (Should mark as Off-Market)
        old_local_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
        should_delete_a = parsed_ts > old_local_ts
        print(f"\nScenario A: Local Mod (2020-01-01) vs Deletion ({parsed_ts.date()})")
        print(f"   Should mark Off-Market? {should_delete_a} (Expected: True)")
        
        # Case B: Local modification is NEWER than deletion (Should SKIP)
        new_local_ts = datetime(2028, 1, 1, tzinfo=timezone.utc)
        should_delete_b = parsed_ts > new_local_ts
        print(f"\nScenario B: Local Mod (2028-01-01) vs Deletion ({parsed_ts.date()})")
        print(f"   Should mark Off-Market? {should_delete_b} (Expected: False)")

        # Verify comparison works
        if should_delete_a == True and should_delete_b == False:
            print("\n✨ Logic Verification SUCCESS: Timestamps are correctly comparable.")
        else:
            print("\n❌ Logic Verification FAILED: Comparison results were unexpected.")

    except Exception as e:
        print(f"⚠️ Error during test: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        await service.close()

if __name__ == "__main__":
    asyncio.run(test_deletion_timestamp_logic())
