"""An agent's own domain while their IDX website is off (owner 11/18): it advertises THEM, never
OpenHousePal. public_site_routes.get_hub_only_domain answers the website with one of
  {"hub_slug": ...}  show the Hub,  {"forward_to": ...}  "Turn my domain off completely",
  {"dead": true}     no live Hub (plain 404),  {"hub_slug": null}  not a hub-only domain.
Real queries on the local database in one rolled-back transaction (the first IDX agent with a
published site and a Hub that's live)."""
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.api.public_site_routes import get_hub_only_domain
from app.models.database import AgentHub, Site, SiteDomain, User
from app.services.website import domain_health_service

HOST = "www.hub-mode-test.example"


@pytest_asyncio.fixture
async def db():
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        session.commit = session.flush
        yield session
        await session.rollback()


async def _agent(db):
    from fastapi import HTTPException
    from app.api.agent_hub_routes import _resolve_public_hub
    rows = (await db.execute(
        select(User, Site, AgentHub).join(Site, Site.owner_id == User.id).join(AgentHub, AgentHub.owner_id == User.id)
        .where(User.plan_tier == "IDX", Site.status == "PUBLISHED"))).all()
    for user, site, hub in rows:
        try:
            await _resolve_public_hub(hub.slug, db)
        except HTTPException:
            continue
        return user, site, hub
    pytest.skip("no IDX agent with a live Hub on this database")


async def _domain(db, site, enabled=False, forwards=False):
    site.is_enabled = enabled
    site.domain_forwards_when_off = forwards
    db.add(SiteDomain(site_id=site.id, owner_id=site.owner_id, hostname=HOST, status="ACTIVE", is_primary=True))
    await db.flush()


@pytest.mark.asyncio
async def test_website_off_shows_the_hub(db):
    user, site, hub = await _agent(db)
    await _domain(db, site)
    assert await get_hub_only_domain(HOST, db) == {"hub_slug": hub.slug}


@pytest.mark.asyncio
async def test_turned_off_completely_forwards_to_the_hub(db):
    user, site, hub = await _agent(db)
    await _domain(db, site, forwards=True)
    out = await get_hub_only_domain(HOST, db)
    assert out["hub_slug"] is None and out["forward_to"].endswith(f"/{hub.slug}")


@pytest.mark.asyncio
async def test_no_live_hub_is_dead_not_openhousepal(db):
    user, site, hub = await _agent(db)
    await _domain(db, site)
    user.plan_tier = "BASIC"          # a plan with no Agent Hub
    await db.flush()
    assert await get_hub_only_domain(HOST, db) == {"hub_slug": None, "dead": True}


@pytest.mark.asyncio
async def test_website_on_is_not_a_hub_only_domain(db):
    user, site, hub = await _agent(db)
    await _domain(db, site, enabled=True, forwards=True)
    assert await get_hub_only_domain(HOST, db) == {"hub_slug": None}
    assert await get_hub_only_domain("nobody-has-this.example", db) == {"hub_slug": None}


@pytest.mark.asyncio
async def test_website_off_on_purpose_is_not_a_domain_problem(db):
    from datetime import datetime, timezone
    row = SiteDomain(site_id="x", hostname=HOST, status="ACTIVE", is_primary=True, created_at=datetime.now(timezone.utc))
    assert domain_health_service.issues_for([row], tier="IDX", enabled=False, site_status="PUBLISHED") == []
    unpublished = domain_health_service.issues_for([row], tier="IDX", enabled=True, site_status="DRAFT")
    assert unpublished and unpublished[0]["code"] == "site_off" and "published" in unpublished[0]["fix"]
