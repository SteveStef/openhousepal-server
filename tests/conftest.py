"""Several test files run the app's real database code, each on its own event loop, in one pytest
process. A pooled connection opened on one loop can't be used from the next ("cannot perform
operation: another operation is in progress"), so a full run failed tests that pass on their own.
After each test the app engine starts a fresh pool, so no connection outlives the loop that opened
it (close=False: the old loop may already be gone). The app itself is unchanged."""
import pytest

import app.database as _db


@pytest.fixture(autouse=True)
def _fresh_db_pool_per_test():
    yield
    _db.engine.sync_engine.dispose(close=False)


# Homes under contract stopped moving the value on 11/14 (cma_ai_service._PENDING_NUDGE_ENABLED, default off).
# The tests written for that machinery keep exercising it switched ON, so it stays correct if it is ever turned
# back on; tests/test_cma_grader_1114.py checks the default.
_NUDGE_MACHINERY_TESTS = {
    "test_cma_deep_dive_valuation", "test_cma_golden_1021", "test_cma_round2_1113", "test_cma_rule_audit_1024",
}


@pytest.fixture(autouse=True)
def _no_final_read_unless_a_test_asks(monkeypatch):
    """The CMA final read is on by default since 11/15 and makes a real AI call. No test may reach it by accident:
    it is off here, and the tests that exercise it (tests/test_cma_final_audit.py) switch it on with a stand-in model."""
    monkeypatch.setenv("CMA_FINAL_AUDIT", "0")
    yield


@pytest.fixture(autouse=True)
def _pending_nudge_for_its_own_tests(request, monkeypatch):
    if request.module.__name__.split(".")[-1] in _NUDGE_MACHINERY_TESTS:
        from app.services import cma_ai_service
        monkeypatch.setattr(cma_ai_service, "_PENDING_NUDGE_ENABLED", True)
    yield

