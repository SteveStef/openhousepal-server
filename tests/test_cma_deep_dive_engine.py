"""Deep dive 11/13 (Fable review of comp scoring, the adjustment math, and the advice text). Each test is a
case a reviewer ran and saw go wrong."""
import sys
from datetime import date
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as svc  # noqa: E402
from app.services import cma_fact_check as fc  # noqa: E402


# ---- condition read from the listing description ------------------------------------------------------
def _tier(text):
    return svc._condition_tier({"public_remarks": text})


def test_a_renovated_home_is_not_read_as_a_fixer_upper_from_boilerplate():
    assert _tier("Completely renovated top to bottom in 2025. Property is being sold as-is.") == 4.0
    assert _tier("Gut renovated rowhome, turn-key. Great investment opportunity with 10-year tax abatement.") == 4.0
    assert _tier("Fully renovated colonial with new kitchen. Priced to sell!") == 4.0
    assert _tier("Great investment opportunity, fully renovated.") == 4.0
    assert _tier("Meticulously maintained and updated home. This is an estate sale.") == 3.0
    assert _tier("New construction townhome near the Shell station") == 4.0


def test_real_work_needed_still_reads_low_and_mixed_messages_read_as_unknown():
    assert _tier("Investor special, sold as-is.") == 0.5
    assert _tier("Needs TLC, bring your contractor. Sold as-is.") == 1.5
    assert _tier("Sold as-is.") == 0.5
    assert _tier("Fully renovated kitchen, but the second floor needs a full gut.") is None
    # sale-type words still count when nothing describes a renovation - the free past-sales check (11/13) was
    # less accurate without them
    assert _tier("Priced to sell! Estate sale.") == 1.5
    assert _tier("Great investment opportunity near the train.") == 0.5


# ---- condition counted once ----------------------------------------------------------------------------
def test_grid_condition_dollars_are_weighted_and_signed():
    anchor = {"comps": [{"normalized_weight": 0.5, "feature_adjustments": {"condition": -30000}},
                        {"normalized_weight": 0.5, "feature_adjustments": {"condition": -20000}}]}
    assert svc._grid_condition_delivered(anchor) == 25000        # comps moved DOWN $25K on average
    assert svc._grid_condition_delivered({"comps": [{"normalized_weight": 1.0, "feature_adjustments": {}}]}) == 0


# ---- size basis and basement estimate -------------------------------------------------------------------
def test_blank_above_grade_with_a_basement_on_record_does_not_count_the_basement_twice():
    assert svc._v10_sqft({"sqft": 2800, "below_grade_finished_area": 800}) == 2000
    assert svc._v10_sqft({"sqft": 2800, "above_grade_finished_area": 2000, "below_grade_finished_area": 800}) == 2000
    assert svc._v10_sqft({"sqft": 2800}) == 2800


def test_unrecorded_finished_basement_is_half_the_footprint_not_half_the_house():
    rowhome = {"basement": "Fully Finished", "levels": ["3"], "property_type": "TOWNHOUSE"}
    assert svc._v10_bg_for_grid(rowhome, 1800) == (300, True)                 # was 900: 1.5x its 600 sq ft footprint
    assert svc._v10_bg_for_grid({"basement": "Fully Finished", "levels": ["1"]}, 1500) == (750, True)
    assert svc._story_count({"property_type": "TOWNHOUSE"}) == 2.0 and svc._story_count({"property_type": "SINGLE_FAMILY"}) == 1.0


# ---- outliers, duplicates -----------------------------------------------------------------------------------
def _c(key, price, area):
    return {"listing_key": key, "sale_price": price, "above_grade_finished_area": area, "price_verified": True}


def test_the_outlier_check_reads_the_stored_price_per_foot_with_no_minimum_gap():
    # The deep dive changed this to above-grade $/sq ft with a 15% minimum gap; the free check on 60 past sales was
    # LESS accurate that way, so it was put back: the comp furthest from a tight pack is still trimmed.
    comps = [dict(_c(k, 0, 0), price_per_sqft=p) for k, p in (("a", 200), ("b", 201), ("c", 202), ("d", 215))]
    assert list(svc._flag_psf_outliers(comps)) == ["d"]


def test_a_real_outlier_is_still_flagged():
    comps = [dict(_c(k, 0, 0), price_per_sqft=p) for k, p in (("a", 200), ("b", 201), ("c", 202), ("d", 310))]
    assert "d" in svc._flag_psf_outliers(comps)


def test_two_sales_on_the_same_street_are_not_duplicates_unless_price_and_date_match():
    a = {"key": "a", "address": "12 Elm St", "psf": 250.0, "sqft": 2000, "sale_date": "2026-03-01", "_c": {"latitude": 40.0, "longitude": -75.0}}
    b = {"key": "b", "address": "20 Elm St", "psf": 290.0, "sqft": 2400, "sale_date": "2026-08-01", "_c": {"latitude": 40.0005, "longitude": -75.0}}
    assert svc._v10_redundancy_penalties([a, b]) == {"a": 1.0, "b": 1.0}
    twin = {**b, "key": "t", "psf": 252.0, "sale_date": "2026-03-10"}
    pen = svc._v10_redundancy_penalties([a, twin])
    assert pen["a"] < 1.0 and pen["t"] < 1.0


