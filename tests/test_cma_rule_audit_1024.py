"""10/24 rule audit (891 rules reviewed) + the remade 8084 Goshen Rd / 102 Steeplewood Dr reports. Each test
is a rule that used to turn true text into false text, or a fact bug - inputs are the shapes those reports had."""
from types import SimpleNamespace

import app.main  # noqa: F401
from app.services import cma_ai_service as ai
from app.services import cma_fact_check as fc

GOSHEN = {"street_address": "8084 GOSHEN RD", "sqft": 4870, "above_grade_finished_area": 4870, "below_grade_finished_area": 0,
          "township": "WILLISTOWN", "school_district": "GREAT VALLEY", "subdivision_name": None, "year_built": 1981}
JOANNA = {"street_address": "6 JOANNA DR", "sqft": 3750, "township": "WILLISTOWN", "school_district": "GREAT VALLEY"}
WARREN = {"street_address": "629 S WARREN AVE", "sqft": 6593, "above_grade_finished_area": 4857,
          "below_grade_finished_area": 1736, "subdivision_name": "RADNOR HUNT", "township": "WILLISTOWN"}


# ---- size claims ---------------------------------------------------------------------------------

def test_true_size_difference_is_never_rewritten_to_the_comps_own_size():
    text = "6 Joanna Dr sold above the subject's rate despite being roughly 1,100 sq ft smaller and on a smaller lot."
    out, _ = ai._reconcile_comp_hard_facts(text, "Comp #2", GOSHEN, JOANNA)
    assert "1,100 sq ft smaller" in out and "3,750 sq ft smaller" not in out


def test_wrong_size_difference_drops_the_number_not_the_sentence():
    text = "It is roughly 2,500 sq ft smaller than the subject."
    out, flags = ai._reconcile_comp_hard_facts(text, "Comp #2", GOSHEN, JOANNA)
    assert "2,500" not in out and "smaller than the subject" in out and "roughly smaller" not in out


def test_comps_own_total_size_is_not_an_unsupported_claim():
    linda = {"street_address": "100 LINDA CIR", "sqft": 3031}
    out, flags = ai._reconcile_comp_hard_facts("A 3,031 sq ft colonial.", "Comp #4", GOSHEN, linda)
    assert "3,031" in out and not any("unsupported" in f for f in flags)


def test_much_larger_claim_for_a_same_size_comp_is_corrected():
    text = "It is a much larger (4,857 sq ft above-grade), newer (2002) stone Colonial."
    out, flags = fc.fix_text(text, subject=GOSHEN, comp=WARREN)
    assert "much larger" not in out and "similarly sized" in out and flags
    assert fc.size_claim_problem("Its larger size naturally pulls its $/sqft down.", GOSHEN, WARREN)
    # a real size gap is left alone
    assert fc.fix_text("It is a much smaller home.", subject=GOSHEN, comp=JOANNA)[0] == "It is a much smaller home."
    assert "smaller lot" in fc.fix_text("It sits on a smaller lot.", subject=GOSHEN, comp=WARREN)[0]


# ---- place names ---------------------------------------------------------------------------------

def test_same_named_corridor_rewritten_when_subject_is_not_in_it():
    out, flags = fc.fix_text("It sold quickly on this same Radnor Hunt corridor.", subject=GOSHEN, comp=WARREN)
    assert "same Radnor Hunt" not in out and "nearby Radnor Hunt corridor" in out and flags


def test_same_township_area_is_true_and_kept():
    text = "Both sit in the same Willistown area."
    assert fc.fix_text(text, subject=GOSHEN, comp=JOANNA)[0] == text
    assert fc.fix_text("It is in the same area.", subject=GOSHEN, comp=WARREN)[0] == "It is in the same area."


# ---- superlatives --------------------------------------------------------------------------------

def test_closest_most_recent_claim_checked_against_the_comp_set():
    comps = [{"street_address": "223 WARREN CT", "distance_miles": 0.73, "close_date": "2025-11-17"},
             {"street_address": "113 BLUE RIBBON WAY", "distance_miles": 0.54, "close_date": "2025-12-09"},
             {"street_address": "523 SHERBROOK DR", "distance_miles": 1.04, "close_date": "2026-07-13"}]
    text = "223 Warren Ct is the closest, most recent sale and the best size match."
    out, flags = ai._fix_comp_note_superlatives(text, "Comp #1", comps[0], comps)
    assert "closest" not in out and "most recent" not in out and "is a nearby sale and" in out and len(flags) == 2
    # true for the comp that really is closest
    t2 = "113 Blue Ribbon Way is the closest sale."
    assert ai._fix_comp_note_superlatives(t2, "Comp #2", comps[1], comps)[0] == t2


def test_closest_match_means_similarity_not_distance():
    comps = [{"listing_key": "a", "distance_miles": 0.73}, {"listing_key": "b", "distance_miles": 0.54}]
    text = "Comp #1 is the closest match and sold below list."
    out, _ = ai._reconcile_closest_comp_claims(text, "Offer", comps, {"a": 1, "b": 2}, {"a": "223 Warren Ct", "b": "113 Blue"})
    assert out == text
    out, _ = ai._reconcile_closest_comp_claims("Comp #1, the closest sale, sold below list.", "Offer", comps,
                                               {"a": 1, "b": 2}, {"a": "223 Warren Ct", "b": "113 Blue"})
    assert "the a nearby" not in out and "a nearby sale" in out


# ---- inspection wording --------------------------------------------------------------------------

def test_negated_deferred_maintenance_is_left_alone():
    t = "Ask the inspector to evaluate the foundation without assuming deferred maintenance absent specific findings."
    assert ai._soften_age_condition_words(t) == t
    assert "possible repair items to verify" in ai._soften_age_condition_words("The home has deferred maintenance.")


