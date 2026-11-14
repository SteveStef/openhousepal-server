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