# ---- market area: mailing city ------------------------------------------------------------------------------
def test_in_city_mode_a_different_mailing_city_still_zeroes_the_area_score():
    # Letting it through when the township matches was tried (deep dive 11/13) and reverted: on 60 past sales it
    # was less accurate - the mailing town carries a real price difference on the Main Line.
    subject = {"city": "Ardmore", "township": "Lower Merion", "school_district": "Lower Merion", "zipcode": "19003", "subdivision_name": "Ardmore Park"}
    comp = {"city": "Wynnewood", "township": "Lower Merion", "school_district": "Lower Merion", "zipcode": "19003", "subdivision_name": "Ardmore Park"}
    assert svc._relevance_market_area_score(subject, comp, urban=True)[0] == 0.0
    same = {**comp, "city": "Ardmore"}
    assert svc._relevance_market_area_score(subject, same, urban=True)[0] > 0.8


# ---- asking-price check ---------------------------------------------------------------------------------------
COMPS = [{"sale_price": 500000, "sold_list_ratio": 1.06, "dom": None}, {"sale_price": 510000, "sold_list_ratio": 1.05, "dom": None},
         {"sale_price": 505000, "sold_list_ratio": 1.07, "dom": None}]


def test_passed_over_needs_real_comp_days_on_market():
    check = svc._asking_price_market_check({"current_list_price": 500000, "dom": 16}, {}, {"state": "normal_exposure"}, COMPS)
    assert check["passed_over"] is False


def test_a_home_under_contract_is_not_passed_over_or_long_unsold():
    check = svc._asking_price_market_check({"current_list_price": 500000, "dom": 130}, {"is_under_contract": True},
                                           {"state": "under_contract_normal", "comp_median_dom": 20}, COMPS)
    assert check["passed_over"] is False and check["long_unsold"] is False


# ---- an old closed sale is history ---------------------------------------------------------------------------
def test_an_old_closing_is_not_todays_contract_price():
    old = svc._compute_exposure({"last_sale_price": 340000, "list_price": 350000}, {"is_closed": True, "close_date": "2023-06-01"}, 20, {"anchor_value": 500000})
    assert old["verified_contract_price"] is None and not old.get("exposure_ceiling")
    recent_day = (svc._tmach.today()).isoformat()
    new = svc._compute_exposure({"last_sale_price": 340000}, {"is_closed": True, "close_date": recent_day}, 20, {"anchor_value": 500000})
    assert new["verified_contract_price"] == 340000


# ---- advice wording ---------------------------------------------------------------------------------------------
def test_seller_gap_is_a_share_of_the_value():
    kw = dict(range_min=480000, range_max=520000, status_timeline={"active_market_days": 10}, comps=[])
    assert "about 20% above the supported value" in svc._seller_pricing_position(value=500000, subject_asking=600000, **kw)
    low = svc._seller_pricing_position(value=500000, subject_asking=400000, **kw)
    assert "about 20% below the supported value" in low and "listing at $" not in low


def test_backup_offer_wording_matches_the_numbers():
    band = svc._v10_build_buyer_offer_band(497000, 500000, trace={}, confidence="high",
                                           status_timeline={"is_under_contract": True, "active_market_days": 150},
                                           range_min=480000, range_max=515000)
    text = svc._v10_rebuild_buyer_offer_prose("", band, 4.0)[0]
    assert "quick contract" not in text and "above the asking price" not in text


def test_walk_away_never_rounds_up_past_the_value_on_a_passed_over_home():
    tr = {"asking_price_check": {"passed_over": True}, "exposure": {"state": "extended_exposure", "active_market_days": 60}}
    band = svc._v10_build_buyer_offer_band(648000, 675000, trace=tr, confidence="high", status_timeline={"active_market_days": 60})
    assert band["comfort_ceiling"] <= 648000
    assert "stretch" not in svc._v10_rebuild_buyer_offer_prose("", band, 5.0)[0]


# ---- days on market stop at the contract -------------------------------------------------------------------
def test_history_days_stop_when_the_home_goes_under_contract():
    events = [{"date": "2026-08-13", "event": "Listed for sale", "price": 450000},
              {"date": "2026-10-06", "event": "Taken off market", "price": 450000},
              {"date": "2026-10-09", "event": "Listed for sale", "price": 450000},
              {"date": "2026-10-20", "event": "Under contract", "price": 450000}]
    h = fc.summarize_history(events, current_list_date="2026-10-09", today=date(2026, 11, 13))
    assert h["current_listing_days"] == 11 and h["marketing_days_total"] == 65          # was 35 and 89


