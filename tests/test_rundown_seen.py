"""Today's Rundown, once looked at, steps aside for the header's "Briefing" button (owner, 10/26) -
rundown_service.seen_state / mark_seen. Real queries on the local database in one rolled-back
transaction."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.models.database import EmailLog, Notification, User
from app.services import rundown_service as rs

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest_asyncio.fixture
async def db():
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        session.commit = session.flush
        yield session
        await session.rollback()


async def _clean_owner(db):
    owner = (await db.execute(select(User).order_by(User.created_at).limit(1))).scalars().first()
    start = rs.day_start(NOW)
    await db.execute(text("DELETE FROM system_settings WHERE key = :k"), {"k": f"rundown_seen:{owner.id}"})
    await db.execute(delete(EmailLog).where(EmailLog.owner_id == owner.id, EmailLog.template.like(rs.BRIEFING_EMAIL), EmailLog.created_at >= start))
    await db.execute(delete(Notification).where(Notification.agent_id == owner.id, Notification.created_at >= start))
    return owner


def _note(owner, at, kind="OPEN_HOUSE_SIGN_IN"):
    return Notification(id=str(uuid.uuid4()), agent_id=owner.id, type=kind, reference_type=kind, title="Sign-in", message="Jane signed in",
                        reference_id=uuid.uuid4().hex, is_read=False, created_at=at)


def _briefing_email(owner, sent, opened=None, clicked=None):
    return EmailLog(id=str(uuid.uuid4()), template="showcase_daily_digest_agent_v2", audience="agent", owner_id=owner.id, via="openhousepal",
                    status="sent", created_at=sent, tracked=True, opened_at=opened, clicked_at=clicked, open_count=1 if opened else 0)


@pytest.mark.asyncio
async def test_not_looked_at_yet_keeps_the_dash_card(db):
    owner = await _clean_owner(db)
    db.add(_note(owner, NOW - timedelta(minutes=5)))
    await db.flush()
    st = await rs.seen_state(db, owner, NOW)
    assert st == {"seen": False, "seen_at": None, "via": None, "new_since": 0}


@pytest.mark.asyncio
async def test_opening_it_in_the_app_then_new_activity_lights_the_button(db):
    owner = await _clean_owner(db)
    db.add(_note(owner, NOW - timedelta(minutes=30)))
    await db.flush()
    await rs.mark_seen(db, owner, NOW - timedelta(minutes=10))
    st = await rs.seen_state(db, owner, NOW)
    assert st["seen"] and st["via"] == "app" and st["new_since"] == 0
    db.add(_note(owner, NOW - timedelta(minutes=2)))
    db.add(_note(owner, NOW - timedelta(minutes=1), "PROPERTY_INTERACTION"))
    await db.flush()
    assert (await rs.seen_state(db, owner, NOW))["new_since"] == 2
    await rs.mark_seen(db, owner, NOW)          # opened again - back to calm
    assert (await rs.seen_state(db, owner, NOW))["new_since"] == 0


@pytest.mark.asyncio
async def test_opening_or_clicking_the_briefing_email_counts(db):
    owner = await _clean_owner(db)
    db.add(_briefing_email(owner, NOW - timedelta(hours=1), opened=NOW - timedelta(minutes=20)))
    await db.flush()
    st = await rs.seen_state(db, owner, NOW)
    assert st["seen"] and st["via"] == "email" and st["seen_at"] == NOW - timedelta(minutes=20)
    db.add(_briefing_email(owner, NOW - timedelta(minutes=50), opened=NOW - timedelta(minutes=19), clicked=NOW - timedelta(minutes=5)))
    await db.flush()
    assert (await rs.seen_state(db, owner, NOW))["seen_at"] == NOW - timedelta(minutes=5)


@pytest.mark.asyncio
async def test_yesterdays_look_does_not_count_today(db):
    owner = await _clean_owner(db)
    await rs.mark_seen(db, owner, NOW - timedelta(days=1))
    assert (await rs.seen_state(db, owner, NOW))["seen"] is False


@pytest.mark.asyncio
async def test_briefing_sent_after_they_looked_is_new(db):
    owner = await _clean_owner(db)
    local_day = NOW.astimezone(rs.EASTERN).date().isoformat()
    await rs.mark_seen(db, owner, NOW - timedelta(minutes=10))
    db.add(Notification(id=str(uuid.uuid4()), agent_id=owner.id, type=rs.RUNDOWN_TYPE, reference_type=rs.RUNDOWN_TYPE, title="Today's Rundown", message="3 new homes.",
                        reference_id=f"digest-{local_day}", is_read=False, created_at=NOW - timedelta(minutes=1)))
    await db.flush()
    assert (await rs.seen_state(db, owner, NOW))["new_since"] == 1


# ---------------------------------------------------------------- setup: "I don't use this"
@pytest.mark.asyncio
async def test_not_using_a_connect_step_counts_as_finished(db):
    from app.api.setup_routes import _hub
    from app.services import setup_flow_service as flow
    user = (await db.execute(select(User).where(User.plan_tier.isnot(None), User.broker_authorized.is_(True),
                                                User.crm_push_enabled.isnot(True)).limit(1))).scalars().first()
    if user is None:
        pytest.skip("no approved agent on this database")
    hub = await _hub(db, user)
    user.setup_state = {**flow.state_of(user), "skipped": ["crm"], "not_using": ["crm", "about"]}
    data = await flow.compute(db, user, hub)
    status = {s["id"]: s["status"] for s in data["steps"]}
    assert status["crm"] == "not_using"
    assert status["about"] != "not_using"             # a required step can't be answered that way
    others = [s for s in data["steps"] if s["id"] != "crm" and s["status"] not in ("done", "not_using")]
    assert data["complete"] == (not others)
