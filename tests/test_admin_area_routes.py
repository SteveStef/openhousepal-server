"""Admin "Neighborhoods" tab (owner, 10/26) - app/api/admin_area_routes.py, and the admin_edited
protection in app/services/areas/town_places_check.py. Nothing here calls Google or the AI."""
import copy
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import admin_area_routes as ar
from app.models.database import MarketArea
from app.services.areas import town_places_check as tpc

NOW = "2026-10-26T12:00:00+00:00"
ADMIN = SimpleNamespace(id="admin-1")


def place(name, **kw):
    return {"name": name, "category": kw.pop("category", "Restaurant"), "city": kw.pop("city", "Wayne"), **kw}


def town_facts():
    return {
        "restaurants": {
            "notes": "Lancaster Avenue is lined with places to eat.",
            "entities": [
                place("Burtons Grill", distance_miles=0.2, google_checked=True, lat=40.04, lng=-75.39, address="312 W Lancaster Ave, Wayne, PA"),
                place("Teresa's Cafe", distance_miles=0.4, google_checked=True, lat=40.041, lng=-75.388, description="Italian on the main street."),
                place("Ella's American Bistro", distance_miles=0.3, google_checked=True, lat=40.042, lng=-75.387),
            ],
            "_removed": [place("Winberie's", removed_reason="closed (Google)", removed_at="2026-10-01T00:00:00+00:00")],
        },
        "private_schools": {"entities": [place("St. Katharine of Siena School", category="Grades K-8")]},
        "public_schools": {
            "districts": [{"name": "Radnor Township School District", "share": 60}],
            "elementary_schools": [{"name": "Wayne Elementary School", "district": "Radnor Township School District", "source_url": "https://x"}],
            "middle_schools": [{"name": "Radnor Middle School", "district": "Radnor Township School District"}],
            "high_schools": [],
        },
        "public_schools_research": {"districts": [{"name": "Radnor Township School District"}],
                                    "elementary_schools": [{"name": "Wayne Elementary School", "district": "Radnor Township School District"}]},
    }


def body_from(facts, cat="restaurants", **kw):
    block = facts[cat]
    ents = kw.pop("entities", None)
    if ents is None:
        ents = [ar.PlaceIn(ref=i, **{f: e.get(f) for f in ar.EDIT_FIELDS}) for i, e in enumerate(block["entities"])]
    return ar.PlacesSave(entities=ents, base_names=[e["name"] for e in block["entities"]],
                         base_removed_names=[r["name"] for r in block.get("_removed") or []], **kw)


def as_item(e, ref=None, removed_ref=None, **changes):
    return ar.PlaceIn(ref=ref, removed_ref=removed_ref, **{**{f: e.get(f) for f in ar.EDIT_FIELDS}, **changes})


# ---------------------------------------------------------------- edit / add / remove / put back / reorder

def test_saving_unchanged_list_changes_nothing():
    facts = town_facts()
    out = ar.apply_place_edits(facts, "restaurants", body_from(facts), NOW)
    assert out["restaurants"]["entities"] == facts["restaurants"]["entities"]
    assert "_admin_order" not in out["restaurants"]
    assert out["_admin_edited_at"] == NOW


def test_edit_marks_only_the_changed_place_and_keeps_its_other_fields():
    facts = town_facts()
    ents = facts["restaurants"]["entities"]
    body = body_from(facts, entities=[as_item(ents[0], 0), as_item(ents[1], 1, name="Teresa's Next Door", description="Beer bar."), as_item(ents[2], 2)])
    out = ar.apply_place_edits(facts, "restaurants", body, NOW)["restaurants"]["entities"]
    assert out[1]["name"] == "Teresa's Next Door" and out[1]["description"] == "Beer bar."
    assert out[1]["admin_edited"] is True and out[1]["admin_edited_at"] == NOW
    assert out[1]["distance_miles"] == 0.4 and out[1]["google_checked"]  # same address - the checked distance stays
    assert not out[0].get("admin_edited") and not out[2].get("admin_edited")
    assert facts["restaurants"]["entities"][1]["name"] == "Teresa's Cafe"  # the input is never changed in place


def test_new_address_drops_the_old_map_spot_and_distance():
    facts = town_facts()
    ents = facts["restaurants"]["entities"]
    body = body_from(facts, entities=[as_item(ents[0], 0, address="1 New Rd, Wayne, PA"), as_item(ents[1], 1), as_item(ents[2], 2)])
    e = ar.apply_place_edits(facts, "restaurants", body, NOW)["restaurants"]["entities"][0]
    assert e["address"] == "1 New Rd, Wayne, PA" and "distance_miles" not in e and "lat" not in e and "google_checked" not in e


