"""The generate-ai-analysis route with the start gate: everything that touches the DB or an AI
provider is stubbed (no paid call can happen). Calls the route function directly."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.database as app_database
from app.api import cma_routes
from app.schemas.cma import CmaGenerateAiAnalysisRequest
from app.services.ai import ai_rate_limit_service as rl
from app.services.ai.ai_config_service import ai_config_service

CONFIG = {"agent_monthly_ai_budget_usd_by_tier": {"IDX": 20.0}, "cma_ai_max_cost_per_generation_usd": 0.75}
USER = SimpleNamespace(id="owner-route", plan_tier="IDX")


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def stubs(monkeypatch):
    monkeypatch.setattr(rl, "_cma_holds", rl._CmaGenerationHolds())
    state = SimpleNamespace(release=None, started=[], finished=[], ran=[])

    async def get_config(db):
        return CONFIG

    async def total(db, owner_id):
        return 19.50   # room for exactly one more run

    async def get_report(db, owner_id, report_id):
        return SimpleNamespace(id=report_id)

    async def mark_started(db, owner_id, report_id):
        state.started.append(report_id)

    async def mark_finished(owner_id, report_id, outcome, error=None):
        state.finished.append((report_id, outcome))

    async def drop(*a, **k):
        return None

    async def fake_generate(s, owner_id, report_id, tier, opts):
        state.ran.append(report_id)
        await state.release.wait()

    monkeypatch.setattr(ai_config_service, "get_config", get_config)
    monkeypatch.setattr(rl, "total_monthly_ai_cost", total)
    monkeypatch.setattr(cma_routes.service, "get_report", get_report)
    monkeypatch.setattr(cma_routes.service, "mark_generation_started", mark_started)
    monkeypatch.setattr(cma_routes.service, "mark_generation_finished", mark_finished)
    monkeypatch.setattr(cma_routes.service, "drop_subject_from_comps", drop)
    monkeypatch.setattr(cma_routes, "_generate_maybe_as_of", fake_generate)
    monkeypatch.setattr(app_database, "AsyncSessionLocal", _FakeSession)
    return state


def _start(report_id):
    return cma_routes.generate_cma_ai_analysis(
        report_id, CmaGenerateAiAnalysisRequest(background=True), db=None, current_user=USER, _broker=USER,
    )


def test_second_simultaneous_start_refused_then_allowed_after_first_finishes(stubs):
    async def go():
        stubs.release = asyncio.Event()
        results = await asyncio.gather(_start("A"), _start("B"), return_exceptions=True)
        refused = [r for r in results if isinstance(r, HTTPException)]
        assert len(refused) == 1 and refused[0].status_code == 429
        assert "still running" in refused[0].detail
        assert len(stubs.started) == 1            # the refused one was never marked 'generating'
        await asyncio.sleep(0.01)
        assert stubs.ran == stubs.started         # only the accepted run did any work
        first = stubs.started[0]
        # Same report again while it runs -> 409, and its marker is untouched.
        with pytest.raises(HTTPException) as exc:
            await _start(first)
        assert exc.value.status_code == 409
        assert stubs.finished == []
        stubs.release.set()
        for _ in range(50):
            if stubs.finished:
                break
            await asyncio.sleep(0.01)
        assert stubs.finished == [(first, "complete")]
        assert rl._cma_holds.holds == {}          # hold released when the run ended
        other = "B" if first == "A" else "A"
        stubs.release = asyncio.Event()
        stubs.release.set()
        await _start(other)                       # now there's room again
    asyncio.run(go())


def test_hold_released_when_run_fails(stubs, monkeypatch):
    async def boom(*a, **k):
        raise rl.AIRateLimitExceeded("feature_capacity", "busy")
    monkeypatch.setattr(cma_routes, "_generate_maybe_as_of", boom)

    async def go():
        await _start("A")
        for _ in range(50):
            if stubs.finished:
                break
            await asyncio.sleep(0.01)
        assert stubs.finished == [("A", "failed")]
        assert rl._cma_holds.holds == {}
    asyncio.run(go())
