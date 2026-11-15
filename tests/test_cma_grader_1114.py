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


# ---- two new test reports, 11/14 late morning (1802 Ginnodo St, 701 Great Oak Ln) -------------------------------

def _closed_owner_home():
    subject = {"listing_key": "803664249080", "street_address": "701 GREAT OAK LN", "last_sale_price": 595000.0,
               "last_sale_date": "2025-04-24", "list_price": 610000.0, "source": "mls"}
    timeline = {"current_status": "Closed", "is_closed": True, "is_active": False, "close_date": "2025-04-24",
                "list_date": "2025-03-10"}
    return svc._seller_not_listed("listing", subject, timeline)


def test_seller_report_on_a_home_bought_recently_states_that_sale():
    from app.services import cma_fact_check as fc
    subj, tl = _closed_owner_home()
    hist = subj["_listing_history"]
    assert hist["last_sale"] == {"date": "2025-04-24", "price": 595000.0} and hist["last_sale_is_this_listing"]
    assert hist["lines"] == ["Sold 2025-04-24 for $595,000"]
    assert svc._property_record_facts(subj)["history_lines"] == ["Sold 2025-04-24 for $595,000"]
    import datetime
    out = fc.history_sentence("The value is $671,000.", hist, today=datetime.date(2026, 11, 14), listed=False)
    assert "It last sold on Apr 24, 2025 for $595,000." in out and "updates made since then are not reflected" in out
    assert "confirmed with the seller" not in out
    # an earlier sale found by the history lookup keeps the usual wording
    out = fc.history_sentence("The value is $671,000.", {"last_sale": {"date": "2025-04-24", "price": 595000}},
                              today=datetime.date(2026, 11, 14))
    assert "confirmed with the seller" in out


def test_agent_is_told_when_the_value_is_far_from_the_homes_own_recent_sale(monkeypatch):
    import datetime
    monkeypatch.setattr(svc._tmach, "today", lambda: datetime.date(2026, 11, 14))
    subj, tl = _closed_owner_home()
    note = svc._own_sale_review(subj, 671000)
    assert note and "$595,000" in note["message"] and "13% higher" in note["message"] and "19 months later" in note["message"]
    assert svc._subject_reviews(subj, tl, 671000)["message"].startswith("Review before sending: this home sold for $595,000")
    # in line with an ordinary market: no "% higher" note - but a 19-month-old listing still gets the reminder that
    # the home's condition is as that listing showed it (outside audit 11/14)
    stale = svc._own_sale_review(subj, 640000)
    assert stale and "higher" not in stale["message"] and "the value assumes nothing has" in stale["message"]
    # sold four months ago and valued in line: nothing to say
    recent = dict(subj, last_sale_date="2026-07-10")
    assert svc._own_sale_review(recent, 600000) is None
    # a sale found in the home's EARLIER history (a listed home, a flip) is not this note's business
    assert svc._own_sale_review({"last_sale_price": 300000, "last_sale_date": "2025-04-24",
                                 "_listing_history": {"last_sale": {"date": "2025-04-24", "price": 300000}}}, 500000) is None


def _pend(addr, lp, sqft, score=76):
    return {"status": "pending", "street_address": addr, "list_price": lp, "sqft": sqft, "match_score": score,
            "listing_key": addr, "distance_miles": 0.8, "days_on_market": 10}


def test_a_pending_that_needs_a_large_adjustment_does_not_move_the_value(monkeypatch):
    """701 Great Oak Ln: three pendings 'agreed' above the value, two of them only after 14-17% of adjusting."""
    implied = {"103 LANTANA DR": 688000, "906 UNIONVILLE WAWASET RD": 746000, "109 W CLAY CREEK LN": 789000,
               "12 NEAR ST": 700000}
    monkeypatch.setattr(svc, "_pending_implied_value", lambda subject, h, psf: implied[h["street_address"]])
    homes = [_pend("103 LANTANA DR", 725000, 2453), _pend("906 UNIONVILLE WAWASET RD", 639900, 2010, 73),
             _pend("109 W CLAY CREEK LN", 919000, 2948, 72)]
    trace = {"anchor": {"market_psf": 250}}
    v, lo, hi, info = svc._pending_competition_review({"sqft": 2342}, homes, 655000, 605000, 710000, trace)
    assert v == 655000 and info["nudge_pct"] == 0.0 and not any(r["moved_value"] for r in info["pendings_used"])
    # the agent still hears about the far-off one, without "closely matches"
    assert "906 UNIONVILLE WAWASET RD" in info["review"] and "partial match" in info["review"]
    # two that ARE close matches would move it - when the nudge is switched on (it is off since 11/14, see below)
    monkeypatch.setattr(svc, "_PENDING_NUDGE_ENABLED", True)
    homes = [_pend("103 LANTANA DR", 725000, 2453), _pend("12 NEAR ST", 715000, 2400, 80)]
    v, lo, hi, info = svc._pending_competition_review({"sqft": 2342}, homes, 655000, 605000, 710000, trace)
    assert v > 655000 and info["nudge_pct"] > 0


