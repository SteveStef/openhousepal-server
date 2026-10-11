"""v10 CMA valuation - historical backtest & parameter-calibration harness (hardening step 10).

WHAT IT MEASURES
    The DETERMINISTIC part of v10 - _compute_comp_anchor + _compute_exposure - against real
    eventual sale prices. The AI qualitative-adjustment layer is deliberately bounded and small,
    so if the deterministic anchor is a good predictor on its own, the whole model is sound. Each
    fixture supplies everything that was known BEFORE the subject sold, plus the hidden actual
    close price.

FIXTURE FORMAT  (one JSON file per historical sale, in --dir; see fixtures/EXAMPLE.json)
    {
      "id": "123-main-st-2025",
      "as_of_date": "2025-03-01",
      "geography": "Lower Merion",              # optional label for the per-geo breakdown
      "actual_close_price": 3175000,            # HIDDEN from the engine, used only to score
      "subject": { ...CmaSubjectProperty-ish dict... , "_status_timeline": {...} },
      "comps":   [ ...CmaComparable-ish dicts that were available on as_of_date... ]
    }

USAGE
    python -m scripts.cma_v10_backtest --dir scripts/fixtures/cma_backtest
    python -m scripts.cma_v10_backtest --dir <dir> --sweep          # parameter sweep
    python -m scripts.cma_v10_backtest --dir <dir> --json out.json   # machine-readable

CALIBRATION RULE
    Do NOT pick the sweep winner if it only helps one geography/tier. The report prints a
    per-geography MdAPE for every candidate so you can see whether a gain generalises.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import app.main as _app_main  # resolves the package's circular-import ordering
import app.services.cma_ai_service as cma

_ = _app_main  # imported for its import-time side effect only


# --------------------------------------------------------------------------------------------
def _anchor_fmv(subject: dict, comps: list) -> dict:
    """Run the deterministic v10 stages with a ZERO AI adjustment layer -> the pure comp-anchor
    prediction, plus its own range and a confidence proxy."""
    ev = cma._build_evidence_by_key(subject, comps)
    outliers = cma._flag_psf_outliers(comps)
    anchor = cma._compute_comp_anchor(subject, comps, ev, outliers)
    tl = subject.get("_status_timeline") or {}
    exposure = cma._compute_exposure(subject, tl, cma._v10_comp_median_dom(comps), anchor)
    res = cma._apply_v10_valuation(anchor, exposure, {"value": None, "range_pct": 5,
                                                      "qualitative_adjustments": []},
                                   cma._as_float(subject.get("list_price")))
    return {
        "fmv": res["fmv"],
        "range_pct": res["range_pct"],
        "confidence": res["confidence_seed"],
        "anchor_mode": (anchor or {}).get("mode"),
        "anchor_validity": (anchor or {}).get("anchor_validity"),
        "closed_comps": (anchor or {}).get("closed_comp_count", 0),
        "weighted_comps": (anchor or {}).get("weighted_comp_count", 0),
        "exposure_state": exposure.get("state"),
        "insufficient": bool(res.get("insufficient_evidence")),
    }


def _tier(price: float) -> str:
    if price < 750_000:
        return "<750k"
    if price < 1_500_000:
        return "750k-1.5M"
    if price < 3_000_000:
        return "1.5M-3M"
    return "3M+"


def _pct_err(pred: float, actual: float) -> float:
    return (pred - actual) / actual * 100.0


# --------------------------------------------------------------------------------------------
def score(fixtures: list) -> dict:
    rows = []
    for fx in fixtures:
        subj, comps, actual = fx["subject"], fx["comps"], float(fx["actual_close_price"])
        try:
            out = _anchor_fmv(subj, comps)
        except Exception as exc:  # noqa: BLE001
            rows.append({"id": fx.get("id"), "error": str(exc)})
            continue
        if not out["fmv"]:
            rows.append({"id": fx.get("id"), "error": "no fmv", **out})
            continue
        err = _pct_err(out["fmv"], actual)
        rows.append({
            "id": fx.get("id"), "geography": fx.get("geography") or "?",
            "property_type": (fx.get("property_type") or subj.get("property_type")
                              or subj.get("property_sub_type") or "?"),
            "actual": actual, "pred": out["fmv"], "err_pct": err, "abs_err_pct": abs(err),
            "in_range": abs(err) <= out["range_pct"],
            "tier": _tier(actual), "confidence": out["confidence"],
            "anchor_mode": out["anchor_mode"], "weighted_comps": out["weighted_comps"],
            "exposure_state": out["exposure_state"], "insufficient": out["insufficient"],
            "anchor_validity": out.get("anchor_validity"),
        })

    ok = [r for r in rows if "err_pct" in r]
    def agg(subset):
        if not subset:
            return None
        ae = [r["abs_err_pct"] for r in subset]
        se = [r["err_pct"] for r in subset]
        return {
            "n": len(subset),
            "mdape": round(statistics.median(ae), 2),
            "mape": round(statistics.fmean(ae), 2),
            "bias": round(statistics.fmean(se), 2),
            "within_3": round(100 * sum(a <= 3 for a in ae) / len(ae), 1),
            "within_5": round(100 * sum(a <= 5 for a in ae) / len(ae), 1),
            "within_10": round(100 * sum(a <= 10 for a in ae) / len(ae), 1),
            "range_capture": round(100 * sum(r["in_range"] for r in subset) / len(subset), 1),
        }

    by = lambda key: {v: agg([r for r in ok if r.get(key) == v]) for v in sorted({r.get(key) for r in ok})}
    conf_tbl = by("confidence")
    conf_ok = None
    ms = [conf_tbl.get(c, {}).get("mdape") for c in ("high", "medium", "low")]
    if all(m is not None for m in ms):
        conf_ok = ms[0] <= ms[1] <= ms[2]

    return {
        "overall": agg(ok),
        "errors": [r for r in rows if "error" in r],
        "by_tier": by("tier"),
        "by_confidence": conf_tbl,
        "confidence_calibrated": conf_ok,
        "confidence_detail": {c: {"mdape": conf_tbl.get(c, {}).get("mdape") if conf_tbl.get(c) else None,
                                  "n": conf_tbl.get(c, {}).get("n") if conf_tbl.get(c) else 0}
                             for c in ("high", "medium", "low")},
        "by_anchor_mode": by("anchor_mode"),
        "by_exposure_state": by("exposure_state"),
        "by_geography": by("geography"),
        "by_property_type": by("property_type"),
        "by_comp_count": by("weighted_comps"),
        "rows": rows,
    }


# --------------------------------------------------------------------------------------------
# Every v10 tunable the hardening spec calls out. Scalars are set directly on the module; the two
# dict params (_V10_ITEM_CAP, _V10_DOM_FALLBACK) are swept via a scale factor / tuple so one axis
# moves at a time. Keep grids small - this is calibration evidence, not a black-box optimiser.
_SWEEP_SCALAR = {
    "_V10_BETA":                 [0.40, 0.55, 0.70],          # size elasticity fallback
    "_V10_ANCHOR_BLEND":         [0.50, 0.60, 0.70],          # 60/40 sale-vs-$/sqft blend
    "_V10_ADJ_BOUND":            [0.10, 0.12, 0.15],          # normal adjustment bound
    "_V10_ADJ_BOUND_DISPUTED":   [0.15, 0.18, 0.22],          # disputed-anchor bound
    "_V10_WEAK_UP_SUBCAP":       [0.03, 0.05, 0.07],          # speculative-up sub-cap
    "_V10_MODEL_JUDGMENT_SUBCAP":[0.025, 0.035, 0.05],        # model-judgment budget
    "_V10_FAMILY_STACK_CAP":     [1.15, 1.30, 1.50],          # same-family stack cap
    "_V10_OUTLIER_PENALTY":      [0.35, 0.5, 0.65],           # $/sqft-outlier weight penalty
    "_V10_REDUNDANCY_EXP":       [0.35, 0.5, 0.65],           # near-dupe cluster 1/N**exp
    "_V10_ANCHOR_MIN_SPREAD":    [0.03, 0.04, 0.05],          # anchor spread floor
    "_V10_ANCHOR_MAX_SPREAD":    [0.10, 0.12, 0.15],          # anchor spread ceiling
    "_V10_REGRESSION_MIN_COMPS": [4, 5, 6],                   # min comps to trust a regression
    "_V10_REGRESSION_MIN_R2":    [0.4, 0.5, 0.6],             # min R^2 to trust a regression
    "_V10_RECENCY_HALF_LIFE_DAYS": [120, 182, 270],           # recency half-life
    "_V10_DOM_SHORT_MULT":       [0.3, 0.4, 0.5],             # DOM state thresholds (x comp-median)
    "_V10_DOM_NORMAL_MULT":      [1.25, 1.5, 1.75],
    "_V10_DOM_EXTENDED_MULT":    [2.0, 2.5, 3.0],
    # round 9 - sqft-composition normalization (Fix 1)
    "_V10_SQFT_MIXED_FRAC":         [0.10, 0.12, 0.15],       # "materially mixed" below-grade fraction
    "_V10_BELOW_GRADE_CREDIT":      [0.40, 0.50, 0.60],       # FALLBACK-heuristic below-grade credit vs blended $/sqft
    "_V10_SQFT_ADJ_MAX":           [0.12, 0.18, 0.25],        # max downward $/sqft normalization
    "_V10_PSF_LEG_MIXED_PENALTY":   [0.50, 0.60, 0.75],       # $/sqft-leg weight when the FALLBACK heuristic normalized
    "_V10_PSF_LEG_PAIRED_PENALTY":  [0.78, 0.85, 0.92],       # ...lighter weight when paired/explicit below-grade evidence set the rate
    # round 9 - market-location consistency (Fix 2)
    "_V10_LOC_NEG_STEP":           [0.08, 0.12, 0.16],        # weight cost per location negative
    "_V10_LOC_MIN_MULT":           [0.45, 0.55, 0.65],        # floor on the location weight penalty
    # round 9 - weighted qualitative reconciliation (Fix 3)
    "_V10_QUAL_SIGNAL_MATERIAL":    [0.25, 0.30, 0.40],       # |signal| that counts as a directional imbalance
    "_V10_QUAL_CONDITION_GAP_FRAC": [0.10, 0.15, 0.20],       # implied full condition-gap size (x anchor)
    "_V10_ADJ_BOUND_BROAD_INF":     [0.16, 0.22, 0.28],       # downward bound when the broad-inferiority mechanism fires
    "_V10_BROAD_INFERIORITY_MIN_FRAC": [0.015, 0.02, 0.03],   # min unpriced shortfall (x anchor) to apply the aggregate
    "_V10_BROAD_INFERIORITY_APPLY_FRAC": [0.4, 0.5, 0.6, 0.75],  # share of the shortfall actually applied
    "_V10_BROAD_INFERIORITY_SIGNAL_MIN": [0.22, 0.25, 0.30],  # min bucket signal to count toward the shortfall
    # round 12 - condition-only comp re-promotion (issue #1)
    "_V10_COND_PROMOTE_MIN_EV":        [0.50, 0.55, 0.62],    # min overall evidence score to re-promote a condition-demoted comp
    "_V10_COND_PROMOTE_MIN_COMPONENT": [0.35, 0.40, 0.50],    # min proximity/size/recency component to re-promote
    # round 13 - competitive (over-ask / bidding-war) high-side driver awareness
    "_V10_COMPETITIVE_LTS":            [1.05, 1.06, 1.08],    # sale/list that counts as sold materially over asking
    "_V10_COMPETITIVE_DOMINANCE":      [0.28, 0.32, 0.38],    # normalized weight at which a competitive sale is a "high-side driver"
    # round 16 - anchor bias controls
    "_V10_COMPETITIVE_WEIGHT_MULT":    [0.6, 0.7, 0.85, 1.0],  # anchor-weight multiplier for a bidding-war sale (1.0 = off)
    "_V10_SOFT_OFFSET_FACTOR":         [0.5, 0.6, 0.75, 1.0],  # extra shrink on an unmeasurable UP qualitative offset (1.0 = off)
}
_SWEEP_SPECIAL = {
    # scale every per-item cap together
    "_V10_ITEM_CAP_scale":  [0.75, 1.0, 1.5],
    # absolute DOM fallbacks (short, normal, extended)
    "_V10_DOM_FALLBACK":    [(10.0, 35.0, 60.0), (14.0, 45.0, 75.0), (21.0, 60.0, 100.0)],
}
_ITEM_CAP_BASE = dict(cma._V10_ITEM_CAP)


def _apply_special(name: str, value) -> None:
    if name == "_V10_ITEM_CAP_scale":
        cma._V10_ITEM_CAP = {k: v * value for k, v in _ITEM_CAP_BASE.items()}
    elif name == "_V10_DOM_FALLBACK":
        cma._V10_DOM_FALLBACK = value


def _stability(s: dict) -> str:
    """Spread of MdAPE across the breakdown dimensions - a change that only helps one slice shows
    up as a WIDE spread here even when the aggregate MdAPE drops."""
    parts = []
    for key in ("by_geography", "by_tier", "by_property_type", "by_comp_count",
                "by_confidence", "by_exposure_state"):
        ms = [t["mdape"] for t in (s.get(key) or {}).values() if t]
        if len(ms) >= 2:
            parts.append(max(ms) - min(ms))
    if not parts:
        return "n/a"
    return f"{statistics.fmean(parts):.2f}pp avg / {max(parts):.2f}pp worst"


def sweep(fixtures: list) -> None:
    base = {k: getattr(cma, k) for k in _SWEEP_SCALAR}
    base_score = score(fixtures)
    b = base_score["overall"] or {}
    print("\n=== PARAMETER SWEEP (one param at a time) ===")
    print(f"baseline: MdAPE {b.get('mdape')}%  bias {b.get('bias')}%  ±5% {b.get('within_5')}%  "
          f"conf_calibrated={base_score['confidence_calibrated']}  stability {_stability(base_score)}")
    print("cols: param=value | MdAPE | bias | ±5% | conf_cal | stability(avg/worst MdAPE spread) | per-geo MdAPE\n")

    def _run_line(param, v, is_base):
        s = score(fixtures)
        o = s["overall"] or {}
        geo = {g: (t or {}).get("mdape") for g, t in s["by_geography"].items()}
        tag = "  <-- baseline" if is_base else ""
        print(f"  {param}={str(v):<20} {str(o.get('mdape')):>6}% {str(o.get('bias')):>7}% "
              f"{str(o.get('within_5')):>6}%  cc={str(s['confidence_calibrated']):<5} "
              f"{_stability(s):<26} {geo}{tag}")

    for param, values in _SWEEP_SCALAR.items():
        print(f"\n{param}  (baseline {base[param]})")
        for v in values:
            setattr(cma, param, v)
            _run_line(param, v, v == base[param])
        setattr(cma, param, base[param])

    for param, values in _SWEEP_SPECIAL.items():
        print(f"\n{param}")
        for v in values:
            _apply_special(param, v)
            _run_line(param, v, False)
        # restore
        cma._V10_ITEM_CAP = dict(_ITEM_CAP_BASE)
        cma._V10_DOM_FALLBACK = (14.0, 45.0, 75.0)

    print("\nCALIBRATION RULE: accept a change only if it lowers MdAPE across MOST geographies / tiers /")
    print("property types AND does not worsen the stability spread or break confidence calibration.")
    print("Do NOT brute-force the lowest aggregate error - a change that only helps one slice is overfit.")


# --------------------------------------------------------------------------------------------
def _fmt(tbl: dict) -> str:
    if not tbl:
        return "  (none)"
    return "\n".join(
        f"  {str(k):<18} n={v['n']:<3} MdAPE {v['mdape']:>5}%  bias {v['bias']:>6}%  "
        f"±3/5/10% {v['within_3']}/{v['within_5']}/{v['within_10']}  range-capture {v['range_capture']}%"
        for k, v in tbl.items() if v
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="directory of fixture JSON files")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--json", help="write the full result to this path")
    args = ap.parse_args()

    files = sorted(Path(args.dir).glob("*.json"))
    files = [f for f in files if f.name != "EXAMPLE.json"]
    if not files:
        print(f"No fixture JSON files in {args.dir} (EXAMPLE.json is skipped).")
        return 1
    fixtures = [json.loads(f.read_text()) for f in files]
    print(f"Loaded {len(fixtures)} historical sale fixtures from {args.dir}")

    s = score(fixtures)
    print("\n=== OVERALL (deterministic anchor vs. actual close) ===")
    o = s["overall"]
    if o:
        print(f"  n={o['n']}  MdAPE {o['mdape']}%  MAPE {o['mape']}%  bias {o['bias']}%")
        print(f"  within ±3% {o['within_3']}%   ±5% {o['within_5']}%   ±10% {o['within_10']}%")
        print(f"  actual fell inside the reported range: {o['range_capture']}%")
    if s["errors"]:
        print(f"  {len(s['errors'])} fixture(s) errored / produced no FMV: {[e.get('id') for e in s['errors']]}")
    print("\n=== BY PRICE TIER ===\n" + _fmt(s["by_tier"]))
    print("\n=== BY PROPERTY TYPE ===\n" + _fmt(s["by_property_type"]))
    print("\n=== BY CONFIDENCE  (High should beat Moderate should beat Low) ===\n" + _fmt(s["by_confidence"]))
    cd = s["confidence_detail"]
    print(f"  MdAPE  high={cd['high']['mdape']} (n={cd['high']['n']})  "
          f"medium={cd['medium']['mdape']} (n={cd['medium']['n']})  low={cd['low']['mdape']} (n={cd['low']['n']})")
    if s["confidence_calibrated"] is True:
        print("  confidence_calibrated: TRUE  (High <= Medium <= Low MdAPE - the confidence model is honest)")
    elif s["confidence_calibrated"] is False:
        print("  confidence_calibrated: FALSE  *** the confidence model is INVERTED somewhere - fix the")
        print("      confidence seeding / _compute_confidence, do NOT just retune the FMV constants ***")
    else:
        print("  confidence_calibrated: n/a  (need scored fixtures in all three confidence buckets)")
    print("\n=== BY ANCHOR MODE ===\n" + _fmt(s["by_anchor_mode"]))
    print("\n=== BY EXPOSURE STATE ===\n" + _fmt(s["by_exposure_state"]))
    print("\n=== BY GEOGRAPHY ===\n" + _fmt(s["by_geography"]))
    print("\n=== BY WEIGHTED COMP COUNT ===\n" + _fmt(s["by_comp_count"]))
    print(f"\nstability spread (avg / worst MdAPE gap across slices): {_stability(s)}")

    if args.sweep:
        sweep(fixtures)
    if args.json:
        Path(args.json).write_text(json.dumps(s, indent=2, default=str))
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
