"""Offline checks for the 10/20 CMA fixes from the graded 417 Dogwood Ln report - no DB, no AI.

- Style / levels is its own Match % component; split-levels are not ranches.
- The anchor's feature grid prices finished lower level, baths, garage and a cesspool.
- Area "Same Market" needs the township to match too.
"""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as ai  # noqa: E402

RANCH = {"architectural_style": ["Ranch/Rambler"], "levels": ["1"]}


def test_level_classes_prefer_style_over_level_count():
    assert ai._relevance_level_class({"architectural_style": ["Ranch/Rambler"], "levels": ["2"]}) == "single"
    assert ai._relevance_level_class({"architectural_style": ["Split Level"], "levels": ["3"]}) == "split"
    assert ai._relevance_level_class({"architectural_style": ["Raised Ranch/Rambler"]}) == "split"
    assert ai._relevance_level_class({"architectural_style": ["Cape Cod"], "levels": ["2"]}) == "one_half"
    assert ai._relevance_level_class({"architectural_style": ["Contemporary"], "levels": ["2"]}) == "multi"
    assert ai._relevance_level_class({"architectural_style": ["Other"], "levels": ["1"]}) == "single"
    assert ai._relevance_level_class({}) is None


def test_ranch_buyer_ranks_ranches_first():
    ranch = ai._relevance_style_score(RANCH, {"architectural_style": ["Ranch/Rambler"], "levels": ["1"]})
    cape = ai._relevance_style_score(RANCH, {"architectural_style": ["Cape Cod"], "levels": ["1.5"]})
    split = ai._relevance_style_score(RANCH, {"architectural_style": ["Split Level"], "levels": ["3"]})
    colonial = ai._relevance_style_score(RANCH, {"architectural_style": ["Colonial"], "levels": ["2"]})
    assert ranch == 1.0
    assert ranch > cape > split > colonial
    assert colonial <= 0.2


def test_split_level_is_not_in_the_ranch_family():
    assert ai._relevance_style_family("Split Level") == "split"
    assert ai._relevance_style_family(["Ranch/Rambler"]) == "ranch"
    assert ai._relevance_style_family("Other") is None


def test_style_is_a_real_part_of_the_match_score():
    subject = {**RANCH, "listing_key": "s", "year_built": 1972}
    base = {"listing_key": "x", "distance_miles": 1.0, "year_built": 1972, "status": "sold"}
    rel = ai._continuous_relevance(subject, [
        {**base, "listing_key": "ranch", "architectural_style": ["Ranch/Rambler"], "levels": ["1"]},
        {**base, "listing_key": "colonial", "architectural_style": ["Colonial"], "levels": ["2"]},
    ], density_context={"distance_half_life_miles": 2.0})
    rows = {r["listing_key"]: r for r in rel["comps"]}
    assert rows["ranch"]["base_relevance_score"] - rows["colonial"]["base_relevance_score"] > 0.05


SUBJECT = {"below_grade_finished_area": 1300, "basement": "Full, Fully Finished", "baths": 3.5,
           "garage": "2 Car", "sewer": "Public Sewer"}


def _row(comp, price, ag):
    bg, est = ai._v10_bg_for_grid(comp, ag)
    return {"_c": comp, "price": price, "ag": ag, "bg": bg, "bg_estimated": est}


def test_grid_prices_size_lower_level_baths_and_garage_at_one_rate():
    comp = {"below_grade_finished_area": None, "basement": "Full", "baths": 2, "garage": "1 Car", "sewer": "On Site Septic"}
    fa = ai._v10_grid_adjustments({**SUBJECT, "above_grade_finished_area": 1830}, _row(comp, 420000, 1710), 1830, 1300, False, 290.0)
    assert fa["size"] > 0 and fa["below_grade"] > 0 and fa["baths"] > 0 and fa["garage"] > 0
    assert fa["below_grade_rate_psf"] == round(290.0 * ai._V10_GRID_BG_FRAC, 2)
    assert fa["sewer"] == 0
    assert fa["total"] == fa["size"] + fa["below_grade"] + fa["baths"] + fa["garage"]


def test_smaller_comp_is_always_adjusted_up_and_larger_down():
    small = ai._v10_grid_adjustments(SUBJECT, _row({"basement": "None"}, 450000, 1472), 1830, 1300, False, 290.0)
    big = ai._v10_grid_adjustments(SUBJECT, _row({"basement": "None"}, 450000, 2400), 1830, 1300, False, 290.0)
    assert small["size"] > 0 and big["size"] < 0


