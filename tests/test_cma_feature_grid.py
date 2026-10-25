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


def test_outpaced_subject_is_not_valued_at_an_over_asking_premium():
    packets = [{"sale_price": 420000, "sold_list_ratio": 1.138, "dom": 6}, {"sale_price": 452000, "sold_list_ratio": 1.159, "dom": 6},
               {"sale_price": 410000, "sold_list_ratio": 1.065, "dom": 42}, {"sale_price": 443000, "sold_list_ratio": 0.963, "dom": 28},
               {"sale_price": 340000, "sold_list_ratio": 0.983, "dom": 40}]
    chk = ai._asking_price_market_check({"current_list_price": 500000, "dom": 14}, {}, {"state": "normal_exposure", "comp_median_dom": 28}, packets)
    assert chk["outpaced_by_over_ask_sales"] is True
    assert chk["expected_sale_price"] == 500000
    fresh = ai._asking_price_market_check({"current_list_price": 500000, "dom": 4}, {}, {"state": "normal_exposure", "comp_median_dom": 28}, packets)
    assert fresh["outpaced_by_over_ask_sales"] is False and fresh["expected_sale_price"] > 500000


def test_offer_prose_rebuild_is_idempotent():
    trace = {"anchor": {"comp_list_to_sale_median": 1.065, "comps": [
                {"sale_to_list": 1.16, "dom": 6}, {"sale_to_list": 1.14, "dom": 6}]},
             "exposure": {"state": "normal_exposure", "active_market_days": 14}, "asking_price_check": {"passed_over": False}}
    band = ai._v10_build_buyer_offer_band(525000, 500000, trace=trace, confidence="medium")
    once, _ = ai._v10_rebuild_buyer_offer_prose("Use a standard inspection contingency.", band, 6)
    twice, _ = ai._v10_rebuild_buyer_offer_prose(once, band, 6)
    thrice, _ = ai._v10_rebuild_buyer_offer_prose(twice, band, 6)
    assert thrice == once
    assert thrice.count("bidding war is unlikely") <= 1


# ---- 417 Dogwood Ln v5 (10/20) ------------------------------------------------------------------

def test_suggested_room_uses_are_not_amenities():
    for remarks in (
        "The clean and versatile basement offers endless possibilities - think home gym, media room, or storage.",
        "A bonus space above the garage offers flexibility for a home office, playroom, exercise room, or hobby area.",
        "The expansive unfinished basement is a blank canvas. Create the family room, home gym, office or workshop you've always wanted.",
    ):
        assert ai._amenity_signals({"public_remarks": remarks}) == set(), remarks
    assert ai._amenity_signals({"public_remarks": "The finished lower level has a home theater and a wine cellar."}) == {
        "home_theater", "wine_cellar"}
    # finish updates keep the plain reading
    assert "updated kitchen" in ai._documented_finish_updates({"public_remarks": "Buyers would love the updated kitchen."})


def test_strong_label_needs_light_adjustment_and_no_issues():
    row = {"normalized_weight": 0.21, "gross_adjustment_pct": 12.0, "location_negatives": []}
    assert ai._v10_role_from_anchor_row(row, {}) == "strong"
    assert ai._v10_role_from_anchor_row({**row, "normalized_weight": 0.12}, {}) == "supporting"
    assert ai._v10_role_from_anchor_row({**row, "gross_adjustment_pct": 16.7}, {}) == "supporting"
    assert ai._v10_role_from_anchor_row({**row, "location_negatives": ["a", "b"]}, {}) == "supporting"
    assert ai._v10_role_from_anchor_row({**row, "location_negatives": ["a", "b"], "location_override": True}, {}) == "strong"
    assert ai._v10_role_from_anchor_row(row, {"sewer": "Cess Pool"}) == "supporting"


def test_offer_prose_drops_little_room_sentence_when_value_is_below_asking():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 456000, "opening_high": 475000,
            "target": 488000, "comfort_ceiling": 490000}
    text = ("Structure the offer with a standard inspection contingency. Given the gap between the indicated "
            "value and list price, there may be limited room for the seller to negotiate down significantly, "
            "so be prepared to move toward the top of this range.")
    out, _ = ai._v10_rebuild_buyer_offer_prose(text, band, 0.04)
    assert "inspection contingency" in out
    assert "limited room" not in out


# ---- 417 Dogwood Ln v6 (10/20) ------------------------------------------------------------------

DOGWOOD_SUBJECT = {"sqft": 3130, "above_grade_finished_area": 1830, "below_grade_finished_area": 1300}


def test_subject_size_in_a_comp_note_is_not_corrected_to_the_comps():
    comp = {"sqft": 1472, "above_grade_finished_area": 1472, "below_grade_finished_area": 0}
    text = "The high rate reflects a small home, not a signal the subject should price that high on its much larger 3,130 sqft total."
    out, _ = ai._reconcile_comp_hard_facts(text, "Comp #2 (1219 SCOTT DR)", DOGWOOD_SUBJECT, comp)
    assert "3,130" in out and "1,472 sqft total" not in out


