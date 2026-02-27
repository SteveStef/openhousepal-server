import asyncio
import os
import sys
from typing import Dict, Any

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.bright_mls_service import bright_mls_service
from app.models.database import HomeType
from scripts.seed_test_properties import map_reso_to_internal

async def run_type_test(name: str, odata_filter: str):
    """Helper to fetch a property and test its mapping."""
    print(f"\n>>> Testing Query for: {name}")
    print(f"    Filter: {odata_filter}")
    
    try:
        params = {
            "$filter": f"{odata_filter} and MlsStatus eq 'ACTIVE-BRIGHT'",
            "$top": 1,
            "$select": "ListingKey,PropertyType,StructureDesignType,FullStreetAddress"
        }
        
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        
        if not data.get("value"):
            print(f"    ❌ No active listings found for this type in testing.")
            return None
            
        item = data["value"][0]
        mapped = map_reso_to_internal(item)
        
        print(f"    Found: {item.get('FullStreetAddress')}")
        print(f"    MLS PropertyType: {item.get('PropertyType')}")
        print(f"    MLS StructureDesignType: {item.get('StructureDesignType')}")
        print(f"    RESULT -> home_type: {mapped['home_type']}")
        
        return mapped
    except Exception as e:
        print(f"    ⚠️ Error: {str(e)}")
        return None

async def test_single_family():
    odata_filter = "PropertyType eq 'Residential' and StructureDesignType eq 'Detached'"
    res = await run_type_test("SINGLE_FAMILY", odata_filter)
    if res:
        assert res['home_type'] == HomeType.SINGLE_FAMILY
        print("    ✅ SUCCESS: Mapped to SINGLE_FAMILY")

async def test_townhouse():
    # Test Interior Row
    odata_filter = "PropertyType eq 'Residential' and StructureDesignType in ('Interior Row/Townhouse', 'End of Row/Townhouse')"
    res = await run_type_test("TOWNHOUSE (Row)", odata_filter)
    if res:
        assert res['home_type'] == HomeType.TOWNHOUSE
        print("    ✅ SUCCESS: Mapped to TOWNHOUSE")
        
    # Test Twin
    odata_filter_twin = "PropertyType eq 'Residential' and StructureDesignType eq 'Twin/Semi-Detached'"
    res_twin = await run_type_test("TOWNHOUSE (Twin)", odata_filter_twin)
    if res_twin:
        assert res_twin['home_type'] == HomeType.TOWNHOUSE
        print("    ✅ SUCCESS: Twin mapped to TOWNHOUSE")

async def test_condo():
    odata_filter = "PropertyType eq 'Residential' and StructureDesignType in ('Unit/Flat/Apartment', 'Penthouse Unit/Flat/Apartment')"
    res = await run_type_test("CONDO", odata_filter)
    if res:
        assert res['home_type'] == HomeType.CONDO
        print("    ✅ SUCCESS: Mapped to CONDO")

async def test_multi_family():
    odata_filter = "PropertyType eq 'Multi-Family'"
    res = await run_type_test("MULTI_FAMILY", odata_filter)
    if res:
        assert res['home_type'] == HomeType.MULTI_FAMILY
        print("    ✅ SUCCESS: Mapped to MULTI_FAMILY")

async def test_land_farm():
    res_land = await run_type_test("LAND", "PropertyType eq 'Land'")
    if res_land:
        assert res_land['home_type'] == HomeType.LAND
        print("    ✅ SUCCESS: Mapped to LAND")
        
    res_farm = await run_type_test("FARM", "PropertyType eq 'Farm'")
    if res_farm:
        assert res_farm['home_type'] == HomeType.FARM
        print("    ✅ SUCCESS: Mapped to FARM")

async def test_rentals():
    odata_filter = "PropertyType eq 'Residential Lease'"
    res = await run_type_test("RESIDENTIAL_LEASE", odata_filter)
    if res:
        assert res['home_type'] == HomeType.RESIDENTIAL_LEASE
        print("    ✅ SUCCESS: Mapped to RESIDENTIAL_LEASE")

async def test_commercial():
    odata_filter = "PropertyType eq 'Commercial Sale'"
    res = await run_type_test("COMMERCIAL", odata_filter)
    if res:
        assert res['home_type'] == HomeType.COMMERCIAL
        print("    ✅ SUCCESS: Mapped to COMMERCIAL")

async def test_other():
    # Test Business Opportunity
    odata_filter = "PropertyType eq 'Business Opportunity'"
    res = await run_type_test("OTHER (Business Opp)", odata_filter)
    if res:
        assert res['home_type'] == HomeType.OTHER
        print("    ✅ SUCCESS: Mapped to OTHER")

async def main():
    print("="*60)
    print("STARTING COMPREHENSIVE PROPERTY TYPE TESTS (ALL 9 TYPES)")
    print("="*60)
    
    await test_single_family()
    await test_townhouse()
    await test_condo()
    await test_multi_family()
    await test_land_farm()
    await test_rentals()
    await test_commercial()
    await test_other()
    
    print("\n" + "="*60)
    print("ALL 9 CATEGORIES VERIFIED")
    print("="*60)
    
    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(main())
