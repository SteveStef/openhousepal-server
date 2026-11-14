"""Owner, 11/14: a follow-up the agent missed (overdue) must never stop a new one. Something new the person
does makes a fresh follow-up; activity that folds onto their open card (a second message, new likes, another
open house sign-in) moves that card to a fresh day with a fresh reminder instead of landing in the past."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import tasks_service as ts

NOW = datetime.now(timezone.utc)


def test_is_overdue():
    t = lambda days, **kw: SimpleNamespace(completed_at=None, due_at=NOW + timedelta(days=days), all_day=True, auto_source="open_house_signin", **kw)
    assert ts.is_overdue(t(-3)) is True
    assert ts.is_overdue(t(1)) is False
    assert ts.is_overdue(SimpleNamespace(completed_at=NOW, due_at=NOW - timedelta(days=3), all_day=True, auto_source="x")) is False


async def _setup(db, tag):
    from app.models.database import Lead, User
    agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Test", last_name="Agent", state="PA",
                 brokerage="Test Brokerage", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
    db.add(agent)
    await db.commit()
    lead = Lead(owner_id=agent.id, name="Olive Overdue", email=f"{tag}-guest@example.com", phone="6105550199",
                represented="no", status="new", tags=[])
    db.add(lead)
    await db.commit()
    return agent, lead


def _task(agent, lead, source, days, **kw):
    from app.models.database import AgentTask
    return AgentTask(owner_id=agent.id, lead_id=lead.id, title="Follow up with Olive", kind="call", priority="high",
                     due_at=NOW + timedelta(days=days), remind_at=NOW + timedelta(days=days), all_day=True,
                     auto_source=source, auto_source_id=str(uuid.uuid4()), reminder_sent_at=NOW - timedelta(days=4), **kw)


async def _cleanup(tag):
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask, Lead, LeadEvent, User
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
        if ids:
            await db.execute(delete(AgentTask).where(AgentTask.owner_id.in_(ids)))
            lids = (await db.execute(select(Lead.id).where(Lead.owner_id.in_(ids)))).scalars().all()
            if lids:
                await db.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(lids)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(ids)))
            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.commit()


@pytest.mark.asyncio
async def test_overdue_followup_does_not_block_a_new_one():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    tag = f"zqover-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            missed = _task(agent, lead, "open_house_signin", -5)
            db.add(missed)
            await db.commit()
            assert await ts._open_auto_task_for_lead(db, agent.id, lead.id) is False
            made = await ts.create_auto_followup(
                db, owner_id=agent.id, rule="form_submission", source_id=str(uuid.uuid4()), title="Get back to Olive about their form",
                context="Filled out your form", notes=None, message="Hi Olive!", occurred_at=NOW, lead_id=lead.id)
            assert made is not None and made.id != missed.id and not ts.is_overdue(made)
            # ...while one that's still due today / upcoming keeps covering them (one reminder, not two)
            again = await ts.create_auto_followup(
                db, owner_id=agent.id, rule="invite_rsvp", source_id=str(uuid.uuid4()), title="Thank Olive for the RSVP",
                context="RSVPed", notes=None, message="Hi Olive!", occurred_at=NOW, lead_id=lead.id)
            assert again is None
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_new_message_on_a_missed_card_gets_a_fresh_day():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    tag = f"zqover-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            missed = _task(agent, lead, "listing_inquiry", -6)
            db.add(missed)
            await db.commit()
            out = await ts.create_auto_followup(
                db, owner_id=agent.id, rule="listing_inquiry", source_id=str(uuid.uuid4()), title="Answer Olive's new question",
                context="Asked again", notes=None, message="Hi Olive!", occurred_at=NOW, lead_id=lead.id, needs_reply=True)
            assert out is not None and out.id == missed.id            # still one card for the same kind of ask ...
            await db.refresh(out)
            assert not ts.is_overdue(out) and out.reminder_sent_at is None   # ... moved to a fresh day, reminder re-armed
            assert out.title == "Answer Olive's new question"
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_signin_at_another_open_house_moves_a_missed_card_forward():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    tag = f"zqover-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            missed = _task(agent, lead, "open_house_signin", -8, context="Signed in at your open house - 1 Old Rd")
            other = _task(agent, lead, "form_submission", -9)
            db.add_all([missed, other])
            await db.commit()
            made = await ts.create_open_house_followup(db, owner_id=agent.id, lead=lead, signin_id=None,
                                                       property_address="22 New Ln, Wayne, PA", occurred_at=NOW, commit=True)
            assert made is None                                        # no second sign-in card ...
            await db.refresh(missed)
            assert "22 New Ln" in (missed.context or "") and not ts.is_overdue(missed)   # ... the missed one moved + re-dated
    finally:
        await _cleanup(tag)
