"""Test round 2, 11/13: five real reports on live comp data (701 Sturbridge Dr, 50 S 16th St #4407, 418 Reedville Rd,
735 Concord Ave, 524 Wales Rd). What they showed, and the rule each one now follows."""
import sys
from datetime import date, timedelta
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as svc  # noqa: E402
from app.services import cma_fact_check as fc  # noqa: E402


# ---- 1. the audit trail records the value and range the report shows ---------------------------------------

def test_trace_final_follows_the_last_steps():
    # 418 Reedville Rd: the model said $690,000 ($640,000-$745,000); the under-contract step made it $673,000
    trace = {"final_fmv": 690000, "range_min": 640000, "range_max": 745000, "range_pct": 7.61,
             "reasoning_model_fmv": 690000, "reasoning_model_range_low": 640000, "reasoning_model_range_high": 745000}
    svc._sync_trace_final(trace, 673000, 620000, 730000, 8.17)
    assert (trace["final_fmv"], trace["range_min"], trace["range_max"], trace["range_pct"]) == (673000, 620000, 730000, 8.17)
    # the model's own numbers stay on the trail
    assert (trace["reasoning_model_fmv"], trace["reasoning_model_range_low"]) == (690000, 640000)


def test_final_check_reads_the_final_range_not_the_models_earlier_one():
    trace = {"range_min": 620000, "range_max": 730000, "reasoning_model_range_low": 640000, "reasoning_model_range_high": 745000}
    assert svc._authoritative_range(673000, 8.17, trace) == (620000, 730000)
    # nothing synced yet: the model's endpoints still serve
    assert svc._authoritative_range(690000, 7.6, {"reasoning_model_range_low": 640000, "reasoning_model_range_high": 745000}) == (640000, 745000)


def test_sync_ignores_a_missing_trace_or_value():
    svc._sync_trace_final(None, 500000, 480000, 520000, 4.0)   # no error
    t = {"final_fmv": 500000}
    svc._sync_trace_final(t, None, None, None, None)
    assert t == {"final_fmv": 500000}


# ---- 2. homes under contract ----------------------------------------------------------------------------

def _pendings(monkeypatch, homes):
    monkeypatch.setattr(svc, "_pending_implied_value", lambda subject, h, psf: h["implied"])
    return [{"status": "pending", "match_score": 85, "list_price": h["implied"], "street_address": f"{i} TEST RD",
             "listing_key": str(i), **h} for i, h in enumerate(homes, 1)]


def test_other_homes_asking_prices_do_not_move_a_home_that_is_itself_under_contract(monkeypatch):
    # 524 Wales Rd: under contract at a $479,000 asking price; two other pendings took 2% off its value
    homes = _pendings(monkeypatch, [{"implied": 440000}, {"implied": 408000}])
    trace = {"anchor": {"market_psf": 300}}
    v, lo, hi, info = svc._pending_competition_review({"above_grade_finished_area": 1352}, homes, 458000, 438000, 480000,
                                                     trace, buyer_report=True, subject_under_contract=True)
    assert (v, lo, hi) == (458000, 438000, 480000)
    assert info["nudge_pct"] == 0.0 and info["nudged_from"] is None and info["review"] is None and info["pendings_used"] == []
    # the same homes still inform an ACTIVE listing's value
    v2, _, _, info2 = svc._pending_competition_review({"above_grade_finished_area": 1352}, homes, 458000, 438000, 480000,
                                                     trace, buyer_report=True)
    assert v2 < 458000 and info2["nudged_from"] == 458000


