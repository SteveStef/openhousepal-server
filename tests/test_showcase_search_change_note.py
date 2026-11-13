"""The agent's bell note when a client changes their own Showcase search (audit 11/13): only what
changed, in normal capitals - the window sends every field, so it used to list "Price: Any ·
Bathrooms: Any · Towns: BERWYN, PA ..." for a beds-only change."""
from types import SimpleNamespace

from app.api.collections_routes import _describe_search_change, _only_changed, _place


def saved(**kw):
    base = dict(min_price=None, max_price=None, min_beds=None, max_beds=None, min_baths=None, max_baths=None,
                address=None, diameter=None, cities=["BERWYN, PA"], school_districts=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_beds_only_change_lists_only_beds():
    payload = dict(min_price=None, max_price=None, min_beds=3, max_beds=None, min_baths=None, max_baths=None,
                   cities=["BERWYN, PA"], school_districts=[])
    rows = _describe_search_change(_only_changed(payload, saved()))
    assert rows == [{"label": "Bedrooms", "value": "3+"}]


def test_max_price_change_keeps_saved_min():
    payload = dict(max_price=900000)
    rows = _describe_search_change(_only_changed(payload, saved(min_price=500000, max_price=800000)))
    assert rows == [{"label": "Price", "value": "$500,000-$900,000"}]


def test_town_change_tidy_names():
    payload = dict(cities=["BERWYN, PA", "WAYNE, PA"])
    rows = _describe_search_change(_only_changed(payload, saved()))
    assert rows == [{"label": "Towns", "value": "Berwyn, Wayne"}]


def test_town_order_or_case_is_not_a_change():
    assert _only_changed(dict(cities=["berwyn, pa"]), saved()) == {}


def test_place():
    assert _place("BERWYN, PA") == "Berwyn, PA"
    assert _place("Tredyffrin-Easttown") == "Tredyffrin-Easttown"
    assert _place("") == ""


def test_no_saved_prefs_describes_everything():
    assert _describe_search_change(_only_changed(dict(min_beds=2), None)) == [{"label": "Bedrooms", "value": "2+"}]