def test_size_followed_later_by_basement_is_not_a_basement_size():
    comp = {"sqft": 1710, "above_grade_finished_area": 1710, "below_grade_finished_area": None}
    note = "This 1,710 sq ft ranch with a partially finished basement sold quickly."
    assert ai._comp_note_number_errors(note, comp, DOGWOOD_SUBJECT) == []
    assert ai._comp_note_number_errors("It adds 1,710 sq ft of finished basement.", comp, DOGWOOD_SUBJECT)


def test_listed_home_cannot_have_sold_today():
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    subj, flag = ai._drop_impossible_last_sale({"last_sale_date": today, "list_price": 500000, "days_on_market": 14})
    assert subj["last_sale_date"] is None and flag
    subj, flag = ai._drop_impossible_last_sale({"last_sale_date": "2019-05-01", "list_price": 500000, "days_on_market": 14})
    assert subj["last_sale_date"] == "2019-05-01" and flag is None


def test_dated_installs_count_as_system_updates():
    found = ai._documented_system_updates({"public_remarks": "Recent improvements include a water softener and UV "
                                           "treatment system installed in 2023, EV charging added in 2024."})
    assert "water softener" in found and "ev charging" in found
    assert ai._documented_system_updates({"public_remarks": "Roof and new windows."}) == ["windows"]


# ---- 417 Dogwood Ln v7 (10/20) ------------------------------------------------------------------

def test_size_quoted_from_the_comps_own_listing_is_fine():
    comp = {"sqft": 1504, "above_grade_finished_area": 1504, "below_grade_finished_area": 0,
            "public_remarks": "The impressive 900-square-foot, two-car garage provides abundant room."}
    assert ai._comp_note_number_errors("It has an oversized 900 sq ft two-car garage.", comp, DOGWOOD_SUBJECT) == []
    assert ai._comp_note_number_errors("It has an oversized 950 sq ft two-car garage.", comp, DOGWOOD_SUBJECT)


def test_double_claim_may_describe_the_subject():
    comp = {"sqft": 1472, "above_grade_finished_area": 1472, "below_grade_finished_area": 0}
    ok = "The subject offers roughly double the total finished living space of this smaller home."
    assert ai._comp_note_number_errors(ok, comp, DOGWOOD_SUBJECT) == []
    bad = "Its above-grade area is under half the subject's."
    assert ai._comp_note_number_errors(bad, comp, DOGWOOD_SUBJECT)


def test_inspection_rewrite_keeps_the_item_heading():
    text = ("**Roof Age:** Ask the seller for the roof's age.\n\n"
            "**Water Heater and Electrical:** The water heater, since the home was built in 1972, may be original. "
            "Ask the inspector to note the age and remaining service life of each.")
    out, notes = ai._soften_unsupported_system_claims(text, {"public_remarks": "Updated kitchen."})
    assert notes, "the undocumented claim should still be rewritten"
    assert "**Water Heater and Electrical:**" in out


def test_passed_over_home_gets_no_stretch_and_no_early_exposure_line():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 463000, "opening_high": 483000,
            "target": 490000, "comfort_ceiling": 490000, "passed_over": True}
    text = ("Make the offer contingent on a standard home inspection. Since the home is still early in its market "
            "exposure (14 days), there's no strong signal of urgency from the seller.")
    out, _ = ai._v10_rebuild_buyer_offer_prose(text, band, 0.04)
    assert "home inspection" in out and "early in its market" not in out


# ---- independent review of v7 (10/20) ----------------------------------------------------------

def test_mailing_town_is_not_the_municipality():
    subj = {"city": "COATESVILLE", "township": "CALN", "school_district": "COATESVILLE AREA"}
    summit = {"city": "DOWNINGTOWN", "township": "CALN", "school_district": "COATESVILLE AREA"}
    assert ai._same_municipality(subj, summit) is True
    assert ai._derive_quality_from_data(subj, summit).get("area") == "same_as_subject"
    borough = {"city": "COATESVILLE", "township": "SOUTH COATESVILLE", "school_district": "COATESVILLE AREA"}
    assert ai._same_municipality(subj, borough) is False
    assert ai._same_municipality({"city": "A"}, {"city": "A"}) is True   # no township: mailing town fallback


def test_vague_adjusted_sales_line_gets_the_number():
    notes = "The adjusted sales point somewhat higher, but this home has sat longer than its comparables."
    trace = {"authority": "reasoning_model", "asking_price_check": {"passed_over": True}}
    out, _ = ai._v10_reconcile_anchor_direction_prose(notes, final={"value": 490000, "anchor_value": 497340}, trace=trace)
    assert "$497,000" in out and "$490,000" in out and "small trim" in out
    # a small gap with no time-on-market trim and no vague wording needs no sentence
    plain = "The comparable sales support this value."
    calm = {"authority": "reasoning_model", "asking_price_check": {"passed_over": False}}
    assert ai._v10_reconcile_anchor_direction_prose(plain, final={"value": 490000, "anchor_value": 497340}, trace=calm)[0] == plain


