"""Admin town page preview (owner, 10/26) - GET /admin/areas/{id}/preview in app/api/admin_area_routes.py.

Admin only; works for a town no agent serves (a stand-in agent); and for a town an agent serves it is
exactly what that agent's public site gets. Runs against the local Postgres in one rolled-back
transaction (skipped when it isn't reachable). Nothing here calls Google or the AI."""
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402

ADMIN_EMAIL = "town-preview-admin@example.com"


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


def _facts():
    return {
        "restaurants": {"notes": "Main Street has places to eat.",
                        "entities": [{"name": "Test Diner", "category": "Diner", "city": "Zqxville", "distance_miles": 0.3}]},
        "parks": {"entities": [{"name": "Zqx Park", "category": "Park", "distance_miles": 1.0}]},
        "public_schools": {"districts": [{"name": "Zqx School District"}],
                           "elementary_schools": [{"name": "Zqx Elementary", "district": "Zqx School District"}]},
    }


@pytest.fixture
def scenario(monkeypatch):
    """(client factory, records) on a rolled-back transaction: one town nobody serves, one town an
    IDX agent with a published site serves."""
    return _Scenario(monkeypatch)


class _Scenario:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch

    async def __aenter__(self):
        import httpx
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.database import get_db
        from app.main import app as fastapi_app
        from app.models.database import AgentMarketArea, MarketArea, Site, User
        from app.services import app_cache
        from app.utils import auth

        self.monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
        self.monkeypatch.setattr(app_cache, "save_bg", lambda *_a, **_k: None)  # no saved-cache rows from test towns
        self.engine = create_async_engine(_db_url())
        try:
            self.conn = await self.engine.connect()
        except Exception as e:  # noqa: BLE001
            await self.engine.dispose()
            pytest.skip(f"local database not reachable: {e}")
        self.trans = await self.conn.begin()
        db = AsyncSession(bind=self.conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.lonely = MarketArea(id=str(uuid.uuid4()), name=f"Zqx{tag} Borough", state="ZZ", slug=f"zqx{tag}-borough-zz",
                                 canonical_key=f"zz|city|zqx{tag}b", area_type="city", county="Zqx County",
                                 enrichment_status="ENRICHED", facts=_facts(), narrative={"summary": "A small test borough. It has a main street."})
        self.served = MarketArea(id=str(uuid.uuid4()), name=f"Zqx{tag} Village", state="ZZ", slug=f"zqx{tag}-village-zz",
                                 canonical_key=f"zz|city|zqx{tag}v", area_type="city", county="Zqx County",
                                 enrichment_status="ENRICHED", facts=_facts(), narrative={"summary": "A test village. It has a park."})
        self.agent = User(id=str(uuid.uuid4()), email=f"tp-agent-{tag}@example.com", hashed_password="x", first_name="Tess",
                          last_name="Agent", state="PA", brokerage="Test Realty", plan_tier="IDX", subscription_status="ACTIVE",
                          broker_authorized=True)
        db.add_all([self.lonely, self.served, self.agent])
        await db.flush()
        self.site = Site(id=str(uuid.uuid4()), owner_id=self.agent.id, subdomain_slug=f"tp-{tag}", status="PUBLISHED", is_enabled=True,
                         draft_data={}, published_snapshot={"theme": {"template": "default", "mode": "light"},
                                                            "pages": [{"slug": "", "title": "Tess Agent Homes", "is_home": True, "blocks": []}]})
        db.add_all([self.site, AgentMarketArea(owner_id=self.agent.id, area_id=self.served.id)])
        await db.flush()

        self.current = {"user": SimpleNamespace(id="admin-test", email=ADMIN_EMAIL, is_active=True)}

        async def _db():
            yield db

        async def _user():
            return self.current["user"]

        self.app = fastapi_app
        fastapi_app.dependency_overrides[get_db] = _db
        fastapi_app.dependency_overrides[auth.get_current_user] = _user
        fastapi_app.dependency_overrides[auth.get_current_active_user] = _user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test")
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        self.app.dependency_overrides.clear()
        await self.db.close()
        await self.trans.rollback()
        await self.conn.close()
        await self.engine.dispose()


@pytest.mark.asyncio
async def test_preview_is_admin_only(scenario):
    async with scenario as s:
        s.current["user"] = SimpleNamespace(id=s.agent.id, email=s.agent.email, is_active=True)
        r = await s.client.get(f"/admin/areas/{s.served.id}/preview")
        assert r.status_code == 403
        r = await s.client.get(f"/admin/areas/{s.lonely.id}/preview?site=none")
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_preview_works_for_a_town_no_agent_serves(scenario):
    async with scenario as s:
        r = await s.client.get(f"/admin/areas/{s.lonely.id}/preview")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["site"] is None and out["agents"] == [] and out["slug"] == s.lonely.slug
        area = out["area"]
        assert area["name"] == s.lonely.name and area["narrative"]["summary"].startswith("A small test borough")
        assert area["facts"]["restaurants"]["entities"][0]["name"] == "Test Diner"
        assert area["my_listings"] == [] and area["sales"] == [] and area["open_houses"] == [] and area["related_areas"] == []
        assert out["listings"] == {"listings": [], "total": 0}
        assert out["profile"]["name"] == "Your name here" and out["profile"]["sample"] is True
        assert [a["slug"] for a in out["areas"]] == [s.lonely.slug]
        assert out["reviews"]["show"] is False
        # an agent that doesn't serve it can't be picked
        r = await s.client.get(f"/admin/areas/{s.lonely.id}/preview?site={s.site.id}")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_preview_matches_the_public_page_for_a_town_an_agent_serves(scenario):
    async with scenario as s:
        sid, slug = s.site.id, s.served.slug
        r = await s.client.get(f"/admin/areas/{s.served.id}/preview?limit=12&sort=price_desc")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["agents"] == [{"site_id": sid, "name": "Tess Agent", "host": f"{s.site.subdomain_slug}.openhousepal.com"}]
        assert out["site"]["site_id"] == sid and out["site"]["agent_name"] == "Tess Agent"
        assert out["site"]["theme"]["template"] == "default" and out["site"]["pages"][0]["title"] == "Tess Agent Homes"

        async def public(path):
            pr = await s.client.get(f"/public/sites/{sid}{path}")
            assert pr.status_code == 200, pr.text
            return pr.json()
        assert out["area"] == await public(f"/areas/{slug}")
        assert out["listings"] == await public(f"/areas/{slug}/listings?limit=12&sort=price_desc")
        assert out["profile"] == await public("/profile")
        assert out["areas"] == await public("/areas")
        assert out["reviews"] == await public("/reviews")
        resolved = (await s.client.get(f"/public/sites/resolve?host={s.site.subdomain_slug}.openhousepal.com&path={slug}")).json()
        assert out["site"]["theme"] == resolved["published_snapshot"]["theme"]

        # The same town with no agent: the shared town data is identical, the agent's own parts are empty.
        r = await s.client.get(f"/admin/areas/{s.served.id}/preview?site=none")
        sample = r.json()
        assert sample["site"] is None and sample["profile"]["name"] == "Your name here"
        for key in ("name", "state", "slug", "county", "facts", "narrative", "market_trends", "market_sales", "insights", "municipalities", "explore_origin"):
            assert sample["area"][key] == out["area"][key], key
        assert sample["area"]["my_listings"] == [] and sample["area"]["related_areas"] == []