def test_add_a_new_place():
    facts = town_facts()
    ents = facts["restaurants"]["entities"]
    new = ar.PlaceIn(name="  Pizza   Town ", category="Pizza", city="Wayne")
    body = body_from(facts, entities=[as_item(e, i) for i, e in enumerate(ents)] + [new])
    out = ar.apply_place_edits(facts, "restaurants", body, NOW)["restaurants"]["entities"]
    assert len(out) == 4 and out[3]["name"] == "Pizza Town"
    assert out[3]["admin_edited"] and out[3]["added_by_admin"] and out[3]["source_type"] == "admin"


def test_remove_then_put_back():
    facts = town_facts()
    ents = facts["restaurants"]["entities"]
    # Take Ella's off.
    out = ar.apply_place_edits(facts, "restaurants", body_from(facts, entities=[as_item(ents[0], 0), as_item(ents[1], 1)]), NOW)
    block = out["restaurants"]
    assert [e["name"] for e in block["entities"]] == ["Burtons Grill", "Teresa's Cafe"]
    gone = block["_removed"][-1]
    assert gone["name"] == "Ella's American Bistro" and gone["admin_removed"] and gone["removed_reason"] == "Taken off by the admin"
    # Put back the one Google took off (closed) - it comes back marked, without its removal reason.
    winberies = block["_removed"][0]
    body = body_from(out, entities=[as_item(e, i) for i, e in enumerate(block["entities"])] + [as_item(winberies, removed_ref=0)])
    back = ar.apply_place_edits(out, "restaurants", body, NOW)["restaurants"]
    w = back["entities"][-1]
    assert w["name"] == "Winberie's" and w["admin_edited"] and w["put_back_by_admin"]
    assert "removed_reason" not in w and "removed_at" not in w
    assert [r["name"] for r in back["_removed"]] == ["Ella's American Bistro"]


def test_reorder_is_remembered():
    facts = town_facts()
    ents = facts["restaurants"]["entities"]
    body = body_from(facts, entities=[as_item(ents[2], 2), as_item(ents[0], 0), as_item(ents[1], 1)])
    block = ar.apply_place_edits(facts, "restaurants", body, NOW)["restaurants"]
    assert [e["name"] for e in block["entities"]] == ["Ella's American Bistro", "Burtons Grill", "Teresa's Cafe"]
    assert block["_admin_order"] == ["Ella's American Bistro", "Burtons Grill", "Teresa's Cafe"]
    assert not any(e.get("admin_edited") for e in block["entities"])  # moving a place isn't editing it


def test_changed_since_loaded_is_refused():
    facts = town_facts()
    body = body_from(facts)
    body.base_names = ["Something else", *body.base_names[1:]]
    with pytest.raises(HTTPException) as e:
        ar.apply_place_edits(facts, "restaurants", body, NOW)
    assert e.value.status_code == 409


@pytest.mark.parametrize("change, code", [({"name": "  "}, 422), ({"source_url": "javascript:alert(1)"}, 422), ({"name": "x" * 300}, 422)])
def test_bad_input_is_refused(change, code):
    facts = town_facts()
    ents = facts["restaurants"]["entities"]
    body = body_from(facts, entities=[as_item(ents[0], 0, **change), as_item(ents[1], 1), as_item(ents[2], 2)])
    with pytest.raises(HTTPException) as e:
        ar.apply_place_edits(facts, "restaurants", body, NOW)
    assert e.value.status_code == code


def test_private_school_name_and_grades():
    facts = town_facts()
    e = facts["private_schools"]["entities"][0]
    body = body_from(facts, "private_schools", entities=[as_item(e, 0, category="Grades PK-8")])
    out = ar.apply_place_edits(facts, "private_schools", body, NOW)["private_schools"]["entities"][0]
    assert out["category"] == "Grades PK-8" and out["admin_edited"]


def test_public_schools_by_level_write_both_copies():
    facts = town_facts()
    body = ar.SchoolsSave(
        levels={"elementary_schools": [ar.SchoolIn(ref=0, name="Wayne Elementary School", district="Radnor Township School District"),
                                       ar.SchoolIn(name="Radnor Elementary School", district="Radnor Township School District")],
                "high_schools": [ar.SchoolIn(name="Radnor High School", district="Radnor Township School District")]},
        base={"elementary_schools": ["Wayne Elementary School"], "high_schools": []})
    out = ar.apply_school_edits(facts, body, NOW)
    elem = out["public_schools"]["elementary_schools"]
    assert [s["name"] for s in elem] == ["Wayne Elementary School", "Radnor Elementary School"]
    assert elem[0]["source_url"] == "https://x" and not elem[0].get("admin_edited")  # unchanged one keeps everything
    assert elem[1]["admin_edited"]
    assert out["public_schools"]["high_schools"][0]["name"] == "Radnor High School"
    assert out["public_schools"]["middle_schools"] == facts["public_schools"]["middle_schools"]  # not sent = untouched
    # The page rebuilds the list from the research copy - it has the admin's list too.
    assert [s["name"] for s in out["public_schools_research"]["elementary_schools"]] == ["Wayne Elementary School", "Radnor Elementary School"]


