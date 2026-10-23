"""Golden cases from the 10/21 grader review: 239 Windham Dr (Exton), 1420 Locust St #23H
(Philadelphia condo / rental listing) and 417 Dogwood Ln (Coatesville). Pure-function checks on the
comp search plan, Match %, Strong rule and rental/sanity gates - no database."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main  # noqa: F401,E402
from app.schemas.cma import CmaCompCandidate  # noqa: E402
from app.services import cma_ai_service as ai  # noqa: E402
from app.services import cma_service as svc  # noqa: E402

TODAY = date(2026, 10, 21)
DENSITY = {"distance_half_life_miles": 1.0}

WINDHAM = {"street_address": "239 WINDHAM DR", "zipcode": "19341", "city": "Exton", "state": "PA",
           "property_type": "SINGLE_FAMILY", "year_built": 2012, "sqft": 5024, "above_grade_finished_area": 3524,
           "below_grade_finished_area": 1500, "lot_size": 25700, "school_district": "Downingtown Area",
           "township": "Uwchlan", "water_source": "Public", "sewer": "Public Sewer", "list_price": 1300000}


def _sale(**kw):
    base = {"status": "sold", "price_verified": True, "close_date": "2026-07-01", "is_included": True,
            "property_type": "SINGLE_FAMILY", "school_district": "Downingtown Area"}
    base.update(kw)
    base.setdefault("sale_price", base.get("close_price"))
    return base


WOODLAND = _sale(listing_key="woodland", street_address="281 WOODLAND DR", distance_miles=1.02, close_price=850000,
                 list_price=870000, year_built=1985, sqft=3319, above_grade_finished_area=3319, lot_size=61000,
                 water_source="Well", sewer="Septic Pump", township="Uwchlan")
LOBELLA = _sale(listing_key="lobella", street_address="4428 LOBELLA CT", distance_miles=3.23, close_price=1050000,
                list_price=1050000, year_built=2010, sqft=4387, above_grade_finished_area=3387, lot_size=24000,
                water_source="Public", sewer="Public Sewer", township="Upper Uwchlan")


# ---- B: Match % ------------------------------------------------------------------------------------

def test_windham_2010s_home_three_miles_out_outranks_1985_well_septic_home_a_mile_away():
    rel = ai._continuous_relevance(WINDHAM, [WOODLAND, LOBELLA], density_context=DENSITY)
    score = {r["listing_key"]: r["adjusted_relevance_score"] for r in rel["comps"]}
    assert score["lobella"] > score["woodland"]
    pts = rel["component_weights"]
    assert pts["age"] == 12.0 and pts["proximity"] == 14.0


def test_no_distance_penalty_inside_a_mile():
    near = _sale(listing_key="a", street_address="1 A ST", distance_miles=0.2, close_price=1e6, year_built=2012, sqft=3500)
    mile = {**near, "listing_key": "b", "street_address": "2 B ST", "distance_miles": 0.95}
    rel = ai._continuous_relevance(WINDHAM, [near, mile], density_context=DENSITY)
    prox = [r["components"]["proximity"] for r in rel["comps"]]
    assert prox == [1.0, 1.0]


def test_school_district_names_match_in_normalized_form():
    assert ai._same_district("The School District Of Philadelphia", "Philadelphia City")
    assert ai._same_district("Downingtown Area", "Downingtown Area School District")
    assert not ai._same_district("West Chester Area", "Downingtown Area")
    # 10/23: one name, two kinds of municipality = two districts
    assert not ai._same_district("EGG HARBOR CITY SCHOOLS", "EGG HARBOR TOWNSHIP PUBLIC SCHOOLS")
    assert not ai._same_district("BERLIN BOROUGH PUBLIC SCHOOLS", "BERLIN TOWNSHIP PUBLIC SCHOOLS")
    assert not ai._same_district("CHARLES CITY COUNTY PUBLIC SCHOOLS", "CHARLES COUNTY PUBLIC SCHOOLS")
    assert not ai._same_district("DISTRICT OF COLUMBIA PUBLIC SCHOOLS", "COLUMBIA BOROUGH")
    assert ai._same_district("HADDONFIELD BORO", "HADDONFIELD BOROUGH PUBLIC SCHOOLS")
    assert ai._same_district("Philadelphia City", None) is None


def test_place_names_match_across_spellings():
    # 10/23 all-DB check: one town spelled several ways scored as two places
    assert ai._same_place("City of Gaithersburg", "Gaithersburg")
    assert ai._same_place("Mt. Airy", "Mt Airy")
    assert ai._same_place("Hamilton", "Hamilton Twp")
    assert ai._same_place("Baltimore", "Baltimore City")
    assert not ai._same_place("Egg Harbor City", "Egg Harbor Township")
    assert not ai._same_place("Chevy Chase Village", "Town of Chevy Chase")
    assert ai._same_place('"None Available"', "Roxborough") is None
    assert ai._same_municipality({"township": "City of Rockville"}, {"township": "ROCKVILLE"})


def test_1985_well_septic_comp_rated_inferior_is_never_strong():
    row = {"normalized_weight": 0.23, "gross_adjustment_pct": 8,
           "quality": {"finish_quality": "below", "interior": "below", "exterior": "below"}}
    assert ai._v10_role_from_anchor_row(row, WOODLAND, WINDHAM) == "supporting"
    # same numbers, a like-for-like home -> Strong
    like = {**LOBELLA, "year_built": 2012}
    assert ai._v10_role_from_anchor_row({"normalized_weight": 0.23, "gross_adjustment_pct": 8, "quality": {}}, like, WINDHAM) == "strong"
    # 25+ years apart alone, or well/septic vs public alone, is enough
    assert ai._v10_role_from_anchor_row({"normalized_weight": 0.23, "gross_adjustment_pct": 8}, {**LOBELLA, "year_built": 1990}, WINDHAM) == "supporting"
    assert ai._v10_role_from_anchor_row({"normalized_weight": 0.23, "gross_adjustment_pct": 8},
                                        {**LOBELLA, "water_source": "Well", "sewer": "Septic"}, WINDHAM) == "supporting"


# ---- G: condos --------------------------------------------------------------------------------------

LOCUST_CONDO = {"street_address": "1420 LOCUST ST #23H", "zipcode": "19102", "city": "Philadelphia", "state": "PA",
                "property_type": "CONDO", "year_built": 1977, "sqft": 624, "lot_size": None,
                "school_district": "The School District Of Philadelphia", "list_price": 169900}


def _condo(key, street, dist, **kw):
    base = dict(listing_key=key, street_address=street, zipcode="19102", distance_miles=dist, close_price=160000,
                list_price=165000, year_built=1977, sqft=650, property_type="CONDO", school_district="Philadelphia City")
    base.update(kw)
    base["sale_price"] = base["close_price"]
    return _sale(**base)


def test_same_building_condo_sale_ranks_first_and_lot_is_not_scored():
    same = _condo("same", "1420 LOCUST ST #12B", 0.0)
    other = _condo("other", "1500 WALNUT ST #8A", 0.35)
    rel = ai._continuous_relevance(LOCUST_CONDO, [other, same], density_context={"distance_half_life_miles": 0.5})
    by = {r["listing_key"]: r for r in rel["comps"]}
    assert by["same"]["adjusted_relevance_score"] > by["other"]["adjusted_relevance_score"]
    assert by["same"]["components"]["building"] == 1.0 and by["other"]["components"]["building"] == 0.0
    assert "lot" not in by["same"]["components"]
    assert ai._building_key({"street_address": "1420 Locust St Unit 23H", "zipcode": "19102"}) == \
        ai._building_key({"street_address": "1420 LOCUST ST #4C", "zipcode": "19102-1234"})


def test_condo_search_is_condos_only():
    assert svc._FAMILY_HOME_TYPES[ai._type_family("CONDO")] == ("CONDO",)
    assert ai._type_family("TOWNHOUSE") == "attached" and svc._FAMILY_HOME_TYPES["attached"] == ("TOWNHOUSE",)


# ---- F + H: rentals and junk data ----------------------------------------------------------------

def test_rental_listing_is_detected():
    lease = {"street_address": "1420 LOCUST ST #23H", "property_type": "RESIDENTIAL_LEASE", "list_price": 1995}
    assert svc.is_lease_listing(lease)
    assert svc.is_lease_listing({"property_type": "CONDO", "list_price": 1995})          # rent-sized price
    assert not svc.is_lease_listing({"property_type": "CONDO", "list_price": 169900})
    assert not svc.is_lease_listing(WINDHAM)


def test_rental_or_junk_comps_are_not_usable_sales():
    assert not ai._usable_sale_comp(LOCUST_CONDO, _condo("lease", "100 X ST", 1, close_price=1300, list_price=1500))
    assert not ai._usable_sale_comp(LOCUST_CONDO, _condo("ratio", "814 HESSIAN AVE", 5, close_price=197500, list_price=500))
    assert not ai._usable_sale_comp(LOCUST_CONDO, {**_condo("twin", "3601 NANTON PL", 14), "property_type": "TOWNHOUSE"})
    assert ai._usable_sale_comp(LOCUST_CONDO, _condo("ok", "1420 LOCUST ST #12B", 0))


def _cand(**kw):
    base = dict(listing_key="k", status="sold", street_address="12 OAK LN", sale_price=400000, list_price=410000,
                price_verified=True, beds=3, baths=2)
    base.update(kw)
    return CmaCompCandidate(**base)


def test_comp_sanity_filter():
    assert svc._comp_sanity_problem(_cand()) is None
    assert svc._comp_sanity_problem(_cand(street_address="PA")) == "no street address"
    assert svc._comp_sanity_problem(_cand(sale_price=197500, list_price=500)) == "sale-to-list ratio out of range"
    assert svc._comp_sanity_problem(_cand(sale_price=1300, list_price=1300)) == "sale price under $10,000"
    assert svc._comp_sanity_problem(_cand(beds=4, baths=0)) == "bedrooms with no bathrooms on record"


# ---- A: the automatic search plan ------------------------------------------------------------------

def _pool(spec):
    """spec: (distance_mi, days_ago, score) -> scored candidates."""
    out = []
    for i, (d, days, score) in enumerate(spec):
        out.append(_cand(listing_key=f"c{i}", street_address=f"{i + 10} ELM ST", distance_miles=d,
                         sale_date=(TODAY - timedelta(days=days)).isoformat(), match_score=score,
                         similarity_details={"valuation_eligible": True}))
    return out


def test_one_of_a_kind_home_never_widens_past_its_limit_and_says_limited():
    # Windham-like: every match tops out in the 40s-50s -> stops at 5 mi (rural extension to 10), flagged limited
    pool = _pool([(1.0, 60, 52), (1.8, 90, 50), (2.5, 120, 48), (4.0, 300, 47), (9.0, 30, 45)])
    plan = svc.plan_auto_search(pool, "detached", today=TODAY)
    assert plan["radius_miles"] <= 10 and plan["limited"]


def test_widens_time_before_distance():
    # 5 usable sales within 1 mi, but 3 of them 8-10 months old: 12 months at 1 mi beats 6 months at 2 mi
    pool = _pool([(0.5, 60, 70), (0.7, 100, 66), (0.8, 250, 64), (0.9, 280, 62), (0.95, 300, 60), (1.6, 30, 75)])
    plan = svc.plan_auto_search(pool, "detached", today=TODAY)
    assert plan["steps"][0] == {"radius_mi": 1.0, "sold_within_days": 183, "usable": 2}
    assert plan["steps"][1]["radius_mi"] == 1.0 and plan["steps"][1]["sold_within_days"] == 365
    assert plan["sold_within_days"] == 365 and plan["radius_miles"] == 2.0   # one step past "enough"
    assert not plan["limited"]


def test_condo_search_stays_within_a_mile():
    pool = _pool([(0.0, 40, 80), (0.0, 90, 78), (0.2, 50, 70), (0.3, 70, 66), (0.4, 120, 60), (0.45, 140, 58)])
    plan = svc.plan_auto_search(pool, "condo", today=TODAY)
    assert plan["density"] == "urban" and plan["radius_miles"] <= 1.0


def test_dogwood_keeps_its_best_sale_just_past_the_first_circle():
    # 417 Dogwood Ln: enough at 1 mi, but 2702 E Kings Hwy (1.96 mi, verified) must still be in the pool
    pool = _pool([(0.27, 117, 82), (0.6, 60, 70), (0.8, 90, 66), (0.9, 100, 63), (0.95, 150, 60), (1.96, 28, 80)])
    plan = svc.plan_auto_search(pool, "detached", today=TODAY)
    assert plan["radius_miles"] >= 1.96


# ---- I: report text --------------------------------------------------------------------------------

def test_windham_comp_note_text_checks():
    comp = {**LOBELLA, "lot_size": 0.59 * 43560, "price_per_sqft": 256}
    note = ("It has a heated in-ground pool on a one-acre lot, and is a very fast-selling comp (3 days on market, "
            "sold over its list price) that. At $256/sqft it sold close to the subject's implied per-foot value. Good comp.")
    out, flags = ai._reconcile_comp_hard_facts(note, "Comp #2 (1029 WELSH AYRES WAY)", WINDHAM, comp)
    assert "0.59-acre lot" in out and "one-acre" not in out
    assert "list price)." in out and ") that." not in out
    assert "close to the subject" not in out and "Good comp." in out


def test_tidy_prose_keeps_real_sentences():
    assert ai._tidy_prose("minor concessions (, first month's fee) rather", "x")[0] == "minor concessions (first month's fee) rather"
    assert ai._tidy_prose("the comps used for that.", "x")[0] == "the comps used for that."
    assert ai._tidy_prose("a Class A. building", "x")[0] == "a Class A. building"


def test_floor_review_note_shows_both_numbers():
    trace = {"deterministic_anchor": 955315, "asking_price_check": {"allowed_low_price": 1081795, "allowed_high_price": 1272700}}
    note = ai._asking_band_review_note(trace, 1082000)
    assert note and note["comps_value"] == 955315 and note["final_value"] == 1082000 and "Review before sending" in note["message"]
    assert ai._asking_band_review_note({**trace, "deterministic_anchor": 1060000}, 1082000) is None   # within 5%
    assert ai._asking_band_review_note({**trace, "deterministic_anchor": 1000000, "asking_price_check": {"allowed_low_price": 900000}}, 1080000) is None   # 8% off, not at an edge


# ---- D: current competition -----------------------------------------------------------------------

def _home(key, status, lp, match, **kw):
    base = {"listing_key": key, "status": status, "street_address": f"{key} STEEPLEWOOD DR", "list_price": lp,
            "match_score": match, "sqft": 3500, "beds": 4, "baths": 4.5, "year_built": 2012,
            "below_grade_finished_area": 1400, "garage": "3 Car"}
    base.update(kw)
    return base


WINDHAM_TRACE = {"anchor": {"market_psf": 260.0}, "asking_price_check": {
    "allowed_low_price": 1081795, "allowed_high_price": 1272700, "asking_price": 1300000, "passed_over": True}}


def test_windham_near_identical_pending_triggers_review_flag():
    homes = [_home("102", "pending", 1225000, 82)]
    v, lo, hi, info = ai._pending_competition_review(WINDHAM, homes, 1082000, 975000, 1180000, WINDHAM_TRACE)
    assert v == 1082000                       # one pending alone never moves the value
    assert info["review"] and "Review before sending" in info["review"] and "102 STEEPLEWOOD DR" in info["review"]


def test_two_strong_pendings_nudge_at_most_two_and_a_half_percent():
    homes = [_home("1", "pending", 1300000, 80), _home("2", "pending", 1320000, 78), _home("3", "active", 900000, 90)]
    v, lo, hi, info = ai._pending_competition_review(WINDHAM, homes, 1082000, 975000, 1180000, WINDHAM_TRACE)
    assert 1082000 < v <= round(1082000 * 1.025 / 1000) * 1000
    assert info["nudged_from"] == 1082000 and info["nudge_pct"] <= 2.5
    assert hi <= 1300000                      # passed over: the range still stops at asking
    assert [p["listing_key"] for p in info["pendings_used"]] == ["1", "2"]   # the active never counts


def test_dogwood_value_stable_without_strong_pendings():
    dog = {"property_type": "SINGLE_FAMILY", "above_grade_finished_area": 1830, "sqft": 3130, "year_built": 1972}
    homes = [_home("a", "active", 520000, 80, sqft=1900), _home("b", "pending", 510000, 55, sqft=1800)]
    v, lo, hi, info = ai._pending_competition_review(dog, homes, 490000, 470000, 500000, {"anchor": {"market_psf": 258.0}})
    assert (v, lo, hi) == (490000, 470000, 500000) and info["review"] is None


def test_building_key_ignores_house_number_ranges():
    assert ai._building_key({"street_address": "1420-00 LOCUST ST #12B", "zipcode": "19102"}) == \
        ai._building_key({"street_address": "1420 LOCUST ST #23H", "zipcode": "19102"})


# ---- C: grid adjustments -----------------------------------------------------------------------------

def test_condition_line_follows_the_finish_interior_exterior_ratings():
    comp = {**WOODLAND, "quality": {"finish_quality": "below", "interior": "below", "exterior": "below"}}
    r = {"_c": comp, "price": 850000, "sqft": 3319, "ag": 3319, "bg": 0, "bg_estimated": False}
    feat = ai._v10_grid_adjustments(WINDHAM, r, 3524, 1500, False, 260.0)
    assert feat["condition"] == round(3 * 850000 * ai._V10_FEATURE_COND_PCT)
    better = {**comp, "quality": {"finish_quality": "above", "interior": "similar", "exterior": "similar"}}
    assert ai._v10_grid_adjustments(WINDHAM, {**r, "_c": better}, 3524, 1500, False, 260.0)["condition"] < 0


def test_condo_report_with_lot_na_loads():
    from app.schemas.cma import CmaQualityAssessment
    assert CmaQualityAssessment(lot="n_a").lot == "n_a"


# ---- V2 Windham (10/21) ------------------------------------------------------------------------------

V2_TRACE = {"authority": "reasoning_model", "anchor": {"anchor_value": 976734}, "deterministic_anchor": 976734,
            "asking_price_check": {"allowed_low_price": 1082900, "allowed_high_price": 1274000, "asking_price": 1300000,
                                   "passed_over": True, "ratio_used": 0.98},
            "pending_competition": {"nudge_pct": 2.5, "nudged_from": 1090000, "pendings_used": [{"listing_key": "x"}]}}


def test_v2_value_above_comps_is_explained_and_flagged():
    notes = "The final fair market value is $1,117,000. The concluded value sits below the asking price."
    out, _ = ai._v10_reconcile_anchor_direction_prose(notes, final={"value": 1117000, "anchor_value": 976734}, trace=V2_TRACE)
    assert ("Weighted together, the adjusted comparable sales point to about $977,000. How this market prices "
            "homes like this one supports a value near $1,090,000. Similar homes now under contract nearby are listed "
            "higher, which moves it up about 2.5% to $1,117,000.") in out
    steps = out.split("Weighted together")[1].split("1,117,000.")[0]
    assert "asking" not in steps   # clients never read that the asking price set the value
    assert "floor held it at about $1,082,900" in ai._asking_band_review_note(V2_TRACE, 1117000)["message"]   # agent keeps the math
    note = ai._asking_band_review_note(V2_TRACE, 1117000)
    assert note and note["comps_value"] == 976734 and "14% higher" in note["message"]


def test_v2_range_after_nudge_is_round():
    homes = [_home("1", "pending", 1300000, 80), _home("2", "pending", 1320000, 78)]
    v, lo, hi, info = ai._pending_competition_review(WINDHAM, homes, 1082000, 975000, 1175000, WINDHAM_TRACE)
    assert lo % 5000 == 0 and hi % 5000 == 0


def test_v2_comp_note_psf_on_the_card_basis():
    comp = {"sale_price": 880000, "above_grade_finished_area": 3388, "sqft": 4388}
    out, _ = ai._reconcile_comp_hard_facts("At $200.55/sqft on a larger total footprint it sold under list.", "Comp #2", WINDHAM, comp)
    assert "$260/sqft above grade" in out and "200.55" not in out
    out2, _ = ai._reconcile_comp_hard_facts("It closed at $259.74/sqft.", "Comp #2", WINDHAM, comp)
    assert "$260/sqft" in out2 and "above grade above grade" not in out2


def test_new_construction_pending_never_moves_the_value():
    homes = [_home("1", "pending", 1300000, 80, new_construction=True), _home("2", "pending", 1320000, 78, new_construction=True)]
    v, lo, hi, info = ai._pending_competition_review(WINDHAM, homes, 1082000, 975000, 1180000, WINDHAM_TRACE)
    assert v == 1082000 and info["pendings_used"] == []


def test_full_acre_phrase_is_checked():
    comp = {**LOBELLA, "lot_size": 0.59 * 43560}
    out, _ = ai._reconcile_comp_hard_facts("It has a heated pool on a full acre.", "Comp #3", WINDHAM, comp)
    assert "0.59 acres" in out and "full acre" not in out
