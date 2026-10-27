"""Finished towns shipped with the code (owner, 10/26) - app/services/areas/town_seeds.py."""
from types import SimpleNamespace

import pytest

from app.models.database import MarketArea
from app.services.areas import town_seeds

SEED = {"seed_id": "2026-10-26", "town": {
    "name": "Wayne", "state": "PA", "slug": "wayne-pa", "canonical_key": "PA|city|wayne", "county": "Delaware",
    "municipality_type": "CDP", "area_type": "city", "parent_area_name": None, "zip_codes": ["19087"],
    "centroid_lat": 40.04, "centroid_lng": -75.39, "geography": {}, "narrative": {"summary": "Wayne is..."},
    "facts": {"restaurants": {"entities": [{"name": "Matador"}]}, "_places_check_version": 1},
    "enrichment_status": "ENRICHED", "research_version": 3, "last_enriched_at": "2026-10-26T15:00:00+00:00", "last_verified_at": None,
}}


class Db:
    def __init__(self, existing=None):
        self.existing, self.added, self.commits = existing, [], 0

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: self.existing))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_a_town_not_here_is_created_finished():
    db = Db()
    assert (await town_seeds.apply_town_seeds(db, [SEED]))["created"] == 1
    area = db.added[0]
    assert area.slug == "wayne-pa" and area.facts["restaurants"]["entities"][0]["name"] == "Matador"
    assert area.facts["_seeded_from"] == "2026-10-26" and area.last_enriched_at.year == 2026


@pytest.mark.asyncio
async def test_an_unresearched_town_here_gets_the_finished_version_but_keeps_its_address():
    live = MarketArea(name="Wayne", state="PA", slug="wayne-pa-2", canonical_key="PA|city|wayne", facts={"walk": {"x": 1}},
                      enrichment_status="PARTIAL")
    db = Db(live)
    assert (await town_seeds.apply_town_seeds(db, [SEED]))["filled"] == 1
    assert live.slug == "wayne-pa-2" and live.enrichment_status == "ENRICHED" and live.narrative["summary"] == "Wayne is..."


@pytest.mark.asyncio
@pytest.mark.parametrize("facts", [
    {"_places_check_version": 1},                                   # already has the new research
    {"parks": {"entities": [{"name": "X", "admin_edited": True}]}},   # an admin changed it
    {"_seeded_from": "2026-10-26"},                                  # this seed is already in
])
async def test_never_overwrites(facts):
    live = MarketArea(name="Wayne", state="PA", slug="wayne-pa", canonical_key="PA|city|wayne", facts=facts, enrichment_status="ENRICHED")
    db = Db(live)
    assert (await town_seeds.apply_town_seeds(db, [SEED]))["kept"] == 1 and db.commits == 0
    assert live.facts == facts


@pytest.mark.asyncio
async def test_a_newer_seed_replaces_an_older_seed_unless_an_admin_edited_it():
    newer = {**SEED, "seed_id": "2026-10-27"}
    live = MarketArea(name="Wayne", state="PA", slug="wayne-pa-2", canonical_key="PA|city|wayne", enrichment_status="ENRICHED",
                      facts={"_seeded_from": "2026-10-26", "_places_check_version": 1, "restaurants": {"entities": [{"name": "Old"}]}})
    db = Db(live)
    assert (await town_seeds.apply_town_seeds(db, [newer]))["updated"] == 1
    assert live.slug == "wayne-pa-2" and live.facts["_seeded_from"] == "2026-10-27"
    assert live.facts["restaurants"]["entities"][0]["name"] == "Matador"
    edited = MarketArea(name="Wayne", state="PA", slug="wayne-pa", canonical_key="PA|city|wayne", enrichment_status="ENRICHED",
                        facts={"_seeded_from": "2026-10-26", "_places_check_version": 1,
                               "parks": {"entities": [{"name": "X", "admin_edited": True}]}})
    db = Db(edited)
    assert (await town_seeds.apply_town_seeds(db, [newer]))["kept"] == 1 and db.commits == 0


@pytest.mark.asyncio
async def test_a_seed_can_replace_a_town_researched_here_only_when_marked_and_unedited():
    marked = {**SEED, "seed_id": "2026-10-27", "replaces_live_research": True}
    live = MarketArea(name="Wayne", state="PA", slug="wayne-pa", canonical_key="PA|city|wayne", enrichment_status="ENRICHED",
                      facts={"_places_check_version": 1, "restaurants": {"entities": [{"name": "Old"}]}})
    db = Db(live)
    assert (await town_seeds.apply_town_seeds(db, [marked]))["updated"] == 1
    assert live.facts["restaurants"]["entities"][0]["name"] == "Matador"
    unmarked = {**SEED, "seed_id": "2026-10-27"}
    live = MarketArea(name="Wayne", state="PA", slug="wayne-pa", canonical_key="PA|city|wayne", enrichment_status="ENRICHED",
                      facts={"_places_check_version": 1})
    assert (await town_seeds.apply_town_seeds(Db(live), [unmarked]))["kept"] == 1
    edited = MarketArea(name="Wayne", state="PA", slug="wayne-pa", canonical_key="PA|city|wayne", enrichment_status="ENRICHED",
                        facts={"_places_check_version": 1, "_admin_edited_at": "2026-10-27"})
    assert (await town_seeds.apply_town_seeds(Db(edited), [marked]))["kept"] == 1
