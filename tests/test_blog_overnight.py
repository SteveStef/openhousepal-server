"""Overnight articles + the 11/14 cost cuts (owner: "make the half price overnight mode work and fully test it";
"cut all the fat ... but do not diminish quality"; "make sure the suggestions count toward the usage bar ... very
little ... at 97% or greater" they stop).

No network and no database: the batch queue, the article row and the usage log are stand-ins.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.ai import blog_overnight_service as night
from app.services.ai import blog_suggest_service as sug

PRICES = {"model_pricing": {"opus": {"input_per_mtok": 4.0, "output_per_mtok": 20.0, "cache_read_multiplier": 0.05},
                            "sonnet": {"input_per_mtok": 2.0, "output_per_mtok": 10.0, "cache_read_multiplier": 0.05}}}


def usage(inp=0, out=0, searches=0, read=0, write=0, **more):
    return {**pipe._new_usage(), "input_tokens": inp, "output_tokens": out, "web_search_requests": searches,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write, **more}


# ----------------------------------------------------------------------------- price

def test_overnight_tokens_are_half_price_and_searches_are_not():
    now = pipe._cost(PRICES, "sonnet", usage(100_000, 2_000, searches=6))
    later = pipe._cost(PRICES, "sonnet", usage(100_000, 2_000, searches=6, batch=True))
    assert now == pytest.approx(0.2 + 0.02 + 0.06) and later == pytest.approx(0.1 + 0.01 + 0.06)
    assert pipe.overnight_usd({}, 0.90) == pytest.approx((0.90 - 0.06) / 2 + 0.06)
    assert pipe.overnight_usd({}, 0.04) == pytest.approx(0.04)          # never below the searches themselves


# ----------------------------------------------------------------------------- the cost cuts

def test_claim_lists_name_a_sentence_by_its_opening_words():
    writer_text = pipe._WRITER_TOOL["input_schema"]["properties"]["claims"]["items"]["properties"]["text"]["description"]
    checker_quote = pipe._REVIEW_TOOL["input_schema"]["properties"]["claims"]["items"]["properties"]["quote"]["description"]
    assert "first six words" in writer_text and "never the whole sentence" in writer_text
    # the checker still quotes anything it doubts in full - those sentences are acted on afterwards
    assert "first six words" in checker_quote.lower() and "any other verdict: the whole sentence" in checker_quote
    assert "none skipped" in pipe._REVIEW_SYSTEM and "quoted exactly, with the ledger ids" not in pipe._REVIEW_SYSTEM


def test_opening_words_get_their_whole_sentence_back():
    text = ("Buying in Ardmore\n## Prices\nThe home on St. Davids Road sold for $640,000 in June. Condos ran a median of $310,000 "
            "over the last 90 days. Condos ran a median of $298,000 a year earlier.\n- Twins: 12 sold in that time. Most went fast.\n")
    claims = [{"text": "The home on St. Davids Road"}, {"text": "Condos ran a median of"}, {"text": "Condos ran a median of $298,000"},
              {"text": "Most went fast"}, {"text": "A sentence that is not there"},
              {"text": "The home on St. Davids Road sold for $640,000 in June."}]
    assert pipe.expand_quotes(text, claims, "text") == 4
    assert claims[0]["text"] == "The home on St. Davids Road sold for $640,000 in June."      # "St." does not end a sentence
    assert claims[1]["text"].endswith("over the last 90 days.") and claims[2]["text"].endswith("a year earlier.")
    assert claims[3]["text"] == "Most went fast." and claims[4]["text"] == "A sentence that is not there"
    assert claims[5]["text"] == "The home on St. Davids Road sold for $640,000 in June."      # a whole sentence is left alone


def test_the_checkers_share_one_cached_ledger():
    block = pipe._ledger_block("[E1] A fact.")
    shared = pipe._system_blocks("You are the grader.", block)
    assert shared[0] == {"type": "text", "text": block, "cache_control": {"type": "ephemeral"}} and shared[1] == {"type": "text", "text": "You are the grader."}
    alone = pipe._system_blocks("You are the writer.", None)
    assert alone == [{"type": "text", "text": "You are the writer.", "cache_control": {"type": "ephemeral"}}]
    assert [t["name"] for t in pipe._CHECK_TOOLS] == ["submit_review", "submit_grade", "submit_fixes"]
    # read from the cache at a twentieth of the price
    first = pipe._cost(PRICES, "sonnet", usage(7_000, 0, write=20_000))
    second = pipe._cost(PRICES, "sonnet", usage(5_000, 0, read=20_000))
    assert first == pytest.approx((7_000 * 2 + 20_000 * 2 * 1.25) / 1e6) and second == pytest.approx((5_000 * 2 + 20_000 * 2 * 0.05) / 1e6)


def test_a_call_to_the_wrong_shared_tool_is_answered_and_asked_again(monkeypatch):
    sent = []

    async def fake_send(_client, kwargs, _usage):
        sent.append(kwargs)
        if len(sent) == 1:
            return SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5), content=[
                SimpleNamespace(type="tool_use", id="tu_1", name="submit_review", input={})])
        return SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5), content=[
            SimpleNamespace(type="tool_use", id="tu_2", name="submit_grade", input={"items": []})])

    monkeypatch.setattr(pipe, "_send", fake_send)
    monkeypatch.setattr(pipe.writer, "_client", lambda: object())
    got = asyncio.run(pipe._run_tool_call(model="sonnet", system="grade", user_msg="article", tools=pipe._CHECK_TOOLS, tool_name="submit_grade",
                                          max_tokens=100, effort="high", usage=pipe._new_usage(), shared="ledger"))
    assert got == {"items": []} and len(sent) == 2
    reply = sent[1]["messages"][-1]["content"]
    assert reply[0] == {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True, "content": "Not this tool - call submit_grade."}
    assert reply[1]["text"] == "Now call submit_grade with your result."


# ----------------------------------------------------------------------------- the batch queue

class Queue:
    """A stand-in for Anthropic's batch endpoints."""

    def __init__(self, answers, polls_before_done=2, result="succeeded"):
        self.answers, self.polls_before_done, self.result = list(answers), polls_before_done, result
        self.created, self.polls, self.canceled = [], {}, []
        self.messages = SimpleNamespace(batches=self)

    async def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"batch_{len(self.created)}")

    async def retrieve(self, batch_id):
        self.polls[batch_id] = self.polls.get(batch_id, 0) + 1
        return SimpleNamespace(processing_status="ended" if self.polls[batch_id] > self.polls_before_done else "in_progress")

    async def cancel(self, batch_id):
        self.canceled.append(batch_id)

    async def results(self, batch_id):
        n = int(batch_id.split("_")[1]) - 1

        async def rows():
            if self.result == "succeeded":
                yield SimpleNamespace(result=SimpleNamespace(type="succeeded", message=self.answers[n]))
            elif self.result == "invalid":
                yield SimpleNamespace(result=SimpleNamespace(type="errored", error=SimpleNamespace(error=SimpleNamespace(
                    type="invalid_request_error", message="web_search_20250305 is not supported"))))
            else:
                yield SimpleNamespace(result=SimpleNamespace(type=self.result))
        return rows()