def test_a_pending_on_a_fraction_of_the_land_is_not_a_close_match(monkeypatch):
    # 418 Reedville Rd: 7.5 acres with a pond; two 1.4-acre subdivision homes took 2.5% off its value
    subject = {"above_grade_finished_area": 2469, "lot_size": 7.5 * 43560}
    homes = _pendings(monkeypatch, [{"implied": 619000, "lot_size": 59285}, {"implied": 603000, "lot_size": 64033}])
    v, lo, hi, info = svc._pending_competition_review(subject, homes, 690000, 640000, 745000, {"anchor": {"market_psf": 240}})
    assert (v, lo, hi) == (690000, 640000, 745000) and info["pendings_used"] == []
    # similar land: they count
    homes = _pendings(monkeypatch, [{"implied": 619000, "lot_size": 6 * 43560}, {"implied": 603000, "lot_size": 9 * 43560}])
    v2, _, _, info2 = svc._pending_competition_review(subject, homes, 690000, 640000, 745000, {"anchor": {"market_psf": 240}})
    assert v2 < 690000 and len(info2["pendings_used"]) == 2


def test_land_mismatch_needs_both_the_ratio_and_a_real_gap():
    assert svc._pending_land_mismatch({"lot_size": 7.5 * 43560}, {"lot_size": 1.4 * 43560})
    assert not svc._pending_land_mismatch({"lot_size": 6000}, {"lot_size": 20000})       # 3x, but a third of an acre apart
    assert not svc._pending_land_mismatch({"lot_size": 2 * 43560}, {"lot_size": 4 * 43560})  # 2x only
    # a home with no lot on record: not a match for an acreage subject (11/14), no call for an ordinary one
    assert svc._pending_land_mismatch({"lot_size": 7.5 * 43560}, {})
    assert not svc._pending_land_mismatch({"lot_size": 0.3 * 43560}, {})


def test_review_note_shows_whole_thousands(monkeypatch):
    # "about $2,152,925 adjusted to this home" read as a precision the estimate doesn't have
    homes = _pendings(monkeypatch, [{"implied": 2152925, "match_score": 76}])
    homes[0]["list_price"] = 1925000
    _, _, _, info = svc._pending_competition_review({"above_grade_finished_area": 4241}, homes, 1820000, 1660000, 1980000,
                                                    {"anchor": {"market_psf": 430}}, buyer_report=True)
    assert "$2,153,000 adjusted" in info["review"] and "2,152,925" not in info["review"]
    assert info["pendings_used"][0]["implied_value"] == 2153000


# ---- 3. a town's own name is not a community ------------------------------------------------------------

def test_town_name_as_subdivision_is_not_a_community():
    a = {"subdivision_name": "DREXEL HILL", "city": "DREXEL HILL", "township": "UPPER DARBY TWP"}
    b = {"subdivision_name": "DREXEL HILL", "city": "Drexel Hill", "township": "UPPER DARBY TWP"}
    assert fc.subdivision_of(a) is None
    assert fc.same_subdivision(a, b) is None        # unknown - never "same community"
    # a real community inside the town still counts
    c = {"subdivision_name": "ARONIMINK", "city": "DREXEL HILL"}
    d = {"subdivision_name": "Aronimink", "city": "DREXEL HILL"}
    assert fc.subdivision_of(c) == "Aronimink" and fc.same_subdivision(c, d) is True


def test_town_name_rule_can_be_switched_off_for_the_past_sales_check(monkeypatch):
    monkeypatch.setattr(fc, "CITY_NAME_IS_NOT_A_COMMUNITY", False)
    assert fc.subdivision_of({"subdivision_name": "DREXEL HILL", "city": "DREXEL HILL"}) == "Drexel Hill"


# ---- 4. wording ---------------------------------------------------------------------------------------

def test_sale_to_list_ratio_is_written_as_a_percentage():
    out, flags = fc.fix_ratio_of_list("It sold for $1,380,000, closing at 0.986 of list after 14 days.")
    assert "98.6% of list" in out and "0.986" not in out and flags
    assert "120.4% of list" in fc.fix_ratio_of_list("It sold in 4 days at 1.204 of list.")[0]
    assert "97.3% of its final asking price" in fc.fix_ratio_of_list("at 0.973 of its final asking price")[0]
    # not a ratio: left alone
    for same in ("about 2.5 of the homes", "0.66 acres of land", "1.5 baths of the list"):
        assert fc.fix_ratio_of_list(same) == (same, [])
    # and it runs in the fact gate
    assert "98.6% of list" in fc.fix_text("Closing at 0.986 of list.", subject={}, comp={})[0]