def test_street_weight_comes_from_road_data_not_the_ai_rating():
    subj = {"street_address": "417 DOGWOOD LN", "school_district": "X", "township": "CALN", "city": "COATESVILLE"}
    fronting = {"listing_key": "1", "strength": "supporting", "road_context": {"verdict": "through"},
                "quality": {"street_context": "below"}, "distance_miles": 1.0, "school_district": "X", "township": "CALN"}
    set_back = {"listing_key": "2", "strength": "supporting", "road_context": {"verdict": "neutral"},
                "quality": {"street_context": "below"}, "distance_miles": 1.0, "school_district": "X", "township": "CALN"}
    comps = [fronting, set_back]
    ai._v10_location_class_review(subj, comps, ai._build_evidence_by_key(subj, comps))
    assert comps[0]["_loc"]["street_weight_penalty"] < 1.0
    assert comps[1]["_loc"]["street_weight_penalty"] == 1.0


def test_passed_over_drops_too_short_a_window_line():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 463000, "opening_high": 483000,
            "target": 490000, "comfort_ceiling": 490000, "passed_over": True}
    text = ("The property has only been actively listed for 14 days, which is too short a window to draw conclusions "
            "about seller flexibility. Key contingencies should include a standard home inspection.")
    out, _ = ai._v10_rebuild_buyer_offer_prose(text, band, 0.04)
    assert "too short a window" not in out and "home inspection" in out


# ---- 417 Dogwood Ln v10 (10/21) -----------------------------------------------------------------

def test_listing_garage_size_is_not_corrected_to_the_house_size():
    comp = {"sqft": 1504, "above_grade_finished_area": 1504, "below_grade_finished_area": 0,
            "public_remarks": "The impressive 900-square-foot, two-car garage provides abundant room."}
    out, _ = ai._reconcile_comp_hard_facts("It has a large 900 sqft garage.", "Comp #5 (2011 E KINGS HWY)", DOGWOOD_SUBJECT, comp)
    assert "900 sqft garage" in out and "1,504 sqft garage" not in out


def test_engine_words_become_plain_english():
    text = ("This home sold at $307/sqft, well above the anchor rate. It fronts the highway, which depressed its "
            "location weight in the anchor, and has no finished-basement decomposition.")
    out = ai._strip_internal_data_refs(text)
    assert "anchor" not in out.lower() and "decomposition" not in out
    assert "the typical price per square foot of these sales" in out


def test_fallback_range_is_rounded():
    r = ai._v10_fallback_reasoning({"fmv": 490000, "range_pct": 5}, {"comparables": []}, "HTTP 429")
    assert r["range_low"] % 5000 == 0 and r["range_high"] % 5000 == 0


def test_passed_over_trim_is_explained_with_numbers():
    trace = {"authority": "v10", "asking_price_check": {"passed_over": True}}
    out, _ = ai._v10_reconcile_anchor_direction_prose(
        "This value reflects the closest verified comparable sales.", final={"value": 490000, "anchor_value": 497491}, trace=trace)
    assert "$497,000" in out and "small trim" in out


# ---- independent review of v10 (10/21) ---------------------------------------------------------

def test_how_the_engine_treated_a_comp_is_dropped():
    t = ("It fronts the highway, a clear drawback versus the subject's quiet street, which is why its location weight "
         "was reduced in the anchor. This is the closest sale and anchors the valuation most heavily.")
    out = ai._strip_internal_data_refs(t)
    assert "weight" not in out and "anchor" not in out and "quiet street." in out


def test_strong_comps_listed_first_then_by_weight():
    rows = [{"label": "Comp #1 (A)", "normalized_weight": 0.22, "display_role": "strong"},
            {"label": "Comp #2 (B)", "normalized_weight": 0.19, "display_role": "strong"},
            {"label": "Comp #3 (C)", "normalized_weight": 0.21, "display_role": "supporting"}]
    a = {"anchor_value": 1, "comps": rows}
    assert "comps are not listed strongest-first" not in ai._anchor_integrity_errors(a, {})
    a2 = {"anchor_value": 1, "comps": [rows[2], rows[0], rows[1]]}
    assert "comps are not listed strongest-first" in ai._anchor_integrity_errors(a2, {})


def test_fallback_notes_name_the_strongest_sale_and_the_uncertainty():
    anchor = {"comps": [{"label": "Comp #1 (1219 SCOTT DR)", "normalized_weight": 0.22, "display_role": "strong", "distance_miles": 0.27}],
              "main_uncertainties": ["None of the verified sales has a finished lower level like this home's 1,300 sq ft, so how "
                                     "buyers value that space is the main uncertainty in this value (the grid credits it at about $31/sq ft)."]}
    out = ai._fallback_methodology_details(anchor)
    assert "1219 Scott Dr, 0.3 miles away" in out and "grid" not in out and "main uncertainty" in out


def test_offer_always_keeps_an_inspection_line():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 456000, "opening_high": 475000,
            "target": 488000, "comfort_ceiling": 490000, "passed_over": True}
    out, _ = ai._v10_rebuild_buyer_offer_prose("", band, 0.04)
    assert "inspection contingency" in out


def test_inspection_page_doesnt_point_at_the_valuation_notes():
    out, _ = ai._soften_unsupported_system_claims(
        "**Lower level:** The valuation notes a 2023 lower-level suite. Check the egress window.", {"public_remarks": "x"})
    assert "valuation" not in out.lower() and "the listing notes a 2023" in out.lower()


# ---- 417 Dogwood Ln v11 (10/22) -----------------------------------------------------------------

