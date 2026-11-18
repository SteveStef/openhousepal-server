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
async def test_clicking_the_briefing_email_counts_but_just_opening_it_does_not(db):
    # iPhone Mail "opens" every email on its own - only a click proves they looked (owner, 11/18).
    owner = await _clean_owner(db)
    db.add(_briefing_email(owner, NOW - timedelta(hours=1), opened=NOW - timedelta(minutes=20)))
    await db.flush()
    assert (await rs.seen_state(db, owner, NOW))["seen"] is False
    db.add(_briefing_email(owner, NOW - timedelta(minutes=50), opened=NOW - timedelta(minutes=19), clicked=NOW - timedelta(minutes=5)))
    await db.flush()
    st = await rs.seen_state(db, owner, NOW)
    assert st["seen"] and st["via"] == "email" and st["seen_at"] == NOW - timedelta(minutes=5)


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


# ---------------------------------------------------------------- "Since you last looked" (11/18)
async def _clean_window(db, owner, days=10):
    start = rs.day_start(NOW) - timedelta(days=days)
    await db.execute(delete(Notification).where(Notification.agent_id == owner.id, Notification.created_at >= start))


@pytest.mark.asyncio
async def test_never_opened_means_no_catch_up(db):
    owner = await _clean_owner(db)
    assert await rs.catchup_since(db, owner, NOW) is None


@pytest.mark.asyncio
async def test_days_away_show_up_and_stay_after_todays_first_look(db):
    owner = await _clean_owner(db)
    await _clean_window(db, owner)
    start = rs.day_start(NOW)
    last_look = start - timedelta(days=3) + timedelta(hours=10)
    await rs.mark_seen(db, owner, last_look)
    db.add(_note(owner, last_look - timedelta(hours=1)))                    # before the look - already seen
    db.add(_note(owner, last_look + timedelta(hours=2)))                    # 3 days ago, after the look
    db.add(_note(owner, start - timedelta(hours=20)))                       # yesterday
    db.add(_note(owner, start - timedelta(hours=19), "PROPERTY_INTERACTION"))
    db.add(_note(owner, NOW))                                               # today - not catch-up
    await db.flush()

    cu = await rs.build_catchup(db, owner, NOW, showcase_access=False)
    assert cu is not None and not cu["capped"] and cu["days_away"] == 3
    assert cu["count"] == 3 and cu["unread"] == 3
    assert [d["count"] for d in cu["days"]] == [2, 1] and cu["days"][0]["label"] == "Yesterday"
    assert cu["range_label"].endswith("to yesterday")

    # Opening it today keeps the catch-up up until midnight...
    await rs.mark_seen(db, owner, NOW)
    assert await rs.catchup_since(db, owner, NOW) == last_look
    await rs.mark_seen(db, owner, NOW + timedelta(seconds=1))
    assert await rs.catchup_since(db, owner, NOW) == last_look
    # ...and "Mark all as read" clears those days too.
    await rs.mark_today_read(db, owner, NOW)
    cu = await rs.build_catchup(db, owner, NOW, showcase_access=False)
    assert cu["unread"] == 0
    # Tomorrow, today's look is the last one - nothing to catch up on.
    tomorrow = start + timedelta(days=1, hours=9)
    assert await rs.catchup_since(db, owner, tomorrow) == NOW + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_away_longer_than_a_week_is_capped(db):
    owner = await _clean_owner(db)
    await _clean_window(db, owner, days=20)
    start = rs.day_start(NOW)
    await rs.mark_seen(db, owner, start - timedelta(days=15))
    db.add(_note(owner, start - timedelta(days=10)))     # older than a week - not shown
    db.add(_note(owner, start - timedelta(days=2)))
    await db.flush()
    cu = await rs.build_catchup(db, owner, NOW, showcase_access=False)
    assert cu["capped"] and cu["count"] == 1 and cu["range_label"] == f"Your last {rs.CATCHUP_DAYS} days"


@pytest.mark.asyncio
async def test_looked_late_yesterday_and_nothing_since_means_no_catch_up(db):
    owner = await _clean_owner(db)
    await _clean_window(db, owner)
    await rs.mark_seen(db, owner, rs.day_start(NOW) - timedelta(minutes=30))
    await db.flush()
    assert await rs.build_catchup(db, owner, NOW, showcase_access=False) is None


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
