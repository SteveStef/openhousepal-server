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
