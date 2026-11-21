"""The whole 3-then-2 email rule against the real database (owner, 11/18): 3 unopened days -> weekly,
an open house follow-up counts as one of the 2 weekly ones, the 2nd -> off + the Showcase falls asleep
and automatic emails are held; opening one old email puts them straight back on daily and wakes the
Showcase.

Local dev Postgres with throwaway agents (zqcyc-*@example.com), all removed afterward. Nothing is sent."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, or_, select

import app.main  # noqa: F401
from app.database import AsyncSessionLocal
from app.models.database import (ClientEmail, ClientEmailPref, Collection, Lead, LeadEvent, Notification, ScheduledEmail, User)
from app.services import client_email_service as ce

PREFIX = "zqcyc"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _email(label: str) -> str:
    return f"{PREFIX}-{label}-{uuid.uuid4().hex[:8]}@example.com"


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        await db.execute(delete(ScheduledEmail).where(or_(ScheduledEmail.recipient_email.ilike(f"{PREFIX}-%"), ScheduledEmail.owner_id.in_(users))))
        if users:
            await db.execute(delete(Notification).where(Notification.agent_id.in_(users)))
            await db.execute(delete(ClientEmail).where(ClientEmail.owner_id.in_(users)))
            await db.execute(delete(ClientEmailPref).where(ClientEmailPref.owner_id.in_(users)))
            leads = (await db.execute(select(Lead.id).where(Lead.owner_id.in_(users)))).scalars().all()
            if leads:
                await db.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(leads)))
            await db.execute(delete(Collection).where(Collection.owner_id.in_(users)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


async def _setup():
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        agent = User(email=_email("agent"), hashed_password="x", first_name="Test", last_name="Agent", state="PA",
                     brokerage="Test Brokerage", mls_id=f"ZQCYC-{uuid.uuid4().hex[:8]}", plan_tier="PREMIUM", subscription_status="ACTIVE")
        db.add(agent)
        await db.flush()
        client = _email("client")
        lead = Lead(owner_id=agent.id, name="Casey Quiet", email=client, represented="no", status="new", tags=[])
        col = Collection(name="Casey's homes", owner_id=agent.id, visitor_email=client, visitor_name="Casey Quiet",
                         status="ACTIVE", share_token=uuid.uuid4().hex)
        db.add_all([lead, col])
        await db.flush()
        # They came in 2 months ago and haven't done anything since.
        db.add(LeadEvent(lead_id=lead.id, source_type="collection_visitor", summary="Showcase created", collection_id=col.id,
                         occurred_at=now - timedelta(days=60)))
        await db.commit()
        return {"agent": agent.id, "lead": lead.id, "client": client, "col": col.id}


async def _sent(s, days_ago: float, template: str = "showcase_daily_digest") -> str:
    async with AsyncSessionLocal() as db:
        # Midday (17:00 UTC = noon/1 PM Eastern): near midnight, and around the clock change, two "days ago" can be one Eastern day.
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(hour=17, minute=0)
        rec = ClientEmail(owner_id=s["agent"], recipient_email=s["client"], template=template, subject="x",
                          kind="auto" if template in ce.AUTO_TEMPLATES else "personal", status="sent", sent_at=at, created_at=at)
        db.add(rec)
        await db.commit()
        return rec.id


async def _refresh(s):
    async with AsyncSessionLocal() as db:
        return await ce.refresh_status(db, s["agent"], s["client"])


async def _col_status(s):
    async with AsyncSessionLocal() as db:
        c = await db.get(Collection, s["col"])
        return c.status, c.inactive_reason


@pytest.mark.asyncio
async def test_three_daily_then_invite_and_one_weekly_sleep_then_an_open_wakes():
    s = await _setup()
    first = await _sent(s, 20)
    await _sent(s, 19)
    assert (await _refresh(s))["mode"] == "daily"          # 2 days isn't enough
    await _sent(s, 18)
    st = await _refresh(s)
    assert (st["mode"], st["code"]) == ("weekly", "unopened_days")
    assert "1 more" not in st["reason"] and "2 more go unopened" in st["reason"]

    await _sent(s, 12, "visitor_confirmation")            # an open house follow-up counts as one
    st = await _refresh(s)
    assert (st["mode"], st["weekly_unopened"]) == ("weekly", 1)
    assert await _col_status(s) == ("ACTIVE", None)

    await _sent(s, 5, ce.WEEKLY_TEMPLATE)
    st = await _refresh(s)
    assert (st["mode"], st["code"], st["unopened"]) == ("off", "weekly_unopened", 5)
    assert await _col_status(s) == ("INACTIVE", "no_response")     # asleep
    held = await ce.before_send(s["agent"], s["client"], "showcase_daily_digest", "New homes", {"properties": []})
    assert held is None                                            # automatic emails held back
    async with AsyncSessionLocal() as db:
        bells = (await db.execute(select(Notification.title).where(Notification.agent_id == s["agent"], Notification.type == "EMAIL_STATUS"))).scalars().all()
    assert any("weekly" in t for t in bells) and any("asleep" in t or "stopped" in t for t in bells)

    # They open the very first (old) email -> back to daily, Showcase awake.
    async with AsyncSessionLocal() as db:
        await ce.record_open(db, first)
    async with AsyncSessionLocal() as db:
        st = await ce.status_for(db, s["agent"], s["client"])
    assert (st["mode"], st["code"]) == ("daily", "ok")
    assert await _col_status(s) == ("ACTIVE", None)


@pytest.mark.asyncio
async def test_several_emails_on_one_day_are_one_unopened_day():
    s = await _setup()
    for _ in range(3):
        await _sent(s, 10)
    await _sent(s, 9, "visitor_confirmation")
    st = await _refresh(s)
    assert (st["mode"], st["unopened"], st["unopened_days"]) == ("daily", 4, 2)
