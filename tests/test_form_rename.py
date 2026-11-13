"""Naming a new custom form: the heading visitors see and an unused /untitled-form link follow the
new name; a heading the agent wrote, or a link already in use, never changes (launch audit 11/13).

Runs against the local dev Postgres with throwaway agents (zqformname-*@example.com), removed after."""
import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.api import forms_routes as routes
from app.database import AsyncSessionLocal
from app.models.database import AgentForm, FormSubmission, User
from app.schemas.forms import AgentFormCreate, AgentFormUpdate

PREFIX = "zqformname"


def _fresh_pool():
    from app.database import engine
    engine.sync_engine.pool = engine.sync_engine.pool.recreate()


@pytest.fixture(scope="module")
def event_loop():
    _fresh_pool()
    loop = asyncio.new_event_loop()
    yield loop
    _fresh_pool()
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


async def _agent(db) -> User:
    u = User(email=f"{PREFIX}-{uuid.uuid4().hex[:8]}@example.com", hashed_password="not-a-real-hash", first_name="Test",
             last_name="Agent", state="PA", brokerage="Test Brokerage", mls_id=f"ZQFN-{uuid.uuid4().hex[:8]}",
             plan_tier="BASIC", subscription_status="ACTIVE")
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def _save(form: AgentForm, **changes) -> AgentFormUpdate:
    """What the builder sends: the whole form, with the agent's edits."""
    body = {"title": form.title, "headline": form.headline, "subtitle": form.subtitle, **changes}
    return AgentFormUpdate(**body)


@pytest.mark.asyncio
async def test_naming_a_new_form_moves_heading_and_unused_link():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        form = await routes.create_form(AgentFormCreate(title=""), db, agent)
        assert (form.title, form.headline, form.slug) == ("Untitled Form", "Untitled Form", "untitled-form")
        form = await routes.update_form(form.id, _save(form, title="Spring Café Survey"), db, agent)
        assert form.headline == "Spring Café Survey"
        assert form.slug == "spring-caf-survey"
        # a second untitled form with the same name gets its own link
        other = await routes.create_form(AgentFormCreate(title=""), db, agent)
        assert other.slug == "untitled-form"
        other = await routes.update_form(other.id, _save(other, title="Spring Café Survey"), db, agent)
        assert other.slug == "spring-caf-survey-2"


@pytest.mark.asyncio
async def test_written_heading_and_used_link_stay():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        form = await routes.create_form(AgentFormCreate(title=""), db, agent)
        # agent writes their own heading in the same save -> kept
        form = await routes.update_form(form.id, _save(form, title="Open House Feedback", headline="Tell me what you thought"), db, agent)
        assert form.headline == "Tell me what you thought"
        # renaming again later leaves that heading alone
        form = await routes.update_form(form.id, _save(form, title="Feedback v2"), db, agent)
        assert form.headline == "Tell me what you thought"

        used = await routes.create_form(AgentFormCreate(title=""), db, agent)
        db.add(FormSubmission(form_id=used.id, owner_id=agent.id, form_type="custom", answers=[]))
        await db.commit()
        first_link = used.slug
        used = await routes.update_form(used.id, _save(used, title="Buyer Intake"), db, agent)
        assert used.slug == first_link and first_link.startswith("untitled-form")  # already used - never moves
        assert used.headline == "Buyer Intake"


@pytest.mark.asyncio
async def test_named_form_link_never_moves():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db)
        form = await routes.create_form(AgentFormCreate(title="Home Tour Sign Up"), db, agent)
        assert form.slug == "home-tour-sign-up"
        form = await routes.update_form(form.id, _save(form, title="Tour Sign Up"), db, agent)
        assert form.slug == "home-tour-sign-up"
        assert form.headline == "Tour Sign Up"     # heading was never customised, so it follows
