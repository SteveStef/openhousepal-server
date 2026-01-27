import asyncio
import os
import json
import httpx
from datetime import datetime
from dotenv import load_dotenv
from app.services.bright_mls_service import BrightMlsService
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema

# Load environment variables
load_dotenv()

async def test_bright_service():
    print("Initializing BrightMlsService...")
    service = BrightMlsService()
    
    # Mock preferences
    preferences = CollectionPreferencesSchema(
        id="test-pref-id",
        collection_id="test-col-id",
        created_at=datetime.utcnow(),
        cities=["Villanova"],
        min_price=500000,
        max_price=2000000,
        min_beds=3,
        is_single_family=True
    )

    # 0. Broad Discovery Search
    print("\n--- 0. Discovery Search (No Filters) ---")
    try:
        # Check if we want to test PROD
        if os.getenv("BRIGHT_MLS_ENV", "").lower() == "prod":
            print("⚠️  WARNING: Running against PRODUCTION environment ⚠️")
            # Load prod env vars if needed
            from dotenv import dotenv_values
            prod_env = dotenv_values(".prod.env")
            if prod_env:
                os.environ["BRIGHT_MLS_CLIENT"] = prod_env.get("BRIGHT_MLS_CLIENT", "")
                os.environ["BRIGHT_MLS_SECRET"] = prod_env.get("BRIGHT_MLS_SECRET", "")
                # Re-init service with new env vars
                service = BrightMlsService()

        # Create empty preferences to get ANY property
        broad_prefs = CollectionPreferencesSchema(
            id="test-broad", collection_id="test-broad", created_at=datetime.utcnow()
        )
        # We need to manually call the internal method or pass empty lists
        # But get_matching_properties requires cities/townships to do anything
        # So we'll use the service's internal helper or just a direct URL for this test script context
        # Actually, let's just try to find *anything* in PA
        preferences.cities = [] # Clear city
        # preferences.townships = [] 
        # Note: The service might return empty if no location is provided. 
        # Let's try a very common city if Villanova fails, or just use the known ListingKey.
        
        # Strategy: Use the known ListingKey from previous debug session to verify details
        known_key = "240243016499" 
        print(f"Skipping broad search, using known valid ListingKey from debug: {known_key}")
        
    except Exception as e:
        print(f"Discovery failed: {e}")

    # 1. Test Search by Location (Villanova, PA)
    print("\n--- Testing Search by Location (Villanova, PA) ---")
    
    # Mock preferences
    preferences = CollectionPreferencesSchema(
        id="test-pref-id",
        collection_id="test-col-id",
        created_at=datetime.utcnow(),
        cities=["Villanova"],
        min_price=500000,
        max_price=2000000,
        min_beds=3,
        is_single_family=True
    )
    
    try:
        results = await service.get_matching_properties(preferences)
        print(f"Found {len(results)} properties in Villanova.")
        
        if results:
            first_prop = results[0]
            print("First property sample:")
            print(json.dumps(first_prop, indent=2, default=str))
            
            # Check for listing_key
            if 'listing_key' in first_prop:
                print(f"\n✅ listing_key present: {first_prop['listing_key']}")
            else:
                print("\n❌ listing_key MISSING!")
    except Exception as e:
        print(f"❌ Search failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response Body: {e.response.text}")

    # 2. Test Get Property by Address
    # Fetch a real address from the API first to ensure success
    print("\n--- Fetching a real address from API for testing ---")
    headers = {
        "Authorization": f"Bearer {await service._get_access_token()}",
        "Accept": "application/json"
    }
    # Get one active property
    url = f"{service.api_base_url}/BrightProperties?$top=1&$filter=MlsStatus eq 'ACTIVE-BRIGHT'"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        data = resp.json()
        if data.get('value'):
            real_prop = data['value'][0]
            test_address = real_prop.get('UnparsedAddress')
            print(f"Found real property at: {test_address}")
        else:
            test_address = "123 Main St" # Fallback
            print("Could not find any active properties in test DB.")

    print(f"\n--- Testing Get Property by Address: {test_address} ---")
    
    try:
        # Request details=True to get ResoFacts
        detail_response = await service.get_property_by_address(test_address, details=True)
        
        print("Property Details retrieved successfully!")
        # Convert Pydantic model to dict for printing
        if hasattr(detail_response, 'model_dump'):
            data = detail_response.model_dump()
        else:
            data = detail_response.dict()
            
        print(json.dumps(data, indent=2, default=str))
        
        # Verify ResoFacts
        if data.get('resoFacts'):
            print("\n✅ ResoFacts present.")
            print(f"Architectural Style: {data['resoFacts'].get('architectural_style')}")
            print(f"Tax Annual Amount: {data['resoFacts'].get('tax_annual_amount')}")
        else:
            print("\n❌ ResoFacts MISSING!")
            
        # Verify ListPictureURL usage
        # We can't see the internal API call here, but we can check if we got photos
        print(f"\nPhotos returned: {len(data.get('originalPhotos', []))}")

    except Exception as e:
        print(f"❌ Get Property failed: {e}")

if __name__ == "__main__":
    # Ensure client ID/Secret are set
    if not os.getenv("BRIGHT_MLS_CLIENT"):
        # Fallback for testing if .env isn't loaded correctly in this context
        # You might need to set these manually or ensure .env is correct
        print("WARNING: BRIGHT_MLS_CLIENT_ID not found in env. Logic might fail if not set.")
        
    asyncio.run(test_bright_service())
