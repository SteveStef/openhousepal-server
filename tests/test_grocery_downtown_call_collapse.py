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
    # real public grocery stores must still pass - a Dollar General "DG Market" is a dollar store (10/25)
    assert not svc._is_full_service_grocery(place("DG Market", ["grocery_store"]))
    assert svc._is_full_service_grocery(place("Food Lion", ["grocery_store"]))


@pytest.mark.asyncio
async def test_fetch_grocery_stores_falls_through_past_a_commissary(monkeypatch):
    """End-to-end: a closer commissary must not fill a slot - the real next-nearest legitimate
    public grocery store should win instead."""
    async def fake_search_nearby_types(*args, **kwargs):
        return [
            place("Safeway", ["grocery_store"], lat=40.0, lng=-75.001),
            place("Dover Commissary", ["grocery_store"], lat=40.0, lng=-75.01),
            place("Food Lion", ["grocery_store"], lat=40.0, lng=-75.02),
        ]

    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_grocery_stores(None, "fake-key", 40.0, -75.0)

    assert [r.name for r in results] == ["Safeway", "Food Lion"]


# ---------------------------------------------------------------------------
# Downtown: real town centers only (10/25 audit)
# ---------------------------------------------------------------------------
def town(name, lat, lng, state="PA"):
    p = place(name, ["locality"], lat=lat, lng=lng)
    p["formattedAddress"] = f"{name}, {state}, USA"
    return p


def _patch_downtown(monkeypatch, localities, scores, census=()):
    """scores: name -> (score, size) of restaurants + shops around that town's point."""
    paid = []
    by_point = {}

    async def fake_search_nearby(*args, **kwargs):
        paid.append(args[4] if len(args) > 4 else kwargs.get("included_type"))
        return localities

    async def fake_score(client, api_key, lat, lng, half_miles=svc.DOWNTOWN_CORE_HALF_MILES):
        return by_point.get((round(lat, 4), round(lng, 4)))

    for p in localities:
        by_point[(round(p["location"]["latitude"], 4), round(p["location"]["longitude"], 4))] = scores.get(p["displayName"]["text"])
    for name, state, lat, lng, is_cdp in census:
        by_point[(round(lat, 4), round(lng, 4))] = scores.get(name)
    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    monkeypatch.setattr(svc, "_town_center_score", fake_score)
    monkeypatch.setattr(svc, "_census_places_near", lambda lat, lng, miles: list(census))
    return paid


@pytest.mark.asyncio
async def test_downtown_makes_one_paid_call_and_keeps_only_real_town_centers(monkeypatch):
    """Real sheets showed "Edgmont", "Evesham", "Deptford", "Cherry Hill Township", "Gulph Mills" and
    subdivision names as downtowns. The restaurant/shop counts are Google's free IDs-only search, so
    the locality search stays the only paid call."""
    localities = [
        town("Media", 40.01, -75.0),               # borough with a main street
        town("Rose Valley", 40.02, -75.0),         # borough, no businesses
        town("Cherry Hill Township", 40.03, -75.0),
        town("Edgmont", 40.04, -75.0),             # township name pinned on a shopping strip
        town("Larchmont Meadows", 40.05, -75.0),   # subdivision next to a busy road
        town("Havertown", 40.06, -75.0),           # real, unofficial, a full main street
    ]
    scores = {"Media": (40, 80), "Rose Valley": (0, 0), "Cherry Hill Township": (40, 80), "Edgmont": (22, 22),
              "Larchmont Meadows": (33, 33), "Havertown": (38, 50)}
    paid = _patch_downtown(monkeypatch, localities, scores)

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, None)

    assert paid == ["locality"]
    assert [r.name for r in results] == ["Media", "Havertown"]


@pytest.mark.asyncio
async def test_downtown_adds_incorporated_towns_google_left_out(monkeypatch):
    """The Shaker Ln, West Chester sheet never listed West Chester - Google's locality list was all
    subdivision names. Census-designated communities aren't added this way (Haverford College is one)."""
    localities = [town("Willistown Hunt", 40.001, -75.0)]
    census = [("West Chester", "PA", 40.06, -75.0, False), ("Haverford College", "PA", 40.02, -75.0, True)]
    scores = {"Willistown Hunt": (0, 0), "West Chester": (40, 80), "Haverford College": (40, 80)}
    _patch_downtown(monkeypatch, localities, scores, census)

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, None)

    assert [r.name for r in results] == ["West Chester"]


@pytest.mark.asyncio
async def test_downtown_collapses_one_place_with_two_names_preferring_the_homes_town(monkeypatch):
    localities = [town("Rosemont", 40.010, -75.0), town("Bryn Mawr", 40.014, -75.0), town("Ardmore", 40.05, -75.0)]
    scores = {"Rosemont": (40, 80), "Bryn Mawr": (40, 80), "Ardmore": (40, 80)}
    _patch_downtown(monkeypatch, localities, scores)

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, None, home_city="BRYN MAWR")

    assert [r.name for r in results] == ["Bryn Mawr", "Ardmore"]


