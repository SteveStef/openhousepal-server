"""Owner 11/20 (IDX town page, Day-to-day map): "show the town on the map, not district, not the school - just Wayne -
then show restaurants in there". The town's places are searched across its own outline and only the ones inside it
are shown, nearest the town center first - from the saved search, no Google call."""
import asyncio

import app.main  # noqa: F401
from app.services import nearby_amenities_service as nas

# a 2 x 2 mile-ish square town around (40.04, -75.39)
RINGS = [[[-75.41, 40.025], [-75.37, 40.025], [-75.37, 40.055], [-75.41, 40.055], [-75.41, 40.025]]]


def _place(i, lat, lng):
    return {"place_id": f"p{i}", "name": f"Place {i}", "category": "Restaurant", "latitude": lat, "longitude": lng,
            "distance_miles": 0.0, "rating": None, "user_rating_count": None, "address": None}


def test_search_reaches_from_the_town_center_never_past_the_town():
    r = nas._town_reach(RINGS, 40.04, -75.39, 2.5)
    assert 0.75 <= r <= 2.5                                   # a small town: less than the usual 2.5 miles
    assert nas._town_reach([[[-76.5, 39.0], [-74.0, 39.0], [-74.0, 41.0], [-76.5, 41.0], [-76.5, 39.0]]], 40.0, -75.3, 2.5) == 2.5


def test_only_places_inside_the_town_nearest_first(monkeypatch):
    inside = [_place(i, 40.03 + i * 0.002, -75.40 + i * 0.002) for i in range(8)]
    outside = [_place(100, 40.10, -75.39), _place(101, 40.04, -75.30)]
    payload = {"category": "restaurants", "origin": {"lat": 40.04, "lng": -75.39}, "radius_miles": 2.0,
               "places": outside + inside, "cache_hit": False, "sku": "nearby_search_pro"}

    async def cached(db, key, ttl):
        assert key.startswith(f"search:v{nas.EXPLORE_NEARBY_CACHE_VERSION}:town:area-1:")   # its own key per town
        return payload

    async def config(db):
        return {"places_explore_cache_ttl_days": 7}

    monkeypatch.setattr(nas, "_read_explore_cache", cached)
    monkeypatch.setattr(nas.ai_config_service, "get_config", config)
    out = asyncio.run(nas.get_explore_nearby(None, 40.04, -75.39, "restaurants", town={"key": "area-1", "rings": RINGS}))
    ids = [p.place_id for p in out.places]
    assert "p100" not in ids and "p101" not in ids                           # outside the town: never shown
    assert len(ids) == 8                                                      # every place inside the town (the page shows 10, then Show more)
    d = [p.distance_miles for p in out.places]
    assert d == sorted(d)                                                     # nearest the town center first


def _rated(i, lat, lng, rating, n):
    return {**_place(i, lat, lng), "rating": rating, "review_count": n}


def test_best_rated_first_weighs_how_many_reviews():
    big = {"rating": 4.8, "review_count": 900}
    tiny = {"rating": 5.0, "review_count": 3}
    none = {"rating": None, "review_count": None}
    assert nas._rated_score(big) > nas._rated_score(tiny) > nas._rated_score(none)


def _run(monkeypatch, share, cache):
    async def cached(db, key, ttl):
        return cache.get("rated" if key.endswith(":rated") else "plain")

    async def config(db):
        return {"places_explore_cache_ttl_days": 7}

    async def used(sku):
        assert sku == "text_search_enterprise"
        return share

    from app.services import google_free_tier
    monkeypatch.setattr(nas, "_read_explore_cache", cached)
    monkeypatch.setattr(nas.ai_config_service, "get_config", config)
    monkeypatch.setattr(google_free_tier, "share_used", used)
    return asyncio.run(nas.get_explore_nearby(None, 40.04, -75.39, "restaurants", town={"key": "area-1", "rings": RINGS}))


def test_rated_search_sorts_best_first(monkeypatch):
    places = [_rated(1, 40.031, -75.399, 4.2, 40), _rated(2, 40.035, -75.395, 4.9, 600), _rated(3, 40.04, -75.39, 3.9, 10)]
    out = _run(monkeypatch, 0.1, {"rated": {"category": "restaurants", "origin": {}, "radius_miles": 2.0, "places": places}})
    assert [p.place_id for p in out.places] == ["p2", "p1", "p3"]


