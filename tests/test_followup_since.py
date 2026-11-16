"""Owner, 11/16: when a person does something new while a follow-up for them is already waiting, that
follow-up - on the card AND its calendar event - says so ("New since this was added"). Plus: the same
follow-up added twice in a row (double tap / two devices) is one follow-up."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import tasks_service as ts

NOW = datetime.now(timezone.utc)


async def _setup(db, tag):
    from app.models.database import Lead, User
    agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Test", last_name="Agent", state="PA",
                 brokerage="Test Brokerage", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
    db.add(agent)
    await db.commit()
    lead = Lead(owner_id=agent.id, name="Sally Since", email=f"{tag}-guest@example.com", phone="6105550123",
                represented="no", status="new", tags=[])
    db.add(lead)
    await db.commit()
    return agent, lead


def _task(agent, lead, *, source="open_house_signin", made=-60, done=False, **kw):
    from app.models.database import AgentTask
    return AgentTask(owner_id=agent.id, lead_id=lead.id, title="Follow up with Sally", kind="call", priority="high",
                     due_at=NOW + timedelta(days=1), remind_at=NOW + timedelta(days=1), all_day=True,
                     auto_source=source, auto_source_id=str(uuid.uuid4()) if source else None,
                     created_at=NOW + timedelta(minutes=made), completed_at=NOW if done else None, **kw)


def _event(lead, source_type, summary, **kw):
    from app.models.database import LeadEvent
    return LeadEvent(lead_id=lead.id, source_type=source_type, summary=summary, occurred_at=kw.pop("at", NOW), **kw)


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


async def _drain():
    for _ in range(20):
        if not ts._PENDING:
            return
        await asyncio.gather(*list(ts._PENDING), return_exceptions=True)


@pytest.mark.asyncio
async def test_activity_lands_on_open_followups_made_before_it():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            open_auto = _task(agent, lead)
            manual = _task(agent, lead, source=None)
            done = _task(agent, lead, done=True)
            later = _task(agent, lead, made=+5)       # made after it happened - it already knew
            db.add_all([open_auto, manual, done, later])
            ev = _event(lead, "form_submission", "Asked about 26 OLD GULPH RD, GLADWYNE",
                        raw_snapshot={"answers": [{"questionId": "buy_working_with_agent", "value": "Yes"}]})
            db.add(ev)
            await db.commit()
            changed = await ts.note_activity_on_open_followups(db, agent.id, lead.id, ev.id)
            assert set(changed) == {open_auto.id, manual.id}
            await db.refresh(open_auto)
            await db.refresh(done)
            lines = ts.since_lines(open_auto.notes)
            assert len(lines) == 1 and lines[0].startswith("Asked about 26 Old Gulph Rd, Gladwyne") and "(said they have an agent)" in lines[0]
            assert not ts.since_lines(done.notes)
            # Once only, however many times it's run
            assert await ts.note_activity_on_open_followups(db, agent.id, lead.id, ev.id) == []
            # Keeps the newest five
            for i in range(7):
                e = _event(lead, "invite_rsvp", f"RSVP'd Coming - Party {i}")
                db.add(e)
                await db.commit()
                await ts.note_activity_on_open_followups(db, agent.id, lead.id, e.id)
            await db.refresh(open_auto)
            lines = ts.since_lines(open_auto.notes)
            assert len(lines) == ts.SINCE_MAX and lines[-1].startswith("RSVP'd Coming - Party 6")
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_activity_with_its_own_followup_is_not_repeated():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            open_auto = _task(agent, lead)
            db.add(open_auto)
            ev = _event(lead, "property_tour", "Asked for a showing - 1 Main St")
            db.add(ev)
            await db.commit()
            own = _task(agent, lead, source="showing_request", made=+1)
            own.auto_source_id = ev.id
            db.add(own)
            await db.commit()
            assert await ts.note_activity_on_open_followups(db, agent.id, lead.id, ev.id) == []
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_a_blocked_form_fill_reaches_the_open_card_end_to_end():
    """The real path: a form while their sign-in follow-up is open makes no second card (one open per
    person) - and the open card now says they did it."""
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask
    from sqlalchemy import select
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            card = _task(agent, lead)
            db.add(card)
            ev = _event(lead, "form_submission", "Submitted Buyer Questions form")
            db.add(ev)
            await db.commit()
            agent_id, lead_id, ev_id, card_id = agent.id, lead.id, ev.id, card.id
        ts.schedule_followup_for_lead_event(agent_id, lead_id, ev_id, "form_submission")
        await _drain()
        async with AsyncSessionLocal() as db:
            tasks = (await db.execute(select(AgentTask).where(AgentTask.owner_id == agent_id))).scalars().all()
            assert [t.id for t in tasks] == [card_id]          # still one follow-up
            assert ts.since_lines(tasks[0].notes)[0].startswith("Submitted Buyer Questions form")
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_showcase_emails_off_reaches_the_open_card():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask
    from app.services.leads_service import log_activity
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            card = _task(agent, lead)
            db.add(card)
            await db.commit()
            await log_activity(db, owner_id=agent.id, email=lead.email, lead=lead, source_type="showcase_emails_off",
                               summary="Turned off their Showcase emails")
            card_id = card.id
        await _drain()
        async with AsyncSessionLocal() as db:
            t = await db.get(AgentTask, card_id)
            assert ts.since_lines(t.notes)[0].startswith("Turned off their Showcase emails")
    finally:
        await _cleanup(tag)


def test_calendar_event_has_its_own_section():
    from types import SimpleNamespace
    from app.services.calendar_sync_service import _task_event_body
    t = SimpleNamespace(id="x", title="Follow up with Sally", context="Signed in at your open house", completed_at=None,
                        due_at=NOW + timedelta(days=1), all_day=True, auto_source="open_house_signin", auto_source_id="v",
                        kind="call", created_at=NOW, suggested_message=None,
                        notes="Auto-created from an open house sign-in.\nYour own line\nSince then: Submitted Buyer form · Sun, Nov 16")
    desc = _task_event_body(t, done_link="https://openhousepal.com/d/abc")["description"]
    assert "NEW SINCE THIS WAS ADDED\n• Submitted Buyer form · Sun, Nov 16" in desc
    assert desc.count("Submitted Buyer form") == 1 and "Your own line" in desc
    t.completed_at = NOW
    assert "NEW SINCE" not in _task_event_body(t, done_link="https://openhousepal.com/d/abc")["description"]


@pytest.mark.asyncio
async def test_same_followup_added_twice_is_one():
    import app.main  # noqa: F401
    from app.api.task_routes import create_task, quick_add_checkin, CheckinAddBody
    from app.database import AsyncSessionLocal
    from app.schemas.tasks import TaskCreate
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            body = TaskCreate(title="Text Sally about 1 Main St - they liked it", kind="text", lead_id=lead.id,
                              due_at=NOW + timedelta(days=1), all_day=True)
            a = await create_task(body, db, agent)
            b = await create_task(body, db, agent)
            assert a.id == b.id
            c = await quick_add_checkin(lead.id, CheckinAddBody(reason="Opened your email"), db, agent)
            d = await quick_add_checkin(lead.id, CheckinAddBody(reason="Opened your email"), db, agent)
            assert c.id == d.id and c.id != a.id
            # a different follow-up for them is still its own
            e = await create_task(TaskCreate(title="Call Sally", kind="call", lead_id=lead.id), db, agent)
            assert e.id not in (a.id, c.id)
        await _drain()
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_open_house_signin_reaches_the_open_followup():
    """11/16: signing in at an open house while another follow-up (a form) is open used to leave no
    trace on it. Now it's a "Since then" line. And with their open-house card AND a tour request
    open, the visit goes onto the open-house card - never the tour card."""
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            form_card = _task(agent, lead, source="form_submission", context="Submitted Buyer form")
            db.add(form_card)
            await db.commit()
            made = await ts.create_open_house_followup(db, owner_id=agent.id, lead=lead, signin_id=None,
                                                       property_address="239 WINDHAM DR, BERWYN, PA",
                                                       occurred_at=NOW, commit=True)
            assert made is None   # one open follow-up per person - the form card covers reaching out
            await db.refresh(form_card)
            assert ts.since_lines(form_card.notes) and ts.since_lines(form_card.notes)[0].startswith("Signed in at your open house - 239 Windham Dr")
            assert form_card.suggested_message is None   # the message is never touched

            form_card.completed_at = NOW
            signin_card = _task(agent, lead, made=-120, context="Signed in at your open house - 1650 Skinners Turn Rd")
            tour_card = _task(agent, lead, source="showing_request", made=-30, context="Asked to tour 1650 Skinners Turn Rd")
            db.add_all([signin_card, tour_card])
            await db.commit()
            await ts.create_open_house_followup(db, owner_id=agent.id, lead=lead, signin_id=None,
                                                property_address="12 OAK ST, WAYNE, PA", occurred_at=NOW, commit=True)
            await db.refresh(signin_card)
            await db.refresh(tour_card)
            assert "12 Oak St" in (signin_card.context or "")
            assert "Also signed in at: 12 Oak St" in (signin_card.notes or "")   # tidy case on the calendar
            assert tour_card.context == "Asked to tour 1650 Skinners Turn Rd" and not ts.since_lines(tour_card.notes)
        await _drain()
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_search_change_says_what_changed():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    tag = f"zqsince-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent, lead = await _setup(db, tag)
            card = _task(agent, lead)
            db.add(card)
            ev = _event(lead, "showcase_search_changed", "Changed their Showcase search",
                        raw_snapshot={"answers": [{"label": "Price", "value": "Up to $1,200,000"}, {"label": "Bedrooms", "value": "4+"}]})
            db.add(ev)
            await db.commit()
            await ts.note_activity_on_open_followups(db, agent.id, lead.id, ev.id)
            await db.refresh(card)
            assert ts.since_lines(card.notes)[0].startswith("Changed their Showcase search - Price up to $1,200,000; Bedrooms 4+ · ")
    finally:
        await _cleanup(tag)


def test_short_person_drops_bracketed_words():
    assert ts.short_person("Olivia Story (Test)") == "Olivia S."
    assert ts.short_person("Jane Buyer") == "Jane B."


def test_search_change_wording_has_no_zero_minimum():
    from app.api.collections_routes import _describe_search_change
    rows = _describe_search_change({"min_price": 0, "max_price": 1200000})
    assert rows[0] == {"label": "Price", "value": "Up to $1,200,000"}