def test_mailing_town_sentence_reads_as_english():
    subject = {"city": "BRYN MAWR", "township": "RADNOR TWP", "state": "PA"}
    comp = {"city": "VILLANOVA", "township": "RADNOR TWP", "state": "PA", "street_address": "538 CAMP WOODS CIR"}
    out, flags = svc._reconcile_comp_hard_facts(
        "It is in Villanova, with the same Radnor school district.", "Comp #5", subject, comp)
    assert "It has a Villanova mailing address, with the same Radnor school district." in out
    assert "is with a" not in out and any("mailing town" in f for f in flags)
    out2, _ = svc._reconcile_comp_hard_facts("A renovated colonial in Villanova on a cul-de-sac.", "Comp #5", subject, comp)
    assert "colonial with a Villanova mailing address on a cul-de-sac" in out2


_OFF_MARKET_BAND = {
    "fmv": 448000, "backup": False, "target": 448000, "auction": False, "basis_ref": 448000, "confidence": "medium",
    "hot_market": False, "list_price": None, "off_market": True, "opening_low": 423000, "passed_over": False,
    "underpriced": False, "opening_high": 441000, "comfort_ceiling": 457000, "over_ask_passed": None,
    "outpaced_by_comps": False, "opening_pct_phrase": None, "market_over_ask_pct": None, "under_contract_days": None,
    "valuation_range_low": 415000, "valuation_range_high": 480000,
}


def test_off_market_offer_note_says_it_once():
    # 735 Concord Ave: "This home isn't for sale, so there is no asking price to negotiate from" printed twice
    ai_text = ("This home isn't for sale, so there is no asking price to negotiate from. An owner who wasn't planning to sell "
               "may ask for more, and paying past that figure needs a clear reason. Keep inspection and appraisal "
               "contingencies, since the owner has no obligation to sell and an appraisal needs to support the price.")
    out, _ = svc._v10_rebuild_buyer_offer_prose(ai_text, dict(_OFF_MARKET_BAND), 7.25)
    assert out.count("no asking price to negotiate from") == 1
    assert out.count("wasn't planning to sell") == 1
    assert "Keep inspection and appraisal contingencies" in out


def test_backup_offer_note_does_not_repeat_the_under_contract_frame():
    band = {**_OFF_MARKET_BAND, "off_market": False, "backup": True, "list_price": 479000, "fmv": 449000, "target": 449000,
            "opening_low": 440000, "opening_high": 449000, "comfort_ceiling": 449000, "under_contract_days": 42,
            "valuation_range_low": 425000, "valuation_range_high": 475000, "basis_ref": 479000}
    ai_text = ("The seller is not actively negotiating new offers. Be ready to move toward the indicated value if the "
               "primary contract fails, and keep an inspection contingency.")
    out, _ = svc._v10_rebuild_buyer_offer_prose(ai_text, band, 5.57)
    assert "not actively negotiating" not in out
    assert "Be ready to move toward the indicated value" in out


# ---- 5. a last listing that ended without a sale -----------------------------------------------------------

def _reedville_timeline(today):
    return {"current_status": "Off Market", "is_active": False, "is_closed": False, "is_under_contract": False,
            "current_list_price": 835750, "original_list_price": 849900,
            "list_date": (today - timedelta(days=182)).isoformat(), "off_market_date": (today - timedelta(days=1)).isoformat(),
            "active_market_days": 136}


