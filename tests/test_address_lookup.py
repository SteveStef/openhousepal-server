import asyncio
import os
import sys
import argparse
import json

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.services.property_service import property_service

async def test_address_lookup(address: str):
    """
    Test the get_property_by_address method in property_service.
    """
    print(f"\n--- Searching for Address: '{address}' ---")
    
    async with AsyncSessionLocal() as db:
        try:
            property_detail = await property_service.get_property_by_address(db, address)
            
            if property_detail:
                print("✅ Property Found!")
                print(f"Listing Key: {property_detail.listing_key}")
                print(f"Address:     {property_detail.street_address}")
                print(f"City:        {property_detail.city}")
                print(f"Zip:         {property_detail.zipcode}")
                print(f"Price:       ${property_detail.price:,.2f}" if property_detail.price else "Price: N/A")
                print(f"Status:      {property_detail.home_status}")
                print(f"Type:        {property_detail.home_type}")
                print(f"Beds/Baths:  {property_detail.bedrooms}/{property_detail.bathrooms}")
            else:
                print("❌ Property NOT Found in local database.")
                
        except Exception as e:
            print(f"⚠️ Error during lookup: {str(e)}")
            import traceback
            traceback.print_exc()

def main():
    asyncio.run(test_address_lookup("543 Foxglove Ln, WYNNEWOOD, PA 19096"))

if __name__ == "__main__":
    main()
