"""Open House Manager / Discovery launch audit (11/13) fixes.

- The same host ask sent twice (double tap, or sent again later) is ONE request: no second card,
  alert or email for the listing agent - even when both taps land at the same moment.
- Refused: asking to host your own listing (single ask and "any of these"), a sold / closed home,
  and an end time before the start time (ask, suggested time, team ask).
- Two yeses for one home's day at the same moment: one host, and the other gets the plain
  "already has a confirmed host" answer (it used to hit a database deadlock).
- The "open house cancelled" email says which day and time.

Local dev Postgres, throwaway agents/listings (zqaud-*), all removed afterwards. Nothing is sent:
email sending is stubbed."""
import asyncio
import uuid
from datetime import date, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, or_, select

import app.main  # noqa: F401
from app.database import AsyncSessionLocal
from app.models.database import (Notification, OpenHouseHostRequest, OpenHouseHostRequestEvent, OpenHouseTeamBroadcast,
                                 Property, User)
from app.schemas.open_house_manager import (CancelApprovedOpenRequest, CounterHostRequestRequest, CreateBroadcastRequest,
                                            CreateHostRequestBatch, CreateHostRequestRequest, HostAskItem)
from app.services import open_house_manager_service as svc

PREFIX = "zqaud"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(svc, "_send_email_safely", lambda recipient, subject, template, variables, notification_type=None:
                        out.append((getattr(recipient, "email", recipient), subject, template, variables)))
    return out


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        reqs = (await db.execute(select(OpenHouseHostRequest.id).where(OpenHouseHostRequest.listing_key.like(f"{PREFIX}-%")))).scalars().all()
        if reqs:
            await db.execute(delete(OpenHouseHostRequestEvent).where(OpenHouseHostRequestEvent.request_id.in_(reqs)))
            await db.execute(delete(OpenHouseHostRequest).where(OpenHouseHostRequest.id.in_(reqs)))
        await db.execute(delete(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.listing_key.like(f"{PREFIX}-%")))
        if users:
            await db.execute(delete(Notification).where(Notification.agent_id.in_(users)))
        await db.execute(delete(Property).where(Property.listing_key.like(f"{PREFIX}-%")))
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


def _sat(weeks=1) -> date:
    d = date.today() + timedelta(days=1)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d + timedelta(weeks=weeks - 1)


async def _world():
    tag = uuid.uuid4().hex[:6]
    async with AsyncSessionLocal() as db:
        mk = lambda k, first: User(email=f"{PREFIX}-{k}-{tag}@example.com", hashed_password="x", first_name=first, last_name="Zq", state="PA",
                                   brokerage="Zq Realty", mls_id=f"ZQAUD{k.upper()}-{tag}", plan_tier="PREMIUM", subscription_status="ACTIVE")
        la, h1, h2 = mk("la", "Lena"), mk("h1", "Hank"), mk("h2", "Hope")
        db.add_all([la, h1, h2])
        for k, street, status in (("A", "1 Ash St", "ACTIVE-BRIGHT"), ("B", "2 Birch Rd", "ACTIVE-BRIGHT"), ("SOLD", "3 Sold Ct", "CLOSED-BRIGHT")):
            db.add(Property(listing_key=f"{PREFIX}-{tag}-{k}", street_address=street, city="Berwyn", state="PA", home_status=status,
                            list_agent_mls_id=la.mls_id, is_manual=False, price=500000))
        await db.commit()
        return tag, la.id, h1.id, h2.id


async def _as(uid, fn, *args):
    async with AsyncSessionLocal() as db:
        return await fn(db, await db.get(User, uid), *args)


def _ask(tag, k, d, a=13, b=15):
    return CreateHostRequestRequest(listing_key=f"{PREFIX}-{tag}-{k}", requested_date=d, requested_start_time=time(a), requested_end_time=time(b))


async def _rows(tag, k):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.listing_key == f"{PREFIX}-{tag}-{k}"))).scalars().all()


