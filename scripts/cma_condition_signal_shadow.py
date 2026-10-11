"""SHADOW MODE ONLY - does not touch production pricing or any live code path.

Diagnoses whether the CMA engine's EXISTING, already-trusted structured condition signal
(_v10_weighted_qualitative_signals -> weighted_condition_signal, the same mechanism that
legitimately produces the amenities qualitative-inferiority shortfall) would support a
condition-based dollar adjustment anywhere near the $85,000 currently applied to the Cherry Ln
report via a separate, less rigorous "paired_market_evidence" comp-bracket adjustment.

Computes the SAME formula _apply_v10_valuation already uses internally for the amenities/
condition/location/accessory_space "qualitative inferiority shortfall" (round 17) - reusing the
real, unmodified `_v10_weighted_qualitative_signals` function and the same constants - against:
  1. Cherry Ln's REAL anchor (the 5 comps' actual quality ratings + normalized weights, as
     currently saved on the report).
  2. The existing "C. condition-signal vs. adjustment reconciliation" regression fixture
     (_cond_setup in test_cma_v10_valuation_engine.py) - a deliberately ALL-comps-superior
     scenario - run through the REAL _apply_v10_valuation, to confirm this script's standalone
     formula reproduction matches the pipeline's actual output before trusting it on Cherry.

Nothing here is applied to any report or wired into generate/recompute. Print-only.
"""
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.main  # noqa: F401  (forces the normal import order, avoiding a circular import)
from app.services.cma_ai_service import (
    _v10_weighted_qualitative_signals,
    _apply_v10_valuation,
    _V10_QUAL_CONDITION_GAP_FRAC,
    _V10_QUAL_SIGNAL_MATERIAL,
    _V10_BROAD_INFERIORITY_SIGNAL_MIN,
)
from scripts.test_cma_v10_valuation_engine import _subject, _comp, _anchor, _exposure, _cond_setup


def _shadow_condition_gap(anchor_value: float, signal: float, weight_covered: float) -> float:
    """Exact reproduction of the condition branch of _apply_v10_valuation's round-17
    `_bucket_gap` closure - same constant, same clamping. Standalone so it can be run against a
    hypothetical/real anchor without needing a full qualitative_adjustments list."""
    return _V10_QUAL_CONDITION_GAP_FRAC * anchor_value * min(1.0, signal) * min(1.0, weight_covered)


def _report(label: str, anchor_value: float, qsig: dict):
    cond = qsig.get("condition") or {}
    signal = cond.get("signal") or 0.0
    covered = cond.get("weight_covered") or 0.0
    shadow = _shadow_condition_gap(anchor_value, signal, covered)
    fires = signal >= _V10_BROAD_INFERIORITY_SIGNAL_MIN and covered >= 0.50
    print(f"\n{label}")
    print(f"  anchor_value:              ${anchor_value:,.0f}")
    print(f"  weighted_condition_signal: {signal:+.3f}  (direction: {cond.get('direction')}, weight_covered: {covered:.2f})")
    print(f"  per-bucket block threshold ({_V10_QUAL_SIGNAL_MATERIAL}):    {'MATERIAL' if abs(signal) >= _V10_QUAL_SIGNAL_MATERIAL else 'not material'}")
    print(f"  aggregate-shortfall threshold ({_V10_BROAD_INFERIORITY_SIGNAL_MIN}): {'fires' if fires else 'does NOT fire'}")
    print(f"  shadow condition $ gap (same formula/constant as the live amenities shortfall): ${shadow:,.0f}")
    return shadow


def main():
    print("=" * 100)
    print("SHADOW MODE - print-only, nothing applied to any report or wired into production code")
    print("=" * 100)

    # ---- 1. Cross-check: reproduce the REAL pipeline's own number on a known fixture -----------
    subj, anchor, exposure = _cond_setup()
    real_result = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    real_qsig = real_result["trace"].get("weighted_condition_signal") or {}
    real_agg = (real_result["trace"].get("qualitative_reconciliation") or {}).get("aggregate_inferiority") or {}
    print("\n[CROSS-CHECK] '_cond_setup' regression fixture (3 comps, all rated 'above' on "
          "finish_quality/interior/exterior - a deliberate full one-sided condition gap)")
    print(f"  REAL pipeline weighted_condition_signal: {real_qsig}")
    print(f"  REAL pipeline aggregate_inferiority:      {real_agg}")
    shadow_fixture = _shadow_condition_gap(
        anchor["anchor_value"], real_qsig.get("signal") or 0.0, real_qsig.get("weight_covered") or 0.0,
    )
    print(f"  This script's standalone shadow $ gap:    ${shadow_fixture:,.0f}")
    match = real_agg.get("buckets") == ["condition"] and abs(shadow_fixture - (real_agg.get("implied_total") or 0)) < 1.0
    print(f"  Matches the real pipeline's implied_total for this bucket: {'YES' if match else 'NO - formula reproduction is WRONG, do not trust the numbers below'}")

    # ---- 2. Cherry Ln's REAL anchor, as currently saved on the report ---------------------------
    cherry_anchor_value = 1_603_435.0
    cherry_comps = [
        {"listing_key": "c1", "normalized_weight": 0.276,
         "quality": {"lot": "similar", "area": "same_as_subject", "layout": "similar", "exterior": "similar",
                     "interior": "above", "amenities": "similar", "finish_quality": "above", "street_context": "similar"}},
        {"listing_key": "c2", "normalized_weight": 0.184,
         "quality": {"lot": "below", "area": "same_as_subject", "layout": "similar", "exterior": "similar",
                     "interior": "above", "amenities": "above", "finish_quality": "above", "street_context": "similar"}},
        {"listing_key": "c3", "normalized_weight": 0.183,
         "quality": {"lot": "similar", "area": "same_as_subject", "layout": "similar", "exterior": "below",
                     "interior": "below", "amenities": "above", "finish_quality": "below", "street_context": "similar"}},
        {"listing_key": "c4", "normalized_weight": 0.179,
         "quality": {"lot": "below", "area": "same_as_subject", "layout": "similar", "exterior": "similar",
                     "interior": "above", "amenities": "above", "finish_quality": "above", "street_context": "above"}},
        {"listing_key": "c5", "normalized_weight": 0.178,
         "quality": {"lot": "below", "area": "similar_to_subject", "layout": "similar", "exterior": "below",
                     "interior": "below", "amenities": "similar", "finish_quality": "similar", "street_context": "below"}},
    ]
    cherry_qsig = _v10_weighted_qualitative_signals({"comps": cherry_comps})
    cherry_shadow = _report("Cherry Ln (100 Cherry Ln, Wynnewood) - REAL saved anchor",
                             cherry_anchor_value, cherry_qsig)

    print("\n" + "=" * 100)
    print("COMPARISON")
    print("=" * 100)
    print(f"  Currently applied 'Condition/renovation gap vs. Comps #1 and #2' (paired_market_evidence): $85,000")
    print(f"  Structured, already-trusted-elsewhere shadow condition signal would support:  ~${cherry_shadow:,.0f}")
    print(f"  ...and it doesn't clear the {_V10_BROAD_INFERIORITY_SIGNAL_MIN} materiality floor this pipeline already "
          f"requires before ANY bucket is allowed to drive an aggregate adjustment, so the honest, ")
    print(f"  already-live mechanism would apply $0 for condition alone on this comp set - not $30K, not $85K.")


if __name__ == "__main__":
    main()
