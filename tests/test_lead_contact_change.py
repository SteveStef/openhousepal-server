"""Editing a lead's email / phone updates it everywhere (user, 10/24): their Showcase's recipient,
sign-in records, waiting emails; a Showcase asleep for no response wakes; "Send them a new
invitation" queues the chosen invite to the new address.

Local dev Postgres with throwaway agents (zqcon-*@example.com), all removed afterward. Nothing is
sent - the email scheduler isn't run; ScheduledEmail rows are only inspected."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, or_, select

import app.main  # noqa: F401
from app.api.lead_routes import lead_invite_options, lead_reinvite, update_lead, LeadReinvite
from app.database import AsyncSessionLocal
from app.models.database import (BlacklistedEmail, ClientEmail, ClientEmailPref, Collection, Lead, LeadEvent, Notification,
                                 OpenHouseEvent, OpenHouseVisitor, ScheduledEmail, User)
from app.schemas.leads import LeadUpdate
from app.services import client_email_service as ce

PREFIX = "zqcon"


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
        await db.execute(delete(ScheduledEmail).where(or_(ScheduledEmail.recipient_email.ilike(f"{PREFIX}-%"),
                                                          ScheduledEmail.owner_id.in_(users))))
        await db.execute(delete(BlacklistedEmail).where(BlacklistedEmail.email.ilike(f"{PREFIX}-%")))
        if users:
            await db.execute(delete(Notification).where(Notification.agent_id.in_(users)))
            await db.execute(delete(ClientEmail).where(ClientEmail.owner_id.in_(users)))
            await db.execute(delete(ClientEmailPref).where(ClientEmailPref.owner_id.in_(users)))
            leads = (await db.execute(select(Lead.id).where(Lead.owner_id.in_(users)))).scalars().all()
            if leads:
                await db.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(leads)))
        if users:
            ohs = (await db.execute(select(OpenHouseEvent.id).where(OpenHouseEvent.agent_id.in_(users)))).scalars().all()
            await db.execute(delete(Collection).where(Collection.owner_id.in_(users)))
            if ohs:
                await db.execute(delete(OpenHouseVisitor).where(OpenHouseVisitor.open_house_event_id.in_(ohs)))
                await db.execute(delete(OpenHouseEvent).where(OpenHouseEvent.id.in_(ohs)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


async def _setup():
    async with AsyncSessionLocal() as db:
        agent = User(email=_email("agent"), hashed_password="x", first_name="Test", last_name="Agent", state="PA",
                     brokerage="Test Brokerage", mls_id=f"ZQCON-{uuid.uuid4().hex[:8]}", plan_tier="PREMIUM", subscription_status="ACTIVE")
        db.add(agent)
        await db.flush()
        old = _email("typo")
        lead = Lead(owner_id=agent.id, name="John Smith", email=old, phone="6105550100", represented="no", status="new", tags=[])
        oh = OpenHouseEvent(agent_id=agent.id, address="9 Ivy Ln", city="Wayne", state="PA", qr_code=uuid.uuid4().hex,
                            scheduled_date=datetime.now(timezone.utc).date())
        db.add_all([lead, oh])
        await db.flush()
        visitor = OpenHouseVisitor(full_name="John Smith", email=old, phone="6105550100", has_agent="NO", open_house_event_id=oh.id,
                                   qr_code=oh.qr_code, lead_id=lead.id)
        col = Collection(name="John's homes", owner_id=agent.id, visitor_email=old, visitor_name="John Smith", visitor_phone="6105550100",
                         status="INACTIVE", inactive_reason="no_response", share_token=uuid.uuid4().hex, original_open_house_event_id=oh.id)
        db.add_all([visitor, col])
        await db.flush()
        db.add(LeadEvent(lead_id=lead.id, source_type="collection_visitor", summary="Showcase created", collection_id=col.id,
                         occurred_at=datetime.now(timezone.utc)))
        waiting = ScheduledEmail(recipient_email=old, subject="Following up", template_name="visitor_confirmation", template_variables={},
                                 owner_id=agent.id, status="PENDING", scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1))
        db.add(waiting)
        await db.commit()
        return {"agent": agent.id, "lead": lead.id, "old": old, "col": col.id, "visitor": visitor.id, "waiting": waiting.id}


@pytest.mark.asyncio
async def test_email_and_phone_change_goes_everywhere():
    s = await _setup()
    new = _email("right")
    async with AsyncSessionLocal() as db:
        out = await update_lead(lead_id=s["lead"], body=LeadUpdate(email=new.upper(), phone="(484) 555-0199"), db=db,
                                current_user=await db.get(User, s["agent"]))
    assert out.email == new
    async with AsyncSessionLocal() as db:
        col = await db.get(Collection, s["col"])
        assert col.visitor_email == new and col.visitor_phone == out.phone and "0199" in (out.phone or "")
        assert col.status == "ACTIVE" and col.inactive_reason is None   # the old address's silence no longer counts
        v = await db.get(OpenHouseVisitor, s["visitor"])
        assert v.email == new
        assert (await db.get(ScheduledEmail, s["waiting"])).recipient_email == new

        opts = await lead_invite_options(lead_id=s["lead"], db=db, current_user=await db.get(User, s["agent"]))
        assert opts == {"collection_id": s["col"], "open_house": True}
        res = await lead_reinvite(lead_id=s["lead"], body=LeadReinvite(kind="open_house"), db=db, current_user=await db.get(User, s["agent"]))
        assert res == {"sent": True, "to": new}
        sent = (await db.execute(select(ScheduledEmail).where(ScheduledEmail.recipient_email == new,
                                                              ScheduledEmail.id != s["waiting"]))).scalars().all()
        assert len(sent) == 1 and sent[0].template_name == "visitor_confirmation" and sent[0].status == "PENDING"
        assert "/showcase/" in sent[0].template_variables["showcase_link"]


@pytest.mark.asyncio
async def test_normal_showcase_invite():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        await update_lead(lead_id=s["lead"], body=LeadUpdate(email=_email("fixed")), db=db, current_user=await db.get(User, s["agent"]))
    async with AsyncSessionLocal() as db:
        lead = await db.get(Lead, s["lead"])
        res = await lead_reinvite(lead_id=s["lead"], body=LeadReinvite(kind="showcase"), db=db, current_user=await db.get(User, s["agent"]))
        assert res["sent"] is True
        row = (await db.execute(select(ScheduledEmail).where(ScheduledEmail.recipient_email == lead.email,
                                                             ScheduledEmail.template_name == "showcase_ready"))).scalars().first()
        assert row is not None


# ---- emails to the old address were off: ask before turning them back on (user, 11/12) ----

async def _sent(db, owner, to, days_ago, template="showcase_daily_digest", **kw):
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(ClientEmail(id=str(uuid.uuid4()), owner_id=owner, recipient_email=to, template=template, subject="x", kind="auto",
                       via="openhousepal", status="sent", sent_at=at, created_at=at, **kw))


async def _change(s, new):
    async with AsyncSessionLocal() as db:
        await update_lead(lead_id=s["lead"], body=LeadUpdate(email=new), db=db, current_user=await db.get(User, s["agent"]))


async def _status(s, email):
    async with AsyncSessionLocal() as db:
        return await ce.status_for(db, s["agent"], email)


async def _turn_on(s, email):
    async with AsyncSessionLocal() as db:
        return await ce.set_manual(db, s["agent"], email, True)


@pytest.mark.asyncio
async def test_bounced_old_address_holds_new_until_agent_says_yes():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        await _sent(db, s["agent"], s["old"], 2, delivery="bounced", delivery_note="Bounced - that address doesn't exist",
                    delivery_at=datetime.now(timezone.utc) - timedelta(days=2))
        db.add(BlacklistedEmail(email=s["old"]))
        await db.commit()
    assert (await _status(s, s["old"]))["code"] == "bounced"
    new = _email("fixed")
    await _change(s, new)
    st = await _status(s, new)
    assert st["mode"] == "off" and st["code"] == "email_changed"
    assert "bounced" in st["reason"] and "until you turn them back on" in st["reason"]
    async with AsyncSessionLocal() as db:
        ev = (await db.execute(select(LeadEvent).where(LeadEvent.lead_id == s["lead"], LeadEvent.source_type == "email_paused"))).scalars().all()
        assert len(ev) == 1
    on = await _turn_on(s, new)
    assert on["mode"] == "daily" and on["code"] == "ok" and on["manual"] is None
    async with AsyncSessionLocal() as db:
        pref = (await db.execute(select(ClientEmailPref).where(ClientEmailPref.owner_id == s["agent"], ClientEmailPref.email == new))).scalar_one()
        assert pref.reset_at and pref.reset_at > datetime.now(timezone.utc) - timedelta(minutes=2)   # the count starts over
    assert (await _status(s, s["old"]))["code"] == "bounced"   # the bad address stays blocked


@pytest.mark.asyncio
async def test_agent_off_and_unsubscribed_old_address_hold_too():
    s = await _setup()
    await _turn_off(s, s["old"])
    new = _email("new1")
    await _change(s, new)
    st = await _status(s, new)
    assert st["code"] == "email_changed" and "You had turned their emails off" in st["reason"]

    s2 = await _setup()
    async with AsyncSessionLocal() as db:
        db.add(BlacklistedEmail(email=s2["old"]))   # unsubscribed - no bounce on record
        await db.commit()
    new2 = _email("new2")
    await _change(s2, new2)
    st2 = await _status(s2, new2)
    assert st2["code"] == "email_changed" and "unsubscribed" in st2["reason"] and "only turn emails back on if they asked" in st2["reason"]


async def _turn_off(s, email):
    async with AsyncSessionLocal() as db:
        return await ce.set_manual(db, s["agent"], email, False)


@pytest.mark.asyncio
async def test_ignored_weekly_emails_hold_and_showcase_stays_asleep_until_yes():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        ev = (await db.execute(select(LeadEvent).where(LeadEvent.lead_id == s["lead"]))).scalars().all()
        for e in ev:
            e.occurred_at = datetime.now(timezone.utc) - timedelta(days=90)
        for i in range(ce.WEEKLY_STRIKES):
            await _sent(db, s["agent"], s["old"], 3 + 7 * i, template=ce.WEEKLY_TEMPLATE)
        await db.commit()
    old_st = await _status(s, s["old"])
    assert old_st["mode"] == "off" and old_st["code"] == "weekly_unopened"
    new = _email("new3")
    await _change(s, new)
    st = await _status(s, new)
    assert st["code"] == "email_changed" and "stopped opening" in st["reason"]
    async with AsyncSessionLocal() as db:
        col = await db.get(Collection, s["col"])
        assert col.visitor_email == new and col.status == "INACTIVE"   # not woken behind the agent's back
    await _turn_on(s, new)
    async with AsyncSessionLocal() as db:
        col = await db.get(Collection, s["col"])
        assert col.status == "ACTIVE" and col.inactive_reason is None   # yes wakes it
    # fresh cycle: the old address's ignored emails don't count against the new one
    st = await _status(s, new)
    assert st["mode"] == "daily" and st["unopened"] == 0


@pytest.mark.asyncio
async def test_emails_on_at_old_address_means_no_question():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        await _sent(db, s["agent"], s["old"], 1, opened_at=datetime.now(timezone.utc) - timedelta(hours=20))
        await db.commit()
    new = _email("new4")
    await _change(s, new)
    st = await _status(s, new)
    assert st["mode"] == "daily" and st["code"] == "ok"
    async with AsyncSessionLocal() as db:
        assert (await db.get(Collection, s["col"])).status == "ACTIVE"   # old behaviour kept


@pytest.mark.asyncio
async def test_new_address_already_blocked_or_turned_off_stays_that_way():
    s = await _setup()
    new = _email("blocked")
    async with AsyncSessionLocal() as db:
        db.add(BlacklistedEmail(email=new))
        await db.commit()
    await _change(s, new)
    assert (await _status(s, new))["code"] == "unsubscribed"   # page shows "can't go to this address"
    assert (await _turn_on(s, new))["mode"] == "off"            # and turning on can't override it

    s2 = await _setup()
    await _turn_off(s2, s2["old"])
    new2 = _email("mine-off")
    await _turn_off(s2, new2)    # the agent had already switched this address off
    await _change(s2, new2)
    assert (await _status(s2, new2))["code"] == "manual_off"


@pytest.mark.asyncio
async def test_keep_them_off_blocks_sending_and_frequency_pick_turns_on():
    s = await _setup()
    await _turn_off(s, s["old"])
    new = _email("new5")
    await _change(s, new)
    async with AsyncSessionLocal() as db:
        assert (await ce.refresh_status(db, s["agent"], new, announce=False))["mode"] == "off"   # nightly check doesn't lift it
    async with AsyncSessionLocal() as db:
        st = await ce.set_frequency(db, s["agent"], new, "daily")
    assert st["mode"] == "daily" and st["manual"] is None


@pytest.mark.asyncio
async def test_ai_helper_warning_matches():
    from app.services.ai.ai_actions_catalog import _p_update_lead
    s = await _setup()
    async with AsyncSessionLocal() as db:
        p = await _p_update_lead(db, await db.get(User, s["agent"]), {"lead_id": s["lead"], "email": _email("ai1")})
        assert "restarts their automatic emails" in p.warning
    await _turn_off(s, s["old"])
    async with AsyncSessionLocal() as db:
        p = await _p_update_lead(db, await db.get(User, s["agent"]), {"lead_id": s["lead"], "email": _email("ai2")})
        assert "stay paused at the new address" in p.warning