def test_leftover_engine_clauses_are_cut_but_ordinary_english_stays():
    t = ("This home fronts East Kings Highway, a busier setting than the subject's cul-de-sac, which is reflected in its "
         "reduced location weight in the valuation. It sold at the market rate the valuation anchors to. "
         "The seller weighted the kitchen budget toward cabinets.")
    out = ai._strip_internal_data_refs(t)
    assert "location weight" not in out and "anchors to" not in out
    assert "busier setting than the subject's cul-de-sac." in out and "weighted the kitchen budget" in out


def test_asking_check_range_is_rounded():
    reasoning = {"final_fmv": 500000, "range_low": 470000, "range_high": 530000, "confidence": "medium"}
    check = {"allowed_low": 416500, "allowed_high": 490000, "asking_price": 500000, "ratio_used": 0.98,
             "expected_sale_price": 490000}
    out, _ = ai._apply_asking_price_check(reasoning, {"asking_price_check": check})
    assert out["final_fmv"] == 490000
    assert out["range_low"] % 5000 == 0 and out["range_high"] % 5000 == 0 and out["range_low"] < 490000 < out["range_high"]


# ---- 417 Dogwood Ln v12 (10/22) -----------------------------------------------------------------

def test_passed_over_drops_short_exposure_and_no_signal_lines():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 463000, "opening_high": 483000,
            "target": 490000, "comfort_ceiling": 490000, "passed_over": True}
    text = ("Given the short market exposure so far, there's no strong signal yet of pricing pressure, so a "
            "well-supported offer is best. Key contingencies should include a standard inspection contingency "
            "(given the well and septic-alternative sewer connection).")
    out, _ = ai._v10_rebuild_buyer_offer_prose(text, band, 0.04)
    assert "short market exposure" not in out and "no strong signal" not in out
    assert "sewer connection" in out and "septic-alternative" not in out and "public sewer" not in out


def test_subject_total_size_in_a_comp_note_is_left_alone():
    comp = {"sqft": 1472, "above_grade_finished_area": 1472, "below_grade_finished_area": 0}
    text = "Size-adjusted to the subject's 3,130 sqft above-grade and lower-level space, the gap narrows."
    out, flags = ai._reconcile_comp_hard_facts(text, "Comp #1 (1219 SCOTT DR)", DOGWOOD_SUBJECT, comp)
    assert "3,130" in out and not any("unsupported 3,130" in f for f in flags)


# ---- v12 grader (10/22) ---------------------------------------------------------------------------

def test_value_notes_dont_claim_what_buyers_did():
    r = {"final_fmv": 490000, "range_low": 470000, "range_high": 515000,
         "final_valuation_summary": "Buyers have passed on the current offering longer than the fastest comparable sales."}
    out = ai._reasoning_client_methodology(r)
    assert "passed on" not in out and "has been on the market longer" in out


def test_lower_level_suite_always_gets_its_own_inspection_item():
    subj = {"public_remarks": "In 2023, an entire additional primary suite was added to the lower level, including "
                              "the home's fourth bedroom with egress and a stunning new full bathroom."}
    out = ai._ensure_lower_level_inspection_item("**Roof:** Check the roof age.", subj)
    assert "**Lower-Level Suite (added 2023):**" in out and "egress" in out
    already = "**Lower level:** Confirm the egress window meets code."
    assert ai._ensure_lower_level_inspection_item(already, subj) == already
    assert ai._ensure_lower_level_inspection_item("**Roof:** x", {"public_remarks": "Updated kitchen."}) == "**Roof:** x"


def _edge_check(ask, dom, state):
    packets = [{"sale_price": 5e5, "sold_list_ratio": 1.0, "dom": 10}] * 3
    return ai._asking_price_market_check({"current_list_price": ask, "dom": dom}, {},
                                         {"state": state, "comp_median_dom": 10}, packets)


def _edge_final(chk, fmv=500000):
    out, _ = ai._apply_asking_price_check(
        {"final_fmv": fmv, "range_low": fmv * 0.95, "range_high": fmv * 1.05, "confidence": "medium"},
        {"asking_price_check": chk})
    return out["final_fmv"]


