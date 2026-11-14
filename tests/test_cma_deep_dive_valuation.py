"""Deep dive 11/13 (Fable review of the valuation guards). Each test is a case the reviewer ran and
saw go wrong."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as svc  # noqa: E402


# ---- V9: the buyer cap is about the asking price sitting above the sales ----------------------------
def _packet(asking, expected, anchor=510000, top=525000):
    return {"asking_price_check": {"asking_price": asking, "expected_sale_price": expected, "allowed_low": 0,
                                   "allowed_high": expected * 1.05, "ratio_used": expected / asking, "buyer_report": True},
            "deterministic_evidence": {"deterministic_anchor": anchor, "anchor_high": top}}


def test_buyer_cap_does_not_fire_when_the_asking_price_is_below_the_sales():
    # asking $500K is BELOW the $510K adjusted sales; only the comps' 1.08 ratio put "expected" above them
    out, flags = svc._apply_asking_price_check({"final_fmv": 535000, "range_low": 510000, "range_high": 560000}, _packet(500000, 540000))
    assert out["final_fmv"] == 535000 and not flags


def test_buyer_cap_still_fires_when_the_asking_price_is_above_the_sales():
    out, flags = svc._apply_asking_price_check({"final_fmv": 560000, "range_low": 530000, "range_high": 590000}, _packet(600000, 594000))
    assert out["final_fmv"] == 525000 and "Buyer report" in flags[0]


# ---- V1: similar homes under contract never lift a buyer's value -------------------------------------
def _nudge(buyer, monkeypatch):
    homes = [{"status": "pending", "match_score": 90, "street_address": "1 A St", "list_price": 580000},
             {"status": "pending", "match_score": 88, "street_address": "2 B St", "list_price": 590000}]
    monkeypatch.setattr(svc, "_pending_implied_value", lambda subject, home, psf: float(home["list_price"]))
    trace = {"anchor": {"market_psf": 250.0}, "asking_price_check": {"allowed_low_price": 0, "allowed_high_price": 630000}}
    return svc._pending_competition_review({}, homes, 520000, 495000, 545000, trace, buyer_report=buyer)


def test_pendings_do_not_lift_a_buyers_value(monkeypatch):
    value, low, high, info = _nudge(True, monkeypatch)
    assert value == 520000 and not info.get("nudged_from")
    assert all(not p.get("moved_value") for p in info["pendings_used"])


def test_pendings_still_nudge_a_sellers_value(monkeypatch):
    value, _, _, info = _nudge(False, monkeypatch)
    assert value > 520000 and info.get("nudged_from") == 520000


# ---- V3 / V4: a comp switched off, or a subject fact the grid prices, is a real change -----------------
def _evidence(comps, garage=0):
    return svc._build_reasoning_evidence_packet(
        subject={"street_address": "1 Elm St", "city": "Media", "state": "PA", "list_price": 500000, "sqft": 2000, "garage": garage},
        comps=comps, status_timeline={}, anchor={}, exposure={}, v10_result={}, subject_estimate=None,
        evidence_by_key={}, outliers={}, mode="seller")


COMPS = [{"listing_key": "a", "sale_price": 500000, "list_price": 500000, "sqft": 2000, "days_on_market": 9},
         {"listing_key": "b", "sale_price": 505000, "list_price": 500000, "sqft": 2000, "days_on_market": 12},
         {"listing_key": "c", "sale_price": 620000, "list_price": 500000, "sqft": 2000, "days_on_market": 3, "is_included": False},
         {"listing_key": "d", "sale_price": 610000, "list_price": 500000, "sqft": 2000, "days_on_market": 4, "is_included": False}]


def test_switched_off_comps_are_not_evidence():
    p = _evidence(COMPS)
    assert [c["comp_id"] for c in p["comparables"]] == ["a", "b"]
    assert [c["comp_label"][:7] for c in p["comparables"]] == ["Comp #1", "Comp #2"]          # numbering unchanged
    assert p["asking_price_check"]["comp_median_sold_to_list_ratio"] < 1.02               # was 1.115 with c and d counted
    assert p["asking_price_check"]["over_ask_pattern"] is None


def test_switching_a_comp_off_changes_the_lock_fingerprint():
    fp = lambda p: svc._value_lock_fingerprint(svc._packet_lock_identity(p), model="m")
    on = [dict(c, is_included=True) for c in COMPS]
    assert fp(_evidence(on)) != fp(_evidence(COMPS))


def test_a_garage_count_change_changes_the_lock_fingerprint():
    fp = lambda p: svc._value_lock_fingerprint(svc._packet_lock_identity(p), model="m")
    assert fp(_evidence(COMPS, garage=0)) != fp(_evidence(COMPS, garage=3))


# ---- V5: total marketing days is a figure the model was given ----------------------------------------
def test_refreshing_the_cumulative_day_count_keeps_a_stored_answer_valid():
    raw = {"final_valuation_summary": "It was pulled and relisted and has been marketed for 72 days."}
    out, changed = svc._refresh_cached_subject_days(raw, 72, 73, set())
    assert changed and "73 days" in out["final_valuation_summary"]
