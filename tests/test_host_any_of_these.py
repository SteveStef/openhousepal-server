"""'I'd host any of these' (owner, 11/12): one ask covering several of one listing agent's homes.

- listing_agent_homes: the listing agent's other hostable homes, taken / already-asked days,
  on-platform + eligibility.
- create_host_requests_batch: one group, one alert, skips what can't be asked, off-platform = nothing
  created (the page drafts a text / email instead), different listing agents refused.
- Approving one home closes the host's other homes in that ask on the SAME day only.
- "None of these work" declines the whole ask with one alert to the host.

Local dev Postgres, throwaway agents/listings (zqany-*), all removed afterwards. Nothing is sent:
email sending is stubbed."""
import asyncio
import uuid
from datetime import date, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, or_, select

import app.main  # noqa: F401
from app.database import AsyncSessionLocal
from app.models.database import Notification, OpenHouseHostRequest, OpenHouseHostRequestEvent, Property, User
from app.schemas.open_house_manager import CreateHostRequestBatch, HostAskItem
from app.services import open_house_manager_service as svc

PREFIX = "zqany"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    sent = []
    monkeypatch.setattr(svc, "_send_email_safely", lambda *a, **k: sent.append((a, k)))
    return sent


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        reqs = (await db.execute(select(OpenHouseHostRequest.id).where(OpenHouseHostRequest.listing_key.like(f"{PREFIX}-%")))).scalars().all()
        if reqs:
            await db.execute(delete(OpenHouseHostRequestEvent).where(OpenHouseHostRequestEvent.request_id.in_(reqs)))
            await db.execute(delete(OpenHouseHostRequest).where(OpenHouseHostRequest.id.in_(reqs)))
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


async def _setup(on_platform=True, brokerage="Zq Realty"):
    tag = uuid.uuid4().hex[:6]
    mls = f"ZQANY-{tag}"
    async with AsyncSessionLocal() as db:
        host = User(email=f"{PREFIX}-host-{tag}@example.com", hashed_password="x", first_name="Hana", last_name="Host", state="PA",
                    brokerage=brokerage, mls_id=f"ZQANYH-{tag}", plan_tier="PREMIUM", subscription_status="ACTIVE")
        db.add(host)
        lister = None
        if on_platform:
            lister = User(email=f"{PREFIX}-lister-{tag}@example.com", hashed_password="x", first_name="Lee", last_name="Lister", state="PA",
                          brokerage="Zq Realty", mls_id=mls, plan_tier="PREMIUM", subscription_status="ACTIVE")
            db.add(lister)
        homes = []
        for i, (street, status) in enumerate([("1 Ash St", "ACTIVE-BRIGHT"), ("2 Birch Rd", "ACTIVE-BRIGHT"), ("3 Cedar Ln", "COMING SOON-BRIGHT"),
                                              ("4 Dogwood Ct", "PENDING-BRIGHT")]):
            p = Property(listing_key=f"{PREFIX}-{tag}-{i}", street_address=street, city="Wayne", state="PA", home_status=status,
                         list_agent_mls_id=mls, list_agent_full_name="Lee Lister", list_agent_email="lee@example.com",
                         list_agent_preferred_phone="6105550111", price=500000 + i, is_manual=False, home_type="SINGLE_FAMILY")
            db.add(p)
            homes.append(p)
        await db.commit()
        return {"host": host.id, "lister": lister.id if lister else None, "keys": [h.listing_key for h in homes]}


def _item(key, d, a=13, b=15):
    return HostAskItem(listing_key=key, requested_date=d, requested_start_time=time(a), requested_end_time=time(b))


@pytest.mark.asyncio
async def test_homes_lists_this_agents_hostable_listings():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        out = await svc.listing_agent_homes(db, await db.get(User, s["host"]), s["keys"][0])
    keys = [h["ListingKey"] for h in out["homes"]]
    assert keys[0] == s["keys"][0]                       # the one they tapped comes first
    assert set(keys) == set(s["keys"][:2])               # coming soon w/o a date and under contract are left out
    assert out["agent"]["on_platform"] is True and out["agent"]["eligible"] is True
    assert out["agent"]["phone"] == "6105550111" and out["agent"]["email"] == "lee@example.com"


