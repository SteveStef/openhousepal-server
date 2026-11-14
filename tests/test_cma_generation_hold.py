"""CMA "Create AI Analysis" start gate (ai_rate_limit_service.hold_cma_generation).

No database and no AI calls: the recorded-spend lookup is stubbed, so these only exercise the
gate's own rules - one run per report, and in-flight runs counted against the monthly allowance.
"""
import asyncio

import pytest

from app.services.ai import ai_rate_limit_service as rl

CONFIG = {
    "agent_monthly_ai_budget_usd_by_tier": {"IDX": 20.0},
    "cma_ai_max_cost_per_generation_usd": 0.75,
}
OWNER = "owner-aaaa"


@pytest.fixture(autouse=True)
def _fresh_holds(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(rl, "_cma_holds", rl._CmaGenerationHolds())

    # no database in these tests: no admin-set allowance, and a fixed billing period for the "resets on" text
    async def no_custom(db, owner_id):
        return None

    async def period(db, owner_id):
        return datetime(2026, 11, 13, tzinfo=timezone.utc), datetime(2026, 12, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(rl, "_custom_allowance", no_custom)
    monkeypatch.setattr(rl, "usage_period", period)
    yield


def _used(monkeypatch, usd: float, delay: float = 0.0):
    async def fake_total(db, owner_id):
        if delay:
            await asyncio.sleep(delay)   # widen the read window, like a slow DB
        return usd
    monkeypatch.setattr(rl, "total_monthly_ai_cost", fake_total)


def test_single_run_rule_unchanged(monkeypatch):
    _used(monkeypatch, 19.90)   # under the allowance by less than one run's cap - still starts, as before
    assert asyncio.run(rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")) == 0.75


def test_out_of_allowance_refused(monkeypatch):
    _used(monkeypatch, 20.0)
    with pytest.raises(rl.AIRateLimitExceeded) as exc:
        asyncio.run(rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX"))
    assert exc.value.reason == "agent_monthly_budget"


def test_out_of_allowance_message_says_when_it_resets(monkeypatch):
    _used(monkeypatch, 20.0)
    with pytest.raises(rl.AIRateLimitExceeded) as exc:
        asyncio.run(rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX"))
    assert "It resets on Dec 13." in exc.value.user_message


def test_admin_set_allowance_is_the_one_the_gate_uses(monkeypatch):
    """An agent whose admin gave their group $30 a month is not refused at the plan's $20 - and an agent
    given $10 is refused at $10 (test round 11/14: the gate read only the plan's amount)."""
    async def custom_30(db, owner_id):
        return 30.0

    async def custom_10(db, owner_id):
        return 10.0
    _used(monkeypatch, 23.19)
    monkeypatch.setattr(rl, "_custom_allowance", custom_30)
    assert asyncio.run(rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")) == 0.75
    monkeypatch.setattr(rl, "_cma_holds", rl._CmaGenerationHolds())
    _used(monkeypatch, 10.0)
    monkeypatch.setattr(rl, "_custom_allowance", custom_10)
    with pytest.raises(rl.AIRateLimitExceeded) as exc:
        asyncio.run(rl.hold_cma_generation(None, OWNER, "r2", CONFIG, "IDX"))
    assert exc.value.reason == "agent_monthly_budget"


def test_same_report_twice_refused(monkeypatch):
    _used(monkeypatch, 1.0)

    async def go():
        await rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")
        with pytest.raises(rl.CmaGenerationAlreadyRunning):
            await rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")
        await rl.release_cma_generation("r1")
        await rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")   # free again once released
    asyncio.run(go())


def test_two_at_once_only_room_for_one(monkeypatch):
    """$19.50 used of $20: two simultaneous starts - exactly one runs, the other gets the clear message."""
    _used(monkeypatch, 19.50, delay=0.05)

    async def go():
        return await asyncio.gather(
            rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX"),
            rl.hold_cma_generation(None, OWNER, "r2", CONFIG, "IDX"),
            return_exceptions=True,
        )
    results = asyncio.run(go())
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, rl.AIRateLimitExceeded)]
    assert len(ok) == 1 and len(refused) == 1
    assert refused[0].reason == "agent_monthly_budget_in_flight"


def test_two_at_once_with_room_for_both(monkeypatch):
    _used(monkeypatch, 5.0, delay=0.05)

    async def go():
        return await asyncio.gather(
            rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX"),
            rl.hold_cma_generation(None, OWNER, "r2", CONFIG, "IDX"),
        )
    assert asyncio.run(go()) == [0.75, 0.75]


def test_release_gives_room_back_and_other_agents_unaffected(monkeypatch):
    _used(monkeypatch, 19.50)

    async def go():
        await rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")
        with pytest.raises(rl.AIRateLimitExceeded):
            await rl.hold_cma_generation(None, OWNER, "r2", CONFIG, "IDX")
        await rl.hold_cma_generation(None, "someone-else", "r3", CONFIG, "IDX")   # their own allowance
        await rl.release_cma_generation("r1")
        await rl.hold_cma_generation(None, OWNER, "r2", CONFIG, "IDX")
    asyncio.run(go())


def test_stale_hold_stops_counting(monkeypatch):
    _used(monkeypatch, 19.50)

    async def go():
        await rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "IDX")
        o, h, _ = rl._cma_holds.holds["r1"]
        rl._cma_holds.holds["r1"] = (o, h, -10_000.0)   # started "long ago" - a run that never cleaned up
        await rl.hold_cma_generation(None, OWNER, "r2", CONFIG, "IDX")   # the stale hold no longer blocks
        assert "r1" not in rl._cma_holds.holds
    asyncio.run(go())


def test_tier_without_cma_refused(monkeypatch):
    _used(monkeypatch, 0.0)
    with pytest.raises(rl.AIRateLimitExceeded) as exc:
        asyncio.run(rl.hold_cma_generation(None, OWNER, "r1", CONFIG, "NO_MLS"))
    assert exc.value.reason == "plan_tier"