def test_seller_report_states_an_unsold_last_listing():
    today = svc._tmach.today()
    subject = {"street_address": "418 REEDVILLE RD", "listing_key": "804899454744", "list_price": 835750}
    subj, tl = svc._seller_not_listed("listing", subject, _reedville_timeline(today))
    ll = tl["last_listing"]
    assert ll["price"] == 835750 and ll["original_price"] == 849900 and ll["days"] == 181
    assert "about 181 days after it was listed" in tl["summary_lines"][1]
    line = next(x for x in tl["summary_lines"] if x.startswith("Its last MLS listing"))
    assert "asked $835,750 (reduced from $849,900)" in line and "without a recorded sale" in line and "ceiling" in line
    # still not an asking price anywhere
    assert subj["list_price"] is None and tl["current_list_price"] is None and tl["not_listed"] is True
    text = svc._seller_pricing_position(value=690000, range_min=640000, range_max=745000, subject_asking=None,
                                        status_timeline=tl, comps=[])
    assert "We recommend listing at about $690,000" in text
    assert "last listing, at $835,750, came off the market" in text and "with no sale recorded" in text and "about 21% above" in text


def test_no_unsold_listing_line_for_a_sold_old_or_hand_entered_home():
    today = svc._tmach.today()
    tl = _reedville_timeline(today)
    assert svc._unsold_last_listing({"listing_key": "1"}, {**tl, "is_closed": True}) is None
    old = {**tl, "list_date": (today - timedelta(days=1500)).isoformat(), "off_market_date": (today - timedelta(days=1300)).isoformat()}
    assert svc._unsold_last_listing({"listing_key": "1"}, old) is None
    assert svc._unsold_last_listing({"source": "manual"}, tl) is None
    assert svc._unsold_last_listing({"listing_key": "1"}, {**tl, "current_list_price": None}) is None
    # a past SALE keeps its own line and gets no unsold-listing line
    _, sold_tl = svc._seller_not_listed("listing", {"listing_key": "1", "last_sale_price": 500000},
                                        {"current_status": "Closed", "is_closed": True, "close_date": "2021-06-01"})
    assert sold_tl["last_listing"] is None and any(x.startswith("Last sold") for x in sold_tl["summary_lines"])


def test_pricing_position_is_silent_when_the_old_price_was_not_above_value():
    tl = {"not_listed": True, "last_listing": {"price": 680000, "days": 60, "ended": "2026-10-01"}}
    text = svc._seller_pricing_position(value=690000, range_min=640000, range_max=745000, subject_asking=None,
                                        status_timeline=tl, comps=[])
    assert "last listing" not in text


def test_competition_list_leaves_out_homes_on_a_fraction_of_the_land(monkeypatch):
    import asyncio

    async def fake(db, subject, listing_key=None):
        return [{"street_address": "143 TRINITY DR", "status": "active", "lot_size": 11542},
                {"street_address": "9 ACRE LN", "status": "active", "lot_size": 6 * 43560},
                {"street_address": "NO LOT ON RECORD", "status": "pending"}]
    monkeypatch.setattr(svc.cma_service, "current_competition", fake)
    homes = asyncio.run(svc._load_current_competition(None, None, {"listing_key": "1", "lot_size": 7.5 * 43560}))
    assert [h["street_address"] for h in homes] == ["9 ACRE LN"]      # (no lot on record: not shown for an acreage subject)


# ---- 6. the value steps explain every dollar ---------------------------------------------------------------

def _sturbridge_trace():
    return {"deterministic_anchor": 1854857,
            "asking_price_check": {"asking_price": 1875000, "expected_sale_price": 1837500, "allowed_low_price": 0,
                                   "allowed_high_price": 1837500, "ratio_used": 0.98, "comp_median_sold_to_list_ratio": 0.986,
                                   "passed_over": True, "buyer_report": True},
            "reasoning_model": {"major_value_drags": [{"factor": "kitchen is more dated than the renovated sales", "direction": "down"}]}}


def test_value_below_the_asking_price_limit_gets_its_own_judgment_line():
    # was one line: "...the value is not set above about $1,838,000. That takes off $35,000. Together: $1,820,000."
    bridge = svc._v10_value_bridge(_sturbridge_trace(), value=1820000, range_min=1660000, range_max=1980000)
    steps = {s["key"]: s["amount"] for s in bridge["steps"]}
    assert set(steps) == {"listing", "judgment"}
    assert steps["listing"] == -17000 and steps["judgment"] == -18000
    assert bridge["adjusted_sales_value"] + sum(steps.values()) == 1820000
    listing = next(s for s in bridge["steps"] if s["key"] == "listing")
    assert "points to a sale near $1,838,000" in listing["basis"] and "not set above" not in listing["basis"]


