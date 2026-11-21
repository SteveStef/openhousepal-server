"""Day 18 launch audit (11/21): a plan over time, through the real PayPal webhook route, in order - trial ends,
PayPal's first payment comes hours later, a card fails, the card is fixed, a plan change, a re-sent notice,
cancelling keeps the paid time, then it expires. Checks the agent's status, what the public sees
(owner_has_access) and the account emails at every step. PayPal is faked; one rolled-back transaction."""
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


def test_trial_grace_only_with_a_paypal_subscription():
    from types import SimpleNamespace as U
    from app.utils.auth import subscription_is_active
    now = datetime.now(timezone.utc)
    ended = now - timedelta(hours=5)
    assert subscription_is_active(U(subscription_status="TRIAL", trial_ends_at=ended, subscription_id="I-X"))
    assert not subscription_is_active(U(subscription_status="TRIAL", trial_ends_at=ended, subscription_id=None))
    assert not subscription_is_active(U(subscription_status="TRIAL", trial_ends_at=now - timedelta(hours=49), subscription_id="I-X"))
    assert subscription_is_active(U(subscription_status="TRIAL", trial_ends_at=now + timedelta(days=3), subscription_id=None))
    # a failed card is never covered by the grace
    assert not subscription_is_active(U(subscription_status="SUSPENDED", trial_ends_at=ended, subscription_id="I-X"))


