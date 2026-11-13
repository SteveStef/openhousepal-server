"""Sign-up/settings launch audit 11/13 - Cancel Plan works on a subscription that is approved but hasn't
started yet. A plan switch or restart made during a trial (or mid-month) starts on the next bill date, so
PayPal keeps it APPROVED until then; Cancel used to refuse it ("Only active or suspended...") and the agent
would be charged when it started. PayPal is faked; runs in one rolled-back transaction."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
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


async def _scenario(paypal_status):
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.database import get_db
    from app.main import app as fastapi_app
    from app.models.database import User
    from app.services.paypal_service import paypal_service
    from app.utils.auth import create_access_token

    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    tag = uuid.uuid4().hex[:8]
    starts = (datetime.now(timezone.utc) + timedelta(days=9)).replace(microsecond=0)
    cancelled = []

    async def fake_get(sub_id):
        return {"id": sub_id, "status": paypal_status, "billing_info": {"next_billing_time": starts.isoformat().replace("+00:00", "Z")}}

    async def fake_cancel(subscription_id, reason=""):
        cancelled.append(subscription_id)
        return True

    orig = (paypal_service.get_subscription, paypal_service.cancel_subscription)
    paypal_service.get_subscription, paypal_service.cancel_subscription = fake_get, fake_cancel
    try:
        agent = User(id=str(uuid.uuid4()), email=f"cancel-approved-{tag}@example.com", hashed_password="x", first_name="Cal",
                     last_name="Test", state="PA", brokerage="Test Realty", mls_id=f"CA{tag}", plan_tier="IDX",
                     subscription_status="TRIAL", subscription_id=f"I-TEST{tag.upper()}", broker_authorized=True,
                     trial_ends_at=starts, next_billing_date=starts)
        db.add(agent)
        await db.flush()

        async def _db():
            yield db

        fastapi_app.dependency_overrides[get_db] = _db
        token = create_access_token({"sub": agent.id})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
            res = await c.post("/subscriptions/cancel", headers={"Authorization": f"Bearer {token}"})
        await db.refresh(agent)
        return res.status_code, agent.subscription_status, agent.next_billing_date, cancelled, starts
    finally:
        paypal_service.get_subscription, paypal_service.cancel_subscription = orig
        fastapi_app.dependency_overrides.clear()
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


def test_cancel_works_before_a_switched_plan_starts():
    code, status, until, cancelled, starts = asyncio.run(_scenario("APPROVED"))
    assert code == 200
    assert status == "CANCELLED"
    assert len(cancelled) == 1
    assert until == starts  # keeps access until the day the new plan would have started


def test_cancel_still_refuses_an_already_cancelled_subscription():
    code, status, _until, cancelled, _ = asyncio.run(_scenario("CANCELLED"))
    assert code == 400
    assert status == "TRIAL" and cancelled == []