def test_value_sitting_on_the_limit_is_still_one_asking_price_line():
    bridge = svc._v10_value_bridge(_sturbridge_trace(), value=1835000, range_min=1680000, range_max=1990000)
    assert [s["key"] for s in bridge["steps"]] == ["listing"]
    assert bridge["steps"][0]["amount"] == -20000 and "not set above about $1,838,000" in bridge["steps"][0]["basis"]


# ---- 7. confidence -------------------------------------------------------------------------------------

def _conf_comps(psfs):
    return [{"listing_key": str(i), "is_included": True, "strength": "supporting", "price_per_sqft": p, "sale_price": p * 1300,
             "distance_miles": 0.1 * (i + 1), "price_verified": True} for i, p in enumerate(psfs)]


def test_a_flagged_sale_close_to_the_pack_does_not_lower_confidence():
    # 524 Wales Rd: $290 against a $316 middle (8% apart) was "a statistical outlier" and lowered confidence
    comps = _conf_comps([319, 316, 330, 316, 290])
    ev = {c["listing_key"]: {"components": {"proximity": 0.9, "size": 0.9}} for c in comps}
    _, flags = svc._compute_confidence(comps, ev, {"4": "statistical outlier"}, None)
    assert not any("Confidence reduced" in f for f in flags)
    # a sale really apart from the pack still does
    comps = _conf_comps([319, 316, 330, 316, 540])
    _, flags = svc._compute_confidence(comps, ev, {"4": "more than 1.45x the median"}, None)
    assert any("Confidence reduced - 1 valuation comp" in f for f in flags)


# ---- 8. acreage ----------------------------------------------------------------------------------------

def _grid_row(comp, price, ag):
    bg, est = svc._v10_bg_for_grid(comp, ag)
    return {"_c": comp, "price": price, "ag": ag, "bg": bg, "bg_estimated": est}


_ACRE_SUBJECT = {"above_grade_finished_area": 2469, "lot_size": 7.5 * 43560, "baths": 3, "year_built": 2016}


def test_acreage_line_prices_a_big_land_gap(monkeypatch):
    monkeypatch.setattr(svc, "_V10_ACREAGE_LOT_PCT", 0.08)
    one_acre = {"lot_size": 1.06 * 43560, "baths": 3, "year_built": 2016}
    fa = svc._v10_grid_adjustments(_ACRE_SUBJECT, _grid_row(one_acre, 585000, 2469), 2469, 0, False, 235.0)
    # 7.5 vs 1.06 acres is 2.8 doublings, capped at 2.5: 2.5 x 8% x $585,000
    assert fa["lot"] == round(2.5 * 0.08 * 585000)
    assert any("land 7.50 vs 1.06 acres" in n for n in fa["notes"])
    # the other way round: a comp on far more land is adjusted DOWN
    small_subject = {**_ACRE_SUBJECT, "lot_size": 1.0 * 43560}
    fa2 = svc._v10_grid_adjustments(small_subject, _grid_row({"lot_size": 8.5 * 43560, "baths": 3, "year_built": 2016}, 715000, 2469), 2469, 0, False, 235.0)
    assert fa2["lot"] < 0


def test_acreage_line_leaves_ordinary_lots_alone(monkeypatch):
    monkeypatch.setattr(svc, "_V10_ACREAGE_LOT_PCT", 0.08)
    # 1.2 acres against a third of an acre (719 Beechwood Rd): 3.6x, but under two acres apart - tested 11/01, no line
    subj = {**_ACRE_SUBJECT, "lot_size": 1.2 * 43560}
    fa = svc._v10_grid_adjustments(subj, _grid_row({"lot_size": 0.33 * 43560, "baths": 3, "year_built": 2016}, 400000, 2469), 2469, 0, False, 235.0)
    assert fa["lot"] == 0
    # 8.5 against 7.5 acres: similar land, no line
    fa = svc._v10_grid_adjustments(_ACRE_SUBJECT, _grid_row({"lot_size": 8.5 * 43560, "baths": 3, "year_built": 2016}, 715000, 2469), 2469, 0, False, 235.0)
    assert fa["lot"] == 0


