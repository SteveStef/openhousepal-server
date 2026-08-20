"""
One-time historical backfill of SOLD/CLOSED Bright MLS listings for a specific city/state/date
range - used to seed enough real sold-comp data to test/build the AI-Generated CMA feature
against. The regular sync (property_sync_service.run_global_sync, every 5 minutes) never inserts
a listing that wasn't already ACTIVE/COMING SOON the first time it was seen - see
_sync_single_property's allow_inactive_insert parameter, which this script sets True to insert
CLOSED-BRIGHT rows too, reusing that method's mapping/dedup/reference-table logic rather than
duplicating it.

Hits the live Bright MLS API - run deliberately by hand, never as part of any automated pipeline.

Two-gate design so no single flag can trigger a real write:
  - `--city --state --date-from [--date-to]` alone -> COUNT ONLY (the default). A cheap
    $count=true probe - no property data or photos are ever transferred in this mode.
  - add `--run-pull --dry-run` (or just `--run-pull`, since --dry-run is the default once
    --run-pull is set) -> fetches full listing data + photos, prints what WOULD be inserted,
    writes nothing to the database.
  - add `--run-pull --no-dry-run` -> the actual write. Requires both flags together - there is
    no single flag that performs a real write.

Usage:
    python scripts/pull_sold_comps.py --city Villanova --state PA --date-from 2024-08-20
    python scripts/pull_sold_comps.py --city Villanova --state PA --date-from 2024-08-20 --run-pull
    python scripts/pull_sold_comps.py --city Villanova --state PA --date-from 2024-08-20 --run-pull --no-dry-run
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from dotenv import load_dotenv
load_dotenv(root_dir / ".env")

from app.database import AsyncSessionLocal
from app.services.bright_mls_service import bright_mls_service
from app.services.property_sync_service import PropertySyncService
from app.config.logging import get_logger

logger = get_logger(__name__)

PAGE_SIZE = 200


async def count_only(city: str, state: str, date_from: str, date_to: Optional[str]) -> None:
    count = await bright_mls_service.count_sold_properties(city, state, date_from, date_to)
    range_desc = f"CloseDate >= {date_from}" + (f" and <= {date_to}" if date_to else "")
    print(f"\n{count} CLOSED-BRIGHT listing(s) in {city}, {state} with {range_desc}.")
    print("No property data or photos were transferred - this was a count-only probe.")
    print("Re-run with --run-pull to see what a real pull would fetch (still no writes), or add --no-dry-run to actually import.\n")


async def run_pull(city: str, state: str, date_from: str, date_to: Optional[str], dry_run: bool) -> None:
    service = PropertySyncService()
    inserted = updated = skipped = errors = 0
    skip = 0

    async with AsyncSessionLocal() as db:
        while True:
            page = await bright_mls_service.get_sold_properties(city, state, date_from, date_to, top=PAGE_SIZE, skip=skip)
            if not page:
                break

            listing_keys = [str(row["ListingKey"]) for row in page if row.get("ListingKey")]
            photo_map = await bright_mls_service.get_media_for_properties(listing_keys)

            for raw in page:
                address = raw.get("UnparsedAddress") or raw.get("FullStreetAddress") or raw.get("ListingKey")
                photo_count = len(photo_map.get(str(raw.get("ListingKey")), []))
                if dry_run:
                    print(f"  WOULD INSERT: {address} | sold {raw.get('CloseDate')} for {raw.get('ClosePrice')} | {photo_count} photo(s)")
                    inserted += 1
                    continue
                try:
                    result = await service._sync_single_property(db, raw, photo_map, allow_inactive_insert=True)
                    if result is None:
                        skipped += 1
                    elif result["type"] == "NEW_GLOBAL":
                        inserted += 1
                    else:
                        updated += 1
                except Exception as exc:
                    errors += 1
                    logger.warning(f"Failed to sync {address}: {exc}")

            skip += PAGE_SIZE
            if len(page) < PAGE_SIZE:
                break

    label = "DRY RUN - " if dry_run else ""
    print(f"\n{label}Done. inserted={inserted} updated={updated} skipped={skipped} errors={errors}\n")


async def main(args: argparse.Namespace) -> None:
    try:
        if not args.run_pull:
            await count_only(args.city, args.state, args.date_from, args.date_to)
        else:
            await run_pull(args.city, args.state, args.date_from, args.date_to, args.dry_run)
    finally:
        await bright_mls_service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True, help="Two-letter state code, e.g. PA")
    parser.add_argument("--date-from", required=True, help="YYYY-MM-DD - CloseDate lower bound")
    parser.add_argument("--date-to", default=None, help="YYYY-MM-DD - CloseDate upper bound (default: no upper bound)")
    parser.add_argument("--run-pull", action="store_true", help="Fetch full listing data + photos instead of just counting. Still writes nothing unless --no-dry-run is also passed.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Required alongside --run-pull to actually write to the database.")
    parsed = parser.parse_args()
    asyncio.run(main(parsed))
