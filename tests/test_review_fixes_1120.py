"""Fixes from the pre-push review (11/20): old township / school must-haves move into the search instead of being
dropped; a 4th item that's a filter is never cut by the 3-item cap; new districts drop the old high-school pick on the
agent's save too; the chat works a district's schools out on the spot; only explicit places / schools become filters."""
import uuid

import pytest

from app.services import home_school_service as hs

OPTS = [{"key": "nces:E", "name": "Downingtown High School East Campus", "homes": 40},
        {"key": "nces:W", "name": "Downingtown High School West Campus", "homes": 60}]


async def _agent_and_showcase(db, tag, **prefs):
    from app.models.database import Collection, CollectionPreferences, User
    agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                 mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
    db.add(agent)
    await db.commit()
    c = Collection(owner_id=agent.id, name="T", visitor_name="T", visitor_email=f"{tag}-c@example.com", share_token=tag, status="ACTIVE")
    db.add(c)
    await db.commit()
    base = {"townships": [], "cities": [], "counties": [], "school_districts": [], "max_price": 900000}
    base.update(prefs)
    db.add(CollectionPreferences(collection_id=c.id, **base))
    await db.commit()
    return c.id, agent.id


async def _cleanup(db, cid, aid):
    from sqlalchemy import delete
    from app.models.database import Collection, CollectionPreferences, PropertyInteraction, User, collection_properties
    await db.rollback()
    await db.execute(delete(PropertyInteraction).where(PropertyInteraction.collection_id == cid))
    await db.execute(collection_properties.delete().where(collection_properties.c.collection_id == cid))
    await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id == cid))
    await db.execute(delete(Collection).where(Collection.id == cid))
    await db.execute(delete(User).where(User.id == aid))
    await db.commit()


async def _a_township(db):
    from sqlalchemy import select
    from app.models.database import Property
    t = (await db.execute(select(Property.township).where(Property.township.isnot(None), Property.township != "",
                                                          Property.home_status == "ACTIVE-BRIGHT").limit(1))).scalar()
    return t.upper().replace(" TWP", "").replace(" TOWNSHIP", "").title() if t else None


@pytest.mark.asyncio
async def test_old_school_and_township_must_haves_move_into_the_search(monkeypatch):
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import CollectionPreferences
    from app.services import showcase_smart_filter as sf

    async def fake_schools(db, districts):
        return OPTS
    monkeypatch.setattr(hs, "schools_in", fake_schools)
    async with AsyncSessionLocal() as db:
        twp = await _a_township(db)
        if not twp:
            pytest.skip("no townships locally")
        cid, aid = await _agent_and_showcase(db, f"zqrf-{uuid.uuid4().hex[:8]}", school_districts=["Downingtown Area, PA"],
                                             ai_wants={"wants": ["Downingtown East schools", f"In {twp} Township"], "state": "done"})
        try:
            out = await sf.run_for(cid)
            assert out["wants"] == [] and out["state"] == "off"
            assert {m["to"] for m in out["moved"]} == {"high_schools", "townships"}
            db.expire_all()
            pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == cid))).scalar_one()
            assert pref.high_schools == ["nces:E"] and pref.townships == [twp]
        finally:
            await _cleanup(db, cid, aid)


@pytest.mark.asyncio
async def test_a_fourth_item_that_is_a_filter_is_never_cut():
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import CollectionPreferences
    from app.services import showcase_smart_filter as sf
    async with AsyncSessionLocal() as db:
        twp = await _a_township(db)
        if not twp:
            pytest.skip("no townships locally")
        cid, aid = await _agent_and_showcase(db, f"zqrf-{uuid.uuid4().hex[:8]}")
        orig = sf.schedule
        sf.schedule = lambda c: None
        try:
            st = await sf.save_wants(db, cid, ["Pool", "Fenced yard", "Garage", f"{twp} Township"], user=None)
            assert st["wants"] == ["Pool", "Fenced yard", "Garage"]
            db.expire_all()
            pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == cid))).scalar_one()
            assert pref.townships == [twp]
        finally:
            sf.schedule = orig
            await _cleanup(db, cid, aid)


@pytest.mark.asyncio
async def test_new_districts_drop_the_old_school_pick_on_the_agents_save():
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import CollectionPreferences, User
    from app.api.collections_routes import update_preferences_and_refresh
    from app.schemas.collection_preferences import CollectionPreferencesUpdate
    async with AsyncSessionLocal() as db:
        cid, aid = await _agent_and_showcase(db, f"zqrf-{uuid.uuid4().hex[:8]}", school_districts=["Downingtown Area, PA"],
                                             high_schools=["nces:E"])
        try:
            # the route gets the signed-in user from its own lookup - a loaded, detached copy here
            user = await db.get(User, aid)
            db.expunge(user)
            await update_preferences_and_refresh(cid, CollectionPreferencesUpdate(school_districts=["West Chester Area, PA"]), db=db, current_user=user)
            db.expire_all()
            pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == cid))).scalar_one()
            assert pref.school_districts == ["West Chester Area, PA"] and (pref.high_schools or []) == []
            # the same districts (only the price changed) keep the pick
            await db.execute(CollectionPreferences.__table__.update().where(CollectionPreferences.collection_id == cid)
                             .values(high_schools=["nces:W"]))
            await db.commit()
            await update_preferences_and_refresh(cid, CollectionPreferencesUpdate(max_price=800000, school_districts=["West Chester Area, PA"]),
                                                 db=db, current_user=user)
            db.expire_all()
            pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == cid))).scalar_one()
            assert pref.high_schools == ["nces:W"]
        finally:
            await _cleanup(db, cid, aid)


@pytest.mark.asyncio
async def test_chat_works_a_new_districts_schools_out_on_the_spot(monkeypatch):
    from app.services.ai import ai_actions_catalog as cat
    calls = []

    async def only_unknown(db, districts):
        return [{"key": "unknown", "name": "School unknown", "homes": 12}] if not calls else OPTS
    async def fill(db, st, d):
        calls.append((st, d))

    class _Res:
        def all(self):
            return [("PA", "DOWNINGTOWN AREA")]

    class _DB:
        async def execute(self, *a, **k):
            return _Res()
    monkeypatch.setattr(hs, "schools_in", only_unknown)
    monkeypatch.setattr(hs, "fill_unit", fill)
    got = await cat._district_schools(_DB(), ["Downingtown Area, PA"])
    assert calls and {o["key"] for o in got} == {"nces:E", "nces:W"}


@pytest.mark.asyncio
async def test_only_explicit_places_and_schools_become_filters():
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import Property
    from app.services import showcase_smart_filter as sf
    async with AsyncSessionLocal() as db:
        city = (await db.execute(select(Property.city).where(Property.city.isnot(None), Property.city != "").limit(1))).scalar()
        if not city:
            pytest.skip("no cities locally")
        bare = city.title()
        keep, moves = await sf.split_filter_wants(db, [bare, f"In {bare}", "Historic district", "Ocean view"], [])
    assert bare in keep and "Historic district" in keep and "Ocean view" in keep
    assert [m["want"] for m in moves] == [f"In {bare}"]
