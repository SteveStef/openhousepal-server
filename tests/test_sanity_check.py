import asyncio
import os
import sys
import json
from pathlib import Path
from sqlalchemy import select, update

# Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.database import AsyncSessionLocal
from app.models.database import Property
from app.services.bright_mls_service import bright_mls_service
from dotenv import load_dotenv

load_dotenv(root_dir / ".env")

async def run_sanity_check_test():
    """
    Performs a 'Set Difference' audit and generates a Sync Health Report.
    """
    print("\n" + "="*60)
    print("📊 BRIGHT MLS SYNC HEALTH REPORT")
    print("="*60)

    # PHASE 1: Local Inventory
    async with AsyncSessionLocal() as db:
        stmt = select(Property.listing_key).where(
            Property.home_status.in_(['ACTIVE-BRIGHT', 'COMING SOON-BRIGHT'])
        )
        res = await db.execute(stmt)
        local_keys = set(str(row[0]) for row in res.all())
    
    # PHASE 2: MLS Source of Truth
    print(f"📡 Querying Bright MLS Source of Truth...")
    mls_active_keys = await fetch_all_mls_active_keys()
    
    # PHASE 3: Gap Analysis
    ghost_keys = local_keys - mls_active_keys
    missing_from_local = mls_active_keys - local_keys
    
    # CALCULATE METRICS
    total_local = len(local_keys)
    total_mls = len(mls_active_keys)
    accuracy = 100 - (len(ghost_keys) / total_local * 100) if total_local > 0 else 100

    print("\n📈 CORE METRICS:")
    print(f"  - Local Active Inventory:   {total_local}")
    print(f"  - MLS Active Inventory:     {total_mls}")
    print(f"  - Ghost Records Found:      {len(ghost_keys)} ⚠️")
    print(f"  - Missing Local Records:    {len(missing_from_local)} (Will be caught by next sync)")
    print(f"  - SYNC ACCURACY:            {accuracy:.2f}%")

    if ghost_keys:
        print("\n🔍 SAMPLE GHOST RECORDS (Keys in DB but NOT in MLS):")
        for key in list(ghost_keys)[:5]:
            print(f"  - {key}")
        print(f"  ...and {len(ghost_keys) - 5} others.")
        print("\n💡 ACTION REQUIRED: Run 'python3 scripts/sync_off_market_properties.py' to fix.")
    else:
        print("\n✅ SYNC HEALTH: PERFECT. No stale records detected.")
    
    print("="*60 + "\n")

async def fetch_all_mls_active_keys() -> set:
    keys = set()
    page_size = 1000
    skip = 0
    while True:
        params = {
            "$filter": "MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT')",
            "$select": "ListingKey",
            "$top": page_size,
            "$skip": skip
        }
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        batch = data.get("value", [])
        if not batch: break
        for item in batch:
            keys.add(str(item["ListingKey"]))
        if len(batch) < page_size: break
        skip += page_size
    return keys

if __name__ == "__main__":
    async def main():
        await run_sanity_check_test()
        await bright_mls_service.close()
    asyncio.run(main())
