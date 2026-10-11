"""Read-only diagnostic: trace the 5 selected comps on the 78 Jeri Ann Dr (NJ) CMA report from
raw Bright MLS fields (StandardStatus/MlsStatus/ClosePrice/CloseDate + alternates) through the
mapped Property row through to why the verified-sale gate rejected them.

Usage: python -m scripts.inspect_jeri_ann_comps
"""
import asyncio
import json
import os
import sys

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import CmaReport, Property
from app.database import DATABASE_URL


async def main():
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as db:
        # Filter in Python since JSON address matching differs across DB backends.
        result = await db.execute(select(CmaReport).order_by(CmaReport.created_at.desc()).limit(500))
        reports = result.scalars().all()
        matches = [
            r for r in reports
            if "jeri ann" in str((r.subject or {}).get("street_address", "")).lower()
        ]
        if not matches:
            print("No CMA report found with subject street_address containing 'Jeri Ann'.")
            return
        report = matches[0]
        print(f"Report {report.id}  status={report.status}  mode={report.mode}  created={report.created_at}")
        subject = report.subject or {}
        print(f"Subject: {subject.get('street_address')}, {subject.get('city')}, {subject.get('state')} {subject.get('zipcode')}")

        comps = report.comparables or []
        included = [c for c in comps if c.get("is_included", True)]
        print(f"\n{len(comps)} total comps stored, {len(included)} included.\n")

        listing_keys = [c.get("listing_key") for c in comps if c.get("listing_key")]
        prop_by_key = {}
        if listing_keys:
            pr = await db.execute(select(Property).where(Property.listing_key.in_(listing_keys)))
            prop_by_key = {p.listing_key: p for p in pr.scalars().all()}

        for i, c in enumerate(comps, 1):
            print("=" * 100)
            print(f"COMP #{i}: {c.get('street_address') or c.get('address')}  listing_key={c.get('listing_key')}")
            print("-- report.comparables (as stored on the CMA card) --")
            for f in ("status", "sale_price", "sale_date", "close_price", "close_date",
                      "price_verified", "list_price", "price_per_sqft"):
                print(f"   {f}: {c.get(f)!r}")

            prop = prop_by_key.get(c.get("listing_key"))
            if not prop:
                print("-- No matching Property row found for this listing_key --")
                continue

            raw = prop.raw_mls_data or {}
            print("-- Property row (mapped DB columns) --")
            print(f"   home_status (mapped from MlsStatus): {prop.home_status!r}")
            print(f"   updated_at: {prop.updated_at}")

            print("-- raw_mls_data (authoritative Bright fields) --")
            for f in ("StandardStatus", "MlsStatus", "ClosePrice", "CloseDate",
                      "PurchaseContractDate", "ContractStatusChangeDate", "ContractStatusChangeTimestamp",
                      "StatusChangeTimestamp", "CloseDateTime", "OffMarketDate", "ListingContractDate"):
                print(f"   {f}: {raw.get(f)!r}")

            # Replicate _categorize_status + the verified-sale gate exactly as cma_service.py does.
            from app.services.cma_service import _categorize_status, _clean_status
            category = _categorize_status(prop.home_status)
            print(f"-- Verifier trace --")
            print(f"   _clean_status(home_status) = {_clean_status(prop.home_status)!r}")
            print(f"   _categorize_status -> category = {category!r}")
            raw_close = raw.get('ClosePrice')
            try:
                close_price_val = float(raw_close) if raw_close not in (None, "", 0, "0") else None
            except (TypeError, ValueError):
                close_price_val = None
            price_verified = bool(category == 'sold' and close_price_val and close_price_val > 0)
            reasons = []
            if category != 'sold':
                reasons.append(f"category != 'sold' (got {category!r}) -> home_status/MlsStatus did not match CLOSED/SOLD prefix")
            if not close_price_val:
                reasons.append("ClosePrice missing/zero/unparseable in raw_mls_data")
            print(f"   => price_verified = {price_verified}" + (f"   REJECTED because: {'; '.join(reasons)}" if reasons else ""))

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