def answer(inp=1000, out=500, searches=0):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                                 server_tool_use=SimpleNamespace(web_search_requests=searches)), content=[], stop_reason="end_turn")


def journal(state=None, resumed=False):
    saves = []

    async def save():
        saves.append(1)
    j = night.Journal(state if state is not None else {}, save, resumed=resumed)
    return j, saves


async def no_sleep(_s):
    return None


KW = dict(model="sonnet", max_tokens=100, system=[{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}],
          tools=[{"name": "t", "input_schema": {}}], tool_choice={"type": "auto"}, messages=[{"role": "user", "content": "hello"}],
          extra_body={"output_config": {"effort": "high"}})


def test_a_request_goes_through_the_queue_unchanged_and_is_marked_half_price():
    q = Queue([answer(1000, 500, searches=2)])
    j, saves = journal()
    u = pipe._new_usage()
    got = asyncio.run(night.BatchTransport(j, sleep=no_sleep).send(q, KW, u))
    assert got is q.answers[0] and u["batched"]["output_tokens"] == 500 and "replayed" not in u     # this answer: half price
    (request,), = q.created
    params = request["params"]
    assert params["model"] == "sonnet" and params["output_config"] == {"effort": "high"} and "extra_body" not in params
    assert params["system"] == [{"type": "text", "text": "rules"}]                    # no cache write premium for an answer hours away
    assert params["messages"] == KW["messages"] and request["custom_id"] == night.request_key(params)
    (entry,) = j.state["calls"].values()
    assert entry["batch"] == "batch_1" and entry["done"] and len(saves) == 2        # written when submitted, and when answered
    assert q.polls["batch_1"] == 3


def test_after_a_restart_the_answer_is_fetched_again_not_bought_again():
    q = Queue([answer(1000, 500)])
    j, _ = journal()
    asyncio.run(night.BatchTransport(j, sleep=no_sleep).send(q, KW, pipe._new_usage()))
    # the process dies; another one picks the run up with the state that was written to the row
    j2, _ = journal(dict(j.state), resumed=True)
    u = pipe._new_usage()
    again = asyncio.run(night.BatchTransport(j2, sleep=no_sleep).send(q, KW, u))
    assert again is q.answers[0] and len(q.created) == 1                             # no second batch
    pipe._add_usage(u, again)
    assert u["replayed"]["output_tokens"] == 500                                     # counted for the receipt, charged once

    # died while still waiting (submitted, not answered): the same batch is waited for, and it IS charged - once
    state = {"calls": {night.request_key(night.batch_params(KW)): {"batch": "batch_1", "at": "x"}}, "events": {}}
    j3, _ = journal(state, resumed=True)
    u3 = pipe._new_usage()
    asyncio.run(night.BatchTransport(j3, sleep=no_sleep).send(q, KW, u3))
    assert len(q.created) == 1 and "replayed" not in u3