# ---------------------------------------------------------------- Fair Housing pass on save

class FakeDb:
    def __init__(self, area):
        self.area, self.commits = area, 0

    async def get(self, _model, _id):
        return self.area

    def add(self, _obj):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass


def make_area(facts, narrative=None):
    return MarketArea(id="area-1", name="Wayne", state="PA", slug="wayne-pa", canonical_key="pa|city|wayne",
                      facts=copy.deepcopy(facts), narrative=narrative or {"summary": "Old write-up."}, centroid_lat=40.04, centroid_lng=-75.39)


@pytest.fixture
def no_detail(monkeypatch):
    async def detail(_db, area):
        return {"facts": area.facts, "narrative": area.narrative}

    async def forget(_db, _area):
        pass
    monkeypatch.setattr(ar, "area_detail", detail)
    monkeypatch.setattr(ar, "forget_cached_town", forget)


@pytest.mark.asyncio
async def test_saved_description_goes_through_fair_housing_rules(no_detail):
    facts = town_facts()
    area = make_area(facts)
    ents = facts["restaurants"]["entities"]
    body = body_from(facts, entities=[as_item(ents[0], 0, description="A family-friendly spot, perfect for young families."), as_item(ents[1], 1), as_item(ents[2], 2)])
    out = await ar.save_places("area-1", "restaurants", body, db=FakeDb(area), admin=ADMIN)
    desc = out["facts"]["restaurants"]["entities"][0]["description"]
    assert "family-friendly" not in desc.lower() and "young families" not in desc.lower()
    assert out["wording_changes"] and out["wording_changes"][0]["before"].startswith("A family-friendly")
    assert area.facts["_fair_housing"]["originals"]  # the original wording is kept for review


@pytest.mark.asyncio
async def test_saved_summary_goes_through_fair_housing_rules(no_detail):
    area = make_area(town_facts())
    out = await ar.save_summary("area-1", ar.SummarySave(text="Wayne is a safe neighborhood with top-rated schools."), db=FakeDb(area), admin=ADMIN)
    s = out["narrative"]["summary"]
    assert "safe" not in s.lower() and "top-rated" not in s.lower()
    assert out["narrative"]["admin_edited"] and area.facts["_admin_edited_at"]
    assert out["wording_changes"][0]["where"] == "Write-up"


# ---------------------------------------------------------------- town_places_check leaves admin changes alone

@pytest.mark.asyncio
async def test_place_check_keeps_admin_places_untouched(monkeypatch):
    facts = {
        "restaurants": {
            "entities": [
                place("Ghost Cafe"),                                                   # not on Google -> taken off
                place("Joe's Diner", admin_edited=True, admin_edited_at=NOW, description="Breakfast all day."),  # not on Google, but the admin's
                place("Real Grill"),
            ],
            "_admin_order": ["Joe's Diner", "Real Grill"],
        },
        "medical": {"entities": [place("Main Line Urgent Care", category="URGENT_CARE")],
                    "_removed": [place("Paoli Hospital", category="HOSPITAL", admin_removed=True, removed_reason="Taken off by the admin")]},
        "private_schools": {"entities": [place("Little Acorns Preschool", category="Grades PK-K", admin_edited=True)]},
    }
    area = make_area(facts)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    looked_up = []

    async def lookup(_client, _key, _area, cat, e):
        looked_up.append(e["name"])
        if e["name"] in ("Real Grill", "Main Line Urgent Care"):
            return {"displayName": {"text": e["name"]}, "primaryType": "restaurant" if cat == "restaurants" else "medical_clinic",
                    "types": ["restaurant"] if cat == "restaurants" else ["medical_clinic"], "businessStatus": "OPERATIONAL",
                    "formattedAddress": "5 Main St, Wayne, PA 19087, USA", "location": {"latitude": 40.05, "longitude": -75.4}}
        return None

    async def hospitals(*_a):  # Google's nearest hospital is the one the admin took off
        return [{"name": "Paoli Hospital", "category": "HOSPITAL", "lat": 40.04, "lng": -75.48, "google_checked": True}]

    async def drive(_c, _k, _a, ents):
        for e in ents:
            e["distance_miles"] = 2.0

    monkeypatch.setattr(tpc, "_lookup", lookup)
    monkeypatch.setattr(tpc, "_hospitals", hospitals)
    monkeypatch.setattr(tpc, "_drive_miles", drive)
    monkeypatch.setattr(tpc, "_survey_schools_near", lambda *a: [])
    monkeypatch.setattr(tpc.nas, "_is_legitimate_urgent_care", lambda p: True)

    await tpc.check_town_places(FakeDb(area), area)
    out = area.facts
    rest = out["restaurants"]["entities"]
    assert [e["name"] for e in rest] == ["Joe's Diner", "Real Grill"]  # the admin's order, Ghost Cafe gone
    joe = rest[0]
    assert joe == facts["restaurants"]["entities"][1]  # every field exactly as the admin left it (no distance added)
    assert "Joe's Diner" not in looked_up and "Little Acorns Preschool" not in looked_up  # never sent to Google
    assert [r["name"] for r in out["restaurants"]["_removed"]] == ["Ghost Cafe"]
    assert not any(e["name"] == "Paoli Hospital" for e in out["medical"]["entities"])  # never added back
    assert [e["name"] for e in out["private_schools"]["entities"]] == ["Little Acorns Preschool"]  # a preschool, but the admin's


