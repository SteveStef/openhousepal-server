import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import AsyncSessionLocal
from app.services.property_service import PropertyService


async def main() -> None:
    listing_key = sys.argv[1] if len(sys.argv) > 1 else "804648700996"
    async with AsyncSessionLocal() as db:
        prop = await PropertyService.get_property_by_listing_key(db, listing_key)
        if not prop:
            print(f"Property not found: {listing_key}")
            return
        data = prop.model_dump(by_alias=True, exclude_none=True)
        print(json.dumps({
            "ListingKey": data.get("ListingKey"),
            "FullAddress": data.get("FullAddress"),
            "BrightLatitude": data.get("Latitude"),
            "BrightLongitude": data.get("Longitude"),
            "GoogleRooftopLatitude": data.get("googleRooftopLatitude"),
            "GoogleRooftopLongitude": data.get("googleRooftopLongitude"),
            "GoogleLocationType": data.get("googleGeocodeLocationType"),
        }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