def test_an_answer_is_sent_back_as_plain_data_when_a_turn_continues():
    class Block:
        def model_dump(self, **kw):
            assert kw == {"mode": "json", "exclude_unset": True}
            return {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "ardmore"}}
    kw = {**KW, "messages": [{"role": "user", "content": "hello"}, {"role": "assistant", "content": [Block()]}]}
    params = night.batch_params(kw)
    assert params["messages"][1]["content"] == [{"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "ardmore"}}]
    assert night.request_key(params) != night.request_key(night.batch_params(KW))


def test_queue_failures_end_the_run_cleanly():
    import anthropic
    from fastapi import HTTPException
    j, _ = journal()
    with pytest.raises(HTTPException) as slow:
        asyncio.run(night.BatchTransport(j, sleep=no_sleep).send(Queue([answer()], result="expired"), KW, pipe._new_usage()))
    assert slow.value.status_code == 504 and "Nothing was charged" in slow.value.detail
    # the research step's fallback to the other web tools still works through the queue
    j, _ = journal()
    with pytest.raises(anthropic.BadRequestError):
        asyncio.run(night.BatchTransport(j, sleep=no_sleep).send(Queue([answer()], result="invalid"), KW, pipe._new_usage()))
    # past the deadline: the batch is canceled and the run stops
    j, _ = journal({"latest_at": "2020-01-01T00:00:00+00:00"})
    q = Queue([answer()], polls_before_done=99)
    with pytest.raises(HTTPException):
        asyncio.run(night.BatchTransport(j, sleep=no_sleep).send(q, KW, pipe._new_usage()))
    assert q.canceled == ["batch_1"]


def test_a_run_that_is_no_longer_ours_stops_and_cannot_be_swallowed():
    assert not issubclass(night.Superseded, Exception)       # the pipeline carries on past ordinary errors - not this

    async def gone():
        raise night.Superseded()
    j = night.Journal({}, gone)
    with pytest.raises(night.Superseded):
        asyncio.run(night.BatchTransport(j, sleep=no_sleep).send(Queue([answer()]), KW, pipe._new_usage()))


# ----------------------------------------------------------------------------- charging

class Log:
    def __init__(self):
        self.events, self.refunded = [], []

    async def record(self, _db, _owner, _conv, model, _inp, _out, cost, _n, blocked, reason, tool_names=None, event_type=None):
        e = SimpleNamespace(id=f"ev{len(self.events) + 1}", cost=cost, blocked=blocked, tool_names=tool_names, event_type=event_type)
        self.events.append(e)
        return e

    async def refund(self, _db, ids, reason="refunded_failed_run"):
        self.refunded.append((list(ids), reason))


def test_each_step_is_charged_once_even_across_a_restart(monkeypatch):
    log = Log()
    monkeypatch.setattr(pipe, "record_usage_event", log.record)
    monkeypatch.setattr(night, "refund_events", log.refund)
    user = SimpleNamespace(id="u1")
    j, _ = journal()

    async def run():
        meter = pipe._Meter(None, user, PRICES, j)
        # first run: research answered and charged at half price
        await meter.record("research", "sonnet", usage(100_000, 2_000, searches=4, batch=True), 0.0)
        assert log.events[0].cost == pytest.approx(0.1 + 0.01 + 0.04) and log.events[0].tool_names == ["blog_pipeline_research", "overnight"]
        assert j.state["events"] == {"research": ["ev1"]} and meter.stages[0]["overnight"] is True

        # picked up after a restart: research comes back from the queue (replayed) - on the receipt, not charged again
        j2 = night.Journal(dict(j.state), j.save, resumed=True)
        meter2 = pipe._Meter(None, user, PRICES, j2)
        same = usage(100_000, 2_000, searches=4, batch=True)
        same["replayed"] = usage(100_000, 2_000, searches=4)
        await meter2.record("research", "sonnet", same, 0.0)
        assert len(log.events) == 1 and meter2.total == pytest.approx(0.15) and len(meter2.stages) == 1
        # the writing step is new: charged
        await meter2.record("write", "opus", usage(30_000, 17_000, batch=True), 0.0)
        assert len(log.events) == 2 and log.events[1].cost == pytest.approx((30_000 * 4 + 17_000 * 20) / 2e6)
        assert j2.event_ids() == ["ev1", "ev2"]

        # a step that had to be asked again after a restart (its request changed): the first charge goes back
        j3 = night.Journal(dict(j2.state), j.save, resumed=True)
        meter3 = pipe._Meter(None, user, PRICES, j3)
        await meter3.record("research", "sonnet", usage(90_000, 1_500, searches=3, batch=True), 0.0)
        assert log.refunded == [(["ev1"], "replaced_after_restart")] and j3.state["events"]["research"] == ["ev3"]
    asyncio.run(run())


def test_the_allowance_check_asks_for_the_worst_case_either_way(monkeypatch):
    """At worst (the slower line is busy and the time cap finishes it the fast way) a Save usage article uses the
    normal amount - so it must fit, or the article could push the agent past their allowance."""
    import inspect
    from fastapi import HTTPException
    spent = {"usd": 19.20}

    async def limit(*_a):
        return 20.0

    async def total(*_a):
        return spent["usd"]

    async def period(*_a):
        from datetime import datetime, timezone
        return datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc)

    from app.services.ai import ai_rate_limit_service as rl
    monkeypatch.setattr(rl, "monthly_limit_usd", limit)
    monkeypatch.setattr(pipe, "total_monthly_ai_cost", total)
    monkeypatch.setattr(pipe, "usage_period", period)
    user = SimpleNamespace(id="someone", plan_tier="IDX")
    config = {"blog_pipeline_gate_usd": {"max": 1.2}}
    with pytest.raises(HTTPException):                      # 19.20 + 1.20 > 20
        asyncio.run(pipe._gate(None, user, config, "max"))
    spent["usd"] = 18.70
    asyncio.run(pipe._gate(None, user, config, "max"))      # fits
    assert "overnight" not in inspect.signature(pipe._gate).parameters and "overnight" not in inspect.signature(pipe.precheck).parameters


# ----------------------------------------------------------------------------- times, staleness, email

def test_overnight_state_times_and_staleness():
    from datetime import datetime, timedelta, timezone
    state = night.new_state({"topic": "Ardmore", "listings": []}, {"blog_overnight_typical_minutes": 60})
    assert state["worker"] == night.WORKER and state["calls"] == {} and state["brief"]["topic"] == "Ardmore"
    likely, latest = datetime.fromisoformat(state["likely_at"]), datetime.fromisoformat(state["latest_at"])
    # "likely ready by" never says later than the cap plus the fast finish (an hour's typical time fits under the 60-minute cap)
    assert timedelta(minutes=59) < likely - datetime.now(timezone.utc) < timedelta(minutes=61)
    assert timedelta(minutes=59) < datetime.fromisoformat(state["cap_at"]) - datetime.now(timezone.utc) < timedelta(minutes=61)
    slow = night.new_state({"topic": "Ardmore", "listings": []}, {"blog_overnight_typical_minutes": 180})
    assert timedelta(minutes=69) < datetime.fromisoformat(slow["likely_at"]) - datetime.now(timezone.utc) < timedelta(minutes=71)
    assert timedelta(hours=19, minutes=59) < latest - datetime.now(timezone.utc) <= timedelta(hours=20)
    review = {"status": "running", "mode": "overnight", "started_at": state["lease_at"], "overnight": state}
    assert night.is_overnight(review) and not night.is_stale(review)                      # hours of waiting is not "stale"
    old = {**review, "overnight": {**state, "latest_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}}
    assert night.is_stale(old)
    shown = night.public_state({**review, "overnight": {**state, "calls": {"a": {"done": True}, "b": {}}}})
    assert shown["mode"] == "overnight" and shown["steps_done"] == 1 and shown["likely_at"] == state["likely_at"] and "brief" not in shown


def test_the_ready_and_failed_emails():
    subject, html, text = night.render_email("blog_article_ready", "Your article is ready: Buying in Ardmore", {
        "first_name": "Peter", "article_title": "Buying in Ardmore", "link": "https://openhousepal.com/studio/blogs/b1",
        "used_line": "It used about 2.2% of this month's AI usage - about half of what \"Write it now\" uses.", "app_url": "https://openhousepal.com"})
    assert subject.startswith("Your article is ready") and "Buying in Ardmore" in html and "https://openhousepal.com/studio/blogs/b1" in html
    assert "nothing is public until you publish it" in html and "about 2.2% of this month" in html and "Read your article" in html
    assert "Read it: https://openhousepal.com/studio/blogs/b1" in text
    _s, html, text = night.render_email("blog_article_failed", "We couldn't finish your article", {
        "first_name": "Peter", "article_title": "Buying in Ardmore", "link": "https://x/y", "reason": night.SLOW_ERROR})
    assert "Nothing was charged" in html and "try again" in html and "Nothing was charged" in text
    from app.services import email_service
    assert email_service.BLOG_OVERNIGHT_TEMPLATES == night.EMAIL_TEMPLATES


# ----------------------------------------------------------------------------- smart suggestions: tiny, and they stop first

class CountDb:
    def __init__(self, asked=0):
        self.asked = asked

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalar=lambda: self.asked, all=lambda: [])

    async def rollback(self):
        return None

    def begin_nested(self):
        class Savepoint:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False
        return Savepoint()


def _limits(monkeypatch, used, limit=20.0, pool=None):
    from datetime import datetime, timezone
    from app.services.ai import ai_rate_limit_service as rl

    async def _limit(*_a):
        return limit

    async def _used(*_a):
        return used

    async def _pool(*_a):
        return pool

    async def _period(*_a):
        return datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(rl, "monthly_limit_usd", _limit)
    monkeypatch.setattr(rl, "total_monthly_ai_cost", _used)
    monkeypatch.setattr(rl, "group_pool", _pool)
    monkeypatch.setattr(rl, "usage_period", _period)


def test_smart_suggestions_rest_at_97_percent_and_after_a_days_worth(monkeypatch):
    user = SimpleNamespace(id="someone", plan_tier="IDX")
    _limits(monkeypatch, used=19.30)                                  # 96.5%
    assert asyncio.run(sug.usage_guard(CountDb(), user, {})) is None
    _limits(monkeypatch, used=19.40)                                  # 97%
    paused = asyncio.run(sug.usage_guard(CountDb(), user, {}))
    assert paused["reason"] == "nearly_out" and "almost used up" in paused["message"] and "It resets on Dec 1." in paused["message"]
    _limits(monkeypatch, used=1.0, pool={"used": 98.0, "total": 100.0, "resets": __import__("datetime").datetime(2026, 12, 1, tzinfo=__import__("datetime").timezone.utc)})
    assert asyncio.run(sug.usage_guard(CountDb(), user, {}))["reason"] == "nearly_out"      # the team's shared total counts too
    _limits(monkeypatch, used=1.0)
    assert asyncio.run(sug.usage_guard(CountDb(asked=sug.DAILY_ASKS - 1), user, {})) is None
    assert asyncio.run(sug.usage_guard(CountDb(asked=sug.DAILY_ASKS), user, {}))["reason"] == "daily"
    # the worst a full day of asks can cost one agent stays tiny: a fraction of a percent of the smallest allowance
    assert sug.DAILY_ASKS * 0.0006 < 0.03


def test_a_resting_guard_serves_the_builtin_choices_without_calling_the_ai(monkeypatch):
    called = {"ai": 0}

    async def paused(*_a, **_k):
        return {"reason": "nearly_out", "message": "resting"}

    async def ask(*_a, **_k):
        called["ai"] += 1
        return None, 0.0
    monkeypatch.setattr(sug, "usage_guard", paused)
    monkeypatch.setattr(sug, "_ask", ask)
    sug._CACHE.clear()
    user = SimpleNamespace(id="u-rest", plan_tier="IDX")
    out = asyncio.run(sug.suggest(CountDb(), user, title="Selling a home in Wayne this spring", served_areas=["Wayne"]))
    assert out["paused"]["message"] == "resting" and out["ai"] is False and out["cost_usd"] == 0.0 and out["key_points"] and called["ai"] == 0
    ctx = {"month": "November", "month_no": 11, "year": 2026, "market": "Main Line", "about": "", "towns": ["Wayne"], "sales": [], "past_titles": []}
    titles = asyncio.run(sug.title_ideas(CountDb(), user, context=ctx))
    assert titles["paused"]["reason"] == "nearly_out" and titles["ai"] is False and titles["titles"] and called["ai"] == 0


# ----------------------------------------------------------------------------- the job, end to end (stand-ins)

STARTED = "2026-11-14T22:21:59+00:00"


class JobSession:
    def __init__(self, world):
        self.w = world

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, _id):
        return self.w["user"] if model.__name__ == "User" else self.w["blog"]

    async def execute(self, stmt):
        self.w["writes"] += 1
        return SimpleNamespace(rowcount=self.w["rowcount"])

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def invalidate(self):
        pass

    def add(self, obj):
        self.w["added"].append(obj)


@pytest.fixture
def night_world(monkeypatch):
    import app.main  # noqa: F401
    import app.database
    from app.api import blog_routes
    from app.services import agent_email_service
    from app.services.ai import ai_notify
    from app.services.ai import ai_rate_limit_service as rl
    w = {"user": SimpleNamespace(id="u1", plan_tier="IDX"), "writes": 0, "rowcount": 1, "added": [], "emails": [], "refunded": [],
         "blog": SimpleNamespace(id="b1", owner_id="u1", title="Gladwyne prices", slug="gladwyne", ai_review={
             "status": "running", "mode": "overnight", "stage": "gathering", "engine": "max", "started_at": STARTED, "new": True,
             "overnight": night.new_state({"topic": "Gladwyne prices"})})}
    monkeypatch.setattr(app.database, "AsyncSessionLocal", lambda: JobSession(w))

    async def finished(*a, **k):
        pass

    async def slug(db, owner, base, exclude_id=None):
        return base

    async def limit(*_a):
        return 20.0

    async def config(_db):
        return {}

    async def refund(_db, ids, reason="refunded_failed_run"):
        w["refunded"].append(list(ids))
    monkeypatch.setattr(ai_notify, "finished", finished)
    monkeypatch.setattr(blog_routes, "next_available_slug", slug)
    monkeypatch.setattr(blog_routes, "_apply_ai_result", lambda blog, result: setattr(blog, "ai_review", {"status": "done", "overnight": True}))
    monkeypatch.setattr(blog_routes, "_AI_JOB_POLL_SECONDS", 0.02)
    monkeypatch.setattr(rl, "monthly_limit_usd", limit)
    monkeypatch.setattr(pipe.ai_config_service, "get_config", config)
    monkeypatch.setattr(night, "refund_events", refund)
    monkeypatch.setattr(agent_email_service, "email_agent", lambda user, ntype, template, subject, variables=None, **kw: w["emails"].append((template, subject, variables, kw)))
    return w


def test_an_overnight_job_runs_through_the_queue_and_alerts_by_bell_and_email(night_world, monkeypatch):
    from app.api import blog_routes
    seen = {}

    async def run_pipeline(db, user, brief, *, engine=None, on_stage=None, overnight=False, journal=None):
        seen.update(transport=pipe.TRANSPORT.get(), overnight=overnight, journal=journal, running=set(night._RUNNING))
        await on_stage("gathering")
        await on_stage("research")
        return {"title": "Gladwyne Home Prices", "usage": {"cost_usd": 0.5}}
    monkeypatch.setattr(pipe, "run_pipeline", run_pipeline)
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "Gladwyne prices"}, "max", overnight=True), timeout=5))
    assert isinstance(seen["transport"], night.BatchTransport) and seen["overnight"] is True and seen["journal"] is seen["transport"].journal
    assert seen["running"] == {"b1"} and night._RUNNING == set()
    assert pipe.TRANSPORT.get() is None                                   # the queue is this run's only - nothing else is sent through it
    (bell,) = night_world["added"]
    assert bell.type == "BLOG_AI_READY" and bell.title.startswith("Your article is ready")          # the same product, the same bell
    ((template, subject, variables, kw),) = night_world["emails"]
    assert template == "blog_article_ready" and kw == {"always": True} and "Gladwyne" in subject
    assert variables["used_line"] == "It used about 2.5% of this month's AI usage."                # 0.50 of 20 - what it really used
    assert night_world["blog"].ai_review["run_cost_usd"] == 0.5 and night_world["refunded"] == []


