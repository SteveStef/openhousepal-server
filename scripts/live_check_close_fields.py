"""Read-only, bounded live verification (no DB writes): for a small fixed list of ListingKeys,
query Bright's BrightProperties resource TWICE -
  (a) without scoping the $filter to closed status (mirrors what the general modified-since sync
      sends), and
  (b) WITH the $filter scoped to MlsStatus eq 'CLOSED-BRIGHT' (mirrors get_closed_properties_
      modified_since) -
and print StandardStatus/ClosePrice/CloseDate from each response. This is 5 x 2 = 10 single-record
$top=1 reads, same lightweight pattern already used by get_property_by_listing_key for CMA subject
refresh - no bulk crawl, no writes, no Discord webhook.

Usage: python -m scripts.live_check_close_fields
"""
import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from app.services.bright_mls_service import bright_mls_service
from app.utils.mls_mapper import BRIGHT_PROPERTY_SELECT_FIELDS

LISTING_KEYS = [
    "804622551592",  # 304 Morris Blvd
    "804854665530",  # 82 Jeri Ann Dr
    "804563805124",  # 51 Barry Lane
    "804701495774",  # 1951 Mill Creek Road
    "804967714020",  # 41 Harry Drive
]

FIELDS = ",".join(BRIGHT_PROPERTY_SELECT_FIELDS)


async def probe(listing_key: str, scoped: bool):
    filt = f"ListingKey eq {listing_key}"
    if scoped:
        filt += " and MlsStatus eq 'CLOSED-BRIGHT'"
    params = {"$filter": filt, "$top": 1, "$select": FIELDS}
    data = await bright_mls_service._make_request("BrightProperties", params=params)
    rows = data.get("value", [])
    return rows[0] if rows else None


async def main():
    for key in LISTING_KEYS:
        print("=" * 90)
        print(f"ListingKey {key}")
        for scoped, label in ((False, "UNSCOPED (general modified-since style)"), (True, "SCOPED to MlsStatus eq CLOSED-BRIGHT")):
            try:
                row = await probe(key, scoped)
            except Exception as exc:
                print(f"  [{label}] ERROR: {exc}")
                continue
            if row is None:
                print(f"  [{label}] No row returned.")
                continue
            print(f"  [{label}]")
            for f in ("MlsStatus", "StandardStatus", "ClosePrice", "CloseDate",
                      "PurchaseContractDate", "ContractStatusChangeDate", "ModificationTimestamp"):
                print(f"     {f}: {row.get(f)!r}")
    await bright_mls_service.close()


if __name__ == "__main__":
    asyncio.run(main())
