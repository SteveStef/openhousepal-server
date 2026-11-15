"""CMA/blog test 11/13 - a town typed into the AI blog writer is the town we already know by that name.

"Cherry Hill" typed by a PA agent had created (and researched) a new "Cherry Hill, PA" next to the
real Cherry Hill, NJ; "Fishtown" had created an empty city row next to the researched Fishtown
neighborhood, so the article's MLS facts read "0 sales, 0 homes for sale in Fishtown".
Covers town_knowledge_service.best_area_by_name / find_areas and POST /studio/blogs/ai/town-card.
Runs against the local Postgres in one rolled-back transaction (skipped when it isn't reachable).
Nothing here calls Google or the AI."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


class _Db:
    async def __aenter__(self):
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        self.engine = create_async_engine(_db_url())
        try:
            self.conn = await self.engine.connect()
        except Exception as e:  # noqa: BLE001
            await self.engine.dispose()
            pytest.skip(f"local database not reachable: {e}")
        self.trans = await self.conn.begin()
        self.db = AsyncSession(bind=self.conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        return self.db

    async def __aexit__(self, *exc):
        await self.db.close()
        await self.trans.rollback()
        await self.conn.close()
        await self.engine.dispose()


def _area(name, state, *, area_type="city", status="ENRICHED", geography=None, age_days=30):
    from app.models.database import MarketArea
    tag = uuid.uuid4().hex[:10]
    return MarketArea(id=str(uuid.uuid4()), name=name, state=state, slug=f"t-{tag}", canonical_key=f"t|{tag}",
                      area_type=area_type, enrichment_status=status, geography=geography or {},
                      created_at=datetime.now(timezone.utc) - timedelta(days=age_days))


POLY = {"type": "polygon", "bounds": {"min_lat": 39.9, "max_lat": 40.0, "min_lng": -75.2, "max_lng": -75.1}}


@pytest.mark.asyncio
async def test_bare_name_finds_the_town_in_another_state():
    from app.services.areas.town_knowledge_service import best_area_by_name, find_areas
    name = f"Zqx Hill {uuid.uuid4().hex[:6]}"
    async with _Db() as db:
        nj = _area(name, "NJ")
        db.add(nj)
        await db.flush()
        assert (await best_area_by_name(db, name.upper(), prefer_state="PA")).id == nj.id
        assert [a.id for a in await find_areas(db, names=[name], state="PA")] == [nj.id]
        # A state the agent typed is binding.
        assert await best_area_by_name(db, name, state="PA", prefer_state="PA") is None
        assert [a.id for a in await find_areas(db, names=[f"{name}, NJ"], state="PA")] == [nj.id]


@pytest.mark.asyncio
async def test_researched_neighborhood_beats_an_empty_stray_row():
    from app.services.areas.town_knowledge_service import best_area_by_name, find_areas
    name = f"Zqxtown {uuid.uuid4().hex[:6]}"
    async with _Db() as db:
        hood = _area(name, "PA", area_type="neighborhood", geography=POLY, age_days=20)
        stray = _area(name, "PA", status="PARTIAL", age_days=0)
        db.add_all([hood, stray])
        await db.flush()
        assert (await best_area_by_name(db, name, prefer_state="PA")).id == hood.id
        # The stray picked up by its slug and the real one by name -> one town, the real one.
        got = await find_areas(db, slugs=[stray.slug], names=[name], state="PA")
        assert [a.id for a in got] == [hood.id]


@pytest.mark.asyncio
async def test_same_name_in_two_states_prefers_the_agents_state():
    from app.services.areas.town_knowledge_service import best_area_by_name
    name = f"Zqxfield {uuid.uuid4().hex[:6]}"
    async with _Db() as db:
        nj, pa = _area(name, "NJ", age_days=90), _area(name, "PA", age_days=10)
        db.add_all([nj, pa])
        await db.flush()
        assert (await best_area_by_name(db, name, prefer_state="PA")).id == pa.id
        assert (await best_area_by_name(db, name, prefer_state="NJ")).id == nj.id


@pytest.mark.asyncio
async def test_town_card_uses_the_known_town_and_creates_nothing():
    from sqlalchemy import func, select
    from app.api.blog_routes import blog_town_card
    from app.models.database import MarketArea
    name = f"Zqx Hill {uuid.uuid4().hex[:6]}"
    async with _Db() as db:
        # PARTIAL: the route answers "researching" without starting research or fetching photos.
        nj = _area(name, "NJ", status="PARTIAL")
        db.add(nj)
        await db.flush()
        agent = SimpleNamespace(id="t-agent", state="PA")
        out = await blog_town_card({"name": name}, db=db, current_user=agent)
        assert out["slug"] == nj.slug
        count = (await db.execute(select(func.count()).select_from(MarketArea).where(func.lower(MarketArea.name) == name.lower()))).scalar()
        assert count == 1
