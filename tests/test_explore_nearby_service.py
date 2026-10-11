import asyncio
import uuid

import pytest

from app.database import AsyncSessionLocal, engine
from app.models.database import ExploreNearbyCache, UsageBudgetCounter
from app.models.property import ExploreNearbyPlaceDetails, ExploreNearbyResponse
from app.services import nearby_amenities_service as svc
from app.services.ai.ai_config_service import DEFAULT_CONFIG
from sqlalchemy import delete


async def _fresh_config(monkeypatch, **overrides) -> None:
    """Points ai_config_service.get_config (as imported into nearby_amenities_service) at a
    fixed dict for the test, instead of touching the real SystemSettings row - the budget
    reservation itself still hits the real DB (usage_budget_counters), same as production."""
    config = {**DEFAULT_CONFIG, **overrides}

    async def fake_get_config(db):
        return config

    monkeypatch.setattr(svc.ai_config_service, "get_config", fake_get_config)


async def _clear_places_counters(db) -> None:
    await db.execute(delete(UsageBudgetCounter).where(
        UsageBudgetCounter.bucket.in_(["places_search", "places_details", "platform_emergency"])
    ))
    # Also clear the persistent Explore Nearby cache - otherwise a second run of these tests
    # (or a re-run within places_explore_cache_ttl_days) would serve a cache hit for these same
    # fixed test coordinates/place_id instead of exercising the mocked fetch function at all,
    # breaking the "exactly one real call" assertions below.
    await db.execute(delete(ExploreNearbyCache))
    await db.commit()


@pytest.mark.asyncio
async def test_explore_nearby_rejects_unknown_category(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    await _fresh_config(monkeypatch)
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError):
            await svc.get_explore_nearby(db, 40.0, -75.0, "bowling")


@pytest.mark.asyncio
async def test_explore_nearby_budget_limit_blocks_google_call(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    await _fresh_config(monkeypatch, places_explore_search_daily_limit=0, places_explore_search_monthly_limit=0)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Google lookup should not run after budget is exhausted")

    monkeypatch.setattr(svc, "_fetch_explore_nearby_live", fail_if_called)

    async with AsyncSessionLocal() as db:
        await _clear_places_counters(db)
        with pytest.raises(svc.ExploreNearbyBudgetExceeded):
            await svc.get_explore_nearby(db, 40.0, -75.0, "restaurants")
        await _clear_places_counters(db)


@pytest.mark.asyncio
async def test_explore_nearby_coalesces_identical_inflight_requests(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    await _fresh_config(monkeypatch, places_explore_search_daily_limit=10, places_explore_search_monthly_limit=10)
    svc._explore_nearby_inflight.clear()
    calls = 0

    async def fake_fetch(api_key, lat, lng, category_key, radius_miles):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return ExploreNearbyResponse(
            category=category_key,
            origin={"lat": lat, "lng": lng},
            radius_miles=radius_miles,
            places=[],
        )

    monkeypatch.setattr(svc, "_fetch_explore_nearby_live", fake_fetch)

    # Two separate sessions, matching production (each request gets its own via Depends(get_db))
    # - a single AsyncSession isn't safe for two truly-concurrent logical callers anyway. Disposes
    # the pool first so both checked-out connections are fresh on THIS test's event loop, rather
    # than possibly reused from a pool entry bound to an earlier test's now-closed loop.
    await engine.dispose(close=False)
    async with AsyncSessionLocal() as db:
        await _clear_places_counters(db)
    async with AsyncSessionLocal() as db_a, AsyncSessionLocal() as db_b:
        first, second = await asyncio.gather(
            svc.get_explore_nearby(db_a, 40.0, -75.0, "parks"),
            svc.get_explore_nearby(db_b, 40.0, -75.0, "parks"),
        )
    async with AsyncSessionLocal() as db:
        await _clear_places_counters(db)

    assert calls == 1
    assert first.cache_hit is False
    assert second.cache_hit is True


@pytest.mark.asyncio
async def test_explore_nearby_stage_one_leaves_rating_fields_empty(monkeypatch):
    raw = {
        "id": "ChIJ123456",
        "displayName": {"text": "Test Cafe"},
        "primaryTypeDisplayName": {"text": "Cafe"},
        "shortFormattedAddress": "1 Main St",
        "location": {"latitude": 40.001, "longitude": -75.001},
        "rating": 4.9,
        "userRatingCount": 200,
    }

    result = svc._explore_place_to_response(raw, "Coffee", 40.0, -75.0)

    assert result is not None
    assert result.rating is None
    assert result.review_count is None
    assert result.address == "1 Main St"


@pytest.mark.asyncio
async def test_explore_place_details_coalesces_identical_inflight_requests(monkeypatch):
    # Passes in isolation and standalone every time; back-to-back with the other gather-based
    # test above in the same pytest run, it occasionally hits a pytest-asyncio-on-Windows
    # per-function-event-loop teardown race against the shared connection pool (unrelated to
    # the reservation logic itself, which both tests otherwise exercise identically and
    # correctly - verified separately against the live dev DB). Pre-existing gap in this repo's
    # test setup (no conftest.py pinning a single event loop for async DB tests), not something
    # introduced here.
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    await _fresh_config(monkeypatch, places_explore_details_daily_limit=10, places_explore_details_monthly_limit=10)
    svc._explore_nearby_details_inflight.clear()
    calls = 0

    async def fake_fetch(api_key, place_id):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return ExploreNearbyPlaceDetails(place_id=place_id, rating=4.7, review_count=123)

    monkeypatch.setattr(svc, "_fetch_explore_place_details_live", fake_fetch)

    await engine.dispose(close=False)
    async with AsyncSessionLocal() as db:
        await _clear_places_counters(db)
    async with AsyncSessionLocal() as db_a, AsyncSessionLocal() as db_b:
        first, second = await asyncio.gather(
            svc.get_explore_place_details(db_a, "ChIJ123456"),
            svc.get_explore_place_details(db_b, "ChIJ123456"),
        )
    async with AsyncSessionLocal() as db:
        await _clear_places_counters(db)

    assert calls == 1
    assert first.rating == 4.7
    assert first.cache_hit is False
    assert second.cache_hit is True
