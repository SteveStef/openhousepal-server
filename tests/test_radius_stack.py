"""Towns + a search center stack (owner, 11/09): the circle narrows picked towns/counties; alone it's its own
area. New-listing matcher side (property_sync_service._area_ok) - no database."""
from types import SimpleNamespace

from app.services.property_sync_service import _area_ok

LAT, LNG = 40.036, -75.515            # Malvern-ish
NEAR = (LAT + 0.002, LNG)             # ~0.14 mi
FAR = (LAT + 0.05, LNG)               # ~3.5 mi


def prefs(**k):
    base = dict(cities=[], townships=[], counties=[], search_polygon=None, lat=None, long=None, diameter=None)
    return SimpleNamespace(**{**base, **k})


def test_town_and_circle_both_needed():
    p = prefs(cities=["MALVERN, PA"], lat=LAT, long=LNG, diameter=1)
    assert _area_ok(p, "MALVERN", None, "CHESTER", *NEAR)
    assert not _area_ok(p, "MALVERN", None, "CHESTER", *FAR)
    assert not _area_ok(p, "PAOLI", None, "CHESTER", *NEAR)


def test_town_alone_and_circle_alone():
    assert _area_ok(prefs(cities=["MALVERN, PA"]), "MALVERN", None, "CHESTER", *FAR)
    assert _area_ok(prefs(lat=LAT, long=LNG, diameter=1), "PAOLI", None, "CHESTER", *NEAR)
    assert not _area_ok(prefs(lat=LAT, long=LNG, diameter=1), "MALVERN", None, "CHESTER", *FAR)


def test_county_is_narrowed_too_and_a_shape_still_adds():
    assert not _area_ok(prefs(counties=["CHESTER-PA"], lat=LAT, long=LNG, diameter=1), "MALVERN", None, "CHESTER", *FAR)
    d = 0.01
    shape = {"type": "Polygon", "coordinates": [[[FAR[1] - d, FAR[0] - d], [FAR[1] + d, FAR[0] - d], [FAR[1] + d, FAR[0] + d], [FAR[1] - d, FAR[0] + d], [FAR[1] - d, FAR[0] - d]]]}
    assert _area_ok(prefs(cities=["MALVERN, PA"], lat=LAT, long=LNG, diameter=1, search_polygon=shape), "MALVERN", None, "CHESTER", *FAR)
