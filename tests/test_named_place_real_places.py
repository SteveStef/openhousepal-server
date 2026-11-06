"""Adding a neighborhood: a search that finds only businesses is not a place (owner, 11/06: "Main Line"
came back as Main Line Pizza & Catering, then the save crashed)."""
import pytest

from app.services.areas import geocoding


class _Reply:
    def __init__(self, places):
        self._places = places

    def raise_for_status(self):
        return None

    def json(self):
        return {"places": self._places}


def _google_says(monkeypatch, places):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Reply(places)

    async def no_charge(*a, **k):
        return None

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test")
    monkeypatch.setattr(geocoding.httpx, "AsyncClient", Client)
    monkeypatch.setattr(geocoding, "charge_google", no_charge)


PIZZA = {"displayName": {"text": "Main Street Pizza"}, "types": ["pizza_restaurant", "restaurant", "point_of_interest", "establishment"],
         "location": {"latitude": 40.0, "longitude": -75.4}}
TOWN = {"displayName": {"text": "Philadelphia"}, "types": ["locality", "political"], "location": {"latitude": 39.95, "longitude": -75.16},
        "addressComponents": [{"types": ["locality"], "longText": "Philadelphia"}, {"types": ["administrative_area_level_1"], "shortText": "PA"}]}


@pytest.mark.asyncio
async def test_main_line_is_a_known_region_without_asking_google(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    r = await geocoding.resolve_named_place("  main line ", "pa")
    assert r["name"] == "Main Line" and r["state"] == "PA" and r["geography"]["type"] == "bounds"


@pytest.mark.asyncio
async def test_only_businesses_means_not_found(monkeypatch):
    _google_says(monkeypatch, [PIZZA])
    assert await geocoding.resolve_named_place("Main Street", "PA") is None


@pytest.mark.asyncio
async def test_a_town_typed_as_a_neighborhood_says_so(monkeypatch):
    _google_says(monkeypatch, [PIZZA, TOWN])
    r = await geocoding.resolve_named_place("Philly", "PA")
    assert r["name"] == "Philadelphia" and r["is_city"] is True
