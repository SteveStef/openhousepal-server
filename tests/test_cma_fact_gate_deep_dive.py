"""Deep dive 11/13 (Fable review of the fact gate): the text-correction rules added that day changed
CORRECT sentences. Every case here is one the reviewer ran and saw go wrong."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_fact_check as fc  # noqa: E402

SUBJ_344 = {"list_price": 688000, "above_grade_finished_area": 2000, "sqft": 2000}


# ---- $/sq ft direction: a difference is not a rate ---------------------------------------------------
def test_a_per_foot_difference_is_not_flipped():
    for s in ("It closed about $20 per sq ft above the subject's $344 list rate.",
              "That is a $40/sqft premium over the subject's $344."):
        assert fc.fix_psf_direction(s, SUBJ_344) == (s, [])


def test_a_wrong_direction_on_real_rates_is_still_fixed():
    out, flags = fc.fix_psf_direction("It sold at $303 per sq ft, well above the subject's $344 list rate.", SUBJ_344)
    assert "well below the subject's $344" in out and flags


# ---- busy road ----------------------------------------------------------------------------------------
QUIET = {"street_address": "701 Sturbridge Dr", "road_context": {"verdict": "quiet"}}


def test_busy_road_clause_at_sentence_start_leaves_a_clean_sentence():
    out, flags = fc.fix_subject_busy_road_claim("Unlike the subject's busy road, this comp sits on a cul-de-sac.", QUIET)
    assert out == "This comp sits on a cul-de-sac." and flags


def test_busy_road_clause_that_runs_on_is_left_alone():
    s = "Its cul-de-sac is a plus over the subject's busier Lancaster Avenue frontage."
    assert fc.fix_subject_busy_road_claim(s, QUIET) == (s, [])


def test_busy_road_clause_at_the_end_is_cut():
    out, _ = fc.fix_subject_busy_road_claim("It sits on a quiet cul-de-sac, a plus versus the subject's more trafficked road setting.", QUIET)
    assert out == "It sits on a quiet cul-de-sac, a plus."


def test_main_street_with_no_road_data_may_be_busy():
    s = "A plus versus the subject's busy road."
    assert fc.fix_subject_busy_road_claim(s, {"street_address": "12 Main St"}) == (s, [])


# ---- lot acres ----------------------------------------------------------------------------------------
def test_a_known_bad_lot_size_is_never_written_into_a_note():
    comp = {"lot_size": 18774360, "sale_price": 450000}          # 523 Sherbrook Dr: "431 acres"
    out, _ = fc.fix_comp_lot_acres("It sits on a 0.4-acre lot in a subdivision.", {}, comp)
    assert "431" not in out


def test_a_comp_whose_record_only_has_acres_keeps_its_correct_figure():
    s = "It sits on a 0.40-acre lot in a subdivision."
    assert fc.fix_comp_lot_acres(s, {}, {"lot_size_acres": 0.4}) == (s, [])


def test_the_subjects_acreage_inside_a_comp_note_is_left_alone():
    s = "It is far smaller than the subject, which sits on a roughly 2-acre lot."
    assert fc.fix_comp_lot_acres(s, {"lot_size": 1.9 * 43560}, {"lot_size": 0.5 * 43560}) == (s, [])


def test_an_invented_comp_lot_is_still_dropped():
    out, flags = fc.fix_comp_lot_acres("A new-construction home on a roughly 8.8-acre lot.", {}, {})
    assert out == "A new-construction home on a lot." and flags


# ---- building height ----------------------------------------------------------------------------------
TOWER = {"street_address": "50 S 16TH ST #5203", "zipcode": "19102", "property_type": "CONDO"}


def test_a_comp_in_another_building_keeps_its_own_building_type():
    s = "This unit is in a mid-rise building two blocks away."
    assert fc.fix_building_height_words(s, TOWER, {"street_address": "1414 S PENN SQ #14E", "zipcode": "19102"}) == (s, [])


def test_a_comp_in_the_same_building_is_corrected():
    out, _ = fc.fix_building_height_words("Another unit in this mid-rise building.", TOWER, {"street_address": "50 S 16TH ST #4504", "zipcode": "19102"})
    assert "high-rise" in out


def test_report_text_sentences_about_a_comp_are_left_alone():
    s = "As a mid-rise condo unit, the roof is the association's. Comp #3 is in a mid-rise two blocks away."
    out, _ = fc.fix_building_height_words(s, TOWER)
    assert out == "As a high-rise condo unit, the roof is the association's. Comp #3 is in a mid-rise two blocks away."


def test_garden_condo_unit_numbers_are_not_a_high_rise():
    assert fc.building_height_class({"street_address": "200 Lincoln Ter Unit 1305", "property_type": "CONDO"}) is None
    assert fc.building_height_class({"street_address": "9 Elm Ct #1412", "property_type": "CONDO",
                                     "public_remarks": "Garden-style community with a pool."}) is None
    assert fc.building_height_class({"street_address": "1 Oak St #1412", "property_type": "CONDO",
                                     "public_remarks": "High-rise living on the 14th floor."}) == "high"
    assert fc.building_height_class({"street_address": "12 Farm Ln #4407", "property_type": "SINGLE_FAMILY"}) is None


# ---- basements ----------------------------------------------------------------------------------------
FULL = {"basement": "Full", "below_grade_finished_area": 0}


def test_decimals_and_other_facts_survive_the_basement_rewrite():
    out, flags = fc.fix_basement_unlike_comparison(
        "It has 2.5 baths and an unfinished basement, a step down from the subject's full basement.", FULL, {})
    assert out == "It has 2.5 baths and an unfinished basement, and the subject's basement has no finished area on record either." and flags
    out, _ = fc.fix_basement_unlike_comparison(
        "Its 0.46-acre lot is larger, but its unfinished basement is a step down from the subject's full basement, worth roughly 1.5% here.", FULL, {})
    assert out.startswith("Its 0.46-acre lot is larger, but its basement is unfinished") and out.endswith("worth roughly 1.5% here.")


def test_a_size_comparison_that_only_says_below_is_untouched():
    s = "Its unfinished basement adds storage, though its 1,800 sq ft is below the subject's 2,100 and the basement adds no living area."
    assert fc.fix_basement_unlike_comparison(s, FULL, {}) == (s, [])


def test_no_rewrite_when_the_subject_has_no_basement():
    s = "Its unfinished basement is a step down from the subject's full basement."
    assert fc.fix_basement_unlike_comparison(s, {"basement": "None"}, {}) == (s, [])
    assert fc.fix_basement_unlike_comparison(s, {}, {}) == (s, [])


# ---- sewer, bedrooms ----------------------------------------------------------------------------------
def test_only_the_impossible_public_septic_is_dropped():
    assert fc.clean_sewer(["Public Sewer", "Public Septic"]) == ["Public Sewer"]
    assert fc.clean_sewer(["Private Sewer", "On Site Septic"]) == ["Private Sewer", "On Site Septic"]
    assert fc.clean_sewer(["Public Sewer", "On Site Septic"]) == ["Public Sewer", "On Site Septic"]


def test_bedroom_note_needs_a_real_plus_den_layout():
    assert fc.bedroom_record_note({"beds": 4, "public_remarks": "Three bedrooms and a study, plus a first-floor primary suite."}) is None
    assert fc.bedroom_record_note({"beds": 2, "public_remarks": "A true one-bedroom-plus-den layout."})
    assert fc.bedroom_record_note({"beds": 2, "public_remarks": "1 bedroom + den with city views."})


# ---- builder base price --------------------------------------------------------------------------------
B = {"builder_base_price": True}


def test_the_builder_rule_is_stable_on_a_second_pass():
    first, _ = fc.fix_builder_base_price_claims("It sold well above its list price, reflecting strong demand for new builds.", B)
    assert fc.fix_builder_base_price_claims(first, B) == (first, [])
    assert first.endswith("rather than a bidding war.")


def test_not_a_bidding_war_is_already_right():
    s = "This was not a bidding war; the buyer added options over the base price."
    assert fc.fix_builder_base_price_claims(s, B) == (s, [])


def test_a_decimal_ratio_goes_with_its_clause():
    out, _ = fc.fix_builder_base_price_claims("It sold for $867,620, reflecting strong demand at a sold-to-list ratio of 1.56.", B)
    assert out.startswith("It sold for $867,620.") and "620.56" not in out


# ---- older rules that rewrote correct text ---------------------------------------------------------------
def test_a_statement_about_the_subject_in_a_comp_note_keeps_the_subjects_facts():
    comp = {"basement": "Fully Finished", "sqft": 2000, "above_grade_finished_area": 2000}
    s = "It also offers a finished lower level, while the subject has no finished basement."
    assert fc.fix_lower_level_claims(s, comp, "Comp #1") == (s, [])
    s2 = "The subject is a larger home with more above-grade space."
    assert fc.fix_size_claims(s2, {"sqft": 2800, "above_grade_finished_area": 2800}, comp)[0] == s2


def test_a_lot_word_about_the_comps_is_not_flipped():
    subject = {"lot_size": 0.2 * 43560}
    comps = [{"lot_size": 0.5 * 43560}, {"lot_size": 0.6 * 43560}, {"lot_size": 0.7 * 43560}]
    for s in ("Most of the comparable sales sit on a larger lot than this home.", "The comps' larger lot sizes explain part of the gap."):
        assert fc.fix_subject_lot_words(s, subject, comps) == (s, [])


def test_school_district_and_corridor_names_are_not_subdivision_claims():
    subject, comp = {"subdivision_name": "Exton Station", "city": "Exton"}, {"subdivision_name": "Indian King", "city": "West Chester"}
    for s in ("Both homes are in the same West Chester Area School District.", "It sits in the same Main Line corridor."):
        assert fc.fix_community_claims(s, subject, comp)[0] == s


def test_a_wrong_same_community_claim_is_still_fixed():
    subject, comp = {"subdivision_name": "Exton Station"}, {"subdivision_name": "Indian King"}
    out, flags = fc.fix_community_claims("It is in the same Exton Station community as the subject.", subject, comp)
    assert "same Exton Station community" not in out and flags


def test_parallel_inspection_items_are_all_kept():
    text = ("Have the roof inspected by a licensed professional before closing. Have the oil tank inspected by a licensed "
            "professional before closing. Have the sump pump inspected by a licensed professional before closing.")
    out, flags = fc.drop_repeated_sentences(text)
    assert "oil tank" in out and "sump pump" in out and not flags
