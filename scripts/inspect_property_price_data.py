import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import or_, select

from app.database import AsyncSessionLocal
from app.models.database import Property


PRICE_KEYS = [
    "ListingKey",
    "ListingId",
    "FullStreetAddress",
    "UnparsedAddress",
    "City",
    "StateOrProvince",
    "MlsStatus",
    "ListPrice",
    "OriginalListPrice",
    "PreviousListPrice",
    "PriceChangeTimestamp",
    "MajorChangeTimestamp",
    "MajorChangeType",
    "StatusChangeTimestamp",
    "ModificationTimestamp",
]


async def main() -> None:
    query = " ".join(sys.argv[1:]).strip() or "840 harriton"
    terms = [part for part in query.split() if part]

    async with AsyncSessionLocal() as db:
        conditions = []
        for term in terms:
            like = f"%{term}%"
            conditions.append(Property.full_address.ilike(like))
            conditions.append(Property.street_address.ilike(like))
            conditions.append(Property.unparsed_address.ilike(like))

        stmt = (
            select(Property)
            .where(or_(*conditions))
            .order_by(Property.updated_at.desc().nullslast())
            .limit(20)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        print(f"query={query!r} matches={len(rows)}")

        for prop in rows:
            raw = prop.raw_mls_data or {}
            payload = {
                "id": prop.id,
                "listing_key": prop.listing_key,
                "listing_id": prop.listing_id,
                "street_address": prop.street_address,
                "full_address": prop.full_address,
                "city": prop.city,
                "state": prop.state,
                "status": prop.home_status,
                "price_column": prop.price,
                "original_list_price_column": prop.original_list_price,
                "price_change_timestamp_column": prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                "raw_price_fields": {key: raw.get(key) for key in PRICE_KEYS if key in raw},
                "raw_price_related_keys": sorted(
                    key for key in raw.keys()
                    if "price" in key.lower() or "history" in key.lower() or "change" in key.lower()
                ),
            }
            print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