def test_time_on_market_trim_does_not_compound():
    # a fairly priced home: one small trim once passed over, the same at 30 days or 3 years
    vals = {d: _edge_final(_edge_check(505000, d, "extended_exposure" if d > 45 else "elevated_exposure"))
            for d in (30, 90, 365, 1095)}
    # the band edge, rounded down to a whole $1,000 inside the band (10/24)
    assert len(set(vals.values())) == 1 and vals[30] == (round(505000 * 0.98) // 1000) * 1000


def test_long_unsold_overpriced_listing_lets_the_sales_win():
    early = _edge_check(650000, 30, "elevated_exposure")
    late = _edge_check(650000, 400, "extended_exposure")
    assert not early["long_unsold"] and late["long_unsold"]
    assert _edge_final(early) > 540000          # a month in, the asking price still carries weight
    assert _edge_final(late) == 500000          # a year+ unsold: the comparable sales stand


def test_fresh_underpriced_listing_can_value_above_the_old_cap():
    chk = _edge_check(450000, 5, "insufficient_exposure")
    assert chk["fresh_listing"] and chk["allowed_high"] == 495000
    assert _edge_final(chk) == 495000
    # not fresh any more and passed over: no premium over asking
    assert _edge_final(_edge_check(450000, 30, "elevated_exposure")) <= 450000


def test_band_edge_prose_names_the_asking_price_reason():
    trace = {"anchor": {"anchor_value": 500000},
             "asking_price_check": {"allowed_low_price": 541450, "allowed_high_price": 637000}}
    text, _ = ai._v10_reconcile_anchor_direction_prose(
        "The home compares well.", final={"value": 541450, "anchor_value": 500000}, trace=trace)
    assert "rarely sell this far under their asking price" in text


def test_reasoning_cache_key_ignores_day_counts_but_not_prices():
    pkt = {"subject": {"dom": 14, "cdom": 14, "current_list_price": 500000},
           "comparables": [{"sale_price": 452000, "normalized_weight": 0.2341, "sale_date": "2026-05-01"}],
           "deterministic_evidence": {"deterministic_anchor": 496542, "exposure_state": "elevated_exposure"},
           "asking_price_check": {"passed_over": True, "allowed_high": 490000}}
    next_day = {**pkt, "subject": {**pkt["subject"], "dom": 15, "cdom": 15},
                "comparables": [{**pkt["comparables"][0], "normalized_weight": 0.2338}],
                "deterministic_evidence": {**pkt["deterministic_evidence"], "deterministic_anchor": 496610}}
    price_cut = {**pkt, "subject": {**pkt["subject"], "current_list_price": 485000}}
    state_change = {**pkt, "deterministic_evidence": {**pkt["deterministic_evidence"], "exposure_state": "extended_exposure"}}
    k = ai._reasoning_cache_key(pkt, "m")
    assert ai._reasoning_cache_key(next_day, "m") == k
    assert ai._reasoning_cache_key(price_cut, "m") != k
    assert ai._reasoning_cache_key(state_change, "m") != k
    assert ai._day_stable_text("listed 14 days, comps $496,542 (23.4%)") == ai._day_stable_text("listed 15 days, comps $496,610 (23.1%)")


def test_cached_answer_day_count_is_refreshed():
    raw = {"methodology": "It has been listed 14 days, longer than its comps."}
    out, changed = ai._refresh_cached_subject_days(raw, 14, 16, {6, 9})
    assert changed and "listed 16 days" in out["methodology"]
    same, changed = ai._refresh_cached_subject_days(raw, 14, 16, {14})   # a comp also took 14 days
    assert not changed and same == raw


def test_asking_gap_sentence_and_far_old_flags():
    trace = {"asking_price_check": {"asking_price": 600000}}
    text, _ = ai._v10_asking_gap_sentence("Notes.", final={"anchor_value": 480000}, trace=trace)
    assert "The $600,000 asking price is about 25% above what the adjusted comparable sales alone point to (about $480,000)." in text
    assert ai._v10_asking_gap_sentence(text, final={"anchor_value": 480000}, trace=trace)[0] == text   # once only
    assert ai._v10_asking_gap_sentence("Notes.", final={"anchor_value": 590000}, trace=trace)[0] == "Notes."
    from datetime import timedelta
    old = (ai._tmach.today() - timedelta(days=300)).isoformat()
    flags = ai._far_or_old_comp_flags([{"distance_miles": 2.6, "close_date": old, "street_address": "1 A St"},
                                       {"distance_miles": 0.4, "close_date": ai._tmach.today().isoformat()}])
    assert len(flags) == 1 and "2.6 miles away" in flags[0] and "months ago" in flags[0]


def test_grid_knob_defaults_time_and_age_on_road_off():
    assert ai._V10_FEATURE_AGE_PCT == 0.02 and ai._V10_FEATURE_ROAD_PCT == 0 and ai._V10_TIME_PCT_PER_YEAR == 0.03


def test_v13_valuation_notes_say_trim_once():
    notes = ("The final fair market value is $490,000. Adjusted comparable sales point somewhat higher than the final "
             "estimate, but the current asking price and longer marketing time support a modest trim. The home has been "
             "on the market longer than the fastest comparable sales - a small trim, not a sign of distress.")
    trace = {"authority": "reasoning_model", "anchor": {"anchor_value": 499877},
             "asking_price_check": {"passed_over": True, "asking_price": 500000, "allowed_high_price": 490000}}
    out, _ = ai._v10_reconcile_anchor_direction_prose(notes, final={"value": 490000, "anchor_value": 499877}, trace=trace)
    assert "somewhat higher" not in out
    assert out.lower().count("trim") == 2
    assert out.endswith("point to about $500,000; after that small time-on-market trim, the final value lands about 2% below that, at $490,000.")


def test_v13_passed_over_range_top_not_capped_at_asking():
    # 10/24 grader (417 Dogwood Ln): asking price + time on market move the value, they don't cap the range
    chk = {"asking_price": 500000, "allowed_low": 416500, "allowed_high": 490000, "passed_over": True,
           "expected_sale_price": 490000, "ratio_used": 0.98}
    out, flags = ai._apply_asking_price_check(
        {"final_fmv": 490000, "range_low": 475000, "range_high": 515000, "confidence": "medium"}, {"asking_price_check": chk})
    assert out["final_fmv"] == 490000 and out["range_low"] == 475000 and out["range_high"] == 515000 and not flags
    fresh = {**chk, "passed_over": False, "allowed_high": 530000}
    same, _ = ai._apply_asking_price_check(
        {"final_fmv": 490000, "range_low": 475000, "range_high": 515000, "confidence": "medium"}, {"asking_price_check": fresh})
    assert same["range_high"] == 515000


def test_v13_normalized_basement_wording_is_plain():
    text = ("Its finished basement is below-grade value already normalized into the comparison, and the main-level "
            "living area is dated with original cabinetry.")
    out = ai._strip_internal_data_refs(text)
    assert out == "The main-level living area is dated with original cabinetry."


def _oa_packets(rows):
    return [{"sale_price": 5e5, "sold_list_ratio": r, "dom": d} for r, d in rows]


def test_over_ask_pattern_is_reported_not_priced_in():
    hot = _oa_packets([(1.16, 5), (1.12, 4), (1.08, 7), (1.00, 20), (0.97, 30)])
    chk = ai._asking_price_market_check({"current_list_price": 400000, "dom": 3}, {},
                                        {"state": "insufficient_exposure", "comp_median_dom": 7}, hot)
    pat = chk["over_ask_pattern"]
    assert pat == {"count": 3, "of": 5, "top_pct": 16.0, "median_days": 5}
    assert chk["allowed_high"] == round(400000 * 1.08 * 1.05)   # the usual band - wording only (backtest)
    trace = {"asking_price_check": {"over_ask_pattern": pat}}
    text, _ = ai._v10_over_ask_pattern_sentence("Notes.", trace=trace)
    assert text == ("Notes. Three of the five comparable sales sold at least 2% over their asking price, typically within 5 days "
                    "(up to 16% over), so this newly listed home could draw competing offers above its asking price.")
    assert ai._v10_over_ask_pattern_sentence(text, trace=trace)[0] == text


def test_over_ask_pattern_off_when_not_most_comps_or_not_fresh():
    dogwood = _oa_packets([(1.159, 6), (1.138, 6), (1.065, 42), (1.011, 3), (0.963, 28)])   # 2 of 5 fast over
    chk = ai._asking_price_market_check({"current_list_price": 500000, "dom": 3}, {},
                                        {"state": "insufficient_exposure", "comp_median_dom": 6}, dogwood)
    assert chk["over_ask_pattern"] is None and chk["allowed_high"] < 500000 * 1.159
    hot = _oa_packets([(1.16, 5), (1.12, 4), (1.08, 7), (1.00, 20), (0.97, 30)])
    late = ai._asking_price_market_check({"current_list_price": 400000, "dom": 16}, {},
                                         {"state": "normal_exposure", "comp_median_dom": 7}, hot)
    assert late["over_ask_pattern"] is None   # sat longer than those sales: no promise of competition


def test_priced_above_sales_gets_no_over_ask_wording():
    hot = _oa_packets([(1.16, 5), (1.12, 4), (1.08, 7), (1.00, 20), (0.97, 30)])
    exp = {"state": "insufficient_exposure", "comp_median_dom": 7}
    fair = ai._asking_price_market_check({"current_list_price": 500000, "dom": 3}, {}, exp, hot, anchor_value=505000)
    high = ai._asking_price_market_check({"current_list_price": 600000, "dom": 3}, {}, exp, hot, anchor_value=520000)
    assert fair["over_ask_pattern"] and not fair["priced_above_sales"] and fair["ratio_used"] > 1
    # the agent already priced it 15% over what the sales support: no "it'll go over asking too"
    assert high["priced_above_sales"] and high["over_ask_pattern"] is None
    assert high["ratio_used"] == fair["ratio_used"]   # the band itself is unchanged (backtest)


def test_loose_fit_estate_is_flagged_and_sales_dont_win():
    rows = [{"ag": 2400, "price": 700000, "feat": {"gross": 60000}, "_c": {"match_score": 66}},
            {"ag": 2600, "price": 760000, "feat": {"gross": 70000}, "_c": {"match_score": 62}}]
    reasons = ai._anchor_loose_fit_reasons({"sqft": 5200}, rows)
    assert any("much larger than every sale" in r for r in reasons)
    assert any("best is a 66% match" in r for r in reasons)
    close = [{"ag": 2400, "price": 700000, "feat": {"gross": 30000}, "_c": {"match_score": 88}}]
    assert ai._anchor_loose_fit_reasons({"sqft": 2500}, close) == []
    packets = [{"sale_price": 7e5, "sold_list_ratio": 1.0, "dom": 10}] * 3
    exp = {"state": "extended_exposure", "comp_median_dom": 10}
    loose = ai._asking_price_market_check({"current_list_price": 1500000, "dom": 400}, {}, exp, packets, sales_loose_fit=True)
    tight = ai._asking_price_market_check({"current_list_price": 1500000, "dom": 400}, {}, exp, packets)
    assert not loose["long_unsold"] and tight["long_unsold"] and loose["allowed_low"] > tight["allowed_low"]
    trace = {"asking_price_check": {"asking_price": 1500000}, "anchor": {"loose_fit": True}}
    text, _ = ai._v10_asking_gap_sentence("Notes.", final={"anchor_value": 1100000}, trace=trace)
    assert text.endswith("(about $1,100,000), but none of those sales is a close match for this home, so treat that as a rough guide.")


def test_v15_offer_prose_drops_leverage_and_above_list_sentences():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 463000, "opening_high": 483000,
            "target": 490000, "comfort_ceiling": 490000, "passed_over": True}
    text = ("With only 15 active-market days and no price reductions, there is limited leverage to pursue a "
            "below-list offer, so buyers should be prepared to move decisively if comparable demand exists. "
            "Standard contingencies (financing and a general home inspection) are appropriate, with particular "
            "attention to verifying permits for the 2023 lower-level addition and well water quality. If multiple "
            "offers emerge, escalating modestly above list within the indicated value range is supportable given "
            "the documented updates and larger lot.")
    out, _ = ai._v10_rebuild_buyer_offer_prose(text, band, 0.04)
    assert "Standard contingencies" in out
    assert "limited leverage" not in out and "move decisively" not in out and "above list" not in out
    hot = {**band, "hot_market": True, "passed_over": False, "market_over_ask_pct": 6.5, "comfort_ceiling": 499800}
    kept, _ = ai._v10_rebuild_buyer_offer_prose("If multiple offers emerge, escalating modestly above list is reasonable.", hot, 0.04)
    assert "escalating modestly above list" in kept   # a hot market may say it


