"""11/20 second outside-grader round (tests 6-8). Real records and the real sentences that went wrong."""
import datetime

from app.services import cma_ai_service as ai
from app.services import cma_comp_pick as pick
from app.services import cma_fact_check as fc

# 101 Watkins Pond Blvd #4-103 - our price-history events for the home (the agent picked the Mar 2025 sale record)
WATKINS_EVENTS = [
    {"date": "2025-01-17", "event": "Listed for sale", "price": 425000.0},
    {"date": "2025-01-23", "event": "Under contract", "price": 425000.0},
    {"date": "2025-03-31", "event": "Sold", "price": 425000.0},
    {"date": "2026-05-28", "event": "Listed for sale", "price": None},
    {"date": "2026-06-24", "event": "Price changed", "price": 410000.0},
    {"date": "2026-11-02", "event": "Taken off market", "price": 410000.0},
]
TODAY = datetime.date(2026, 11, 20)


def test_a_listing_after_the_picked_sale_is_kept():
    s = fc.summarize_history(WATKINS_EVENTS, current_list_date="2025-01-17", today=TODAY)
    assert s["later_listings"] and s["later_listings"][-1]["listed"] == "2026-05-28"
    assert s["later_listings"][-1]["last_price"] == 410000.0 and s["later_listings"][-1]["end_event"] != "sold"
    # a listing that is the newest record keeps no "later" ones
    assert fc.summarize_history(WATKINS_EVENTS, current_list_date="2026-05-28", today=TODAY)["later_listings"] == []


def test_later_unsold_listing_is_the_unsold_listing_fact(monkeypatch):
    monkeypatch.setattr(ai._tmach, "today", lambda: TODAY)
    s = fc.summarize_history(WATKINS_EVENTS, current_list_date="2025-01-17", today=TODAY)
    ll = ai._later_unsold_listing({"_listing_history": s})
    assert ll and ll["price"] == 410000 and ll["list_date"] == "2026-05-28"
    assert "asked $410,000" in ll["line"] and "without a recorded sale" in ll["line"]
    # sold again afterwards, or long ago: nothing
    sold = fc.summarize_history(WATKINS_EVENTS + [{"date": "2026-08-01", "event": "Sold", "price": 409000.0}],
                                current_list_date="2025-01-17", today=TODAY)
    assert ai._later_unsold_listing({"_listing_history": sold}) is None


def test_client_is_told_the_home_was_listed_again_and_did_not_sell():
    s = fc.summarize_history(WATKINS_EVENTS, current_list_date="2025-01-17", today=TODAY)
    s["last_sale"], s["last_sale_is_this_listing"] = {"date": "2025-03-31", "price": 425000}, True
    out = fc.history_sentence("The value is $423,000.", s, today=TODAY, listed=False)
    assert "This home last sold on Mar 31, 2025 for $425,000." in out
    assert "It was listed for sale again on May 28, 2026, last asking $410,000, and came off the market on Nov 2, 2026 without selling." in out
    assert fc.history_sentence(out, s, today=TODAY, listed=False).count("without selling") == 1      # never twice


# --- "What the competition tells us" was empty on 10 of 12 reports --------------------------------------------------

def test_sizes_and_day_counts_are_not_addresses():
    found = lambda t: [m.group(0) for m in ai._COMPETITION_ADDRESS_RE.finditer(t)]
    assert found("sizes range from 1,293 to 3,147 square feet, against your 1,843.") == []
    assert found("close to your 1,161 square feet") == []
    assert found("went under contract after only 4 days. 111 Tyson Rd, at $899,000, is the nearest") == ["111 Tyson Rd"]
    assert found("The closest alternative is 3500 Caley Rd, just 0.1 miles away.") == ["3500 Caley Rd"]


def test_one_bad_sentence_no_longer_drops_the_paragraph():
    facts = {"subject": {"address": "435 Goodwick Dr"},
             "similar_homes": [{"address": "521 Swansea Dr", "list_price": "$435,000"},
                               {"address": "6038 Freestone Blvd", "list_price": "$487,250"}]}
    text = ("Three similar homes are for sale and two are under contract. The closest alternative is 521 Swansea Dr at "
            "$435,000, nearly the same size as your 2,575 square feet. A home nearby sold for $999,000 last year. "
            "6038 Freestone Blvd went under contract after 14 days.")
    out = ai._competition_note_checked(text, facts)
    assert out and "521 Swansea Dr" in out and "$999,000" not in out and "6038 Freestone Blvd" in out


# --- the closest home for sale, against the value (seller reports) ---------------------------------------------------

def _trace(implied, match=99, dist=0.1):
    return {"pending_competition": {"actives_compared": [{"street_address": "521 SWANSEA DR", "list_price": 435000,
                                                          "implied_value": implied, "match_score": match,
                                                          "distance_miles": dist, "days_on_market": 20}]}}


def test_closest_active_below_the_value_is_explained():
    out, flags = ai._v10_active_context_sentence("The value is $451,000.", final={"value": 451000},
                                                 trace=_trace(436000), seller=True)
    assert "Homes for sale now (asking prices, not sales): the closest match, 521 Swansea Dr, 0.10 miles away, is asking $435,000" in out
    assert "priced a little below this home's value" in out and flags
    assert "$436,000" not in out                                              # no second price for this home
    assert ai._v10_active_context_sentence(out, final={"value": 451000}, trace=_trace(436000), seller=True)[0] == out


def test_closest_active_rules():
    v = {"value": 451000}
    assert "priced about the same as" in ai._v10_active_context_sentence("x.", final=v, trace=_trace(450000), seller=True)[0]
    assert "priced a little above" in ai._v10_active_context_sentence("x.", final=v, trace=_trace(480000), seller=True)[0]
    assert ai._v10_active_context_sentence("x.", final=v, trace=_trace(436000), seller=False)[0] == "x."      # buyer: unchanged
    assert ai._v10_active_context_sentence("x.", final=v, trace=_trace(436000, match=70), seller=True)[0] == "x."
    assert ai._v10_active_context_sentence("x.", final=v, trace=_trace(436000, dist=2.5), seller=True)[0] == "x."


# --- a sold home's old list price is not an asking price in the comp search ----------------------------------------

def test_stored_status_words_mean_off_market():
    for st in ("CLOSED-BRIGHT", "OFF_MARKET-BRIGHT", "EXPIRED-BRIGHT"):
        assert pick.asking_price_now({"list_price": 599500, "home_status": st}) is None
    assert pick.asking_price_now({"list_price": 599500, "home_status": "ACTIVE-BRIGHT"}) == 599500


def test_a_home_back_on_the_market_is_never_called_unsold(monkeypatch):
    monkeypatch.setattr(ai._tmach, "today", lambda: TODAY)
    still_listed = [e for e in WATKINS_EVENTS if e["event"] != "Taken off market"]
    s = fc.summarize_history(still_listed, current_list_date="2025-01-17", today=TODAY)
    assert ai._later_unsold_listing({"_listing_history": s}) is None
    assert "without selling" not in fc.history_sentence("The value is $423,000.", s, today=TODAY, listed=False)
