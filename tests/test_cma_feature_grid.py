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
