import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

# Add server directory to sys.path
server_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(server_dir))

from app.utils.property_sync_scheduler import sync_all_collections_with_rate_limit
from app.database import AsyncSessionLocal
from app.models.database import Collection
from sqlalchemy import select, func

async def run_manual_sync_test():
    """
    Manually triggers the sync_all_collections_with_rate_limit function
    using real database data and the real Bright MLS API.
    """
    print("🚀 Starting Manual Property Sync Integration Test...")
    print(f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Force enable for this test run
    os.environ["PROPERTY_SYNC_ENABLED"] = "true"
    
    async with AsyncSessionLocal() as db:
        # Check if there are any active collections to sync
        count_query = select(func.count(Collection.id)).where(Collection.status == 'ACTIVE')
        result = await db.execute(count_query)
        total_active = result.scalar()
        
        if total_active == 0:
            print("❌ ABORTING: No ACTIVE collections found in the database.")
            print("Please create an active collection with preferences before running this test.")
            return

        print(f"📊 Found {total_active} active collections in the database.")
        print("🔍 Running sync_all_collections_with_rate_limit()...")
        
        start_time = datetime.now()
        results = await sync_all_collections_with_rate_limit()
        end_time = datetime.now()
        
        duration = (end_time - start_time).total_seconds()
        
        print("" + "="*50)
        print("🏁 SYNC TEST RESULTS")
        print("="*50)
        print(f"Status:        {'✅ SUCCESS' if results['success'] else '❌ FAILED'}")
        print(f"Processed:     {results['collections_processed']} collections")
        print(f"New Props:     {results['total_new_properties']}")
        print(f"Duration:      {duration:.2f} seconds")
        
        if results['errors']:
            print(f"⚠️ ERRORS ENCOUNTERED ({len(results['errors'])}):")
            for err in results['errors']:
                print(f"  - {err}")
        else:
            print("✨ No errors reported during sync.")
            
        print("="*50)

if __name__ == "__main__":
    try:
        asyncio.run(run_manual_sync_test())
    except KeyboardInterrupt:
        print("Stopped by user.")
    except Exception as e:
        print(f"❌ Critical error: {e}")
