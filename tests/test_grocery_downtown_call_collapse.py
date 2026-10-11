"""Regression tests for the Grocery/Downtown Google Places call-count optimizations - see the
Places API cost-experiment findings this implements. The Downtown tests exist specifically to
prevent ever silently reintroducing a per-candidate paid restaurant-validation call."""
import pytest

from app.services import nearby_amenities_service as svc


def place(name, types, lat=40.0, lng=-75.0, address=None):
    return {
        "id": name.lower().replace(" ", "-"),
        "displayName": {"text": name},
        "primaryType": types[0] if types else None,
        "types": types,
        "formattedAddress": address or "1 Main St, Test City, PA",
        "location": {"latitude": lat, "longitude": lng},
    }


# ---------------------------------------------------------------------------
# Grocery: 2 calls -> 1 call per radius tier
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_grocery_stores_makes_one_combined_call_per_tier_not_two(monkeypatch):
    calls = []

    async def fake_search_nearby_types(*args, **kwargs):
        included_types = args[4] if len(args) > 4 else kwargs.get("included_types")
        calls.append(included_types)
        return [
            place("Whole Foods Market", ["supermarket"], lat=40.0, lng=-75.001),
            place("ACME Markets", ["supermarket"], lat=40.0, lng=-75.01),
        ]

    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_grocery_stores(None, "fake-key", 40.0, -75.0)

    assert len(calls) == 1, f"expected exactly one combined grocery call for a tier that already found 2 valid results, got {len(calls)}"
    assert set(calls[0]) == {"supermarket", "grocery_store"}
    assert [r.name for r in results] == ["Whole Foods Market", "ACME Markets"]


@pytest.mark.asyncio
async def test_fetch_grocery_stores_still_escalates_radius_when_needed(monkeypatch):
    """Collapsing the 2 per-tier calls into 1 must not remove the existing radius-escalation
    behavior for sparse markets - only one fewer call per tier, not fewer tiers."""
    tier_radii = []

    async def fake_search_nearby_types(*args, **kwargs):
        tier_radii.append(kwargs.get("radius_miles"))
        if len(tier_radii) < 3:
            return []
        return [
            place("Only Grocery In Town", ["grocery_store"], lat=40.0, lng=-75.001),
        ]

    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    await svc._fetch_grocery_stores(None, "fake-key", 40.0, -75.0)

    assert tier_radii == [8, 12, 16, 24], f"expected all 4 radius tiers to still be tried (1 call each) when fewer than 2 valid results are ever found, got {tier_radii}"


def test_rejects_restricted_access_military_commissaries():
    """A real Google result, "Dover Commissary" (268 Galaxy St, Dover AFB, DE), was winning a
    grocery slot despite requiring military affiliation or an accompanied guest to shop there -
    not accessible to a typical home buyer. "Commissary" is used exclusively by the DoD's Defense
    Commissary Agency on-base grocery system, not any civilian chain, so this is a general rule,
    not a Dover-specific one."""
    assert not svc._is_full_service_grocery(place("Dover Commissary", ["grocery_store"]))
    assert not svc._is_full_service_grocery(place("Fort Bragg Commissary", ["grocery_store"]))
    assert not svc._is_full_service_grocery(place("Naval Station Norfolk Commissary", ["supermarket"]))
    # real public grocery stores must still pass
    assert svc._is_full_service_grocery(place("DG Market", ["grocery_store"]))
    assert svc._is_full_service_grocery(place("Food Lion", ["grocery_store"]))


@pytest.mark.asyncio
async def test_fetch_grocery_stores_falls_through_past_a_commissary(monkeypatch):
    """End-to-end: a closer commissary must not fill a slot - the real next-nearest legitimate
    public grocery store should win instead."""
    async def fake_search_nearby_types(*args, **kwargs):
        return [
            place("DG Market", ["grocery_store"], lat=40.0, lng=-75.001),
            place("Dover Commissary", ["grocery_store"], lat=40.0, lng=-75.01),
            place("Food Lion", ["grocery_store"], lat=40.0, lng=-75.02),
        ]

    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_grocery_stores(None, "fake-key", 40.0, -75.0)

    assert [r.name for r in results] == ["DG Market", "Food Lion"]


# ---------------------------------------------------------------------------
# Downtown: eliminate the per-candidate restaurant-validation loop entirely
# ---------------------------------------------------------------------------
class _FakeCityRows:
    def __init__(self, names):
        self._names = names

    def scalars(self):
        return self

    def all(self):
        return self._names


