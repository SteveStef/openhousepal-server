"""Email opens are the client opening the email - never the agent scrolling their own Sent folder
(owner, 10/26). Runs the real tracking code on the local database inside one rolled-back transaction."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.database import ClientEmail, Lead, LeadEvent, Notification, User
from app.services import client_email_service as ce

T0 = datetime(2026, 10, 26, 17, 30, tzinfo=timezone.utc)   # 1:30 PM Eastern


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


async def _owner(db):
    return (await db.execute(select(User.id).order_by(User.created_at).limit(1))).scalar()


async def _lead(db, owner_id, tag):
    lead = Lead(id=str(uuid.uuid4()), owner_id=owner_id, name=f"Test {tag}", email=f"open-test-{tag}-{uuid.uuid4().hex[:6]}@example.com")
    db.add(lead)
    await db.flush()
    return lead


async def _email(db, owner_id, lead, sent_at, via="mailbox"):
    rec = ClientEmail(id=str(uuid.uuid4()), owner_id=owner_id, recipient_email=lead.email, template="showcase_daily_digest",
                      subject="Your homes", kind="auto", via=via, status="sent", sent_at=sent_at,
                      links={"k1": "https://openhousepal.com/x"})
    db.add(rec)
    await db.flush()
    return rec


async def _history(db, lead):
    return (await db.execute(select(func.count()).select_from(LeadEvent).where(
        LeadEvent.lead_id == lead.id, LeadEvent.source_type.in_(("email_opened", "email_clicked"))))).scalar()


async def _bell(db, owner_id, lead):
    return (await db.execute(select(Notification).where(Notification.agent_id == owner_id, Notification.type == "EMAIL_ACTIVITY",
                                                        Notification.reference_id.like(f"{lead.email}|%")))).scalars().all()


@pytest_asyncio.fixture
async def db(monkeypatch):
    from app.database import AsyncSessionLocal
    clock = Clock()
    monkeypatch.setattr(ce, "_now", clock)
    async with AsyncSessionLocal() as session:
        session.commit = session.flush          # everything stays in one transaction...
        session.clock = clock
        yield session
        await session.rollback()                # ...that is thrown away


@pytest.mark.asyncio
async def test_one_client_opening_an_older_email_counts(db):
    owner = await _owner(db)
    jane = await _lead(db, owner, "jane")
    rec = await _email(db, owner, jane, T0 - timedelta(days=1))
    await ce.record_open(db, rec.id)
    assert rec.opened_at == T0 and rec.open_count == 1
    assert await _history(db, jane) == 1 and len(await _bell(db, owner, jane)) == 1


@pytest.mark.asyncio
async def test_sent_folder_scroll_is_not_counted_and_takes_back_the_first_one(db):
    owner = await _owner(db)
    a, b, c = await _lead(db, owner, "a"), await _lead(db, owner, "b"), await _lead(db, owner, "c")
    ea = await _email(db, owner, a, T0 - timedelta(days=3))
    eb = await _email(db, owner, b, T0 - timedelta(days=1))
    ec = await _email(db, owner, c, T0 - timedelta(hours=5))
    await ce.record_open(db, ea.id)                 # looks like a real open at first...
    assert ea.opened_at is not None and await _history(db, a) == 1
    db.clock.now = T0 + timedelta(seconds=40)
    await ce.record_open(db, eb.id)                 # ...a second person's older email 40 s later: the Sent folder
    assert eb.opened_at is None and ea.opened_at is None and ea.open_count == 0
    assert await _history(db, a) == 0 and await _bell(db, owner, a) == []
    db.clock.now = T0 + timedelta(seconds=90)
    await ce.record_open(db, ec.id)                 # still scrolling
    db.clock.now = T0 + timedelta(seconds=100)
    await ce.record_open(db, ea.id)                 # the same one again
    assert ec.opened_at is None and ea.opened_at is None
    assert [await _history(db, x) for x in (a, b, c)] == [0, 0, 0]


@pytest.mark.asyncio
async def test_clients_opening_the_evening_update_right_away_all_count(db):
    owner = await _owner(db)
    leads = [await _lead(db, owner, f"eve{i}") for i in range(3)]
    sent = T0 - timedelta(minutes=4)
    recs = [await _email(db, owner, x, sent) for x in leads]
    for i, r in enumerate(recs):
        db.clock.now = T0 + timedelta(seconds=30 * i)
        await ce.record_open(db, r.id)
    assert all(r.opened_at is not None for r in recs)


@pytest.mark.asyncio
async def test_emails_sent_by_openhousepal_have_no_sent_copy(db):
    owner = await _owner(db)
    x, y = await _lead(db, owner, "x"), await _lead(db, owner, "y")
    ex, ey = await _email(db, owner, x, T0 - timedelta(days=2), via="openhousepal"), await _email(db, owner, y, T0 - timedelta(days=2), via="openhousepal")
    await ce.record_open(db, ex.id)
    db.clock.now = T0 + timedelta(seconds=20)
    await ce.record_open(db, ey.id)
    assert ex.opened_at is not None and ey.opened_at is not None


@pytest.mark.asyncio
async def test_a_click_always_counts_and_a_later_real_open_counts(db):
    owner = await _owner(db)
    a, b = await _lead(db, owner, "ca"), await _lead(db, owner, "cb")
    ea, eb = await _email(db, owner, a, T0 - timedelta(days=2)), await _email(db, owner, b, T0 - timedelta(days=2))
    await ce.record_open(db, ea.id)
    db.clock.now = T0 + timedelta(seconds=30)
    await ce.record_open(db, eb.id)                 # Sent folder - neither counted
    assert ea.opened_at is None and eb.opened_at is None
    db.clock.now = T0 + timedelta(minutes=1)
    url = await ce.record_click(db, ea.id, "k1")    # but a click is the client, even right after
    assert url == "https://openhousepal.com/x" and ea.clicked_at is not None and ea.opened_at is not None
    db.clock.now = T0 + timedelta(hours=6)
    await ce.record_open(db, eb.id)                 # Jane's real open that evening
    assert eb.opened_at == T0 + timedelta(hours=6) and await _history(db, b) == 1


@pytest.mark.asyncio
async def test_past_sent_folder_opens_are_corrected_and_real_ones_kept(db):
    owner = await _owner(db)
    a, b, solo = await _lead(db, owner, "pa"), await _lead(db, owner, "pb"), await _lead(db, owner, "ps")
    ea, eb, es = (await _email(db, owner, a, T0 - timedelta(days=3)), await _email(db, owner, b, T0 - timedelta(days=3)),
                  await _email(db, owner, solo, T0 - timedelta(days=3)))
    for r, at in ((ea, T0), (eb, T0 + timedelta(seconds=50)), (es, T0 + timedelta(hours=2))):
        r.opened_at, r.open_count = at, 1
    await db.flush()
    fixed = await ce.correct_sent_folder_opens(db)
    assert fixed >= 2 and ea.opened_at is None and eb.opened_at is None
    assert es.opened_at == T0 + timedelta(hours=2)
    assert ea.sent_folder_seen_at == T0
