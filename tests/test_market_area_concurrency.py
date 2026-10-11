"""Real-database concurrency proof for MarketArea canonical deduplication (see the audit +
canonical_key migration this implements). Runs directly against the actual local Postgres
instance via AsyncSessionLocal - a mocked/fake DB session cannot prove anything about real
Postgres unique-index/ON CONFLICT race behavior, which is the actual thing being verified here.

Every test either reuses real, already-ENRICHED rows (Villanova, Rittenhouse Square) to prove
zero-cost reuse, or creates disposable rows/users clearly prefixed with TEST_PREFIX so they can
never collide with real production data and are fully cleaned up afterward (see the autouse
cleanup fixture). No test ever lets a BackgroundTasks-enqueued research job actually run (that
would spend real AI/Places money) - add_my_served_area is called directly as a plain coroutine,
so background_tasks.add_task(...) only appends to a list; nothing executes unless something later
calls `await background_tasks()`, which none of these tests ever do.
"""
import asyncio
import uuid

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal
from app.api import site_routes
from app.models.database import AgentMarketArea, MarketArea, User
from app.schemas.areas import AgentMarketAreaCreate
from app.services.areas import area_enrichment_service
from app.services.areas.canonical_key import compute_canonical_key

TEST_PREFIX = "Zqtest"


