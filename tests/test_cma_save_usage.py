"""Save usage for CMA reports (owner 11/17): the same report through the half-price queue, 20 minutes at most in it,
charged for what it really used, and the agent told by the bell and an email."""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from app.services import cma_ai_service  # noqa: F401  (loaded first, as the app does)
from app.services import cma_claude, cma_save_usage, cma_service


def _world(monkeypatch, *, queue_answers=True):
    queued, sent = [], []

    async def queue(client, kw, cap_seconds=None, **_):
        if cap_seconds is None and not cma_claude.slow_lane_on():
            return None
        queued.append(cap_seconds)
        return NS(content=[], usage=NS(input_tokens=5, output_tokens=5), stop_reason="end_turn") if queue_answers else None

    class Client:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kw):
                sent.append(kw)
                return NS(content=[], usage=NS(input_tokens=10, output_tokens=10), stop_reason="end_turn")

    monkeypatch.setattr(cma_claude, "through_the_queue", queue)
    monkeypatch.delenv("CMA_SLOW_LANE", raising=False)
    ask = lambda **k: asyncio.run(cma_claude.create(Client(), model="claude-haiku-5-5", max_tokens=10, messages=[], **k))  # noqa: E731
    return ask, queued, sent


def test_without_save_usage_a_report_goes_the_normal_way(monkeypatch):
    ask, queued, sent = _world(monkeypatch)
    ask()
    assert queued == [] and len(sent) == 1


def test_inside_save_usage_every_request_waits_in_the_slower_line_with_the_time_left(monkeypatch):
    ask, queued, sent = _world(monkeypatch)
    t = [1000.0]
    with cma_claude.save_usage(60, clock=lambda: t[0]) as su:
        ask(); t[0] += 600; ask()
    assert queued == [3600.0, 3000.0]                        # (the hour here is the test's own setting) and sent == [] and su["queued"] == 2 and su["fast"] == 0
    ask()                                                    # after the block: back to normal
    assert len(sent) == 1 and len(queued) == 2


def test_once_the_hour_is_used_up_the_rest_is_made_the_fast_way(monkeypatch):
    ask, queued, sent = _world(monkeypatch)
    t = [0.0]
    with cma_claude.save_usage(60, clock=lambda: t[0]) as su:
        ask(); t[0] = 3560.0; ask(); ask()                   # under a minute left: not worth queueing
    assert len(queued) == 1 and len(sent) == 2 and su == {**su, "queued": 1, "fast": 2}


def test_a_request_the_queue_did_not_answer_in_time_is_made_the_fast_way_at_full_price(monkeypatch):
    ask, queued, sent = _world(monkeypatch, queue_answers=False)
    with cma_claude.save_usage(60) as su:
        resp = ask()
    assert len(queued) == 1 and len(sent) == 1 and su["fast"] == 1 and su["queued"] == 0
    # full price: the normal answer's counts are NOT halved (unlike the video's promise, a CMA pays what it used)
    assert resp.usage.input_tokens == 10 and resp.usage.output_tokens == 10


def test_direct_requests_and_the_video_rule_are_untouched(monkeypatch):
    ask, queued, sent = _world(monkeypatch)
    with cma_claude.save_usage(60):
        ask(direct=True)
        ask(slow_cap=900)
    assert queued == [900] and len(sent) == 1               # the photo sorter stays direct; video keeps its own cap


def test_a_save_usage_run_is_given_its_time_before_it_counts_as_dead():
    started = datetime.now(timezone.utc) - timedelta(minutes=35)
    now_run = NS(generation_status="generating", generation_started_at=started, generation_mode="now")   # normal runs: 30 min
    save_run = NS(generation_status="generating", generation_started_at=started, generation_mode="save_usage")
    cma_service._reconcile_stale_generation(now_run)
    cma_service._reconcile_stale_generation(save_run)
    assert now_run.generation_status == "failed" and save_run.generation_status == "generating"
    late = NS(generation_status="generating", generation_started_at=datetime.now(timezone.utc) - timedelta(minutes=45), generation_mode="save_usage")
    cma_service._reconcile_stale_generation(late)
    assert late.generation_status == "failed"
    assert cma_save_usage.stale_after_seconds() == (20 + 5 + 15) * 60


