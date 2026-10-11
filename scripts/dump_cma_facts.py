"""Read-only: dump a CMA report's subject/comp authoritative text fields + the AI commentary
text that's currently blocking export, so a false-positive vs. genuine-hallucination QA
finding can be told apart by eye.

Usage: python -m scripts.dump_cma_facts <report_id>
"""
import asyncio
import os
import sys
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import CmaReport
from app.database import DATABASE_URL

FACT_FIELDS = [
    "public_remarks", "remarks", "exterior_features", "interior_features",
    "lot_features", "condition", "systems", "appliances", "basement",
    "construction_materials", "roof", "heating", "cooling", "utilities",
    "year_built", "school_district",
]


async def dump(report_id: str):
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CmaReport).where(CmaReport.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            print("Not found.")
            return
        subject = report.subject or {}
        print("=== SUBJECT FACT FIELDS ===")
        for f in FACT_FIELDS:
            v = subject.get(f)
            if v:
                print(f"  {f}: {v}")

        print("\n=== COMPARABLES: ai_commentary/commentary ===")
        for c in (report.comparables or []):
            addr = c.get("street_address") or c.get("address")
            print(f"\n-- {addr} --")
            print("  ai_commentary:", (c.get("ai_commentary") or "")[:800])
            if c.get("commentary") != c.get("ai_commentary"):
                print("  commentary(edited):", (c.get("commentary") or "")[:800])

        estimate = (report.pricing or {}).get("ai_estimate") or {}
        print("\n=== METHODOLOGY SUMMARY ===")
        print((estimate.get("methodology_summary") or estimate.get("methodology") or "")[:2000])

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.dump_cma_facts <report_id>")
    else:
        asyncio.run(dump(sys.argv[1]))