@pytest.mark.asyncio
async def test_downtown_caps_at_max_results_per_category(monkeypatch):
    localities = [town(f"Town{i}", 40.0 + i * 0.02, -75.0) for i in range(10)]
    _patch_downtown(monkeypatch, localities, {f"Town{i}": (40, 80) for i in range(10)})
    monkeypatch.setattr(svc, "_town_kind", lambda place: "incorporated")

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, None)

    assert len(results) == svc.MAX_RESULTS_PER_CATEGORY


def test_town_kind_reads_the_census_lists():
    assert svc._town_kind(town("Media", 40, -75)) == "incorporated"
    assert svc._town_kind(town("Wayne", 40, -75)) == "cdp"
    assert svc._town_kind(town("Moorestown", 40, -75, state="NJ")) == "township"
    assert svc._town_kind(town("Larchmont Meadows", 40, -75)) == "other"


@pytest.mark.asyncio
async def test_grocery_prefers_two_different_real_supermarkets(monkeypatch):
    """10/25 grader round: "Trader Joe's | Trader Joe's", "ShopRite of Sewell | ShopRite of Glassboro"
    and a farm market ahead of a supermarket were on real sheets - two different real grocers win."""
    async def fake_search_nearby_types(*args, **kwargs):
        return [
            place("Trader Joe's", ["grocery_store"], lat=40.0, lng=-75.001, address="1 A St"),
            place("Linvilla Orchards", ["grocery_store"], lat=40.0, lng=-75.002),
            place("Trader Joe's", ["grocery_store"], lat=40.0, lng=-75.003, address="2 B St"),
            place("ShopRite of Brookhaven", ["grocery_store"], lat=40.0, lng=-75.03),
        ]

    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_grocery_stores(None, "fake-key", 40.0, -75.0)

    assert [r.name for r in results] == ["Trader Joe's", "ShopRite of Brookhaven"]


@pytest.mark.asyncio
async def test_downtown_uses_ai_names_only_when_our_data_backs_them(monkeypatch):
    """The AI (ai_downtown_service) names the downtowns locals would - "Marlton" for the township
    "Evesham" - but every name must still show restaurants + shops at its point: an AI pick with
    almost nothing there (Wenonah, 4) never prints."""
    localities = [town("Evesham", 40.01, -75.0, state="NJ"), town("Moorestown", 40.05, -75.0, state="NJ"),
                  town("Wenonah", 40.07, -75.0, state="NJ")]
    scores = {"Evesham": (33, 33), "Moorestown": (34, 40), "Wenonah": (4, 4)}
    _patch_downtown(monkeypatch, localities, scores)
    seen = {}

    async def fake_picks(db, city, state, zipcode, candidates):
        seen["candidates"] = candidates
        return [{"name": "Marlton", "state": "NJ", "kind": "downtown", "main_street": "Main Street"},
                {"name": "Moorestown", "state": "NJ", "kind": "downtown", "main_street": None},
                {"name": "Wenonah", "state": "NJ", "kind": "downtown", "main_street": None}]

    async def fake_search_text(client, api_key, lat, lng, query, radius, **kwargs):
        assert query == "Main Street, Marlton, NJ"
        return [{"location": {"latitude": 40.012, "longitude": -75.0}}]

    real_score = svc._town_center_score

    async def score_with_marlton(client, api_key, lat, lng, half_miles=svc.DOWNTOWN_CORE_HALF_MILES):
        if round(lat, 3) == 40.012:
            return (38, 60)
        return await real_score(client, api_key, lat, lng, half_miles)

    import app.services.ai.ai_downtown_service as ai_downtown
    monkeypatch.setattr(ai_downtown, "downtown_picks", fake_picks)
    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_town_center_score", score_with_marlton)

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, None, home_city="MARLTON", home_state="NJ")

    assert [r.name for r in results] == ["Marlton", "Moorestown"]
    passed = {c["name"]: c["passed"] for c in seen["candidates"]}
    assert passed == {"Evesham": True, "Moorestown": True, "Wenonah": False}


@pytest.mark.asyncio
async def test_downtown_keeps_the_data_list_when_the_ai_has_no_answer(monkeypatch):
    localities = [town("Media", 40.01, -75.0)]
    _patch_downtown(monkeypatch, localities, {"Media": (40, 80)})

    async def no_answer(*args, **kwargs):
        return None

    import app.services.ai.ai_downtown_service as ai_downtown
    monkeypatch.setattr(ai_downtown, "downtown_picks", no_answer)

    results = await svc._fetch_downtown_areas(None, "fake-key", 40.0, -75.0, None, home_city="MEDIA", home_state="PA")

    assert [r.name for r in results] == ["Media"]


def test_downtown_ai_answer_is_cleaned():
    from app.services.ai.ai_downtown_service import _clean
    picks = _clean([{"name": "Downtown Phoenixville", "state": "pa", "kind": "downtown", "main_street": "Bridge Street"},
                    {"name": "X", "state": "PA", "kind": "downtown"},
                    {"name": "King of Prussia", "state": "PA", "kind": "mall"}])
    assert picks == [{"name": "Phoenixville", "state": "PA", "kind": "downtown", "main_street": "Bridge Street"},
                     {"name": "King of Prussia", "state": "PA", "kind": "downtown", "main_street": None}]
