"""Changing a Showcase from the chat (owner, 11/19):
- the card says how many homes it will hold after the change, before anything is saved;
- homes the client engaged with stay - liked, commented, toured, and now passed on (disliked) too."""
import uuid

import pytest


@pytest.mark.asyncio
async def test_card_counts_homes_and_disliked_homes_stay():
    import app.main  # noqa: F401
    from sqlalchemy import delete, insert, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, PropertyInteraction, User, collection_properties
    from app.services.ai import ai_actions
    from app.services.collections_service import CollectionsService, engaged_home_ids
    from app.services.property_service import property_service
    tag = f"zqcnt-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            sample = (await db.execute(select(Property).where(Property.home_status == "ACTIVE-BRIGHT", Property.township.isnot(None),
                                                              Property.price.isnot(None)).limit(1))).scalars().first()
            if sample is None:
                pytest.skip("no active listings with a township in this database")
            twp = sample.township
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            col = Collection(name="Lisa", owner_id=agent.id, status="ACTIVE", share_token=tag, visitor_name="Lisa Testcase")
            db.add(col)
            await db.commit()
            db.add(CollectionPreferences(collection_id=col.id, townships=[twp]))
            # a home from somewhere else that she passed on, and one she liked
            others = (await db.execute(select(Property.id).where(Property.home_status == "ACTIVE-BRIGHT", Property.township != twp).limit(2))).scalars().all()
            await db.execute(insert(collection_properties), [{"collection_id": col.id, "property_id": i} for i in others])
            db.add_all([PropertyInteraction(collection_id=col.id, property_id=others[0], disliked=True),
                        PropertyInteraction(collection_id=col.id, property_id=others[1], liked=True)])
            await db.commit()

            assert await engaged_home_ids(db, col.id) == set(others)

            # what the search will find, the same way the refresh does
            from types import SimpleNamespace
            from app.services.showcase_limit import SHOWCASE_FETCH
            prefs = SimpleNamespace(**{k: None for k in CollectionPreferences.__table__.columns.keys()}, )
            prefs.townships = [twp]
            prefs.min_beds = 1
            n = len(await property_service.get_properties_by_preferences(db, prefs, max_properties=SHOWCASE_FETCH))

            ai_actions._load()
            res, entry = await ai_actions.propose(db, agent, "update_showcase_search", {"collection_id": col.id, "townships": [twp], "min_beds": 1})
            assert entry is not None, res
            line = next(d for d in entry["details"] if d.startswith("After this change"))
            assert "plus 2 they liked, passed on, commented on or asked to tour" in line, line
            if n <= 100:
                assert f"{n} home" in line, (n, line)

            # the real refresh keeps both
            p = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == col.id))).scalar_one()
            p.min_beds = 1
            await db.commit()
            await CollectionsService.repopulate_collection_from_preferences(db, col.id)
            linked = {r[0] for r in (await db.execute(select(collection_properties.c.property_id).where(collection_properties.c.collection_id == col.id))).all()}
            assert set(others) <= linked
            if n <= 100:
                assert len(linked - set(others)) == n, (len(linked), n)   # the card said exactly what the refresh did
    finally:
        async with AsyncSessionLocal() as db:
            uids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if uids:
                cols = (await db.execute(select(Collection.id).where(Collection.owner_id.in_(uids)))).scalars().all()
                if cols:
                    await db.execute(delete(PropertyInteraction).where(PropertyInteraction.collection_id.in_(cols)))
                    await db.execute(delete(collection_properties).where(collection_properties.c.collection_id.in_(cols)))
                    await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id.in_(cols)))
                await db.execute(delete(Collection).where(Collection.owner_id.in_(uids)))
                await db.execute(delete(User).where(User.id.in_(uids)))
                await db.commit()
