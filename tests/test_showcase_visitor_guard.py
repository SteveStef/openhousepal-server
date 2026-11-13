"""A visitor's like / pass / comment / view on a Showcase (audit 11/13): a wrong or old link gets a
clean 404 instead of a 500, a Showcase the agent turned off takes no more likes, a lapsed plan gives
the "paused" 410, and a home that isn't in the Showcase can't be liked. The agent's own actions still
work on a turned-off Showcase. Runs against the local Postgres in one rolled-back transaction."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402

NOW = datetime.now(timezone.utc)


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


async def _scenario():
    import httpx
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.database import get_db
    from app.main import app as fastapi_app
    from app.models.database import Collection, Property, User, collection_properties
    from app.utils import auth

    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    tag = uuid.uuid4().hex[:8]
    out = {}
    try:
        ann = User(id=str(uuid.uuid4()), email=f"vg-ann-{tag}@example.com", hashed_password="x", first_name="Ann",
                   last_name="Test", state="PA", brokerage="Test Realty", plan_tier="PREMIUM", subscription_status="ACTIVE",
                   broker_authorized=True)
        db.add(ann)
        inside = Property(id=str(uuid.uuid4()), listing_key=f"VG-in-{tag}", street_address="1 In Ln", city="Testville",
                          state="PA", home_status="ACTIVE-BRIGHT", price=500000)
        outside = Property(id=str(uuid.uuid4()), listing_key=f"VG-out-{tag}", street_address="2 Out Ln", city="Testville",
                           state="PA", home_status="ACTIVE-BRIGHT", price=500000)
        db.add_all([inside, outside])
        s = Collection(id=str(uuid.uuid4()), name="Buyer", owner_id=ann.id, share_token=f"vg-{tag}", is_public=True,
                       status="ACTIVE", visitor_name="Pat Buyer", visitor_email=f"vg-pat-{tag}@example.com")
        db.add(s)
        await db.flush()
        await db.execute(insert(collection_properties), [{"collection_id": s.id, "property_id": inside.id, "added_at": NOW}])
        await db.flush()

        current = {"user": None}

        async def _db():
            yield db

        async def _user():
            return current["user"]

        fastapi_app.dependency_overrides[get_db] = _db
        fastapi_app.dependency_overrides[auth.get_current_user_optional] = _user
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            like = {"interaction_type": "like", "value": True}
            fake = str(uuid.uuid4())
            out["like_ok"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/interact", json=like)).status_code
            out["comment_ok"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/comments", json={"content": "Love it"})).status_code
            out["view_ok"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/view")).status_code
            out["like_not_in_showcase"] = (await c.post(f"/collections/{s.id}/properties/{outside.id}/interact", json=like)).status_code
            out["like_fake_showcase"] = (await c.post(f"/collections/{fake}/properties/{inside.id}/interact", json=like)).status_code
            out["comments_fake_showcase"] = (await c.get(f"/collections/{fake}/properties/{inside.id}/comments")).status_code
            # Agent turns it off: the visitor's actions stop, the agent's still work.
            s.status = "INACTIVE"
            await db.flush()
            out["like_turned_off"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/interact", json=like)).status_code
            out["comment_turned_off"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/comments", json={"content": "Hi"})).status_code
            current["user"] = ann
            out["agent_like_turned_off"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/interact", json=like)).status_code
            out["agent_comments_turned_off"] = (await c.get(f"/collections/{s.id}/properties/{inside.id}/comments")).status_code
            current["user"] = None
            # Back on, but the agent's plan lapsed to Basic: "paused".
            s.status = "ACTIVE"
            ann.plan_tier = "BASIC"
            await db.flush()
            out["like_plan_lapsed"] = (await c.post(f"/collections/{s.id}/properties/{inside.id}/interact", json=like)).status_code
    finally:
        fastapi_app.dependency_overrides.clear()
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()
    return out


@pytest.fixture(scope="module")
def scenario():
    return asyncio.run(_scenario())


def test_visitor_actions_on_a_live_showcase(scenario):
    assert scenario["like_ok"] == 200
    assert scenario["comment_ok"] == 200
    assert scenario["view_ok"] == 200


def test_wrong_links_are_404_not_500(scenario):
    assert scenario["like_fake_showcase"] == 404
    assert scenario["comments_fake_showcase"] == 404
    assert scenario["like_not_in_showcase"] == 404


def test_turned_off_showcase_takes_no_visitor_actions(scenario):
    assert scenario["like_turned_off"] == 404
    assert scenario["comment_turned_off"] == 404
    assert scenario["agent_like_turned_off"] == 200
    assert scenario["agent_comments_turned_off"] == 200


def test_lapsed_plan_is_paused(scenario):
    assert scenario["like_plan_lapsed"] == 410


# ---- the daily briefing words showing requests and search-change requests apart (audit 11/13) ----

async def _counts():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.models.database import Collection, Property, PropertyTour, User
    from app.services.collections_service import CollectionsService
    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    tag = uuid.uuid4().hex[:8]
    try:
        u = User(id=str(uuid.uuid4()), email=f"vg-bri-{tag}@example.com", hashed_password="x", first_name="Bri",
                 last_name="Test", state="PA", brokerage="Test Realty", plan_tier="PREMIUM", subscription_status="ACTIVE")
        h = Property(id=str(uuid.uuid4()), listing_key=f"VG-bri-{tag}", street_address="3 Tour Ln", city="Testville", state="PA", home_status="ACTIVE-BRIGHT", price=400000)
        s = Collection(id=str(uuid.uuid4()), name="B", owner_id=u.id, share_token=f"vgb-{tag}", is_public=True, status="ACTIVE")
        db.add_all([u, h, s]); await db.flush()
        db.add(PropertyTour(collection_id=s.id, property_id=h.id, visitor_name="Pat", visitor_email="p@example.com",
                            preferred_date="2026-11-20", preferred_time="12:00"))
        await db.flush()
        return (await CollectionsService.count_pending_refine_requests(db, u.id),
                await CollectionsService.count_pending_refine_requests(db, u.id, split=True))
    finally:
        await db.close(); await trans.rollback(); await conn.close(); await engine.dispose()


def test_briefing_counts_showings_apart_from_search_changes():
    total, split = asyncio.run(_counts())
    assert total == 1            # the Showcases nav badge still counts both
    assert split == (0, 1)       # the briefing: 0 search-change requests, 1 showing request