def test_judgment_on_something_the_table_prices_says_what_the_step_is():
    grid = [{"feature_adjustments": {"condition": -27000}, "size_adjustment": 0}]
    drags = [{"factor": "Home could use updating per its listing remarks", "direction": "down",
              "evidence_strength": "clearly_market_supported"}]
    trace = {"authority": "reasoning_model", "deterministic_anchor": 667116, "anchor": {"anchor_value": 667116, "comps": grid},
             "asking_price_check": {}, "pending_competition": {}, "reasoning_model_fmv": None,
             "reasoning_model": {"major_value_drivers": [], "major_value_drags": drags}}
    b = svc._v10_value_bridge(trace, value=655000, range_min=605000, range_max=710000)
    j = next(x for x in b["steps"] if x["key"] == "judgment")
    assert j["already_in_grid"] and j["amount"] == -12000
    assert "already partly counted" not in j["basis"] and "larger than those rates cover" in j["basis"]


def test_old_comp_flag_reads_as_a_sentence(monkeypatch):
    import datetime
    monkeypatch.setattr(svc._tmach, "today", lambda: datetime.date(2026, 11, 14))
    flags = svc._far_or_old_comp_flags([{"street_address": "302 MANOR DR", "close_date": "2026-02-27", "distance_miles": 0.13},
                                        {"street_address": "9 FAR RD", "close_date": "2026-02-27", "distance_miles": 3.2}])
    assert "sold 9 months ago - check" in flags[0] and " is sold" not in flags[0]
    assert "is 3.2 miles away and sold 9 months ago" in flags[1]


def test_a_rewritten_sale_write_up_never_opens_on_a_bare_it():
    comp = {"street_address": "702 GREAT OAK LN", "status": "sold", "sale_price": 800000, "list_price": 725000,
            "close_date": "2026-07-13", "days_on_market": 5}
    orphan = ("Its interior is materially superior to the subject: remodeled kitchen with quartz and renovated baths. "
              "The sale sets the ceiling for a fully updated home here. It sold above its list price in 5 days.")
    out = svc._lead_in_orphan_note(orphan, comp, "Comp #1 (702 GREAT OAK LN)", {})
    assert out.startswith("702 Great Oak Ln sold for $800,000 on Jul 13, 2026")
    assert "Its interior is materially superior" in out and "It sold above its list price" not in out
    # a write-up that already names the sale is left alone
    named = "Comp #2 (710 WILLOWDALE LN) is in Hickory Hill, 1.08 miles away. It sold as-is."
    assert svc._lead_in_orphan_note(named, comp, "Comp #2 (710 WILLOWDALE LN)", {}) == named


# ---- outside audit of the two new reports, 11/14 afternoon (Ginnodo 79, Great Oak 66) ----------------------------