def test_an_overnight_job_another_server_took_over_writes_nothing(night_world, monkeypatch):
    from app.api import blog_routes

    async def run_pipeline(db, user, brief, *, engine=None, on_stage=None, overnight=False, journal=None):
        night_world["rowcount"] = 0          # the row is no longer this process's
        await on_stage("research")
        raise AssertionError("the run should have stopped at the save")
    monkeypatch.setattr(pipe, "run_pipeline", run_pipeline)
    before = dict(night_world["blog"].ai_review)
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "x"}, "max", overnight=True, resume=True), timeout=5))
    assert night_world["added"] == [] and night_world["emails"] == [] and night_world["refunded"] == []
    assert night_world["blog"].ai_review["status"] == before["status"] == "running" and night._RUNNING == set()


def test_an_overnight_job_that_fails_refunds_its_own_charges_and_says_so(night_world, monkeypatch):
    from fastapi import HTTPException
    from app.api import blog_routes

    async def run_pipeline(db, user, brief, *, engine=None, on_stage=None, overnight=False, journal=None):
        journal.state["events"]["research"] = ["ev1"]
        raise HTTPException(504, night.SLOW_ERROR)
    monkeypatch.setattr(pipe, "run_pipeline", run_pipeline)
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "x"}, "max", overnight=True), timeout=5))
    assert night_world["blog"].ai_review["status"] == "failed" and "Nothing was charged" in night_world["blog"].ai_review["error"]
    assert [n.type for n in night_world["added"]] == ["BLOG_AI_FAILED"]
    ((template, _subject, variables, _kw),) = night_world["emails"]
    assert template == "blog_article_failed" and variables["reason"] == night.SLOW_ERROR