def test_acreage_line_is_off_when_its_rate_is_zero(monkeypatch):
    monkeypatch.setattr(svc, "_V10_ACREAGE_LOT_PCT", 0.0)
    fa = svc._v10_grid_adjustments(_ACRE_SUBJECT, _grid_row({"lot_size": 1.06 * 43560, "baths": 3, "year_built": 2016}, 585000, 2469), 2469, 0, False, 235.0)
    assert fa["lot"] == 0


def test_no_dollar_step_sentence_only_when_land_was_not_priced():
    rows = [{"_c": {"lot_size": 1.1 * 43560}, "feat": {"lot": 0}}, {"_c": {"lot_size": 1.0 * 43560}, "feat": {"lot": 0}},
            {"_c": {"lot_size": 1.3 * 43560}, "feat": {"lot": 0}}]
    said = svc._lot_and_sewer_uncertainties(_ACRE_SUBJECT, rows)
    assert any("no separate dollar step" in x for x in said)
    for r in rows:
        r["feat"]["lot"] = 117000
    said = svc._lot_and_sewer_uncertainties(_ACRE_SUBJECT, rows)
    assert not any("dollar step" in x for x in said)
    # ...and the report says the land WAS adjusted for, and that the rate is an estimate
    line = next(x for x in said if "acres" in x)
    assert "lot is 7.50 acres" in line and "Three of the three sales are on far less land and were adjusted up" in line
    assert "estimate" in line and "each doubling of land" in line      # the rate is stated (grader audit 11/14)
    # it reaches the client's notes even when they already mention the acreage - unless they already say it was adjusted for
    notes = "This home has 7.50 acres, a pond and a workshop."
    out, flags = svc._v10_lot_sewer_sentences(notes, trace={"anchor": {"main_uncertainties": said}})
    assert "adjusted up for it" in out and flags
    notes2 = "The one-acre sales were adjusted up for the land, an estimate."
    assert svc._v10_lot_sewer_sentences(notes2, trace={"anchor": {"main_uncertainties": said}})[0] == notes2


def _rel_comp(key, lot_acres, miles):
    return {"listing_key": key, "status": "sold", "sale_price": 600000, "close_date": svc._tmach.today().isoformat(),
            "above_grade_finished_area": 2500, "sqft": 2500, "year_built": 2010, "property_type": "SINGLE_FAMILY",
            "lot_size": lot_acres * 43560, "distance_miles": miles, "price_verified": True}


def test_land_match_counts_more_for_an_acreage_subject(monkeypatch):
    subject = {"above_grade_finished_area": 2469, "year_built": 2016, "property_type": "SINGLE_FAMILY", "lot_size": 7.5 * 43560}
    comps = [_rel_comp("near-small-lot", 0.3, 0.8), _rel_comp("far-acreage", 8.5, 4.0)]

    def scores():
        rows = svc._continuous_relevance(subject, comps)["comps"]
        return {r["listing_key"]: r["adjusted_relevance_score"] for r in rows}

    monkeypatch.setattr(svc, "_REL_ACREAGE_LOT_MULT", 1.0)
    before = scores()
    monkeypatch.setattr(svc, "_REL_ACREAGE_LOT_MULT", 4.0)
    after = scores()
    assert after["far-acreage"] - after["near-small-lot"] > before["far-acreage"] - before["near-small-lot"]
    # an ordinary-lot subject is scored exactly as before
    subject2 = {**subject, "lot_size": 0.5 * 43560}
    monkeypatch.setattr(svc, "_REL_ACREAGE_LOT_MULT", 1.0)
    a = {r["listing_key"]: r["adjusted_relevance_score"] for r in svc._continuous_relevance(subject2, comps)["comps"]}
    monkeypatch.setattr(svc, "_REL_ACREAGE_LOT_MULT", 4.0)
    b = {r["listing_key"]: r["adjusted_relevance_score"] for r in svc._continuous_relevance(subject2, comps)["comps"]}
    assert a == b