def test_unreported_partial_basement_is_estimated_not_ignored():
    comp = {"below_grade_finished_area": 0, "basement": "Partially Finished", "baths": 3.5, "garage": "2 Car", "sewer": "Public Sewer"}
    assert ai._v10_bg_for_grid(comp, 1819) == (455, True)
    fa = ai._v10_grid_adjustments(SUBJECT, _row(comp, 410000, 1819), 1830, 1300, False, 290.0)
    assert fa["below_grade"] > 0 and any("estimated" in n for n in fa["notes"])


def test_cesspool_comp_is_adjusted_up_toward_a_sewer_subject():
    comp = {"basement": "None", "baths": 3.5, "garage": "2 Car", "sewer": "Cess Pool"}
    fa = ai._v10_grid_adjustments({**SUBJECT, "below_grade_finished_area": 0, "basement": "None"}, _row(comp, 410000, 1830), 1830, 0, False, 290.0)
    assert fa["sewer"] == round(min(ai._V10_FEATURE_CESSPOOL_PCT * 410000, ai._V10_FEATURE_CESSPOOL_MAX))
    assert ai._material_system_issues(comp) == ["cesspool (sewer)"]


def test_weights_cap_heavy_and_dominant_comps():
    shares = ai._v10_capped_weights([(0.8, 0.10), (0.7, 0.12), (0.2, 0.36), (0.75, 0.14), (0.78, 0.11)])
    assert abs(sum(shares) - 1) < 1e-9
    assert shares[2] <= ai._V10_GRID_HEAVY_CAP + 1e-9
    assert max(shares) <= ai._V10_GRID_MAX_SHARE + 1e-9


def test_dogwood_style_set_value_is_a_true_weighted_average():
    subject = {**SUBJECT, "above_grade_finished_area": 1830, "sqft": 3130, "property_type": "SINGLE_FAMILY",
               "architectural_style": ["Ranch/Rambler"], "latitude": 39.98, "longitude": -75.79}
    comps = []
    for i, (price, ag, bg, basement, baths, garage, sewer, dist) in enumerate([
        (420000, 1710, None, "Full", 2, "1 Car", "On Site Septic", 1.48),
        (452000, 1472, 0, "Fully Finished, Outside Entrance", 3, "2 Car", "On Site Septic", 0.27),
        (443000, 1504, 0, "Full, Outside Entrance", 3, "2 Car", "On Site Septic", 1.14),
        (410000, 1819, 0, "Partially Finished", 1.5, "2 Car", "Cess Pool", 1.96),
        (340000, 884, 884, "Full, Fully Finished", 2, "None", "Public Sewer", 0.11),
    ]):
        comps.append({"listing_key": f"k{i}", "street_address": f"{i} TEST RD", "status": "sold", "sale_price": price,
                      "close_price": price, "price_verified": True, "list_price": price, "sqft": ag + (bg or 0),
                      "above_grade_finished_area": ag, "below_grade_finished_area": bg, "basement": basement,
                      "baths": baths, "garage": garage, "sewer": sewer, "distance_miles": dist, "property_type": "SINGLE_FAMILY",
                      "architectural_style": ["Ranch/Rambler"], "close_date": "2026-09-01", "strength": "strong"})
    _, a, _ = ai._apply_continuous_relevance_weighting(subject, comps, {}, {})
    adjusted = [c["size_adjusted_price"] for c in a["comps"]]
    assert min(adjusted) <= a["anchor_value"] <= max(adjusted)
    assert abs(sum(c["normalized_weight"] for c in a["comps"]) - 1) < 0.01
    for c in a["comps"]:
        sa = c["above_grade_sqft"] or c["sqft"]
        if sa < 1830 - 25:
            assert c["size_adjustment"] > 0, c["label"]
def test_same_market_needs_the_same_township():
    subject = {"school_district": "Coatesville Area", "city": "COATESVILLE", "township": "CALN"}
    same = {"school_district": "Coatesville Area", "city": "COATESVILLE", "township": "CALN"}
    borough = {"school_district": "Coatesville Area", "city": "COATESVILLE", "township": "SOUTH COATESVILLE"}
    assert ai._derive_quality_from_data(subject, same)["area"] == "same_as_subject"
    assert ai._derive_quality_from_data(subject, borough)["area"] == "similar_to_subject"