def test_the_usage_note_comes_from_what_recent_articles_really_cost():
    from app.api import blog_routes
    assert blog_routes._middle_cost([1.23, 1.08]) is None                 # not enough to go by yet
    assert blog_routes._middle_cost([1.23, 1.08, 0.87, None, 0]) == 1.08

    class Db:
        def __init__(self, rows):
            self.rows = rows

        def begin_nested(self):
            class Savepoint:
                async def __aenter__(s):
                    return s

                async def __aexit__(s, *exc):
                    return False
            return Savepoint()

        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: self.rows)
    config = {"blog_pipeline_typical_usd": {"max": 0.9}}
    # nothing measured yet: the fixed figure, and overnight worked out from it
    usd, night_usd = asyncio.run(blog_routes._article_estimate(Db([]), config, "max", "standard"))
    assert usd == 0.9 and night_usd == pytest.approx((0.9 - 0.06) / 2 + 0.06)
    rows = [(1.23, False), (1.08, False), (0.87, False), (0.53, True), (0.55, True), (0.51, True)]
    usd, night_usd = asyncio.run(blog_routes._article_estimate(Db(rows), config, "max", "standard"))
    assert usd == 1.08 and night_usd == 0.53
    usd, night_usd = asyncio.run(blog_routes._article_estimate(Db(rows), config, "max", "long"))
    assert usd == pytest.approx(1.08 * 1.3) and night_usd == pytest.approx(0.53 * 1.3)