# ---- 9. buyer, home not for sale: the owner's last asking price is a fact for the negotiation -------------------

def test_off_market_buyer_report_states_the_last_listing():
    today = svc._tmach.today()
    tl = {"current_status": "Off Market", "is_active": False, "is_closed": False, "is_under_contract": False,
          "current_list_price": 499500, "list_date": (today - timedelta(days=157)).isoformat(),
          "off_market_date": (today - timedelta(days=136)).isoformat(), "active_market_days": 21}
    subj, new_tl = svc._buyer_off_market({"street_address": "735 CONCORD AVE", "listing_key": "805012170068"}, tl, "off market")
    assert new_tl["last_listing"]["price"] == 499500 and new_tl["last_listing"]["days"] == 21
    assert new_tl["last_list_price"] == 499500 and new_tl["current_list_price"] is None
    assert any("$499,500" in x and "ended without a sale" in x for x in new_tl["summary_lines"])
    band = {**_OFF_MARKET_BAND, "last_listing": new_tl["last_listing"]}
    out, _ = svc._v10_rebuild_buyer_offer_prose("Keep inspection and appraisal contingencies.", band, 7.0)
    assert "The owner last listed this home at $499,500" in out and "with no sale recorded" in out and "about 11% more" in out
    # nothing on record, or an old price at or under the value: the note says nothing about it
    out2, _ = svc._v10_rebuild_buyer_offer_prose("Keep inspection and appraisal contingencies.", dict(_OFF_MARKET_BAND), 7.0)
    assert "last listed" not in out2
    low = {**_OFF_MARKET_BAND, "last_listing": {"price": 440000, "days": 30, "ended": None}}
    assert "last listed" not in svc._v10_rebuild_buyer_offer_prose("Keep contingencies.", low, 7.0)[0]


def test_last_listing_days_only_when_the_end_date_is_on_record():
    today = svc._tmach.today()
    tl = {"current_status": "Off Market", "is_active": False, "is_closed": False, "is_under_contract": False,
          "current_list_price": 499500, "list_date": (today - timedelta(days=157)).isoformat(),
          "off_market_date": None, "active_market_days": 158}       # the timeline's count ran to today
    ll = svc._unsold_last_listing({"listing_key": "805012170068"}, tl)
    assert ll["days"] is None and "days on the market" not in ll["line"] and "without a recorded sale" in ll["line"]


def test_last_listing_price_is_a_known_figure_for_the_final_dollar_sweep():
    trace = {"subject_last_listing": {"price": 499500, "original_price": 525000, "days": 21}}
    assert {499500.0, 525000.0} <= svc._structured_currency_values(trace)


def test_acres_lot_reads_as_english():
    out, flags = fc.fix_acres_lot_grammar("a two-car garage and a 0.14 acres lot, and a 0.25 acres lot nearby")
    assert out == "a two-car garage and a 0.14-acre lot, and a 0.25-acre lot nearby" and flags
    assert fc.fix_acres_lot_grammar("on a 8.5 acres parcel")[0] == "on an 8.5-acre parcel"
    assert fc.fix_acres_lot_grammar("It sits on 0.66 acres in Radnor.") == ("It sits on 0.66 acres in Radnor.", [])
    assert "0.14-acre lot" in fc.fix_text("same 0.14 acres lot", subject={}, comp={})[0]


# ---- 10. third pass (11/14): what the fresh Oxford and Drexel Hill reports showed ---------------------------

