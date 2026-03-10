import asyncio
import os
import sys
import uuid
from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func

# Add the server directory to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, Collection, CollectionPreferences, User
from app.services.property_service import PropertyService
from app.services.property_sync_service import PropertySyncService
from app.schemas.collection_preferences import CollectionPreferencesBase

async def get_sample_properties(db: AsyncSession, limit: int = 5) -> List[Property]:
    """Fetch a few real properties to use as test anchors."""
    stmt = select(Property).where(Property.latitude.isnot(None)).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()

async def test_consistency(db: AsyncSession, name: str, preferences_dict: Dict[str, Any], anchor_property: Property):
    """
    Verifies that for a given set of preferences:
    1. PropertyService (Search) finds the anchor property.
    2. PropertySyncService (Sync) matches the anchor property to the collection.
    """
    print(f"\n>>> Testing Scenario: {name}")
    
    # 1. Create a temporary collection & preferences for this test
    # We use a real user if one exists, otherwise just a random string
    col_id = str(uuid.uuid4())
    collection = Collection(
        id=col_id,
        name=f"Consistency Test {name}",
        owner_id=str(uuid.uuid4()) # Dummy owner
    )
    db.add(collection)
    await db.flush()
    
    prefs = CollectionPreferences(
        collection_id=col_id,
        **preferences_dict
    )
    db.add(prefs)
    await db.commit()
    
    try:
        # 2. TEST SEARCH SERVICE
        prefs_schema = CollectionPreferencesBase(**preferences_dict)
        search_results = await PropertyService.get_properties_by_preferences(db, prefs_schema)
        search_ids = {p.id for p in search_results}
        
        found_in_search = anchor_property.id in search_ids
        print(f"    - Search Service: {'✅ Found' if found_in_search else '❌ Not Found'} anchor property")

        # 3. TEST SYNC SERVICE
        sync_service = PropertySyncService()
        # Mock the property data as it would come from the sync engine
        p_data = {
            "listing_key": anchor_property.listing_key,
            "city": anchor_property.city,
            "township": anchor_property.township,
            "school_district": anchor_property.school_district_name,
            "state": anchor_property.state,
            "lat": anchor_property.latitude,
            "lng": anchor_property.longitude,
            "price": anchor_property.price,
            "beds": anchor_property.bedrooms,
            "baths": anchor_property.bathrooms,
            "year_built": anchor_property.year_built,
            "home_type": anchor_property.home_type
        }
        
        matching_collections = await sync_service.get_matching_collections(
            db, p_data, p_data["city"], p_data["township"], p_data["school_district"], 
            p_data["state"], p_data["lat"], p_data["lng"], 
            p_data["price"], p_data["beds"], p_data["baths"]
        )
        
        found_in_sync = any(c.id == col_id for c in matching_collections)
        print(f"    - Sync Service:   {'✅ Matched' if found_in_sync else '❌ No Match'} to collection")

        # 4. VALIDATE CONSISTENCY
        if found_in_search == found_in_sync:
            print(f"    ✅ RESULT: CONSISTENT")
            return True
        else:
            print(f"    ❌ RESULT: DISPARITY DETECTED!")
            return False
            
    finally:
        # Cleanup temporary collection
        await db.execute(delete(Collection).where(Collection.id == col_id))
        await db.commit()

async def main():
    async with AsyncSessionLocal() as db:
        samples = await get_sample_properties(db)
        if not samples:
            print("No properties found in database to test with.")
            return

        prop = samples[0]
        print(f"Using Anchor Property: {prop.street_address}, {prop.city} ({prop.id})")
        
        results = []
        
        # Test 1: City Match
        results.append(await test_consistency(db, "City Match", {
            "cities": [f"{prop.city}, {prop.state}"],
            "diameter": None
        }, prop))
        
        # Test 2: Radius Match
        results.append(await test_consistency(db, "Radius Match", {
            "lat": prop.latitude,
            "long": prop.longitude,
            "diameter": 2.0, # 1 mile radius
            "cities": []
        }, prop))
        
        # Test 3: Exclusive logic (Should match via City, ignore Radius)
        results.append(await test_consistency(db, "Exclusive (City > Radius)", {
            "cities": [f"{prop.city}, {prop.state}"],
            "lat": prop.latitude + 5.0, # Center radius 350 miles away
            "long": prop.longitude + 5.0,
            "diameter": 10.0
        }, prop))

        if all(results):
            print("\n" + "="*40)
            print("ALL GEOGRAPHIC CONSISTENCY TESTS PASSED!")
            print("="*40)
        else:
            print("\n" + "!"*40)
            print("CONSISTENCY TESTS FAILED!")
            print("!"*40)
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