def test_a_reused_answer_speaks_in_todays_comp_numbers():
    """701 Great Oak Ln: written when the order was 702 / 302 Manor / 710 Willowdale / 207 Whitestone / 300 Manor,
    reused after the report had re-sorted them - "Comp #2" must follow 302 Manor Dr to its new place."""
    then = ["702", "302M", "710W", "207W", "300M"]
    now = ["702", "710W", "207W", "302M", "300M"]
    raw = {"subject_estimate": {"listing_strategy": "Buyers will compare the home with Comp #1, Comp #2 and Comp #5, all of "
                                                    "which sold above list within 10 days. Comp #3 shows that a dated home "
                                                    "that lingers closes below list. Comps #3 and #4 are farther away."}}
    got = svc._lock_in_todays_order({"fp": "x", "raw": raw, "order": then}, now)
    text = got["raw"]["subject_estimate"]["listing_strategy"]
    assert "Comp #1, Comp #4 and Comp #5, all of which sold above list" in text
    assert "Comp #2 shows that a dated home" in text and "Comps #2 and #3 are farther away" in text
    assert got["order"] == now and raw["subject_estimate"]["listing_strategy"].count("Comp #2 and") == 1   # the stored copy is untouched
    # same order: handed back as it is
    same = {"fp": "x", "raw": raw, "order": now}
    assert svc._lock_in_todays_order(same, list(now)) is same
    # stored before the order was kept, and it names comps by number: not reusable
    assert svc._lock_in_todays_order({"fp": "x", "raw": raw}, now) is None
    # ...but an answer with no comp numbers in it is
    plain = {"fp": "x", "raw": {"final_fmv": 655000, "summary": "The value sits below the renovated sales."}}
    assert svc._lock_in_todays_order(plain, now) is plain
    # a different set of comps under the same fingerprint can't happen; if it does, ask again
    assert svc._lock_in_todays_order({"fp": "x", "raw": raw, "order": then}, ["702", "710W", "207W", "302M", "OTHER"]) is None


def test_lock_keeps_the_order_it_was_written_in(monkeypatch):
    import asyncio
    store = {}

    async def put(db, key, value):
        store[key] = value

    async def get(db, key):
        return store.get(key)
    monkeypatch.setattr(svc, "_reasoning_cache_put", put)
    monkeypatch.setattr(svc, "_reasoning_cache_get", get)

    async def go():
        await svc._value_lock_put(None, "r1", "estimate", "fp1", {"t": "Comp #1 and Comp #2"}, order=["a", "b"])
        assert store["cma_value_lock:estimate:r1"]["order"] == ["a", "b"]
        swapped = await svc._value_lock_get(None, "r1", "estimate", "fp1", order=["b", "a"])
        assert swapped["raw"]["t"] == "Comp #2 and Comp #1"
        assert await svc._value_lock_get(None, "r1", "estimate", "other-facts", order=["b", "a"]) is None
    asyncio.run(go())


def _great_oak_comps():
    return [{"street_address": "702 GREAT OAK LN", "sale_price": 800000, "list_price": 725000, "days_on_market": 5},
            {"street_address": "710 WILLOWDALE LN", "sale_price": 550000, "list_price": 600000, "days_on_market": 55},
            {"street_address": "207 WHITESTONE DR", "sale_price": 725000, "list_price": 725000, "days_on_market": 7},
            {"street_address": "302 MANOR DR", "sale_price": 620000, "list_price": 600000, "days_on_market": 10},
            {"street_address": "300 MANOR DR", "sale_price": 806500, "list_price": 749000, "days_on_market": 4}]


def test_a_claim_about_a_numbered_comp_must_match_its_sale_record():
    from app.services import cma_fact_check as fc
    comps = _great_oak_comps()
    wrong = ("**Pricing Position:** We recommend listing at about $655,000.\n\n"
             "**Competition:** Buyers will compare the home with the updated Fox Lee Manor sales, Comp #1, Comp #2 and Comp #5, "
             "all of which sold above list within 10 days. Position the home as the value-priced option.\n\n"
             "**Overpricing Risk:** Pricing at the level of the updated comps invites buyers to price in renovation costs, "
             "and Comp #3 shows that a dated home that lingers and is reduced closes below list.")
    out, flags = fc.fix_comp_sale_claims(wrong, comps)
    assert "Comp #2" not in out and "Comp #3" not in out and len(flags) == 2
    assert "Comp #2 did not sell above its asking price" in flags[0] and "Comp #3 did not sell below" in flags[1]
    assert "We recommend listing at about $655,000." in out and "Position the home as the value-priced option." in out
    # the same sentences with the right numbers are untouched
    right = wrong.replace("Comp #1, Comp #2 and Comp #5", "Comp #1, Comp #4 and Comp #5").replace("Comp #3 shows", "Comp #2 shows")
    assert fc.fix_comp_sale_claims(right, comps) == (right, [])
    # "within N days" on its own
    assert fc.comp_sale_claim_problem("Comp #2 went under contract within 10 days.", comps) == "Comp #2 took 55 days, not 10 or fewer"
    # a contrast, or both directions in one sentence, is not a one-way claim - left alone
    assert fc.comp_sale_claim_problem("Comp #1 sold above list, while Comp #2 sold below list.", comps) is None
    assert fc.comp_sale_claim_problem("Unlike Comp #1, Comp #2 lingered, but both sold above list eventually.", comps) is None
    # no comp named, or no sale claim: nothing to check
    assert fc.comp_sale_claim_problem("Most sales here went above asking within 10 days.", comps) is None
    assert fc.comp_sale_claim_problem("Comp #2 has a dated interior.", comps) is None