class _FakeDb:
    """Only supports the one query shape _known_locality_names actually issues."""
    def __init__(self, known_names: set[str]):
        self.known_names = known_names

    async def execute(self, _statement):
        return _FakeCityRows([n for n in self.known_names])


@pytest.mark.asyncio
async def test_fetch_downtown_areas_makes_exactly_one_call_total(monkeypatch):
    """Regression: must never issue a second Google call for downtown, under any circumstance -
    this is the actual assertion protecting against reintroducing per-candidate validation."""
    calls = []

    async def fake_search_nearby(*args, **kwargs):
        calls.append(kwargs.get("included_type") if "included_type" in kwargs else (args[4] if len(args) > 4 else None))
        return [
            place("Ardmore", ["locality"], lat=40.001, lng=-75.001),
            place("Narberth", ["locality"], lat=40.002, lng=-75.002),
            place("Some Random Estate", ["locality"], lat=40.003, lng=-75.003),
        ]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    db = _FakeDb(known_names={"ardmore", "narberth"})

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, db)

    assert len(calls) == 1, f"expected exactly one Google call (the locality search) for downtown areas, got {len(calls)}: {calls}"
    assert calls[0] == "locality"
    assert [r.name for r in results] == ["Ardmore", "Narberth"]


@pytest.mark.asyncio
async def test_fetch_downtown_areas_never_calls_restaurant_search(monkeypatch):
    """Explicit guard against silently reintroducing the old per-candidate commercial-core
    validation call - patches _search_nearby to fail loudly if it's ever called with
    included_type='restaurant', then drives _fetch_downtown_areas through several candidates to
    prove the validation path is truly gone, not just absent from this one code path today."""
    async def fake_search_nearby(*args, **kwargs):
        included_type = args[4] if len(args) > 4 else kwargs.get("included_type")
        assert included_type != "restaurant", "downtown areas must never issue a per-candidate restaurant-validation call"
        return [place(f"Town{i}", ["locality"], lat=40.0 + i * 0.01, lng=-75.0) for i in range(5)]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    db = _FakeDb(known_names={f"town{i}".lower() for i in range(5)})

    await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, db)

    assert not hasattr(svc, "_commercial_core_size"), "the restaurant-validation helper should be fully removed, not just unused"


@pytest.mark.asyncio
async def test_fetch_downtown_areas_drops_unrecognized_named_places(monkeypatch):
    async def fake_search_nearby(*args, **kwargs):
        return [
            place("Georgetown", ["locality"], lat=38.1, lng=-75.1),
            place("Redden", ["locality"], lat=38.11, lng=-75.11),
            place("Milton", ["locality"], lat=38.12, lng=-75.12),
            place("Flea Hill", ["locality"], lat=38.13, lng=-75.13),
        ]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    db = _FakeDb(known_names={"georgetown", "milton"})

    results = await svc._fetch_downtown_areas(None, "fake-key", 38.1, -75.1, db)

    assert [r.name for r in results] == ["Georgetown", "Milton"]


@pytest.mark.asyncio
async def test_fetch_downtown_areas_preserves_googles_natural_return_order(monkeypatch):
    """Google's default rankPreference is POPULARITY - the un-reordered response list is the
    busyness signal the old validation loop used to pay to reconstruct. A closer-but-later-in-the-
    list known town must not be promoted ahead of an earlier one just for being nearer."""
    async def fake_search_nearby(*args, **kwargs):
        return [
            place("Narberth", ["locality"], lat=40.05, lng=-75.05),   # farther, but ranked first by Google
            place("Ardmore", ["locality"], lat=40.001, lng=-75.001),  # closer, but ranked second by Google
        ]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    db = _FakeDb(known_names={"ardmore", "narberth"})

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, db)

    assert [r.name for r in results] == ["Narberth", "Ardmore"]


@pytest.mark.asyncio
async def test_fetch_downtown_areas_caps_at_max_results_per_category(monkeypatch):
    async def fake_search_nearby(*args, **kwargs):
        return [place(f"Town{i}", ["locality"], lat=40.0 + i * 0.01, lng=-75.0) for i in range(10)]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    db = _FakeDb(known_names={f"town{i}".lower() for i in range(10)})

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, db)

    assert len(results) == svc.MAX_RESULTS_PER_CATEGORY