def test_no_present_tense_history_sentence_for_a_home_that_is_not_listed():
    summary = {"relisted": True, "marketing_days_total": 206, "earlier_listings": [{"listed": "2024-02-01", "ended": "2024-05-01"}],
               "current_listed": "2024-05-10"}
    assert fc.history_sentence("The value rests on five sales.", summary, listed=False) == "The value rests on five sales."
    assert "206 days in total" in fc.history_sentence("The value rests on five sales.", summary, listed=True)


# ---- last batch ---------------------------------------------------------------------------------------------
def test_comps_that_agree_on_price_are_not_one_comp_counted_three_times():
    def comp(key, psf, lat, day):
        return {"listing_key": key, "strength": "strong", "price_per_sqft": psf, "distance_miles": 0.5,
                "latitude": lat, "longitude": -75.3, "close_date": day}
    spread_out = [comp("a", 200, 40.00, "2026-03-01"), comp("b", 204, 40.01, "2026-05-15"), comp("c", 207, 39.99, "2026-07-20")]
    assert svc._redundancy_adjusted_count(spread_out) == 3.0
    clones = [comp("a", 200, 40.0000, "2026-03-01"), comp("b", 202, 40.0005, "2026-03-10")]
    assert svc._redundancy_adjusted_count(clones) == 1.0


def test_two_sold_to_list_ratios_that_disagree_give_no_asking_check():
    comps = [{"sale_price": 485000, "sold_list_ratio": 0.97, "dom": 20}, {"sale_price": 605000, "sold_list_ratio": 1.21, "dom": 3}]
    assert svc._asking_price_market_check({"current_list_price": 500000, "dom": 5}, {}, {"state": "normal_exposure"}, comps) is None
    agree = [{"sale_price": 485000, "sold_list_ratio": 0.97, "dom": 20}, {"sale_price": 495000, "sold_list_ratio": 0.99, "dom": 12}]
    assert svc._asking_price_market_check({"current_list_price": 500000, "dom": 5}, {}, {"state": "normal_exposure"}, agree)


def test_seller_report_never_prints_an_old_list_price_as_the_last_sale_price():
    subj, tl = svc._seller_not_listed("seller", {"list_price": 400000}, {"is_closed": True, "close_date": "2019-06-01", "current_status": "Closed"})
    text = " ".join(tl.get("summary_lines") or [])
    assert "Last sold 2019-06-01" in text and "$400,000" not in text


def test_a_comp_sentence_is_not_rewritten_for_a_hand_entered_subject():
    s = "Comp #1's sale price is verified in the MLS data."
    assert fc.fix_manual_subject_record(s, {"source": "manual"}, None)[0] == s


# ---- sold, then dropped by Bright: still a sale -----------------------------------------------------------
def test_a_sale_bright_later_removed_is_still_read_as_sold():
    from app.services import cma_service as cs
    raw = {"MlsStatus": "CLOSED-BRIGHT", "ClosePrice": 625000, "CloseDate": "2026-10-19"}
    assert cs._sold_then_removed("OFF_MARKET-BRIGHT", raw)
    assert not cs._sold_then_removed("OFF_MARKET-BRIGHT", {"MlsStatus": "WITHDRAWN-BRIGHT", "ClosePrice": None})
    assert not cs._sold_then_removed("OFF_MARKET-BRIGHT", {"MlsStatus": "CLOSED-BRIGHT", "ClosePrice": 0, "CloseDate": "2026-10-19"})
    assert not cs._sold_then_removed("ACTIVE-BRIGHT", raw)


def test_subject_status_notes_are_one_helper_for_both_runs():
    tl = {"is_active": False, "is_under_contract": True, "active_market_days": 30, "current_status": "Pending",
          "under_contract_date": "2026-10-20"}
    flags = svc._subject_status_flags({}, tl, 500000, 510000)
    assert len(flags) == 1 and "not evidence of weak demand" in flags[0] and "2026-10-20" in flags[0]


def test_offer_check_accepts_the_engines_own_at_or_up_to_phrase():
    band = svc._v10_build_buyer_offer_band(812000, 800000, trace={"anchor": {"comp_list_to_sale_median": 1.04}}, confidence="high",
                                           status_timeline={"active_market_days": 3}, range_min=771000, range_max=853000)
    text = svc._v10_rebuild_buyer_offer_prose("", band, 5.0)[0]
    assert "at or up to about 1% above the list price" in text
    assert svc._v10_offer_sanity_check(text, band, 771000, 853000) == []


def test_percent_below_list_ignores_comp_prices_from_another_clause():
    out = svc._fix_pct_below_list("Comp #1 closed at $655,000 and Comp #2 at $690,000; the concluded value is about 5% below list.",
                                  675000, 641000, 641000)
    assert "5.0% below list" in out and "2.2" not in out
