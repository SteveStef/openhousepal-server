"""Kit Similar Homes (10/24): widening the radius must never drop a home that matched at a smaller
radius. The search used to cap the results at the most recently UPDATED homes in the square around
the circle, so a bigger circle pulled newer homes in from further out and pushed matched ones out.

Read-only against the local mirror DB (skips when there is no data to test with)."""
import asyncio

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.schemas.collection_preferences import CollectionPreferencesBase
from app.services.property_service import PropertyService
from app.services.showcase_limit import SHOWCASE_FETCH, SHOWCASE_MAX_HOMES, cap_matches


async def _dense_subjects(db, n=4):
    rows = (await db.execute(text("""
        select latitude, longitude, price from properties
        where home_status = 'ACTIVE-BRIGHT' and is_manual = false and home_type = 'SINGLE_FAMILY'
          and latitude is not null and price between 350000 and 700000
          and city in (select city from properties where home_status = 'ACTIVE-BRIGHT'
                       group by city order by count(*) desc limit 6)
        order by listing_key limit :n"""), {"n": n})).all()
    return rows


async def _ids(db, lat, lng, price, radius, cap):
    prefs = CollectionPreferencesBase(lat=float(lat), long=float(lng), diameter=float(radius),
                                      min_price=int(price * 0.8), max_price=int(price * 1.2),
                                      is_single_family=True)
    res = await PropertyService.get_properties_by_preferences(db, prefs, max_properties=cap, nearest_first=True)
    return [p.listing_key for p in res]


def test_growing_radius_never_drops_a_match():
    async def run():
        async with AsyncSessionLocal() as db:
            subjects = await _dense_subjects(db)
            if not subjects:
                pytest.skip("no local listings to test with")
            for lat, lng, price in subjects:
                prev = None
                for radius in (3, 5, 7, 10, 15):
                    ids = await _ids(db, lat, lng, float(price), radius, cap=60)
                    if prev is not None:
                        dropped = set(prev) - set(ids)
                        assert not dropped, f"{radius} mi dropped {len(dropped)} homes that matched at the smaller radius"
                    prev = ids
    async def main():
        try:
            await run()
        finally:
            await engine.dispose()  # pooled connections belong to this event loop
    asyncio.run(main())


def test_showcase_radius_widening_never_drops_a_match():
    """Showcase Save / repopulate and the live previews call the search with its defaults (no price
    window, a cap) - widening the distance must only ever add homes there too."""
    async def run():
        async with AsyncSessionLocal() as db:
            subjects = await _dense_subjects(db, n=3)
            if not subjects:
                pytest.skip("no local listings to test with")
            for lat, lng, _ in subjects:
                prev = None
                for radius in (2, 3, 5, 7, 10):
                    prefs = CollectionPreferencesBase(lat=float(lat), long=float(lng), diameter=float(radius))
                    kept, _ = cap_matches(await PropertyService.get_properties_by_preferences(db, prefs, max_properties=SHOWCASE_FETCH))
                    ids = [p.listing_key for p in kept]
                    assert len(ids) <= SHOWCASE_MAX_HOMES
                    if prev is not None:
                        dropped = set(prev) - set(ids)
                        assert not dropped, f"widening to {radius} mi dropped {len(dropped)} Showcase matches"
                    prev = ids
    async def main():
        try:
            await run()
        finally:
            await engine.dispose()  # pooled connections belong to this event loop
    asyncio.run(main())


def test_showcase_limit_is_100_and_flags_more():
    kept, more = cap_matches(list(range(130)))
    assert SHOWCASE_MAX_HOMES == 100 and len(kept) == 100 and more and kept[0] == 0
    kept, more = cap_matches(list(range(100)))
    assert len(kept) == 100 and not more


def test_full_showcase_new_listing_replaces_farthest_unprotected():
    """Owner option A (10/24): a new listing joining a full (100) distance Showcase takes the place of
    the farthest home nobody acted on; with every home protected it isn't added. Rolled back."""
    import uuid
    from sqlalchemy import insert, select as sa_select
    from app.models.database import Collection, CollectionPreferences, PropertyInteraction, User, collection_properties
    from app.services.showcase_limit import active_linked_count, make_room_for_new_listing
    from app.utils.geo import haversine_distance

    async def run():
        async with AsyncSessionLocal() as db:
            subjects = await _dense_subjects(db, n=1)
            owner = (await db.execute(sa_select(User.id).limit(1))).scalar_one_or_none()
            if not subjects or not owner:
                pytest.skip("no local data to test with")
            lat, lng, _ = subjects[0]
            prefs = CollectionPreferencesBase(lat=float(lat), long=float(lng), diameter=10.0)
            homes = await PropertyService.get_properties_by_preferences(db, prefs, max_properties=SHOWCASE_FETCH)
            if len(homes) <= SHOWCASE_MAX_HOMES + 1:
                pytest.skip("not enough homes nearby")
            cid = str(uuid.uuid4())
            try:
                db.add(Collection(id=cid, name="limit test", owner_id=owner))
                await db.flush()
                db.add(CollectionPreferences(collection_id=cid, lat=float(lat), long=float(lng), diameter=10.0))
                await db.flush()
                kept = homes[:SHOWCASE_MAX_HOMES]
                await db.execute(insert(collection_properties), [{"collection_id": cid, "property_id": p.id} for p in kept])
                await db.flush()
                assert await active_linked_count(db, cid) == SHOWCASE_MAX_HOMES
                # The farthest one is liked -> the next farthest makes way.
                db.add(PropertyInteraction(id=str(uuid.uuid4()), collection_id=cid, property_id=kept[-1].id, liked=True))
                await db.flush()
                newcomer = homes[SHOWCASE_MAX_HOMES]
                assert await make_room_for_new_listing(db, cid, newcomer)
                linked = {r[0] for r in (await db.execute(sa_select(collection_properties.c.property_id).where(collection_properties.c.collection_id == cid))).all()}
                gone = {p.id for p in kept} - linked
                assert len(gone) == 1 and kept[-1].id in linked
                d = {p.id: haversine_distance(float(lat), float(lng), p.latitude, p.longitude) for p in kept}
                assert d[next(iter(gone))] >= max(v for k, v in d.items() if k not in gone and k != kept[-1].id) - 1e-6
                # Everyone protected (the newcomer joined and got liked too) -> no room.
                await db.execute(insert(collection_properties).values(collection_id=cid, property_id=newcomer.id))
                for pid in linked | {newcomer.id}:
                    if pid != kept[-1].id:
                        db.add(PropertyInteraction(id=str(uuid.uuid4()), collection_id=cid, property_id=pid, liked=True))
                await db.flush()
                assert not await make_room_for_new_listing(db, cid, homes[SHOWCASE_MAX_HOMES + 1])
            finally:
                await db.rollback()

    async def main():
        try:
            await run()
        finally:
            await engine.dispose()
    asyncio.run(main())
