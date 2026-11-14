"""Owner, 11/13: a buyer's report must never raise the value toward the seller's asking price
(701 Sturbridge Dr: "that adds $45,000"). The asking price may still hold a buyer's value DOWN.
Seller reports keep the asking-price floor."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.cma_ai_service import (  # noqa: E402
    _apply_asking_price_check, _asking_price_market_check, _v10_value_bridge,
)

COMPS = [{"sale_price": 500000, "sold_list_ratio": 0.99, "dom": 20},
         {"sale_price": 520000, "sold_list_ratio": 0.98, "dom": 25},
         {"sale_price": 510000, "sold_list_ratio": 1.00, "dom": 18}]
SUBJECT = {"current_list_price": 650000, "dom": 10}


def _check(buyer):
    return _asking_price_market_check(SUBJECT, {}, {"state": "normal"}, COMPS, anchor_value=500000, buyer_report=buyer)


def _packet(check):
    return {"asking_price_check": check,
            "deterministic_evidence": {"deterministic_anchor": 500000, "anchor_low": 470000, "anchor_high": 530000}}


def test_seller_report_keeps_the_asking_floor():
    c = _check(False)
    assert c["allowed_low"] > 500000 and not c["buyer_report"]
    out, _ = _apply_asking_price_check({"final_fmv": 500000, "range_low": 480000, "range_high": 520000}, _packet(c))
    assert out["final_fmv"] > 500000   # pulled up toward the asking price, as before


def test_buyer_report_has_no_floor():
    c = _check(True)
    assert c["allowed_low"] == 0 and c["buyer_report"]
    out, flags = _apply_asking_price_check({"final_fmv": 500000, "range_low": 480000, "range_high": 520000}, _packet(c))
    assert out["final_fmv"] == 500000 and not flags


def test_buyer_value_capped_at_top_of_sales_when_asking_is_higher():
    c = _check(True)
    out, flags = _apply_asking_price_check({"final_fmv": 600000, "range_low": 570000, "range_high": 630000}, _packet(c))
    assert out["final_fmv"] == 530000
    assert out["asking_price_check_applied"]["buyer_sales_cap"] == 530000
    assert "Buyer report" in flags[0]


def test_buyer_bridge_never_calls_a_raise_asking_price_evidence():
    c = _check(True)
    trace = {"deterministic_anchor": 500000, "asking_price_check": {
        "asking_price": 650000, "expected_sale_price": c["expected_sale_price"], "allowed_low_price": 0,
        "allowed_high_price": c["allowed_high"], "ratio_used": c["ratio_used"], "buyer_report": True},
        "reasoning_model": {"major_value_drivers": [{"direction": "up", "factor": "renovated kitchen"}]}}
    b = _v10_value_bridge(trace, value=525000)
    keys = [s["key"] for s in b["steps"]]
    assert "listing" not in keys and "judgment" in keys


# ---- Grader audit 11/13, finding 28: wording matches confidence -------------------------------------
from app.services.cma_ai_service import _v10_build_buyer_offer_band, _v10_rebuild_buyer_offer_prose  # noqa: E402


def _offer_text(confidence):
    band = _v10_build_buyer_offer_band(445000, None, confidence=confidence, status_timeline={"not_listed_buyer": True},
                                       range_min=405000, range_max=480000)
    return _v10_rebuild_buyer_offer_prose("", band, 8.0)[0]


def test_moderate_confidence_gets_a_sensible_limit_not_a_hard_walk_away_price():
    text = _offer_text("medium")
    assert "walk-away price" not in text
    assert "sensible limit rather than a hard line" in text and "$405,000 to $480,000" in text


def test_high_confidence_keeps_the_walk_away_price():
    assert "walk-away price" in _offer_text("high")
