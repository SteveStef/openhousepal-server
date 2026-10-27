"""Google Maps calls stop at the free monthly allowance (owner, 10/26) - app/services/google_free_tier.py."""
import json

import httpx
import pytest

from app.services import google_free_tier as gft


def req(url, mask=None, body=None):
    headers = {"X-Goog-FieldMask": mask} if mask else {}
    return httpx.Request("POST", url, headers=headers, content=json.dumps(body).encode() if body is not None else None)


def test_each_call_is_counted_under_the_sku_google_bills():
    base = "places.googleapis.com/v1/places"
    assert gft.classify(req(f"https://{base}:searchText", "places.id,places.displayName,places.location,places.types,places.businessStatus")) == ("text_search_pro", "pro", 1)
    assert gft.classify(req(f"https://{base}:searchText", "places.id,places.displayName,places.userRatingCount")) == ("text_search_enterprise", "enterprise", 1)
    assert gft.classify(req(f"https://{base}:searchText", "places.id")) is None  # IDs only: free, no cap
    assert gft.classify(req(f"https://{base}:searchNearby", "places.displayName,places.location")) == ("nearby_search_pro", "pro", 1)
    route = req("https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", body={"origins": [1], "destinations": [1, 2, 3], "travelMode": "DRIVE"})
    assert gft.classify(route) == ("route_matrix_essentials", "essentials", 3)
    traffic = req("https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", body={"origins": [1], "destinations": [1, 2], "routingPreference": "TRAFFIC_AWARE"})
    assert gft.classify(traffic) == ("route_matrix_pro", "pro", 2)
    assert gft.classify(httpx.Request("GET", "https://maps.googleapis.com/maps/api/geocode/json?address=x")) == ("geocoding", "essentials", 1)
    assert gft.classify(httpx.Request("GET", "https://maps.googleapis.com/maps/api/streetview/metadata?location=x")) is None
    # Gmail, Calendar, Search Console are free - never counted
    assert gft.classify(httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages")) is None
    assert gft.classify(httpx.Request("GET", "https://www.googleapis.com/calendar/v3/calendars")) is None


def test_stops_just_under_the_free_allowance():
    assert gft.monthly_limit("pro") == 4750
    assert gft.monthly_limit("enterprise") == 950
    assert gft.monthly_limit("essentials") == 9500


@pytest.mark.asyncio
async def test_a_used_up_call_is_stopped_before_it_goes_out(monkeypatch):
    sent = []

    async def fake_original(self, request, *a, **k):
        sent.append(str(request.url))
        return httpx.Response(200, json={"places": []}, request=request)

    budget = {"left": 1}

    async def take(sku, tier, units):
        budget["left"] -= units
        return budget["left"] >= 0

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_original)
    monkeypatch.setattr(gft, "_installed", False)
    monkeypatch.setattr(gft, "_take", take)
    gft.install()
    async with httpx.AsyncClient() as c:
        await c.post("https://places.googleapis.com/v1/places:searchText", headers={"X-Goog-FieldMask": "places.displayName,places.location"}, json={})
        with pytest.raises(httpx.HTTPError):  # every caller's "Google failed" path catches this
            await c.post("https://places.googleapis.com/v1/places:searchText", headers={"X-Goog-FieldMask": "places.displayName,places.location"}, json={})
        await c.get("https://www.googleapis.com/calendar/v3/x")  # not Maps - never blocked
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_calls_google_saw_before_the_counter_started_count_toward_the_month(monkeypatch):
    monkeypatch.setattr(gft, "_used_locally", {})
    import app.database as database
    from app.services import usage_budget_service as ubs
    limits = []

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    async def reserve(_db, bucket, _ptype, _month, _units, _cost, limit, _cost_limit):
        limits.append((bucket, limit))
        return True
    monkeypatch.setattr(database, "AsyncSessionLocal", _Db)
    monkeypatch.setattr(ubs, "reserve_bucket", reserve)
    monkeypatch.setattr(ubs, "_period_keys", lambda: ("2026-10-26", "2026-10"))
    await gft._take("text_search_pro", "pro", 1)
    await gft._take("geocoding", "essentials", 1)
    assert limits == [("google_free:text_search_pro", 4750 - 3100), ("google_free:geocoding", 9500)]
    monkeypatch.setattr(ubs, "_period_keys", lambda: ("2026-11-01", "2026-11"))
    await gft._take("text_search_pro", "pro", 1)
    assert limits[-1] == ("google_free:text_search_pro", 4750)  # a new month starts clean


def test_live_server_adds_the_calls_made_on_the_owners_computer(monkeypatch):
    monkeypatch.setattr(gft, "_used_locally", {"2026-10": {"text_search_pro": 900}})
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@open-house-pal-postgresdb:5432/ohp")
    assert gft._used_before("text_search_pro", "2026-10") == 3100 + 900
    assert gft._used_before("text_search_pro", "2026-11") == 0
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/ohp")
    assert gft._used_before("text_search_pro", "2026-10") == 3100  # the local counter already has them
