"""Outside audit of the five 11/14 reports (80/100). The findings are treated as engine rules, not address patches."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as svc  # noqa: E402


# ---- finding 3 / 9: one factor, one dollar step -------------------------------------------------------------

def _packet(anchor, check=None):
    return {"deterministic_evidence": {"deterministic_anchor": anchor, "anchor_low": anchor * 0.9, "anchor_high": anchor * 1.1},
            "asking_price_check": check}


_STURBRIDGE_CHECK = {"asking_price": 1875000, "expected_sale_price": 1837500, "allowed_low": 0, "allowed_high": 1837500,
                     "ratio_used": 0.98, "passed_over": True, "buyer_report": True}


def test_market_time_is_not_deducted_twice():
    # 701 Sturbridge Dr: -$17,000 through the asking-price check for time on the market, then -$18,000 more
    # "because the market time is longer than the comparable sales"
    reasoning = {"final_fmv": 1820000, "range_low": 1660000, "range_high": 1980000,
                 "major_value_drags": [{"factor": "Market time is longer than the comparable sales", "direction": "down"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(1854857, _STURBRIDGE_CHECK))
    assert out["final_fmv"] == 1837000                       # the asking-price step only, inside the band
    assert out["judgment_discipline"]["why"].startswith("market time is already counted")
    assert flags and "judgment step" in flags[0]
    # the range moves with the value and still brackets it
    assert out["range_low"] < out["final_fmv"] < out["range_high"] and out["range_high"] - out["range_low"] >= 320000
    # the value steps now show one line
    trace = {"deterministic_anchor": 1854857, "reasoning_model": out,
             "asking_price_check": {**_STURBRIDGE_CHECK, "allowed_low_price": 0, "allowed_high_price": 1837500}}
    bridge = svc._v10_value_bridge(trace, value=out["final_fmv"], range_min=out["range_low"], range_max=out["range_high"])
    assert [s["key"] for s in bridge["steps"]] == ["listing"]


def test_a_judgment_for_something_the_sales_cannot_measure_stays():
    # 50 S 16th St #4407: +$19,000 for the corner exposure and City Hall views (3.2%) - labelled judgment, kept
    reasoning = {"final_fmv": 612000, "range_low": 570000, "range_high": 650000,
                 "major_value_drivers": [{"factor": "Corner exposure with City Hall views", "direction": "up"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(592591))
    assert out["final_fmv"] == 612000 and flags == []


def test_buyer_demand_is_never_its_own_step():
    # 735 Concord Ave: +$3,000 for a "brisk local market" - every closed sale price already holds it
    reasoning = {"final_fmv": 462000, "range_low": 430000, "range_high": 495000,
                 "major_value_drivers": [{"factor": "Brisk local market", "direction": "up"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(444638))
    assert out["final_fmv"] == 445000 and "buyer demand" in out["judgment_discipline"]["why"]


def test_a_step_under_one_percent_is_dropped():
    # 524 Wales Rd: +$2,000 on $456,000
    reasoning = {"final_fmv": 458000, "range_low": 438000, "range_high": 480000,
                 "major_value_drivers": [{"factor": "Renovated kitchen and baths", "direction": "up"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(456129))
    assert out["final_fmv"] == 456000 and "under 1%" in out["judgment_discipline"]["why"]


def test_an_unsold_earlier_listing_is_a_ceiling_not_a_deduction():
    # 418 Reedville Rd: adjusted sales $771,000, then -$11,000 "prior listing at $835,750 ended without a sale"
    reasoning = {"final_fmv": 760000, "range_low": 720000, "range_high": 800000,
                 "major_value_drags": [{"factor": "Prior listing at 835,750 ended without a sale after about 182 days"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(771110))
    assert out["final_fmv"] == 771000 and "ceiling" in out["judgment_discipline"]["why"]
    assert out["range_low"] == 730000 and out["range_high"] == 815000


def test_a_real_mixed_judgment_is_left_alone():
    reasoning = {"final_fmv": 730000, "range_low": 690000, "range_high": 770000,
                 "major_value_drags": [{"factor": "One-car garage against larger garages"}, {"factor": "Prior listing ended without a sale"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(771110))
    assert out["final_fmv"] == 730000 and flags == []


def test_a_value_set_by_the_asking_price_band_is_not_touched_again():
    check = {"asking_price": 600000, "expected_sale_price": 582000, "allowed_low": 523800, "allowed_high": 611100,
             "ratio_used": 0.97, "passed_over": False, "buyer_report": False}
    reasoning = {"final_fmv": 480000, "range_low": 450000, "range_high": 510000,
                 "major_value_drags": [{"factor": "Days on market"}]}
    out, flags = svc._apply_value_guards(reasoning, _packet(480000, check))
    assert out["final_fmv"] == 524000 and "judgment_discipline" not in out


# ---- finding 5 / 6: wording follows confidence and how well the sales fit ------------------------------------

_QUICK_COMPS = [{"status": "sold", "sale_price": 600000, "list_price": 585000, "days_on_market": 4, "is_included": True, "price_verified": True},
                {"status": "sold", "sale_price": 585000, "list_price": 570000, "days_on_market": 4, "is_included": True, "price_verified": True},
                {"status": "sold", "sale_price": 715000, "list_price": 720000, "days_on_market": 14, "is_included": True, "price_verified": True}]


def test_low_confidence_seller_report_gives_the_range_first_not_a_precise_price():
    tl = {"not_listed": True}
    sure = svc._seller_pricing_position(value=760000, range_min=720000, range_max=800000, subject_asking=None,
                                        status_timeline=tl, comps=_QUICK_COMPS, confidence="high")
    assert "We recommend listing at about $760,000" in sure
    unsure = svc._seller_pricing_position(value=760000, range_min=720000, range_max=800000, subject_asking=None,
                                          status_timeline=tl, comps=_QUICK_COMPS, confidence="low")
    assert "support a range of $720,000 to $800,000" in unsure and "starting point rather than a precise figure" in unsure
    assert "We recommend listing at about" not in unsure
    # loosely fitting sales have the same effect, whatever the confidence label
    loose = svc._seller_pricing_position(value=760000, range_min=720000, range_max=800000, subject_asking=None,
                                         status_timeline=tl, comps=_QUICK_COMPS, confidence="medium", loose_fit=True)
    assert "starting point rather than a precise figure" in loose


def test_quick_sales_of_other_kinds_of_homes_do_not_promise_a_quick_sale():
    tl = {"not_listed": True, "last_listing": {"price": 835750, "days": 182, "ended": "2026-11-13"}}
    text = svc._seller_pricing_position(value=760000, range_min=720000, range_max=800000, subject_asking=None,
                                        status_timeline=tl, comps=_QUICK_COMPS, confidence="low")
    assert "creates that competition" not in text
    assert "a different kind of home" in text and "takes longer" in text
    # the unsold listing is still stated, in the record's own terms
    assert "came off the market in November 2026" in text and "with no sale recorded" in text


# ---- finding 1 / 8: under-contract evidence must be current ---------------------------------------------------

def test_an_old_pending_is_not_recent_evidence():
    from datetime import date, datetime, timezone
    from app.services import cma_service as cs
    today = date(2026, 11, 14)
    # 659 Aronimink Pl: last MLS change May 12 - 186 days
    assert cs.pending_is_stale(None, datetime(2026, 5, 12, tzinfo=timezone.utc), today)
    # 375 Wickmere St (to-be-built): contract Apr 30, last change May 1
    assert cs.pending_is_stale("2026-04-30", datetime(2026, 5, 1, tzinfo=timezone.utc), today)
    # 524 Wales Rd: contract Oct 26
    assert not cs.pending_is_stale("2026-10-26", datetime(2026, 11, 4, tzinfo=timezone.utc), today)
    # an old contract date on a record the MLS touched last month is still live
    assert not cs.pending_is_stale("2026-05-01", datetime(2026, 10, 20, tzinfo=timezone.utc), today)
    assert not cs.pending_is_stale(None, None, today)


def test_acreage_subject_does_not_compete_with_a_home_that_has_no_lot_on_record():
    assert svc._pending_land_mismatch({"lot_size": 7.5 * 43560}, {"street_address": "375 WICKMERE ST"})
    assert not svc._pending_land_mismatch({"lot_size": 0.3 * 43560}, {"street_address": "X"})


def test_agent_is_asked_to_confirm_a_listing_that_left_the_mls_days_ago():
    from datetime import timedelta
    today = svc._tmach.today()
    note = svc._recent_status_review({"last_listing": {"price": 835750, "ended": (today - timedelta(days=1)).isoformat()}})
    assert note and "off the market yesterday" in note["message"] and "$835,750" in note["message"] and "Confirm with the owner" in note["message"]
    assert svc._recent_status_review({"last_listing": {"price": 499500, "ended": (today - timedelta(days=130)).isoformat()}}) is None
    assert svc._recent_status_review({"last_listing": {"price": 499500, "ended": None}}) is None
    assert svc._recent_status_review({}) is None


# ---- the owner's live report, 239 Windham Dr (outside audit 78/100) ---------------------------------------------

def test_doubled_house_number_is_shown_once():
    from app.schemas.cma import CmaComparable
    c = CmaComparable(id="1", source="mls", street_address="523-523 SHERBROOK DR")
    assert c.street_address == "523 SHERBROOK DR"
    # a real range, a unit and an empty unit are untouched / handled as before
    assert CmaComparable(id="2", source="mls", street_address="1414-00 S PENN SQ #27A").street_address == "1414-00 S PENN SQ #27A"
    assert CmaComparable(id="3", source="mls", street_address="601 ARGYLE CIR #").street_address == "601 ARGYLE CIR"


def test_a_sentence_that_repeats_part_of_an_earlier_one_is_dropped():
    from app.services import cma_fact_check as fc
    text = ("Four of the five comparable sales went at least 2% over their asking price, most within 5 days, but this home "
            "has now been on the market longer than that without an accepted offer, which suggests it isn't drawing the same "
            "competition, so opening below asking is reasonable. The value is an estimate, not a precise figure. This home "
            "has now been on the market longer than that without an accepted offer, which suggests it isn't drawing the same "
            "competition, so opening below asking is reasonable. Keep a standard inspection contingency.")
    out, flags = fc.drop_repeated_sentences(text)
    assert out.count("has now been on the market longer than that") == 1 and flags
    assert "Keep a standard inspection contingency." in out and "The value is an estimate" in out


def test_seller_rent_back_reaches_the_buyer_offer_note():
    assert "seller rent-back" in svc._possession_note(["Seller Rent Back"])
    assert svc._possession_note(["Settlement"]) is None and svc._possession_note(None) is None
    band = {"fmv": 1095000, "list_price": 1300000, "opening_low": 1020000, "opening_high": 1060000, "target": 1090000,
            "comfort_ceiling": 1095000, "basis_ref": 1300000, "confidence": "medium", "valuation_range_low": 1025000,
            "valuation_range_high": 1175000, "possession_note": svc._possession_note(["Seller Rent Back"])}
    out, _ = svc._v10_rebuild_buyer_offer_prose("Keep a standard inspection contingency.", band, 6.8)
    assert out.count("seller rent-back after closing") == 1 and "how long the seller stays" in out


def test_demand_is_not_named_as_a_reason_for_a_judgment_step():
    text, _, _ = svc._bridge_driver_text([{"factor": "2025 roof, whole-house generator and water softener"},
                                          {"factor": "Strong local demand"}], set())
    assert "roof" in text and "demand" not in text.lower()


def test_only_reasons_the_table_does_not_price_decide_the_step():
    # 418 Reedville Rd: the garage is already in the adjustment table, so the one reason left is the unsold listing
    packet = _packet(771110)
    packet["deterministic_evidence"]["comp_adjustment_grid"] = [{"comp_label": "Comp #1", "garage": -10725, "lot": 0},
                                                                {"comp_label": "Comp #2", "garage": -8550, "lot": 111365}]
    reasoning = {"final_fmv": 760000, "range_low": 720000, "range_high": 800000,
                 "major_value_drags": [{"factor": "One-car garage against larger garages elsewhere"},
                                       {"factor": "Prior listing at 835,750 ended without a sale after about 182 days"}]}
    out, flags = svc._apply_value_guards(reasoning, packet)
    assert out["final_fmv"] == 771000 and "ceiling" in out["judgment_discipline"]["why"]


def test_a_seller_who_accepted_an_offer_is_not_holding_out():
    from app.services import cma_fact_check as fc
    text = ("The sales support a value below the asking price. The seller may be holding out for more than the sales "
            "support. The home took longer to go under contract than the quick sales nearby.")
    out, flags = fc.fix_under_contract_holdout(text, {"mls_status": "Pending"})
    assert "holding out" not in out and "took longer to go under contract" in out and flags
    # an active listing keeps the sentence
    assert fc.fix_under_contract_holdout(text, {"mls_status": "Active"}) == (text, [])
