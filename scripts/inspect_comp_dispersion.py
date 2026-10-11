"""Read-only: dump each comp's address/price/sqft/psf/strength/distance for a report, to see
concretely which comp(s) are driving a wide anchor spread (e.g. a comp priced 2-3x the others).

Usage: python -m scripts.inspect_comp_dispersion <report_id>
"""
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import CmaReport
from app.database import DATABASE_URL


async def main(report_id: str):
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSessionLocal() as db:
        r = await db.get(CmaReport, report_id)
        if not r:
            print("Report not found.")
            return
        subject = r.subject or {}
        print(f"SUBJECT: {subject.get('street_address')}  sqft={subject.get('living_area') or subject.get('sqft')}  "
              f"beds={subject.get('bedrooms')}  baths={subject.get('bathrooms')}  year_built={subject.get('year_built')}  "
              f"lot={subject.get('lot_size')}")
        trace = ((r.pricing or {}).get("ai_estimate") or {}).get("valuation_trace") or {}
        anchor_trace = (trace.get("anchor") or {}).get("comps") or []
        by_key = {c.get("listing_key"): c for c in anchor_trace}
        print(f"\n{len(r.comparables or [])} comps:\n")
        for c in r.comparables or []:
            key = c.get("listing_key")
            at = by_key.get(key, {})
            print(f"- {c.get('street_address') or c.get('address')}")
            print(f"    sale_price={c.get('sale_price')}  list_price={c.get('list_price')}  price_per_sqft={c.get('price_per_sqft')}")
            print(f"    sqft={c.get('sqft') or c.get('living_area')}  beds={c.get('bedrooms')}  baths={c.get('bathrooms')}  year_built={c.get('year_built')}")
            print(f"    distance_miles={c.get('distance_miles')}  strength={c.get('strength')}  price_verified={c.get('price_verified')}")
            print(f"    relevance_score={c.get('relevance_score') or c.get('score')}  weight(anchor)={at.get('weight')}  price_adj_to_subject={at.get('price_adj_to_subject')}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
