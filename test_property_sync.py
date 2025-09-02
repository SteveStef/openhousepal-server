#!/usr/bin/env python3
"""
Simple test script for PropertySyncService functionality.
Tests the sync service using existing collections in the database.
"""

import asyncio
import os
import sys
from datetime import datetime

# Add the app directory to Python path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from app.database import AsyncSessionLocal, init_db
from app.models.database import Collection, CollectionPreferences, collection_properties, Property
from app.services.property_sync_service import PropertySyncService
from sqlalchemy import select, func


async def get_existing_collections(db):
    """Get all existing collections with their preferences"""
    print("Finding existing collections...")
    
    result = await db.execute(
        select(Collection, CollectionPreferences)
        .join(CollectionPreferences, isouter=True)
        .order_by(Collection.created_at.desc())
    )
    collections_data = result.fetchall()
    
    print(f"Found {len(collections_data)} collections in database")
    
    collections_with_prefs = []
    collections_without_prefs = []
    
    for collection, preferences in collections_data:
        if preferences:
            collections_with_prefs.append((collection, preferences))
        else:
            collections_without_prefs.append(collection)
    
    print(f"  - {len(collections_with_prefs)} collections have preferences (can be synced)")
    print(f"  - {len(collections_without_prefs)} collections have no preferences (cannot be synced)")
    
    return collections_with_prefs, collections_without_prefs


async def display_collection_info(db, collection, preferences=None):
    """Display detailed information about a collection"""
    print(f"\n--- Collection Details ---")
    print(f"ID: {collection.id}")
    print(f"Name: {collection.name}")
    print(f"Status: {collection.status}")
    print(f"Visitor: {collection.visitor_name} ({collection.visitor_email})")
    print(f"Created: {collection.created_at}")
    
    if preferences:
        print(f"\n--- Preferences ---")
        print(f"Price Range: ${preferences.min_price:,} - ${preferences.max_price:,}")
        print(f"Beds: {preferences.min_beds} - {preferences.max_beds}")
        print(f"Baths: {preferences.min_baths} - {preferences.max_baths}")
        print(f"Location: {preferences.lat}, {preferences.long} (radius: {preferences.diameter} miles)")
        if preferences.special_features:
            print(f"Features: {preferences.special_features}")
    
    # Count existing properties in collection
    property_count_result = await db.execute(
        select(func.count(collection_properties.c.property_id))
        .where(collection_properties.c.collection_id == collection.id)
    )
    property_count = property_count_result.scalar() or 0
    print(f"Current properties in collection: {property_count}")


async def choose_collection_to_test(collections_with_prefs):
    """Let user choose which collection to test"""
    if not collections_with_prefs:
        print("❌ No collections with preferences found. Cannot test sync.")
        return None, None
    
    print(f"\n=== Available Collections for Testing ===")
    for i, (collection, preferences) in enumerate(collections_with_prefs, 1):
        print(f"{i}. {collection.name[:50]} (ID: {collection.id[:8]}...)")
        print(f"   Status: {collection.status}, Visitor: {collection.visitor_name}")
    
    while True:
        try:
            choice = input(f"\nChoose collection to test (1-{len(collections_with_prefs)}, or 'all' for all collections): ").strip()
            
            if choice.lower() == 'all':
                return 'all', collections_with_prefs
            
            choice_num = int(choice)
            if 1 <= choice_num <= len(collections_with_prefs):
                return collections_with_prefs[choice_num - 1]
            else:
                print(f"Please enter a number between 1 and {len(collections_with_prefs)}")
        except ValueError:
            print("Please enter a valid number or 'all'")
        except KeyboardInterrupt:
            print("\nTest cancelled.")
            return None, None


async def test_sync_single_collection(collection_id: str):
    """Test syncing a single collection"""
    print(f"\n=== Testing Single Collection Sync ===")
    print(f"Collection ID: {collection_id}")
    
    sync_service = PropertySyncService()
    result = await sync_service.sync_single_collection(collection_id)
    
    print(f"Sync Result: {result}")
    
    if result['success']:
        print(f"✓ Successfully synced collection!")
        print(f"✓ Properties added: {result['new_properties_added']}")
        print(f"✓ Synced at: {result['synced_at']}")
    else:
        print(f"✗ Sync failed: {result['error']}")
    
    return result


async def test_sync_all_collections():
    """Test syncing all active collections"""
    print(f"\n=== Testing All Collections Sync ===")
    
    sync_service = PropertySyncService()
    result = await sync_service.sync_all_active_collections()
    
    print(f"Sync Results Summary:")
    print(f"  Success: {result['success']}")
    print(f"  Collections processed: {result['collections_processed']}")
    print(f"  Total new properties: {result['total_new_properties']}")
    print(f"  Duration: {result.get('duration_seconds', 0):.2f} seconds")
    
    if result['errors']:
        print(f"  Errors: {len(result['errors'])}")
        for error in result['errors']:
            print(f"    - {error}")
    else:
        print(f"  ✓ No errors!")
    
    return result


