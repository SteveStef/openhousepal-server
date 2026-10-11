"""Read-only: for the most recent CMA reports (by street address match), print exactly how the
final valuation range was derived - value, range_pct, confidence, anchor spread_pct, comp sale-price
spread, and the reasoning model's own range endpoints - to see whether a wide displayed range
reflects genuine thin/inconsistent comp evidence or a bug.

Usage: python -m scripts.inspect_range_width "847 Lesley" "100 Cherry"
"""
import asyncio
import json
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import CmaReport
from app.database import DATABASE_URL


async def main(needles):
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CmaReport).order_by(CmaReport.created_at.desc()).limit(500))
        reports = result.scalars().all()
        for needle in needles:
            matches = [r for r in reports if needle.lower() in str((r.subject or {}).get("street_address", "")).lower()]
            print("#" * 100)
            print(f"Needle '{needle}': {len(matches)} report(s)")
            for r in matches[:3]:
                pricing = r.pricing or {}
                ai = pricing.get("ai_estimate") or {}
                fv = ai.get("final_valuation") or {}
                trace = ai.get("valuation_trace") or {}
                anchor = (trace.get("anchor") or {})
                comp_stats = ai.get("comp_stats") or {}
                print("-" * 100)
                print(f"Report {r.id}  status={r.status}  updated_at={r.updated_at}")
                print(f"  value={ai.get('value')}  range_min={ai.get('range_min')}  range_max={ai.get('range_max')}  confidence={ai.get('confidence')}")
                print(f"  final_valuation: value={fv.get('value')} range_min={fv.get('range_min')} range_max={fv.get('range_max')} range_pct={round(((fv.get('range_max',0)-fv.get('range_min',0))/(2*fv.get('value',1)))*100,2) if fv.get('value') else None}")
                print(f"  trace.mode={trace.get('mode')}  trace.authority={trace.get('authority')}")
                print(f"  v10_formula_fmv={trace.get('v10_formula_fmv')}  v10_formula_range_pct={trace.get('v10_formula_range_pct')}")
                print(f"  reasoning_model_fmv={trace.get('reasoning_model_fmv')} range_low={trace.get('reasoning_model_range_low')} range_high={trace.get('reasoning_model_range_high')} confidence={trace.get('reasoning_model_confidence')}")
                print(f"  anchor.spread_pct={anchor.get('spread_pct')}  anchor.weighted_comp_count={anchor.get('weighted_comp_count')}  anchor.sale_psf_divergence_pct={anchor.get('sale_psf_divergence_pct')}  anchor.anchor_validity={anchor.get('anchor_validity')}")
                print(f"  comp_stats: sale_min={comp_stats.get('sale_min')} sale_max={comp_stats.get('sale_max')} psf_min={comp_stats.get('psf_min')} psf_max={comp_stats.get('psf_max')}")
                rm = (trace.get("reasoning_model") or {})
                print(f"  reasoning rationale (final_valuation_summary): {str(rm.get('final_valuation_summary') or '')[:600]}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["847 Lesley", "Cherry"]))
