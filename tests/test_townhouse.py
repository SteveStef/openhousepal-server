import asyncio
import os
import sys
from typing import List

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.bright_mls_service import bright_mls_service

async def test_structure_design():
    """
    Query unique values of StructureDesignType to see how Townhouses are categorized.
    """
    print("Sampling StructureDesignType values in Residential properties...")
    
    try:
        # Fetch a sample of residential properties
        params = {
            "$filter": "PropertyType eq 'Residential' and MlsStatus eq 'ACTIVE-BRIGHT'",
            "$select": "StructureDesignType",
            "$top": 1000
        }
        
        data = await bright_mls_service._make_request("BrightProperties", params=params)
        
        design_types = {}
        for item in data.get("value", []):
            dt = item.get("StructureDesignType")
            if dt:
                design_types[dt] = design_types.get(dt, 0) + 1

        print("\nUnique StructureDesignType values found in 1000 Residential listings:")
        print(f"{'StructureDesignType':<30} | {'Count':<10}")
        print("-" * 45)
        for t, count in sorted(design_types.items(), key=lambda x: x[1], reverse=True):
            print(f"{str(t):<30} | {count:<10}")

        # Now try a targeted search for anything containing "Townhouse" or "Row"
        # in common fields we've seen
        print("\nSearching for 'Townhouse' or 'Row' in StructureDesignType...")
        targeted_filters = [
            "contains(StructureDesignType, 'Townhouse')",
            "contains(StructureDesignType, 'Row')",
            "contains(StructureDesignType, 'Attached')"
        ]
        
        for filt in targeted_filters:
            try:
                p = {"$filter": f"PropertyType eq 'Residential' and {filt}", "$top": 1, "$count": "true"}
                d = await bright_mls_service._make_request("BrightProperties", params=p)
                count = d.get("@odata.count", 0)
                print(f"Filter '{filt}': Found {count} results")
                if count > 0:
                     example = d.get("value")[0]
                     print(f"   Example: {example.get('StructureDesignType')}")
            except Exception as e:
                pass

    except Exception as e:
        print(f"Discovery failed: {str(e)}")

    await bright_mls_service.close()

if __name__ == "__main__":
    asyncio.run(test_structure_design())
