"""Must-haves cost less for the same stars (owner, 11/19: "as cheap but as efficient as possible"): a star needs every
must-have, so the next one is read only for homes that passed the ones before it; town must-haves are free and go
first; each home's answer per must-have is kept, so adding one more only reads the new one."""
import json
import uuid

import pytest


@pytest.mark.asyncio
async def test_reads_only_what_can_still_be_starred(monkeypatch):
    import app.main  # noqa: F401
    from sqlalchemy import delete, select, text
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, PropertyInteraction, User, collection_properties
    from app.services import showcase_smart_filter as sf
    from app.services.ai import ai_home_reader as reader
    from app.services.ai import ai_rate_limit_service as rl

    asked = []

    async def fake_read(config, question, homes):
        asked.append((question, sorted(p.id for p in homes)))
        q = question.lower()
        yes = {p.id for i, p in enumerate(sorted(homes, key=lambda h: h.id)) if ("pool" in q and i % 2 == 0) or ("garage" in q)}
        return {"verdicts": {pid: {"v": "yes", "quote": "", "why": question} for pid in yes},
                "read": [p.id for p in homes], "failed": [], "usd": 0.001 * len(homes), "in": 10, "out": 10}

    async def no_limit(db, user, config):
        return None, 0.0

    async def reserve(*a, **k):
        return {"id": "x"}

    async def nothing(*a, **k):
        return None

    async def no_phrases(db, cid, user, st):
        return {}

    monkeypatch.setattr(reader, "read", fake_read)
    monkeypatch.setattr(reader, "room", no_limit)
    monkeypatch.setattr(rl, "reserve_global_and_emergency", reserve)
    monkeypatch.setattr(rl, "reconcile_budget", nothing)
    monkeypatch.setattr(rl, "record_usage_event", nothing)
    monkeypatch.setattr(sf, "fill_phrases", no_phrases)

    tag = f"zqmh-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        homes = (await db.execute(select(Property).where(Property.home_status == "ACTIVE-BRIGHT", Property.township.isnot(None))
                                  .order_by(Property.id).limit(8))).scalars().all()
        if len(homes) < 8:
            pytest.skip("not enough listings")
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        c = Collection(name=tag, owner_id=agent.id, status="ACTIVE", share_token=tag)
        db.add(c)
        await db.commit()
        db.add(CollectionPreferences(collection_id=c.id, ai_wants={"wants": ["Pool"], "state": "off"}))
        for p in homes:
            await db.execute(collection_properties.insert().values(collection_id=c.id, property_id=p.id))
        await db.commit()
        cid = c.id
    try:
        async def set_wants(w):
            async with AsyncSessionLocal() as db:
                await db.execute(text("update collection_preferences set ai_wants = cast(:v as jsonb) where collection_id = :c"),
                                 {"v": json.dumps({"wants": w, "state": "off"}), "c": cid})
                await db.commit()

        await set_wants(["Pool", "3-car garage"])
        st = await sf.run_for(cid)
        assert [q for q, _ in asked] == ["Pool", "3-car garage"]
        assert len(asked[0][1]) == 8 and len(asked[1][1]) == 4      # the garage read only the 4 pool homes
        assert st["matched"] == 4
        asked.clear()
        # one more must-have: only the new one is read, only for the 4 homes that still qualify
        await set_wants(["Pool", "3-car garage", "Big yard"])
        await sf.run_for(cid)
        assert [q for q, _ in asked] == ["Big yard"] and len(asked[0][1]) == 4
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(PropertyInteraction).where(PropertyInteraction.collection_id == cid))
            await db.execute(collection_properties.delete().where(collection_properties.c.collection_id == cid))
            await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id == cid))
            await db.execute(delete(Collection).where(Collection.id == cid))
            await db.execute(delete(User).where(User.email == f"{tag}@example.com"))
            await db.commit()