def test_set_back_house_is_not_rated_as_fronting_the_highway():
    comp = {"street_address": "2702 E KINGS HWY", "public_remarks": "Tucked back from the road on a private drive."}
    assert ai._listing_says_set_back(comp)
    assert ai._street_tier(comp) == "neutral"
    ctx = ai._setback_adjusted_road_context(comp, {"verdict": "through", "summary": "fronts an arterial"})
    assert ctx["verdict"] == "neutral" and ctx["setback_from_listing"]
    assert ai._street_tier({"street_address": "2702 E KINGS HWY"}) == "through"


def test_listing_red_flags_ignore_negations():
    issues = ai._material_system_issues({"sewer": "Cess Pool", "public_remarks": "Sold as-is. Not in a flood zone. No water damage."})
    assert "cesspool (sewer)" in issues
    assert "listing says: sold as-is" in issues
    assert not any("flood" in i or "damage" in i for i in issues)
    assert ai._material_system_issues({"public_remarks": "Handyman special in a flood zone"}) == [
        "listing says: flood zone", "listing says: needs major work"]


def test_hot_market_opening_offer_is_near_asking():
    trace = {"anchor": {"comp_list_to_sale_median": 1.10}, "exposure": {"state": "normal_exposure", "active_market_days": 14},
             "asking_price_check": {"passed_over": False}}
    band = ai._v10_build_buyer_offer_band(500000, 500000, trace=trace, confidence="low")
    assert band["hot_market"] is True
    assert band["opening_low"] >= 485000 and band["opening_high"] >= 495000
    assert band["opening_pct_phrase"] == "at to about 3% below"


def test_unmatched_lower_level_is_stated_as_the_main_uncertainty():
    rows = [{"_c": {"below_grade_finished_area": 0, "basement": "None"}, "price": 420000, "size_only_adj_price": 436000,
             "feat": {"below_grade": 60000, "baths": 9000, "garage": 4000, "sewer": 0, "below_grade_rate_psf": 61.0}}]
    out = ai._anchor_main_uncertainties(SUBJECT, rows, 1)
    assert any("lower level" in u for u in out)
    assert any("Only 1 verified sale" in u for u in out)


def test_size_ratio_claims_must_match_the_records():
    comp = {"above_grade_finished_area": 1472, "sqft": 1472}
    subject = {"above_grade_finished_area": 1830, "sqft": 3130}
    bad = "Its above-grade area is under half the subject's, so its $/sqft runs high."
    assert ai._size_ratio_claim_errors(bad, comp, subject)
    small = {"above_grade_finished_area": 800, "sqft": 800}
    assert not ai._size_ratio_claim_errors(bad, small, subject)
    assert not ai._size_ratio_claim_errors("It sits on half an acre.", comp, subject)


def test_subject_outpaced_by_fast_over_ask_sales_is_not_a_bidding_war():
    trace = {"anchor": {"comp_list_to_sale_median": 1.065, "comps": [
                {"sale_to_list": 1.16, "dom": 6}, {"sale_to_list": 1.14, "dom": 6}, {"sale_to_list": 0.96, "dom": 28}]},
             "exposure": {"state": "normal_exposure", "active_market_days": 14},
             "asking_price_check": {"passed_over": False}}
    band = ai._v10_build_buyer_offer_band(510000, 500000, trace=trace, confidence="medium")
    assert band["outpaced_by_comps"] is True and band["hot_market"] is False
    assert band["comfort_ceiling"] <= 510000


def test_engine_self_checks_pass_on_a_dogwood_style_set_and_catch_a_wrong_way_size():
    good = {"anchor_value": 490000, "comps": [
        {"label": "Comp #1", "size_adjusted_price": 512000, "normalized_weight": 0.5, "gross_adjustment_pct": 13, "above_grade_sqft": 1472, "size_adjustment": 36000},
        {"label": "Comp #2", "size_adjusted_price": 470000, "normalized_weight": 0.5, "gross_adjustment_pct": 15, "above_grade_sqft": 1710, "size_adjustment": 12000}]}
    assert ai._anchor_integrity_errors(good, {"above_grade_finished_area": 1830}) == []
    bad = {**good, "comps": [{**good["comps"][0], "size_adjustment": -806}, good["comps"][1]]}
    assert any("against the size difference" in e for e in ai._anchor_integrity_errors(bad, {"above_grade_finished_area": 1830}))
    outside = {**good, "anchor_value": 521173}
    assert any("outside the adjusted comps" in e for e in ai._anchor_integrity_errors(outside, {"above_grade_finished_area": 1830}))