def test_v15_grader_wording_fixes():
    # basement clause: engine talk dropped, the rest of the sentence kept and capitalised
    t = ("Its finished basement is accounted for separately in the comparison, and the main-level living area is "
         "dated with original laminate-style cabinetry.")
    assert ai._strip_internal_data_refs(t) == "The main-level living area is dated with original laminate-style cabinetry."
    t2 = ("Its finished basement is below-grade value already normalized into the comparison, and the main-level "
          "living area is dated.")
    assert ai._strip_internal_data_refs(t2) == "The main-level living area is dated."
    # mailing town vs township
    subj = {"city": "COATESVILLE", "township": "Caln Twp"}
    comp = {"city": "DOWNINGTOWN", "township": "Caln Twp"}
    out, flags = ai._reconcile_comp_hard_facts(
        "It sits about a mile farther out in Downingtown on a quiet cul-de-sac.", "Comp #4", subj, comp)
    assert "with a Downingtown mailing address" in out and flags
    other, _ = ai._reconcile_comp_hard_facts("It sits in Downingtown.", "Comp #4", subj,
                                             {"city": "DOWNINGTOWN", "township": "Downingtown Boro"})
    assert "in Downingtown" in other
    # "larger lot" only when it is larger
    comps = [{"lot_size_sqft": 47916}, {"lot_size_sqft": 17424}]
    s = {"lot_size_sqft": 29621}
    txt, f = ai._reconcile_lot_size_claims("Supportable given the documented updates and larger lot.", "Offer", s, comps)
    assert "larger lot" not in txt and txt == "Supportable given the documented updates." and f
    big, _ = ai._reconcile_lot_size_claims("Its larger lot helps.", "Offer", {"lot_size_sqft": 60000}, comps)
    assert big == "Its larger lot helps."
    # appraisal contingency always present
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 463000, "opening_high": 483000,
            "target": 490000, "comfort_ceiling": 490000, "passed_over": True}
    offer, _ = ai._v10_rebuild_buyer_offer_prose("Standard contingencies are appropriate.", band, 0.04)
    assert "appraisal contingency" in offer and "inspection contingency" in offer