def test_land_sentence_is_backed_by_the_grids_own_land_adjustments():
    trace = {"anchor": {"comps": [{"feature_adjustments": {"lot": 117000, "total": 160000}},
                                  {"feature_adjustments": {"lot": 0, "total": 83000}}]}}
    text = ("This home's lot is 7.50 acres. Four of the five sales are on far less land and were adjusted up for it, "
            "at the rate past local acreage sales support.")
    assert svc._v10_directional_adjustment_backing_check({"m": ("Methodology summary", text)}, trace) == []
    # with no land line in the grid the same sentence is still refused
    bare = {"anchor": {"comps": [{"feature_adjustments": {"lot": 0, "total": 5000}}]}}
    assert svc._v10_directional_adjustment_backing_check({"m": ("Methodology summary", text)}, bare)


def test_land_sentence_is_never_appended_twice():
    said = ["This home's lot is 7.50 acres. Four of the five sales are on far less land and were adjusted up for it, at "
            "the rate past local acreage sales support. That rate is an estimate, so how much buyers pay for the land "
            "is the main uncertainty in this value."]
    once, _ = svc._v10_lot_sewer_sentences("The value rests on the 8.50-acre sale.", trace={"anchor": {"main_uncertainties": said}})
    twice, flags = svc._v10_lot_sewer_sentences(once, trace={"anchor": {"main_uncertainties": said}})
    assert twice == once and flags == [] and once.count("This home's lot is 7.50 acres") == 1
    # even when a repair round cut its middle sentence
    cut = "The value rests on the 8.50-acre sale. This home's lot is 7.50 acres. That rate is an estimate."
    assert svc._v10_lot_sewer_sentences(cut, trace={"anchor": {"main_uncertainties": said}})[0] == cut


def test_bare_price_in_a_judgment_reason_gets_its_dollar_sign():
    text, _, _ = svc._bridge_driver_text([{"factor": "Prior listing at 835,750 ended without a sale after about 182 days"}], set())
    assert "at $835,750 ended" in text and "182 days" in text
    assert "3,619 sq ft" in svc._bridge_driver_text([{"factor": "3,619 sq ft of finished space"}], set())[0]


def test_per_foot_figure_is_not_above_grade_on_total_area():
    comp = {"street_address": "213 JOSEPH RD", "sale_price": 570000, "above_grade_finished_area": 2744, "sqft": 3517,
            "city": "OXFORD"}
    out, _ = svc._reconcile_comp_hard_facts("It sold for $570,000 in November 2025, at $162/sqft on total area.",
                                            "Comp #2", {"city": "OXFORD"}, comp)
    assert "$208/sqft above grade." in out and "on total area" not in out


def test_saved_note_with_the_old_mailing_wording_is_repaired():
    out, flags = fc.fix_mailing_address_grammar("It is with a Villanova mailing address, with the same Radnor school district.")
    assert out == "It has a Villanova mailing address, with the same Radnor school district." and flags
    assert fc.fix_mailing_address_grammar("A colonial with a Villanova mailing address.") == ("A colonial with a Villanova mailing address.", [])
    assert "has a West Chester mailing address" in fc.fix_text("It sits with a West Chester mailing address.", subject={}, comp={})[0]


# ---- 11. the comp search never filters on the MLS payload in SQL (live slowdown, 11/14) -------------------------

def test_comp_search_sql_never_filters_on_the_mls_payload():
    """An hour after the 11/14 push the live comp step took 65 seconds: three WHERE filters read raw_mls_data
    (design type, closing price, MLS status), so the database opened the full payload of every home in the
    search box. Those checks now use mirrored columns or run on the rows already read."""
    import inspect
    from app.services import cma_service
    src = inspect.getsource(cma_service.search_comparables)
    sql_part = src.split("rows = result.scalars().all()")[0]
    # the one payload read left is the subject's own design type: a single row looked up by its key
    assert sql_part.count("raw_mls_data[") == 1 and "Property.listing_key == req.listing_key" in sql_part
    assert "mls_mirror.close_price()" in sql_part


def test_parking_and_manufactured_records_are_still_left_out():
    import inspect
    from app.services import cma_service
    loop_part = inspect.getsource(cma_service.search_comparables).split("rows = result.scalars().all()")[1]
    assert '"parking" in _design' in loop_part and "_subject_manufactured" in loop_part