# ----------------------------------------------------------------------------- Save usage mode (the agent-facing name), finish it now

def test_agents_never_read_the_word_overnight():
    """Owner 11/14: "don't call it overnight ... save usage mode where it's the same product ... made slower so it's
    cheaper". The code keeps its first name; nothing an agent reads does."""
    for template in ("blog_article_ready", "blog_article_failed"):
        _s, html, text = night.render_email(template, "Your article", {"first_name": "Peter", "article_title": "T", "link": "https://x/y",
                                                                       "reason": night.SLOW_ERROR, "used_line": "It used about 2%."})
        assert "overnight" not in (html + text).lower()
    assert "overnight" not in night.SLOW_ERROR.lower() and "Save usage mode" in night.SLOW_ERROR
    from app.services import agent_email_health_service as health
    assert "overnight" not in (health.LABELS["blog_article_ready"] + health.LABELS["blog_article_failed"]).lower()


def test_the_research_request_keeps_its_cache_mark_in_the_slower_line():
    web = {**KW, "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}, {"name": "submit_evidence", "input_schema": {}}]}
    assert night.batch_params(web)["system"] == KW["system"]                       # re-reads cost a twentieth, on top of half price
    assert night.batch_params(KW)["system"] == [{"type": "text", "text": "rules"}]  # a request that reads nothing twice: no mark


class Streamed:
    """A stand-in client whose normal (streamed) send can be told apart from the queue."""

    def __init__(self, queue, message):
        self.queue, self.message, self.streamed = queue, message, []
        outer = self

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get_final_message(self):
                return outer.message
        self.messages = SimpleNamespace(batches=queue, stream=lambda **kw: (outer.streamed.append(kw), Stream())[1])


def test_ready_time_comes_from_real_runs_and_is_shown_to_the_screens():
    from datetime import datetime, timedelta, timezone
    from app.api import blog_routes

    class Db:
        def __init__(self, rows):
            self.rows = rows

        def begin_nested(self):
            class Savepoint:
                async def __aenter__(s):
                    return s

                async def __aexit__(s, *exc):
                    return False
            return Savepoint()

        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: self.rows)
    t0 = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)
    run = lambda minutes: (t0.isoformat(), (t0 + timedelta(minutes=minutes)).isoformat())
    assert asyncio.run(blog_routes._slow_minutes(Db([run(12.5), run(14)]), "max")) is None          # two runs: not enough to go by
    assert asyncio.run(blog_routes._slow_minutes(Db([run(12.5), run(14), run(30)]), "max")) == 20    # middle 14 min, a third added, up to the next 5
    assert asyncio.run(blog_routes._slow_minutes(Db([run(2), run(3), run(4)]), "max")) == 10         # never promised in under ten
    state = night.new_state({"topic": "x"}, {}, 20)
    assert timedelta(minutes=19) < datetime.fromisoformat(state["likely_at"]) - datetime.now(timezone.utc) < timedelta(minutes=21)
    assert night.TYPICAL_MINUTES == 20
    shown = night.public_state({"overnight": state})
    assert "hurry" not in shown and shown["likely_at"] == state["likely_at"]


