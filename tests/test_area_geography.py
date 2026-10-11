from types import SimpleNamespace

from app.services.areas import geography


def area(**kwargs):
    defaults = {
        "name": "Villanova",
        "state": "PA",
        "area_type": "city",
        "parent_area_name": None,
        "zip_codes": ["19085"],
        "centroid_lat": None,
        "centroid_lng": None,
        "geography": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_city_scope_uses_stored_zip_codes():
    scope = geography.scope_for_market_area(area())

    assert scope["kind"] == "zip_city"
    assert scope["zip_codes"] == ["19085"]
    assert scope["city"] == "Villanova"


def test_neighborhood_scope_uses_stored_bounds():
    scope = geography.scope_for_market_area(area(
        name="Rittenhouse Square",
        area_type="neighborhood",
        parent_area_name="Philadelphia",
        zip_codes=["19103"],
        geography={
            "type": "bounds",
            "bounds": {"min_lat": 39.945, "max_lat": 39.955, "min_lng": -75.18, "max_lng": -75.16},
            "source": "google_places_viewport",
        },
    ))

    assert scope["kind"] == "bounds"
    assert scope["parent_city"] == "Philadelphia"
    assert scope["bounds"]["min_lat"] == 39.945


def test_neighborhood_scope_prefers_stored_polygon():
    polygon = {
        "type": "Polygon",
        "coordinates": [[
            [-75.18, 39.94],
            [-75.16, 39.94],
            [-75.16, 39.96],
            [-75.18, 39.96],
            [-75.18, 39.94],
        ]],
    }
    scope = geography.scope_for_market_area(area(
        name="Rittenhouse Square",
        area_type="neighborhood",
        parent_area_name="Philadelphia",
        geography={
            "type": "polygon",
            "geometry": polygon,
            "bounds": {"min_lat": 39.94, "max_lat": 39.96, "min_lng": -75.18, "max_lng": -75.16},
            "source": "test",
        },
    ))

    assert scope["kind"] == "polygon"
    assert scope["geometry"] == polygon


def test_legacy_neighborhood_scope_uses_centroid_radius_not_zip_city():
    scope = geography.scope_for_market_area(area(
        name="Rittenhouse Square",
        area_type="neighborhood",
        parent_area_name="Philadelphia",
        zip_codes=["19103"],
        centroid_lat=39.9496,
        centroid_lng=-75.1718,
        geography={},
    ))

    assert scope["kind"] == "radius"
    assert scope["center"] == {"lat": 39.9496, "lng": -75.1718}
    assert scope["radius_miles"] == geography.DEFAULT_NEIGHBORHOOD_RADIUS_MILES


def test_geography_precision_rank_orders_polygon_above_bounds_and_radius():
    assert geography.is_more_precise({"type": "polygon"}, {"type": "bounds"})
    assert geography.is_more_precise({"type": "bounds"}, {"type": "radius"})
    assert not geography.is_more_precise({"type": "radius"}, {"type": "polygon"})
