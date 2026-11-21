"""Day 21 launch audit (11/21): a brokerage group's AI allowance - $5 per agent and $8 for the whole group together.
Ann spends $6 -> she's stopped (her own $5), Ben can still use the group's last $2, then both are stopped by the
group total; a personal amount for Ann lifts her own limit but not the group's; leaving the group puts Ann back on her
plan's normal allowance. One rolled-back transaction, no AI called."""
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


async def _run():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.models.database import AIUsageEvent, BillingGroup, User
    from app.services.ai import ai_rate_limit_service as rl
    from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded

    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    cfg = {"agent_monthly_ai_budget_usd_by_tier": {"PREMIUM": 20.0}}
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)

    async def blocked(uid):
        try:
            await rl.enforce_agent_monthly_budget(db, uid, cfg, "PREMIUM")
            return False
        except AIRateLimitExceeded:
            return True

    try:
        g = BillingGroup(id=str(uuid.uuid4()), name=f"Test Brokerage {tag}", ai_budget_per_agent_usd=5.0, ai_budget_total_usd=8.0)
        db.add(g)
        ids = {}
        for k in ("ann", "ben"):
            u = User(id=str(uuid.uuid4()), email=f"grp-{k}-{tag}@example.com", hashed_password="x", first_name=k.title(), last_name="Group",
                     state="PA", brokerage="Test Brokerage", mls_id=f"G{k}{tag}", plan_tier="PREMIUM", subscription_status="ACTIVE",
                     broker_authorized=True, billing_group_id=g.id, subscription_started_at=now - timedelta(days=3))
            db.add(u)
            ids[k] = u.id
        await db.flush()

        def spend(uid, usd):
            db.add(AIUsageEvent(owner_id=uid, estimated_cost_usd=usd, blocked=False, event_type="chat", created_at=now - timedelta(minutes=5)))

        assert await rl.monthly_limit_usd(db, ids["ann"], cfg, "PREMIUM") == 5.0   # the group's per-agent amount, not the plan's $20
        spend(ids["ann"], 6.0)
        await db.flush()
        assert await blocked(ids["ann"]), "Ann spent $6 of her $5"
        assert not await blocked(ids["ben"]), "Ben still has the group's last $2"
        snap = await rl.monthly_ai_budget_snapshot(db, ids["ben"], cfg, "PREMIUM")
        assert snap["percent"] >= 75, snap          # the bar shows the group total running out first (6 of 8)
        spend(ids["ben"], 2.5)
        await db.flush()
        assert await blocked(ids["ben"]), "group total $8 used"

        # a personal amount for Ann lifts her own limit, but the group's total still stops her
        ann = await db.get(User, ids["ann"])
        ann.ai_budget_override_usd = 10.0
        await db.flush()
        assert await rl.monthly_limit_usd(db, ids["ann"], cfg, "PREMIUM") == 10.0
        assert await blocked(ids["ann"]), "the group total is used up"

        # out of the group: back to the plan's normal $20, no group total
        ann.billing_group_id = None
        await db.flush()
        assert await rl.monthly_limit_usd(db, ids["ann"], cfg, "PREMIUM") == 20.0
        assert await rl.group_pool(db, ids["ann"]) is None
        assert not await blocked(ids["ann"])
    finally:
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


def test_group_ai_allowance():
    asyncio.run(_run())