def test_the_report_text_chain_runs_the_comp_claim_check():
    from app.services import cma_fact_check as fc
    out, flags = fc.fix_text("Comp #2 sold above its asking price within 10 days. The range is wide.", subject={},
                             comps=_great_oak_comps())
    assert out == "The range is wide." and any("Comp #2" in f for f in flags)


def test_a_judgment_whose_every_reason_is_in_the_table_is_removed():
    """701 Great Oak Ln: condition came off every sale in the table, then $12,000 more "because it could use updating"."""
    packet = _packet(667116)
    packet["deterministic_evidence"]["comp_adjustment_grid"] = [{"comp_label": "Comp #1", "condition": -48000},
                                                                {"comp_label": "Comp #2", "condition": 22000}]
    reasoning = {"final_fmv": 655000, "range_low": 605000, "range_high": 705000,
                 "major_value_drags": [{"factor": "Home could use updating per its listing remarks", "direction": "down"},
                                       {"factor": "No documented system or finish updates", "direction": "down"},
                                       {"factor": "No pool or special amenities", "direction": "neutral"}]}
    out, flags = svc._apply_value_guards(reasoning, packet)
    assert out["final_fmv"] == 667000 and "already priced in the adjustment table" in out["judgment_discipline"]["why"]
    assert out["range_low"] == 615000 and out["range_high"] == 720000 and flags
    # a reason the table can't price (a creek, a view) keeps the step
    reasoning["major_value_drags"].append({"factor": "Backs to a power-line easement", "direction": "down"})
    out, flags = svc._apply_value_guards(reasoning, packet)
    assert out["final_fmv"] == 655000 and not flags


def test_seller_market_sentence_states_the_slow_sale_too():
    comps = [{"sale_price": 800000, "list_price": 725000, "days_on_market": 5, "is_included": True},
             {"sale_price": 550000, "list_price": 600000, "days_on_market": 55, "is_included": True},
             {"sale_price": 725000, "list_price": 725000, "days_on_market": 7, "is_included": True}]
    note = svc._seller_slow_sale_note(comps, 7)
    assert "one of these sales took 55 days and closed about 8% under its asking price" in note
    assert svc._seller_slow_sale_note(comps[:1] + comps[2:], 7) == ""


def test_predictions_are_reworded_as_what_the_evidence_supports():
    from app.services import cma_fact_check as fc
    out, flags = fc.fix_certain_predictions("The home has been on the market longer than most of the sales took, so competing "
                                            "offers above asking are not expected. A well-prepared listing should find ready buyers.")
    assert "competing offers above asking look less likely" in out and "should draw buyer interest" in out and flags
    assert fc.fix_certain_predictions("Offers above asking look less likely.") == ("Offers above asking look less likely.", [])