async def _bells(uid):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Notification).where(Notification.agent_id == uid))).scalars().all()


@pytest.mark.asyncio
async def test_same_ask_twice_is_one_request(sent):
    tag, la, h1, _ = await _world()
    first = await _as(h1, svc.create_host_request, _ask(tag, "A", _sat()))
    again = await _as(h1, svc.create_host_request, _ask(tag, "A", _sat()))
    assert again.request.id == first.request.id
    assert len(await _rows(tag, "A")) == 1
    assert len(await _bells(la)) == 1
    assert len([m for m in sent if m[2] == "host_request_received"]) == 1


@pytest.mark.asyncio
async def test_two_taps_at_once_make_one_request():
    tag, la, h1, _ = await _world()
    await asyncio.gather(_as(h1, svc.create_host_request, _ask(tag, "B", _sat())), _as(h1, svc.create_host_request, _ask(tag, "B", _sat())))
    assert len(await _rows(tag, "B")) == 1
    assert len(await _bells(la)) == 1


@pytest.mark.asyncio
async def test_refused_asks():
    tag, la, h1, _ = await _world()
    with pytest.raises(ValueError, match="your own listing"):
        await _as(la, svc.create_host_request, _ask(tag, "A", _sat()))
    with pytest.raises(ValueError, match="your own listings"):
        await _as(la, svc.create_host_requests_batch, CreateHostRequestBatch(items=[
            HostAskItem(listing_key=f"{PREFIX}-{tag}-A", requested_date=_sat(), requested_start_time=time(13), requested_end_time=time(15)),
            HostAskItem(listing_key=f"{PREFIX}-{tag}-B", requested_date=_sat(), requested_start_time=time(13), requested_end_time=time(15))]))
    with pytest.raises(ValueError, match="no longer for sale"):
        await _as(h1, svc.create_host_request, _ask(tag, "SOLD", _sat()))
    with pytest.raises(ValueError, match="end time"):
        await _as(h1, svc.create_host_request, _ask(tag, "A", _sat(), 15, 13))
    with pytest.raises(ValueError, match="end time"):
        await _as(la, svc.create_broadcast, CreateBroadcastRequest(listing_key=f"{PREFIX}-{tag}-A", requested_date=_sat(),
                                                                   requested_start_time=time(16), requested_end_time=time(12)))
    ok = await _as(h1, svc.create_host_request, _ask(tag, "A", _sat()))
    with pytest.raises(ValueError, match="end time"):
        await _as(la, svc.counter_host_request, ok.request.id, CounterHostRequestRequest(
            counter_date=_sat(), counter_start_time=time(14), counter_end_time=time(14)))
    assert [r.listing_key for r in await _rows(tag, "SOLD")] == []


@pytest.mark.asyncio
async def test_two_yeses_for_one_day_at_once():
    tag, la, h1, h2 = await _world()
    r1 = await _as(h1, svc.create_host_request, _ask(tag, "A", _sat(2)))
    r2 = await _as(h2, svc.create_host_request, _ask(tag, "A", _sat(2), 14, 16))
    res = await asyncio.gather(_as(la, svc.approve_host_request, r1.request.id), _as(la, svc.approve_host_request, r2.request.id),
                               return_exceptions=True)
    assert sum(1 for x in res if not isinstance(x, Exception)) == 1
    errors = [x for x in res if isinstance(x, Exception)]
    assert all(isinstance(e, ValueError) for e in errors), errors   # a plain answer, not a database error
    assert len([r for r in await _rows(tag, "A") if r.status == "approved"]) == 1


@pytest.mark.asyncio
async def test_cancel_email_names_the_day(sent):
    tag, la, h1, _ = await _world()
    r = await _as(h1, svc.create_host_request, _ask(tag, "A", _sat(3), 12, 14))
    await _as(la, svc.approve_host_request, r.request.id)
    sent.clear()
    await _as(la, svc.cancel_approved_open, f"{PREFIX}-{tag}-A", CancelApprovedOpenRequest(hosting_agent_id=h1, scheduled_date=_sat(3)))
    mail = [m for m in sent if m[0].startswith(f"{PREFIX}-h1-")]
    assert len(mail) == 1
    when = svc._format_date(_sat(3))
    assert when in mail[0][1] and "12:00 PM" in mail[0][3]["message_after"]