async def show_collection_properties_summary(db, collection_id: str):
    """Show a summary of properties in the collection after sync"""
    print(f"\n=== Collection Properties Summary ===")
    
    try:
        # Get property count and sample properties
        property_result = await db.execute(
            select(Property.street_address, Property.price, Property.bedrooms, Property.bathrooms)
            .join(collection_properties)
            .where(collection_properties.c.collection_id == collection_id)
            .limit(5)
        )
        properties = property_result.fetchall()
        
        total_count_result = await db.execute(
            select(func.count(collection_properties.c.property_id))
            .where(collection_properties.c.collection_id == collection_id)
        )
        total_count = total_count_result.scalar() or 0
        
        print(f"Total properties in collection: {total_count}")
        
        if properties:
            print(f"\nSample properties:")
            for prop in properties:
                price_str = f"${prop.price:,}" if prop.price else "Price N/A"
                beds_baths = f"{prop.bedrooms}bed/{prop.bathrooms}bath" if prop.bedrooms and prop.bathrooms else "N/A"
                print(f"  - {prop.street_address} - {price_str} - {beds_baths}")
            
            if total_count > 5:
                print(f"  ... and {total_count - 5} more properties")
        
    except Exception as e:
        print(f"✗ Error getting collection summary: {e}")


async def show_test_instructions():
    """Show instructions for testing the fixed sync"""
    print(f"\n" + "="*60)
    print(f"🔧 PROPERTY SYNC FIXES APPLIED!")
    print(f"="*60)
    print(f"Fixed field mapping issues that were preventing property saves:")
    print(f"  ✅ 'address' → 'street_address'")
    print(f"  ✅ 'image_url' → 'img_src'") 
    print(f"  ✅ 'last_updated' → 'last_synced'")
    print(f"  ✅ 'zpid' string → integer conversion")
    print(f"  ✅ Added complete 'zillow_data' storage")
    print(f"\nThe sync should now work correctly!")
    print(f"="*60)


async def check_environment():
    """Check if required environment variables are set"""
    print("=== Environment Check ===")
    
    rapid_api_key = os.getenv("RAPID_API_KEY")
    if rapid_api_key:
        print(f"✓ RAPID_API_KEY is set (ends with: ...{rapid_api_key[-4:]})")
    else:
        print("✗ RAPID_API_KEY not set - Zillow API calls will fail")
        print("  Set it with: export RAPID_API_KEY=your_key")
        return False
    
    database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./collections.db")
    print(f"✓ Database URL: {database_url}")
    
    return True


async def main():
    """Main test function"""
    print("🏠 PropertySyncService Test Script")
    print("=" * 50)
    
    # Show fix information
    await show_test_instructions()
    
    # Check environment
    if not await check_environment():
        print("\n❌ Environment check failed. Please set required environment variables.")
        return
    
    # Initialize database
    await init_db()
    
    try:
        async with AsyncSessionLocal() as db:
            # Get existing collections
            collections_with_prefs, collections_without_prefs = await get_existing_collections(db)
            
            if not collections_with_prefs:
                print("\n❌ No collections with preferences found in database.")
                print("   Create a collection through the app first, then run this test.")
                return
            
            # Let user choose what to test
            choice_result = await choose_collection_to_test(collections_with_prefs)
            if choice_result[0] is None:
                return
            
            if choice_result[0] == 'all':
                # Test all collections sync
                print(f"\n=== Testing All Collections Sync ===")
                all_result = await test_sync_all_collections()
                
                print(f"\n=== Test Summary ===")
                print(f"All collections sync: {'✓ Success' if all_result['success'] else '✗ Failed'}")
                
                if all_result['success'] and all_result['total_new_properties'] > 0:
                    print(f"✓ Added {all_result['total_new_properties']} new properties across all collections")
                
            else:
                # Test single collection
                collection, preferences = choice_result
                
                # Display collection info
                await display_collection_info(db, collection, preferences)
                
                # Ask for confirmation
                confirm = input(f"\nProceed with sync test for this collection? (y/N): ").lower()
                if confirm != 'y':
                    print("Test cancelled.")
                    return
                
                # Test single collection sync
                single_result = await test_sync_single_collection(collection.id)
                
                print(f"\n=== Test Summary ===")
                print(f"Single collection sync: {'✓ Success' if single_result['success'] else '✗ Failed'}")
                
                if single_result['success']:
                    if single_result['new_properties_added'] > 0:
                        print(f"✓ Added {single_result['new_properties_added']} new properties")
                        
                        # Show collection summary
                        await show_collection_properties_summary(db, collection.id)
                    else:
                        print("ℹ No new properties added (may already be up to date)")
                else:
                    print(f"✗ Error: {single_result.get('error', 'Unknown error')}")
    
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # Check if we're in the right directory
    if not os.path.exists("app"):
        sys.exit(1)
    
    # Run the test
    asyncio.run(main())
