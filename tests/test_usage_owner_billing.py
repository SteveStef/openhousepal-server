"""Paid lookups charge the signed-in agent's AI usage bar (user, 10/25: "all things that use AI usage
should charge the agent's usage bar") - see app/services/usage_owner.py."""
import asyncio

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.services.ai import ai_rate_limit_service
from app.services.usage_owner import bill_to, charge_google, current_usage_owner, set_usage_owner


@pytest.fixture
def recorded(monkeypatch):
    rows = []

    async def fake_record(db, owner_id, *, kind, calls=1, cost_usd=None, config=None, blocked=False):
        rows.append((owner_id, kind, calls))

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_config(db):
        return {}

    monkeypatch.setattr(ai_rate_limit_service, "record_external_api_cost", fake_record)
    monkeypatch.setattr("app.database.AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr("app.services.ai.ai_config_service.ai_config_service.get_config", fake_config)
    return rows


@pytest.mark.asyncio
async def test_charge_google_with_no_agent_records_nothing(recorded):
    with bill_to(None):
        await charge_google("geocoding")
    assert recorded == []


@pytest.mark.asyncio
async def test_charge_google_charges_the_marked_agent_and_tasks_inherit_it(recorded):
    with bill_to("agent-1"):
        await charge_google("places_text")
        await asyncio.gather(charge_google("routes_matrix_element_traffic", 3))
    await charge_google("geocoding")  # outside the block: nobody
    assert recorded == [("agent-1", "places_text", 1), ("agent-1", "routes_matrix_element_traffic", 3)]
    assert current_usage_owner() is None


def test_sign_in_dependency_marks_the_request_for_async_and_sync_endpoints():
    app = FastAPI()

    async def fake_db():
        yield None

    async def fake_current_user(db=Depends(fake_db)):
        set_usage_owner("agent-7")
        return "agent-7"

    @app.get("/async")
    async def async_endpoint(user=Depends(fake_current_user)):
        return {"owner": current_usage_owner()}

    @app.get("/sync")
    def sync_endpoint(user=Depends(fake_current_user)):
        return {"owner": current_usage_owner()}

    @app.get("/public")
    async def public_endpoint():
        return {"owner": current_usage_owner()}

    client = TestClient(app)
    assert client.get("/async").json() == {"owner": "agent-7"}
    assert client.get("/sync").json() == {"owner": "agent-7"}
    assert client.get("/public").json() == {"owner": None}  # never leaks into the next request


@pytest.mark.asyncio
async def test_new_price_kinds_cost_money_even_with_an_older_saved_price_table(monkeypatch):
    rows = []

    async def fake_usage_event(db, owner_id, conversation_id, model, i, o, cost, tools, blocked, reason, **kw):
        rows.append((model, cost))

    monkeypatch.setattr(ai_rate_limit_service, "record_usage_event", fake_usage_event)
    old_table = {"external_api_prices_usd": {"geocoding": 0.005}}  # saved before street_view existed
    await ai_rate_limit_service.record_external_api_cost(None, "agent-1", kind="street_view", config=old_table)
    await ai_rate_limit_service.record_external_api_cost(None, "agent-1", kind="routes_matrix_element_traffic", calls=4, config=old_table)
    assert rows == [("google:street_view", 0.007), ("google:routes_matrix_element_traffic", 0.04)]


@pytest.mark.asyncio
async def test_downtown_ai_charges_the_agent_whose_cheat_sheet_asked(monkeypatch):
    from app.services import nearby_amenities_service as svc
    from app.services.ai import ai_downtown_service as dt

    events = []

    class FakeDb:
        async def rollback(self):
            pass

    async def no_cache(db, key, ttl):
        return None

    async def write_cache(db, key, value):
        pass

    async def fake_config(db):
        return {"area_research_global_daily_cost_ceiling_usd": 5, "area_research_global_monthly_cost_ceiling_usd": 50,
                "model_pricing": {"claude-sonnet-5-5": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}}

    async def reserve(*a, **k):
        return object()

    async def reconcile(*a, **k):
        pass

    async def ask(city, state, zipcode, candidates, config):
        return [{"name": "Media", "kind": "downtown"}], {"model": "claude-sonnet-5-5", "input": 1000, "output": 100, "searches": 2}

    async def fake_usage_event(db, owner_id, conversation_id, model, i, o, cost, searches, blocked, reason, **kw):
        events.append((owner_id, kw.get("event_type"), cost > 0))

    monkeypatch.setattr(svc, "_read_explore_cache", no_cache)
    monkeypatch.setattr(svc, "_write_explore_cache", write_cache)
    monkeypatch.setattr("app.services.ai.ai_config_service.ai_config_service.get_config", fake_config)
    monkeypatch.setattr(ai_rate_limit_service, "reserve_global_and_emergency", reserve)
    monkeypatch.setattr(ai_rate_limit_service, "reconcile_budget", reconcile)
    monkeypatch.setattr(ai_rate_limit_service, "record_usage_event", fake_usage_event)
    monkeypatch.setattr(dt, "_ask", ask)
    monkeypatch.setattr(dt, "_clean", lambda raw: raw)

    with bill_to("agent-3"):
        await dt._downtown_picks(FakeDb(), "Media", "PA", "19063", [])
    with bill_to(None):  # a re-lookup we caused: platform pays - written down with no agent (Admin site spend)
        await dt._downtown_picks(FakeDb(), "Media", "PA", "19063", [])
    assert events == [("agent-3", "cheat_sheet_downtown", True), (None, "cheat_sheet_downtown", True)]


@pytest.mark.asyncio
async def test_adding_a_town_never_charges_the_agent(recorded, monkeypatch):
    """User, 10/26: the only thing that never counts on an agent's AI usage is adding a town."""
    from app.api import site_routes

    async def fake_enrich(area_id, user_id):
        await charge_google("places_text", 5)
        await asyncio.gather(charge_google("routes_matrix_element_traffic", 4))

    monkeypatch.setattr(site_routes, "_enrich_area", fake_enrich)
    with bill_to("agent-1"):  # the agent who added the town is signed in
        await site_routes._enrich_area_background("area-1", "agent-1")
        await charge_google("geocoding")  # their next own lookup still charges them
    assert recorded == [("agent-1", "geocoding", 1)]


@pytest.mark.asyncio
async def test_town_research_ai_stays_off_the_agent_bar():
    from sqlalchemy.dialects import postgresql

    class CaptureDb:
        statements = []

        async def execute(self, stmt):
            self.statements.append(str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))

            class R:
                def scalar_one(self):
                    return 0.0
            return R()

    db = CaptureDb()
    await ai_rate_limit_service.total_monthly_ai_cost(db, "agent-1")
    await ai_rate_limit_service.monthly_cost_so_far(db, "agent-1", event_type="area_research")
    total_sql, own_type_sql = db.statements
    assert "NOT IN ('area_research', 'area_narrative', 'area_fair_housing')" in total_sql
    assert "NOT IN" not in own_type_sql  # the platform's own research ceiling still sees them
