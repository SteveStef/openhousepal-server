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
from app.models.database import (Collection, Lead, LeadEvent, OpenHouseEvent, OpenHouseVisitor, ScheduledEmail, User)
from app.schemas.leads import LeadUpdate

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
