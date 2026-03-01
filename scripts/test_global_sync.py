import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from sqlalchemy import text

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.services.property_sync_service import PropertySyncService
from app.config.logging import get_logger

logger = get_logger("sync_test")

async def test_sync(rewind_hours: float = 1.0, dry_run: bool = True):
    """
    Test the global sync logic by manually overriding the start time.
    """
    print(f"\n--- 🧪 Property Sync Test (Dry Run: {dry_run}) ---")
    
    # Calculate a specific start time
    start_time = (datetime.now(timezone.utc) - timedelta(hours=rewind_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"🕒 Simulating sync starting from: {start_time}")

    sync_service = PropertySyncService()
    
    async with AsyncSessionLocal() as db:
        # We don't want to actually update the system_settings during a test
        # So we'll monkey-patch the get_last_sync_time just for this run
        async def mock_get_last_sync_time(db_session):
            return start_time
        
        sync_service.get_last_sync_time = mock_get_last_sync_time
        
        print("Running sync logic...")
        results = await sync_service.run_global_sync()
        
        print("\n--- 📊 Test Results ---")
        print(f"✅ Properties Updated/Created: {results.get('updated', 0)}")
        
        # Display breakdown by type
        details = results.get('property_details', [])
        if details:
            print("\n🏠 Property Type Breakdown:")
            type_counts = {}
            for d in details:
                t = d['home_type']
                type_counts[t] = type_counts.get(t, 0) + 1
            for h_type, count in type_counts.items():
                print(f"  • {h_type}: {count}")

        print(f"\n🔔 Notifications Generated: {results.get('notifications_sent', 0)}")
        print(f"❌ Batch Errors: {results.get('errors', 0)}")
        print("------------------------\n")

if __name__ == "__main__":
    # Default: Rewind 2 hours
    rewind = 2.0
    if len(sys.argv) > 1:
        try:
            rewind = float(sys.argv[1])
        except ValueError:
            print("Invalid rewind hours. Using default (2.0).")
        
    asyncio.run(test_sync(rewind_hours=rewind, dry_run=False))