def test_a_pending_homes_adjustments_are_spelled_out():
    assert svc._pending_adjustment_text([["size", -41000.0], ["baths", 11000.0]]) == "size -$41,000, baths +$11,000"
    assert svc._pending_adjustment_text(None) == ""
    final = {"value": 655000, "range_min": 605000, "range_max": 705000}
    trace = {"pending_competition": {"nudge_pct": 0.0, "pendings_used": [
        {"street_address": "103 LANTANA DR", "list_price": 725000, "implied_value": 688000, "match_score": 76,
         "distance_miles": 0.85, "days_on_market": 20, "moved_value": False,
         "adjustments": [["size", -26000.0], ["age", -18000.0], ["baths", 7000.0]]}]}}
    out, _ = svc._v10_pending_context_sentence("The value is $655,000.", final=final, trace=trace)
    assert "with the same rates as the sales (size -$26,000, age -$18,000, baths +$7,000), that asking price implies about $688,000" in out
    # an older report without the lines keeps the plain wording
    trace["pending_competition"]["pendings_used"][0].pop("adjustments")
    out, _ = svc._v10_pending_context_sentence("The value is $655,000.", final=final, trace=trace)
    assert "Adjusted to this home for size and features, that asking price implies about $688,000" in out


def test_a_sale_taxed_far_below_this_home_says_so():
    subject = {"annual_taxes": 8579, "list_price": 675000}
    leland = {"street_address": "827 LELAND ST", "sale_price": 520000, "annual_taxes": 1889,
              "ai_commentary": "It is a 2021 build with 4 beds.", "commentary": "It is a 2021 build with 4 beds."}
    usual = {"street_address": "852 N 15TH ST", "sale_price": 540000, "annual_taxes": 7248,
             "ai_commentary": "A 2013 end unit.", "commentary": "A 2013 end unit."}
    edited = {"street_address": "9 OWN NOTE ST", "sale_price": 520000, "annual_taxes": 1500,
              "ai_commentary": "A 2020 build.", "commentary": "My own words about this sale."}
    flags = svc._append_tax_gap_notes(subject, [leland, usual, edited])
    assert "annual property taxes are $1,889, against $8,579 for this home" in leland["commentary"]
    assert "tax abatement or an old assessment" in leland["commentary"] and leland["commentary"] == leland["ai_commentary"]
    assert usual["commentary"] == "A 2013 end unit."
    assert edited["commentary"] == "My own words about this sale." and "annual property taxes" in edited["ai_commentary"]
    assert len(flags) == 2
    # a second pass adds nothing
    before = leland["commentary"]
    assert svc._append_tax_gap_notes(subject, [leland]) == [] and leland["commentary"] == before
    # the listing itself says abatement
    advertised = {"sale_price": 520000, "annual_taxes": 1889, "public_remarks": "Five years left on the tax abatement!",
                  "ai_commentary": "New build."}
    svc._append_tax_gap_notes(subject, [advertised])
    assert "its listing advertised a tax abatement" in advertised["ai_commentary"]


# ---- round 4: a Phoenixville townhouse and a King of Prussia condo (11/14 afternoon) ----------------------------

def test_a_value_is_never_printed_to_the_dollar():
    assert svc._tidy_value(465196) == 465000 and svc._tidy_value(84260) == 84500
    assert svc._tidy_value(319900) == 319900 and svc._tidy_value(614000) == 614000 and svc._tidy_value(None) is None
    out, _ = svc._apply_value_guards({"final_fmv": 465196, "range_low": 442000, "range_high": 492000}, _packet(465196))
    assert out["final_fmv"] == 465000
    # never rounded across the asking-price band
    check = {"asking_price": 470000, "expected_sale_price": 460600, "allowed_low": 0, "allowed_high": 460600,
             "ratio_used": 0.98, "passed_over": True}
    out, _ = svc._apply_value_guards({"final_fmv": 460600.4, "range_low": 440000, "range_high": 480000}, _packet(470000, check))
    assert out["final_fmv"] <= 460600


def test_small_and_bracketed_comp_numbers_follow_a_re_order():
    mapping = {1: 2, 2: 1, 4: 5, 5: 4}
    assert svc._renumber_comp_refs("Interior-unit position vs. end-unit comps (#2, #4)", mapping) == \
        "Interior-unit position vs. end-unit comps (#1, #5)"
    assert svc._renumber_comp_refs("Like comp #5, and Comps #1 and #2.", mapping) == "Like comp #4, and Comps #2 and #1."
    # no "#": a distance or a count is never touched
    assert svc._renumber_comp_refs("the comps 2 miles away and comps (4 of them)", mapping) == "the comps 2 miles away and comps (4 of them)"


