import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from app.services.bright_mls_service import BrightMlsService
from app.schemas.collection_preferences import CollectionPreferences

load_dotenv()

async def test_bright_mls_service():
    service = BrightMlsService()
    
    # Test 1: get_matching_properties
    print("--- Testing get_matching_properties ---")
    preferences = CollectionPreferences(
        id="test-id",
        collection_id="test-collection",
        created_at=datetime.now(),
        cities=["Philadelphia"],
        state="PA",
        min_price=100000,
        max_price=1000000,
        min_beds=1,
        is_single_family=True
    )
    
    try:
        properties = await service.get_matching_properties(preferences, max_properties=5)
        print(f"Found {len(properties)} properties.")
        for idx, p in enumerate(properties):
            print(f"{idx+1}. {p.get('address')}, {p.get('city')}, {p.get('state')} - ${p.get('price')}")
            
        if properties:
            # Test 2: get_property_by_address
            print("--- Testing get_property_by_address ---")
            first_property = properties[0]
            address = first_property.get('address')
            print(f"Fetching details for address: {address}")
            try:
                details = await service.get_property_by_address(address)
                print(f"Successfully fetched details for: {details.get('abbreviatedAddress')}")
                print(f"Price: {details.get('price')}")
                print(f"Beds/Baths: {details.get('bedrooms')}/{details.get('bathrooms')}")
                # print(f"Details JSON: {details.get('details')}")
            except Exception as e:
                print(f"Error in get_property_by_address: {e}")
        else:
            print("No properties found to test get_property_by_address.")
            
    except Exception as e:
        print(f"Error in get_matching_properties: {e}")

if __name__ == "__main__":
    asyncio.run(test_bright_mls_service())
