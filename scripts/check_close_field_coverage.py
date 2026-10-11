"""Read-only diagnostic: across all CLOSED-BRIGHT Property rows, how often is raw_mls_data.ClosePrice
/CloseDate/StandardStatus actually populated, broken down by state - to tell whether the 78 Jeri Ann
Dr (NJ) comps' missing close fields are an app-wide mapping bug or specific to this market/board.

Usage: python -m scripts.check_close_field_coverage
"""
import asyncio
import os
import sys
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import Property
from app.database import DATABASE_URL


async def main():
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Property.state, Property.raw_mls_data, Property.listing_key, Property.home_status)
            .where(Property.home_status.ilike('CLOSED%'))
            .limit(20000)
        )
        rows = result.all()
        stats = defaultdict(lambda: {"total": 0, "has_close_price": 0, "has_close_date": 0, "has_standard_status": 0})
        examples_missing = defaultdict(list)
        examples_present = defaultdict(list)
        for state, raw, listing_key, home_status in rows:
            raw = raw or {}
            st = stats[state]
            st["total"] += 1
            has_cp = raw.get("ClosePrice") not in (None, "", 0, "0")
            has_cd = raw.get("CloseDate") not in (None, "")
            has_ss = raw.get("StandardStatus") not in (None, "")
            if has_cp:
                st["has_close_price"] += 1
            if has_cd:
                st["has_close_date"] += 1
            if has_ss:
                st["has_standard_status"] += 1
            if not has_cp and len(examples_missing[state]) < 3:
                examples_missing[state].append(listing_key)
            if has_cp and len(examples_present[state]) < 3:
                examples_present[state].append(listing_key)

        print(f"{'State':<8}{'Total':<8}{'ClosePrice%':<14}{'CloseDate%':<14}{'StandardStatus%':<16}")
        for state, st in sorted(stats.items(), key=lambda kv: -kv[1]["total"]):
            t = st["total"] or 1
            print(f"{str(state):<8}{st['total']:<8}"
                  f"{st['has_close_price']}/{t} ({100*st['has_close_price']/t:.0f}%)".ljust(14) + "  "
                  f"{st['has_close_date']}/{t} ({100*st['has_close_date']/t:.0f}%)".ljust(14) + "  "
                  f"{st['has_standard_status']}/{t} ({100*st['has_standard_status']/t:.0f}%)")
        print("\nExample listing_keys MISSING ClosePrice by state:")
        for state, keys in examples_missing.items():
            print(f"  {state}: {keys}")
        print("\nExample listing_keys WITH ClosePrice by state:")
        for state, keys in examples_present.items():
            print(f"  {state}: {keys}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