@pytest.mark.asyncio
async def test_dash_keeps_unanswered_ask_after_alert_is_seen():
    tag, la, h1, _ = await _world()
    from app.models.database import OpenHouseTeamMember
    async with AsyncSessionLocal() as db:
        db.add(OpenHouseTeamMember(owner_id=la, member_agent_id=h1, status="accepted"))
        await db.commit()
    await _as(la, svc.create_broadcast, CreateBroadcastRequest(listing_key=f"{PREFIX}-{tag}-A", requested_date=_sat(),
                                                               requested_start_time=time(13), requested_end_time=time(15)))
    c = await _as(h1, svc.get_action_counts)
    assert c.pending_incoming_broadcasts == 1 and c.open_incoming_asks == 1
    async with AsyncSessionLocal() as db:   # the host opens the alert but doesn't answer
        for n in (await db.execute(select(Notification).where(Notification.agent_id == h1))).scalars().all():
            n.is_read = True
        await db.commit()
    c = await _as(h1, svc.get_action_counts)
    assert c.pending_incoming_broadcasts == 0   # the red badge clears...
    assert c.open_incoming_asks == 1            # ...but Dash still says it's waiting on them
    async with AsyncSessionLocal() as db:
        await db.execute(delete(OpenHouseTeamMember).where(OpenHouseTeamMember.owner_id == la))
        await db.commit()


@pytest.mark.asyncio
async def test_cancelled_team_ask_tells_the_team():
    tag, la, h1, h2 = await _world()
    from app.models.database import OpenHouseTeamMember
    async with AsyncSessionLocal() as db:
        db.add_all([OpenHouseTeamMember(owner_id=la, member_agent_id=h, status="accepted") for h in (h1, h2)])
        await db.commit()
    try:
        b = await _as(la, svc.create_broadcast, CreateBroadcastRequest(listing_key=f"{PREFIX}-{tag}-A", requested_date=_sat(),
                                                                       requested_start_time=time(13), requested_end_time=time(15)))
        await _as(la, svc.cancel_broadcast, b.id)
        for h in (h1, h2):
            bells = await _bells(h)
            gone = [n for n in bells if n.title == "Host no longer needed"]
            assert len(gone) == 1 and not gone[0].is_read
            assert "1 Ash St" in gone[0].message and svc._format_date(_sat()) in gone[0].message
            assert all(n.is_read for n in bells if n.type == "TEAM_BROADCAST_RECEIVED")   # the old "needs a host" stops asking
            assert (await _as(h, svc.get_action_counts)).open_incoming_asks == 0
        await _as(la, svc.cancel_broadcast, b.id)   # cancelling again doesn't alert twice
        assert len([n for n in await _bells(h1) if n.title == "Host no longer needed"]) == 1
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(OpenHouseTeamMember).where(OpenHouseTeamMember.owner_id == la))
            await db.commit()


@pytest.mark.asyncio
async def test_turned_down_time_email_names_the_day(sent):
    tag, la, h1, _ = await _world()
    r = await _as(h1, svc.create_host_request, _ask(tag, "A", _sat(2)))
    await _as(la, svc.counter_host_request, r.request.id, CounterHostRequestRequest(
        counter_date=_sat(2), counter_start_time=time(16), counter_end_time=time(18)))
    sent.clear()
    await _as(h1, svc.respond_to_counter, r.request.id, False)
    mail = [m for m in sent if m[0].startswith(f"{PREFIX}-la-")]
    assert len(mail) == 1
    assert svc._format_date(_sat(2)) in mail[0][3]["message_after"] and "4:00 PM" in mail[0][3]["message_after"]
