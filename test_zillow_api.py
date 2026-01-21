import asyncio
import os
import sys
from dotenv import load_dotenv
import logging

# Add the current directory to sys.path so we can import from app
sys.path.append(os.getcwd())

# Load environment variables
load_dotenv()

# Configure logging to see output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from app.services.zillow_working_service import ZillowWorkingService
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema

async def main():
    service = ZillowWorkingService()
    
    location = "Villanova, PA; Radnor Township, PA"
    
    print(f"Testing Zillow API search for location: {location}")
    print("-" * 50)

    # Mock preferences based on the log context (we don't know the exact filters, but we can try broad ones first)
    # The logs said "Batch returned 0 results"
    
    from datetime import datetime
    
    preferences = CollectionPreferencesSchema(
        id="test-pref-id",
        collection_id="test",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        min_price=None,
        max_price=None,
        min_beds=None,
        min_baths=None,
        cities=[],
        townships=[],
        lat=0,
        long=0,
        address="",
        diameter=0,
        is_single_family=True,
        is_town_house=True,
        is_condo=True,
        is_multi_family=True,
        is_lot_land=True,
        is_apartment=True
    )

    try:
        # 1. Call search_properties_by_location directly to see the raw API response structure
        print("Calling search_properties_by_location...")
        raw_response = await service.search_properties_by_location(location, preferences)
        
        print("\nRaw Response Keys:", raw_response.keys())
        
        results = raw_response.get('searchResults', [])
        print(f"Number of results found: {len(results)}")
        
        if len(results) > 0:
            print("\nFirst result sample:")
            print(results[0])
        else:
            print("\nNo results found in raw response.")
            # Print full response if no results, to check for errors/messages
            print("Full Raw Response:", raw_response)

    except Exception as e:
        print(f"\nError occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