# ---------------------------------------------------------------- actions (mocked - no Google, no AI)

@pytest.mark.asyncio
async def test_research_again_needs_the_cost_confirmed(monkeypatch, no_detail):
    area = make_area(town_facts())
    called = []

    async def fake_re_enrich(area_id, background_tasks, db, admin):
        called.append(area_id)
    import app.api.admin_routes as admin_routes
    monkeypatch.setattr(admin_routes, "re_enrich_area", fake_re_enrich)
    with pytest.raises(HTTPException) as e:
        await ar.research_again("area-1", background_tasks=None, confirm_cost=False, db=FakeDb(area), admin=ADMIN)
    assert e.value.status_code == 400 and "0.66" in e.value.detail and not called
    await ar.research_again("area-1", background_tasks=None, confirm_cost=True, db=FakeDb(area), admin=ADMIN)
    assert called == ["area-1"]


@pytest.mark.asyncio
async def test_check_places_runs_the_check_for_that_town(monkeypatch, no_detail):
    area = make_area(town_facts())
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    ran = []

    async def fake_check(_db, a):
        ran.append(a.slug)
        return {"checked": 3, "removed": 1, "removed_names": {"restaurants": ["Ghost Cafe"]}}
    monkeypatch.setattr(tpc, "check_town_places", fake_check)
    out = await ar.check_places("area-1", db=FakeDb(area), admin=ADMIN)
    assert ran == ["wayne-pa"] and out["check_result"]["removed"] == 1 and "area-1" not in ar._busy


# ---------------------------------------------------------------- search (real database, rolled back)

@pytest.mark.asyncio
async def test_search_filters():
    from app.database import AsyncSessionLocal
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as db:
        try:
            db.add_all([
                MarketArea(name=f"Zqx{tag} Borough", state="ZZ", slug=f"zqx{tag}-zz", canonical_key=f"zz|city|zqx{tag}", area_type="city",
                           enrichment_status="ENRICHED", facts={"_places_check_version": 1, "parks": {"entities": [{"name": "A"}]}}),
                MarketArea(name=f"Zqx{tag} Square", state="ZZ", slug=f"zqx{tag}-square-zz", canonical_key=f"zz|neighborhood|zqx{tag}sq",
                           area_type="neighborhood", parent_area_name="Zqxville", enrichment_status="PENDING", facts={}),
            ])
            await db.flush()
            everything = await ar.search_areas(db, q=f"zqx{tag}")
            assert everything["total"] == 2 and "ZZ" in everything["states"]
            boro = next(i for i in everything["items"] if i["area_type"] == "city")
            assert boro["places_checked"] and boro["place_count"] == 1 and boro["agent_count"] == 0
            assert (await ar.search_areas(db, q=f"zqx{tag}", area_type="neighborhood"))["items"][0]["parent_area_name"] == "Zqxville"
            assert (await ar.search_areas(db, q=f"zqx{tag}", research="PENDING"))["total"] == 1
            assert (await ar.search_areas(db, q=f"zqx{tag}", used="yes"))["total"] == 0
            assert (await ar.search_areas(db, q=f"zqx{tag}", used="no"))["total"] == 2
            assert (await ar.search_areas(db, q=f"Zqx{tag} squ"))["total"] == 1  # every word must match
            assert (await ar.search_areas(db, state="ZZ", page_size=1, page=2))["items"]
        finally:
            await db.rollback()