def test_the_choice_shows_a_ready_time_and_the_record_says_how_it_went():
    t = cma_save_usage.times({}, None)
    assert t["typical_minutes"] == 15 and t["cap_minutes"] == 20 and t["max_minutes"] == 25
    assert cma_save_usage.times({}, 11.6)["typical_minutes"] == 12 and cma_save_usage.times({"cma_save_usage_cap_minutes": 30}, 90)["typical_minutes"] == 35
    assert cma_save_usage.times({}, 60)["typical_minutes"] == 25            # never promised past the limit
    began = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
    rec = cma_save_usage.run_record({"queued": 7, "fast": 0}, began, began + timedelta(minutes=12, seconds=30))
    assert rec == {"minutes": 12.5, "queued_calls": 7, "fast_calls": 0, "capped": False}
    assert cma_save_usage.run_record({"queued": 5, "fast": 2}, began, began)["capped"] is True


def test_the_ready_and_failed_emails_read_plainly():
    subj, html, text = cma_save_usage.render_email("cma_report_ready", "Your CMA is ready: 940 Turner Ave",
                                                   {"address": "940 Turner Ave", "link": "https://x/studio/cma/1", "used_line": "It used about 2% of this month's AI usage."})
    assert "Your CMA is ready" in html and "940 Turner Ave" in text and "2% of this month" in text and "https://x/studio/cma/1" in text
    subj, html, text = cma_save_usage.render_email("cma_report_failed", "s", {"address": "940 Turner Ave", "link": "L", "reason": "The AI hit an error."})
    assert "We couldn't finish the CMA for 940 Turner Ave." in text and "The AI hit an error." in text and "couldn" in html


def test_the_route_runs_save_usage_in_the_background_and_tells_the_agent():
    from app.api import cma_routes
    from app.schemas.cma import CmaGenerateAiAnalysisRequest
    assert CmaGenerateAiAnalysisRequest().save_usage is False and CmaGenerateAiAnalysisRequest(save_usage=True).save_usage is True
    body = inspect.getsource(cma_routes.generate_cma_ai_analysis)
    assert "if payload.background or save_usage:" in body
    assert 'mode="save_usage" if save_usage else "now"' in body
    assert "_start_run(report_id, current_user.id" in body
    run = inspect.getsource(cma_routes._run_generation)
    assert "left = cma_save_usage.cap_minutes(config) - (_dt.now(_tz.utc) - began).total_seconds() / 60" in run
    assert "with cma_claude.save_usage(max(0.0, left)) as su:" in run
    assert "cma_save_usage.run_record(su, began)" in run
    assert "await cma_save_usage.tell_agent(owner_id, report_id, address, None if ok else err, cost)" in inspect.getsource(cma_routes._tell)
    est = inspect.getsource(cma_routes.cma_ai_estimate)
    assert '"save_usage": {"usd"' in est and '"now": {"usd"' in est and '"max_percent": pct(cap)' in est


def test_the_emails_are_routed_and_count_as_ones_the_agent_must_get():
    from app.services import email_service, agent_email_health_service as h
    assert email_service.CMA_SAVE_USAGE_TEMPLATES == cma_save_usage.EMAIL_TEMPLATES
    src = inspect.getsource(h)
    assert '"cma_report_ready", "cma_report_failed",' in src and '"cma_report_ready": "CMA ready (Save usage mode)"' in src


# --- a run cut off by a restart (owner 11/17: "CMAs don't [pick back up] - easy fix? do it") ---

def _run_world(monkeypatch, gen):
    from app.api import cma_routes
    from app.services import cluster
    import app.database as dbmod
    log = {"finished": [], "released_hold": 0, "lease_released": 0, "told": [], "minutes": []}

    class S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): pass

    async def acquire(*a, **k): return "tok"
    async def release(*a, **k): log["lease_released"] += 1
    async def noop(*a, **k): return None
    async def finished(owner, rid, outcome, error=None): log["finished"].append(outcome)
    async def rel(rid): log["released_hold"] += 1
    async def tell(*a): log["told"].append(a)
    real = cma_claude.save_usage

    class SU(real):
        def __init__(self, minutes, **k):
            log["minutes"].append(round(minutes, 1)); super().__init__(minutes, **k)

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", S)
    monkeypatch.setattr(cluster, "acquire", acquire)
    monkeypatch.setattr(cluster, "release", release)
    monkeypatch.setattr(cluster, "start_keep_alive", lambda *a: asyncio.get_event_loop().create_task(asyncio.sleep(3600)))
    monkeypatch.setattr(cma_routes.service, "drop_subject_from_comps", noop)
    monkeypatch.setattr(cma_routes.service, "mark_generation_finished", finished)
    monkeypatch.setattr(cma_routes, "release_cma_generation", rel)
    monkeypatch.setattr(cma_routes, "_tell", tell)
    monkeypatch.setattr(cma_routes, "_generate_maybe_as_of", gen)
    monkeypatch.setattr(cma_claude, "save_usage", SU)
    return cma_routes, log