# ----------------------------------------------------------------------------- the time cap

def test_after_the_time_cap_the_rest_is_done_fast_and_the_agent_pays_what_each_step_really_used(monkeypatch):
    """Owner 11/14: "if they take more than 30 minutes, it gets all passed to the fast version" - and "the amount
    that they actually use": half price for what the slower line answered, the normal price for what was finished
    fast. Nothing is covered by the platform, nothing is charged twice."""
    from datetime import datetime, timedelta, timezone
    log = Log()
    monkeypatch.setattr(pipe, "record_usage_event", log.record)
    fast = answer(30_000, 17_000)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    async def run():
        # a step still to start when the cap has passed: it never enters the slower line
        q = Queue([answer()])
        client = Streamed(q, fast)
        j, _ = journal({"cap_at": past})
        u = pipe._new_usage()
        got = await night.BatchTransport(j, sleep=no_sleep).send(client, KW, u)
        assert got is fast and j.state["capped"] is True and q.created == [] and len(client.streamed) == 1
        pipe._add_usage(u, got)
        assert "batch" not in u and "batched" not in u and "absorbed" not in u
        meter = pipe._Meter(None, SimpleNamespace(id="u1"), PRICES, j)
        await meter.record("write", "opus", u, 0.0)
        full = (30_000 * 4 + 17_000 * 20) / 1e6
        (agent,) = log.events                                             # one charge, the agent's, at the normal price
        assert agent.cost == pytest.approx(full) and agent.tool_names == ["blog_pipeline_write"] and meter.total == pytest.approx(full)
        assert not hasattr(meter, "absorbed")

        # a step that was waiting when the cap passed: its batch is canceled, it is sent the normal way
        q2 = Queue([answer()], polls_before_done=99)
        client2 = Streamed(q2, fast)
        j2, _ = journal({"cap_at": (datetime.now(timezone.utc) + timedelta(seconds=0.05)).isoformat()})

        async def tick(_s):
            await asyncio.sleep(0.03)
        got = await night.BatchTransport(j2, sleep=tick).send(client2, KW, pipe._new_usage())
        assert got is fast and q2.canceled == ["batch_1"] and j2.state["calls"] == {} and len(client2.streamed) == 1

        # before the cap nothing changes: the slower line, half price
        q3 = Queue([answer(1000, 500)])
        j3, _ = journal({"cap_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()})
        u3 = pipe._new_usage()
        got3 = await night.BatchTransport(j3, sleep=no_sleep).send(Streamed(q3, fast), KW, u3)
        pipe._add_usage(u3, got3)
        assert len(q3.created) == 1 and not j3.state.get("capped")
        assert pipe._cost(PRICES, "sonnet", u3) == pytest.approx((1000 * 2 + 500 * 10) / 2e6)
    asyncio.run(run())


def test_a_step_answered_partly_in_the_slower_line_and_partly_fast_is_charged_exactly():
    slow, quick = answer(20_000, 1_000, searches=3), answer(60_000, 2_000, searches=2)
    u = pipe._new_usage()
    pipe._add_usage(u, slow)
    pipe._add_usage(u.setdefault("batched", pipe._new_usage()), slow)        # what the transport notes for a slower-line answer
    pipe._add_usage(u, quick)                                                 # the continuation, finished fast after the cap
    slow_tokens, quick_tokens = (20_000 * 2 + 1_000 * 10) / 1e6, (60_000 * 2 + 2_000 * 10) / 1e6
    assert pipe._cost(PRICES, "sonnet", u) == pytest.approx(slow_tokens / 2 + quick_tokens + 5 * 0.01)   # searches are never discounted


def test_the_screens_are_told_the_cap():
    state = night.new_state({"topic": "x"}, {})
    assert state["cap_minutes"] == 60 == night.CAP_MINUTES                        # owner 11/15: an hour before the fast finish
    t = night.times({})
    assert t["cap_minutes"] == 60 and t["max_minutes"] == 70 and t["typical_minutes"] == 20
    assert night.times({"blog_save_usage_cap_minutes": 45})["max_minutes"] == 55
    shown = night.public_state({"overnight": {**state, "capped": True}})
    assert shown["capped"] is True and shown["max_minutes"] == 70


# ----------------------------------------------------------------------------- the worst case shown before creating

def test_the_top_of_the_range_is_the_dearest_recent_article_not_the_middle_one():
    """Owner 11/15: before creating, the agent sees "worst case scenario spend". The top figure was the MIDDLE of what
    "Write it now" articles cost ($0.97) while a heavy one had cost $1.23 - and a Save usage article the time cap
    finishes the fast way costs what a "Write it now" one does."""
    class Db:
        def __init__(self, rows):
            self.rows = rows

        def begin_nested(self):
            class Ctx:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *a):
                    return False
            return Ctx()

        async def execute(self, *_a, **_k):
            rows = self.rows
            return type("R", (), {"all": lambda self_inner: rows})()

    from app.api import blog_routes
    now_runs = [(0.97, False), (1.23, False), (0.85, False), (1.08, False)]
    save_runs = [(0.53, True), (0.66, True), (0.47, True)]
    worst = asyncio.run(blog_routes._article_worst(Db(now_runs + save_runs), "max", "standard", 1.025))
    assert worst == 1.23                                                          # the dearest "Write it now" one; Save usage ones don't count
    assert asyncio.run(blog_routes._article_worst(Db(now_runs), "max", "standard", 1.5)) == 1.5      # never under the middle figure
    # too few to go by: a third above the middle
    assert asyncio.run(blog_routes._article_worst(Db(now_runs[:2]), "max", "standard", 0.9)) == round(0.9 * 1.3, 5)
    assert asyncio.run(blog_routes._article_worst(Db([]), "max", "standard", 0.9)) == 1.17
