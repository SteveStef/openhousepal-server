import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.database import Property
from app.services.bright_mls_service import bright_mls_service


async def local_active_listings(limit: int) -> list[dict]:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Property)
            .where(Property.home_status == "ACTIVE-BRIGHT")
            .order_by(Property.updated_at.desc().nullslast())
            .limit(limit)
        )
        result = await db.execute(stmt)
        listings = []
        for prop in result.scalars().all():
            listings.append({
                "ListingKey": prop.listing_key,
                "ListingId": prop.listing_id,
                "FullStreetAddress": prop.street_address,
                "UnparsedAddress": prop.unparsed_address,
            })
        return listings


async def main() -> None:
    limit = int(os.getenv("BRIGHT_PRICE_HISTORY_PROBE_LIMIT", "10"))
    listing_key_arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    listings = [{"ListingKey": listing_key_arg, "ListingId": None, "FullStreetAddress": listing_key_arg}] if listing_key_arg else await local_active_listings(limit)
    if not listings:
        print("No active local Bright listings found for the price-history probe.")
        return

    print(f"Checking Bright price-history data for {len(listings)} active local listing(s).\n")
    discovered_resources = await bright_mls_service.discover_price_history_resources()
    print("History-like Bright resources discovered from $metadata:")
    print(json.dumps(discovered_resources, indent=2))
    print()

    for listing in listings:
        listing_key = str(listing.get("ListingKey") or "")
        listing_id = listing.get("ListingId")
        address = listing.get("FullStreetAddress") or listing.get("UnparsedAddress") or listing_key
        print(f"Listing: {address} | ListingKey={listing_key} | ListingId={listing_id}")
        result = await bright_mls_service.probe_price_history_for_listing(listing_key, listing_id)
        print(f"Matched resource: {result.get('resource')}")
        print(f"Matched filter: {result.get('filter')}")
        rows = result.get("rows") or []
        print(f"Rows returned: {len(rows)}")
        if rows:
            print(json.dumps(rows[:5], indent=2, default=str))
        else:
            print("Attempts:")
            print(json.dumps(result.get("attempts", [])[:20], indent=2, default=str))
            price_change_fields = await bright_mls_service.get_price_change_fields_for_listing(listing_key)
            print("BrightProperties price-change fields:")
            print(json.dumps(price_change_fields, indent=2, default=str))
        print("-" * 80)

    await bright_mls_service.close()


if __name__ == "__main__":
    asyncio.run(main())
