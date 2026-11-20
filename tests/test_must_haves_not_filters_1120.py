"""Owner 11/20: "a township actually shouldn't be a must-have - it's a basic sorting feature now". Must-haves are
features; a township / town / school typed as one goes into the Showcase's search filters instead
(showcase_smart_filter.split_filter_wants), and a school only when exactly one of the district's schools fits."""
import uuid

import pytest

from app.services import home_school_service as hs

OPTS = [{"key": "nces:E", "name": "Downingtown High School East Campus", "homes": 40},
        {"key": "nces:W", "name": "Downingtown High School West Campus", "homes": 60},
        {"key": "nces:S", "name": "Downingtown STEM Academy", "homes": 2},
        {"key": "unknown", "name": "School unknown", "homes": 80}]


def test_school_words_pick_one_school_or_none():
    assert hs.match_school_name(OPTS, "Downingtown East schools")["key"] == "nces:E"
    assert hs.match_school_name(OPTS, "DASD West high school")["key"] == "nces:W"
    assert hs.match_school_name(OPTS, "Downingtown schools") is None      # East and West both fit - never a guess
    assert hs.match_school_name(OPTS, "Good schools") is None
    assert hs.match_school_name(OPTS, "North high school") is None        # a side the district doesn't have


@pytest.mark.asyncio
async def test_split_moves_places_and_schools_keeps_features(monkeypatch):
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import Property
    from app.services import showcase_smart_filter as sf

    async def fake_schools(db, districts):
        return OPTS
    monkeypatch.setattr(hs, "schools_in", fake_schools)
    async with AsyncSessionLocal() as db:
        twp = (await db.execute(select(Property.township).where(Property.township.isnot(None), Property.township != "")
                                .limit(1))).scalar()
        if not twp:
            pytest.skip("no townships locally")
        name = twp.upper().replace(" TWP", "").replace(" TOWNSHIP", "").title()
        keep, moves = await sf.split_filter_wants(db, ["Pool", f"In {name} Township", "Downingtown East schools", "Good schools",
                                                       "Walk to the elementary school"], ["Downingtown Area, PA"])
    assert keep == ["Pool", "Walk to the elementary school"]                 # features stay - "walk to school" is a feature
    to = {m["want"]: (m["to"], m["value"]) for m in moves}
    assert to[f"In {name} Township"] == ("townships", name)
    assert to["Downingtown East schools"] == ("high_schools", "nces:E")
    assert to["Good schools"] == ("school_note", None)
    line = sf.moves_line(moves)
    assert "is now a Townships filter" in line and "is now the High school filter" in line and "isn't a must-have" in line


@pytest.mark.asyncio
async def test_a_school_without_a_district_is_only_a_note():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services import showcase_smart_filter as sf
    async with AsyncSessionLocal() as db:
        keep, moves = await sf.split_filter_wants(db, ["Downingtown East schools"], [])
    assert keep == [] and moves[0]["to"] == "school_note"


@pytest.mark.asyncio
async def test_save_puts_the_township_in_the_search():
    """Saving "Pool" + a township as must-haves: the township lands in Townships, only Pool is read."""
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, User, collection_properties
    from app.services import showcase_smart_filter as sf
    tag = f"zqmh-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        twp = (await db.execute(select(Property.township).where(Property.township.isnot(None), Property.township != "",
                                                                 Property.home_status == "ACTIVE-BRIGHT").limit(1))).scalar()
        if not twp:
            pytest.skip("no townships locally")
        name = twp.upper().replace(" TWP", "").replace(" TOWNSHIP", "").title()
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        c = Collection(owner_id=agent.id, name="T", visitor_name="T", visitor_email=f"{tag}-c@example.com")
        db.add(c)
        await db.commit()
        pref = CollectionPreferences(collection_id=c.id, townships=[], cities=[], counties=[], school_districts=[], max_price=900000)
        db.add(pref)
        await db.commit()
        scheduled = []
        orig = sf.schedule
        sf.schedule = lambda cid: scheduled.append(cid)       # no AI reading in a test
        try:
            st = await sf.save_wants(db, c.id, ["Pool", f"{name} Township"], user=None)
            await db.refresh(pref)
            assert st["wants"] == ["Pool"] and pref.townships == [name]
            assert st["moved"][0]["to"] == "townships" and scheduled == [c.id]
        finally:
            sf.schedule = orig
            await db.execute(collection_properties.delete().where(collection_properties.c.collection_id == c.id))
            await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id == c.id))
            await db.execute(delete(Collection).where(Collection.id == c.id))
            await db.execute(delete(User).where(User.id == agent.id))
            await db.commit()


