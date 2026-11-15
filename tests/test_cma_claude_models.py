"""Owner, 11/13: move the CMA to the current Claude models (Opus 5.5 for the photo / comp-rating step, Fable 5.2
for the final value) and take OpenAI out. Those models reject a forced tool choice and always think, so every
CMA call goes through app.services.cma_claude; the final-value step has a Claude path with the same packet,
instructions and answer schema. No provider is called in these tests."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as svc  # noqa: E402
from app.services import cma_claude  # noqa: E402

TOOL = {"name": "submit_cma_analysis", "description": "x", "input_schema": {"type": "object", "properties": {}}}
CALL = dict(max_tokens=12000, system="sys", tools=[TOOL], tool_choice={"type": "tool", "name": "submit_cma_analysis"},
            messages=[{"role": "user", "content": [{"type": "text", "text": "facts"}]}])


def test_older_models_are_sent_exactly_as_before():
    kw, name = cma_claude.prepare(dict(CALL, model="claude-sonnet-5"))
    assert name is None and kw == dict(CALL, model="claude-sonnet-5")


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"])
def test_current_models_get_auto_tool_choice_the_instruction_headroom_and_effort(model):
    kw, name = cma_claude.prepare(dict(CALL, model=model))
    assert name == "submit_cma_analysis"
    assert kw["tool_choice"] == {"type": "auto"}                      # a forced choice is a 400 on these models
    assert kw["max_tokens"] == 12000 + cma_claude.THINKING_HEADROOM_TOKENS
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["messages"][-1]["content"][-1]["text"] == cma_claude.tool_instruction("submit_cma_analysis")
    assert CALL["messages"][-1]["content"] == [{"type": "text", "text": "facts"}]      # the caller's list is untouched


def test_a_plain_text_call_only_gets_headroom_and_effort():
    kw, name = cma_claude.prepare(dict(model="claude-opus-5-5", max_tokens=3000, system="s",
                                       messages=[{"role": "user", "content": "rewrite"}]), "low")
    assert name is None and kw["max_tokens"] == 9000 and kw["output_config"] == {"effort": "low"} and "tool_choice" not in kw


class _Client:
    def __init__(self, responses):
        self.calls, self._responses = [], list(responses)
        self.messages = NS(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


def _resp(tool=None, text="", stop="end_turn", i=100, o=10):
    content = [NS(type="tool_use", name=tool, input={"ok": True})] if tool else [NS(type="text", text=text)]
    return NS(content=content, stop_reason=stop, usage=NS(input_tokens=i, output_tokens=o))


def test_the_model_is_asked_once_more_when_the_tool_call_did_not_come():
    client = _Client([_resp(text="Here is my analysis..."), _resp(tool="submit_cma_analysis", i=120, o=30)])
    resp = asyncio.run(cma_claude.create(client, **dict(CALL, model="claude-opus-5-5")))
    assert len(client.calls) == 2 and client.calls[1]["messages"][-1]["content"] == "Now call submit_cma_analysis with your result."
    assert resp.content[0].name == "submit_cma_analysis"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (220, 40)       # both attempts are booked


def test_one_call_when_the_tool_call_comes_first_time():
    client = _Client([_resp(tool="submit_cma_analysis")])
    asyncio.run(cma_claude.create(client, **dict(CALL, model="claude-fable-5-2")))
    assert len(client.calls) == 1


def test_final_value_step_follows_the_model_name():
    assert svc._reasoning_is_claude({"cma_reasoning_model": "claude-fable-5-2"})
    assert svc._reasoning_is_claude({"cma_reasoning_model": "claude-opus-5-5"})
    assert not svc._reasoning_is_claude({"cma_reasoning_model": "gpt-6-astra"})


def test_final_value_answer_is_read_from_plain_or_wrapped_json():
    assert svc._reasoning_json_from_text('{"final_fmv": 500000}') == {"final_fmv": 500000}
    assert svc._reasoning_json_from_text('Here it is:\n```json\n{"final_fmv": 500000}\n```') == {"final_fmv": 500000}
    assert svc._reasoning_json_from_text("no json here") is None


def test_every_claude_call_in_the_engine_goes_through_the_helper():
    src = (root_dir / "app" / "services" / "cma_ai_service.py").read_text(encoding="utf-8")
    assert "client.messages.create(" not in src.replace("_cma_claude.create(client", "")


# ---- the slower, half-price line (testing only, 11/15) --------------------------------------------------------------

class _Queue:
    """A stand-in batch queue: `outcomes` are what each 'is it done yet?' answers, then one result row."""
    def __init__(self, states, result):
        self.states, self.result, self.created, self.cancelled = list(states), result, [], []

    async def create(self, requests):
        self.created.append(requests)
        return NS(id="batch_1")

    async def retrieve(self, batch_id):
        return NS(processing_status=self.states.pop(0) if self.states else "ended")

    async def cancel(self, batch_id):
        self.cancelled.append(batch_id)

    async def results(self, batch_id):
        async def rows():
            yield NS(result=self.result)
        return rows()


def _queued_client(states, result, fallback=None):
    c = _Client([fallback] if fallback is not None else [])
    c.messages.batches = _Queue(states, result)
    return c


async def _no_sleep(_):
    return None


def test_the_slow_line_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("CMA_SLOW_LANE", raising=False)
    c = _queued_client(["ended"], NS(type="succeeded", message=_resp(text="queued")), fallback=_resp(text="now"))
    r = asyncio.run(cma_claude.create(c, model="claude-sonnet-5-5", max_tokens=100, messages=[{"role": "user", "content": "x"}]))
    assert r.content[0].text == "now" and c.messages.batches.created == [] and len(c.calls) == 1


def test_switched_on_the_request_goes_through_the_queue_at_half_the_tokens(monkeypatch):
    monkeypatch.setenv("CMA_SLOW_LANE", "1")
    c = _queued_client(["in_progress", "ended"], NS(type="succeeded", message=_resp(text="queued", i=1001, o=40)))
    r = asyncio.run(cma_claude.through_the_queue(c, {"model": "claude-sonnet-5-5", "max_tokens": 100,
                                                     "messages": [{"role": "user", "content": "x"}]}, sleep=_no_sleep))
    assert r.content[0].text == "queued" and c.calls == []                 # nothing was sent the normal way
    assert (r.usage.input_tokens, r.usage.output_tokens) == (501, 20)       # half price = half the tokens, rounded up
    sent = c.messages.batches.created[0][0]["params"]
    assert sent["model"] == "claude-sonnet-5-5" and sent["messages"][0]["content"] == "x"


def test_a_request_the_queue_cannot_answer_is_sent_the_normal_way(monkeypatch):
    monkeypatch.setenv("CMA_SLOW_LANE", "1")
    for kind in ("errored", "expired", "canceled"):
        c = _queued_client(["ended"], NS(type=kind), fallback=_resp(text="now", i=100, o=10))
        r = asyncio.run(cma_claude.create(c, model="claude-sonnet-5", max_tokens=100, messages=[{"role": "user", "content": "x"}]))
        assert r.content[0].text == "now" and r.usage.input_tokens == 100   # the normal way, at the normal price


def test_a_request_still_waiting_at_the_time_limit_is_cancelled_and_sent_the_normal_way(monkeypatch):
    monkeypatch.setenv("CMA_SLOW_LANE", "1")
    monkeypatch.setenv("CMA_SLOW_LANE_CAP_MIN", "1")
    c = _queued_client(["in_progress"] * 50, NS(type="succeeded", message=_resp(text="late")))
    ticks = iter(range(0, 100000, 40))                                      # each look at the clock is 40 seconds later
    r = asyncio.run(cma_claude.through_the_queue(c, {"model": "m", "max_tokens": 1, "messages": []},
                                                 sleep=_no_sleep, clock=lambda: next(ticks)))
    assert r is None and c.messages.batches.cancelled == ["batch_1"]


def test_a_client_with_no_queue_is_always_sent_the_normal_way(monkeypatch):
    monkeypatch.setenv("CMA_SLOW_LANE", "1")                                # the accuracy test's stub client
    c = _Client([_resp(text="now")])
    r = asyncio.run(cma_claude.create(c, model="claude-sonnet-5", max_tokens=100, messages=[{"role": "user", "content": "x"}]))
    assert r.content[0].text == "now"


def test_the_final_value_step_tries_the_queue_first():
    src = (root_dir / "app" / "services" / "cma_ai_service.py").read_text(encoding="utf-8")
    i = src.index("async def _call_cma_reasoning_claude")
    body = src[i:i + 4000]
    assert body.index("_cma_claude.through_the_queue(client, kwargs)") < body.index("messages.stream(**kwargs)")

