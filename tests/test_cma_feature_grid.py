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


def test_subject_lower_level_baths_and_garage_are_priced():
    comp = {"below_grade_finished_area": None, "basement": "Full", "baths": 2, "garage": "1 Car", "sewer": "On Site Septic"}
    fa = ai._v10_feature_adjustments(SUBJECT, comp, 420000, 245.6)
    assert fa["below_grade"] > 0 and fa["baths"] > 0 and fa["garage"] > 0
    assert fa["sewer"] == 0
    assert abs(fa["total"]) <= ai._V10_FEATURE_MAX_PCT * 420000 + 1


def test_unreported_partial_basement_is_estimated_not_ignored():
    comp = {"below_grade_finished_area": 0, "basement": "Partially Finished", "above_grade_finished_area": 1819,
            "sqft": 1819, "baths": 3.5, "garage": "2 Car", "sewer": "Public Sewer"}
    fa = ai._v10_feature_adjustments(SUBJECT, comp, 410000, 225.4)
    assert fa["below_grade"] > 0
    assert any("estimated" in n for n in fa["notes"])


def test_cesspool_comp_is_adjusted_up_toward_a_sewer_subject():
    comp = {"basement": "None", "baths": 3.5, "garage": "2 Car", "sewer": "Cess Pool"}
    fa = ai._v10_feature_adjustments({**SUBJECT, "below_grade_finished_area": 0, "basement": "None"}, comp, 410000, 225.0)
    assert fa["sewer"] == round(min(ai._V10_FEATURE_CESSPOOL_PCT * 410000, ai._V10_FEATURE_CESSPOOL_MAX))
    assert ai._material_system_issues(comp) == ["cesspool (sewer)"]


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