async def _lifecycle():
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.api import webhook_routes
    from app.database import get_db
    from app.main import app as fastapi_app
    from app.models.database import User
    from app.services import agent_email_service, open_house_service
    from app.services.paypal_service import paypal_service
    from app.utils import plan_ids
    from app.utils.auth import owner_has_access

    tiers = {}
    for pid, tier in plan_ids.PLAN_ID_TO_TIER.items():
        tiers.setdefault(tier, pid)
    if not {"BASIC", "PREMIUM"} <= set(tiers):
        pytest.skip("PayPal plan ids not set in this .env")

    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    tag = uuid.uuid4().hex[:8]
    sub_id = f"I-LIFE{tag.upper()}"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    paypal_next = {"t": now + timedelta(days=30)}
    emails = []

    async def fake_verify(**k):
        return True

    async def fake_get(sid):
        return {"id": sid, "status": "ACTIVE", "billing_info": {"next_billing_time": paypal_next["t"].isoformat().replace("+00:00", "Z")}}

    orig = (paypal_service.verify_webhook_signature, paypal_service.get_subscription, webhook_routes.PAYPAL_WEBHOOK_ID,
            agent_email_service.email_agent, open_house_service.schedule_showcase_backfill)
    paypal_service.verify_webhook_signature, paypal_service.get_subscription = fake_verify, fake_get
    webhook_routes.PAYPAL_WEBHOOK_ID = "WH-TEST"
    agent_email_service.email_agent = lambda user, kind, template, subject, variables=None, **k: emails.append((template, subject, variables or {}))
    open_house_service.schedule_showcase_backfill = lambda *a, **k: None
    H = {"paypal-transmission-id": "t", "paypal-transmission-time": now.isoformat(), "paypal-cert-url": "https://api.paypal.com/c",
         "paypal-auth-algo": "SHA256withRSA", "paypal-transmission-sig": "s"}
    try:
        agent = User(id=str(uuid.uuid4()), email=f"life-{tag}@example.com", hashed_password="x", first_name="Liv", last_name="Cycle",
                     state="PA", brokerage="Test Realty", mls_id=f"LC{tag}", plan_tier="BASIC", plan_id=tiers["BASIC"],
                     subscription_status="TRIAL", subscription_id=sub_id, broker_authorized=True,
                     trial_ends_at=now - timedelta(hours=3), next_billing_date=now - timedelta(hours=3),
                     subscription_started_at=now - timedelta(days=30))
        db.add(agent)
        await db.flush()

        async def _db():
            yield db
        fastapi_app.dependency_overrides[get_db] = _db

        async def send(event_type, resource, event_id=None):
            body = {"id": event_id or f"WH-{uuid.uuid4().hex}", "event_type": event_type, "resource": resource}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
                r = await c.post("/webhooks/paypal", json=body, headers=H)
            assert r.status_code == 200, r.text
            await db.refresh(agent)
            return r.json()

        # 1. Trial ended 3 hours ago, PayPal hasn't sent the payment yet: still open (public pages too)
        assert owner_has_access(agent)

        # 2. The first payment arrives
        await send("PAYMENT.SALE.COMPLETED", {"id": "SALE1", "billing_agreement_id": sub_id, "amount": {"total": "24.95"}})
        assert agent.subscription_status == "ACTIVE" and agent.last_billing_date is not None
        assert abs((agent.next_billing_date - paypal_next["t"]).total_seconds()) < 5 and owner_has_access(agent)

        # 3. Next month the card is declined: paused + the payment-failed email
        await send("PAYMENT.SALE.DENIED", {"id": "SALE2", "billing_agreement_id": sub_id, "amount": {"total": "24.95"}})
        assert agent.subscription_status == "SUSPENDED" and not owner_has_access(agent)
        assert emails[-1][0] == "payment_failed" and emails[-1][2].get("amount") == "$24.95"

        # 4. PayPal suspends the subscription too: the paused email
        await send("BILLING.SUBSCRIPTION.SUSPENDED", {"id": sub_id, "plan_id": tiers["BASIC"]})
        assert agent.subscription_status == "SUSPENDED" and emails[-1][0] == "subscription_suspended"

        # 5. Card fixed, PayPal takes the payment: back on, pages back
        paypal_next["t"] = now + timedelta(days=60)
        await send("PAYMENT.SALE.COMPLETED", {"id": "SALE3", "billing_agreement_id": sub_id, "amount": {"total": "24.95"}})
        assert agent.subscription_status == "ACTIVE" and owner_has_access(agent)

        # 6. Upgrade to Premium: plan + the plan-changed email; a re-sent copy changes nothing
        ev = f"WH-UP-{tag}"
        await send("BILLING.SUBSCRIPTION.UPDATED", {"id": sub_id, "plan_id": tiers["PREMIUM"]}, event_id=ev)
        assert agent.plan_tier == "PREMIUM" and emails[-1][0] == "subscription_plan_changed"
        assert emails[-1][2]["old_plan"] != emails[-1][2]["new_plan"]
        n = len(emails)
        again = await send("BILLING.SUBSCRIPTION.UPDATED", {"id": sub_id, "plan_id": tiers["PREMIUM"]}, event_id=ev)
        assert again.get("status") == "already_processed" and len(emails) == n

        # 7. Cancel: keeps the paid time already set (60 days), cancelled email names that date
        await send("BILLING.SUBSCRIPTION.CANCELLED", {"id": sub_id, "plan_id": tiers["PREMIUM"]})
        assert agent.subscription_status == "CANCELLED" and abs((agent.next_billing_date - paypal_next["t"]).total_seconds()) < 5
        assert owner_has_access(agent) and emails[-1][0] == "subscription_cancelled" and emails[-1][2].get("access_end_date")

        # 8. The paid time runs out: locked; PayPal's expired notice
        agent.next_billing_date = now - timedelta(minutes=1)
        assert not owner_has_access(agent)
        await send("BILLING.SUBSCRIPTION.EXPIRED", {"id": sub_id, "plan_id": tiers["PREMIUM"]})
        assert agent.subscription_status == "EXPIRED" and not owner_has_access(agent)
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        (paypal_service.verify_webhook_signature, paypal_service.get_subscription, webhook_routes.PAYPAL_WEBHOOK_ID,
         agent_email_service.email_agent, open_house_service.schedule_showcase_backfill) = orig
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


def test_plan_over_time_through_paypal_notices():
    asyncio.run(_lifecycle())