def test_a_run_stopped_by_the_server_shutting_down_stays_marked_for_the_pickup(monkeypatch):
    async def gen(*a): raise asyncio.CancelledError()
    routes, log = _run_world(monkeypatch, gen)

    async def go():
        try:
            await routes._run_generation("r1", "o1", "PRO", {}, True, {}, "1 Main St", datetime.now(timezone.utc))
        except asyncio.CancelledError:
            return "cancelled"
    assert asyncio.run(go()) == "cancelled"
    # NOT marked failed and no "couldn't finish" email - the sweep starts it again
    assert log["finished"] == [] and log["told"] == []


def test_a_picked_up_save_usage_run_only_gets_what_is_left_of_its_20_minutes(monkeypatch):
    async def gen(*a): return NS(ai_analysis={"cost_usd": 0.4})
    routes, log = _run_world(monkeypatch, gen)
    began = datetime.now(timezone.utc) - timedelta(minutes=15)
    asyncio.run(routes._run_generation("r1", "o1", "PRO", {}, True, {}, "1 Main St", began, run_token="tok", resumed=True))
    assert log["minutes"] == [5.0] and log["finished"] == ["complete"] and log["released_hold"] == 1 and log["lease_released"] == 1
    began = datetime.now(timezone.utc) - timedelta(minutes=23)       # past the 20: all of it the normal way
    asyncio.run(routes._run_generation("r1", "o1", "PRO", {}, True, {}, "1 Main St", began, run_token="tok", resumed=True))
    assert log["minutes"][-1] == 0.0


def test_only_runs_the_button_started_are_picked_up_and_the_mark_is_cleared_when_one_ends():
    from app.api import cma_routes
    src = inspect.getsource(cma_routes.resume_stalled)
    assert 'CmaReport.generation_mode.in_(("now", "save_usage"))' in src and "_RESUME_AFTER_SECONDS" in src
    assert "generation_mode=None" in inspect.getsource(cma_service.mark_generation_finished)
    one = inspect.getsource(cma_routes._resume_one)
    assert "limit - _FAST_RUN_SECONDS" in one and 'await cluster.release("cma_hold", rid)' in one
    import app.main as m
    assert "cma_run_resume" in inspect.getsource(m)


def test_the_final_value_step_also_uses_the_slower_line_in_save_usage(monkeypatch):
    """Live 11/17 (119 East Ln): the final value step called the queue itself, never saw Save usage, paid full price."""
    queued = []

    async def queue(client, kw, cap_seconds=None, **_):
        if cap_seconds is None and not cma_claude.slow_lane_on():
            return None
        queued.append(cap_seconds)
        return NS(content=[], usage=NS(input_tokens=5, output_tokens=5), stop_reason="end_turn")
    monkeypatch.setattr(cma_claude, "through_the_queue", queue)
    monkeypatch.delenv("CMA_SLOW_LANE", raising=False)
    assert asyncio.run(cma_claude.queue_for_report(None, {})) is None and queued == []      # made now: normal way
    t = [0.0]
    with cma_claude.save_usage(20, clock=lambda: t[0]) as su:
        assert asyncio.run(cma_claude.queue_for_report(None, {})) is not None
        t[0] = 1190.0                                                                        # 20 minutes used up
        assert asyncio.run(cma_claude.queue_for_report(None, {})) is None
    assert queued == [1200.0] and su["queued"] == 1 and su["fast"] == 1
    src = inspect.getsource(cma_ai_service._call_cma_reasoning_claude)
    assert "await _cma_claude.queue_for_report(client, kwargs)" in src and "_cma_claude.through_the_queue(" not in src
