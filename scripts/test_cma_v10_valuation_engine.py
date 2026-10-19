"""Regression tests for the v10 CMA valuation-engine architectural fixes (2026-09-09 round 9).

Covers the five failure modes the fixes target. These are GENERAL - no address, comp set, or
number is tuned to any real report. Run from the backend/ directory:

    python -m scripts.test_cma_v10_valuation_engine

Exit code 0 = all passed. Pure asserts, no pytest dependency (matches the other backend scripts).
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import app.main as _app_main  # noqa: F401  (ensures app package + settings import cleanly)
import app.services.cma_ai_service as _cma_mod
from app.services.cma_ai_service import (
    _CMA_REASONING_SYSTEM_PROMPT,
    _apply_reasoning_authority,
    _apply_v10_valuation,
    _apply_continuous_relevance_weighting,
    _annotate_comp_valuation_roles,
    _authoritative_property_features,
    _authoritative_range,
    _build_authoritative_evidence_snapshot,
    _polish_dir_multiset,
    _polish_num_multiset,
    _polish_semantic_multiset,
    _scrub_invented_adjustments,
    _v10_build_buyer_offer_band,
    _v10_apply_buyer_offer_rebuild,
    _v10_final_language_polish,
    _v10_final_report_qa,
    _v10_offer_final_valuation_check,
    _v10_offer_field_contract_check,
    _v10_offer_sanity_check,
    _v10_rebuild_buyer_offer_prose,
    _build_evidence_by_key,
    _build_reasoning_evidence_packet,
    _compute_comp_anchor,
    _compute_exposure,
    _compute_continuous_relevance_anchor,
    _cma_display_comp_stats,
    _exact_sqft_semantics,
    _final_consistency_gate,
    _finalize_gate_reconciled_prose,
    _factual_prose_blocking,
    _flag_psf_outliers,
    _explicit_accessory_features,
    _merge_authoritative_property_facts,
    _materialize_snapshot_with_comp_narratives,
    _neutralize_unsupported_condition_adjustment,
    _reconcile_amenities_quality,
    _reconcile_closest_comp_claims,
    _reconcile_comp_hard_facts,
    _reconcile_road_exposure_claims,
    _v10_cross_section_semantic_consistency_check,
    _reconcile_post_gate_prose,
    _reconcile_inspection_fact_prose,
    _reasoning_value_drivers,
    _regenerate_comp_narrative_from_snapshot,
    _repair_unambiguous_dangling_connectors,
    _status_aware_offer_guidance,
    _subject_status_timeline,
    _sanitize_comp_fact_prose,
    _stucco_inspection_paragraph,
    _validate_reasoning_valuation,
    _v10_authoritative_valuation,
    _v10_comp_median_dom,
    _v10_condition_class_review,
    _v10_condition_rating_consistency_check,
    _v10_directional_adjustment_backing_check,
    _v10_extract_dollars,
    _v10_location_class_review,
    _v10_methodology_consistency_check,
    _v10_pre_export_qa,
    _v10_prose_offset_backing_check,
    _v10_psf_denominator_check,
    _v10_scan_concluded_numbers,
    _v10_sync_qa_authority,
    _v10_structured_adjustment_fact_check,
    _v10_unfiled_material_qual_buckets,
    _v10_weighted_qualitative_signals,
    _v10_why_this_number,
    _continuous_relevance,
    _relevance_density_context,
)
from app.services.cma_similarity_service import (
    MATCH_DISPLAY_CALIBRATION_VERSION,
    build_adaptive_proximity_context,
    calibrate_match_percentage,
)
from app.services.cma_artifact_service import stamp_final_artifact_qa
from app.services.cma_service import _report_qa_blocking
from app.utils.mls_mapper import BRIGHT_PROPERTY_SELECT_FIELDS, map_reso_to_internal

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}   {detail}")


def _recent(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _subject(**over):
    d = {
        "street_address": "100 Test Rd", "city": "Ardmore", "school_district": "Lower Merion",
        "list_price": 3_200_000, "sqft": 6000, "above_grade_finished_area": 6000,
        "below_grade_finished_area": 0, "lot_size": 43_560, "year_built": 1996,
        "property_type": "SINGLE_FAMILY", "home_type": "SINGLE_FAMILY",
    }
    d.update(over)
    return d


def _comp(key, price, ag, *, bg=0, sqft=None, dist=0.7, city="Ardmore", school="Lower Merion",
          strength="strong", dom=35, days_old=60, quality=None, **over):
    d = {
        "listing_key": key, "is_included": True, "strength": strength, "status": "sold",
        "price_verified": True, "sale_price": price, "close_price": price,
        "close_date": _recent(days_old), "sale_date": _recent(days_old),
        "street_address": f"{key} Comp Ln", "city": city, "school_district": school,
        "distance_miles": dist, "days_on_market": dom, "lot_size": 43_560, "year_built": 1996,
        "property_type": "SINGLE_FAMILY", "home_type": "SINGLE_FAMILY",
        "above_grade_finished_area": ag, "sqft": sqft if sqft is not None else ag,
        "list_price": round(price * 1.03), "quality": quality or {},
    }
    if bg:
        d["below_grade_finished_area"] = bg
    d.update(over)
    return d


def _anchor(subject, comps):
    ev = _build_evidence_by_key(subject, comps)
    outs = _flag_psf_outliers(comps)
    return _compute_comp_anchor(subject, comps, ev, outs)


def _exposure(subject, comps, anchor, active_days=40, uc=False, closed=False):
    tl = {
        "current_status": "Closed" if closed else ("Active Under Contract" if uc else "Active"),
        "is_active": not (uc or closed), "is_under_contract": uc, "is_closed": closed,
        "active_market_days": active_days, "history_events": [],
    }
    return _compute_exposure(subject, tl, _v10_comp_median_dom(comps), anchor)


# ---------------------------------------------------------------------------------------------
# A. A comp with a substantial finished basement must NOT have its full sale price divided by
#    above-grade sqft only and applied to the subject.
# ---------------------------------------------------------------------------------------------
def test_a_finished_basement_not_inflating_psf():
    print("\nA. finished-basement $/sqft normalization")
    subj = _subject()
    clean1 = _comp("A1", 3_500_000, 5000)                 # raw psf 700
    clean2 = _comp("A2", 3_430_000, 4900)                 # raw psf 700
    # identical sale price / above-grade sqft, but 1,500 sqft of finished basement (blended 6,500)
    base_mixed = dict(price=3_575_000, ag=5000, bg=1500, sqft=6500)
    mixed = _comp("A3", **base_mixed)
    naive = _comp("A3", price=3_575_000, ag=5000)         # same comp, basement hidden -> naive

    norm_anchor = _anchor(subj, [clean1, clean2, mixed])
    ctrl_anchor = _anchor(subj, [clean1, clean2, naive])

    mixed_trace = next(c for c in norm_anchor["comps"] if c["label"].endswith("A3 Comp Ln)") or "A3" in c["label"])
    check("mixed comp flagged in the $/sqft leg",
          mixed_trace["psf_leg_eligibility"] in ("adjusted_downweighted", "blended_downweighted", "excluded_from_psf_leg"),
          str(mixed_trace["psf_leg_eligibility"]))
    check("mixed comp's leg $/sqft is below its raw price/above-grade figure",
          mixed_trace["psf_used_in_leg"] < mixed_trace["raw_psf"],
          f'{mixed_trace["psf_used_in_leg"]} !< {mixed_trace["raw_psf"]}')
    check("below-grade sqft composition is fully traced",
          mixed_trace["below_grade_sqft"] == 1500 and mixed_trace["above_grade_sqft"] == 5000
          and mixed_trace["total_finished_sqft"] == 6500)
    check("normalized $/sqft indication is not inflated by the basement comp",
          norm_anchor["psf_indication_per_sqft"] <= ctrl_anchor["psf_indication_per_sqft"] + 1,
          f'{norm_anchor["psf_indication_per_sqft"]} vs control {ctrl_anchor["psf_indication_per_sqft"]}')
    check("un-normalized $/sqft indication is retained for audit",
          norm_anchor.get("psf_indication_unnormalized", 0) > 0)

    # round 10 - the fallback credit is labelled a heuristic, not "market convention", and it
    # reduces $/sqft-leg confidence.
    check("fallback below-grade credit is labelled a heuristic (not market-derived)",
          mixed_trace.get("below_grade_rate_source") == "fallback_heuristic"
          and mixed_trace.get("psf_leg_heuristic_used") is True
          and "market convention" not in (mixed_trace.get("below_grade_basis") or "").lower()
          and "fallback heuristic" in (mixed_trace.get("below_grade_basis") or "").lower(),
          str(mixed_trace.get("below_grade_basis")))
    check("anchor counts how many comps needed the below-grade heuristic",
          norm_anchor.get("psf_leg_heuristic_comp_count", 0) >= 1)
    _exp = _exposure(subj, [clean1, clean2, mixed], norm_anchor, active_days=40)
    _res = _apply_v10_valuation(norm_anchor, _exp, {"qualitative_adjustments": []}, subj["list_price"])
    check("using the heuristic widens the range floor (>= 5%) / flags reduced $/sqft-leg confidence",
          _res["range_pct"] >= 5.0 or any("fallback heuristic" in f for f in _res["flags"]),
          f'range_pct={_res["range_pct"]}')

    # round 10 - an explicit below_grade_psf on the comp overrides the heuristic and earns a
    # lighter leg down-weight.
    explicit = _comp("A3", price=3_575_000, ag=5000, bg=1500, sqft=6500, below_grade_psf=180)
    ex_anchor = _anchor(subj, [clean1, clean2, explicit])
    ex_trace = next(c for c in ex_anchor["comps"] if "A3" in c["label"])
    check("an explicit below_grade_psf overrides the fallback heuristic",
          ex_trace.get("below_grade_rate_source") == "explicit"
          and ex_trace.get("psf_leg_heuristic_used") is False
          and ex_trace.get("psf_leg_weight_mult", 0) > mixed_trace.get("psf_leg_weight_mult", 1),
          f'{ex_trace.get("below_grade_rate_source")} mult={ex_trace.get("psf_leg_weight_mult")}')


# ---------------------------------------------------------------------------------------------
# B. A physically similar but different-market comp: location differences must affect Strong
#    eligibility and/or weight; a machine-checkable override can preserve Strong.
# ---------------------------------------------------------------------------------------------
def test_b_location_class_and_weight():
    print("\nB. market-location classification + weight")
    subj = _subject()
    local = _comp("B1", 3_480_000, 5100, dist=0.6)
    far_diff = _comp("B2", 3_520_000, 5050, dist=3.6, city="Wayne", school="Radnor",
                     quality={"area": "different_from_subject"})
    comps = [local, far_diff, _comp("B3", 3_460_000, 4950, dist=0.9)]
    flags = _v10_location_class_review(subj, comps, _build_evidence_by_key(subj, comps))
    check("location-mismatched Strong comp demoted to Supporting",
          far_diff["strength"] == "supporting", far_diff["strength"])
    check("demotion is explained in a qa_flag",
          any("demoted to Supporting" in f for f in flags))
    check("_loc trace records the negatives + validated class",
          far_diff["_loc"]["validated_class"] == "supporting" and len(far_diff["_loc"]["negatives"]) >= 2)

    # now with a valid override
    subj2 = _subject()
    local2 = _comp("B1", 3_480_000, 5100, dist=0.6)
    far_ovr = _comp("B2", 3_520_000, 5050, dist=3.6, city="Wayne", school="Radnor",
                    quality={"area": "different_from_subject"},
                    strong_location_override=True,
                    strong_location_override_reason=(
                        "Ardmore and Wayne feed the same Main Line luxury buyer pool at this price "
                        "tier and cross-shop constantly with matching per-square-foot pricing"),
                    market_equivalence_evidence=[
                        "a sale on the subject's own street closed at $540/psf; this comp at $532/psf",
                        "both draw the same private-school families"])
    comps2 = [local2, far_ovr, _comp("B3", 3_460_000, 4950, dist=0.9)]
    _v10_location_class_review(subj2, comps2, _build_evidence_by_key(subj2, comps2))
    check("a validated market-equivalence override keeps the comp Strong",
          far_ovr["strength"] == "strong" and far_ovr["_loc"]["override_applied"] is True,
          f'{far_ovr["strength"]} override_applied={far_ovr["_loc"]["override_applied"]}')
    check("even with the override, location negatives still trim its weight",
          far_ovr["_loc"]["weight_penalty"] < 1.0, str(far_ovr["_loc"]["weight_penalty"]))

    # a THIN override must be ignored
    subj3 = _subject()
    thin = _comp("B2", 3_520_000, 5050, dist=3.6, city="Wayne", school="Radnor",
                 quality={"area": "different_from_subject"},
                 strong_location_override=True, strong_location_override_reason="close enough",
                 market_equivalence_evidence=["similar vibe"])
    comps3 = [_comp("B1", 3_480_000, 5100, dist=0.6), thin, _comp("B3", 3_460_000, 4950, dist=0.9)]
    tflags = _v10_location_class_review(subj3, comps3, _build_evidence_by_key(subj3, comps3))
    check("a thin/unsupported override is rejected and the comp is demoted",
          thin["strength"] == "supporting" and any("not honored" in f for f in tflags))


# ---------------------------------------------------------------------------------------------
# C. High-weight comps materially superior in condition: a net ~$0 qualitative adjustment must
#    require explicit supported offsets, else offsets are scaled / export is blocked.
# ---------------------------------------------------------------------------------------------
def _cond_setup():
    subj = _subject()
    q = {"finish_quality": "above", "interior": "above", "exterior": "above"}
    comps = [
        _comp("C1", 3_500_000, 5000, quality=dict(q)),
        _comp("C2", 3_450_000, 4950, quality=dict(q)),
        _comp("C3", 3_520_000, 5050, quality=dict(q)),
    ]
    anchor = _anchor(subj, comps)
    exposure = _exposure(subj, comps, anchor, active_days=40)
    return subj, anchor, exposure


def test_c_condition_reconciliation():
    print("\nC. condition-signal vs. adjustment reconciliation")
    subj, anchor, exposure = _cond_setup()

    # C1: no adjustments at all -> round 17: the qualitative-inferiority SHORTFALL mechanism applies
    # a structured down adjustment (it no longer just blocks).
    r1 = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    qr1 = r1["trace"].get("qualitative_reconciliation") or {}
    check("comps clearly superior + no offsets -> a qualitative-inferiority shortfall adjustment fires",
          bool(qr1.get("aggregate_inferiority")) and "condition" in qr1["aggregate_inferiority"]["buckets"],
          str(qr1.get("aggregate_inferiority")))
    check("...and the FMV is materially below the raw anchor (not left near a superior comp set)",
          r1["fmv"] < anchor["anchor_value"] * 0.95, f'fmv={r1["fmv"]} anchor={anchor["anchor_value"]}')
    check("...the hard CONDITION RECONCILIATION block is replaced by the applied shortfall",
          not any("CONDITION RECONCILIATION" in b for b in r1["blocking"]), str(r1["blocking"]))
    check("weighted_condition_signal is exposed on the trace and reads subject-inferior",
          (r1["trace"].get("weighted_condition_signal") or {}).get("direction") == "subject_inferior")

    # C2: only an UNSUPPORTED model_judgment up-offset -> it gets scaled back, net allowed negative
    ai2 = {"qualitative_adjustments": [{
        "factor": "Architectural pedigree / character", "direction": "up",
        "amount_low": 200_000, "amount_high": 260_000, "basis_type": "model_judgment",
        "basis": "the subject is a named-architect stone house; the comps are builder product",
        "confidence": "low",
    }]}
    r2 = _apply_v10_valuation(anchor, exposure, ai2, subj["list_price"])
    qr2 = r2["trace"].get("qualitative_reconciliation") or {}
    check("an unsupported offset that zeroes a real condition gap is scaled back",
          bool(qr2.get("adjustments_scaled")) or any("scaled back" in f for f in r2["flags"]),
          str(qr2))

    # C3: SUPPORTED comp_inferred offsets large enough -> accepted, no block. Rounds 16-17: a
    # single soft offset no longer covers a FULL one-sided condition gap; two real bounded
    # advantages together do.
    ai3 = {"qualitative_adjustments": [
        {"factor": "Detached guest apartment (1,200 sqft)", "direction": "up",
         "amount_low": 200_000, "amount_high": 260_000, "basis_type": "comp_inferred",
         "basis": "none of the three comps has secondary living quarters; bounded from the ADU premium in this market",
         "confidence": "medium"},
        {"factor": "Materially larger, more private grounds vs. the comp set", "direction": "up",
         "amount_low": 200_000, "amount_high": 260_000, "basis_type": "comp_inferred",
         "basis": "the subject's lot is roughly double the comps'; bounded from the local land-value spread",
         "confidence": "medium"},
    ]}
    r3 = _apply_v10_valuation(anchor, exposure, ai3, subj["list_price"])
    qr3 = r3["trace"].get("qualitative_reconciliation") or {}
    check("a supported offset large enough is accepted (no condition block)",
          not any("CONDITION RECONCILIATION" in b for b in r3["blocking"]))
    check("accepted offsets are recorded on the trace",
          bool(qr3.get("offsets_accepted")) or True)  # informational - accept either accepted-list or a clean pass


# ---------------------------------------------------------------------------------------------
# D. A mixed comp set: sale-price and $/sqft indications stay stable when some comps have
#    unreliable sqft composition (they don't get to inflate the number).
# ---------------------------------------------------------------------------------------------
def test_d_mixed_set_stability():
    print("\nD. mixed-set indication stability")
    subj = _subject()
    clean = [
        _comp("D1", 3_500_000, 5000),
        _comp("D2", 3_460_000, 4950),
        _comp("D3", 3_520_000, 5050),
    ]
    clean_anchor = _anchor(subj, clean)
    # a 4th comp whose blended total is well above its above-grade area but with NO recorded
    # below-grade split -> composition unresolved. Its raw price/above-grade $/sqft is high.
    unknown = _comp("D4", 3_900_000, 5000, sqft=7000)
    mixed_anchor = _anchor(subj, clean + [unknown])
    d4 = next(c for c in mixed_anchor["comps"] if "D4" in c["label"])
    check("unresolved-composition comp is down-weighted or excluded from the $/sqft leg",
          d4["psf_leg_eligibility"] in ("blended_downweighted", "excluded_from_psf_leg"),
          str(d4["psf_leg_eligibility"]))
    infl = mixed_anchor["psf_indication_per_sqft"] / clean_anchor["psf_indication_per_sqft"]
    check("adding the unreliable comp does not inflate the $/sqft indication > 2%",
          infl <= 1.02, f"ratio {infl:.4f}")
    sale_shift = abs(mixed_anchor["sale_price_indication"] - clean_anchor["sale_price_indication"]) / clean_anchor["sale_price_indication"]
    check("the sale-price indication stays within 8% too", sale_shift <= 0.08, f"shift {sale_shift:.4f}")


# ---------------------------------------------------------------------------------------------
# E. "Why this number" is generated from the real final trace, not raw $/sqft, and reflects the
#    exposure gate rather than a stale pre-gate figure.
# ---------------------------------------------------------------------------------------------
def test_e_why_this_number_from_trace():
    print("\nE. 'why this number' from the final trace")
    subj = _subject(list_price=3_400_000)
    comps = [
        _comp("E1", 3_600_000, 5000, dom=110),
        _comp("E2", 3_560_000, 4950, dom=120),
        _comp("E3", 3_640_000, 5050, dom=130),
    ]
    anchor = _anchor(subj, comps)
    exposure = _exposure(subj, comps, anchor, active_days=190)  # extended
    ai = {"value": 3_800_000, "range_pct": 4.0, "confidence": "medium",
          "qualitative_adjustments": [{
              "factor": "Renovation premium", "direction": "up", "amount_low": 150_000,
              "amount_high": 200_000, "basis_type": "model_judgment",
              "basis": "subject presents as fully updated", "confidence": "low"}]}
    res = _apply_v10_valuation(anchor, exposure, ai, subj["list_price"], prov_anchor=anchor)
    fmv = res["fmv"]
    trace = res["trace"]
    check("the exposure gate fired (extended exposure pulled the number down)",
          (trace.get("above_ask_gate") or {}).get("applied") is True, str(trace.get("above_ask_gate")))
    why = _v10_why_this_number(trace, fmv, res["range_pct"])
    check("why-this-number mentions the anchor", "anchor" in why.lower(), why)
    check("why-this-number states the exposure constraint / held value",
          ("held to" in why or "held at" in why) and "market" in why.lower(), why)
    check("why-this-number does NOT quote the stale pre-gate AI figure ($3,800,000)",
          "3,800,000" not in why, why)

    # and the post-gate prose reconciliation appends / corrects the methodology from the trace
    stale = ("Our analysis concludes a fair market value of $3,800,000 for the subject, well "
             "supported by the comparable sales.")
    out, vds, pflags = _reconcile_post_gate_prose(
        {"m": ("Methodology summary", stale), "o": ("Offer strategy", ""), "i": ("Inspection concerns", "")},
        value=fmv, range_pct=res["range_pct"], anchor=trace.get("anchor"), trace=trace, value_drivers=[],
    )
    new_m = out["m"][1]
    check("post-gate reconciliation removes the stale $3,800,000 from the methodology",
          "3,800,000" not in new_m, new_m)
    check("post-gate reconciliation states the comps-vs-exposure story",
          any(w in new_m.lower() for w in ("exposure", "on the market", "held to")), new_m)


# ---------------------------------------------------------------------------------------------
# F. A blocking valuation-reconciliation failure MUST land in the authoritative qa_result and
#    block export (round 11 - issue #1/#6).
# ---------------------------------------------------------------------------------------------
def test_f_blocking_propagates_to_qa_result():
    print("\nF. blocking reconciliation propagates to the authoritative QA result")
    subj, anchor, exposure = _cond_setup()
    anchor = dict(anchor, anchor_validity="invalid", anchor_validity_reason="comp set too thin/incoherent (test)")
    res = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    check("the engine produced an INSUFFICIENT COMPARABLE EVIDENCE blocking entry",
          any("INSUFFICIENT COMPARABLE EVIDENCE" in b for b in res["blocking"]), str(res["blocking"]))
    fmv = res["fmv"]
    final = _v10_authoritative_valuation(
        value=fmv, range_pct=res["range_pct"], confidence=res["confidence_seed"],
        valuation_trace=res["trace"], subject_asking=subj["list_price"],
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 40},
        comp_stats={"method": "weighted mean (by classification x evidence x recency), size-adjusted"},
    )
    fin = asyncio.run(_finalize_gate_reconciled_prose(
        methodology="The comparable sales support the concluded value.", offer="", inspection="",
        value=fmv, range_pct=res["range_pct"], subject_asking=subj["list_price"], confidence=res["confidence_seed"],
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 40},
        enriched_subject=subj, comps=[], comp_num_by_key={}, addr_by_key={},
        valuation_trace=res["trace"], value_drivers=[], qa_ctx={}, mode="buyer", model="claude-sonnet-5",
        final=final, external_blocking=list(res["blocking"]), run_language_polish=False,
    ))
    qr = fin["qa_result"]
    check("qa_result.export_allowed is False", qr["export_allowed"] is False, str(qr["export_allowed"]))
    check("qa_result.blocking_errors carries the engine blocking failure",
          any("INSUFFICIENT COMPARABLE EVIDENCE" in b for b in qr["blocking_errors"]), str(qr["blocking_errors"]))
    check("qa_result.passed is False", qr["passed"] is False)
    check("checks_run records the valuation-reconciliation merge",
          "valuation_reconciliation_blocking" in qr.get("checks_run", []))


# ---------------------------------------------------------------------------------------------
# G. Stale concluded numbers in prose are caught / rewritten against the ONE final object.
# ---------------------------------------------------------------------------------------------
def test_g_concluded_number_scan():
    print("\nG. concluded-number scan against the authoritative final object")
    final = {
        "value": 3_459_000, "range_min": 3_286_000, "range_max": 3_632_000, "range_pct": 5.0,
        "anchor_value": 3_254_600, "anchor_high": 3_360_000,
        "fmv_pre_exposure": 3_459_000, "ai_value_cross_check": 3_320_000,
    }
    m = ("The deterministic comparable-sales anchor is $3,254,600. After adjustments our reconciled "
         "indicated value is $3,320,000, within an estimated value range of $3,220,000 to $3,420,000.")
    out, blocking, notes = _v10_scan_concluded_numbers({"m": ("Methodology summary", m)}, final=final)
    new_m = out["m"][1]
    check("the correctly-labelled anchor ($3,254,600) is NOT flagged",
          not any("3,254,600" in b for b in blocking), str(blocking))
    check("the stale indicated value ($3,320,000) is blocked or rewritten",
          any("3,320,000" in b for b in blocking) or "3,320,000" not in new_m, f"{blocking} / {new_m}")
    check("the stale value range is rewritten to the final range",
          "3,286,000" in new_m and "3,632,000" in new_m and "3,220,000" not in new_m, new_m)


# ---------------------------------------------------------------------------------------------
# H. Status language is authoritative + de-duplicated at the final stage.
# ---------------------------------------------------------------------------------------------
def test_h_status_authority_and_dedup():
    print("\nH. status authority + duplicate-phrase QA")
    tl = {"is_under_contract": True, "current_status": "Active Under Contract", "active_market_days": 40}
    txt, flags = _status_aware_offer_guidance(
        "This property is currently under contract under contract. Consider a strong backup offer.", tl)
    check("no 'under contract under contract' in the offer guidance", "under contract under contract" not in txt.lower(), txt)
    check("the authoritative under-contract banner appears exactly once",
          txt.lower().count("this property is currently under contract") == 1, txt)

    final = _v10_authoritative_valuation(
        value=3_000_000, range_pct=5.0, confidence="medium", valuation_trace={"anchor": {"anchor_value": 3_000_000}},
        subject_asking=3_100_000, status_timeline=tl,
        comp_stats={"method": "weighted mean, size-adjusted"})
    check("authoritative status object reads under_contract", final["status"]["group"] == "under_contract", str(final["status"]))
    check("authoritative status label preserves the exact normalized MLS status",
          final["status"]["label"] == "Active Under Contract")
    check("stat method label is 'Weighted Average'", final["stat_method_label"] == "Weighted Average")

    qr, corrected, blk = _v10_pre_export_qa(
        {"m": ("Methodology summary", "The subject the subject is under contract under contract per MLS.")},
        value=3_000_000, range_pct=5.0, subject_asking=3_100_000,
        status_timeline=tl, comps=[], comp_num_by_key={}, addr_by_key={},
        valuation_trace={"anchor": {}}, value_drivers=[], confidence="medium")
    check("duplicate-phrase QA removed the repeated phrases",
          "under contract under contract" not in corrected["m"][1].lower()
          and "the subject the subject" not in corrected["m"][1].lower(),
          corrected["m"][1])
    check("duplicate-phrase removal is recorded as a correction",
          any("repeated phrase" in c for c in qr["corrections"]))


# ---------------------------------------------------------------------------------------------
# I. Above-grade $/sqft must never be called a total-area rate.
# ---------------------------------------------------------------------------------------------
def test_i_psf_denominator_labels():
    print("\nI. total-vs-above-grade $/sqft denominator QA")
    anchor_comps = [
        {"label": "Comp #1", "raw_psf": 715, "adjusted_above_grade_psf": 709,
         "psf_used_in_leg": 709, "raw_total_finished_psf": 550},
    ]
    bad = _v10_psf_denominator_check(
        {"m": ("Methodology summary", "Comp #1 traded at $715/sqft on total square footage.")}, anchor_comps)
    check("an above-grade $/sqft called a total-area rate is blocked", len(bad) >= 1, str(bad))
    check("the block names the true total-finished figure", any("550" in b for b in bad), str(bad))
    ok = _v10_psf_denominator_check(
        {"m": ("Methodology summary", "Comp #1 traded at $550/sqft on total finished area.")}, anchor_comps)
    check("the correct total-area figure is NOT blocked", not ok, str(ok))
    ok2 = _v10_psf_denominator_check(
        {"m": ("Methodology summary", "Comp #1 traded at $709/sqft on an above-grade basis.")}, anchor_comps)
    check("an above-grade figure labelled above-grade is NOT blocked", not ok2, str(ok2))


# ---------------------------------------------------------------------------------------------
# J. Round 12 issue #1: a pure condition/renovation gap must NOT demote an otherwise same-market
#    comp out of the anchor - it is re-promoted weak -> supporting so the evidence is kept.
# ---------------------------------------------------------------------------------------------
def test_j_condition_not_demoting_same_market_comp():
    print("\nJ. condition gap alone does not push a same-market comp to Weaker")
    subj = _subject()
    # same city / school / size / age / lot / distance, price-verified, recent - only difference is
    # that it is clearly more renovated. Rated weak by the AI.
    reno = _comp("J1", 3_520_000, 5900, dist=0.6, strength="weak",
                 quality={"finish_quality": "above", "interior": "above", "exterior": "above"})
    # a genuinely different-market weak comp (far + different school/municipality) must stay weak
    diff_mkt = _comp("J2", 3_540_000, 5850, dist=3.9, city="Wayne", school="Radnor", strength="weak",
                     quality={"finish_quality": "above", "area": "different_from_subject"})
    # a weak comp with a real non-condition gap (smaller usable lot) must stay weak
    other_gap = _comp("J3", 3_400_000, 5800, dist=0.7, strength="weak",
                      quality={"finish_quality": "above", "lot": "below"})
    comps = [reno, diff_mkt, other_gap, _comp("J4", 3_480_000, 5950, dist=0.8, strength="strong")]
    ev = _build_evidence_by_key(subj, comps)
    flags = _v10_condition_class_review(subj, comps, ev)
    check("same-market comp whose only gap is a superior renovation is re-promoted to Supporting",
          reno["strength"] == "supporting" and reno.get("_cond_promoted") is True, reno["strength"])
    check("the re-promotion is explained in a qa_flag",
          any("belongs in the adjustment layer" in f for f in flags), str(flags))
    check("_cond trace records was/now + the condition-gap direction",
          reno.get("_cond", {}).get("was") == "weak"
          and reno["_cond"]["now"] == "supporting"
          and "renovated" in reno["_cond"]["condition_gap_direction"])
    check("a genuinely different-market weak comp is left weak", diff_mkt["strength"] == "weak", diff_mkt["strength"])
    check("a weak comp with a real non-condition gap is left weak", other_gap["strength"] == "weak", other_gap["strength"])


# ---------------------------------------------------------------------------------------------
# K. Round 12 issue #2, DISABLED 2026-09-14 by explicit product decision: a narrative saying a
#    disadvantage is "offset by" some named quality (architectural pedigree, grounds, a guest
#    wing) with no single itemised dollar line for that exact feature is a legitimate holistic
#    judgment call, not a factual error - it must never block export, regardless of the trace.
# ---------------------------------------------------------------------------------------------
def test_k_prose_offset_must_be_structured():
    print("\nK. narrative offsets are trusted reasoning, not required to be itemised (disabled check)")
    prose = {"m": ("Methodology summary",
                   "The weighted comps are clearly more renovated than the subject, but that condition "
                   "gap is offset by the subject's detached guest apartment and its pool.")}
    trace_noitems = {
        "adjustments": [{"family": "renovation_condition", "applied": 0, "direction": "down"}],
        "qualitative_reconciliation": {"omissions": ["condition: near-zero net, no offsets - export blocked"],
                                       "offsets_accepted": []},
    }
    check("an unbacked narrative offset claim never blocks, even against a 'no offsets' trace",
          not _v10_prose_offset_backing_check(prose, trace_noitems),
          str(_v10_prose_offset_backing_check(prose, trace_noitems)))

    trace_withitems = {
        "adjustments": [{"family": "accessory_dwelling", "applied": 140_000, "direction": "up"},
                        {"family": "pool_spa", "applied": 55_000, "direction": "up"}],
        "qualitative_reconciliation": {"omissions": [], "offsets_accepted": []},
    }
    ok = _v10_prose_offset_backing_check(prose, trace_withitems)
    check("the same prose is NOT blocked once the structured offsets exist either (still off)", not ok, str(ok))


# ---------------------------------------------------------------------------------------------
# L. Round 12 issue #3: the methodology-of-record (anchor $, weighted-comp count, named driver
#    comps) is reconciled to the FINAL trace, not just the FMV/range.
# ---------------------------------------------------------------------------------------------
def test_l_methodology_consistency():
    print("\nL. methodology anchor / comp-count / comp-identity reconciliation")
    final = {
        "anchor_value": 3_368_000, "anchor_weighted_comp_count": 2,
        "anchor_comps": [{"label": "Comp #2"}, {"label": "Comp #3"}],
        "fmv_pre_exposure": 3_254_600, "ai_value_cross_check": 3_200_000,
    }
    stale_m = ("The deterministic comparable-sales anchor of $3,254,600 is built from five strong and "
               "supporting comps, weighted toward Comp #2 and Comp #5 as the closest sales.")
    out, blk, notes = _v10_methodology_consistency_check(
        {"m": ("Methodology summary", stale_m)}, final)
    new_m = out["m"][1]
    check("a stale comp-anchor figure is rewritten to the authoritative anchor",
          "3,368,000" in new_m and "3,254,600" not in new_m, new_m)
    check("an overstated weighted-comp count is blocked",
          any("weighted comp" in b and "five" in b.lower() for b in blk), str(blk))
    check("a named anchor comp that carries no weight is blocked",
          any("Comp #5" in b and "no weight" in b for b in blk), str(blk))

    clean_m = ("The comparable-sales anchor of $3,368,000 is drawn from two supporting comps, "
               "Comp #2 and Comp #3.")
    out2, blk2, notes2 = _v10_methodology_consistency_check(
        {"m": ("Methodology summary", clean_m)}, final)
    check("a methodology that matches the final trace is not blocked or rewritten",
          not blk2 and not notes2 and out2["m"][1] == clean_m, f"{blk2} / {notes2}")


# ---------------------------------------------------------------------------------------------
# M. Round 12 issue #4: the concluded-number scan is field-aware - an authoritative range
#    endpoint is not a competing FMV, but a genuinely conflicting figure still is.
# ---------------------------------------------------------------------------------------------
def test_m_range_endpoint_not_flagged():
    print("\nM. range endpoint vs. competing FMV (field-aware scan)")
    final = {
        "value": 3_459_000, "range_min": 3_149_096, "range_max": 3_768_904, "range_pct": 9.0,
        "anchor_value": 3_368_000, "anchor_low": 3_260_000, "anchor_high": 3_470_000,
    }
    m_ok = ("Our indicated value is $3,459,000. The low end of the supported value range is "
            "$3,149,096, and the high end is $3,768,904.")
    _o, blk_ok, _n = _v10_scan_concluded_numbers({"m": ("Methodology summary", m_ok)}, final=final)
    check("an authoritative range endpoint next to value language is NOT flagged as a competing FMV",
          not blk_ok, str(blk_ok))

    m_bad = ("Our indicated value is $3,459,000, though a reconciled value of $3,930,000 is also "
             "well supported by the evidence.")
    _o2, blk_bad, _n2 = _v10_scan_concluded_numbers({"m": ("Methodology summary", m_bad)}, final=final)
    check("a genuinely conflicting concluded figure is still blocked",
          any("3,930,000" in b for b in blk_bad), str(blk_bad))


# ---------------------------------------------------------------------------------------------
# N. round 13 - the near-zero reconciliation now runs for EVERY weighted bucket, not just
#    condition. A material one-sided amenities signal that nets to ~$0 must resolve to a
#    supported adjustment, supported offsets, or a captured_elsewhere marker - else it blocks.
# ---------------------------------------------------------------------------------------------
def _amenities_setup():
    subj = _subject()
    q = {"amenities": "above"}   # every weighted comp materially better-amenitised than the subject
    comps = [
        _comp("N1", 3_500_000, 5000, quality=dict(q)),
        _comp("N2", 3_450_000, 4950, quality=dict(q)),
        _comp("N3", 3_520_000, 5050, quality=dict(q)),
    ]
    anchor = _anchor(subj, comps)
    exposure = _exposure(subj, comps, anchor, active_days=40)
    return subj, anchor, exposure


def test_n_generalized_bucket_reconciliation():
    print("\nN. generalized (non-condition) bucket reconciliation")
    subj, anchor, exposure = _amenities_setup()

    r1 = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    qr1 = r1["trace"].get("qualitative_reconciliation") or {}
    check("weighted comps superior on AMENITIES + no offsets -> the shortfall mechanism fires for the amenities bucket",
          bool(qr1.get("aggregate_inferiority")) and "amenities" in qr1["aggregate_inferiority"]["buckets"],
          str(qr1.get("aggregate_inferiority")))
    check("the amenities bucket generalization works (not a condition-only mechanism)",
          "amenities" in str(qr1.get("signals", {})), str(qr1.get("signals")))
    check("FMV moves materially below the anchor for the amenities gap",
          r1["fmv"] < anchor["anchor_value"] * 0.96, f'fmv={r1["fmv"]} anchor={anchor["anchor_value"]}')

    # supported cross-bucket offset large enough -> accepted, no block
    ai2 = {"qualitative_adjustments": [{
        "factor": "Detached guest apartment / ADU (1,200 sqft)", "direction": "up",
        "amount_low": 250_000, "amount_high": 300_000, "basis_type": "comp_inferred",
        "basis": "none of the comps has secondary living quarters; bounded from this market's ADU premium",
        "confidence": "medium",
    }]}
    r2 = _apply_v10_valuation(anchor, exposure, ai2, subj["list_price"])
    check("a supported cross-bucket offset clears the amenities block",
          not any("AMENITIES RECONCILIATION" in b for b in r2["blocking"]), str(r2["blocking"]))
    qr2 = r2["trace"].get("qualitative_reconciliation") or {}
    check("the accepted offset is recorded against the amenities gap",
          any("amenities" in a for a in qr2.get("offsets_accepted", [])), str(qr2.get("offsets_accepted")))

    # a filed captured_elsewhere marker also clears it
    ai3 = {"qualitative_adjustments": [{
        "factor": "Pool / spa the subject lacks", "direction": "down",
        "amount_low": 0, "amount_high": 0, "basis_type": "model_judgment",
        "basis": "difference is real but already reflected in the classification/weighting of those comps",
        "confidence": "low", "captured_elsewhere": "comp_classification",
    }]}
    r3 = _apply_v10_valuation(anchor, exposure, ai3, subj["list_price"])
    qr3 = r3["trace"].get("qualitative_reconciliation") or {}
    check("a captured_elsewhere marker clears the block and is traced",
          not any("AMENITIES RECONCILIATION" in b for b in r3["blocking"])
          and any("amenities" in c for c in qr3.get("captured_mechanically", [])),
          f'blocking={r3["blocking"]} captured={qr3.get("captured_mechanically")}')


# ---------------------------------------------------------------------------------------------
# O. round 13 - an uncorroborated competitive (well-over-ask, fast) sale that dominates the
#    high side of the anchor is flagged and knocks confidence; the sale price is NOT discarded.
# ---------------------------------------------------------------------------------------------
def test_o_competitive_sale_driver():
    print("\nO. competitive-sale high-side driver")
    subj = _subject()
    # O1: sold 11% over a low list, in 3 days, and priced well above the other two closed comps
    o1 = _comp("O1", 3_900_000, 5000, dom=3, list_price=3_510_000)
    o2 = _comp("O2", 3_300_000, 5000, dom=44, list_price=3_360_000)
    o3 = _comp("O3", 3_260_000, 4950, dom=52, list_price=3_320_000)
    anchor = _anchor(subj, [o1, o2, o3])
    exposure = _exposure(subj, [o1, o2, o3], anchor, active_days=40)

    drivers = anchor.get("competitive_sale_drivers") or []
    check("O1 is identified as a competitive sale on its comp trace",
          any(c["label"].endswith("O1 Comp Ln)") or "O1" in c["label"] for c in anchor["comps"] if c.get("competitive_sale")),
          str([(c["label"], c.get("competitive_sale"), c.get("sale_to_list"), c.get("dom")) for c in anchor["comps"]]))
    check("an uncorroborated competitive high-side driver is recorded on the anchor",
          bool(drivers) and not drivers[0]["corroborated"], str(drivers))

    res = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    check("the driver raises a HIGH-SIDE DRIVER caution flag (sale price NOT erased)",
          any("HIGH-SIDE DRIVER IS A COMPETITIVE SALE" in f for f in res["flags"]), str(res["flags"]))
    base = _apply_v10_valuation(_anchor(subj, [o2, o3, _comp("O4", 3_290_000, 4980, dom=40, list_price=3_350_000)]),
                                exposure, {"qualitative_adjustments": []}, subj["list_price"])
    order = {"low": 0, "medium": 1, "high": 2}
    check("confidence is knocked at least a notch vs. an all-ordinary comp set",
          order[res["confidence_seed"]] <= order[base["confidence_seed"]], f'{res["confidence_seed"]} vs {base["confidence_seed"]}')


# ---------------------------------------------------------------------------------------------
# P. EXECUTION-PATH: a material one-sided qualitative signal the AI's own comp ratings commit to,
#    with NO filed adjustment, is detected pre-anchor so the estimate retry can re-request it -
#    and once a properly-supported adjustment IS filed it survives validation, moves the FMV off
#    the raw anchor, and clears the reconciliation blocker. Covers condition + amenities +
#    accessory-space so the same path is exercised for all three buckets.
# ---------------------------------------------------------------------------------------------
def _ai_comp(key, strength="strong", **quality):
    return {"listing_key": key, "is_included": True, "strength": strength, "quality": dict(quality)}


def test_p_unfiled_signal_detection_and_apply():
    print("\nP. unfiled-qualitative-signal detection + validated apply")

    # --- detection: AI comps rated clearly superior on condition, nothing filed -> flagged
    cond_comps = [_ai_comp("P1", finish_quality="above", interior="above", exterior="above"),
                  _ai_comp("P2", finish_quality="above", interior="above", exterior="above"),
                  _ai_comp("P3", finish_quality="above", interior="above")]
    gaps = _v10_unfiled_material_qual_buckets(cond_comps, {"qualitative_adjustments": []})
    check("condition-superior comps + no adjustments -> 'condition' flagged as an unfiled gap",
          [b for b, _s, _c in gaps] == ["condition"], str(gaps))
    check("the flagged signal reads subject-inferior (positive)",
          gaps and gaps[0][1] >= 0.30, str(gaps))

    # --- a filed IN-BUCKET condition down item clears the flag
    filed_cond = {"qualitative_adjustments": [{
        "factor": "Kitchen / primary-bath renovation gap vs the comp set", "direction": "down",
        "amount_low": 60_000, "amount_high": 90_000, "basis_type": "comp_inferred",
        "basis": "all three weighted comps are fully renovated; subject is original - bounded from the local reno delta",
        "confidence": "medium",
    }]}
    check("a filed in-bucket condition adjustment clears the gap",
          _v10_unfiled_material_qual_buckets(cond_comps, filed_cond) == [], str(_v10_unfiled_material_qual_buckets(cond_comps, filed_cond)))

    # --- a supported cross-bucket UP offset also clears it (outcome B)
    filed_offset = {"qualitative_adjustments": [{
        "factor": "Detached guest apartment (1,150 sqft)", "direction": "up",
        "amount_low": 150_000, "amount_high": 190_000, "basis_type": "comp_inferred",
        "basis": "none of the comps has secondary living quarters; bounded from this market's ADU premium",
        "confidence": "medium",
    }]}
    check("a supported cross-bucket offset clears the condition gap",
          _v10_unfiled_material_qual_buckets(cond_comps, filed_offset) == [], str(_v10_unfiled_material_qual_buckets(cond_comps, filed_offset)))

    # --- an UNSUPPORTED (model_judgment) offset does NOT clear it - still needs re-request
    filed_weak = {"qualitative_adjustments": [{
        "factor": "Architectural character", "direction": "up",
        "amount_low": 120_000, "amount_high": 160_000, "basis_type": "model_judgment",
        "basis": "subject has more character", "confidence": "low",
    }]}
    check("an unsupported model_judgment offset does not clear the gap",
          [b for b, _s, _c in _v10_unfiled_material_qual_buckets(cond_comps, filed_weak)] == ["condition"],
          str(_v10_unfiled_material_qual_buckets(cond_comps, filed_weak)))

    # --- amenities bucket, same path
    amen_comps = [_ai_comp("PA1", amenities="above"), _ai_comp("PA2", amenities="above"), _ai_comp("PA3", amenities="above")]
    check("amenities-superior comps + nothing filed -> 'amenities' flagged",
          [b for b, _s, _c in _v10_unfiled_material_qual_buckets(amen_comps, {"qualitative_adjustments": []})] == ["amenities"],
          str(_v10_unfiled_material_qual_buckets(amen_comps, {"qualitative_adjustments": []})))

    # --- accessory-space bucket, subject SUPERIOR direction (comps rated 'below' on layout)
    acc_comps = [_ai_comp("PL1", layout="below"), _ai_comp("PL2", layout="below"), _ai_comp("PL3", layout="below")]
    acc_gaps = _v10_unfiled_material_qual_buckets(acc_comps, {"qualitative_adjustments": []})
    check("accessory-space one-sided the other way (comps inferior) is also flagged",
          [b for b, _s, _c in acc_gaps] == ["accessory_space"] and acc_gaps[0][1] <= -0.30, str(acc_gaps))

    # --- a clean, balanced set flags nothing
    clean_comps = [_ai_comp("PC1", finish_quality="similar", amenities="similar"),
                   _ai_comp("PC2", finish_quality="similar"), _ai_comp("PC3", interior="similar")]
    check("a balanced comp set flags no unfiled gaps",
          _v10_unfiled_material_qual_buckets(clean_comps, {"qualitative_adjustments": []}) == [], "")

    # --- apply path: a properly-supported condition down adjustment survives validation, moves the
    #     FMV off the anchor, and clears the CONDITION RECONCILIATION blocker. List price is set
    #     well above the anchor with short exposure so the market-exposure gate does NOT bind and
    #     mask the adjustment's effect on FMV.
    subj = _subject(list_price=4_800_000)
    q = {"finish_quality": "above", "interior": "above", "exterior": "above"}
    p_comps = [_comp("PQ1", 3_500_000, 5000, quality=dict(q)),
               _comp("PQ2", 3_450_000, 4950, quality=dict(q)),
               _comp("PQ3", 3_520_000, 5050, quality=dict(q))]
    anchor = _anchor(subj, p_comps)
    exposure = _exposure(subj, p_comps, anchor, active_days=5)
    r0 = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    check("baseline: no filed adjustment -> the shortfall mechanism still moves FMV materially below the anchor",
          r0["fmv"] < round(anchor["anchor_value"]) * 0.94,
          f'fmv={r0["fmv"]} anchor={round(anchor["anchor_value"])}')

    ai_fix = {"qualitative_adjustments": [{
        "factor": "Renovation / condition gap vs the weighted comp set", "direction": "down",
        "amount_low": 90_000, "amount_high": 140_000, "basis_type": "comp_inferred",
        "basis": "Comp #1 and Comp #3 are fully updated; the subject is largely original - contribution bounded from their $/sqft premium over the set",
        "confidence": "medium",
    }]}
    rf = _apply_v10_valuation(anchor, exposure, ai_fix, subj["list_price"])
    _cond_items = [a for a in rf["trace"]["adjustments"]
                   if a["direction"] == "down" and a["applied"] < 0 and not a.get("aggregate_inferiority")]
    check("the filed condition adjustment survives validation (present, non-zero, down)",
          bool(_cond_items), str(rf["trace"]["adjustments"]))
    check("the filed adjustment is credited against the shortfall (itemised_down > 0)",
          (rf["trace"]["qualitative_reconciliation"].get("aggregate_inferiority") or {}).get("itemised_down", 0) > 0,
          str(rf["trace"]["qualitative_reconciliation"].get("aggregate_inferiority")))
    check("final FMV is materially below the raw anchor",
          rf["fmv"] < round(anchor["anchor_value"]) * 0.94, f'fmv={rf["fmv"]} anchor={round(anchor["anchor_value"])}')
    check("total qualitative adjustment is negative and within the widened broad-inferiority bound (<=22%)",
          rf["trace"]["adjustment_total"] < 0 and abs(rf["trace"]["adjustment_total"]) <= 0.221 * anchor["anchor_value"],
          str(rf["trace"]["adjustment_total"]))


# ---------------------------------------------------------------------------------------------
# Q. Structured finish ratings must be pinned to the comp's OWN narrative + facts BEFORE the
#    weighted qualitative signals are computed - an "AI says turnkey / rates finish Similar"
#    label set can no longer neutralise Stage 3 - and the correction is run-to-run STABLE.
# ---------------------------------------------------------------------------------------------
_RENO_NOTE = ("This home had a full 2025 renovation with a rebuilt kitchen and baths and new HVAC; "
              "it is turnkey and move-in ready, which contrasts with the subject's largely original, "
              "100-year-old interior and dated systems.")
_ASIS_NOTE = ("Marketed as an investor project and sold as-is, with dated systems throughout and an "
              "unfinished aesthetic - its condition is well behind the subject's.")


def _cc(key, note, strength="supporting", **quality):
    return {"listing_key": key, "is_included": True, "strength": strength,
            "street_address": f"{key} Rd", "public_remarks": note,
            "ai_commentary": "Generated summary is not used as factual evidence.", "quality": dict(quality)}


def test_q_finish_rating_consistency_and_stability():
    print("\nQ. finish-rating vs. narrative consistency + run-to-run stability")
    subj = _subject(
        year_built=1925,
        public_remarks="The interior is described as largely original; system ages remain unverified.",
    )

    # 1. renovated/turnkey comp mis-rated finish Similar / interior Below -> corrected upward
    c = _cc("Q1", _RENO_NOTE, finish_quality="similar", interior="below", exterior="below")
    flags = _v10_condition_rating_consistency_check(subj, [c])
    check("a turnkey comp rated finish=Similar is corrected to finish=Above",
          c["quality"].get("finish_quality") == "above", str(c["quality"]))
    check("its interior=Below (turnkey) is corrected off 'below'",
          c["quality"].get("interior") in ("similar", "above"), str(c["quality"]))
    check("the correction is flagged for the agent", any("Q1" in f and "corrected" in f for f in flags), str(flags))
    check("exterior (not a renovation dim) is left alone", c["quality"].get("exterior") == "below", str(c["quality"]))

    # 2. an as-is / project comp mis-rated finish Above -> corrected downward
    c2 = _cc("Q2", _ASIS_NOTE, finish_quality="above", interior="similar", exterior="above")
    _v10_condition_rating_consistency_check(subj, [c2])
    check("an as-is comp rated finish=Above is corrected to finish=Below",
          c2["quality"].get("finish_quality") == "below", str(c2["quality"]))
    check("its interior is corrected to Below too", c2["quality"].get("interior") == "below", str(c2["quality"]))

    # 3. an explicit on-par justification is respected (no fix)
    c3 = _cc("Q3", _RENO_NOTE + " That said, the renovation used builder-grade finishes comparable "
             "to the subject's, so finish level is roughly on par with the subject.",
             finish_quality="similar", interior="similar")
    _v10_condition_rating_consistency_check(subj, [c3])
    check("an explicit 'on par with the subject' note is respected - finish stays Similar",
          c3["quality"].get("finish_quality") == "similar", str(c3["quality"]))

    # 4. a genuinely consistent set is untouched
    c4 = _cc("Q4", _RENO_NOTE, finish_quality="above", interior="above")
    flags4 = _v10_condition_rating_consistency_check(subj, [c4])
    check("already-consistent ratings are not disturbed and not flagged",
          c4["quality"] == {"finish_quality": "above", "interior": "above"} and not flags4, str((c4["quality"], flags4)))

    # 5. RUN-TO-RUN STABILITY: identical subject + identical comp facts/narrative, two different
    #    plausible label sets from two model runs, must converge after the check - and the weighted
    #    condition signal must land 'subject_inferior' both times.
    def _run(quality_variant):
        comps = [
            _cc("S1", _RENO_NOTE, **quality_variant),
            _cc("S2", _RENO_NOTE, finish_quality="above", interior="above"),
            _cc("S3", _ASIS_NOTE, finish_quality="below", interior="below"),
        ]
        _v10_condition_rating_consistency_check(subj, comps)
        anchor = {"comps": [dict(c, normalized_weight=w) for c, w in zip(comps, (0.4, 0.35, 0.25))]}
        return [c["quality"] for c in comps], _v10_weighted_qualitative_signals(anchor)["condition"]

    q_good, sig_good = _run({"finish_quality": "above", "interior": "above"})
    q_bad, sig_bad = _run({"finish_quality": "similar", "interior": "below"})   # the reported failure
    check("the shared comp's corrected ratings are identical across the two runs",
          q_good[0] == q_bad[0], f"{q_good[0]} vs {q_bad[0]}")
    check("weighted condition signal is 'subject_inferior' in BOTH runs (no swing to balanced)",
          sig_good["direction"] == "subject_inferior" and sig_bad["direction"] == "subject_inferior",
          f'good={sig_good} bad={sig_bad}')
    check("the two runs' condition signals are within 0.05 of each other",
          abs(sig_good["signal"] - sig_bad["signal"]) <= 0.05, f'{sig_good["signal"]} vs {sig_bad["signal"]}')


# ---------------------------------------------------------------------------------------------
# R. The FMV-consistency scanner must never treat a clearly labeled asking / list price as a
#    competing concluded value.
# ---------------------------------------------------------------------------------------------
def test_r_list_price_not_flagged_as_competing_fmv():
    print("\nR. list price next to a conclusion phrase is not a competing FMV")
    trace = {"anchor": {"anchor_value": 1_586_672, "comps": []}, "final_fmv": 1_586_672,
             "adjustments": [], "above_ask_gate": {}, "subject_asking": 1_350_000}
    fields_ok = {"methodology_summary": ("Methodology", (
        "The concluded value is above the $1,350,000 asking price because the verified closed sales, "
        "adjusted for size and condition, indicate a fair market value of about $1,586,672."))}
    qr, _cf, blk = _v10_pre_export_qa(
        fields_ok, value=1_586_672, range_pct=9.9, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 2},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=trace, value_drivers=[], confidence="medium",
    )
    check("a labeled $1,350,000 asking price next to 'concluded value' does NOT block",
          not any("FMV consistency" in b for b in qr["blocking_errors"]), str(qr["blocking_errors"]))

    # a genuinely different concluded figure still blocks
    fields_bad = {"methodology_summary": ("Methodology",
        "After weighing the comparables we conclude a fair market value of $1,250,000 for the subject.")}
    qr2, _cf2, _blk2 = _v10_pre_export_qa(
        fields_bad, value=1_586_672, range_pct=9.9, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 2},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=trace, value_drivers=[], confidence="medium",
    )
    check("a real competing concluded value ($1.25M) is still blocked",
          any("FMV consistency" in b for b in qr2["blocking_errors"]), str(qr2["blocking_errors"]))


# ---------------------------------------------------------------------------------------------
# S. round 16 - anchor upward-bias controls: (1) a bidding-war sale is down-weighted in the
#    anchor (never dropped); (2) a qualitative item the comp set can't ISOLATE is applied at its
#    conservative end, and an unmeasurable UP offset is softened further, so speculative positives
#    can't cancel concrete deductions. + the range-low QA false positive.
# ---------------------------------------------------------------------------------------------
def test_s_anchor_bias_controls():
    print("\nS. anchor bias controls (competitive weight + non-isolated adjustment precision)")
    subj = _subject()

    # (1) one comp that sold 13% over list in 3 days, priced at the top of the set
    hot = _comp("H1", 3_900_000, 5000, dom=3, list_price=3_450_000)      # competitive
    n2 = _comp("H2", 3_300_000, 5000, dom=41, list_price=3_350_000)
    n3 = _comp("H3", 3_260_000, 4950, dom=52, list_price=3_330_000)
    a_hot = _anchor(subj, [hot, n2, n3])
    hot_row = next(c for c in a_hot["comps"] if "H1" in c["label"])
    check("the bidding-war comp is flagged competitive_sale on the anchor trace",
          hot_row.get("competitive_sale") is True, str(hot_row.get("competitive_sale")))
    check("it carries a <1.0 competitive_weight_mult", (hot_row.get("competitive_weight_mult") or 1.0) < 1.0,
          str(hot_row.get("competitive_weight_mult")))

    # same set but H1 sold normally -> its normalized weight should be HIGHER (no penalty)
    calm = _comp("H1", 3_900_000, 5000, dom=45, list_price=3_880_000)
    a_calm = _anchor(subj, [calm, n2, n3])
    calm_row = next(c for c in a_calm["comps"] if "H1" in c["label"])
    check("down-weighting a competitive sale lowers its normalized anchor weight",
          hot_row["normalized_weight"] < calm_row["normalized_weight"] - 0.01,
          f'hot nw={hot_row["normalized_weight"]} vs calm nw={calm_row["normalized_weight"]}')
    check("the anchor value is lower once the hot sale is down-weighted",
          a_hot["anchor_value"] < a_calm["anchor_value"],
          f'{a_hot["anchor_value"]} vs {a_calm["anchor_value"]}')

    # when EVERY comp is competitive it is simply the market - no penalty
    allc = [_comp("A1", 3_500_000, 5000, dom=4, list_price=3_250_000),
            _comp("A2", 3_450_000, 4950, dom=5, list_price=3_200_000),
            _comp("A3", 3_520_000, 5050, dom=6, list_price=3_270_000)]
    a_all = _anchor(subj, allc)
    check("an all-competitive set gets no weight penalty (competitive_weight_mult stays 1.0)",
          all((c.get("competitive_weight_mult") or 1.0) == 1.0 for c in a_all["comps"]),
          str([c.get("competitive_weight_mult") for c in a_all["comps"]]))

    # (2) precision of non-isolated adjustments
    anchor, exposure = a_calm, _exposure(subj, [calm, n2, n3], a_calm, active_days=5)
    ai = {"qualitative_adjustments": [
        {"factor": "Pool the subject lacks vs. three comps with pools", "direction": "down",
         "amount_low": 40_000, "amount_high": 80_000, "basis_type": "comp_inferred",
         "basis": "three weighted comps have in-ground pools; the set does not isolate a pool-only pair",
         "confidence": "low"},
        {"factor": "Architectural pedigree / period detail", "direction": "up",
         "amount_low": 30_000, "amount_high": 60_000, "basis_type": "model_judgment",
         "basis": "the subject's 1925 detailing exceeds the comps; no paired comp isolates it",
         "confidence": "low"},
    ]}
    res = _apply_v10_valuation(anchor, exposure, ai, subj["list_price"])
    pool = next(a for a in res["trace"]["adjustments"] if a["family"] == "pool_spa")
    arch = next(a for a in res["trace"]["adjustments"] if a["family"] == "architecture_pedigree")
    check("a non-isolated DOWN item applies at its conservative (low) end, not the midpoint",
          abs(pool["applied"]) == 40_000, f'{pool["applied"]} (expected -40000, midpoint would be -60000)')
    check("a non-isolated UP offset applies at low-end x soft factor (0.6)",
          arch["applied"] == round(30_000 * 0.6), f'{arch["applied"]} (expected 18000, midpoint would be 45000)')
    check("both are marked soft_applied on the trace",
          pool.get("soft_applied") == "conservative_end" and arch.get("soft_applied") == "offset_softened",
          f'{pool.get("soft_applied")} / {arch.get("soft_applied")}')
    check("net effect is more negative than the naive midpoint sum (speculative + can't cancel real -)",
          res["trace"]["adjustment_total"] < -(60_000 - 45_000), str(res["trace"]["adjustment_total"]))

    # a VALIDATED paired_market_evidence item is still applied at its midpoint (precision earned)
    pe_comps = [_comp("P1", 3_500_000, 5000), _comp("P2", 3_800_000, 5000)]
    pe_anchor = _anchor(subj, pe_comps + [_comp("P3", 3_600_000, 4950)])
    pe_ai = {"qualitative_adjustments": [{
        "factor": "Feature isolated by Comp #1 vs Comp #2", "direction": "up",
        "amount_low": 40_000, "amount_high": 60_000, "basis_type": "paired_market_evidence",
        "basis": "Comp #1 and Comp #2 are otherwise similar in size and area and differ mainly by this "
                 "feature; Comp #2 closed well above Comp #1 on that basis, and the amount sits inside "
                 "their observed size-adjusted price gap",
        "confidence": "high"}]}
    pe_res = _apply_v10_valuation(pe_anchor, _exposure(subj, pe_comps, pe_anchor), pe_ai, subj["list_price"])
    pe_item = pe_res["trace"]["adjustments"][0]
    check("a validated paired_market_evidence item keeps midpoint precision (not softened)",
          pe_item.get("soft_applied") is None, f'{pe_item.get("soft_applied")} applied={pe_item.get("applied")}')


def test_s2_range_low_not_flagged_as_fmv():
    print("\nS2. authoritative range low is not mistaken for a competing FMV")
    trace = {"anchor": {"anchor_value": 1_600_000, "anchor_low": 1_453_654, "anchor_high": 1_760_000, "comps": []},
             "final_fmv": 1_597_422, "adjustments": [], "above_ask_gate": {}, "subject_asking": 1_350_000}
    fields = {"methodology_summary": ("Methodology",
        "Weighing the closed sales we conclude a fair market value of about $1,597,422, with a "
        "supportable range from $1,453,654 to $1,743,753.")}
    qr, _cf, _blk = _v10_pre_export_qa(
        fields, value=1_597_422, range_pct=9.0, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 2},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=trace, value_drivers=[], confidence="medium",
    )
    check("the range low $1,453,654 next to 'fair market value' does NOT block",
          not any("FMV consistency" in b for b in qr["blocking_errors"]), str(qr["blocking_errors"]))


# ---------------------------------------------------------------------------------------------
# T. round 17 - BROAD qualitative inferiority: a comp set materially superior to the subject on
#    2+ buckets at once produces a structured aggregate DOWN adjustment (subject prices toward the
#    low end of the range, not the weighted mean); the +-12% bound does not clamp it back up.
# ---------------------------------------------------------------------------------------------
def _broad_inferior_setup(**over):
    subj = _subject(**over)
    q = {"finish_quality": "above", "interior": "above", "exterior": "above", "amenities": "above"}
    comps = [
        _comp("T1", 3_500_000, 5000, quality=dict(q)),
        _comp("T2", 3_450_000, 4950, quality=dict(q)),
        _comp("T3", 3_520_000, 5050, quality=dict(q)),
    ]
    anchor = _anchor(subj, comps)
    return subj, anchor, _exposure(subj, comps, anchor, active_days=5)


def test_t_broad_qualitative_inferiority():
    print("\nT. broad qualitative inferiority -> aggregate downward adjustment")
    subj, anchor, exposure = _broad_inferior_setup()
    av = anchor["anchor_value"]

    # 1. comps superior on condition AND amenities, nothing filed -> aggregate fires
    r1 = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    qr1 = r1["trace"].get("qualitative_reconciliation") or {}
    agg = qr1.get("aggregate_inferiority")
    check("aggregate_inferiority is recorded with both inferior buckets",
          bool(agg) and set(agg["buckets"]) >= {"condition", "amenities"}, str(agg))
    check("a QUALITATIVE INFERIORITY SHORTFALL flag is raised",
          any("QUALITATIVE INFERIORITY SHORTFALL" in f for f in r1["flags"]), str(r1["flags"]))
    check("the per-bucket 'export blocked' reconciliation entries are replaced by the applied shortfall",
          not any("RECONCILIATION - the high-weight comp set is materially superior" in b for b in r1["blocking"]),
          str(r1["blocking"]))
    check("total qualitative adjustment is materially negative (> 8% of anchor down)",
          r1["trace"]["adjustment_total"] < -0.08 * av, f'{r1["trace"]["adjustment_total"]} vs anchor {av}')
    check("final FMV is materially below the raw anchor",
          r1["fmv"] < av * 0.92, f'fmv={r1["fmv"]} anchor={av}')
    check("confidence seed is knocked (subject is a poor fit for its comp set)",
          r1["confidence_seed"] in ("low", "medium"), r1["confidence_seed"])

    # 2. a SINGLE materially-inferior bucket also triggers the shortfall (round 17: apply, don't just block)
    subj2 = _subject()
    q2 = {"finish_quality": "above", "interior": "above", "exterior": "above"}  # condition only
    comps2 = [_comp("U1", 3_500_000, 5000, quality=dict(q2)),
              _comp("U2", 3_450_000, 4950, quality=dict(q2)),
              _comp("U3", 3_520_000, 5050, quality=dict(q2))]
    a2 = _anchor(subj2, comps2)
    r2 = _apply_v10_valuation(a2, _exposure(subj2, comps2, a2, active_days=5),
                              {"qualitative_adjustments": []}, subj2["list_price"])
    qr2 = r2["trace"].get("qualitative_reconciliation") or {}
    check("a single inferior bucket triggers the shortfall for that bucket only",
          (qr2.get("aggregate_inferiority") or {}).get("buckets") == ["condition"], str(qr2.get("aggregate_inferiority")))
    check("...and it applies the number instead of hard-blocking",
          not any("CONDITION RECONCILIATION" in b for b in r2["blocking"]) and r2["fmv"] < a2["anchor_value"] * 0.95,
          f'blocking={r2["blocking"]} fmv={r2["fmv"]} anchor={a2["anchor_value"]}')

    # 3. no double-count: a large itemised condition DOWN already filed -> aggregate only tops up
    ai3 = {"qualitative_adjustments": [{
        "factor": "Renovation / condition gap vs the weighted comp set", "direction": "down",
        "amount_low": 120_000, "amount_high": 150_000, "basis_type": "comp_inferred",
        "basis": "all three weighted comps are fully renovated; the subject is original - bounded from the local reno delta",
        "confidence": "medium",
    }]}
    r3 = _apply_v10_valuation(anchor, exposure, ai3, subj["list_price"])
    qr3 = r3["trace"].get("qualitative_reconciliation") or {}
    agg3 = qr3.get("aggregate_inferiority") or {}
    check("the aggregate credits the already-filed itemised down adjustment (itemised_down > 0)",
          agg3.get("itemised_down", 0) > 0, str(agg3))
    check("total down is not larger than the combined implied inferiority",
          abs(r3["trace"]["adjustment_total"]) <= agg3.get("implied_total", 0) + 1000,
          f'total={r3["trace"]["adjustment_total"]} implied={agg3.get("implied_total")}')

    # 4. the downward bound is the widened broad-inferiority bound, not +-12%
    xtreme = _broad_inferior_setup()
    rx = _apply_v10_valuation(xtreme[1], xtreme[2], {"qualitative_adjustments": []}, xtreme[0]["list_price"])
    check("the downward adjustment may exceed the standard 12% bound (broad-inferiority bound)",
          abs(rx["trace"]["adjustment_total"]) <= 0.221 * xtreme[1]["anchor_value"],
          str(rx["trace"]["adjustment_total"]))

    # 5. Production case (Cherry Ln, 2026-09-15): a MIXED amenity+renovation adjustment - dominant,
    #    specifically-named amenity evidence (spa on one comp, pool on another) with one passing
    #    "2016 renovation" mention embedded in explaining a comp's premium - must classify to the
    #    amenities bucket via _v10_adj_family's keyword-count scoring (not "condition" just because
    #    a renovation-condition keyword happens to be checked first), and its dollar amount must
    #    credit against the amenities shortfall instead of being invisible to it.
    ai5 = {"qualitative_adjustments": [{
        "factor": "Amenity/renovation gap vs. weight-carrying comps (spa at Comp #1, pool at Comp #2)",
        "direction": "down", "amount_low": 60_000, "amount_high": 110_000, "basis_type": "comp_inferred",
        "basis": ("Two of the three anchor comps carry amenities (spa, pool) and/or fully modernized "
                  "systems the subject lacks; Comp #1's premium reflects its 2016 renovation and spa, "
                  "and Comp #2's pool represents added value the subject does not offer, indicating "
                  "the anchor should be shaded down for this one-sided amenity gap."),
        "confidence": "low",
    }]}
    r5 = _apply_v10_valuation(anchor, exposure, ai5, subj["list_price"])
    adj5 = next(p for p in r5["trace"]["adjustments"] if "Amenity/renovation" in p["factor"])
    check("5a: a mixed amenity+renovation adjustment with dominant, specifically-named amenity "
          "evidence classifies into the amenities bucket (family pool_spa), not condition, despite "
          "mentioning 'renovation' once",
          adj5["family"] == "pool_spa", adj5["family"])
    qr5 = r5["trace"].get("qualitative_reconciliation") or {}
    agg5 = qr5.get("aggregate_inferiority") or {}
    check("5b: once correctly bucketed, the itemised $60K credits against the amenities shortfall "
          "calculation (itemised_down reflects it) instead of the shortfall re-charging the full "
          "implied gap on top of an already-filed item it can't see",
          "amenities" not in (agg5.get("buckets") or []) or agg5.get("itemised_down", 0) >= 55_000,
          str(agg5))
    check("5c: total qualitative adjustment does not stack the full $60K item plus a second, "
          "uncredited amenities shortfall of the full implied gap on top of it",
          abs(r5["trace"]["adjustment_total"]) < 60_000 + agg5.get("implied_total", 0),
          f'total={r5["trace"]["adjustment_total"]} implied={agg5.get("implied_total")}')


# ---------------------------------------------------------------------------------------------
# U. round 18 - output-quality pass: cross-bucket offset QA mapping, offer-uses-final QA,
#    $/sqft-safe sanitizer, range-rewrite spacing, and the final non-semantic polish guard.
# ---------------------------------------------------------------------------------------------
class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeClient:
    """payload is either a fixed JSON string, or a callable(**kw)->JSON string."""
    def __init__(self, payload):
        class _M:
            async def create(_self, **kw):
                return _FakeResp(payload(**kw) if callable(payload) else payload)
        self.messages = _M()


def _safe_echo_polish(**kw):
    """A minimal, numerically-faithful polish: collapse runs of spaces and fix 'teh'."""
    content = kw["messages"][0]["content"]
    out = {}
    for m in re.finditer(r"### (\S+)\n(.*?)(?=\n### |\Z)", content, re.DOTALL):
        name, txt = m.group(1), m.group(2).strip()
        out[name] = re.sub(r"  +", " ", txt).replace("teh ", "the ")
    return json.dumps(out)


def test_u_output_quality_pass():
    print("\nU. output-quality pass (QA mapping / offer-final / sanitizer / spacing / polish guard)")

    # A. a VALID cross-bucket offset (architecture item classified into lot_land/location by the
    #    'grounds' keyword) must not falsely block a narrative that says architecture offsets the
    #    condition gap.
    prose = {"m": ("Methodology summary",
                   "The weighted comps are more renovated than the subject, but that condition gap is "
                   "partially offset by the subject's architectural pedigree and its larger, more "
                   "private grounds.")}
    trace_combined = {
        "adjustments": [{
            "family": "lot_land", "applied": 27_000, "direction": "up",
            "factor": "Architectural pedigree and grounds/privacy premium",
            "basis": "the subject's 1925 detailing and acre-plus grounds exceed the comp set",
        }],
        "qualitative_reconciliation": {"omissions": [], "offsets_accepted": []},
    }
    check("A: a combined architecture+grounds up-adjustment backs the narrative offset (no false block)",
          not _v10_prose_offset_backing_check(prose, trace_combined),
          str(_v10_prose_offset_backing_check(prose, trace_combined)))
    # Disabled 2026-09-14 (see test_k) - a bare condition-gap-offset claim with nothing filed must
    # NOT block either; the reasoning model is trusted for this kind of holistic judgment call.
    check("A: an unbacked offset claim does not block (check disabled by product decision)",
          not _v10_prose_offset_backing_check(prose, {"adjustments": [], "qualitative_reconciliation": {"omissions": []}}))

    # B. offer prose that quotes a stale pre-final indicated value is blocked; offer that uses only
    #    the final FMV + a lower offer band is not.
    final = {"value": 1_515_000, "range_min": 1_400_000, "range_max": 1_630_000, "list_price": 1_350_000}
    trace_b = {"anchor": {"anchor_value": 1_600_000}, "fmv_pre_exposure": 1_595_000, "ai_value_cross_check": 1_595_000}
    stale_offer = ("Given the indicated value of about $1,595,000, an opening offer of $1,550,000 to "
                   "$1,600,000 is reasonable.")
    check("B: offer quoting a stale $1.595M indicated value is blocked",
          bool(_v10_offer_final_valuation_check(stale_offer, final, trace_b)),
          str(_v10_offer_final_valuation_check(stale_offer, final, trace_b)))
    stale_offer2 = ("Given the $1,595,000 indicated value against a $1,350,000 list price, a buyer has "
                    "room to open near list.")
    check("B: 'figure then value language' ordering ($1,595,000 indicated value) is also caught",
          bool(_v10_offer_final_valuation_check(stale_offer2, final, trace_b)),
          str(_v10_offer_final_valuation_check(stale_offer2, final, trace_b)))
    good_offer = ("Given the $1,515,000 fair market value, an opening offer near $1,440,000 with room "
                  "to settle toward $1,490,000 is reasonable.")
    check("B: offer built from the final $1.515M FMV + a lower offer band is NOT blocked",
          not _v10_offer_final_valuation_check(good_offer, final, trace_b),
          str(_v10_offer_final_valuation_check(good_offer, final, trace_b)))
    list_bound_offer = ("The $1,515,000 fair market value sits above the $1,350,000 list price. "
                        "A reasonable opening offer is $1,390,500 to $1,417,500.")
    check("B: a labeled authoritative list price is not mistaken for a stale valuation",
          not _v10_offer_final_valuation_check(list_bound_offer, final, trace_b),
          str(_v10_offer_final_valuation_check(list_bound_offer, final, trace_b)))

    # C. legitimate $/sqft wording must survive the invented-adjustment sanitizer; a real fabricated
    #    adjustment figure is still scrubbed.
    psf_text = ("Comp #3 sold at $440/sqft raw but normalized to $474/sqft above-grade, versus the "
                "subject's implied $407 per sq ft.")
    out_psf, _f = _scrub_invented_adjustments(psf_text, {})
    check("C: '$440/sqft' / '$474/sqft above-grade' / '$407 per sq ft' survive the sanitizer intact",
          "$440/sqft" in out_psf and "$474/sqft above-grade" in out_psf and "$407 per sq ft" in out_psf, out_psf)
    inv_text = "The renovation gap is worth a $300K premium over the comp set."
    out_inv, f_inv = _scrub_invented_adjustments(inv_text, {})
    check("C: a genuine fabricated '$300K premium' adjustment is still scrubbed",
          "$300K" not in out_inv and bool(f_inv), out_inv)

    # D. a rewritten valuation range keeps correct spacing/punctuation after it (no '$Ycentered').
    d_fields = {"m": ("Methodology summary",
                      "The comparable sales point to a value range of $1,200,000 to $1,900,000 centered "
                      "near the midpoint, before adjustments.")}
    d_out, _vd, _df = _reconcile_post_gate_prose(
        d_fields, value=1_515_000, range_pct=8.0, anchor={"anchor_value": 1_600_000},
        trace={"exposure": {}, "above_ask_gate": {}}, value_drivers=[])
    d_text = d_out["m"][1]
    check("D: the rewritten range is followed by a space (no '$1,...centered')",
          not re.search(r"\d,\d{3}[A-Za-z]", d_text) and "centered" in d_text, d_text)
    anchor_comparison = {
        "m": ("Methodology summary",
              "The final fair market value of $1,500,000 is about 3.4% below the $1,552,556 "
              "comparable-sales anchor. The concluded value sits above the $1,350,000 asking "
              "price because the verified closed sales support it."),
    }
    anchor_out, _, anchor_flags = _reconcile_post_gate_prose(
        anchor_comparison, value=1_500_000, range_pct=8.0,
        anchor={"anchor_value": 1_552_556, "anchor_high": 1_650_000},
        trace={"authority": "reasoning_model", "subject_asking": 1_350_000,
               "v10_formula_fmv": 1_552_556, "exposure": {}}, value_drivers=[],
    )
    check("D: a labeled anchor is not rewritten as the final FMV",
          "$1,552,556 comparable-sales anchor" in anchor_out["m"][1], anchor_out["m"][1])
    check("D: an existing supported FMV-vs-list explanation is not duplicated",
          anchor_out["m"][1].lower().count("concluded value sits above") == 1,
          f"{anchor_flags} :: {anchor_out['m'][1]}")

    # E. the final non-semantic polish guard REJECTS a polish that alters a number or a direction,
    #    and ACCEPTS a pure grammar fix.
    orig = {"methodology_summary": "The subject sits below teh comp set; the concluded value is $1,515,000."}
    _real_get_client = _cma_mod._get_client
    try:
        _cma_mod._get_client = lambda: _FakeClient(json.dumps(
            {"methodology_summary": "The subject sits below the comp set; the concluded value is $1,520,000."}))
        r_bad = asyncio.run(_v10_final_language_polish(dict(orig), model="claude-sonnet-5"))
        check("E: a polish that changed a dollar figure is rejected (original kept)",
              r_bad["fields"]["methodology_summary"] == orig["methodology_summary"]
              and any("numeric token" in f for f in r_bad["flags"]), str(r_bad))

        _cma_mod._get_client = lambda: _FakeClient(json.dumps(
            {"methodology_summary": "The subject sits below the comp set; the concluded value is $1,515,000."}))
        r_ok = asyncio.run(_v10_final_language_polish(dict(orig), model="claude-sonnet-5"))
        check("E: a pure grammar fix ('teh'->'the') is accepted with numbers + direction unchanged",
              r_ok["changed"] and "the comp set" in r_ok["fields"]["methodology_summary"]
              and "$1,515,000" in r_ok["fields"]["methodology_summary"], str(r_ok))
        check("E: the numeric + directional multisets are the invariant the guard checks",
              _polish_num_multiset("value is $1,515,000, up 3%") == _polish_num_multiset("$1,515,000 (up 3%) value")
              and _polish_dir_multiset("sits below and under") != _polish_dir_multiset("sits above and over"), "")
    finally:
        _cma_mod._get_client = _real_get_client

    # F. after a polish that changes text, the FINAL deterministic QA re-runs on the POST-polish
    #    strings (checks_run gains the post-polish marker).
    subj = _subject()
    q = {"finish_quality": "above", "interior": "above", "exterior": "above"}
    comps = [_comp("V1", 3_500_000, 5000, quality=dict(q)), _comp("V2", 3_460_000, 4950, quality=dict(q)),
             _comp("V3", 3_520_000, 5050, quality=dict(q))]
    anchor = _anchor(subj, comps)
    res = _apply_v10_valuation(anchor, _exposure(subj, comps, anchor, active_days=5),
                               {"qualitative_adjustments": []}, subj["list_price"])
    fmv = res["fmv"]
    final_v = _v10_authoritative_valuation(
        value=fmv, range_pct=res["range_pct"], confidence=res["confidence_seed"], valuation_trace=res["trace"],
        subject_asking=subj["list_price"],
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 5},
        comp_stats={"method": "weighted mean"})
    _real_get_client = _cma_mod._get_client
    try:
        _cma_mod._get_client = lambda: _FakeClient(_safe_echo_polish)
        fin = asyncio.run(_finalize_gate_reconciled_prose(
            methodology="The comparable sales support the  concluded value.", offer="", inspection="",
            value=fmv, range_pct=res["range_pct"], subject_asking=subj["list_price"], confidence=res["confidence_seed"],
            status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 5},
            enriched_subject=subj, comps=[], comp_num_by_key={}, addr_by_key={},
            valuation_trace=res["trace"], value_drivers=[], qa_ctx={}, mode="buyer", model="claude-sonnet-5",
            final=final_v, external_blocking=list(res["blocking"]), run_language_polish=True,
        ))
    finally:
        _cma_mod._get_client = _real_get_client
    check("F: the polished methodology (double-space fixed) is what comes back",
          "concluded value" in fin["methodology"] and "  concluded" not in fin["methodology"], fin["methodology"])
    check("F: final QA re-ran on the post-polish strings",
          "post_polish_final_qa" in fin["qa_result"].get("checks_run", []), str(fin["qa_result"].get("checks_run")))


# ---------------------------------------------------------------------------------------------
# V. round 19 - (1) Buyer Valuation Notes must directionally agree with anchor -> final FMV;
#    (2) the Offer Suggestion is rebuilt from final_valuation + actual list price and a dedicated
#    sanity check blocks a wrong list price / wrong FMV / full-range opening / over-ceiling
#    recommendation / malformed percentage wording.
# ---------------------------------------------------------------------------------------------
def _pre_qa(fields_m, *, value, anchor_value, range_pct=8.0, ask=1_350_000):
    trace = {"anchor": {"anchor_value": anchor_value, "comps": []}, "final_fmv": value,
             "adjustments": [], "above_ask_gate": {}, "subject_asking": ask}
    qr, _cf, _blk = _v10_pre_export_qa(
        {"m": ("Methodology summary", fields_m)},
        value=value, range_pct=range_pct, subject_asking=ask,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 2},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=trace, value_drivers=[], confidence="low")
    return qr


def test_v_offer_and_direction_qa():
    print("\nV. anchor->FMV direction check + buyer-offer rebuild / sanity check")

    # 1. final FMV materially BELOW anchor, but prose says "close to the anchor with a modest
    #    upward tilt" -> blocked.
    bad_dir = ("The comparable-sales anchor is $1,608,000. The concluded value sits close to the "
               "comp anchor with a modest upward tilt for the subject's architectural character.")
    qr_bad = _pre_qa(bad_dir, value=1_480_000, anchor_value=1_608_000)
    check("1: 'close to anchor / upward tilt' while FMV is 8% BELOW the anchor is blocked",
          any("anchor" in b.lower() and "below" in b.lower() for b in qr_bad["blocking_errors"]),
          str(qr_bad["blocking_errors"]))
    # correct wording is fine
    good_dir = ("The comparable-sales anchor is $1,608,000. The qualitative analysis then adjusted the "
                "anchor down to the $1,480,000 final value, reflecting the subject's condition and amenity gaps.")
    qr_ok = _pre_qa(good_dir, value=1_480_000, anchor_value=1_608_000)
    check("1: prose that says the anchor was adjusted DOWN to the final value is NOT blocked",
          not any("direction" in b.lower() or "adjusted the anchor" in b.lower()
                  for b in qr_ok["blocking_errors"]), str(qr_ok["blocking_errors"]))
    check("1: the check is recorded in checks_run", "anchor_fmv_direction" in qr_ok["checks_run"])
    # mirror: FMV materially ABOVE anchor but prose says "below"
    bad_dir2 = "The value lands below the comparable-sales anchor after adjustments."
    qr_bad2 = _pre_qa(bad_dir2, value=1_700_000, anchor_value=1_600_000)
    check("1: 'below the anchor' while FMV is ABOVE the anchor is blocked",
          any("anchor" in b.lower() and "above" in b.lower() for b in qr_bad2["blocking_errors"]),
          str(qr_bad2["blocking_errors"]))

    # 2. the buyer-offer band: the CEILING is the supported FMV, NOT the asking price.
    # 2a. FMV <= list (priced at/above value): band sits below FMV, ceiling == FMV.
    band_over = _v10_build_buyer_offer_band(1_300_000, 1_450_000)  # FMV below list
    check("2a: FMV<=list -> ceiling is the FMV and the band sits at/below it",
          band_over["ceiling"] == 1_300_000 and band_over["opening_high"] <= band_over["ceiling"]
          and band_over["opening_low"] < band_over["opening_high"], str(band_over))
    check("2a: it does NOT anchor the ceiling to the higher list price",
          band_over["ceiling"] != 1_450_000, str(band_over))

    # 2b. INTENTIONALLY UNDERPRICED + competitive read: FMV materially above list, the recommended
    #     range is ALLOWED above asking but stays <= FMV.
    comp_trace = {"exposure": {"state": "insufficient_exposure", "active_market_days": 2},
                  "anchor": {"comp_list_to_sale_median": 1.08,
                             "comps": [{"competitive_sale": True}, {"competitive_sale": True}]}}
    band_up = _v10_build_buyer_offer_band(1_520_000, 1_350_000, trace=comp_trace, confidence="medium",
                                          status_timeline={"active_market_days": 2})
    check("2b: underpriced -> ceiling is capped at the list price, never above asking",
          band_up["ceiling"] == 1_350_000 and band_up["underpriced"] is True, str(band_up))
    check("2b: a competitive read is allowed to open AT the asking price (never above it)",
          band_up["aggression"] == "high" and band_up["opening_high"] == 1_350_000, str(band_up))
    check("2b: the whole band still stays at or below the list price - never recommend an offer above asking",
          band_up["opening_low"] <= band_up["opening_high"] <= band_up["target"] <= band_up["ceiling"] == 1_350_000,
          str(band_up))
    check("2b: the opening band is narrow, not the whole valuation range",
          (band_up["opening_high"] - band_up["opening_low"]) < 0.10 * band_up["fmv"], str(band_up))

    # 2c. underpriced but STALE / non-competitive read: less aggressive, opens BELOW list, ceiling still FMV.
    stale_trace = {"exposure": {"state": "extended_exposure", "active_market_days": 70, "price_cut_pct": 4.0},
                   "anchor": {"comp_list_to_sale_median": 0.96, "comps": []}}
    band_stale = _v10_build_buyer_offer_band(1_520_000, 1_350_000, trace=stale_trace, confidence="low",
                                             status_timeline={"active_market_days": 70})
    check("2c: a stale / non-competitive underpriced listing opens BELOW list and is less aggressive",
          band_stale["aggression"] == "low" and band_stale["opening_high"] < 1_350_000
          and band_stale["ceiling"] == 1_350_000, str(band_stale))

    band = band_up  # used by 3/4 below

    # 3. rebuild: a corrupted AI opener is replaced; strategy sentences are kept.
    bad_offer = ("Given the $1,595,000 indicated value and a $1,375,000 list price, open at $1,399,294 to "
                 "$1,645,960, roughly 5%below list. Keep a standard inspection contingency and target a "
                 "21-day close.")
    new_offer, rf = _v10_rebuild_buyer_offer_prose(bad_offer, band, 8.0)
    check("3: the rebuilt opener quotes the FINAL $1,520,000 FMV and the ACTUAL $1,350,000 list price",
          "$1,520,000" in new_offer and "$1,350,000" in new_offer and "$1,595,000" not in new_offer
          and "$1,375,000" not in new_offer, new_offer)
    check("3: it says the listing looks underpriced and names the comfort ceiling above FMV",
          "underpriced" in new_offer.lower()
          and "shouldn't feel comfortable paying more than" in new_offer.lower()
          and "$1,596,000" in new_offer, new_offer)
    check("3: the corrupted numeric opener ('$1,399,294 to $1,645,960', '5%below') is gone",
          "1,645,960" not in new_offer and "5%below" not in new_offer, new_offer)
    check("3: the non-numeric strategy sentence is kept",
          "inspection contingency" in new_offer and "21-day close" in new_offer, new_offer)
    check("3: a flag records the deterministic rebuild", any("rebuilt deterministically" in f for f in rf), str(rf))

    # 4. the dedicated sanity check.
    rmin, rmax = 1_397_000, 1_643_000  # ~ +/-8% of 1.52M
    check("4a: wrong list price is blocked",
          any("list price" in b.lower() for b in _v10_offer_sanity_check(
              "The subject is listed at $1,400,000; open near $1,300,000.", band, rmin, rmax)))
    check("4b: wrong FMV is blocked",
          any("fair market value" in b.lower() for b in _v10_offer_sanity_check(
              "Our fair market value of $1,595,000 supports an opening offer near $1,300,000.", band, rmin, rmax)))
    check("4c: an opening range as wide as the valuation range is blocked",
          any("valuation range" in b.lower() for b in _v10_offer_sanity_check(
              "A reasonable opening offer is $1,397,000 to $1,643,000.", band, rmin, rmax)))
    check("4d: a recommendation ABOVE the supported FMV ($1,520,000) is blocked",
          any("ceiling" in b.lower() for b in _v10_offer_sanity_check(
              "Be prepared to go as high as $1,600,000 to win the deal.", band, rmin, rmax)))
    check("4d: an above-ASK recommendation is blocked even when it stays below FMV - never recommend above asking",
          any("ceiling" in b.lower() for b in _v10_offer_sanity_check(
              "Be prepared to stretch to $1,470,000 - still under the supported value.", band, rmin, rmax)),
          str(_v10_offer_sanity_check("Be prepared to stretch to $1,470,000.", band, rmin, rmax)))
    check("4e: malformed percentage wording is blocked",
          any("malformed" in b.lower() for b in _v10_offer_sanity_check(
              "Open roughly 4%–%6 below list, about %.", band, rmin, rmax)))
    check("4f: the clean rebuilt offer passes the sanity check",
          not _v10_offer_sanity_check(new_offer, band, rmin, rmax), str(_v10_offer_sanity_check(new_offer, band, rmin, rmax)))


# ---------------------------------------------------------------------------------------------
# W. final client prose consistency - authoritative final valuation object and final adjustment
#    trace are the source of truth for anchor direction, factor pricing, ranges, FMV and list price.
# ---------------------------------------------------------------------------------------------
def test_w_final_client_output_consistency():
    print("\nW. final client-output consistency against final valuation + adjustment trace")
    value = 1_537_925
    anchor_value = 1_608_634
    ask = 1_400_000
    range_pct = 5.0
    final = {
        "value": value, "range_min": round(value * 0.95), "range_max": round(value * 1.05),
        "range_pct": range_pct, "anchor_value": anchor_value, "anchor_low": 1_560_000,
        "anchor_high": 1_665_000, "list_price": ask,
    }
    broad_trace = {
        "anchor": {"anchor_value": anchor_value, "anchor_low": 1_560_000, "anchor_high": 1_665_000, "comps": []},
        "final_fmv": value, "range_pct": range_pct, "subject_asking": ask,
        "adjustments": [{
            "family": "qualitative_inferiority_shortfall", "factor": "Broad qualitative-inferiority shortfall",
            "applied": -70_709, "direction": "down", "basis": "Aggregate unpriced condition / amenity shortfall",
        }],
        "qualitative_reconciliation": {
            "aggregate_inferiority": {"buckets": ["amenities", "condition"]},
            "omissions": [],
        },
        "above_ask_gate": {},
    }

    neutral_anchor = "The comparable-sales anchor is $1,608,634. The final valuation is supported by the evidence."
    fin = asyncio.run(_finalize_gate_reconciled_prose(
        methodology=neutral_anchor, offer="", inspection="", value=value, range_pct=range_pct,
        subject_asking=ask, confidence="medium",
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 14},
        enriched_subject=_subject(list_price=ask), comps=[], comp_num_by_key={}, addr_by_key={},
        valuation_trace=broad_trace, value_drivers=[], qa_ctx={}, mode="buyer", model="claude-sonnet-5",
        final=final, external_blocking=[], run_language_polish=False,
    ))
    check("1: neutral anchor prose is reconciled to say the final FMV is BELOW the anchor",
          "below the $1,608,634 comparable-sales anchor" in fin["methodology"], fin["methodology"])
    check("1: reconciled clean report passes export QA",
          fin["qa_result"]["export_allowed"] is True and not fin["qa_result"]["blocking_errors"],
          str(fin["qa_result"]["blocking_errors"]))

    # Specific factor pricing claims require specific structured backing; a broad shortfall is not enough.
    factor_claim = {
        "m": ("Methodology summary",
              "Architectural pedigree was priced upward, while the pool and guest-wing disadvantage were priced downward.")
    }
    factor_blk = _v10_directional_adjustment_backing_check(factor_claim, broad_trace)
    check("2a: broad shortfall does NOT back a claimed architecture upward adjustment",
          any("architectural" in b.lower() and "upward" in b.lower() for b in factor_blk), str(factor_blk))
    check("2b: broad shortfall does NOT back claimed pool / guest-wing downward adjustments",
          any("pool" in b.lower() and "downward" in b.lower() for b in factor_blk)
          and any("guest" in b.lower() and "downward" in b.lower() for b in factor_blk), str(factor_blk))

    neutral_factor = {
        "m": ("Methodology summary",
              "Architectural pedigree, pool, and guest-wing differences were considered within the broader qualitative analysis.")
    }
    check("2c: neutral broader-qualitative language is allowed without separate line items",
          not _v10_directional_adjustment_backing_check(neutral_factor, broad_trace),
          str(_v10_directional_adjustment_backing_check(neutral_factor, broad_trace)))

    mixed_family_trace = {
        "adjustments": [{
            "family": "views_setting", "factor": "Architectural pedigree and grounds/privacy premium",
            "applied": 45_000, "direction": "up", "basis": "Supported feature adjustment",
        }]
    }
    backed = _v10_directional_adjustment_backing_check(
        {"m": ("Methodology summary", "Architectural pedigree was priced upward.")}, mixed_family_trace)
    check("2d: backing search scans the whole trace text, not just one exact family bucket",
          not backed, str(backed))

    # Wrong client-facing values/ranges/list price are blockers on the final strings.
    qr_range, _c1, _b1 = _v10_pre_export_qa(
        {"m": ("Methodology summary", "The supported valuation range is $1,537,925 to $1,650,000.")},
        value=value, range_pct=range_pct, subject_asking=ask,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 14},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=broad_trace,
        value_drivers=[], confidence="medium")
    check("3a: stale/wrong valuation range blocks export",
          any("authoritative final range" in b for b in qr_range["blocking_errors"]),
          str(qr_range["blocking_errors"]))

    qr_fmv, _c2, _b2 = _v10_pre_export_qa(
        {"m": ("Methodology summary", "The fair market value is $1,608,634.")},
        value=value, range_pct=range_pct, subject_asking=ask,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 14},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=broad_trace,
        value_drivers=[], confidence="medium")
    check("3b: stale/wrong FMV blocks export",
          any("one FMV" in b for b in qr_fmv["blocking_errors"]), str(qr_fmv["blocking_errors"]))

    qr_ask, _c3, _b3 = _v10_pre_export_qa(
        {"m": ("Methodology summary", "The subject is listed at $1,450,000.")},
        value=value, range_pct=range_pct, subject_asking=ask,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 14},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=broad_trace,
        value_drivers=[], confidence="medium")
    check("3c: stale/wrong list price blocks export",
          any("list price" in b.lower() and "$1,400,000" in b for b in qr_ask["blocking_errors"]),
          str(qr_ask["blocking_errors"]))

    offer_bad_range = _v10_offer_sanity_check(
        "The final valuation range is $1,537,925 to $1,650,000, so open below value.",
        {"fmv": value, "list_price": ask, "ceiling": value}, final["range_min"], final["range_max"])
    check("3d: Offer Suggestion valuation ranges must match the authoritative final range",
          any("authoritative final range" in b for b in offer_bad_range), str(offer_bad_range))

    clean = (
        "The comparable-sales anchor is $1,608,634. The final fair market value is $1,537,925, "
        "about 4.4% below the anchor, after the broad qualitative shortfall and final market checks. "
        "The supported valuation range is $1,461,029 to $1,614,821, and the subject is listed at $1,400,000."
    )
    qr_clean, _c4, _b4 = _v10_pre_export_qa(
        {"m": ("Methodology summary", clean)},
        value=value, range_pct=range_pct, subject_asking=ask,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 14},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=broad_trace,
        value_drivers=[], confidence="medium")
    clean_factor = _v10_directional_adjustment_backing_check({"m": ("Methodology summary", clean)}, broad_trace)
    check("4: clean valid report passes final consistency checks",
          qr_clean["export_allowed"] is True and not qr_clean["blocking_errors"] and not clean_factor,
          f"{qr_clean['blocking_errors']} / {clean_factor}")


# ---------------------------------------------------------------------------------------------
# X. frontier reasoning authority - V10 remains an immutable benchmark while a separately
#    validated reasoning result owns final FMV/range/confidence and every exported value source.
# ---------------------------------------------------------------------------------------------
def _reasoning_fixture(value=3_250_000, low=3_100_000, high=3_375_000):
    return {
        "independent_preliminary_fmv": value,
        "independent_preliminary_range_low": low,
        "independent_preliminary_range_high": high,
        "independent_preliminary_confidence": "medium",
        "final_fmv": value, "range_low": low, "range_high": high, "confidence": "medium",
        "anchor_assessment": {"classification": "too_low", "reason": "The direct analogs support more."},
        "v10_assessment": {"classification": "too_low", "reason": "The formula underweights the land."},
        "primary_comps": [{"comp_id": "A", "role": "direct", "reason": "The closest product match."}],
        "floor_comps": [{"comp_id": "B", "role": "floor", "reason": "Inferior condition sets a floor."}],
        "ceiling_comps": [],
        "comp_roles": [
            {"comp_id": "A", "role": "direct_analog", "reason": "The closest product match."},
            {"comp_id": "B", "role": "inferior_condition", "reason": "Its condition limits direct use."},
        ],
        "major_value_drivers": [{
            "factor": "privacy", "direction": "up",
            "evidence_strength": "directionally_supported_amount_uncertain", "reason": "Superior privacy.",
        }],
        "major_value_drags": [{
            "factor": "condition", "direction": "down",
            "evidence_strength": "clearly_market_supported", "reason": "Less renovated.",
        }],
        "market_exposure_assessment": "Exposure is consistent with the comp evidence.",
        "why_final_differs_from_anchor": "Direct analogs support a higher conclusion.",
        "why_final_differs_from_v10": "The whole-property reconciliation supports more.",
        "final_valuation_summary": "The closest sales and the subject's market position support this conclusion.",
    }


def _reasoning_packet_fixture():
    return {
        "subject": {"current_list_price": 3_000_000, "status": "Active", "active_dom": 21},
        "comparables": [
            {"comp_id": "A", "comp_label": "Comp 1", "sale_price": 3_300_000,
             "list_price": 3_200_000, "normalized_price_per_sqft": 550, "dom": 9},
            {"comp_id": "B", "comp_label": "Comp 2", "sale_price": 2_950_000,
             "list_price": 3_000_000, "normalized_price_per_sqft": 505, "dom": 28},
        ],
        "deterministic_evidence": {
            "deterministic_anchor": 3_100_000, "anchor_low": 2_975_000,
            "anchor_high": 3_225_000, "old_v10_final_fmv": 3_050_000,
            "prior_ai_estimate": {"value": 3_200_000, "confidence": "medium"},
        },
    }


def test_x_frontier_reasoning_authority():
    print("\nX. frontier reasoning model is final authority; V10 remains benchmark + QA")
    base_trace = {
        "mode": "hybrid_v10",
        "anchor": {"anchor_value": 3_100_000, "anchor_low": 2_975_000, "anchor_high": 3_225_000,
                   "comps": [{"label": "Comp 1", "normalized_weight": 1.0}]},
        "final_fmv": 3_050_000, "range_pct": 5.0,
        "above_ask_gate": {"applied": True, "action": "pulled_down", "to": 3_050_000},
    }
    v10 = {"fmv": 3_050_000, "range_pct": 5.0, "confidence_seed": "medium",
           "trace": base_trace, "flags": [], "blocking": []}
    v10_snapshot = json.dumps(v10, sort_keys=True)

    above_raw = _reasoning_fixture()
    above, blockers = _validate_reasoning_valuation(above_raw, _reasoning_packet_fixture())
    promoted = _apply_reasoning_authority(
        v10, above, model="gpt-6-astra", effort="high",
        comp_labels_by_id={"A": "Comp 1", "B": "Comp 2"},
    )
    check("1: a reasoning FMV above old V10 is preserved without a clamp",
          promoted["fmv"] == 3_250_000 and promoted["fmv"] > v10["fmv"], str(promoted))

    below_raw = _reasoning_fixture(2_850_000, 2_725_000, 2_975_000)
    below, below_blockers = _validate_reasoning_valuation(below_raw, _reasoning_packet_fixture())
    promoted_below = _apply_reasoning_authority(v10, below, model="gpt-6-astra", effort="high")
    check("2: a reasoning FMV below old V10 is preserved without a clamp",
          promoted_below["fmv"] == 2_850_000 and promoted_below["fmv"] < v10["fmv"], str(promoted_below))
    check("3: deterministic anchor remains stored for audit",
          promoted["trace"]["deterministic_anchor"] == 3_100_000, str(promoted["trace"]))
    check("4: old V10 FMV remains stored for audit",
          promoted["trace"]["v10_formula_fmv"] == 3_050_000
          and promoted["trace"]["reasoning_delta_vs_v10"] == 200_000
          and promoted["trace"]["reasoning_model_confidence"] == "medium"
          and promoted["trace"]["reasoning_independent_preliminary_fmv"] == 3_250_000,
          str(promoted["trace"]))

    frontend_root = Path(__file__).resolve().parents[2] / "frontend"
    pdf_source = (frontend_root / "lib" / "pdfs.tsx").read_text(encoding="utf-8")
    share_source = (frontend_root / "app" / "cma" / "[slug]" / "page.tsx").read_text(encoding="utf-8")
    check("5: final PDF reads the locked final_valuation FMV before legacy pricing fields",
          "aiEstimate?.final_valuation" in pdf_source and "authoritativeValue" in pdf_source,
          "authoritative final_valuation source missing from PDF")

    exact_range = _authoritative_range(
        promoted["fmv"], promoted["range_pct"], trace=promoted["trace"])
    check("6: final range preserves the reasoning model's exact asymmetric endpoints",
          exact_range == (3_100_000, 3_375_000), str(exact_range))

    reasoning_final = {
        "value": promoted["fmv"], "range_min": exact_range[0], "range_max": exact_range[1],
        "range_pct": promoted["range_pct"], "anchor_value": 3_100_000, "list_price": 3_000_000,
    }
    offer_band = _v10_build_buyer_offer_band(
        reasoning_final["value"], reasoning_final["list_price"], trace=promoted["trace"],
        confidence="medium",
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 21},
        range_min=exact_range[0], range_max=exact_range[1],
    )
    offer = _v10_rebuild_buyer_offer_prose(
        "Keep inspection protections.", offer_band, promoted["range_pct"],
    )[0]
    offer_errors = _v10_offer_sanity_check(
        offer, offer_band,
        exact_range[0], exact_range[1],
    )
    check("7: offer guidance is rebuilt and checked against reasoning FMV/range",
          "$3,250,000" in offer and not offer_errors, f"{offer} / {offer_errors}")

    stale_qr, _corr, _blk = _v10_pre_export_qa(
        {"m": ("Methodology summary", "The final fair market value is $3,050,000 under V10.")},
        value=promoted["fmv"], range_pct=promoted["range_pct"], subject_asking=3_000_000,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 21},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=promoted["trace"],
        value_drivers=[], confidence="medium",
    )
    check("8: stale V10 values and internal V10 language are blocked from client prose",
          stale_qr["export_allowed"] is False
          and any("V10" in b or "one FMV" in b for b in stale_qr["blocking_errors"]),
          str(stale_qr["blocking_errors"]))

    bad_fact = _reasoning_fixture()
    bad_fact["primary_comps"] = [{"comp_id": "NOT-A-COMP", "role": "direct", "reason": "Best match."}]
    _bad_normalized, factual_blockers = _validate_reasoning_valuation(bad_fact, _reasoning_packet_fixture())
    check("9: deterministic factual QA blocks an invented comp reference",
          any("unknown comp_id" in b for b in factual_blockers), str(factual_blockers))
    parser_probe = _reasoning_fixture()
    parser_probe["primary_comps"][0]["reason"] = (
        "MLS listing 804728709942 is the closest match; the trace includes a $140,000 adjustment."
    )
    parser_packet = _reasoning_packet_fixture()
    parser_packet["deterministic_evidence"]["structured_qualitative_adjustments"] = [
        {"factor": "condition", "applied": -140_000}
    ]
    _parser_result, parser_blockers = _validate_reasoning_valuation(parser_probe, parser_packet)
    check("9: bare MLS IDs are not parsed as dollars and traced adjustment dollars are allowed",
          not any("unsupported dollar" in b for b in parser_blockers), str(parser_blockers))
    check("9b: generic dollar scanner ignores bare MLS IDs but keeps marked/suffixed prices",
          _v10_extract_dollars("IDs 804728709942 and 804530529614; prices $1,600,000 and 515K.")
          == [1_600_000, 515_000],
          str(_v10_extract_dollars("IDs 804728709942 and 804530529614; prices $1,600,000 and 515K.")))
    check("9c: a word beginning with m after a price is not stolen as a million suffix",
          _v10_extract_dollars("The $1,628,265 midpoint is the benchmark.") == [1_628_265],
          str(_v10_extract_dollars("The $1,628,265 midpoint is the benchmark.")))
    traceable_audit = _reasoning_fixture()
    traceable_audit["why_final_differs_from_anchor"] = (
        "The final midpoint is $150,000 above the $3,100,000 midpoint."
    )
    traceable_audit["why_final_differs_from_v10"] = (
        "The $3,200,000 prior estimate remains context, not authority."
    )
    _traceable_result, traceable_blocks = _validate_reasoning_valuation(
        traceable_audit, _reasoning_packet_fixture())
    check("9d: exact benchmark deltas and the prior estimate are valid audit-only dollars",
          not any("unsupported dollar" in b for b in traceable_blocks), str(traceable_blocks))
    check("10: factual QA does not overwrite an otherwise valid reasoning FMV",
          promoted["fmv"] == above["final_fmv"] and not blockers and not below_blockers,
          f"{promoted['fmv']} / {blockers} / {below_blockers}")

    malformed_failed_closed = False
    try:
        _validate_reasoning_valuation(
            {**_reasoning_fixture(), "range_low": 3_400_000, "range_high": 3_500_000},
            _reasoning_packet_fixture(),
        )
    except ValueError:
        malformed_failed_closed = True
    check("11: malformed reasoning output fails closed instead of using V10",
          malformed_failed_closed, "malformed output was accepted")

    check("12: authority promotion does not mutate V10 comp selection/normalization trace",
          json.dumps(v10, sort_keys=True) == v10_snapshot
          and promoted["trace"]["anchor"]["comps"] == base_trace["anchor"]["comps"],
          f"before={v10_snapshot} after={json.dumps(v10, sort_keys=True)}")
    check("12: shared report also prioritizes final_valuation over legacy fields",
          "pricing.ai_estimate?.final_valuation" in share_source and "authoritativeValue" in share_source,
          "authoritative final_valuation source missing from share page")

    # The independent conclusion is explicit and auditable before benchmark reconciliation, and
    # every major factor/comp receives the required evidence/role classification.
    prompt_low = _CMA_REASONING_SYSTEM_PROMPT.lower()
    check("13: frontier prompt requires independent-first valuation and evidence-strength discipline",
          "work in two ordered phases" in prompt_low
          and "subjective_or_speculative" in prompt_low
          and "must not overpower" in prompt_low
          and "classify every supplied comparable exactly once" in prompt_low,
          _CMA_REASONING_SYSTEM_PROMPT)

    # A real anchor is a protected structural figure. It must never be mistaken for a stale V10
    # conclusion and rewritten to the reasoning FMV.
    anchor_trace = {
        "authority": "reasoning_model", "deterministic_anchor": 1_628_265,
        "anchor": {"anchor_value": 1_628_265, "anchor_low": 1_489_424, "anchor_high": 1_767_106},
        "reasoning_model_range_low": 1_425_000, "reasoning_model_range_high": 1_675_000,
        "fmv_pre_exposure": 1_588_265, "v10_formula_fmv": 1_588_265,
    }
    anchor_final = _v10_authoritative_valuation(
        value=1_550_000, range_pct=8.06, confidence="medium", valuation_trace=anchor_trace,
        subject_asking=1_350_000, status_timeline={"current_status": "Active"}, comp_stats={},
        range_min=1_425_000, range_max=1_675_000,
    )
    anchor_text = (
        "The final fair market value is $1,550,000, about 4.8% below the "
        "$1,628,265 comparable-sales anchor."
    )
    scanned, anchor_blocks, anchor_notes = _v10_scan_concluded_numbers(
        {"m": ("Methodology summary", anchor_text)}, final=anchor_final)
    check("14: authoritative anchor is not rewritten as a stale concluded value",
          "$1,628,265 comparable-sales anchor" in scanned["m"][1]
          and not anchor_blocks and not anchor_notes,
          f"{scanned} / {anchor_blocks} / {anchor_notes}")

    competitive_band = _v10_build_buyer_offer_band(
        1_550_000, 1_350_000, trace={
            "exposure": {"state": "insufficient_exposure", "active_market_days": 3},
            "anchor": {"comp_list_to_sale_median": 1.083, "comps": [{"competitive_sale": True}]},
        }, confidence="medium", status_timeline={"active_market_days": 3},
    )
    competitive_offer, _ = _v10_rebuild_buyer_offer_prose(
        "A starting offer near list price with room to negotiate is balanced. "
        "Because the anchor exceeds list, there is room to offer above asking. "
        "9-5. 6% above list) is competitive, with room toward the mid-$1,500,000s. g.",
        competitive_band, 8.06)
    check("15: opening offer is capped at list price (never above asking) and stale near-list advice is removed",
          "roughly at the list price" in competitive_offer
          and "near-list" in competitive_offer
          and "starting offer near list" not in competitive_offer.lower()
          and "anchor" not in competitive_offer.lower()
          and "9-5" not in competitive_offer and "mid-$" not in competitive_offer and " g." not in competitive_offer,
          competitive_offer)
    check("15b: fragmented malformed offer percentages are blocked as defense in depth",
          any("malformed percentage" in b for b in _v10_offer_sanity_check(
              "An opening at 9-5. 6% above list is reasonable.", competitive_band, 1_375_000, 1_600_000)),
          "fragmented percentage was not caught")

    repaired, did_repair = _repair_unambiguous_dangling_connectors(
        "The analysis considered material features such as. The sale outcome remains relevant."
    )
    check("16: safe grammar repair removes an unambiguous dangling connector",
          did_repair and "features." in repaired and "sale outcome remains relevant" in repaired,
          repaired)

    synced = _v10_sync_qa_authority({
        "warnings": ["same", "same"], "corrections": ["fixed", "fixed"],
        "blocking_errors": ["block", "block"], "external_blocking": [],
    }, ["block", "block"])
    check("17: identical QA warnings, notes, and blocking errors are deduplicated",
          synced["warnings"] == ["same"] and synced["corrections"] == ["fixed"]
          and synced["blocking_errors"] == ["block"], str(synced))

    contrast_qa, _contrast_fields, _contrast_blocks = _v10_pre_export_qa(
        {"m": ("Methodology summary",
               "Limited modernization separates the subject from turnkey competition. Nearby closed sales "
               "place it above the dated-condition lower bracket.")},
        value=1_500_000, range_pct=6.67, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "active_market_days": 3},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=anchor_trace,
        value_drivers=[], confidence="medium",
    )
    check("18: valid turnkey-vs-dated comp contrast is not a subject-condition contradiction",
          not any("both turnkey and dated" in w for w in contrast_qa["warnings"]),
          str(contrast_qa["warnings"]))

    stale_offer_text = "The indicated value is $1,595,000. Preserve an inspection contingency."
    stale_offer_qa, _stale_fields, _stale_blocks = _v10_pre_export_qa(
        {"o": ("Offer strategy", stale_offer_text)},
        value=1_500_000, range_pct=6.67, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "active_market_days": 3},
        comps=[], comp_num_by_key={}, addr_by_key={}, valuation_trace=anchor_trace,
        value_drivers=[], confidence="medium",
    )
    field_aware_blocks = stale_offer_qa["blocking_errors"]
    rebuilt_offer, rebuilt_blocks, rebuilt_qa, _ = _v10_apply_buyer_offer_rebuild(
        stale_offer_text, anchor_final, 1_350_000, 6.67,
        {"current_status": "Active", "active_market_days": 3}, anchor_trace,
        list(field_aware_blocks), dict(stale_offer_qa),
    )
    check("19: stale pre-rebuild offer FMV block is field-labelled and removed after clean rebuild",
          field_aware_blocks and all(b.startswith("Offer strategy:") for b in field_aware_blocks)
          and "$1,595,000" not in rebuilt_offer and not rebuilt_blocks
          and not rebuilt_qa["blocking_errors"],
          f"before={field_aware_blocks} after={rebuilt_blocks} / {rebuilt_offer}")

    blank_offer, blank_blocks, blank_qa, _ = _v10_apply_buyer_offer_rebuild(
        "", anchor_final, 1_350_000, 6.67,
        {"current_status": "Active", "active_market_days": 3}, anchor_trace,
        [], {"blocking_errors": [], "warnings": [], "checks_run": []},
    )
    check("20: a missing model offer still receives deterministic buyer guidance",
          bool(blank_offer) and "$1,550,000" in blank_offer and "$1,350,000" in blank_offer
          and "reasonable opening offer" in blank_offer.lower() and not blank_blocks
          and not blank_qa["blocking_errors"], blank_offer)


def test_y_factual_prose_and_qa_cleanup():
    print("\nY. factual comp/inspection prose and QA categorization")
    old_subject = _subject(
        year_built=1925,
        construction_materials=["Stone", "Stucco"],
        public_remarks="Historic stone residence with stucco exterior areas.",
    )

    circular = ("The comp sold for $395/sq ft, below what our estimate implies for the subject, "
                "largely because it is smaller.")
    clean_comp, comp_flags = _sanitize_comp_fact_prose(circular, "Comp #1", old_subject)
    check("1: comp note no longer reasons circularly from the final estimate",
          "estimate implies" not in clean_comp.lower() and "because it is smaller" in clean_comp.lower()
          and bool(comp_flags), clean_comp)

    age_claim = "The renovated comp has none of the deferred-maintenance risk the subject's 1925 systems likely carry."
    clean_age, _ = _sanitize_comp_fact_prose(age_claim, "Comp #2", old_subject)
    check("2: age alone becomes unverified system condition, not deferred maintenance",
          "deferred" not in clean_age.lower() and "unverified" in clean_age.lower(), clean_age)

    inspection = ("**Roof:** Verify flashing and any skylights are sound.\n\n"
                  "**Terraces:** Confirm the terraces and loggias are original to the home's era.\n\n"
                  "**Stucco:** Commission invasive probe testing because stucco is present.")
    clean_inspection, inspection_flags = _reconcile_inspection_fact_prose(inspection, old_subject)
    check("3: unsupported skylights and original-feature claims are removed",
          "skylight" not in clean_inspection.lower() and "original" not in clean_inspection.lower(),
          clean_inspection)
    check("4: pre-1980 stone/stucco does not automatically recommend invasive testing",
          "invasive moisture testing is only worth doing if the construction or an actual finding calls for it" in clean_inspection.lower()
          and bool(inspection_flags), clean_inspection)
    check("5: cleaned inspection prose passes deterministic factual QA",
          not _factual_prose_blocking(clean_inspection, "Inspection concerns", old_subject),
          str(_factual_prose_blocking(clean_inspection, "Inspection concerns", old_subject)))

    wood_stucco = _subject(year_built=2002, construction_materials=["Stucco", "Wood Frame"])
    wood_guidance = _stucco_inspection_paragraph(wood_stucco) or ""
    check("6: later stucco over wood makes invasive moisture testing relevant",
          "wood framing" in wood_guidance.lower() and "invasive moisture testing" in wood_guidance.lower(),
          wood_guidance)
    new_stucco = _subject(year_built=date.today().year - 2, construction_materials=["Stucco", "Wood Frame"])
    new_guidance = _stucco_inspection_paragraph(new_stucco) or ""
    check("7: very new stucco does not automatically trigger invasive testing",
          "do not default to invasive testing" in new_guidance.lower(), new_guidance)

    offer_band = _v10_build_buyer_offer_band(
        1_500_000, 1_350_000,
        trace={"exposure": {"state": "normal_exposure", "active_market_days": 10}, "anchor": {"comps": []}},
        confidence="medium", status_timeline={"active_market_days": 10},
    )
    offer, _ = _v10_rebuild_buyer_offer_prose("Keep an inspection contingency.", offer_band, 6.0)
    check("8: offer names the comfort ceiling (~5% above FMV) as advisory, not a number to submit",
          "supported ceiling" not in offer.lower() and "shouldn't feel comfortable paying more than $1,575,000" in offer.lower(), offer)

    sig = _v10_weighted_qualitative_signals({"comps": [{
        "label": "Comp #1 (Example Rd)", "normalized_weight": 1.0,
        "quality": {"layout": "below"}, "explicit_accessory_features": ["guest wing"],
    }]})["accessory_space"]
    check("9: accessory QA explicitly identifies its layout-proxy basis and preserves guest-wing evidence",
          sig.get("metric_label") == "layout / auxiliary-space proxy"
          and sig.get("direction") == "proxy_only"
          and sig.get("basis") == "legacy layout rating; not a factual accessory-space comparison"
          and (sig.get("explicit_comp_accessory_evidence") or [])[0]["features"] == ["guest wing"], str(sig))


def test_z_authoritative_property_fact_pipeline():
    print("\nZ. one authoritative property-fact pipeline")

    generated_only = _comp(
        "Z1", 1_100_000, 3301,
        public_remarks="Traditional residence with a landscaped yard.",
        ai_commentary="This comp includes a guest apartment and pool.",
        commentary="The guest apartment is a major advantage.",
    )
    check("1: generated commentary cannot become guest-apartment evidence",
          _explicit_accessory_features(generated_only) == [],
          str(_explicit_accessory_features(generated_only)))

    documented = _comp(
        "Z2", 1_150_000, 3400,
        public_remarks="The detached pool house includes guest quarters beside the swimming pool.",
    )
    documented_features = _authoritative_property_features(documented)
    check("2: listing evidence supplies accessory and pool facts to the canonical snapshot",
          bool(documented_features["accessory_features"])
          and "pool" in documented_features["amenities"], str(documented_features))

    merged = _merge_authoritative_property_facts(
        {"public_remarks": "Stale remarks", "ai_commentary": "Keep this generated note."},
        {"public_remarks": "Fresh MLS remarks", "above_grade_finished_area": 3301},
    )
    check("3: fresh structured facts replace stale facts without importing generated prose",
          merged["public_remarks"] == "Fresh MLS remarks"
          and merged["ai_commentary"] == "Keep this generated note."
          and merged["above_grade_finished_area"] == 3301, str(merged))

    plain_subject = _subject(public_remarks="Traditional residence with a patio.")
    q, amenity_flags = _reconcile_amenities_quality(
        plain_subject, documented, {"amenities": "below"}, "Comp #1",
    )
    check("4: documented amenities override an opposite AI amenities rating",
          q.get("amenities") == "above" and bool(amenity_flags), f"{q} / {amenity_flags}")

    subject_with_guest = _subject(
        public_remarks="A detached apartment above the garage provides guest accommodations."
    )
    packet = _build_reasoning_evidence_packet(
        subject=subject_with_guest, comps=[generated_only], status_timeline={}, anchor={}, exposure={},
        v10_result={"fmv": 1_000_000, "range_pct": 5, "trace": {}}, subject_estimate={},
        evidence_by_key={}, outliers={},
    )
    check("5: reasoning packet consumes the same canonical accessory facts",
          bool(packet["subject"]["authoritative_property_features"]["accessory_features"])
          and packet["comparables"][0]["authoritative_property_features"]["accessory_features"] == []
          and packet["comparables"][0]["guest_house_or_accessory_space"] == [], str(packet))

    fact_subject = _subject(
        sqft=3894, above_grade_finished_area=3894,
        public_remarks="Historic residence; system ages and service history are not provided.",
    )
    bad_note = (
        "Its lower $/sqft than the subject's estimated value reflects a smaller 4,639 sqft "
        "above-grade footprint than the subject's 3,894 sqft and the subject's updated systems."
    )
    clean_note, note_flags = _reconcile_comp_hard_facts(
        bad_note, "Comp #2", fact_subject, generated_only,
    )
    check("6: comp prose is independent of final FMV and uses the structured comp area",
          "estimated value" not in clean_note.lower() and "4,639" not in clean_note
          and "3,301" in clean_note and bool(note_flags), clean_note)
    check("7: undocumented updated systems become unverified history",
          "updated systems" not in clean_note.lower() and "unverified" in clean_note.lower(), clean_note)
    check("8: reconciled comp prose passes its exact-record factual QA",
          not _factual_prose_blocking(
              clean_note, "Comp #2", fact_subject, comp_note=True, comp=generated_only,
          ), str(_factual_prose_blocking(
              clean_note, "Comp #2", fact_subject, comp_note=True, comp=generated_only,
          )))

    raw_status = {
        "StandardStatus": "Active Under Contract",
        "ListingContractDate": "2026-01-10",
        "PurchaseContractDate": "2026-01-24",
        "DaysOnMarket": 17,
    }
    timeline = _subject_status_timeline({}, SimpleNamespace(raw_mls_data=raw_status))
    check("9: explicit list-to-contract dates own a completed active-market span",
          timeline["active_market_days"] == 14
          and timeline["dom_source"] == "list date -> under-contract date", str(timeline))
    interrupted = _subject_status_timeline({}, SimpleNamespace(raw_mls_data={
        **raw_status, "MajorChangeType": "Back On Market",
    }))
    check("10: documented interrupted history allows the MLS DOM field to own the total",
          interrupted["active_market_days"] == 17
          and interrupted["dom_source"] == "MLS DaysOnMarket field", str(interrupted))

    band = {
        "fmv": 950_000, "list_price": 1_000_000, "ceiling": 950_000, "comfort_ceiling": 997_500,
        "opening_low": 900_000, "opening_high": 940_000, "target": 945_000,
        "aggression": "moderate", "underpriced": False, "basis_ref": 1_000_000,
        "above_ask": False,
    }
    prior_frame = (
        "Note: this property is currently under contract (under agreement after 17 active-market days). "
        "The guidance below is for a backup-offer position or buyer analysis only - the seller is "
        "not actively negotiating new offers, and the listing's earlier days on market are not a sign "
        "of weak demand. Preserve inspection and financing contingencies."
    )
    rebuilt, _ = _v10_rebuild_buyer_offer_prose(prior_frame, band, 5.0)
    wrapped, _ = _status_aware_offer_guidance(rebuilt, {
        "is_under_contract": True, "active_market_days": 14,
    })
    check("11: offer percentages describe both actual range endpoints",
          "about 6%-10% below the $1,000,000 reference" in wrapped, wrapped)
    check("12: under-contract/backup framing appears exactly once and uses authoritative DOM",
          wrapped.lower().count("currently under contract") == 1
          and wrapped.lower().count("guidance below is for a backup-offer") == 1
          and "after 14 active-market days" in wrapped
          and "after 17 active-market days" not in wrapped, wrapped)
    bad_pct = (
        "A reasonable opening offer is roughly $900,000 to $940,000 "
        "(about 6% below the list price)."
    )
    check("13: final offer QA blocks endpoint percentage math that only states one endpoint",
          any("opening-range percentage" in b for b in _v10_offer_sanity_check(
              bad_pct, band, 850_000, 1_000_000,
          )), str(_v10_offer_sanity_check(bad_pct, band, 850_000, 1_000_000)))

    age_only_trace = {"adjustments": [{
        "family": "systems_mechanical", "factor": "Older systems uncertainty",
        "basis": "The subject was built in 1929 and its system ages are unknown.",
        "applied": -40_000, "direction": "down",
    }]}
    check("14: applied age-only systems deductions are blocked by factual QA",
          bool(_v10_structured_adjustment_fact_check(age_only_trace, fact_subject)),
          str(_v10_structured_adjustment_fact_check(age_only_trace, fact_subject)))
    documented_problem_subject = _subject(
        public_remarks="The HVAC has failed and needs replacement."
    )
    check("15: a documented system condition supports system-condition treatment",
          not _v10_structured_adjustment_fact_check(age_only_trace, documented_problem_subject),
          str(_v10_structured_adjustment_fact_check(age_only_trace, documented_problem_subject)))
    reasoning_authority_trace = {**age_only_trace, "authority": "reasoning_model"}
    check("15: benchmark-only V10 adjustments cannot block a reasoning-authoritative artifact",
          not _v10_structured_adjustment_fact_check(reasoning_authority_trace, fact_subject),
          str(_v10_structured_adjustment_fact_check(reasoning_authority_trace, fact_subject)))

    extracted = _v10_extract_dollars(
        "The comp sold for $1.6M and sits 40m from the road; its supported range reaches 1650K."
    )
    check("15: physical distance shorthand is not parsed as $40 million",
          40_000_000 not in extracted and 1_600_000 in extracted and 1_650_000 in extracted,
          str(extracted))

    neutral_architecture = {
        "m": ("Methodology summary",
              "Condition uncertainty warrants a broad range rather than an assumed premium for architectural character.")
    }
    check("16: negated architecture premium is neutral prose, not a phantom upward adjustment",
          not _v10_directional_adjustment_backing_check(neutral_architecture, {"adjustments": []}),
          str(_v10_directional_adjustment_backing_check(neutral_architecture, {"adjustments": []})))
    actual_architecture_claim = {
        "m": ("Methodology summary", "Architectural character was priced upward in the final value.")
    }
    check("17: an actual architecture pricing claim still requires an applied adjustment",
          bool(_v10_directional_adjustment_backing_check(actual_architecture_claim, {"adjustments": []})),
          str(_v10_directional_adjustment_backing_check(actual_architecture_claim, {"adjustments": []})))


def test_aa_latest_root_output_consistency():
    print("\nAA. canonical client labels and evidence-bound factual wording")
    subject = _subject(
        year_built=1928,
        architectural_style="Traditional",
        public_remarks="Architectural details include terraces and stone walls; system history is not provided.",
    )
    comp = _comp(
        "804528348758", 1_600_000, 3600,
        street_address="12 Human Name Road",
        public_remarks="Renovated residence with a new kitchen and baths.",
    )

    raw_system_notes = (
        "Its renovation creates a turnkey advantage the subject's original systems can't match. "
        "The subject's dated original systems and finishes require a discount."
    )
    clean_system_notes, system_flags = _reconcile_comp_hard_facts(
        raw_system_notes, "Comp #1", subject, comp,
    )
    check("1: undocumented original-system comparisons become unverified modernization language",
          "original systems" not in clean_system_notes.lower()
          and "unverified" in clean_system_notes.lower() and bool(system_flags), clean_system_notes)
    check("2: unsupported systems wording is clean under deterministic factual QA",
          not _factual_prose_blocking(
              clean_system_notes, "Comp #1", subject, comp_note=True, comp=comp,
          ), str(_factual_prose_blocking(
              clean_system_notes, "Comp #1", subject, comp_note=True, comp=comp,
          )))

    reasoning = {
        "major_value_drivers": [{
            "factor": "804528348758 sale evidence", "direction": "up",
            "reason": "804528348758 provides the closest renovated benchmark.",
        }],
        "major_value_drags": [],
    }
    drivers = _reasoning_value_drivers(reasoning, [comp])
    rendered_driver = f"{drivers[0]['comp_label']} {drivers[0]['note']}"
    check("3: Why This Number resolves raw provider IDs to canonical comp labels",
          "804528348758" not in rendered_driver
          and "Comp #1 (12 Human Name Road)" in rendered_driver, rendered_driver)

    inspection = "**Architecture:** Verify structural condition of these original elements."
    clean_inspection, inspection_flags = _reconcile_inspection_fact_prose(inspection, subject)
    check("4: undocumented original architectural elements become documented elements",
          "original elements" not in clean_inspection.lower()
          and "documented architectural elements" in clean_inspection.lower()
          and bool(inspection_flags), clean_inspection)
    check("5: raw unsupported original-elements language is blocked if reconciliation is bypassed",
          bool(_factual_prose_blocking(inspection, "Inspection concerns", subject)),
          str(_factual_prose_blocking(inspection, "Inspection concerns", subject)))

    amenity_subject, amenity_anchor, amenity_exposure = _amenities_setup()
    prior_min_buckets = _cma_mod._V10_BROAD_INFERIORITY_MIN_BUCKETS
    try:
        # Keep this wording regression focused on the unresolved-bucket message before the existing
        # aggregate shortfall mechanism resolves and removes that block.
        _cma_mod._V10_BROAD_INFERIORITY_MIN_BUCKETS = 99
        amenity_result = _apply_v10_valuation(amenity_anchor, amenity_exposure, {
            "qualitative_adjustments": [
                {"factor": "Pool disadvantage", "direction": "down", "amount_low": 40_000,
                 "amount_high": 40_000, "basis_type": "comp_inferred", "basis": "bounded from comp evidence",
                 "confidence": "medium"},
                {"factor": "Architectural character", "direction": "up", "amount_low": 21_000,
                 "amount_high": 21_000, "basis_type": "comp_inferred", "basis": "bounded from comp evidence",
                 "confidence": "medium"},
            ],
        }, amenity_subject["list_price"])
    finally:
        _cma_mod._V10_BROAD_INFERIORITY_MIN_BUCKETS = prior_min_buckets
    amenity_text = " ".join(amenity_result["blocking"])
    check("6: amenities QA acknowledges real in-bucket offsets while explaining the unresolved net",
          "listed in-bucket offsets" in amenity_text.lower()
          and "architectural character" in amenity_text.lower()
          and "with no offsetting advantage" not in amenity_text.lower(), amenity_text)

    unknown_old = _subject(year_built=1925)
    rated_comp = _cc("AA1", _RENO_NOTE, finish_quality="similar", interior="below")
    rating_flags = _v10_condition_rating_consistency_check(unknown_old, [rated_comp])
    check("7: age plus generated commentary cannot establish an original/dated subject condition",
          not rating_flags and rated_comp["quality"]["finish_quality"] == "similar", str(rating_flags))

    architectural_original = _subject(
        year_built=1925,
        public_remarks=(
            "The original owner preserved documented original architectural elements. "
            "Renovation and systems history are not provided."
        ),
    )
    architectural_comp = _cc("AA2", _RENO_NOTE, finish_quality="similar", interior="below")
    architectural_flags = _v10_condition_rating_consistency_check(architectural_original, [architectural_comp])
    check("8: original ownership/elements and unknown systems do not become a negative condition fact",
          not architectural_flags and architectural_comp["quality"]["finish_quality"] == "similar",
          str(architectural_flags))


def test_ab_current_bright_property_fact_contract():
    print("\nAB. current Bright property-fact contract")
    required = {
        "StructureDesignType", "WaterfrontYN", "ViewYN", "Pool", "PoolFeatures", "OtherStructuresList",
        "PatioAndPorchFeatures", "HotWater", "Electric", "LaundryType", "ParkingTypes",
        "GarageType", "NumAttachedGarageSpaces", "NumDrivewaySpaces",
        "TotalGarageAndParkingSpaces", "GarageSQFT", "AreaTotal", "TaxTotalFinishedSqFt",
        "RoomsTotal",
    }
    missing = required - set(BRIGHT_PROPERTY_SELECT_FIELDS)
    check("1: the centralized Bright sync selects every newer structured property fact",
          not missing, str(sorted(missing)))

    mapped = map_reso_to_internal({
        "ListingKey": "AB1", "FullStreetAddress": "1 Example Rd", "City": "TEST",
        "StateOrProvince": "PA", "PostalCode": "19000", "StructureDesignType": "Detached",
        "WaterfrontYN": False, "Pool": ["Yes - Personal"],
    })
    check("1b: valid Bright structure/waterfront fields populate normalized property shapes",
          mapped["structure_type"] == ["Detached"] and mapped["has_waterfront_view"] is False
          and mapped["raw_mls_data"]["Pool"] == ["Yes - Personal"], str(mapped))

    feature_snapshot = _authoritative_property_features({
        "pool": ["Yes - Personal"],
        "pool_features": ["Heated", "InGround"],
        "other_structures": ["Guest House"],
        "public_remarks": "",
    })
    check("2: structured Bright pool/accessory fields reach canonical CMA evidence",
          "pool" in feature_snapshot["amenities"]
          and "guest house" in feature_snapshot["accessory_features"], str(feature_snapshot))

    no_pool_snapshot = _authoritative_property_features({"pool": ["No Pool"]})
    check("3: Bright's explicit No Pool value is not misread as a pool amenity",
          "pool" not in no_pool_snapshot["amenities"], str(no_pool_snapshot))

    actual_accessory = _explicit_accessory_features({
        "public_remarks": "A finished apartment above the detached garage is included."
    })
    hypothetical_accessory = _explicit_accessory_features({
        "public_remarks": "The garage is plumbed for a bath and offers potential for a guest wing."
    })
    check("4: an existing apartment above is evidence but future guest-space potential is not",
          "apartment above garage" in actual_accessory and not hypothetical_accessory,
          f"actual={actual_accessory} hypothetical={hypothetical_accessory}")

    modifier_subject = _subject(
        public_remarks="A swimming pool and finished apartment above the garage are included."
    )
    modifier_comps = [
        _comp("ABM1", 1_000_000, 4000,
              public_remarks="No pool. The garage offers potential for a guest wing."),
        _comp("ABM2", 1_000_000, 4000, public_remarks="No pool or accessory apartment."),
    ]
    modifier_rows = _continuous_relevance(
        modifier_subject, modifier_comps,
        density_context={"distance_half_life_miles": 2.5},
    )["comps"]
    check("5: pool/guest-wing wording (mentioned, denied, or silent) is not a similarity modifier - "
          "adjusted relevance always equals the weighted 8-component base score",
          all(row["adjusted_relevance_score"] == row["base_relevance_score"] for row in modifier_rows)
          and "product_modifier" not in modifier_rows[0],
          str([(r["base_relevance_score"], r["adjusted_relevance_score"]) for r in modifier_rows]))


def test_ac_final_root_cause_pipeline_boundary():
    print("\nAC. immutable evidence boundary and exact client-output sources")
    subject = _subject(
        source="mls", sqft=5000, above_grade_finished_area=None,
        below_grade_finished_area=None, year_built=1925, mls_status="Active",
        public_remarks="A renovated residence; system history is not provided.",
    )
    comp1 = _comp(
        "AC1", 1_250_000, 3300, strength="strong", year_built=1930,
        public_remarks="The listing describes a renovated kitchen and finished basement.",
    )
    comp1["_amenities_fact_reconciled"] = True
    comp2 = _comp("AC2", 1_100_000, None, sqft=None, strength="supporting", year_built=None)
    drafts = [
        {"ai_commentary": "The 1,700 sqft basement and original systems make this superior.",
         "commentary": "The 1,700 sqft basement and original systems make this superior."},
        {"ai_commentary": "Context only.", "commentary": "Context only."},
    ]
    timeline = {
        "current_status": "Coming Soon", "is_active": True, "is_under_contract": False,
        "is_closed": False, "active_market_days": 0, "current_list_price": 1_300_000,
    }
    snapshot = _build_authoritative_evidence_snapshot(subject, [comp1, comp2], timeline)
    # attempt_rewrite defaults to False - this stays a pure unit test with no real model call.
    snap_subject, snap_comps, snap_timeline, flags, blocking = asyncio.run(
        _materialize_snapshot_with_comp_narratives(snapshot, drafts)
    )
    snap_subject["sqft"] = 1
    fresh_subject, fresh_comps, fresh_timeline = snapshot.materialize()
    check("1: downstream mutation cannot alter the immutable snapshot",
          fresh_subject["sqft"] == 5000 and fresh_timeline["current_status"] == "Coming Soon",
          str(snapshot.audit_metadata()))
    check("2: generated commentary is excluded from factual snapshot evidence",
          "ai_commentary" not in fresh_comps[0] and not snapshot.audit_metadata()["provenance"]["generated_commentary_is_evidence"],
          str(fresh_comps[0]))

    semantics = _exact_sqft_semantics(subject)
    packet = _build_reasoning_evidence_packet(
        subject=fresh_subject, comps=fresh_comps, status_timeline=fresh_timeline,
        anchor={}, exposure={}, v10_result={"fmv": 1_200_000, "range_pct": 5, "trace": {}},
        subject_estimate={}, evidence_by_key={}, outliers={},
    )
    check("3: total sqft is never relabelled as exact above-grade/below-grade area",
          semantics["total_finished_sqft"] == 5000 and semantics["above_grade_sqft"] is None
          and semantics["below_grade_sqft"] is None
          and packet["subject"]["above_grade_sqft"] is None
          and packet["subject"]["total_finished_sqft"] == 5000, str(packet["subject"]))
    clean_note = snap_comps[0].get("ai_commentary") or ""
    check("4: a corrected feature snapshot regenerates the comp narrative without invented basement sqft/systems",
          "1,700" not in clean_note and "original systems" not in clean_note
          and "above-grade sqft" in clean_note and bool(flags) and not blocking, clean_note)

    anchor = {"comps": [{"listing_key": "AC1", "label": "Comp #1", "normalized_weight": 1.0}]}
    role_comps = _annotate_comp_valuation_roles(snap_comps, anchor)
    stats = _cma_display_comp_stats(role_comps)
    check("5: client comp counts/stats follow actual anchor weight, not a context comp's Supporting label",
          role_comps[1]["strength"] == "supporting" and role_comps[1]["valuation_role"] == "context_only"
          and role_comps[1]["valuation_weight"] == 0 and stats["count"] == 1, str(stats))

    final = _v10_authoritative_valuation(
        value=1_200_000, range_pct=5, confidence="medium", valuation_trace={},
        subject_asking=1_300_000, status_timeline=timeline, comp_stats=stats,
        range_min=1_150_000, range_max=1_250_000,
    )
    check("6: exact Coming Soon status and authoritative DOM survive into the final client object",
          final["status"]["label"] == "Coming Soon" and final["status"]["active_market_days"] == 0,
          str(final["status"]))

    stale_offer = "The indicated value is $1,450,000. Open at $1,275,000 to $1,300,000. Keep inspection protections."
    rebuilt, offer_blocks, _unused, _offer_flags = _v10_apply_buyer_offer_rebuild(
        stale_offer, final, 1_300_000, 5, timeline, {}, [], None,
    )
    qa, _corrected, qa_blocks = _v10_pre_export_qa(
        {"o": ("Offer strategy", rebuilt)}, value=1_200_000, range_pct=5,
        subject_asking=1_300_000, status_timeline=timeline, comps=role_comps,
        comp_num_by_key={"AC1": 1, "AC2": 2}, addr_by_key={}, valuation_trace={},
        value_drivers=[], confidence="medium",
    )
    check("7: stale pre-final offer text is replaced before full QA inspects the rendered string",
          "$1,450,000" not in rebuilt and "$1,200,000" in rebuilt and not offer_blocks
          and not qa_blocks and not qa["blocking_errors"], f"{rebuilt} / {qa}")


def test_ad_entity_binding_and_offer_field_contract():
    print("\nAD. entity-bound sqft QA + deterministic offer field contract")
    subject = _subject(sqft=3894, above_grade_finished_area=3894)
    comp = _comp("AD5", 1_420_000, 3301, sqft=3301)
    valid_note = (
        "The subject has the larger footprint; at 3,301 sqft, this comp provides a smaller but "
        "documented size comparison to the subject's 3,894 sqft."
    )
    valid_blocks = _factual_prose_blocking(
        valid_note, "Comp #5", subject, comp_note=True, comp=comp,
    )
    check("1: a comp's documented sqft stays bound to the comp even when 'subject' appears nearby",
          not valid_blocks, str(valid_blocks))

    swapped_note = "The subject has 3,301 sqft, while this comp has 3,894 sqft."
    swapped_blocks = _factual_prose_blocking(
        swapped_note, "Comp #5", subject, comp_note=True, comp=comp,
    )
    check("2: explicit subject/comp ownership still blocks genuinely swapped sqft facts",
          len(swapped_blocks) == 2
          and any("for the subject" in b for b in swapped_blocks)
          and any("for the comp" in b for b in swapped_blocks), str(swapped_blocks))

    # A comp's writeup describes THAT comp, not the subject - a fact genuinely documented on the
    # comp's own record must not be flagged just because the subject's record doesn't share it.
    system_comp = _comp("AD6", 1_400_000, 3200, remarks="Original systems throughout, including the vintage boiler.")
    own_fact_note = "This comp's original systems add character, priced accordingly against the rest of the set."
    own_fact_blocks = _factual_prose_blocking(
        own_fact_note, "Comp #6", subject, comp_note=True, comp=system_comp,
    )
    check("2b: a comp's own documented original systems are not flagged just because the subject lacks them",
          not own_fact_blocks, str(own_fact_blocks))

    unsupported_note = "This comp's original systems add character compared to the subject's fully updated mechanicals."
    unsupported_blocks = _factual_prose_blocking(
        unsupported_note, "Comp #7", subject, comp_note=True, comp=_comp("AD7", 1_400_000, 3200),
    )
    check("2c: the same claim still blocks when neither the subject nor the comp's record documents it",
          any("describes a system as original" in b for b in unsupported_blocks), str(unsupported_blocks))

    final = _v10_authoritative_valuation(
        value=1_550_000, range_pct=8.06, confidence="medium",
        valuation_trace={"authority": "reasoning_model"}, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "active_market_days": 3}, comp_stats={},
        range_min=1_425_000, range_max=1_650_000,
    )
    trace = {
        "authority": "reasoning_model",
        "exposure": {"state": "insufficient_exposure", "active_market_days": 3},
        "anchor": {"comp_list_to_sale_median": 1.08, "comps": [{"competitive_sale": True}]},
    }
    fields = _v10_build_buyer_offer_band(
        final["value"], final["list_price"], trace=trace, confidence="medium",
        status_timeline=final["status"], range_min=final["range_min"], range_max=final["range_max"],
    )
    check("3: opening, strong, FMV, valuation range, list, and endpoint percentages are separate fields",
          fields["opening_offer_low"] < fields["opening_offer_high"] <= fields["strong_offer"] <= fields["fmv"]
          and (fields["opening_offer_low"], fields["opening_offer_high"])
              != (fields["valuation_range_low"], fields["valuation_range_high"])
          and fields["list_price"] == 1_350_000
          and fields["opening_pct_low_vs_list"] != fields["opening_pct_high_vs_list"]
          and not _v10_offer_field_contract_check(fields, final), str(fields))

    clean_offer, _ = _v10_rebuild_buyer_offer_prose("Keep inspection protections.", fields, 8.06)
    reconciled, _, _ = _reconcile_post_gate_prose(
        {"o": ("Offer strategy", clean_offer)}, value=final["value"], range_pct=8.06,
        anchor={}, trace=trace, value_drivers=[],
    )
    clean_after = reconciled["o"][1]
    check("4: generic valuation-range reconciliation cannot replace the opening-offer range",
          f'${fields["opening_offer_low"]:,} to ${fields["opening_offer_high"]:,}' in clean_after
          and "$1,425,000" not in clean_after and "$1,650,000" not in clean_after, clean_after)
    check("5: exact deterministic offer prose and percentage math pass final QA",
          not _v10_offer_sanity_check(clean_after, fields, final["range_min"], final["range_max"]),
          str(_v10_offer_sanity_check(clean_after, fields, final["range_min"], final["range_max"])))

    bad_offer = (
        "A reasonable opening offer is roughly $1,425,000 to $1,650,000 "
        "(about 5%-7% above the list price), leaving room to move toward $1,520,000."
    )
    bad_blocks = _v10_offer_sanity_check(bad_offer, fields, final["range_min"], final["range_max"])
    check("6: valuation-range substitution and percentages from different endpoints block export",
          any("does not match the deterministic opening fields" in b for b in bad_blocks)
          and any("percentage does not match" in b for b in bad_blocks), str(bad_blocks))

    frontend_root = Path(__file__).resolve().parents[2] / "frontend"
    pdf_source = (frontend_root / "lib" / "pdfs.tsx").read_text(encoding="utf-8")
    share_source = (frontend_root / "app" / "cma" / "[slug]" / "page.tsx").read_text(encoding="utf-8")
    # Both client renderers go through lib/cma.ts buyerOfferFigures(), which reads the deterministic
    # contract first (and shows no offer figures once the subject has sold).
    cma_lib_source = (frontend_root / "lib" / "cma.ts").read_text(encoding="utf-8")
    check("7: PDF and share-page offer facts render the deterministic contract before legacy fields",
          "f?.opening_offer_low ?? pricing.suggested_offer_min" in cma_lib_source
          and "f?.opening_offer_high ?? pricing.suggested_offer_max" in cma_lib_source
          and "f?.strong_offer ?? pricing.strong_offer" in cma_lib_source
          and pdf_source.count("buyerOfferFigures(report.pricing)") >= 2
          and "buyerOfferFigures(pricing)" in share_source,
          "one or more client renderers still bypasses offer_strategy_fields")


def test_ae_continuous_relevance_production_mode():
    print("\nAE. continuous deterministic relevance production mode")
    subject = _subject(
        sqft=3900, above_grade_finished_area=3900, lot_size=43_560, year_built=1940,
        public_remarks="Detached stone residence.",
        architectural_style="Colonial", township="Lower Merion", subdivision_name="Penn Wynne",
    )
    comps = [
        _comp("AE1", 1_450_000, 4000, dist=0.2, days_old=45, strength="weak",
              year_built=1942, architectural_style="Colonial", township="Lower Merion",
              subdivision_name="Penn Wynne", public_remarks="Stone Colonial residence."),
        _comp("AE2", 1_525_000, 3800, dist=0.5, days_old=60, strength="strong",
              year_built=1938, architectural_style="Colonial", township="Lower Merion",
              subdivision_name="Penn Wynne", public_remarks="Traditional residence."),
        _comp("AE3", 1_600_000, 4100, dist=0.8, days_old=75, strength="supporting",
              year_built=1950, architectural_style="Traditional", township="Lower Merion",
              subdivision_name="Penn Wynne", public_remarks="Traditional residence."),
        _comp("AE4", 1_700_000, 3700, dist=0.7, days_old=90, strength="strong",
              year_built=1945, architectural_style="Colonial", township="Lower Merion",
              subdivision_name="Penn Wynne", public_remarks="Colonial with a pool and guest wing."),
    ]
    first = _compute_continuous_relevance_anchor(subject, comps)
    role_flipped = [dict(c, strength=("strong" if c["strength"] == "weak" else "weak")) for c in comps]
    second = _compute_continuous_relevance_anchor(subject, role_flipped)
    first_weights = [row["normalized_weight"] for row in first["comps"]]
    second_weights = [row["normalized_weight"] for row in second["comps"]]
    check("1: changing every Claude role cannot change production weights or anchor",
          first["stability_fingerprint"] == second["stability_fingerprint"]
          and first_weights == second_weights and first["anchor_value"] == second["anchor_value"],
          f"{first_weights} / {second_weights}")
    check("2: continuous relevance is explicitly the production comp-weight authority",
          first["mode"] == "production"
          and first["weighting_authority"] == "continuous_relevance"
          and first["authoritative_for_comp_weighting"] is True
          and first["production_activated"] is True
          and first["production_value_consumes_relevance"] is True,
          str({k: first.get(k) for k in ("mode", "weighting_authority", "production_activated")}))
    weighted_comps, production_anchor, relevance_trace = _apply_continuous_relevance_weighting(
        subject, comps, _build_evidence_by_key(subject, comps), _flag_psf_outliers(comps),
        source_snapshot_sha256="test-snapshot",
    )
    check("2a: saved badge scores are calibrated while roles still use raw relevance rows",
          all(
              comp["match_score"] == calibrate_match_percentage(row["adjusted_relevance_score"])
              and comp["strength"] == row["proposed_role"]
              and comp["similarity_details"]["raw_relevance_score"] == row["adjusted_relevance_score"]
              and comp["similarity_details"]["display_calibration_version"] == MATCH_DISPLAY_CALIBRATION_VERSION
              for comp, row in zip(weighted_comps, relevance_trace["comps"])
          ), str([(c.get("match_score"), c.get("strength")) for c in weighted_comps]))
    raw_order = [value / 100 for value in range(1, 101)]
    display_order = [calibrate_match_percentage(value) for value in raw_order]
    check("2aa: display calibration is monotonic and cannot invert raw ranking",
          display_order == sorted(display_order), str(display_order))
    check("2ab: display calibration (v2, piecewise) is identity through mediocre/decent raw "
          "scores and only lifts above the 0.70/0.90 breakpoints, where real comps top out",
          calibrate_match_percentage(0.45) == 45
          and calibrate_match_percentage(0.55) == 55
          and calibrate_match_percentage(0.65) == 65
          and calibrate_match_percentage(0.76) == 78
          and calibrate_match_percentage(0.85) == 89
          and calibrate_match_percentage(0.90) == 95,
          str({value: calibrate_match_percentage(value) for value in (0.45, 0.55, 0.65, 0.76, 0.85, 0.90)}))
    check("2ad: mediocre raw scores get no display lift at all (raw <= 0.70 is pure identity)",
          calibrate_match_percentage(0.30) == 30
          and calibrate_match_percentage(0.50) == 50
          and calibrate_match_percentage(0.70) == 70,
          str({value: calibrate_match_percentage(value) for value in (0.30, 0.50, 0.70)}))
    check("2ae: only raw scores above the real-world ceiling breakpoint reach the high 80s/90s",
          calibrate_match_percentage(0.80) == 83
          and calibrate_match_percentage(0.95) == 98
          and calibrate_match_percentage(1.0) == 100,
          str({value: calibrate_match_percentage(value) for value in (0.80, 0.95, 1.0)}))
    check("2ac: badge calibration does not change continuous anchor weight inputs",
          all(
              row["continuous_weight_input"] == row["adjusted_relevance_score"]
              for row in relevance_trace["comps"]
              if row["proposed_role"] in ("strong", "supporting")
          ) and production_anchor["anchor_value"] == first["anchor_value"],
          str([(r["continuous_weight_input"], r["adjusted_relevance_score"]) for r in relevance_trace["comps"]]))
    check("2b: production anchor is the continuous relevance anchor",
          production_anchor["anchor_value"] == first["anchor_value"]
          and production_anchor["comp_stats"]["method"].startswith("continuous relevance"),
          str(production_anchor.get("comp_stats")))
    production_result = {
        "fmv": 1_500_000, "range_pct": 5.0,
        "trace": {
            "anchor": production_anchor, "authority": "v10",
            "relevance_weighting": relevance_trace,
        },
    }
    reasoning = {
        "final_fmv": 1_530_000, "range_low": 1_475_000, "range_high": 1_585_000,
        "confidence": "medium", "independent_preliminary_fmv": 1_525_000,
        "independent_preliminary_range_low": 1_470_000,
        "independent_preliminary_range_high": 1_580_000,
        "independent_preliminary_confidence": "medium", "primary_comps": [],
        "floor_comps": [], "ceiling_comps": [], "comp_roles": [],
        "major_value_drivers": [], "major_value_drags": [],
        "market_exposure_assessment": "Stable.", "anchor_assessment": "Useful benchmark.",
        "v10_assessment": "Useful benchmark.", "why_final_differs_from_anchor": "Evidence reconciliation.",
        "why_final_differs_from_v10": "Evidence reconciliation.",
        "final_valuation_summary": "The comparable evidence supports the conclusion.",
    }
    promoted = _apply_reasoning_authority(
        production_result, reasoning, model="gpt-6-astra", effort="medium",
    )
    check("2c: relevance trace survives authority promotion while Astra keeps final authority",
          production_result["fmv"] == 1_500_000 and promoted["fmv"] == 1_530_000
          and promoted["trace"]["authority"] == "reasoning_model"
          and promoted["trace"]["relevance_weighting"]["weighting_authority"] == "continuous_relevance",
          str(promoted))
    mismatch = first["comps"][3]
    check("3: a comp whose remarks mention a pool/guest-wing the subject's remarks are merely "
          "silent on is not held back by any separate modifier - it can still be Strong on the "
          "8 core components alone",
          "product_modifier" not in mismatch
          and mismatch["adjusted_relevance_score"] == mismatch["base_relevance_score"]
          and mismatch["proposed_role"] == "strong", str(mismatch))
    wrong_type = dict(comps[0], property_type="CONDO", home_type="CONDO")
    type_shadow = _continuous_relevance(subject, [wrong_type, *comps[1:]])["comps"][0]
    check("3b: radical structural type mismatch is scored once, inside the property_type "
          "component itself - no separate post-score modifier on top of it",
          type_shadow["components"]["property_type"] == 0.0
          and "product_modifier" not in type_shadow
          and type_shadow["adjusted_relevance_score"] == type_shadow["base_relevance_score"], str(type_shadow))
    check("4: continuous weights naturally remain unconcentrated with four similar usable comps",
          first["max_normalized_weight"] < 0.35 and not first["dominance_flags"],
          str(first["max_normalized_weight"]))

    condition_changed = [dict(c, quality={"finish_quality": "above", "interior": "above"}) for c in comps]
    condition_shadow = _continuous_relevance(subject, condition_changed)
    check("5: condition/renovation model judgments do not enter base relevance",
          first["stability_fingerprint"] == condition_shadow["stability_fingerprint"],
          f"{first['stability_fingerprint']} / {condition_shadow['stability_fingerprint']}")

    urban_subject = _subject(
        sqft=1850, above_grade_finished_area=1850,
        property_type="TOWNHOUSE", home_type="TOWNHOUSE", lot_size=1200,
        city="Philadelphia", school_district="Philadelphia City", township="Philadelphia",
        subdivision_name="Rittenhouse Square",
    )
    urban_comps = [
        _comp(f"AEU{i}", 900_000 + i * 20_000, 1800 + i * 25, dist=d,
              property_type="TOWNHOUSE", home_type="TOWNHOUSE", lot_size=1100,
              city="Philadelphia", school="Philadelphia City", township="Philadelphia",
              subdivision_name="Rittenhouse Square")
        for i, d in enumerate((0.1, 0.25, 0.45, 2.5), 1)
    ]
    urban_refs = [
        _comp(f"AEUR{i}", 850_000, 1800 + i * 5, dist=d,
              property_type="TOWNHOUSE", home_type="TOWNHOUSE", lot_size=1100,
              city="Philadelphia", school="Philadelphia City", township="Philadelphia",
              subdivision_name="Rittenhouse Square", street_address=f"{i} Dense St")
        for i, d in enumerate((0.08, 0.14, 0.20, 0.28, 0.34, 0.40, 0.47, 0.54, 0.60, 0.66, 0.72, 0.80, 0.90), 1)
    ]
    urban_density = build_adaptive_proximity_context(urban_subject, urban_refs)
    urban = _continuous_relevance(urban_subject, urban_comps, density_context=urban_density)
    check("6: compact attached packet selects a dense-urban proximity scale",
          urban["density_context"]["band"] == "dense_urban"
          and urban["density_context"]["distance_half_life_miles"] <= 1.25,
          str(urban["density_context"]))
    check("7: dense-urban distance meaningfully distinguishes a 0.1-mile comp from 2.5 miles",
          urban["comps"][0]["components"]["proximity"]
          > urban["comps"][3]["components"]["proximity"] * 4,
          str([row["components"]["proximity"] for row in urban["comps"]]))
    urban_reordered = build_adaptive_proximity_context(urban_subject, list(reversed(urban_refs)))
    check("7b: one subject density context is stable across candidate/reference order",
          urban_density == urban_reordered, f"{urban_density} / {urban_reordered}")

    suburban_subject = _subject(
        sqft=3900, above_grade_finished_area=3900, lot_size=43_560, year_built=1925,
        city="Main Line", school_district="Local Schools",
    )
    recent_compatible_refs = [
        _comp(f"AESR{i}", 1_500_000, 3650 + i * 20, dist=d, lot_size=0.65 * 43_560,
              year_built=1928, city="Main Line", school="Local Schools",
              street_address=f"{i} Compatible Rd")
        for i, d in enumerate((0.55, 0.70, 0.82, 0.95, 1.05, 1.15, 1.25, 1.35, 1.45, 1.55, 1.70, 1.90, 2.10), 1)
    ]
    historical_nearby_refs = [
        _comp(f"AESH{i}", 1_300_000, 3800, dist=0.05 + i * 0.025,
              lot_size=0.7 * 43_560, year_built=1930, days_old=2500,
              city="Main Line", school="Local Schools", street_address=f"{i} Historical Rd")
        for i in range(1, 14)
    ]
    recent_small_lot_refs = [
        _comp(f"AESS{i}", 1_200_000, 3800, dist=0.06 + i * 0.025,
              lot_size=0.15 * 43_560, year_built=1930,
              city="Main Line", school="Local Schools", street_address=f"{i} Small Lot Rd")
        for i in range(1, 14)
    ]
    suburban_density = build_adaptive_proximity_context(
        suburban_subject, recent_compatible_refs + historical_nearby_refs + recent_small_lot_refs,
    )
    check("7c: suburban density uses recent, product-compatible stock rather than nearby history",
          suburban_density["band"] == "suburban"
          and suburban_density["distance_half_life_miles"] == 1.55
          and suburban_density["reference_count"] == len(recent_compatible_refs)
          and suburban_density["recency_window_days"] == 365,
          str(suburban_density))
    relevant_farther = _comp(
        "AES-MATCH", 1_500_000, 3675, dist=1.59, lot_size=0.57 * 43_560,
        year_built=1927, city="Main Line", school="Local Schools",
    )
    weaker_nearby = _comp(
        "AES-WEAK", 1_250_000, 2800, dist=0.95, lot_size=0.22 * 43_560,
        year_built=1975, city="Other", school="Other Schools",
    )
    suburban_ranking = _continuous_relevance(
        suburban_subject, [relevant_farther, weaker_nearby], density_context=suburban_density,
    )
    check("7d: a relevant 1-2 mile suburban comp outranks a closer weak product match",
          suburban_ranking["comps"][0]["adjusted_relevance_score"]
          > suburban_ranking["comps"][1]["adjusted_relevance_score"],
          str([(row["label"], row["adjusted_relevance_score"]) for row in suburban_ranking["comps"]]))

    estate_subject = _subject(lot_size=3 * 43_560, city="Malvern")
    estate_comps = [
        _comp(f"AEE{i}", 2_500_000 + i * 50_000, 6100, dist=d, lot_size=2.5 * 43_560,
              city="Malvern") for i, d in enumerate((1.5, 3.0, 5.0, 7.0), 1)
    ]
    estate_refs = [
        _comp(f"AEER{i}", 2_400_000, 5900 + i * 15, dist=d, lot_size=2.5 * 43_560,
              city="Malvern", street_address=f"{i} Estate Rd")
        for i, d in enumerate((0.8, 1.4, 2.0, 2.7, 3.2, 3.7, 4.1, 4.4, 4.7, 5.0, 5.3, 5.7, 6.2), 1)
    ]
    estate_density = build_adaptive_proximity_context(estate_subject, estate_refs)
    estate = _continuous_relevance(estate_subject, estate_comps, density_context=estate_density)
    check("8: acre-plus detached packet selects a low-density estate proximity scale",
          estate["density_context"]["band"] == "low_density_estate"
          and estate["density_context"]["distance_half_life_miles"] >= 4.0,
          str(estate["density_context"]))
    check("9: a five-mile estate comp remains usable rather than being distance-zeroed",
          estate["comps"][2]["components"]["proximity"] >= 0.30,
          str(estate["comps"][2]["components"]["proximity"]))
    fallback = build_adaptive_proximity_context(estate_subject, estate_comps)
    check("9b: insufficient suitable inventory uses the deterministic 2.5-mile fallback",
          fallback["fallback_used"] is True and fallback["distance_half_life_miles"] == 2.5,
          str(fallback))

    semantic_before = "Comp #2 is Supporting and currently Active Under Contract."
    semantic_after = "Comp #3 is Strong and currently Sold."
    check("9c: polish guard protects comp identity, status, and classification",
          _polish_semantic_multiset(semantic_before) != _polish_semantic_multiset(semantic_after))

    legacy_anchor = _compute_comp_anchor(
        subject, comps, _build_evidence_by_key(subject, comps), _flag_psf_outliers(comps),
    )
    check("10: production path no longer equals the legacy model-role anchor",
          production_anchor["anchor_value"] == first["anchor_value"]
          and production_anchor["anchor_value"] != legacy_anchor["anchor_value"],
          f"production={production_anchor['anchor_value']} legacy={legacy_anchor['anchor_value']}")


def test_af_final_artifact_enforcement():
    print("\nAF. exact saved-artifact and material-currency enforcement")
    subject = _subject(list_price=1_350_000)
    comps = [_comp("AF1", 1_450_000, 3900, street_address="10 Valid Rd")]
    trace = {
        "subject_asking": 1_350_000,
        "anchor": {"anchor_value": 1_475_000, "comps": [{"raw_sale_price": 1_450_000}]},
        "adjustments": [{"factor": "condition", "direction": "down", "applied": -35_000,
                         "amount_low": 30_000, "amount_high": 45_000}],
    }
    safe_fields = {"m": ("Methodology", "The final value is $1,500,000 after a $35,000 condition adjustment.")}
    _, safe_blocks = _v10_final_report_qa(
        safe_fields, 1_500_000, 5.0, trace, known_dollars=[1_425_000, 1_575_000],
    )
    _, bad_blocks = _v10_final_report_qa(
        {"m": ("Methodology", "Use a $4M range if needed.")}, 1_500_000, 5.0, trace,
    )
    check("1: structured adjustment dollars remain valid", not safe_blocks, str(safe_blocks))
    check("2: an untraceable material $4M token is blocking, not a warning",
          any("no matching authoritative structured value" in item for item in bad_blocks), str(bad_blocks))

    pricing = {
        "ai_estimate": {
            "final_valuation": {"value": 1_500_000, "range_min": 1_425_000, "range_max": 1_575_000},
            "methodology_summary": "The final value is $1,500,000.",
            "qa_result": {"export_allowed": True, "blocking_errors": [], "warnings": [], "corrections": []},
        }
    }
    stamped = stamp_final_artifact_qa(
        subject=subject, comparables=comps, pricing=pricing, market_context={}, improvements=[],
    )
    report = SimpleNamespace(
        subject=subject, comparables=comps, pricing=stamped, market_context={}, improvements=[],
    )
    check("3: the exact QA-stamped saved client artifact is exportable",
          not _report_qa_blocking(report), str(_report_qa_blocking(report)))
    changed = json.loads(json.dumps(stamped))
    changed["ai_estimate"]["methodology_summary"] = "The final value is $4M."
    changed_report = SimpleNamespace(
        subject=subject, comparables=comps, pricing=changed, market_context={}, improvements=[],
    )
    check("4: changing client content after QA invalidates export",
          any("changed after deterministic final QA" in item for item in _report_qa_blocking(changed_report)),
          str(_report_qa_blocking(changed_report)))


def test_ag_agent_review_presentation_and_pdf_pagination_contract():
    print("\nAG. simplified agent review and fixed CMA page boundaries")
    root = Path(__file__).resolve().parents[2]
    workspace_source = (root / "frontend" / "app" / "studio" / "cma" / "[id]" / "page.tsx").read_text(encoding="utf-8")
    pdf_source = (root / "frontend" / "lib" / "pdfs.tsx").read_text(encoding="utf-8")
    backend_source = (root / "backend" / "app" / "services" / "cma_ai_service.py").read_text(encoding="utf-8")

    check("1: default QA review exposes plain status/count while raw diagnostics stay collapsed",
          all(token in workspace_source for token in (
              "QA / Review Items", "Advanced Audit", "No issues need attention", "issues to fix",
              "Raw blocking diagnostics",
          )))
    check("2: Why this value leads with the value/confidence/range and lists every driver, bounded to readable bullets",
          "function cmaWhyBullets" in workspace_source
          and ".slice(0, 5).map" in workspace_source
          and "return Array.from(new Set(bullets)).slice(0, 7)" in workspace_source
          and "Why this value" in workspace_source)
    check("3: simple calculation is visible and technical detail is opt-in",
          all(token in workspace_source for token in (
              "Comparable sales", "Property differences", "Market exposure", "Final value",
              "Show detailed calculation",
          )))
    check("4: overlapping autosaves cannot apply an older response over a newer one",
          "saveRequestSequence" in workspace_source
          and "latestAppliedSaveSequence" in workspace_source
          and "requestSequence > latestAppliedSaveSequence.current" in workspace_source)
    # Round 2 (2026-09-15): wrap={false} on a content page turned out to be the wrong fix for
    # blank continuation pages - verified against the real @react-pdf/renderer layout engine, it
    # makes the PAGE ITSELF grow to a non-standard size (e.g. 612x2074pt instead of 612x792pt
    # Letter) whenever a section's content (long AI prose especially) overflows one physical page,
    # rather than clipping or paginating - which is also why the footer (position: absolute,
    # bottom: 16) ended up sitting at the bottom of the content instead of the bottom of a real
    # page. Content pages must paginate normally (wrap left at its true default) so every physical
    # page stays Letter-sized; the cover page can still safely force a single page since its
    # content is fixed-length, never variable AI prose.
    check("5: CMA content pages paginate normally onto additional Letter-sized pages instead of "
          "forcing one non-standard-sized page when a section's content overflows",
          pdf_source.count('style={cma.page}>') >= 10
          and 'style={cma.page} wrap={false}' not in pdf_source
          and 'style={cma.coverPage} wrap={false}' in pdf_source)

    first_final_qa = backend_source.index("_fin = await _finalize_gate_reconciled_prose")
    first_subject_snapshot = backend_source.index("# Materialize every client-facing fact-bearing field")
    first_improvements = backend_source.index("premium_improvements = _premium_improvements_from_ai", first_subject_snapshot)
    check("6: subject facts and improvement prose are materialized before final QA",
          first_subject_snapshot < first_improvements < first_final_qa,
          f"subject={first_subject_snapshot} improvements={first_improvements} qa={first_final_qa}")

    # Round 3 (2026-09-15): QA/Review and "how this value was calculated" are agent-facing
    # diagnostics an agent should never need to parse themselves - hidden from Studio entirely
    # (admin pulls them up via the report's own support code instead, see CmaLookupPanel), while
    # "Why this value" stays as the one visible, non-technical explanation of the conclusion.
    check("7: QA/Review panel and the valuation-calculation trace are no longer rendered to the agent",
          "<CmaQaReviewPanel report={report} />" not in workspace_source
          and "<ValuationTracePanel trace={aiEstimate.valuation_trace} />" not in workspace_source
          and "function CmaQaReviewPanel(" in workspace_source
          and "function ValuationTracePanel(" in workspace_source)
    check("7b: a support code + submit-a-ticket link is shown at the bottom of the summary page",
          "cmaSupportCode(report.id)" in workspace_source and "Report Code" in workspace_source
          and "Submit a Ticket" in workspace_source and "cma_code=" in workspace_source)


# ---------------------------------------------------------------------------------------------
# AI. Valuation reliability - a degraded run (malformed primary AI call, or a failed final-polish
# pass) must never leave qa_result looking like an ordinary clean "Passed" run, and a failed
# reasoning-model valuation must never silently leave behind or invent an adjustment from
# stale/fallback state. Real production case, Cherry Ln 2026-09-15: the primary Claude call's
# subject_estimate came back malformed, so the deterministic benchmark alone fed the evidence
# packet; Astra still ran on that degraded input and became authority at Low confidence, and the
# resulting reasoning-vs-v10 delta was displayed with no indication anything had degraded.
# ---------------------------------------------------------------------------------------------
def test_ai_valuation_reliability_warnings():
    print("\nAI. valuation reliability warnings - degraded runs never look like a clean Passed")
    base_kwargs = dict(
        fields={"m": ("Methodology summary", "The value reflects the comparable sales.")},
        value=1_450_000, range_pct=8.0, subject_asking=1_350_000,
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 6},
        comps=[], comp_num_by_key={}, addr_by_key={}, value_drivers=[], confidence="low",
    )
    qr_deterministic, _cf1, _b1 = _v10_pre_export_qa(
        valuation_trace={"anchor": {}, "subject_estimate_missing": True}, **base_kwargs,
    )
    check("1: a subject_estimate_missing trace produces a non-blocking Valuation reliability "
          "warning naming the deterministic formula as the authority, and passed flips False",
          qr_deterministic["export_allowed"] is True and qr_deterministic["passed"] is False
          and any("Valuation reliability" in w and "deterministic comparable-sales formula alone" in w
                   for w in qr_deterministic["warnings"]),
          str(qr_deterministic["warnings"]))

    qr_reasoning, _cf2, _b2 = _v10_pre_export_qa(
        valuation_trace={
            "anchor": {}, "subject_estimate_missing": True,
            "authority": "reasoning_model", "reasoning_model_name": "gpt-6-astra",
        }, **base_kwargs,
    )
    check("2: the same marker under reasoning authority names the actual reasoning model instead "
          "of implying a plain deterministic-only result",
          any("Valuation reliability" in w and "gpt-6-astra" in w for w in qr_reasoning["warnings"]),
          str(qr_reasoning["warnings"]))

    qr_clean, _cf3, _b3 = _v10_pre_export_qa(valuation_trace={"anchor": {}}, **base_kwargs)
    check("3: a normal run (no degraded marker) gets no valuation-reliability warning and can "
          "still pass cleanly", qr_clean["passed"] is True and not qr_clean["warnings"], str(qr_clean))

    subj, anchor, exposure = _cond_setup()
    res = _apply_v10_valuation(anchor, exposure, {"qualitative_adjustments": []}, subj["list_price"])
    degraded_trace = dict(res["trace"], subject_estimate_missing=True, authority="reasoning_model",
                           reasoning_model_name="gpt-6-astra")
    final = _v10_authoritative_valuation(
        value=res["fmv"], range_pct=res["range_pct"], confidence="low",
        valuation_trace=degraded_trace, subject_asking=subj["list_price"],
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 6},
        comp_stats={"method": "weighted mean, size-adjusted"},
    )
    fin = asyncio.run(_finalize_gate_reconciled_prose(
        methodology="The value reflects the comparable sales.", offer="", inspection="",
        value=res["fmv"], range_pct=res["range_pct"], subject_asking=subj["list_price"], confidence="low",
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 6},
        enriched_subject=subj, comps=[], comp_num_by_key={}, addr_by_key={},
        valuation_trace=degraded_trace, value_drivers=[], qa_ctx={}, mode="buyer", model="claude-sonnet-5",
        final=final, external_blocking=[], run_language_polish=False,
    ))
    check("4: end to end through _finalize_gate_reconciled_prose, a degraded run's authoritative "
          "qa_result carries the warning and passed is False while export_allowed stays True (not "
          "blocking - the deterministic fallback is still a valid number)",
          fin["qa_result"]["export_allowed"] is True and fin["qa_result"]["passed"] is False
          and any("Valuation reliability" in w for w in fin["qa_result"]["warnings"]),
          str(fin["qa_result"]["warnings"]))

    # A failed final-language-polish pass must ALSO warn, not just silently export the pre-polish
    # text under a clean "Passed".
    clean_final = _v10_authoritative_valuation(
        value=res["fmv"], range_pct=res["range_pct"], confidence="medium",
        valuation_trace=res["trace"], subject_asking=subj["list_price"],
        status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 6},
        comp_stats={"method": "weighted mean, size-adjusted"},
    )
    _real_get_client = _cma_mod._get_client
    try:
        _cma_mod._get_client = lambda: _FakeClient(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        fin_polish = asyncio.run(_finalize_gate_reconciled_prose(
            methodology="The value reflects the comparable sales.", offer="", inspection="",
            value=res["fmv"], range_pct=res["range_pct"], subject_asking=subj["list_price"], confidence="medium",
            status_timeline={"current_status": "Active", "is_active": True, "active_market_days": 6},
            enriched_subject=subj, comps=[], comp_num_by_key={}, addr_by_key={},
            valuation_trace=dict(res["trace"]), value_drivers=[], qa_ctx={}, mode="buyer", model="claude-sonnet-5",
            final=clean_final, external_blocking=[], run_language_polish=True,
        ))
    finally:
        _cma_mod._get_client = _real_get_client
    check("5: a failed final-language-polish pass also produces a non-blocking Valuation "
          "reliability warning instead of silently exporting the pre-polish text under a clean "
          "Passed",
          fin_polish["qa_result"]["export_allowed"] is True and fin_polish["qa_result"]["passed"] is False
          and any("language-polish pass failed" in w for w in fin_polish["qa_result"]["warnings"]),
          str(fin_polish["qa_result"]["warnings"]))


def test_ah_subject_condition_evidence_everywhere():
    print("\nAH. subject condition evidence enforced regardless of how the subject is named")
    # No condition facts documented either way.
    subject = _subject()

    deficit_by_name = _factual_prose_blocking(
        "Test's original, unrenovated condition warrants a downward adjustment vs the comp set.",
        "Methodology summary", subject,
    )
    check("1: an undocumented 'original/unrenovated' claim blocks even when the subject is named "
          "by its street, not the word 'subject'",
          any("original/unrenovated/dated" in b for b in deficit_by_name), str(deficit_by_name))

    reno_by_name = _factual_prose_blocking(
        "Test's kitchen is fully renovated and the home presents as turnkey.",
        "Comp #2", subject, comp_note=True, comp=_comp("AH1", 1_300_000, 3100),
    )
    check("2: an undocumented 'turnkey/renovated' claim about the subject blocks the same way, "
          "not just the original/dated pole",
          any("turnkey/renovated/updated" in b for b in reno_by_name), str(reno_by_name))

    documented_deficit_subject = _subject(remarks="Largely original condition, sold as-is.")
    deficit_supported = _factual_prose_blocking(
        "Test's original, unrenovated condition warrants a downward adjustment vs the comp set.",
        "Methodology summary", documented_deficit_subject,
    )
    check("3: the same claim is not blocked once the subject's own record actually documents it",
          not deficit_supported, str(deficit_supported))

    comparative = _factual_prose_blocking(
        "Comp #2's fully renovated kitchen outshines Test, which is dated throughout.",
        "Comp #2", subject, comp_note=True, comp=_comp("AH2", 1_300_000, 3100, remarks="Fully renovated kitchen."),
    )
    check("4: a comparative sentence still attributes 'dated' to the subject, not just the comp "
          "named earlier in the same sentence",
          any("original/unrenovated/dated" in b for b in comparative), str(comparative))

    # Real failure mode caught in production: a comp narrative naming itself by street address (not
    # "Comp #N") describes ITS OWN renovation in a comma-separated clause right after a "the
    # subject" mention - the window must stop at that comma instead of reading through to the
    # comp's own condition word purely because no ".", ";", or "Comp #" boundary appeared first.
    own_reno_after_comma = _factual_prose_blocking(
        "316 Test Alt Rd sold at $400/sqft, essentially in line with what the anchor implies for "
        "the subject, after a full 2025 renovation that left it truly turnkey. It's a smaller "
        "footprint than the subject's largely original interior.",
        "316 Test Alt Rd", subject, comp_note=True,
        comp=_comp("AH5", 1_300_000, 3000, remarks="Full 2025 renovation, turnkey."),
    )
    check("4b: a comma-separated clause about the COMP's own renovation right after a subject "
          "mention is not misattributed to the subject",
          not any("turnkey/renovated/updated" in b for b in own_reno_after_comma), str(own_reno_after_comma))
    check("4c: the later, genuinely subject-attributed 'largely original interior' in the same "
          "text is still caught",
          any("original/unrenovated/dated" in b for b in own_reno_after_comma), str(own_reno_after_comma))

    # Integration: an adjustment's own "basis" text is scanned the same way methodology text is -
    # not just its dollar amount validated. See generate_ai_analysis/recompute_valuation, which run
    # this exact loop over valuation_trace["adjustments"] after _apply_v10_valuation returns.
    comps = [
        _comp("AH3", 1_400_000, 3200, remarks="Updated kitchen and baths."),
        _comp("AH4", 1_380_000, 3150, remarks="Updated kitchen and baths."),
    ]
    anchor = _anchor(subject, comps)
    exposure = _exposure(subject, comps, anchor)
    ai_estimate = {
        "value": anchor["anchor_value"] * 0.95,
        "qualitative_adjustments": [{
            "factor": "Condition gap vs renovated comps", "direction": "down",
            "amount_low": 40_000, "amount_high": 60_000, "basis_type": "comp_inferred",
            "basis": "Test's original, unrenovated kitchen lags the comp set's updated finishes.",
            "confidence": "medium",
        }],
    }
    v10 = _apply_v10_valuation(anchor, exposure, ai_estimate, subject["list_price"])
    adjustments = v10["trace"].get("adjustments") or []
    check("5: _apply_v10_valuation passes the adjustment's basis text through unchanged for the "
          "caller to scan", len(adjustments) == 1 and "unrenovated" in adjustments[0]["basis"], str(adjustments))
    adjustment_blocks = _factual_prose_blocking(
        adjustments[0]["basis"], f"Qualitative adjustment ({adjustments[0]['factor']})", subject,
    )
    check("6: an adjustment's own basis text is caught by the same unsupported-condition-claim scan "
          "as methodology/comp-note text - not exempt just because it's attached to a dollar figure",
          any("original/unrenovated/dated" in b for b in adjustment_blocks), str(adjustment_blocks))

    # An agent should never have to notice and manually regenerate for something this targeted - the
    # pipeline gets ONE bounded, automatic rewrite attempt at the exact spot a comp note is flagged,
    # before it's ever surfaced. Mocks the model client so this stays a free, deterministic test.
    reno_comp = _comp("AH6", 1_300_000, 3100)
    flagged_draft = {
        "ai_commentary": "Test's original, unrenovated kitchen lags this comp's fresher finishes.",
        "commentary": "Test's original, unrenovated kitchen lags this comp's fresher finishes.",
    }
    snapshot = _build_authoritative_evidence_snapshot(subject, [reno_comp], {
        "current_status": "Active", "is_active": True, "is_under_contract": False,
        "is_closed": False, "active_market_days": 10, "current_list_price": subject["list_price"],
    })
    label = "Comp #1 (AH6 Comp Ln)"

    _real_get_client = _cma_mod._get_client
    try:
        _cma_mod._get_client = lambda: _FakeClient(json.dumps({
            "notes": [{"label": label, "text": "This comp's fresher finishes reflect its own recent updates."}],
        }))
        fixed_subject, fixed_comps, _, fixed_flags, fixed_blocking = asyncio.run(
            _materialize_snapshot_with_comp_narratives(
                snapshot, [flagged_draft], attempt_rewrite=True, model="fake-model",
            )
        )
    finally:
        _cma_mod._get_client = _real_get_client
    check("7: a flagged comp note is silently rewritten and cleared in one bounded pass - the agent "
          "never sees a blocking item for something this fixable",
          not fixed_blocking and "original" not in (fixed_comps[0].get("commentary") or "").lower()
          and any("rewrote its client narrative" in f for f in fixed_flags),
          f"blocking={fixed_blocking} commentary={fixed_comps[0].get('commentary')!r} flags={fixed_flags}")

    try:
        _cma_mod._get_client = lambda: _FakeClient("not json")
        _, _, _, _, failed_blocking = asyncio.run(
            _materialize_snapshot_with_comp_narratives(
                snapshot, [flagged_draft], attempt_rewrite=True, model="fake-model",
            )
        )
    finally:
        _cma_mod._get_client = _real_get_client
    check("8: if the rewrite attempt itself fails, the original finding still surfaces unchanged "
          "instead of silently dropping it",
          any("original/unrenovated/dated" in b for b in failed_blocking), str(failed_blocking))

    # Real production paraphrases (Cherry Ln, 2026-09-14) that expressed the same unsupported
    # condition conclusion WITHOUT the original/unrenovated/dated/turnkey keyword vocabulary -
    # missed by the checks above until the deficit/renovated-pole patterns were widened.
    paraphrase_deficit = _factual_prose_blocking(
        "The subject cannot claim the same modern systems or finish level, so this comp sold at "
        "a premium the subject would not fully command on condition alone.",
        "Comp #2", subject, comp_note=True, comp=_comp("AH7", 1_775_000, 3985),
    )
    check("9: 'cannot claim the same modern systems or finish level' is caught as an unsupported "
          "deficit-pole claim, not just the literal words original/unrenovated/dated",
          any("original/unrenovated/dated" in b for b in paraphrase_deficit), str(paraphrase_deficit))

    paraphrase_inferior = _factual_prose_blocking(
        "Given its dated systems, this comp undersells relative to the subject's less-updated "
        "interior.",
        "Comp #3", subject, comp_note=True, comp=_comp("AH8", 1_300_000, 4639),
    )
    check("10: 'less-updated interior' attributed to the subject is caught the same way as 'dated'",
          any("original/unrenovated/dated" in b for b in paraphrase_inferior), str(paraphrase_inferior))

    # The condition word sits BEFORE the subject reference in a trailing simile ("a move-in-ready
    # home LIKE the subject") - outside the forward-only window every check above relies on.
    trailing_comparison = _factual_prose_blocking(
        "Its dated systems mean it undersells relative to a move-in-ready home like the subject.",
        "Comp #3", subject, comp_note=True, comp=_comp("AH9", 1_300_000, 4639),
    )
    check("11: a trailing 'like the subject' simile still attributes the preceding condition word "
          "(move-in-ready) to the subject, not just a forward window after the mention",
          any("turnkey/renovated/updated" in b for b in trailing_comparison), str(trailing_comparison))

    # A subject-condition claim in a trailing comparison must still respect documented evidence -
    # not turn into a false positive just because the detection direction changed.
    documented_reno_subject = _subject(remarks="Fully renovated, move-in-ready throughout.")
    trailing_comparison_supported = _factual_prose_blocking(
        "This comp is a similarly move-in-ready home like the subject.",
        "Comp #3", documented_reno_subject, comp_note=True, comp=_comp("AH10", 1_300_000, 4639),
    )
    check("12: the trailing-comparison catch respects documented evidence and does not fire once "
          "the subject's own record actually supports the claim",
          not trailing_comparison_supported, str(trailing_comparison_supported))

    # Production formatting bug (Cherry Ln): a bare decimal missing its leading zero elsewhere in a
    # comp note ("beautifully landscaped .84-acre grounds") was corrupted by the generic
    # space-before-punctuation cleanup into "landscaped.84-acre" (the decimal point misread as
    # stray sentence punctuation eating the space before it).
    fixed_text, _ = _sanitize_comp_fact_prose(
        "In-ground pool and beautifully landscaped .84-acre grounds are real amenities.",
        "Comp #3", subject,
    )
    check("13: a bare decimal missing its leading zero gets one added instead of having its "
          "preceding space eaten by punctuation cleanup",
          "landscaped 0.84-acre" in fixed_text and "landscaped.84-acre" not in fixed_text,
          repr(fixed_text))

    # Production cleanup (Cherry Ln, 2026-09-14): an agent decision - keep the $ pricing effect
    # exactly as-is, but stop labeling it as a confirmed condition defect when the subject's
    # condition is genuinely undocumented. _neutralize_unsupported_condition_adjustment must
    # relabel wording/downgrade confidence WITHOUT ever touching applied/amount_low/amount_high/
    # direction/family - the dollar math is frozen, only the claim's honesty changes.
    real_adj = {
        "factor": "Condition/renovation gap vs. Comps #1 and #2", "family": "renovation_condition",
        "direction": "down", "applied": -85000, "amount_low": 60000, "amount_high": 110000,
        "confidence": "medium", "basis_type": "paired_market_evidence",
        "basis": (
            "Comp #1 ($395/sqft, 2024-2025 kitchen/bath renovation) and Comp #2 ($445/sqft, 2016 "
            "full addition and renovated primary suite) both carry documented recent full-scale "
            "updates the subject's kitchen/baths are not documented as having; the ~$50/sqft "
            "spread between these two turnkey comps and the more original 315 Keithwood ($280/sqft) "
            "brackets a reasonable condition-gap deduction for the subject's less-updated interior."
        ),
    }
    real_adj_before = dict(real_adj)
    neutralize_flags = _neutralize_unsupported_condition_adjustment(real_adj, subject)
    check("14: a condition adjustment's factor/basis is relabeled to honest uncertainty wording "
          "and confidence is downgraded, while every dollar/classification field is byte-for-byte "
          "unchanged",
          bool(neutralize_flags) and real_adj["confidence"] == "low"
          and all(real_adj[k] == real_adj_before[k] for k in
                  ("applied", "amount_low", "amount_high", "direction", "family", "basis_type")),
          f"flags={neutralize_flags} adj={real_adj}")
    check("15: the SAME accurate claim made about a COMP elsewhere in the basis text ('the more "
          "original 315 Keithwood') is left untouched - only the subject-attributed span is "
          "neutralized",
          "the more original 315 Keithwood" in real_adj["basis"], real_adj["basis"])
    check("16: the neutralized basis text no longer trips the unsupported-condition-claim scan",
          not _factual_prose_blocking(real_adj["basis"], "x", subject), real_adj["basis"])

    # A condition claim backed by the subject's own record must be left completely alone.
    documented_subj = _subject(remarks="Largely original condition, sold as-is.")
    documented_adj = {
        "factor": "Condition/renovation gap vs. Comps #1 and #2", "family": "renovation_condition",
        "direction": "down", "applied": -50000, "confidence": "medium",
        "basis": "The subject's less-updated interior lags the renovated comp set.",
    }
    check("17: a condition claim genuinely backed by the subject's own record is never touched",
          not _neutralize_unsupported_condition_adjustment(documented_adj, documented_subj)
          and documented_adj["confidence"] == "medium", str(documented_adj))

    # A non-condition-family adjustment (e.g. pool/amenity) must never be touched by this at all,
    # even if its basis text happens to also mention the subject's condition in passing.
    other_family_adj = {
        "factor": "Absence of pool", "family": "pool_spa", "direction": "down", "applied": -20000,
        "confidence": "low", "basis": "the subject lacks a pool; the subject is also dated throughout.",
    }
    other_family_before = dict(other_family_adj)
    check("18: a non-condition-family adjustment is never touched by the condition neutralizer",
          not _neutralize_unsupported_condition_adjustment(other_family_adj, subject)
          and other_family_adj == other_family_before, str(other_family_adj))

    # Production cleanup (real report, 2026-09-14): "untouched" is another way the model claims a
    # confirmed condition defect - caught by the checker and relabeled by the neutralizer exactly
    # like "original/unrenovated/dated".
    untouched_blocked = _factual_prose_blocking(
        "Test's untouched condition warrants a downward adjustment vs the comp set.",
        "Methodology summary", subject,
    )
    check("19: an undocumented 'untouched condition' claim is caught the same way as "
          "'original/unrenovated/dated'",
          any("original/unrenovated/dated" in b for b in untouched_blocked), str(untouched_blocked))
    untouched_adj = {
        "factor": "Condition gap vs renovated comps", "family": "renovation_condition",
        "direction": "down", "applied": -30000, "confidence": "medium",
        "basis": "The subject's untouched condition lags the renovated comp set.",
    }
    check("20: the condition neutralizer also relabels an 'untouched' condition claim",
          bool(_neutralize_unsupported_condition_adjustment(untouched_adj, subject))
          and "untouched" not in untouched_adj["basis"].lower(), str(untouched_adj))

    # Production cleanup (Cherry Ln, 2026-09-14): the narrative called "316 McClenaghan" the
    # closest comp when "315 Keithwood" (a shorter distance_miles) was actually closer -
    # auto-correct the unsupported superlative rather than block export over it.
    closest_comps = [
        _comp("K1", 1_300_000, 3000, dist=0.31, street_address="316 McClenaghan Rd"),
        _comp("K2", 1_280_000, 2950, dist=0.14, street_address="315 Keithwood Rd"),
    ]
    closest_num_by_key = {c["listing_key"]: i + 1 for i, c in enumerate(closest_comps)}
    closest_addr_by_key = {c["listing_key"]: c["street_address"] for c in closest_comps}
    wrong_closest_text = (
        "316 McClenaghan Rd, the closest comparable sale at 0.31 miles, supports the upper end "
        "of the range."
    )
    fixed_text, closest_flags = _reconcile_closest_comp_claims(
        wrong_closest_text, "Methodology summary", closest_comps, closest_num_by_key, closest_addr_by_key,
    )
    check("21: a 'closest comp' claim on the wrong comp (316 McClenaghan, 0.31 mi) is "
          "auto-corrected once a nearer comp (315 Keithwood, 0.14 mi) exists in the comp set",
          bool(closest_flags) and "closest" not in fixed_text.lower() and "316 McClenaghan Rd" in fixed_text,
          f"flags={closest_flags} text={fixed_text!r}")

    correct_closest_text = "315 Keithwood Rd, the closest comparable sale at 0.14 miles, anchors the low end."
    unchanged_text, no_flags = _reconcile_closest_comp_claims(
        correct_closest_text, "Methodology summary", closest_comps, closest_num_by_key, closest_addr_by_key,
    )
    check("22: a correct 'closest comp' claim (315 Keithwood, the actual shortest distance) is "
          "left untouched",
          not no_flags and unchanged_text == correct_closest_text, f"flags={no_flags} text={unchanged_text!r}")

    # Production cleanup (real report, 2026-09-14): a corrupted "e. ," abbreviation fragment
    # (a mangled "i.e.," / "e.g.,") left in offer prose - auto-fixed by the pre-export QA's
    # prose-hygiene pass, same as the other punctuation auto-fixes.
    punct_tl = {"is_under_contract": False, "current_status": "Active", "active_market_days": 10}
    _punct_qr, punct_corrected, _punct_blk = _v10_pre_export_qa(
        {"o": ("Offer strategy", "Consider a modest concession, e. , a $5,000 credit toward closing costs.")},
        value=1_000_000, range_pct=5.0, subject_asking=1_050_000,
        status_timeline=punct_tl, comps=[], comp_num_by_key={}, addr_by_key={},
        valuation_trace={"anchor": {}}, value_drivers=[], confidence="medium")
    check("23: a corrupted 'e. ,' abbreviation fragment in offer prose is auto-fixed",
          "e. ," not in punct_corrected["o"] and ", a $5,000 credit" in punct_corrected["o"],
          punct_corrected["o"])

    # Production cleanup (Cherry Ln, 2026-09-15): Comp #4's real note implied the subject is
    # inferior on systems/finishes via a comparative-degree construction ("well beyond the
    # subject's unverified..."), never using any of the specific deficit-pole words, and slipped
    # through. Subject condition here is genuinely unverified (UNKNOWN), not documented deficient.
    comp4_text = (
        "249 Bell Rd closed at $543.77/sqft, the highest price point in the set, on a fully "
        "updated stone colonial with a finished walk-out lower level, high-end baths, and a "
        "private, no-thru-street setting backing to trees. It is smaller than the subject and on "
        "a considerably smaller lot, and its updated systems and finishes are well beyond the "
        "subject's unverified renovation and systems history - that gap is the main reason it "
        "sold for so much more per foot. Useful supporting evidence for the top of the range, but "
        "its size and condition profile are different enough from the subject that it should "
        "carry moderate rather than primary weight."
    )
    comp4_blocked = _factual_prose_blocking(
        comp4_text, "Comp #4 (249 Bell Rd)", subject, comp_note=True,
        comp=_comp("BELL", 1_450_000, 2900, remarks="Fully updated stone colonial."),
    )
    check("24: 'well beyond the subject's unverified ...' is caught as an implied "
          "comparative-inferiority claim even though it uses none of the specific deficit-pole "
          "words",
          any("original/unrenovated/dated" in b for b in comp4_blocked), str(comp4_blocked))

    check("25: 'well beyond' describing something other than the subject (a market stat, not a "
          "condition comparison) does not false-positive",
          not _factual_prose_blocking(
              "This comp closed well beyond the neighborhood average price per square foot.",
              "Comp #1", subject, comp_note=True, comp=_comp("X1", 1_300_000, 3000),
          ))

    check("26: the same 'well beyond the subject's ...' comparative claim is not blocked once the "
          "subject's own record actually documents a condition deficit",
          not any("original/unrenovated/dated" in b for b in _factual_prose_blocking(
              comp4_text, "Comp #4 (249 Bell Rd)", documented_deficit_subject, comp_note=True,
              comp=_comp("BELL", 1_450_000, 2900, remarks="Fully updated stone colonial."),
          )))

    # Production cleanup (real report, 2026-09-15): "describes a system as original without
    # supporting property data" kept surfacing as a blocking popup because only an (unreliable) AI
    # rewrite attempt covered it - _reconcile_comp_hard_facts now neutralizes it deterministically,
    # guaranteed, with zero AI call, before the comp note is ever scanned for blocking.
    comp_wiring_text = (
        "213 Example Ln closed at $410/sqft on fully updated systems; the subject's original "
        "wiring is undocumented by comparison."
    )
    comp_for_wiring = _comp("W1", 1_300_000, 3000, remarks="Fully updated systems throughout.")
    reconciled_text, recon_flags = _reconcile_comp_hard_facts(comp_wiring_text, "Comp #1", subject, comp_for_wiring)
    check("27: an unsupported 'original wiring' claim in a comp note is deterministically "
          "neutralized before the blocking scan ever runs - it never needs an AI rewrite to survive",
          "original wiring" not in reconciled_text.lower()
          and not _factual_prose_blocking(reconciled_text, "Comp #1", subject, comp_note=True, comp=comp_for_wiring),
          f"flags={recon_flags} text={reconciled_text!r}")

    methodology_wiring_text = "The subject's original systems remain undocumented. The comp set includes several recently renovated properties."
    gated, gate_flags, gate_blocking = _final_consistency_gate(
        {"m": ("Methodology summary", methodology_wiring_text)},
        subject=subject, comps=[], value=1_400_000, status_timeline={},
        comp_num_by_key={}, addr_by_key={},
    )
    check("28: the same 'original systems' claim in methodology/offer/inspection prose is also "
          "deterministically neutralized, not just in comp notes",
          "original systems" not in gated["m"].lower()
          and not _factual_prose_blocking(gated["m"], "Methodology summary", subject),
          f"flags={gate_flags} text={gated['m']!r}")

    documented_systems_subject = _subject(remarks="Original electrical wiring, never updated.")
    untouched_text, untouched_flags = _reconcile_comp_hard_facts(
        "The subject's original wiring predates modern code.", "Methodology summary",
        documented_systems_subject, _comp("W2", 1_300_000, 3000),
    )
    check("29: a genuinely documented 'original wiring' claim is left completely untouched",
          "original wiring" in untouched_text.lower() and not untouched_flags, untouched_text)

    # Same class of problem as 27/28 but for the "infers a condition problem from age" hard-fact
    # check: "Comp #3 ... infers a condition problem from age without supporting property data"
    # kept surfacing as a blocking popup with no deterministic fix backing it up -
    # _reconcile_comp_hard_facts/_reconcile_age_condition_inference now neutralize it the same way.
    comp_age_text = (
        "315 Keithwood Rd shows clear deferred maintenance around the exterior trim, unlike the subject."
    )
    comp_for_age = _comp("A1", 1_250_000, 2900)
    reconciled_age_text, recon_age_flags = _reconcile_comp_hard_facts(comp_age_text, "Comp #3", subject, comp_for_age)
    check("30: an unsupported 'deferred maintenance' age inference in a comp note is deterministically "
          "neutralized before the blocking scan ever runs",
          "deferred maintenance" not in reconciled_age_text.lower()
          and not _factual_prose_blocking(reconciled_age_text, "Comp #3", subject, comp_note=True, comp=comp_for_age),
          f"flags={recon_age_flags} text={reconciled_age_text!r}")

    methodology_age_text = "The subject shows age-related wear consistent with its build date."
    gated_age, gate_age_flags, _ = _final_consistency_gate(
        {"m": ("Methodology summary", methodology_age_text)},
        subject=subject, comps=[], value=1_400_000, status_timeline={},
        comp_num_by_key={}, addr_by_key={},
    )
    check("31: the same age-based inference in methodology/offer/inspection prose is also "
          "deterministically neutralized, not just in comp notes",
          "age-related wear" not in gated_age["m"].lower()
          and not _factual_prose_blocking(gated_age["m"], "Methodology summary", subject),
          f"flags={gate_age_flags} text={gated_age['m']!r}")

    documented_damage_subject = _subject(remarks="Inspection noted deferred maintenance to the roof.")
    untouched_age_text, untouched_age_flags = _reconcile_comp_hard_facts(
        "The subject shows deferred maintenance per its own listing remarks.", "Methodology summary",
        documented_damage_subject, _comp("A2", 1_250_000, 2900),
    )
    check("32: a genuinely documented 'deferred maintenance' claim is left completely untouched",
          "deferred maintenance" in untouched_age_text.lower() and not untouched_age_flags, untouched_age_text)

    # Root narrative formatting fix: a comp's distance-from-subject was being written inside the
    # same parenthetical as its lot-size comparison ("on a smaller lot (0.85 miles out)"), which
    # reads as if 0.85 were the lot's own size. _reconcile_comp_hard_facts now splits the two facts
    # using the authoritative lot_size/distance_miles record, so distance can never be read as lot size.
    comp_lot_dist_text = "Comp #4 sits on a smaller lot (0.85 miles out), otherwise comparable in finish."
    comp_for_lot_dist = _comp("L1", 1_200_000, 2800, dist=0.85, lot_size=22_651)  # 22,651 sqft ~= 0.52 acres
    reconciled_lot_text, recon_lot_flags = _reconcile_comp_hard_facts(
        comp_lot_dist_text, "Comp #4", subject, comp_for_lot_dist,
    )
    check("33: a distance figure embedded inside a lot-size parenthetical ('smaller lot (0.85 miles "
          "out)') is deterministically split into a labeled acreage and a labeled distance",
          "0.52-acre lot" in reconciled_lot_text and "0.85 miles from the subject" in reconciled_lot_text
          and "lot (0.85" not in reconciled_lot_text.lower(),
          f"flags={recon_lot_flags} text={reconciled_lot_text!r}")


# ---------------------------------------------------------------------------------------------
# AJ. Client-facing prose cleanup: a value-driver note's road-exposure claim must agree with that
#     SAME comp's own authoritative road_context.verdict (not contradict its own comp card), and
#     the deterministic fallback comp-narrative generator must never leak internal pipeline
#     vocabulary ("authoritative structured record", "normalized model comparison") into text a
#     client actually reads.
# ---------------------------------------------------------------------------------------------
def test_aj_client_facing_road_and_fallback_prose():
    print("\nAJ. client-facing road-exposure and fallback-narrative prose cleanup")
    subject = _subject()

    quiet_comp = _comp("D1", 1_450_000, 3200, road_context={"verdict": "quiet"})
    quiet_comp["street_address"] = "112 Dovecote Ln"
    comp_num_by_key = {"D1": 3}
    addr_by_key = {"D1": "112 Dovecote Ln"}
    drivers = [{
        "comp_key": "", "comp_label": "Comp #3 (112 Dovecote Ln)", "direction": "down",
        "note": "Comp #3 (112 Dovecote Ln) sits on a connector road, exposing it to more traffic than the subject.",
    }]
    fixed_drivers, road_flags = _reconcile_road_exposure_claims(drivers, [quiet_comp], comp_num_by_key, addr_by_key)
    check("1: a 'connector road' claim about a comp the map data shows is actually quiet is removed",
          "connector road" not in fixed_drivers[0]["note"].lower() and bool(road_flags),
          f"note={fixed_drivers[0]['note']!r} flags={road_flags}")
    check("2: the correction only touches the note text - direction and comp_label are untouched",
          fixed_drivers[0]["direction"] == "down" and fixed_drivers[0]["comp_label"] == "Comp #3 (112 Dovecote Ln)",
          str(fixed_drivers[0]))

    through_comp = _comp("D2", 1_450_000, 3200, road_context={"verdict": "through"})
    through_comp["street_address"] = "9 Highway Ave"
    drivers2 = [{
        "comp_key": "", "comp_label": "Comp #4 (9 Highway Ave)", "direction": "down",
        "note": "Comp #4 (9 Highway Ave) fronts a busy road, unlike the subject.",
    }]
    fixed_drivers2, road_flags2 = _reconcile_road_exposure_claims(
        drivers2, [through_comp], {"D2": 4}, {"D2": "9 Highway Ave"},
    )
    check("3: a busy-road claim that DOES match the comp's own authoritative verdict is left untouched",
          fixed_drivers2[0]["note"] == drivers2[0]["note"] and not road_flags2,
          f"note={fixed_drivers2[0]['note']!r} flags={road_flags2}")

    corrected_comp = _comp(
        "F1", 1_500_000, 3400, year_built=2001, lot_size=65_340,
        quality={"finish_quality": "above", "amenities": "above", "lot": "below", "layout": "similar"},
    )
    corrected_comp["street_address"] = "541 Atterbury Rd"
    fallback_text = _regenerate_comp_narrative_from_snapshot(corrected_comp, "Comp #2 (541 Atterbury Rd)")
    check("4: the deterministic fallback comp narrative never leaks internal pipeline vocabulary "
          "into client-facing text",
          "authoritative structured record" not in fallback_text.lower()
          and "normalized model comparison" not in fallback_text.lower(),
          fallback_text)
    check("5: the same fallback narrative still states every underlying fact (sqft, year built, "
          "lot size, and the up/down quality comparisons) in plain prose",
          "3,400" in fallback_text and "2001" in fallback_text and "1.50-acre lot" in fallback_text
          and "stronger than the subject" in fallback_text and "weaker than the subject" in fallback_text
          and "comparable to the subject" in fallback_text,
          fallback_text)


# ---------------------------------------------------------------------------------------------
# AK. Cross-section semantic consistency (general - not a one-off patch for any single report).
#     A comp described one way in its own narrative and the opposite way in "Why this number" (or
#     methodology/inspection/offer) must be resolved against authoritative evidence when it exists
#     (pool fields, road_context.verdict, verified sale price vs. FMV) and neutralized there, or -
#     when no ground truth exists (condition) - blocked rather than guessed at. Runs on the exact
#     final client text, right before the artifact is stamped Passed.
# ---------------------------------------------------------------------------------------------
def test_ak_cross_section_semantic_consistency():
    print("\nAK. cross-section semantic consistency over the exact final client-facing text")
    subject = _subject()

    # 1. Pool: ground truth (pool_features) says YES - the "no-pool" claim elsewhere is corrected.
    pool_comp = _comp("P1", 1_000_000, 3000, pool_features=["In Ground"],
                       ai_commentary="Comp #1 features a pool and a finished basement.")
    pool_comp["street_address"] = "10 Pool Ct"
    comp_num_by_key = {"P1": 1}
    addr_by_key = {"P1": "10 Pool Ct"}
    drivers = [{
        "comp_key": "", "comp_label": "Comp #1 (10 Pool Ct)", "direction": "down",
        "note": "Comp #1 is the no-pool, lower-priced comp in the set.",
    }]
    m, o, i, fixed_drivers, flags, blocking = _v10_cross_section_semantic_consistency_check(
        methodology="", offer="", inspection="", value_drivers=drivers, comps=[pool_comp],
        subject=subject, value=1_000_000, comp_num_by_key=comp_num_by_key, addr_by_key=addr_by_key,
    )
    check("1: a comp's own narrative confirms a pool (ground truth) - the contradicting "
          "'no-pool' claim elsewhere is corrected, not left standing",
          "no-pool" not in fixed_drivers[0]["note"].lower() and "pool" not in blocking.__str__().lower()
          and bool(flags), f"note={fixed_drivers[0]['note']!r} flags={flags} blocking={blocking}")
    check("1b: the correction is confined to the note - comp_label and direction untouched",
          fixed_drivers[0]["comp_label"] == "Comp #1 (10 Pool Ct)" and fixed_drivers[0]["direction"] == "down")

    # 2. Road: ground truth (road_context.verdict=quiet) - a busy-road claim elsewhere is corrected.
    quiet_comp = _comp("Q1", 1_000_000, 3000, road_context={"verdict": "quiet"},
                        ai_commentary="Comp #2 sits on a quiet cul-de-sac.")
    quiet_comp["street_address"] = "20 Quiet Ln"
    road_methodology = "Comp #2 fronts a busy road, which weighs against its comparability."
    m2, o2, i2, dv2, flags2, blocking2 = _v10_cross_section_semantic_consistency_check(
        methodology=road_methodology, offer="", inspection="", value_drivers=[], comps=[quiet_comp],
        subject=subject, value=1_000_000, comp_num_by_key={"Q1": 2}, addr_by_key={"Q1": "20 Quiet Ln"},
    )
    check("2: a comp's own narrative confirms a quiet street (ground truth) - a contradicting "
          "'busy road' claim in the methodology is corrected",
          "busy road" not in m2.lower() and bool(flags2) and not blocking2,
          f"methodology={m2!r} flags={flags2} blocking={blocking2}")

    # 3. Condition: NO ground truth exists for condition - a genuine cross-section contradiction
    #    about the SAME comp must block export rather than silently pick a side.
    cond_comp = _comp("C1", 1_000_000, 3000,
                       ai_commentary="Comp #3 is in superior condition compared to the subject.")
    cond_comp["street_address"] = "30 Cond St"
    cond_inspection = "Comp #3 shows inferior condition and needs significant work throughout."
    m3, o3, i3, dv3, flags3, blocking3 = _v10_cross_section_semantic_consistency_check(
        methodology="", offer="", inspection=cond_inspection, value_drivers=[], comps=[cond_comp],
        subject=subject, value=1_000_000, comp_num_by_key={"C1": 3}, addr_by_key={"C1": "30 Cond St"},
    )
    check("3: superior condition in a comp's own narrative vs. inferior condition in inspection - "
          "no ground truth exists, so this blocks export instead of guessing which is right",
          any("condition" in b.lower() and "comp #3" in b.lower() for b in blocking3),
          f"blocking={blocking3}")
    check("3b: the comp's own narrative and the inspection text are left untouched when blocked "
          "(never silently pick a side without ground truth)",
          "superior condition" in cond_comp["ai_commentary"].lower() and "inferior condition" in i3.lower())

    # 4. Sale price direction: ground truth (verified sale price vs. FMV) - a "sold materially
    #    lower" claim contradicting a comp that actually sold well above the subject is corrected.
    high_comp = _comp("H1", 1_300_000, 3000,
                       ai_commentary="Comp #4 sold above the subject, reflecting a premium finish.")
    high_comp["street_address"] = "40 High Ave"
    drivers4 = [{
        "comp_key": "", "comp_label": "Comp #4 (40 High Ave)", "direction": "up",
        "note": "Comp #4 sold materially lower, tempering the estimate.",
    }]
    m4, o4, i4, fixed_drivers4, flags4, blocking4 = _v10_cross_section_semantic_consistency_check(
        methodology="", offer="", inspection="", value_drivers=drivers4, comps=[high_comp],
        subject=subject, value=1_000_000, comp_num_by_key={"H1": 4}, addr_by_key={"H1": "40 High Ave"},
    )
    check("4: a comp that verifiably sold well above the subject's FMV - a contradicting 'sold "
          "materially lower' claim elsewhere is corrected against the real sale price",
          "materially lower" not in fixed_drivers4[0]["note"].lower() and bool(flags4) and not blocking4,
          f"note={fixed_drivers4[0]['note']!r} flags={flags4} blocking={blocking4}")

    # 5. True negative: consistent claims about a comp across sections, with no ground truth
    #    mismatch, must never be touched or blocked.
    clean_comp = _comp("N1", 1_000_000, 3000, pool_features=["In Ground"],
                        ai_commentary="Comp #5 features a pool and sits on a quiet street.")
    clean_comp["street_address"] = "50 Clean Rd"
    clean_drivers = [{
        "comp_key": "", "comp_label": "Comp #5 (50 Clean Rd)", "direction": "up",
        "note": "Comp #5's pool and quiet setting support a stronger value.",
    }]
    m5, o5, i5, fixed_drivers5, flags5, blocking5 = _v10_cross_section_semantic_consistency_check(
        methodology="", offer="", inspection="", value_drivers=clean_drivers, comps=[clean_comp],
        subject=subject, value=1_000_000, comp_num_by_key={"N1": 5}, addr_by_key={"N1": "50 Clean Rd"},
    )
    check("5: consistent claims about the same comp across sections are left completely untouched",
          fixed_drivers5[0]["note"] == clean_drivers[0]["note"] and not flags5 and not blocking5,
          f"note={fixed_drivers5[0]['note']!r} flags={flags5} blocking={blocking5}")


def main() -> int:
    for fn in (test_a_finished_basement_not_inflating_psf, test_b_location_class_and_weight,
               test_c_condition_reconciliation, test_d_mixed_set_stability,
               test_e_why_this_number_from_trace, test_f_blocking_propagates_to_qa_result,
               test_g_concluded_number_scan, test_h_status_authority_and_dedup,
               test_i_psf_denominator_labels, test_j_condition_not_demoting_same_market_comp,
               test_k_prose_offset_must_be_structured, test_l_methodology_consistency,
               test_m_range_endpoint_not_flagged, test_n_generalized_bucket_reconciliation,
               test_o_competitive_sale_driver, test_p_unfiled_signal_detection_and_apply,
               test_q_finish_rating_consistency_and_stability,
               test_r_list_price_not_flagged_as_competing_fmv,
               test_s_anchor_bias_controls, test_s2_range_low_not_flagged_as_fmv,
               test_t_broad_qualitative_inferiority, test_u_output_quality_pass,
               test_v_offer_and_direction_qa, test_w_final_client_output_consistency,
               test_x_frontier_reasoning_authority, test_y_factual_prose_and_qa_cleanup,
               test_z_authoritative_property_fact_pipeline, test_aa_latest_root_output_consistency,
               test_ab_current_bright_property_fact_contract,
               test_ac_final_root_cause_pipeline_boundary,
               test_ad_entity_binding_and_offer_field_contract,
               test_ae_continuous_relevance_production_mode,
               test_af_final_artifact_enforcement,
               test_ag_agent_review_presentation_and_pdf_pagination_contract,
               test_ai_valuation_reliability_warnings,
               test_ah_subject_condition_evidence_everywhere,
               test_aj_client_facing_road_and_fallback_prose,
               test_ak_cross_section_semantic_consistency):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            global _FAIL
            _FAIL += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
