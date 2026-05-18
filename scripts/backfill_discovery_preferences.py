import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import User, DiscoveryPreferences

async def backfill_discovery_preferences():
    """
    1. Finds users who do not have a DiscoveryPreferences entry and creates one.
    2. Updates existing entries where brokerages is NULL with the user's own brokerage.
    """
    print("Connecting to database...")
    
    async with AsyncSessionLocal() as session:
        try:
            # PHASE 1: Create missing entries
            print("🔍 Phase 1: Checking for missing DiscoveryPreferences entries...")
            stmt = (
                select(User)
                .outerjoin(DiscoveryPreferences)
                .where(DiscoveryPreferences.id == None)
            )
            
            result = await session.execute(stmt)
            users_without_prefs = result.scalars().all()
            
            if users_without_prefs:
                print(f"Found {len(users_without_prefs)} users without preferences. Creating them...")
                for user in users_without_prefs:
                    # Initialize with the user's own brokerage if it exists
                    brokerages = [user.brokerage] if user.brokerage else []
                    discovery_prefs = DiscoveryPreferences(
                        user_id=user.id,
                        brokerages=brokerages
                    )
                    session.add(discovery_prefs)
                print(f"✅ Prepared {len(users_without_prefs)} new entries.")
            else:
                print("✅ No missing DiscoveryPreferences entries found.")

            # PHASE 2: Update existing entries where brokerages is NULL
            print("\n🔍 Phase 2: Checking for existing entries with NULL brokerages...")
            # Select DiscoveryPreferences where brokerages is null, and join with User to get their brokerage
            stmt_update = (
                select(DiscoveryPreferences, User.brokerage)
                .join(User, DiscoveryPreferences.user_id == User.id)
                .where(DiscoveryPreferences.brokerages == None)
            )
            
            result_update = await session.execute(stmt_update)
            prefs_to_update = result_update.all() # Returns list of tuples (DiscoveryPreferences, brokerage)
            
            if prefs_to_update:
                print(f"Found {len(prefs_to_update)} existing entries to update. Updating...")
                for prefs, user_brokerage in prefs_to_update:
                    if user_brokerage:
                        prefs.brokerages = [user_brokerage]
                print(f"✅ Updated {len(prefs_to_update)} existing entries.")
            else:
                print("✅ No existing entries need brokerage backfilling.")

            # Commit all changes
            await session.commit()
            print("\n🚀 All tasks completed successfully!")
            
        except Exception as e:
            await session.rollback()
            print(f"❌ Error during backfill: {e}")
            raise

if __name__ == "__main__":
    try:
        asyncio.run(backfill_discovery_preferences())
    except Exception as e:
        print(f"❌ An error occurred: {e}")