def test_reasoning_cache_key_changes_with_the_reasoning_prompt(monkeypatch):
    pkt = {"subject": {"dom": 14, "current_list_price": 500000}}
    k = ai._reasoning_cache_key(pkt, "m")
    monkeypatch.setattr(ai, "_CMA_REASONING_SYSTEM_PROMPT", ai._CMA_REASONING_SYSTEM_PROMPT + "\nNew rule.")
    assert ai._reasoning_cache_key(pkt, "m") != k


# ---- 417 Dogwood Ln v16 (10/21) -----------------------------------------------------------------

def test_v16_valuation_notes_name_the_main_adjustments():
    notes = "The final fair market value is $490,000. The home has been on the market longer than the fastest comparable sales."
    comps = [{"normalized_weight": 0.6, "feature_adjustments": {"size": 32268, "below_grade": 17429, "baths": 4520, "time": 4347}},
             {"normalized_weight": 0.4, "feature_adjustments": {"size": 20000, "below_grade": 30000, "garage": 2000}}]
    trace = {"authority": "reasoning_model", "anchor": {"anchor_value": 499877, "comps": comps},
             "asking_price_check": {"passed_over": True, "asking_price": 500000, "allowed_high_price": 490000}}
    out, _ = ai._v10_reconcile_anchor_direction_prose(notes, final={"value": 490000, "anchor_value": 499877}, trace=trace)
    assert "point to about $500,000 (adjusted mainly for differences in above-grade size and finished lower-level space);" in out
    # nothing big enough to name -> the sentence reads as before
    small = {**trace, "anchor": {"anchor_value": 499877, "comps": [{"normalized_weight": 1, "feature_adjustments": {"time": 1200}}]}}
    out2, _ = ai._v10_reconcile_anchor_direction_prose(notes, final={"value": 490000, "anchor_value": 499877}, trace=small)
    assert "point to about $500,000;" in out2 and "adjusted mainly" not in out2