def test_past_80_percent_the_plain_search_takes_over(monkeypatch):
    plain = [_place(1, 40.031, -75.399), _place(2, 40.035, -75.395)]
    out = _run(monkeypatch, 0.85, {"plain": {"category": "restaurants", "origin": {}, "radius_miles": 2.0, "places": plain}})
    assert {p.place_id for p in out.places} == {"p1", "p2"}          # same places, no stars, no new rated search
    assert all(p.rating is None for p in out.places)


def test_google_answer_gives_price_and_open_now():
    p = nas._explore_place_to_response({"id": "abc12345", "displayName": {"text": "Minella's Diner"}, "location": {"latitude": 40.04, "longitude": -75.39},
                                        "rating": 4.5, "userRatingCount": 1200, "priceLevel": "PRICE_LEVEL_MODERATE",
                                        "regularOpeningHours": {"periods": [{"open": {"day": 1, "hour": 7, "minute": 0}, "close": {"day": 1, "hour": 15, "minute": 0}}]}},
                                       "Restaurant", 40.04, -75.39, rated=True)
    assert (p.rating, p.review_count, p.price_level) == (4.5, 1200, 2) and p.hours


def test_open_now_is_worked_out_at_the_moment_shown():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    mon_7_to_3 = [{"open": {"day": 1, "hour": 7, "minute": 0}, "close": {"day": 1, "hour": 15, "minute": 0}}]
    late_fri = [{"open": {"day": 5, "hour": 17, "minute": 0}, "close": {"day": 6, "hour": 2, "minute": 0}}]   # past midnight
    assert nas._open_now(mon_7_to_3, datetime(2026, 11, 23, 9, 30, tzinfo=et)) is True        # Monday 9:30 AM
    assert nas._open_now(mon_7_to_3, datetime(2026, 11, 23, 16, 0, tzinfo=et)) is False       # Monday 4 PM
    assert nas._open_now(late_fri, datetime(2026, 11, 21, 1, 0, tzinfo=et)) is True           # Saturday 1 AM
    assert nas._open_now([{"open": {"day": 0, "hour": 0, "minute": 0}}], datetime(2026, 11, 23, 3, 0, tzinfo=et)) is True   # 24/7
    assert nas._open_now(None) is None


def test_google_refusing_the_rated_search_mid_request_still_shows_places(monkeypatch):
    """Under 80% we start a rated search; if Google's free-tier guard refuses it right then (it stops at 95%), the SAME
    request runs the plain search instead - the visitor gets the places (no stars) and it's saved under the plain key."""
    from app.services import google_free_tier
    written, calls = {}, []

    async def cached(db, key, ttl):
        return None

    async def write(db, key, payload):
        written[key] = payload

    async def config(db):
        return {"places_explore_cache_ttl_days": 7}

    async def used(sku):
        return 0.79   # just under the switch - so it tries the rated one

    async def budget(*a, **k):
        return None

    async def fetch(api_key, rings, place, category, rated, lat, lng):
        calls.append(rated)
        if rated:
            raise google_free_tier.GoogleFreeTierPaused("free calls used up", request=None)
        return nas.ExploreNearbyResponse(category=category, origin={"lat": lat, "lng": lng}, radius_miles=0.0,
                                         places=[nas.ExploreNearbyPlace(**{k: v for k, v in _place(1, 40.03, -75.40).items() if k != "user_rating_count"})],
                                         cache_hit=False, sku="text_search_pro")

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    monkeypatch.setattr(nas, "_read_explore_cache", cached)
    monkeypatch.setattr(nas, "_write_explore_cache", write)
    monkeypatch.setattr(nas.ai_config_service, "get_config", config)
    monkeypatch.setattr(google_free_tier, "share_used", used)
    monkeypatch.setattr(nas, "_check_and_reserve_explore_budget_locked", budget)
    monkeypatch.setattr(nas, "_fetch_town_text_live", fetch)
    out = asyncio.run(nas.get_explore_nearby(None, 40.04, -75.39, "restaurants", town={"key": "area-1", "rings": RINGS, "name": "Wayne, PA"}))
    assert calls == [True, False]                                   # rated tried, refused, plain ran in the same request
    assert [p.place_id for p in out.places] == ["p1"] and out.places[0].rating is None
    assert list(written) == [f"search:v{nas.EXPLORE_NEARBY_CACHE_VERSION}:town:area-1:restaurants:text"]   # saved as the plain search