@pytest.mark.asyncio
async def test_one_ask_one_alert_and_yes_closes_same_day_only(no_email):
    s = await _setup()
    sat, sun = _sat(), _sat() + timedelta(days=1)
    a, b = s["keys"][0], s["keys"][1]
    async with AsyncSessionLocal() as db:
        res = await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(
            items=[_item(a, sat), _item(b, sat), _item(a, sun, 12, 14)], message="Happy to host any of these"))
    assert res.on_platform and len(res.created) == 3 and not res.skipped
    assert len({r.group_id for r in res.created}) == 1
    async with AsyncSessionLocal() as db:
        notes = (await db.execute(select(Notification).where(Notification.agent_id == s["lister"]))).scalars().all()
        assert len(notes) == 1 and "any of 2" in notes[0].title and "1 Ash St" in notes[0].message
    assert len(no_email) == 1 and no_email[0][0][2] == "host_request_group_received"
    v = no_email[0][0][3]
    assert v["kind"] == "each" and v["homes"] == 2 and len(v["opens"]) == 3           # one line per requested open
    from app.services.host_ask_email_service import render
    _, html, text = render("host_request_group_received", "s", {**v, "app_url": "https://openhousepal.com"})
    assert text.count("\n- ") == 3 and "1 Ash St" in html and "panel=host-requests" in html
    from app.services.agent_email_health_service import is_critical
    assert is_critical("host_request_group_received")   # never held for the weekly summary

    sat_a = next(r for r in res.created if r.listing_key == a and r.requested_date == sat)
    async with AsyncSessionLocal() as db:
        await svc.approve_host_request(db, await db.get(User, s["lister"]), sat_a.id)
    async with AsyncSessionLocal() as db:
        rows = {(r.listing_key, r.requested_date): r.status for r in (await db.execute(
            select(OpenHouseHostRequest).where(OpenHouseHostRequest.group_id == res.group_id))).scalars().all()}
    assert rows[(a, sat)] == "approved"
    assert rows[(b, sat)] == "declined"     # can't host two opens on one day
    assert rows[(a, sun)] == "pending"      # Sunday is still up to the listing agent
    assert all(r.group_homes == 2 for r in res.created)
    async with AsyncSessionLocal() as db:
        mine = {(r.listing_key, r.requested_date): r for r in await svc.get_my_requests(db, await db.get(User, s["host"]))}
    assert mine[(b, sat)].closed_note.startswith("Closed - you're hosting 1 Ash St")   # not a plain "no"
    assert mine[(a, sun)].group_homes == 2 and mine[(a, sat)].closed_note is None


@pytest.mark.asyncio
async def test_skips_what_cant_be_asked_and_refuses_when_nothing_left():
    s = await _setup()
    sat = _sat()
    a, b = s["keys"][0], s["keys"][1]
    async with AsyncSessionLocal() as db:
        first = await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(items=[_item(a, sat)]))
    assert len(first.created) == 1
    async with AsyncSessionLocal() as db:
        res = await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(items=[_item(a, sat), _item(b, sat)]))
    assert [r.listing_key for r in res.created] == [b]
    assert len(res.skipped) == 1 and "already asked" in res.skipped[0].reason
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="already asked"):
            await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(items=[_item(a, sat)]))


@pytest.mark.asyncio
async def test_off_platform_creates_nothing_and_mixed_agents_refused():
    s = await _setup(on_platform=False)
    async with AsyncSessionLocal() as db:
        res = await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(items=[_item(s["keys"][0], _sat()), _item(s["keys"][1], _sat())]))
        assert res.on_platform is False and not res.created
        homes = await svc.listing_agent_homes(db, await db.get(User, s["host"]), s["keys"][0])
        assert homes["agent"]["on_platform"] is False
        assert (await db.execute(select(OpenHouseHostRequest).where(OpenHouseHostRequest.listing_key.in_(s["keys"])))).first() is None
    s2 = await _setup()
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="different listing agents"):
            await svc.create_host_requests_batch(db, await db.get(User, s2["host"]), CreateHostRequestBatch(items=[_item(s["keys"][0], _sat()), _item(s2["keys"][0], _sat())]))


@pytest.mark.asyncio
async def test_other_brokerage_not_on_team_is_refused():
    s = await _setup(brokerage="Some Other Realty")
    async with AsyncSessionLocal() as db:
        homes = await svc.listing_agent_homes(db, await db.get(User, s["host"]), s["keys"][0])
        assert homes["agent"]["eligible"] is False
        with pytest.raises(ValueError, match="isn't accepting"):
            await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(items=[_item(s["keys"][0], _sat())]))


@pytest.mark.asyncio
async def test_none_of_these_declines_all_with_one_alert(no_email):
    s = await _setup()
    sat = _sat()
    async with AsyncSessionLocal() as db:
        res = await svc.create_host_requests_batch(db, await db.get(User, s["host"]), CreateHostRequestBatch(items=[_item(s["keys"][0], sat), _item(s["keys"][1], sat)]))
    async with AsyncSessionLocal() as db:
        n = await svc.decline_host_request_group(db, await db.get(User, s["lister"]), res.group_id)
    assert n == 2
    async with AsyncSessionLocal() as db:
        st = (await db.execute(select(OpenHouseHostRequest.status).where(OpenHouseHostRequest.group_id == res.group_id))).scalars().all()
        assert st == ["declined", "declined"]
        notes = (await db.execute(select(Notification).where(Notification.agent_id == s["host"]))).scalars().all()
        assert len(notes) == 1 and notes[0].title == "Not this time"
        # someone else can't decline it
        with pytest.raises(ValueError):
            await svc.decline_host_request_group(db, await db.get(User, s["host"]), res.group_id)


@pytest.mark.asyncio
async def test_ai_helper_prepares_one_request_for_several_homes():
    from app.services.ai.ai_actions import ActionError
    from app.services.ai.ai_actions_catalog import _p_request_host_several
    s = await _setup()
    sat = _sat().isoformat()
    async with AsyncSessionLocal() as db:
        p = await _p_request_host_several(db, await db.get(User, s["host"]), {"listing_key": s["keys"][0], "all_homes": True, "date": sat, "start_time": "13:00", "end_time": "15:00"})
    assert len(p.payload["body"]["items"]) == 2 and "any of 2" in p.title
    off = await _setup(on_platform=False)
    async with AsyncSessionLocal() as db:
        with pytest.raises(ActionError, match="isn't on OpenHousePal"):
            await _p_request_host_several(db, await db.get(User, off["host"]), {"listing_key": off["keys"][0], "all_homes": True, "date": sat, "start_time": "13:00", "end_time": "15:00"})