def test_plain_dates_and_american_spelling():
    from app.services import cma_fact_check as fc
    out, _ = fc.fix_dates_and_spelling("Comp #1 sold for $525,000 on 2026-09-24 after 7 days. The fee is common to the neighbourhood.")
    assert out == "Comp #1 sold for $525,000 on Sep 24, 2026 after 7 days. The fee is common to the neighborhood."
    assert fc.fix_dates_and_spelling("Sold Sep 24, 2026 in a good neighborhood.") == ("Sold Sep 24, 2026 in a good neighborhood.", [])
    out, _ = fc.fix_text("It closed on 2026-08-05.", subject={}, comp={"street_address": "161 HUDSON"})
    assert "Aug 5, 2026" in out


def test_agent_review_note_uses_whole_thousands():
    trace = {"deterministic_anchor": 361041, "asking_price_check": {"allowed_high_price": 313502, "asking_price": 319900}}
    note = svc._asking_band_review_note(trace, 306000)
    assert note and "$361,000" in note["message"] and "$361,041" not in note["message"]


# ---- outside audit of round 4 (King of Prussia condo 68, Phoenixville townhouse 82) ------------------------------

def test_asking_price_step_states_the_sales_behind_it():
    st = svc._asking_dom_stat("condo", 58)
    assert st == {"n": 3350, "pct_above_ask": 8.1, "median_ratio": 0.98, "kind": "condos", "span": "45 to 89 days", "segment": "condo"}
    assert svc._asking_dom_stat("house", 120)["span"] == "90 days or more" and svc._asking_dom_stat("house", 5) is None
    grid = [{"feature_adjustments": {"condition": -16400}, "size_adjustment": 0}]
    apc = {"asking_price": 319900, "expected_sale_price": 313502, "allowed_low_price": 266477, "allowed_high_price": 313502,
           "ratio_used": 0.98, "comp_median_sold_to_list_ratio": 0.988, "passed_over": True, "dom_used": 58, "home_kind": "condo"}
    trace = {"authority": "reasoning_model", "deterministic_anchor": 361041, "anchor": {"anchor_value": 361041, "comps": grid},
             "asking_price_check": apc, "pending_competition": {}, "reasoning_model_fmv": 335000,
             "reasoning_model": {"major_value_drivers": [], "major_value_drags": [],
                                 "asking_price_check_applied": {"from": 335000, "to": 313500}}}
    b = svc._v10_value_bridge(trace, value=313500, range_min=300000, range_max=365000)
    step = next(x for x in b["steps"] if x["key"] == "listing")
    assert "3,350 condos in our MLS data took 45 to 89 days to sell" in step["basis"]
    assert "median of 98% of their final asking price" in step["basis"] and "only about 8% closed above it" in step["basis"]
    assert "rarely sell for more than" not in step["basis"] and "$313,500" in step["basis"]


def test_a_pending_with_a_very_different_monthly_fee_does_not_move_the_value(monkeypatch):
    implied = {"10502 VALLEY FORGE CIR #502": 296000, "20838 VALLEY FORGE CIR #838": 261000, "10111 VALLEY FORGE CIR #111": 300000}
    monkeypatch.setattr(svc, "_pending_implied_value", lambda subject, h, psf: implied[h["street_address"]])
    subject = {"sqft": 1170, "association_fee": 1018, "association_fee_frequency": "Monthly", "home_type": "CONDO"}
    assert svc._monthly_fee_total(subject) == 1018
    homes = [dict(_pend("10502 VALLEY FORGE CIR #502", 289900, 1170, 84)),
             dict(_pend("20838 VALLEY FORGE CIR #838", 270000, 1254, 78), monthly_fee=681)]
    trace = {"anchor": {"market_psf": 300}}
    v, lo, hi, info = svc._pending_competition_review(subject, homes, 313500, 300000, 365000, trace)
    assert v == 313500 and info["nudge_pct"] == 0.0
    assert info["fee_mismatch"][0]["monthly_fee"] == 681 and info["fee_mismatch"][0]["subject_monthly_fee"] == 1018
    assert [p["street_address"] for p in info["pendings_used"]] == ["10502 VALLEY FORGE CIR #502"]
    # same fee (or a fee nobody recorded): a close match again (it would move the value with the nudge switched on)
    monkeypatch.setattr(svc, "_PENDING_NUDGE_ENABLED", True)
    homes[1] = dict(_pend("10111 VALLEY FORGE CIR #111", 295000, 1170, 80), monthly_fee=1018)
    v, lo, hi, info = svc._pending_competition_review(subject, homes, 313500, 300000, 365000, trace)
    assert v < 313500 and not info.get("fee_mismatch")
    assert svc._fee_mismatch(1018, 950) is False and svc._fee_mismatch(1018, None) is False and svc._fee_mismatch(120, 60) is False


