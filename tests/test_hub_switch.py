"""The Agent Hub on/off switch (owner, 11/18). Off = the Hub page doesn't open and nothing links to
it; the Save Contact card keeps working; an IDX agent's live website keeps showing it inside its own
About / Sales pages (embedded). Real queries on the local database in one rolled-back transaction."""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.api import agent_hub_routes as hr
from app.models.database import AgentHub, Site, User
from app.services.open_house_service import OpenHouseService


@pytest_asyncio.fixture
async def db():
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        session.commit = session.flush
        yield session
        await session.rollback()


async def _live_hub(db, idx_site: bool):
    rows = (await db.execute(select(User, AgentHub).join(AgentHub, AgentHub.owner_id == User.id))).all()
    for user, hub in rows:
        try:
            await hr._resolve_public_hub(hub.slug, db)
        except HTTPException:
            continue
        if await hr._idx_site_live(db, user) == idx_site:
            return user, hub
    pytest.skip(f"no live Hub {'with' if idx_site else 'without'} a live IDX website on this database")


@pytest.mark.asyncio
async def test_on_by_default(db):
    user, hub = await _live_hub(db, idx_site=False)
    assert hub.is_public is not False and hr.hub_switched_on(hub)


@pytest.mark.asyncio
async def test_off_closes_the_hub_but_not_save_contact(db):
    user, hub = await _live_hub(db, idx_site=False)
    hub.is_public = False
    await db.flush()
    with pytest.raises(HTTPException) as e:
        await hr._resolve_public_hub(hub.slug, db)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException):              # no website to show it inside
        await hr._resolve_public_hub(hub.slug, db, embedded=True)
    got, _ = await hr._resolve_public_hub(hub.slug, db, contact_card=True)   # Save Contact still works
    assert got.id == hub.id
    link = await OpenHouseService.agent_hub_link(db, user)
    assert link["url"] is None and link["available"] is False
    hub.is_public = True
    await db.flush()
    got, _ = await hr._resolve_public_hub(hub.slug, db)                       # back on = back
    assert got.id == hub.id


@pytest.mark.asyncio
async def test_idx_website_keeps_showing_it_inside_its_pages(db):
    user, hub = await _live_hub(db, idx_site=True)
    hub.is_public = False
    await db.flush()
    with pytest.raises(HTTPException):
        await hr._resolve_public_hub(hub.slug, db)                            # the link itself: off
    got, _ = await hr._resolve_public_hub(hub.slug, db, embedded=True)        # inside the website: on
    assert got.id == hub.id


@pytest.mark.asyncio
async def test_forms_and_spotlights_stop_linking_to_it(db):
    from app.api.forms_routes import _agent_hub_is_live
    user, hub = await _live_hub(db, idx_site=False)
    assert _agent_hub_is_live(user, hub)
    hub.is_public = False
    assert not _agent_hub_is_live(user, hub)
