"""Studio launch audit 11/13 - the public review link only takes a real 1-5 star rating. No stars (0)
used to be filed as a 1-star complaint (alert, email, private follow-up task). Runs against the local
Postgres in one rolled-back transaction; nothing is saved or sent."""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


async def _scenario():
    import httpx
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.database import get_db
    from app.main import app as fastapi_app
    from app.models.database import AgentHub, AgentReview, User

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
        agent = User(id=str(uuid.uuid4()), email=f"rv-guard-{tag}@example.com", hashed_password="x", first_name="Rae",
                     last_name="Test", state="PA", brokerage="Test Realty", plan_tier="PREMIUM", subscription_status="ACTIVE")
        db.add(agent)
        await db.flush()
        db.add(AgentHub(owner_id=agent.id, slug=f"rv-guard-{tag}", config={}))
        await db.flush()

        async def _db():
            yield db

        fastapi_app.dependency_overrides[get_db] = _db
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            for r in (0, 6, -2):
                res = await c.post(f"/agent-hub/public/rv-guard-{tag}/reviews",
                                   json={"reviewer_name": "Pat", "rating": r, "body": "Great"}, headers={"X-Forwarded-For": f"10.9.{r % 200}.{tag[:2].__hash__() % 250}"})
                out[r] = (res.status_code, res.json().get("detail"))
        out["saved"] = (await db.execute(select(func.count()).select_from(AgentReview).where(AgentReview.owner_id == agent.id))).scalar_one()
    finally:
        fastapi_app.dependency_overrides.clear()
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()
    return out


def test_review_link_needs_real_star_rating():
    out = asyncio.run(_scenario())
    for r in (0, 6, -2):
        assert out[r] == (422, "Pick a star rating first."), (r, out[r])
    assert out["saved"] == 0


# ---- the new-review alert tells the agent when their hub isn't showing reviews (owner, 11/13) ----

async def _alerts():
    import httpx
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.database import get_db
    from app.main import app as fastapi_app
    from app.models.database import AgentHub, Notification, User

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
        for which, cfg in (("off", {}), ("on", {"reviews": {"showOnHub": True}})):
            agent = User(id=str(uuid.uuid4()), email=f"rv-al-{which}-{tag}@example.com", hashed_password="x", first_name="Al",
                         last_name="Test", state="PA", brokerage="Test Realty", plan_tier="PREMIUM", subscription_status="ACTIVE")
            db.add(agent); await db.flush()
            db.add(AgentHub(owner_id=agent.id, slug=f"rv-al-{which}-{tag}", config=cfg)); await db.flush()

            async def _db():
                yield db
            fastapi_app.dependency_overrides[get_db] = _db
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
                res = await c.post(f"/agent-hub/public/rv-al-{which}-{tag}/reviews", json={"reviewer_name": "Pat", "rating": 5, "body": "Great agent"},
                                   headers={"X-Forwarded-For": f"10.8.{len(which)}.{abs(hash(tag)) % 250}"})
            n = (await db.execute(select(Notification).where(Notification.agent_id == agent.id, Notification.type == "NEW_REVIEW"))).scalar_one()
            out[which] = (res.status_code, n.message)
    finally:
        fastapi_app.dependency_overrides.clear()
        await db.close(); await trans.rollback(); await conn.close(); await engine.dispose()
    return out


def test_new_review_alert_mentions_the_hub_switch_only_when_off():
    out = asyncio.run(_alerts())
    assert out["off"][0] in (200, 201) and "Show reviews on my Agent Hub" in out["off"][1]
    assert out["on"][0] in (200, 201) and "Show reviews" not in out["on"][1]