def test_a_sale_with_a_very_different_monthly_fee_says_so():
    subject = {"association_fee": 1018, "association_fee_frequency": "Monthly", "home_type": "CONDO"}
    other = {"street_address": "20932 VALLEY FORGE CIR #932", "condo_fee": 681, "condo_fee_frequency": "Monthly",
             "ai_commentary": "A larger unit in the south tower.", "commentary": "A larger unit in the south tower."}
    same = {"street_address": "10219 VALLEY FORGE CIR", "association_fee": 1018, "association_fee_frequency": "Monthly",
            "home_type": "CONDO", "ai_commentary": "Same plan.", "commentary": "Same plan."}
    flags = svc._append_fee_gap_notes(subject, [other, same])
    assert "monthly fee is $681, against $1,018 for this home" in other["commentary"] and "may overstate" in other["commentary"]
    assert same["commentary"] == "Same plan." and len(flags) == 1
    assert svc._append_fee_gap_notes(subject, [other]) == []   # once


def test_first_listed_long_before_the_day_count_is_said():
    from app.services import cma_fact_check as fc
    gap = {"list_date": "2026-09-12", "days_since_listed": 63, "days_on_market": 26}
    out = fc.first_listed_sentence("The value is $465,000.", gap)
    assert "first entered the MLS on Sep 12, 2026, 63 days ago, though the MLS counts 26 days on market" in out
    assert "Its real exposure is the longer history." in out
    assert fc.first_listed_sentence(out, gap) == out and fc.first_listed_sentence("The value is $465,000.", None) == "The value is $465,000."


def test_lowest_priced_sale_must_be_the_lowest_priced_sale():
    from app.services import cma_fact_check as fc
    comps = [{"street_address": "203 HUDSON DR", "sale_price": 525000}, {"street_address": "113 HUDSON DR", "sale_price": 450000},
             {"street_address": "218 HUDSON DR", "sale_price": 545000}, {"street_address": "238 HUDSON DR", "sale_price": 480000}]
    note = ("Comp #4 (238 HUDSON DR) sold for $480,000 on Aug 19, 2026 after 14 days. Its $221 per above-grade sq ft is below the "
            "subject's $324 list rate, and it is the lowest-priced sale in the set, which marks the floor of the range for this "
            "product. The subject's 3.5 baths argue for a price above it.")
    out, flags = fc.fix_price_superlatives(note, comps[3], comps)
    assert "lowest-priced sale" not in out and "sold for $480,000" in out and "3.5 baths argue" in out and flags
    # true of the sale that really is the lowest, and of the highest
    assert fc.fix_price_superlatives("It is the lowest-priced sale in the set.", comps[1], comps) == ("It is the lowest-priced sale in the set.", [])
    assert fc.fix_price_superlatives("It is the highest-priced sale here.", comps[2], comps)[1] == []
    assert fc.fix_price_superlatives("It is the highest-priced sale here.", comps[0], comps)[1]
    # an ADJUSTED or per-square-foot figure is a different measure - left alone
    assert fc.price_superlative_problem("Its adjusted price is the lowest sale price in the set.", comps[3], comps) is None
    assert fc.price_superlative_problem("It has the lowest price per square foot of the comps, the lowest sale by rate.", comps[3], comps) is None


