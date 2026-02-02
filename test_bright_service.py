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
    print("\n--- Fetching a real address from API for testing ---")
    headers = {
        "Authorization": f"Bearer {await service._get_access_token()}",
        "Accept": "application/json"
    }
    # # Get one active property
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

        # Verify new Agent/Office fields
        print(f"\nOffice Name: {data.get('listOfficeName')}")
        print(f"Office Phone: {data.get('listOfficePhone')}")
        print(f"Agent Name: {data.get('listAgentFullName')}")
        print(f"Agent Email: {data.get('listAgentEmail')}")
            
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
