import asyncio
import json
import os
import sys
import argparse
from datetime import datetime

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.services.bright_mls_service import BrightMlsService

async def query_bright_by_address(street: str, city: str = None, state: str = None, zipcode: str = None, exact: bool = False):
    """
    Query Bright MLS for a property by specific address components.
    """
    print(f"\n--- Querying Bright MLS ---")
    print(f"Street:  {street}")
    if city: print(f"City:    {city}")
    if state: print(f"State:   {state}")
    if zipcode: print(f"Zip:     {zipcode}")
    
    service = BrightMlsService()
    try:
        # Build components of the filter
        filters = []
        
        # Street search (primarily using FullStreetAddress)
        if exact:
            street_filter = f"FullStreetAddress eq '{street}'"
        else:
            street_filter = f"contains(FullStreetAddress, '{street}')"
        filters.append(street_filter)
        
        if city:
            filters.append(f"City eq '{city.upper()}'")
        if state:
            filters.append(f"StateOrProvince eq '{state.upper()}'")
        if zipcode:
            filters.append(f"PostalCode eq '{zipcode}'")
            
        filter_str = " and ".join(filters)
        
        params = {
            "$filter": filter_str,
            "$top": 5
        }
        
        print(f"\nFilter: {filter_str}")
        
        data = await service._make_request("BrightProperties", params=params)
        unique_properties = data.get("value", [])
        
        if unique_properties:
            print(f"✅ Found {len(unique_properties)} matching properties.\n")
            for i, prop in enumerate(unique_properties):
                print(f"--- Result {i+1} ---")
                print(f"Listing Key:    {prop.get('ListingKey')}")
                print(f"Listing ID:     {prop.get('ListingId')}")
                print(f"Address:        {prop.get('FullStreetAddress')}")
                print(f"Unparsed Addr:  {prop.get('UnparsedAddress')}")
                print(f"Status:         {prop.get('MlsStatus')}")
                print(f"Price:          ${prop.get('ListPrice', 0):,.2f}")
                print(f"City/State/Zip: {prop.get('City')}, {prop.get('StateOrProvince')} {prop.get('PostalCode')}")
                print(f"Last Modified:  {prop.get('ModificationTimestamp')}")
                print("-" * 20)
                
                # Save to a file for full detail inspection
                output_dir = os.path.join(os.path.dirname(__file__), "outputs")
                os.makedirs(output_dir, exist_ok=True)
                output_file = os.path.join(output_dir, f"property_details_{prop.get('ListingKey')}.json")
                with open(output_file, "w") as f:
                    json.dump(prop, f, indent=4)
                print(f"Full details saved to {output_file}")
        else:
            print("❌ No properties found matching those criteria in Bright MLS.")
            
    except Exception as e:
        print(f"⚠️ Error during Bright MLS query: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        await service.close()

def main():
    parser = argparse.ArgumentParser(description="Query Bright MLS by address components.")
    parser.add_argument("--street", required=True, help="The street address (e.g., '123 Main St')")
    parser.add_argument("--city", help="The city (e.g., 'WYNNEWOOD')")
    parser.add_argument("--state", help="The state abbreviation (e.g., 'PA')")
    parser.add_argument("--zip", help="The postal code (e.g., '19096')")
    parser.add_argument("--exact", action="store_true", help="Perform an exact match for the street")
    
    args = parser.parse_args()
    
    asyncio.run(query_bright_by_address(args.street, args.city, args.state, args.zip, args.exact))

if __name__ == "__main__":
    main()