@pytest.mark.asyncio
async def test_stars_come_only_from_the_current_must_haves():
    """Owner 11/20: "starred means it's likely perfect, not just a match". New must-haves take off the stars the old
    ones gave (never the agent's own); a school / township saved as a must-have before 11/20 drops on the next read
    and its stars come off."""
    import app.main  # noqa: F401
    from datetime import datetime, timezone
    from sqlalchemy import delete, insert, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, PropertyInteraction, User, collection_properties
    from app.services import showcase_smart_filter as sf
    tag = f"zqst-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    orig = sf.schedule
    sf.schedule = lambda cid: None                         # no AI reading in a test
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(select(Property.id).where(Property.home_status == "ACTIVE-BRIGHT").limit(2))).scalars().all()
        if len(ids) < 2:
            pytest.skip("no listings locally")
        ai_home, own_home = ids
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        c = Collection(owner_id=agent.id, name="T", visitor_name="T", visitor_email=f"{tag}-c@example.com")
        db.add(c)
        await db.commit()
        db.add(CollectionPreferences(collection_id=c.id, townships=[], cities=[], counties=[], school_districts=[],
                                     ai_wants={"wants": ["Pool"], "state": "done", "ai_starred": {ai_home: now.isoformat()}}))
        await db.execute(insert(collection_properties), [{"collection_id": c.id, "property_id": i} for i in ids])
        for i in ids:
            db.add(PropertyInteraction(collection_id=c.id, property_id=i, liked=False, disliked=False, agent_recommended_at=now))
        await db.commit()
        cid, aid = c.id, agent.id

        async def stars():
            rows = (await db.execute(select(PropertyInteraction.property_id, PropertyInteraction.agent_recommended_at)
                                     .where(PropertyInteraction.collection_id == cid))).all()
            return {p for p, at in rows if at is not None}
        try:
            st = await sf.save_wants(db, cid, ["Fenced yard"], user=None)
            assert await stars() == {own_home}                 # the AI's Pool star came off; the agent's own stayed
            assert st["ai_starred"] == {} and st["wants"] == ["Fenced yard"]
            # an old school must-have (saved before 11/20) with an AI star
            await db.execute(PropertyInteraction.__table__.update().where(PropertyInteraction.collection_id == cid,
                                                                          PropertyInteraction.property_id == ai_home).values(agent_recommended_at=now))
            await db.execute(CollectionPreferences.__table__.update().where(CollectionPreferences.collection_id == cid).values(
                ai_wants={"wants": ["Downingtown East schools"], "state": "done", "ai_starred": {ai_home: now.isoformat()}}))
            await db.commit()
            out = await sf.run_for(cid)
            assert out["wants"] == [] and out["state"] == "off", out
            assert await stars() == {own_home}
        finally:
            sf.schedule = orig
            await db.rollback()
            await db.execute(delete(PropertyInteraction).where(PropertyInteraction.collection_id == cid))
            await db.execute(collection_properties.delete().where(collection_properties.c.collection_id == cid))
            await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id == cid))
            await db.execute(delete(Collection).where(Collection.id == cid))
            await db.execute(delete(User).where(User.id == aid))
            await db.commit()
