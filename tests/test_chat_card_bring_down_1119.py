"""Chat fixes from the owner's Lisa chat (11/19).

1. A reply that sends them to a button on an earlier card that's still waiting ("I'd suggest tapping Confirm on the
   Uwchlan and Downingtown change first") brings that card down under the reply; the old spot says it moved, so
   it can only run once.
2. set_showcase_must_haves: the chat can star homes by must-haves (a school, words in the listing, a feature)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api import ai_routes


def test_points_at_card_wording():
    yes = ["I'd suggest tapping Confirm on the Uwchlan and Downingtown change first.",
           "Tap Confirm on the card above and it's done.", "Once you confirm the change, I'll run it again.",
           "Click Save on the card I made earlier.",
           "Also, the card for the Uwchlan and DASD East change is still waiting for you to tap its button. If you want that change, tap it first."]
    no = ["Do you want me to run the check on Lisa's Showcase?", "Her Showcase was opened 5 minutes ago.",
          "I can confirm she has no likes yet."]
    assert all(ai_routes._POINTS_AT_CARD.search(t) for t in yes), [t for t in yes if not ai_routes._POINTS_AT_CARD.search(t)]
    assert not any(ai_routes._POINTS_AT_CARD.search(t) for t in no), [t for t in no if ai_routes._POINTS_AT_CARD.search(t)]


@pytest.mark.asyncio
async def test_waiting_card_moves_down_once():
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import AIConversation, AIMessage, User
    tag = f"zqmove-{uuid.uuid4().hex[:8]}"
    later = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
    card = {"tool": "update_showcase_search", "record_type": "action", "record_id": "card-1", "title": "Change Lisa's Showcase search",
            "details": ["Townships: Uwchlan"], "payload": {"collection_id": "c1", "body": {"townships": ["Uwchlan"]}}, "status": "pending",
            "expires_at": later}
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            conv = AIConversation(owner_id=agent.id, title="Lisa")
            db.add(conv)
            await db.commit()
            old = AIMessage(conversation_id=conv.id, role="assistant", content="Tap Confirm on the card.", tool_calls=[card])
            db.add(old)
            await db.commit()

            # a reply that doesn't point at a button: nothing moves
            refs = await ai_routes._bring_down_waiting_card(db, conv.id, "Do you want me to run the check?", [])
            assert refs == []
            # a reply that does: the same card comes down, the old one says it moved
            refs = await ai_routes._bring_down_waiting_card(db, conv.id, "I'd suggest tapping Confirm on the Uwchlan change first.", [])
            assert len(refs) == 1 and refs[0]["record_id"] == "card-1" and refs[0]["status"] == "pending" and refs[0]["moved_from"] == old.id
            await db.commit()
            new = AIMessage(conversation_id=conv.id, role="assistant", content="...", tool_calls=refs)
            db.add(new)
            await db.commit()
            await db.refresh(old)
            assert old.tool_calls[0]["status"] == "replaced" and "Moved down" in old.tool_calls[0]["result_message"]
            # the old spot can't run it any more; only the new one is waiting
            from app.services.ai import ai_actions
            st = await ai_actions.confirm(db, agent, old.id, "card-1")
            assert st["status"] == "replaced"
            # a reply that made its own card leaves everything alone
            again = await ai_routes._bring_down_waiting_card(db, conv.id, "Tap Confirm on the card below.", [{"record_type": "action", "record_id": "x"}])
            assert [r["record_id"] for r in again] == ["x"]
    finally:
        async with AsyncSessionLocal() as db:
            uids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if uids:
                convs = (await db.execute(select(AIConversation.id).where(AIConversation.owner_id.in_(uids)))).scalars().all()
                if convs:
                    await db.execute(delete(AIMessage).where(AIMessage.conversation_id.in_(convs)))
                await db.execute(delete(AIConversation).where(AIConversation.owner_id.in_(uids)))
                await db.execute(delete(User).where(User.id.in_(uids)))
                await db.commit()


@pytest.mark.asyncio
async def test_must_haves_card():
    import app.main  # noqa: F401
    from sqlalchemy import delete, insert, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, User, collection_properties
    from app.services.ai import ai_actions
    tag = f"zqmh-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            col = Collection(name="Lisa", owner_id=agent.id, status="ACTIVE", share_token=tag, visitor_name="Lisa Testcase")
            db.add(col)
            await db.commit()
            db.add(CollectionPreferences(collection_id=col.id, school_districts=["Downingtown Area"]))
            ids = (await db.execute(select(Property.id).where(Property.home_status == 'ACTIVE-BRIGHT').limit(4))).scalars().all()
            await db.execute(insert(collection_properties), [{"collection_id": col.id, "property_id": i} for i in ids])
            await db.commit()
            ai_actions._load()
            res, entry = await ai_actions.propose(db, agent, "set_showcase_must_haves",
                                                  {"collection_id": col.id, "wants": ["Downingtown East high school"]})
            assert entry is not None, res
            assert entry["title"] == "Star homes in Lisa Testcase's Showcase"
            assert entry["details"][0] == "Must-haves: Downingtown East high school"
            assert any(d.startswith(f"Reads {len(ids)} home") for d in entry["details"]), entry["details"]
            assert entry["payload"]["body"] == {"wants": ["Downingtown East high school"]}
            # the chat often holds the person's id, not the Showcase's - either finds it
            from app.models.database import Lead
            col.visitor_email = f"{tag}-lisa@example.com"
            lead = Lead(owner_id=agent.id, name="Lisa Testcase", email=col.visitor_email, represented="no", status="new", tags=[])
            db.add(lead)
            await db.commit()
            for args in ({"lead_id": lead.id}, {"collection_id": lead.id}):
                res, entry = await ai_actions.propose(db, agent, "set_showcase_must_haves", {**args, "wants": ["Pool"]})
                assert entry is not None and entry["payload"]["collection_id"] == col.id, (args, res)
            res, entry = await ai_actions.propose(db, agent, "set_showcase_must_haves", {"collection_id": col.id, "wants": []})
            assert entry is None and res["error"] == "cannot_prepare"
            # a plan without MLS can't
            agent.plan_tier = "BASIC"
            res, entry = await ai_actions.propose(db, agent, "set_showcase_must_haves", {"collection_id": col.id, "wants": ["Pool"]})
            assert entry is None
    finally:
        async with AsyncSessionLocal() as db:
            uids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if uids:
                cols = (await db.execute(select(Collection.id).where(Collection.owner_id.in_(uids)))).scalars().all()
                if cols:
                    await db.execute(delete(collection_properties).where(collection_properties.c.collection_id.in_(cols)))
                    await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id.in_(cols)))
                await db.execute(delete(Collection).where(Collection.owner_id.in_(uids)))
                from app.models.database import Lead
                await db.execute(delete(Lead).where(Lead.owner_id.in_(uids)))
                await db.execute(delete(User).where(User.id.in_(uids)))
                await db.commit()


@pytest.mark.asyncio
async def test_district_names_resolve_and_sides_become_must_have_hint():
    """'DASD East' isn't a district (11/19 replay saved it and the Showcase would have come back empty)."""
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import Property
    from app.services.ai.ai_actions import ActionError
    from app.services.ai.ai_actions_catalog import _real_districts
    async with AsyncSessionLocal() as db:
        if not (await db.execute(select(Property.id).where(Property.school_district_name.ilike("DOWNINGTOWN AREA")).limit(1))).first():
            pytest.skip("no Downingtown Area listings in this database")
        for raw in ("DASD", "DASD East", "Downingtown Area School District", "downingtown area"):
            search, lines = {"school_districts": [raw]}, [f"School districts: {raw}"]
            await _real_districts(db, search, lines)
            assert search["school_districts"] == ["Downingtown Area"], (raw, search)
            assert "School districts: Downingtown Area" in lines
            side = [l for l in lines if "isn't its own district" in l]
            assert bool(side) == ("East" in raw), (raw, lines)
        with pytest.raises(ActionError):
            await _real_districts(db, {"school_districts": ["Zzyzx Unified"]}, [])


@pytest.mark.asyncio
async def test_side_words_inside_real_district_names():
    """'West Chester Area' / 'Central Bucks' carry a side word in their own name - never split by it; 'Central Bucks East' is."""
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import Property
    from app.services.ai.ai_actions_catalog import _real_districts
    async with AsyncSessionLocal() as db:
        for raw, want, side in (("West Chester Area", "West Chester Area", False), ("WCASD", "West Chester Area", False),
                                ("Central Bucks East", "Central Bucks", True), ("North Penn", "North Penn", False)):
            if not (await db.execute(select(Property.id).where(Property.school_district_name.ilike(want)).limit(1))).first():
                continue
            search, lines = {"school_districts": [raw]}, []
            await _real_districts(db, search, lines)
            assert search["school_districts"] == [want], (raw, search)
            assert any("isn't its own district" in l for l in lines) == side, (raw, lines)