def test_v16_offer_explains_opening_under_asking_when_passed_over_after_over_ask_sales():
    band = {"fmv": 490000, "list_price": 500000, "opening_low": 463000, "opening_high": 483000, "target": 490000,
            "comfort_ceiling": 490000, "passed_over": True, "over_ask_passed": {"count": 3, "of": 5, "median_days": 6}}
    out, _ = ai._v10_rebuild_buyer_offer_prose("Use a standard inspection contingency.", band, 0.04)
    assert "Three of the five comparable sales went at least 2% over their asking price, most within 6 days" in out
    assert "opening below asking is reasonable" in out
    once = ai._v10_rebuild_buyer_offer_prose(out, band, 0.04)[0]
    assert once.count("went at least 2% over their asking price") == 1
    plain = ai._v10_rebuild_buyer_offer_prose("", {**band, "over_ask_passed": None}, 0.04)[0]
    assert "went at least 2% over their asking price" not in plain


def test_v16_vague_farther_out_becomes_real_distance():
    comp = {"distance_miles": 2.63}
    text = "It supports its use despite sitting about a mile farther out with a Downingtown mailing address."
    out, _ = ai._reconcile_comp_hard_facts(text, "Comp #4 (3306 E SUMMIT AVE)", DOGWOOD_SUBJECT, comp)
    assert "about 2.6 miles away" in out and "farther out" not in out


# ---- grader on v16 (10/21) ----------------------------------------------------------------------

def test_v16_permit_check_lives_only_in_the_permit_item():
    text = ("**Foundation/Structure:** Ask the seller for the foundation type and check for moisture, particularly given "
            "the 2023 lower-level addition (confirm the egress window and suite addition were permitted).\n\n"
            "**Electrical:** Verify the panel, and confirm that the 2024 EV charging installation was properly permitted.\n\n"
            "**Lower-Level Egress/Permits:** Verify that the 2023 lower-level bedroom addition, including the egress "
            "window, received proper permits.")
    out = ai._dedupe_permit_checks(text)
    assert "(confirm the egress window" not in out
    assert "particularly given the 2023 lower-level addition." in out
    assert "EV charging installation was properly permitted" in out
    assert out.count("received proper permits") == 1
    assert ai._dedupe_permit_checks(out) == out


def test_v16_original_finishes_softened_unless_the_listing_says_original():
    comp = {"public_remarks": "Fantastic brick ranch with a fully finished basement."}
    text = "The main level is dated with original laminate-style cabinetry and tile. Original kitchen too."
    out, _ = ai._reconcile_comp_hard_facts(text, "Comp #1 (1219 SCOTT DR)", DOGWOOD_SUBJECT, comp)
    assert "older laminate-style cabinetry" in out and "Older kitchen" in out and "original" not in out.lower()
    said = {"public_remarks": "Original kitchen, sold as-is."}
    assert "original kitchen" in ai._reconcile_comp_hard_facts("It has an original kitchen.", "Comp #1", DOGWOOD_SUBJECT, said)[0]
    # other "original" uses are left alone
    assert "original hardwood" in ai._reconcile_comp_hard_facts("Carpet over original hardwood.", "Comp #2", DOGWOOD_SUBJECT, comp)[0]


def test_range_top_never_explained_as_an_asking_cap():
    # 10/24 grader (417 Dogwood Ln V20): the range is no longer capped at asking, and the old cap sentence
    # (printed twice in V20) is dropped from earlier notes on a re-run
    notes = ("The final fair market value is $490,000, with a supported valuation range of $470,000-$500,000. "
             "The range stops at the $500,000 asking price because the home hasn't drawn an accepted offer at that price yet.")
    trace = {"authority": "reasoning_model", "deterministic_anchor": 495040, "anchor": {"anchor_value": 495040},
             "asking_price_check": {"passed_over": True, "asking_price": 500000, "allowed_high_price": 490000,
                                    "expected_sale_price": 490000, "ratio_used": 0.98}}
    trace["value_bridge"] = ai._v10_value_bridge(trace, value=490000, range_min=470000, range_max=500000)
    final = {"value": 490000, "anchor_value": 495040, "range_min": 470000, "range_max": 500000}
    out, _ = ai._v10_reconcile_anchor_direction_prose(notes, final=final, trace=trace)
    assert "range stops" not in out and out.count("How the value was reached:") == 1
    again, _ = ai._v10_reconcile_anchor_direction_prose(out, final=final, trace=trace)
    assert again.count("How the value was reached:") == 1


# ---- 417 Dogwood Ln v17 (10/21) -----------------------------------------------------------------

def test_v17_orphan_distress_line_dropped_but_kept_after_market_time():
    orphan = ("No verified sale closely matches this home's finished lower level, keeping confidence moderate. "
              "This is not evidence of distress or a demonstrated condition problem. Weighted together, the sales agree.")
    out, flags = ai._drop_orphan_distress_disclaimer(orphan)
    assert "distress" not in out and flags
    fine = "The home has been on the market longer than its comps. This is not a sign of distress."
    assert ai._drop_orphan_distress_disclaimer(fine)[0] == fine
    assert ai._num_word(3) == "three" and ai._num_word(12) == "12"