@pytest.fixture(scope="session")
def event_loop():
    """The app's async engine (app.database.engine) is a module-level singleton whose pooled
    asyncpg connections are bound to whichever event loop first used them. pytest-asyncio's
    default is a fresh event loop per test function, which corrupts those pooled connections the
    moment a second test tries to reuse one on a different loop ("another operation is in
    progress" / InterfaceError). Sharing one event loop for this whole test module keeps every
    test on the same loop the engine's connections were created on."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def synthetic_name(label: str) -> str:
    return f"{TEST_PREFIX} {label} {uuid.uuid4().hex[:8]}"


async def make_test_user(db) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        email=f"{TEST_PREFIX.lower()}-{suffix}@example.com",
        hashed_password="not-a-real-hash",
        first_name="Test",
        last_name="Agent",
        state="PA",
        brokerage="Test Brokerage",
        mls_id=f"{TEST_PREFIX}-{suffix}",
        plan_tier="PREMIUM",
        subscription_status="ACTIVE",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture(autouse=True)
async def cleanup_test_rows():
    yield
    async with AsyncSessionLocal() as db:
        test_area_ids = (await db.execute(
            select(MarketArea.id).where(MarketArea.name.like(f"{TEST_PREFIX}%"))
        )).scalars().all()
        test_user_ids = (await db.execute(
            select(User.id).where(User.email.like(f"{TEST_PREFIX.lower()}-%"))
        )).scalars().all()
        if test_area_ids:
            await db.execute(delete(AgentMarketArea).where(AgentMarketArea.area_id.in_(test_area_ids)))
        if test_user_ids:
            await db.execute(delete(AgentMarketArea).where(AgentMarketArea.owner_id.in_(test_user_ids)))
        if test_area_ids:
            await db.execute(delete(MarketArea).where(MarketArea.id.in_(test_area_ids)))
        if test_user_ids:
            await db.execute(delete(User).where(User.id.in_(test_user_ids)))
        await db.commit()


# ---------------------------------------------------------------------------
# A. Two simultaneous requests for the same already-existing town (Villanova)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_two_simultaneous_requests_for_villanova_return_same_row():
    async def call():
        async with AsyncSessionLocal() as db:
            return await area_enrichment_service.get_or_create_market_area(db, "Villanova", "PA")

    first, second = await asyncio.gather(call(), call())
    assert first.id == second.id


# ---------------------------------------------------------------------------
# B. Simultaneous "Villanova" and "Villanova PA"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b_simultaneous_villanova_spelling_variants_return_same_row():
    async def call(name):
        async with AsyncSessionLocal() as db:
            return await area_enrichment_service.get_or_create_market_area(db, name, "PA")

    first, second = await asyncio.gather(call("Villanova"), call("Villanova PA"))
    assert first.id == second.id


# ---------------------------------------------------------------------------
# C. Two different agents simultaneously adding the same NEW geography
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c_two_agents_simultaneous_new_geography_one_row_one_research_job():
    town = synthetic_name("Newtown")
    async with AsyncSessionLocal() as setup_db:
        user_a = await make_test_user(setup_db)
        user_b = await make_test_user(setup_db)

    bg_a, bg_b = BackgroundTasks(), BackgroundTasks()

    async def call(user, bg):
        async with AsyncSessionLocal() as db:
            payload = AgentMarketAreaCreate(name=town, state="PA", area_type="city")
            return await site_routes.add_my_served_area(payload, bg, db, user)

    link_a, link_b = await asyncio.gather(call(user_a, bg_a), call(user_b, bg_b))

    assert link_a.area.id == link_b.area.id
    total_research_jobs = len(bg_a.tasks) + len(bg_b.tasks)
    assert total_research_jobs == 1, f"expected exactly one research job for a genuinely new area, got {total_research_jobs}"

    async with AsyncSessionLocal() as db:
        row_count = (await db.execute(
            select(MarketArea.id).where(MarketArea.name == town, MarketArea.state == "PA")
        )).scalars().all()
        assert len(row_count) == 1


# ---------------------------------------------------------------------------
# D. Villanova, Villanova PA, Villanova, Pennsylvania sequentially -> same ID every time
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_d_villanova_variants_sequential_same_id():
    ids = []
    for name in ["Villanova", "Villanova PA", "Villanova, Pennsylvania"]:
        async with AsyncSessionLocal() as db:
            area = await area_enrichment_service.get_or_create_market_area(db, name, "PA")
            ids.append(area.id)
    assert len(set(ids)) == 1


# ---------------------------------------------------------------------------
# E. Rittenhouse Square spelling variants under Philadelphia -> same MarketArea
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_e_rittenhouse_square_spelling_variants_same_row():
    ids = []
    for name, parent in [
        ("Rittenhouse Square", "Philadelphia"),
        ("rittenhouse square", "Philadelphia, PA"),
        ("Rittenhouse Square", "philadelphia"),
    ]:
        async with AsyncSessionLocal() as db:
            area = await area_enrichment_service.get_or_create_market_area(
                db, name, "PA", area_type="neighborhood", parent_area_name=parent,
            )
            ids.append(area.id)
    assert len(set(ids)) == 1


# ---------------------------------------------------------------------------
# F. Identically-named neighborhoods under genuinely different parent cities -> different IDs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_f_same_neighborhood_name_different_parents_stay_distinct():
    shared_name = synthetic_name("Oak Grove")
    parent_a = f"{TEST_PREFIX} Springfield {uuid.uuid4().hex[:6]}"
    parent_b = f"{TEST_PREFIX} Mediaville {uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db_a:
        area_a = await area_enrichment_service.get_or_create_market_area(
            db_a, shared_name, "PA", area_type="neighborhood", parent_area_name=parent_a,
        )
    async with AsyncSessionLocal() as db_b:
        area_b = await area_enrichment_service.get_or_create_market_area(
            db_b, shared_name, "PA", area_type="neighborhood", parent_area_name=parent_b,
        )

    assert area_a.id != area_b.id
    # cleanup fixture matches on MarketArea.name, and both rows share `shared_name` - covered.


# ---------------------------------------------------------------------------
# G. Existing ENRICHED, current-version geography added by another agent -> zero research
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_g_existing_enriched_current_version_area_triggers_no_research():
    async with AsyncSessionLocal() as setup_db:
        user = await make_test_user(setup_db)

    bg = BackgroundTasks()
    async with AsyncSessionLocal() as db:
        payload = AgentMarketAreaCreate(name="Villanova", state="PA", area_type="city")
        link = await site_routes.add_my_served_area(payload, bg, db, user)

    assert link.area.name == "Villanova"
    assert link.area.enrichment_status == "ENRICHED"
    assert len(bg.tasks) == 0


# ---------------------------------------------------------------------------
# H. Existing ENRICHED area with a STALE research_version, selected normally -> reused, no
#    automatic research spend merely from selection (only explicit refresh should do that).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_h_existing_enriched_stale_version_area_not_auto_reresearched_on_normal_selection():
    stale_name = synthetic_name("Staleville")
    async with AsyncSessionLocal() as setup_db:
        canonical_key = compute_canonical_key(stale_name, "PA", "city")
        stale_area = MarketArea(
            name=stale_name, state="PA", slug=f"zqtest-{uuid.uuid4().hex[:10]}-pa",
            area_type="city", canonical_key=canonical_key,
            enrichment_status="ENRICHED", research_version=1,  # deliberately old
            facts={"parks": {"items": ["dummy"]}},
        )
        setup_db.add(stale_area)
        await setup_db.commit()
        await setup_db.refresh(stale_area)
        stale_id = stale_area.id

        user = await make_test_user(setup_db)

    bg = BackgroundTasks()
    async with AsyncSessionLocal() as db:
        payload = AgentMarketAreaCreate(name=stale_name, state="PA", area_type="city")
        link = await site_routes.add_my_served_area(payload, bg, db, user)

    assert link.area.id == stale_id
    assert len(bg.tasks) == 0, "normal selection must never auto-trigger research merely for a stale research_version"

    async with AsyncSessionLocal() as db:
        refreshed = (await db.execute(select(MarketArea).where(MarketArea.id == stale_id))).scalar_one()
        assert refreshed.enrichment_status == "ENRICHED"
        assert refreshed.research_version == 1