def test_whether_original_is_a_question_not_a_claim():
    subj = {"year_built": 2001, "sqft": 5670}
    p = ("**Roof:** No roof age is documented. Ask the inspector to estimate the remaining useful life of the roof "
         "and confirm whether it is original or has been replaced.")
    blocks = ai._factual_prose_blocking(p, "Inspection concerns", subj)
    assert not any("original" in b for b in blocks)


def test_stucco_on_a_2001_home_gets_moisture_testing_advice():
    subj = {"year_built": 2001, "construction_materials": "Masonry, Stone, Stucco", "exterior": "Stone, Stucco"}
    out = ai._stucco_inspection_paragraph(subj) or ""
    assert "2001" in out and "moisture test" in out and "only if" not in out


def test_lower_level_item_says_only_what_the_listing_says():
    bath_only = {"public_remarks": "The finished basement has a full bathroom and a rec room."}
    out = ai._ensure_lower_level_inspection_item("**Roof:** Check it.", bath_only)
    assert "egress" not in out and "bedroom" not in out and "full bathroom" in out


def test_record_saying_no_damage_is_not_documented_damage():
    assert not ai._record_documents_damage("seller reports no water damage; roof replaced 2019")
    assert ai._record_documents_damage("sold as-is, needs repair to the porch")


def test_new_200_amp_service_is_not_a_dated_system():
    assert not ai._documented_original_systems({"public_remarks": "New 200 amp service and new HVAC in 2022."})
    assert ai._documented_original_systems({"public_remarks": "Original 100 amp service."})


# ---- under-contract offer ------------------------------------------------------------------------

def test_under_contract_offer_says_backup_once():
    tl = {"is_under_contract": True, "active_market_days": 8}
    body = ("The seller has already accepted an offer after 8 days on the market, so a backup offer only comes into play "
            "if that contract falls through. Given the home is already under contract, this analysis should also serve "
            "as a backup-offer benchmark should the current contract fall through.")
    out, _ = ai._status_aware_offer_guidance(body, tl)
    assert out.lower().count("benchmark") == 0 and "only comes into play" in out
    assert "not a sign of weak demand" not in out


# ---- valuation prose -----------------------------------------------------------------------------

def test_asking_gap_not_restated_after_the_value_bridge():
    m = "The value is supported by three sales. How the value was reached: the adjusted comparable sales point to about $1,514,000."
    trace = {"asking_price_check": {"asking_price": 1325000}, "anchor": {"anchor_value": 1514000, "loose_fit": True}}
    out, _ = ai._v10_asking_gap_sentence(m, final={"anchor_value": 1514000}, trace=trace)
    assert "12% below" not in out and "rough guide" in out


def test_pct_below_list_ignores_the_list_price_itself():
    t = "At $3,050,000 vs the $3,199,000 list, that is 4.7% below list."
    out = ai._fix_pct_below_list(t, 3199000, None, None)
    assert "0.0" not in out and "4.7% below list" in out


def test_hedge_only_firmed_in_the_comps_rated_direction():
    comps = [{"street_address": "1 A ST", "quality": {"finish_quality": "below"}}]
    t = "Comp #1 may be superior in finish."
    assert ai._firm_up_hedges(t, comps)[0] == t
    comps_up = [{"street_address": "1 A ST", "quality": {"finish_quality": "above"}}]
    assert "is superior" in ai._firm_up_hedges(t, comps_up)[0]
    # about the subject, not the comp - never firmed
    t2 = "Next to Comp #1 the subject may be superior in finish."
    assert ai._firm_up_hedges(t2, comps_up)[0] == t2


def test_engine_clause_filter_keeps_ordinary_anchored():
    t = "The family room is anchored by a stone fireplace, and the kitchen opens to the deck."
    assert ai._drop_engine_clauses(t) == t


# ---- comp facts / features -----------------------------------------------------------------------

def test_community_pool_is_not_a_private_pool():
    sherbrook = {"public_remarks": "Walk to the community pool and clubhouse in Claremont Village.", "pool": ""}
    assert not [k for k, _ in fc.high_impact_features(sherbrook) if k == "pool"]
    private = {"public_remarks": "Heated inground pool with new liner.", "pool": ""}
    assert [k for k, _ in fc.high_impact_features(private) if k == "pool"]


def test_parallel_sentences_are_not_repeats():
    t = ("Ask the inspector to verify the roof's age, condition and remaining service life. "
         "Ask the inspector to verify the furnace's age, condition and remaining service life.")
    out, flags = fc.drop_repeated_sentences(t)
    assert "roof" in out and "furnace" in out and not flags
    dup = ("The supported range tops out at the asking price on purpose. "
           "The supported range tops out right at the asking price on purpose.")
    assert fc.drop_repeated_sentences(dup)[1]


def test_adjustment_family_uses_whole_words():
    assert ai._v10_adj_family("Extra storage space", "more space in the lower level") != "pool_spa"
    assert ai._v10_adj_family("Landscaped yard", "professionally landscaped") != "lot_land"
    assert ai._v10_adj_family("Pool and spa", "heated pool with spa") == "pool_spa"


def test_school_district_not_strong_survives_reweighting():
    subj = {"school_district": "DOWNINGTOWN AREA"}
    comp = {"school_district": "WEST CHESTER AREA", "strength": "strong"}
    assert ai._same_district(comp["school_district"], subj["school_district"]) is False


def test_mojibake_dash_fixed_in_range_pattern():
    src = open(ai.__file__, encoding="utf-8").read()
    assert "â€" not in src
