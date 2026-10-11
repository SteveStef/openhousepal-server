"""Read-only, no AI calls: recompute the deterministic V10 anchor for a stored report's comps
using the fixed _flag_psf_outliers, to confirm the outlier-weighting fix actually tightens the
anchor spread before spending any AI credits on a real regenerate.

Usage: python -m scripts.verify_outlier_fix <report_id>
"""
import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - forces the app's normal import order first, avoiding a circular import
from app.models.database import CmaReport
from app.database import DATABASE_URL
from app.services.cma_ai_service import _flag_psf_outliers, _build_evidence_by_key, _compute_comp_anchor


async def main(report_id: str):
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSessionLocal() as db:
        r = await db.get(CmaReport, report_id)
        if not r:
            print("Report not found.")
            return
        subject = r.subject or {}
        comps = [c for c in (r.comparables or []) if c.get("is_included", True)]

        outliers = _flag_psf_outliers(comps)
        print(f"Subject: {subject.get('street_address')}")
        print(f"\nOutlier flags now returned: {len(outliers)}")
        for key, note in outliers.items():
            label = next((c.get("street_address") for c in comps if c.get("listing_key") == key), key)
            print(f"  {label}: {note}")

        evidence = _build_evidence_by_key(subject, comps)
        anchor = _compute_comp_anchor(subject, comps, evidence, outliers)
        print(f"\nNEW anchor: value=${anchor.get('anchor_value'):,}  spread_pct={anchor.get('spread_pct')}%  "
              f"mode={anchor.get('mode')}  weighted_comp_count={anchor.get('weighted_comp_count')}")
        for ct in anchor.get("comps") or []:
            print(f"  {ct.get('label')}: weight_raw={ct.get('weight_raw')}  normalized_weight={ct.get('normalized_weight')}  "
                  f"outlier={ct.get('outlier')}  outlier_penalty={ct.get('outlier_penalty')}")

        # Compare against what was flagged/computed with the OLD (pre-fix) logic - the report's
        # own already-stored trace, from before this change.
        old_trace = ((r.pricing or {}).get("ai_estimate") or {}).get("valuation_trace") or {}
        old_anchor = old_trace.get("anchor") or {}
        print(f"\nOLD (stored) anchor: value=${old_anchor.get('anchor_value')}  spread_pct={old_anchor.get('spread_pct')}%  "
              f"weighted_comp_count={old_anchor.get('weighted_comp_count')}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
