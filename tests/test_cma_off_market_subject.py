"""CMA test 11/13 - 735 Concord Ave, Drexel Hill: a buyer CMA on a home whose listing ended without a
sale (withdrawn at $499,500). The sync marks such homes OFF_MARKET-BRIGHT but leaves raw_mls_data
saying "Active", so the report read "this home is listed at $499,500", "on the market longer than
its comps", and opened "13%-16% below the $499,500 asking price".

Now: the sync's off-market status wins over a stale raw status (a sale stays a sale, a live re-pull
still wins), and a buyer report on an off-market home is "not currently listed" - no asking price, no
days on market, the old price kept as history, offer figures from the value."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import cma_ai_service as svc  # noqa: E402


def _prop(home_status, raw_status="ACTIVE-BRIGHT"):
    return SimpleNamespace(home_status=home_status, mls_list_date=None,
                           raw_mls_data={"MlsStatus": raw_status, "DaysOnMarket": 157, "ListPrice": 499500})


SUBJECT = {"source": "mls", "listing_key": "805012170068", "street_address": "735 CONCORD AVE",
           "list_price": 499500.0, "days_on_market": 157, "mls_status": "Active"}


def test_off_market_sync_status_beats_stale_raw_active():
    tl = svc._subject_status_timeline(dict(SUBJECT), _prop("OFF_MARKET-BRIGHT"))
    assert tl["current_status"] == "Off Market"
    assert not tl["is_active"] and not tl["is_closed"] and not tl["is_under_contract"]


def test_a_sale_stays_a_sale_and_a_live_repull_wins():
    assert svc._subject_status_timeline(dict(SUBJECT), _prop("OFF_MARKET-BRIGHT", "CLOSED-BRIGHT"))["current_status"] == "Closed"
    live = svc._subject_status_timeline(dict(SUBJECT), _prop("OFF_MARKET-BRIGHT"), live_overlay={"StandardStatus": "Active"})
    assert live["current_status"] == "Active" and live["is_active"]
    # An active home is untouched.
    assert svc._subject_status_timeline(dict(SUBJECT), _prop("ACTIVE-BRIGHT"))["current_status"] == "Active"


def test_buyer_report_on_off_market_home_is_not_listed():
    tl = svc._subject_status_timeline(dict(SUBJECT), _prop("OFF_MARKET-BRIGHT"))
    subj, new_tl = svc._seller_not_listed("buyer", dict(SUBJECT), tl)
    assert new_tl["not_listed"] and new_tl["not_listed_buyer"]
    assert new_tl["current_list_price"] is None and new_tl["active_market_days"] is None
    assert new_tl["last_list_price"] == 499500
    assert subj["list_price"] is None and subj["days_on_market"] is None
    assert subj["mls_status"] == "Not currently listed"
    text = " ".join(new_tl["summary_lines"])
    assert "$499,500" in text and "not a current asking price" in text
    block = svc._format_status_block(new_tl)
    assert "off-market offer" in block and "no asking price" in block
    assert "listing_strategy_considerations" not in block   # that's the seller wording


def test_buyer_report_on_active_or_closed_home_unchanged():
    for status in ("ACTIVE-BRIGHT", "CLOSED-BRIGHT"):
        tl = svc._subject_status_timeline(dict(SUBJECT), _prop(status, status))
        subj, new_tl = svc._seller_not_listed("buyer", dict(SUBJECT), tl)
        assert new_tl is tl and subj["list_price"] == 499500.0


def test_seller_report_on_off_market_home_uses_the_seller_path():
    tl = svc._subject_status_timeline(dict(SUBJECT), _prop("OFF_MARKET-BRIGHT"))
    subj, new_tl = svc._seller_not_listed("listing", dict(SUBJECT), tl)
    assert new_tl["not_listed"] and not new_tl.get("not_listed_buyer")
    assert subj["list_price"] is None


def test_off_market_offer_is_built_from_value_not_an_asking_price():
    tl = svc._subject_status_timeline(dict(SUBJECT), _prop("OFF_MARKET-BRIGHT"))
    _, new_tl = svc._seller_not_listed("buyer", dict(SUBJECT), tl)
    band = svc._v10_build_buyer_offer_band(450000, None, trace={}, confidence="medium", status_timeline=new_tl,
                                           range_min=410000, range_max=490000)
    assert band["off_market"] and band["list_price"] is None
    assert band["opening_high"] < 450000 and band["target"] <= 450000
    prose, _ = svc._v10_rebuild_buyer_offer_prose("", band, 9.0)
    assert "isn't for sale" in prose and "no asking price" in prose
    assert "below the" not in prose and "499,500" not in prose
    # An ordinary listed home keeps the asking-price wording.
    listed = svc._v10_build_buyer_offer_band(450000, 499500, trace={}, confidence="medium", status_timeline={"is_active": True})
    assert not listed["off_market"]
