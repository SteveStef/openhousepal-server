"""Deleting a lead cleans up after itself (lead_cleanup_service, user 10/21): the person moves to
"Not in your leads" on the Emails page, every waiting email to them is cancelled and automatic ones
are held, and their Showcase is turned off (kept, not deleted). Turning the Showcase back on, or the
person becoming a lead again, lets emails go to them again.

Runs against the local dev Postgres with throwaway agents (zqdel-*@example.com), all removed
afterward. No email is ever sent - the scheduler isn't run; rows are only inspected."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, or_, select

from app.api.client_email_routes import email_people
from app.api.lead_routes import delete_lead
from app.database import AsyncSessionLocal
from app.models.database import (ClientEmail, ClientEmailPref, Collection, Lead, LeadEvent, ScheduledEmail, User)
from app.services import client_email_service as ce
from app.services.collections_service import CollectionsService
from app.services.leads_service import upsert_lead

PREFIX = "zqdel"


@pytest.fixture(scope="session")
def event_loop():
    # The app's pooled asyncpg connections belong to the loop that made them (see
    # test_market_area_concurrency) - one loop for the module.
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _email(label: str) -> str:
    return f"{PREFIX}-{label}-{uuid.uuid4().hex[:8]}@example.com"


async def _agent(db, label: str) -> User:
    u = User(email=_email(label), hashed_password="not-a-real-hash", first_name="Test", last_name="Agent", state="PA",
             brokerage="Test Brokerage", mls_id=f"ZQDEL-{uuid.uuid4().hex[:8]}", plan_tier="PREMIUM", subscription_status="ACTIVE")
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        await db.execute(delete(ScheduledEmail).where(or_(ScheduledEmail.recipient_email.ilike(f"{PREFIX}-%"),
                                                          ScheduledEmail.owner_id.in_(users))))
        if users:
            await db.execute(delete(Collection).where(Collection.owner_id.in_(users)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


async def _setup():
    """Agent + Jane (lead, Showcase, sent and waiting emails) + rows that must NOT be touched."""
    async with AsyncSessionLocal() as db:
        agent, other = await _agent(db, "agent"), await _agent(db, "other")
        jane = _email("jane")
        lead = Lead(owner_id=agent.id, name="Jane Buyer", email=jane, represented="no", status="new", tags=[])
        db.add(lead)
        await db.flush()
        col = Collection(name="Jane's homes", owner_id=agent.id, visitor_email=jane.upper(), visitor_name="Jane Buyer", status="ACTIVE",
                         share_token=uuid.uuid4().hex)
        someone_else = Collection(name="Bob's homes", owner_id=agent.id, visitor_email=_email("bob"), status="ACTIVE", share_token=uuid.uuid4().hex)
        db.add_all([col, someone_else])
        await db.flush()
        db.add(LeadEvent(lead_id=lead.id, source_type="collection_visitor", summary="Showcase created", collection_id=col.id,
                         occurred_at=datetime.now(timezone.utc)))
        now = datetime.now(timezone.utc)
        db.add(ClientEmail(owner_id=agent.id, recipient_email=jane, template="showcase_daily_digest", subject="New homes", kind="auto",
                           status="sent", sent_at=now - timedelta(days=1)))
        rows = {
            "invite": ScheduledEmail(recipient_email=jane, subject="Following up", template_name="visitor_confirmation", template_variables={},
                                     owner_id=agent.id, status="PENDING", scheduled_for=now + timedelta(hours=1)),
            "digest_queue": ScheduledEmail(recipient_email=jane, subject="Price drop", template_name="showcase_daily_digest",
                                           template_variables={"_collection_id": col.id, "_owner_id": agent.id}, status="DIGEST", scheduled_for=now),
            "already_sent": ScheduledEmail(recipient_email=jane, subject="Old", template_name="showcase_ready", template_variables={},
                                           owner_id=agent.id, status="SENT", scheduled_for=now - timedelta(days=2), sent_at=now - timedelta(days=2)),
            "to_agent": ScheduledEmail(recipient_email=agent.email, subject="Showcase Updated: Jane", template_name="showcase_daily_digest_agent",
                                       template_variables={"_collection_id": col.id, "_owner_id": agent.id}, status="DIGEST", scheduled_for=now),
            "other_agent": ScheduledEmail(recipient_email=jane, subject="From another agent", template_name="visitor_confirmation",
                                          template_variables={}, owner_id=other.id, status="PENDING", scheduled_for=now + timedelta(hours=1)),
        }
        db.add_all(rows.values())
        await db.commit()
        return {"agent": agent.id, "lead": lead.id, "jane": jane, "col": col.id, "bob_col": someone_else.id,
                "rows": {k: r.id for k, r in rows.items()}}


async def _person(agent_id: str, email: str) -> dict:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, agent_id)
        out = await email_people(db=db, user=user)
    return next(p for p in out["people"] if p["email"] == email)


@pytest.mark.asyncio
async def test_delete_moves_to_not_in_leads_cancels_emails_and_turns_showcase_off():
    s = await _setup()
    before = await _person(s["agent"], s["jane"])
    assert before["lead_id"] == s["lead"] and before["mode"] == "daily"

    async with AsyncSessionLocal() as db:
        res = await delete_lead(lead_id=s["lead"], db=db, current_user=await db.get(User, s["agent"]))
    assert res["success"] is True
    assert res["message"] == "Emails to Jane stopped and their Showcase was turned off."
    assert res["emails_cancelled"] == 2 and res["showcases_off"] == 1

    # 1. Emails page: no longer a lead -> "Not in your leads", automatic emails off and why.
    after = await _person(s["agent"], s["jane"])
    assert after["lead_id"] is None
    assert after["name"] == "Jane Buyer"  # still has the name from their Showcase
    assert after["mode"] == "off" and after["code"] == "lead_deleted"
    assert "deleted their lead" in after["reason"]

    async with AsyncSessionLocal() as db:
        # 2. Waiting emails to them cancelled; sent history, the agent's own email and another agent's untouched.
        st = {k: (await db.get(ScheduledEmail, rid)).status for k, rid in s["rows"].items()}
        assert st == {"invite": "CANCELLED", "digest_queue": "CANCELLED", "already_sent": "SENT", "to_agent": "DIGEST", "other_agent": "PENDING"}
        assert (await db.get(ScheduledEmail, s["rows"]["invite"])).error_message == "Not sent - you deleted their lead"
        # 3. Their Showcase is off but still there (homes, link, everything); someone else's isn't touched.
        col = await db.get(Collection, s["col"])
        assert col is not None and col.status == "INACTIVE" and col.share_token
        assert (await db.get(Collection, s["bob_col"])).status == "ACTIVE"
        # Their history in the lead is gone with the lead, not left pointing at the Showcase.
        assert (await db.execute(select(LeadEvent).where(LeadEvent.collection_id == s["col"]))).first() is None

    # Any automatic email that tries later is held back.
    assert await ce.before_send(s["agent"], s["jane"], "showcase_daily_digest", "New homes", {}) is None


@pytest.mark.asyncio
async def test_turning_showcase_back_on_lets_emails_go_again():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        await delete_lead(lead_id=s["lead"], db=db, current_user=await db.get(User, s["agent"]))
    async with AsyncSessionLocal() as db:
        assert await CollectionsService.update_collection_status(db, s["col"], s["agent"], "ACTIVE")
    p = await _person(s["agent"], s["jane"])
    assert p["mode"] == "daily" and p["manual"] is None
    async with AsyncSessionLocal() as db:
        assert (await db.get(Collection, s["col"])).status == "ACTIVE"


@pytest.mark.asyncio
async def test_becoming_a_lead_again_lifts_the_hold_but_an_agent_off_stays_off():
    s = await _setup()
    async with AsyncSessionLocal() as db:
        await delete_lead(lead_id=s["lead"], db=db, current_user=await db.get(User, s["agent"]))
    async with AsyncSessionLocal() as db:
        lead = await upsert_lead(db, owner_id=s["agent"], name="Jane Buyer", email=s["jane"], phone=None, source_type="form_submission",
                                 source_id=None, summary="Filled a form", occurred_at=datetime.now(timezone.utc), commit=False)
        await db.commit()
        assert lead is not None
    p = await _person(s["agent"], s["jane"])
    assert p["lead_id"] == lead.id and p["mode"] == "daily" and p["manual"] is None

    # The agent's own "Turn off" isn't undone by a delete + return.
    async with AsyncSessionLocal() as db:
        await ce.set_manual(db, s["agent"], s["jane"], on=False)
        await delete_lead(lead_id=lead.id, db=db, current_user=await db.get(User, s["agent"]))
    async with AsyncSessionLocal() as db:
        pref = (await db.execute(select(ClientEmailPref).where(ClientEmailPref.owner_id == s["agent"], ClientEmailPref.email == s["jane"]))).scalar_one()
        assert pref.manual == "off"
        await upsert_lead(db, owner_id=s["agent"], name="Jane Buyer", email=s["jane"], phone=None, source_type="form_submission",
                          source_id=None, summary="Filled a form", occurred_at=datetime.now(timezone.utc), commit=False)
        await db.commit()
    assert (await _person(s["agent"], s["jane"]))["code"] == "manual_off"


@pytest.mark.asyncio
async def test_lead_without_email_or_showcase():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "agent")
        lead = Lead(owner_id=agent.id, name="phil phone", phone="6105550101", represented="no", status="new", tags=[])
        db.add(lead)
        await db.commit()
        res = await delete_lead(lead_id=lead.id, db=db, current_user=agent)
    assert res["message"] == "Phil was deleted." and res["emails_cancelled"] == 0 and res["showcases_off"] == 0


def test_messages():
    from app.services.lead_cleanup_service import deleted_message
    assert deleted_message("Jane Buyer", {"emails_stopped": True, "showcases_off": 2}) == "Emails to Jane stopped and their 2 Showcases were turned off."
    assert deleted_message("Jane", {"emails_stopped": True, "showcases_off": 0}) == "Emails to Jane stopped."
    assert deleted_message(None, {"emails_stopped": False, "showcases_off": 1}) == "This lead was deleted and their Showcase was turned off."