def test_lower_level_counted_as_above_grade_is_read_as_lower_level():
    subject = {"property_type": "TOWNHOUSE", "subdivision_name": "LONGFORD CROSSING", "city": "PHOENIXVILLE"}
    same = {"property_type": "TOWNHOUSE", "subdivision_name": "LONGFORD CROSSING", "city": "PHOENIXVILLE",
            "lower_level_baths": 1, "lower_level_bedrooms": 1}
    f = svc._v10_lower_level_in_above_grade
    assert f(subject, same, 1530, 370, 2170, 0) == 370
    # a walkout basement with no area on record counts too
    assert f(subject, dict(same, lower_level_baths=None, lower_level_bedrooms=None, basement="Full, Garage Access, Walkout Level"), 1530, 370, 2170, 0) == 370
    # left as reported: the sale has its own lower-level area, is no bigger above grade, shows no lower-level rooms,
    # is in another community, or is a detached house
    assert f(subject, same, 1530, 370, 1914, 370) == 0
    assert f(subject, same, 1530, 370, 1600, 0) == 0
    assert f(subject, dict(same, lower_level_baths=0, lower_level_bedrooms=0), 1530, 370, 2170, 0) == 0
    assert f(subject, dict(same, subdivision_name="STEELPOINTE"), 1530, 370, 2170, 0) == 0
    assert f(dict(subject, property_type="SINGLE_FAMILY"), dict(same, property_type="SINGLE_FAMILY"), 1530, 370, 2170, 0) == 0
    assert f(subject, same, 1530, 0, 2170, 0) == 0


def test_a_stored_final_answer_is_not_reused_when_the_evidence_under_it_moved():
    assert svc._lock_anchor_holds(465196, 465196) and svc._lock_anchor_holds(465196, 466900)   # under 1%: a day's drift
    assert not svc._lock_anchor_holds(465196, 471279)                                           # the table changed
    assert svc._lock_anchor_holds(None, 471279) and svc._lock_anchor_holds(465196, None)        # nothing to compare


def test_homes_under_contract_never_move_the_value(monkeypatch):
    """Owner, 11/14: shown to the agent and the client, scored, flagged when far from the value - and that is all."""
    assert svc._PENDING_NUDGE_ENABLED is False
    implied = {"A ST": 700000, "B ST": 705000, "C ST": 800000}
    monkeypatch.setattr(svc, "_pending_implied_value", lambda subject, h, psf: implied[h["street_address"]])
    homes = [_pend("A ST", 715000, 2400, 90), _pend("B ST", 712000, 2350, 88)]
    v, lo, hi, info = svc._pending_competition_review({"sqft": 2342}, homes, 655000, 605000, 710000, {"anchor": {"market_psf": 250}})
    assert (v, lo, hi) == (655000, 605000, 710000) and info["nudge_pct"] == 0.0 and info["nudged_from"] is None
    assert [p["moved_value"] for p in info["pendings_used"]] == [False, False] and len(info["pendings_used"]) == 2
    # a strong one far from the value still reaches the agent
    v, lo, hi, info = svc._pending_competition_review({"sqft": 2342}, [_pend("C ST", 815000, 2400, 90)], 655000, 605000, 710000,
                                                      {"anchor": {"market_psf": 250}})
    assert v == 655000 and info["review"] and "C ST" in info["review"]


def test_interior_finish_items_count_as_priced_by_the_condition_line():
    """Test round 6, 104 Linda Ln (entered by hand): condition came off the updated sales in the table, then $13,000
    more came off as a judgment labelled "carpet over hardwood in bedrooms"."""
    packet = _packet(630570)
    packet["deterministic_evidence"]["comp_adjustment_grid"] = [{"comp_label": "Comp #2", "condition": -29000, "baths": -7000},
                                                                {"comp_label": "Comp #5", "condition": -24000}]
    reasoning = {"final_fmv": 618000, "range_low": 580000, "range_high": 655000,
                 "major_value_drags": [{"factor": "1990s kitchen and both baths", "direction": "down"},
                                       {"factor": "Carpet over hardwood in bedrooms", "direction": "down"},
                                       {"factor": "No pool or fully finished walkout level versus top sale", "direction": "neutral"}]}
    out, flags = svc._apply_value_guards(reasoning, packet)
    assert out["final_fmv"] == 631000 and "already priced in the adjustment table" in out["judgment_discipline"]["why"] and flags
    # with no condition line in the table, the same reasons are NOT already priced - the step stays
    packet["deterministic_evidence"]["comp_adjustment_grid"] = [{"comp_label": "Comp #2", "garage": 9000}]
    out, flags = svc._apply_value_guards(reasoning, packet)
    assert out["final_fmv"] == 618000
