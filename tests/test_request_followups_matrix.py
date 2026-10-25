"""Every concrete ask ALWAYS makes a same-day follow-up - also from someone who has an agent - and
it says what they asked for and where (user, 10/25: a has-agent guest asked for the seller
disclosure on her Guest Pass; the agent got the alert but no follow-up).

Matrix: request type x has-agent yes/no x source. Runs against the local dev Postgres with
throwaway agents (zqreq-*@example.com), all removed afterward. Agent emails are stubbed out; the
calendar push is a no-op (no calendar connected)."""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.main  # noqa: F401
from app.database import AsyncSessionLocal
from app.models.database import AgentTask, Lead, LeadEvent, OpenHouseEvent, OpenHouseFormRequest, Notification, User
from app.services import tasks_service
from app.services.leads_service import upsert_lead

PREFIX = "zqreq"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def no_agent_emails(monkeypatch):
    import app.services.agent_email_service as aes
    monkeypatch.setattr(aes, "email_agent", lambda *a, **k: None)


def _email(label: str) -> str:
    return f"{PREFIX}-{label}-{uuid.uuid4().hex[:8]}@example.com"


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        if users:
            ohs = (await db.execute(select(OpenHouseEvent.id).where(OpenHouseEvent.agent_id.in_(users)))).scalars().all()
            await db.execute(delete(AgentTask).where(AgentTask.owner_id.in_(users)))
            await db.execute(delete(Notification).where(Notification.agent_id.in_(users)))
            if ohs:
                await db.execute(delete(OpenHouseFormRequest).where(OpenHouseFormRequest.open_house_event_id.in_(ohs)))
                await db.execute(delete(OpenHouseEvent).where(OpenHouseEvent.id.in_(ohs)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


async def _agent(db) -> User:
    u = User(email=_email("agent"), hashed_password="x", first_name="Test", last_name="Agent", state="PA",
             brokerage="Test Brokerage", mls_id=f"ZQREQ-{uuid.uuid4().hex[:8]}", plan_tier="IDX", subscription_status="ACTIVE")
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _person(db, owner_id: str, has_agent: bool) -> str:
    email = _email("guest")
    db.add(Lead(owner_id=owner_id, name="Jenna Guest", email=email, phone="6105550199",
                represented="yes" if has_agent else "no", status="new", tags=[]))
    await db.commit()
    return email


async def _settle():
    """Let the background follow-up builds finish."""
    for _ in range(50):
        pending = [t for t in list(tasks_service._PENDING) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def _task_for(owner_id: str, email: str):
    async with AsyncSessionLocal() as db:
        lead = (await db.execute(select(Lead).where(Lead.owner_id == owner_id, Lead.email == email))).scalar_one()
        return (await db.execute(select(AgentTask).where(AgentTask.lead_id == lead.id).order_by(AgentTask.created_at.desc()))).scalars().all()


def _same_day(user, rule, task) -> bool:
    s = tasks_service.rule_settings(user, rule)
    start, end = tasks_service.followup_window(s["when"], datetime.now(timezone.utc))
    return s["when"] != "next_day" and task.due_at is not None and start <= task.due_at <= end


# Open house requests: disclosure / second visit x Guest Pass / Cheat Sheet QR / invite.
@pytest.mark.asyncio
@pytest.mark.parametrize("has_agent", [True, False])
@pytest.mark.parametrize("kind,rule", [("disclosure", "disclosure_request"), ("second_visit", "second_visit_request")])
@pytest.mark.parametrize("source,where", [("guest_pass", "on their Guest Pass"), (None, "from your Cheat Sheet QR"), ("invite", "from your invite")])
async def test_open_house_requests_always_make_a_followup(has_agent, kind, rule, source, where):
    from app.api.open_houses_routes import submit_open_house_form_request
    from app.schemas.open_house import OpenHouseFormRequestCreate, OpenHouseFormRequestType
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        oh = OpenHouseEvent(agent_id=agent.id, qr_code=f"zqreq-{uuid.uuid4().hex}", address="12 Test Ln, Exton, PA 19341")
        db.add(oh)
        await db.commit()
        email = await _person(db, agent.id, has_agent)
        await submit_open_house_form_request(oh.id, OpenHouseFormRequestCreate(
            form_type=OpenHouseFormRequestType(kind), full_name="Jenna Guest", email=email, phone="6105550199",
            preferred_visit_time="Sat, Oct 31 - Afternoon" if kind == "second_visit" else None, source=source), db)
        user = await db.get(User, agent.id)
    await _settle()
    tasks = await _task_for(agent.id, email)
    assert tasks, f"no follow-up for {kind} via {source} (has agent: {has_agent})"
    t = next(t for t in tasks if t.auto_source == rule)
    assert where in (t.context or ""), t.context
    assert _same_day(user, rule, t), (t.due_at, tasks_service.rule_settings(user, rule))
    async with AsyncSessionLocal() as db:
        note = (await db.execute(select(Notification).where(Notification.agent_id == agent.id))).scalars().first()
    assert note is not None and where in (note.message or "")


# Every other kind of direct ask, from each place it can come from.
@pytest.mark.asyncio
@pytest.mark.parametrize("has_agent", [True, False])
@pytest.mark.parametrize("source_type,rule,summary,snap", [
    ("property_tour", "showing_request", "Requested a tour of 5 Oak St from their Showcase", {}),
    ("property_website_tour_request", "showing_request", "Requested a showing of 5 Oak St on its Spotlight site", {}),
    ("property_page_tour_request", "showing_request", "Requested a showing of 5 Oak St on your website", {}),
    ("property_website_inquiry", "listing_inquiry", "Asked about 5 Oak St on its Spotlight site", {"message": "Is the basement finished?"}),
    ("property_page_inquiry", "listing_inquiry", "Asked about 5 Oak St on its listing page", {"message": "HOA fee?", "src": "pass"}),
    ("portal_lead", "listing_inquiry", "Zillow lead - 5 Oak St", {"message": "Still available?"}),
    ("blog_inquiry", "form_submission", "Asked a question on your blog", {"message": "What about schools?"}),
])
async def test_other_direct_asks_always_make_a_followup(has_agent, source_type, rule, summary, snap):
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        email = await _person(db, agent.id, has_agent)
        await upsert_lead(db, owner_id=agent.id, name="Jenna Guest", email=email, phone="6105550199", source_type=source_type,
                          source_id=str(uuid.uuid4()), summary=summary, occurred_at=datetime.now(timezone.utc),
                          property_address="5 Oak St, Exton, PA 19341", raw_snapshot=snap or None)
        user = await db.get(User, agent.id)
    await _settle()
    tasks = await _task_for(agent.id, email)
    assert tasks, f"no follow-up for {source_type} (has agent: {has_agent})"
    t = tasks[0]
    assert _same_day(user, t.auto_source if t.auto_source in tasks_service._RULE_KEYS else rule, t)


@pytest.mark.asyncio
async def test_has_agent_guest_still_gets_no_nurture():
    """Only direct asks override "they have an agent" - signing up for a home's alerts doesn't."""
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        email = await _person(db, agent.id, True)
        await upsert_lead(db, owner_id=agent.id, name="Jenna Guest", email=email, phone="6105550199", source_type="property_website_inquiry",
                          source_id=str(uuid.uuid4()), summary="Signed up for price alerts on 5 Oak St",
                          occurred_at=datetime.now(timezone.utc), raw_snapshot={"alerts": True})
    await _settle()
    assert await _task_for(agent.id, email) == []


def test_explicit_request_rules():
    for rule in ("disclosure_request", "second_visit_request", "showing_request", "listing_inquiry", "showcase_search_request"):
        assert tasks_service.is_explicit_request(rule)
    for rule in ("open_house_signin", "similar_homes_qr", "form_submission", "invite_rsvp", "website_signin", "showcase_comment"):
        assert not tasks_service.is_explicit_request(rule)
    assert tasks_service.is_explicit_request("form_submission", "blog_inquiry")
    assert not tasks_service.is_explicit_request("listing_inquiry", "property_website_inquiry", alerts_signup=True)


@pytest.mark.asyncio
async def test_a_crashing_build_still_leaves_a_followup(monkeypatch):
    """Safety net: if building the follow-up blows up, a plain "needs your attention" one is made."""
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        email = await _person(db, agent.id, False)

    async def boom(*a, **k):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(tasks_service, "followup_property", boom)
    async with AsyncSessionLocal() as db:
        await upsert_lead(db, owner_id=agent.id, name="Jenna Guest", email=email, phone="6105550199", source_type="property_page_tour_request",
                          source_id=str(uuid.uuid4()), summary="Requested a showing of 5 Oak St on your website",
                          occurred_at=datetime.now(timezone.utc), property_address="5 Oak St, Exton, PA 19341")
    await _settle()
    tasks = await _task_for(agent.id, email)
    assert tasks and tasks[0].title.startswith("Needs your attention"), [t.title for t in tasks]
