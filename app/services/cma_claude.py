"""Claude calls for the CMA engine that work on both the older and the current models (11/13).

Sonnet 5.5, Opus 5.5 and Fable 5.x changed three things the engine's calls relied on:
  - a FORCED tool choice ({"type": "tool" | "any"}) is rejected with a 400. The call is sent with
    tool_choice "auto" plus a plain instruction to answer with that tool, and is asked once more if the
    tool call didn't come;
  - thinking is always on and counts toward max_tokens, so every call gets headroom for it (a small
    rewrite call with max_tokens=2500 would otherwise be cut off mid-answer);
  - effort is set explicitly (Opus 5.5 defaults to medium, Sonnet 5.5 to high).
Older models (Sonnet 5, Haiku) go through unchanged.

Non-streaming on purpose: the accuracy-test dry run swaps in a stub client that only implements
messages.create.
"""
import asyncio
import logging
import math
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---- The slower, half-price line (TESTING ONLY - owner, 11/15) -------------------------------------------------
# "If we use the take-your-time AI we are using with blogs, would we save a lot of money? Maybe we do that for all
# testing." With CMA_SLOW_LANE=1 every CMA request is sent through Anthropic's batch queue - the same request, the
# same model, the same answer, at half the price (measured on the blog writer 11/14: $0.53 against $1.08-1.23) -
# and the engine waits for it. A report then takes many minutes instead of two or three, which a test batch does
# not mind. Off by default and never set on the live site: an agent's report is always sent the normal way.
#   - one request per batch, asked about every 15-30 seconds;
#   - a request still waiting after CMA_SLOW_LANE_CAP_MIN minutes (25) is cancelled and sent the normal way;
#   - a request the queue could not answer (an error, expired) is sent the normal way, so the usual handling of
#     that error runs unchanged;
#   - an answer that came through the queue has its token counts HALVED before it is handed back, so every cost
#     the engine records for it is what it really cost. (The counts in the usage log therefore read half under
#     this switch. A real agent-facing version would keep its own tally, as the blog writer does.)


def slow_lane_on() -> bool:
    return os.getenv("CMA_SLOW_LANE") == "1"


def _cap_seconds() -> float:
    try:
        return max(60.0, float(os.getenv("CMA_SLOW_LANE_CAP_MIN", "25")) * 60)
    except ValueError:
        return 1500.0


def _plain(o):
    """Request parameters as plain JSON (an earlier answer's content blocks, sent back on a second ask, included)."""
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json", exclude_unset=True)
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    return o


def _halve_usage(message) -> None:
    usage = getattr(message, "usage", None)
    for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        n = getattr(usage, field, None)
        if isinstance(n, (int, float)) and n:
            try:
                setattr(usage, field, int(math.ceil(n / 2)))
            except Exception:  # noqa: BLE001 - a usage object that can't be changed over-reports this one call
                pass


async def through_the_queue(client, kwargs: dict, *, sleep=asyncio.sleep, clock=time.monotonic, cap_seconds: Optional[float] = None):
    """Send one request through the batch queue and wait for it. The message (usage halved), or None when the
    switch is off, the client has no queue (the accuracy test's stub), or the queue did not answer - the caller
    then sends the request the normal way."""
    batches = getattr(getattr(client, "messages", None), "batches", None)
    # cap_seconds: an agent chose the slower, half-price way for this request (video "Save usage", 11/16) - it goes
    # through the queue whatever the testing switch says, and is sent the normal way after that long
    if not (cap_seconds or slow_lane_on()) or batches is None:
        return None
    params = _plain({k: v for k, v in kwargs.items() if k != "extra_body"})
    params.update(_plain(kwargs.get("extra_body") or {}))
    try:
        batch = await batches.create(requests=[{"custom_id": "cma", "params": params}])
    except Exception:  # noqa: BLE001
        logger.warning("CMA slow lane: could not queue the request - sending it the normal way", exc_info=True)
        return None
    began, cap = clock(), (float(cap_seconds) if cap_seconds else _cap_seconds())
    while True:
        try:
            state = await batches.retrieve(batch.id)
        except Exception:  # noqa: BLE001 - a dropped connection: ask again
            state = None
        if state is not None and getattr(state, "processing_status", None) == "ended":
            break
        waited = clock() - began
        if waited > cap:
            try:
                await batches.cancel(batch.id)
            except Exception:  # noqa: BLE001 - an answer that still arrives is never read
                pass
            logger.info("CMA slow lane: still waiting after %.0f minutes - sending it the normal way", cap / 60)
            return None
        await sleep(8 if cap_seconds and waited < 300 else 15 if waited < 180 else 30)
    try:
        async for row in await batches.results(batch.id):
            result = getattr(row, "result", None)
            if getattr(result, "type", None) == "succeeded":
                _halve_usage(result.message)
                logger.info("CMA slow lane: answered after %.0f s", clock() - began, extra={"model": params.get("model")})
                return result.message
            logger.warning("CMA slow lane: the queue did not answer (%s) - sending it the normal way", getattr(result, "type", None))
            return None
    except Exception:  # noqa: BLE001
        logger.warning("CMA slow lane: could not read the answer - sending it the normal way", exc_info=True)
    return None


async def _send(client, kw: dict, direct: bool = False, slow_cap: Optional[float] = None):
    msg = None if direct else await through_the_queue(client, kw, cap_seconds=slow_cap)
    if msg is not None:
        return msg
    resp = await client.messages.create(**kw)
    if slow_cap and not direct:
        # the agent chose Save usage (video, 11/17): the half price was promised, so the usage counted is halved even
        # though the queue didn't answer in time and this went the normal way (the platform pays the difference)
        logger.info("save usage request sent the normal way - counted at half price")
        _halve_usage(resp)
    return resp


# Models that reject a forced tool choice and always think.
NEW_CLAUDE_PREFIXES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-fable-5-2",
                       "claude-mythos-5-1", "claude-mythos-5-2")
THINKING_HEADROOM_TOKENS = 6000
DEFAULT_EFFORT = "medium"


def is_new_claude(model) -> bool:
    return any(str(model or "").startswith(p) for p in NEW_CLAUDE_PREFIXES)


def tool_instruction(name: str) -> str:
    return f"Reply only by calling the `{name}` tool, exactly once - no other text."


def prepare(kwargs: dict, effort: Optional[str] = None) -> tuple[dict, Optional[str]]:
    """The request as it must be sent to `kwargs['model']`, and the tool it must end in (None when the
    original call forced none). Pure - the request-shape tests call this."""
    if not is_new_claude(kwargs.get("model")):
        return dict(kwargs), None
    kw = dict(kwargs)
    kw["max_tokens"] = int(kw.get("max_tokens") or 4000) + THINKING_HEADROOM_TOKENS
    kw["output_config"] = {**(kw.get("output_config") or {}), "effort": effort or DEFAULT_EFFORT}
    kw.pop("temperature", None)
    choice = kw.get("tool_choice")
    if not (isinstance(choice, dict) and choice.get("type") in ("tool", "any")):
        return kw, None
    name = choice.get("name") or (kw.get("tools") or [{}])[0].get("name")
    kw["tool_choice"] = {"type": "auto"}
    messages = [dict(m) for m in kw.get("messages") or []]
    if messages:
        content = messages[-1].get("content")
        note = tool_instruction(name)
        messages[-1]["content"] = (f"{content}\n\n{note}" if isinstance(content, str)
                                   else [*(content or []), {"type": "text", "text": note}])
    kw["messages"] = messages
    return kw, name


def _has_tool_call(resp, name: str) -> bool:
    return any(getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == name for b in (resp.content or []))


async def create(client, *, effort: Optional[str] = None, direct: bool = False, slow_cap: Optional[float] = None, **kwargs):
    """client.messages.create(**kwargs) for any Claude model. On a current model, a call that forced a tool
    is asked once more when the tool call didn't come; the returned response's usage is the total spent.
    direct=True: never through the testing-only half-price queue - for a step with seconds to answer (the photo
    sorter), where a queued request would always time out."""
    kw, name = prepare(kwargs, effort)
    resp = await _send(client, kw, direct, slow_cap)
    if not name or _has_tool_call(resp, name) or getattr(resp, "stop_reason", None) in ("max_tokens", "refusal"):
        return resp
    spent_in, spent_out = int(resp.usage.input_tokens or 0), int(resp.usage.output_tokens or 0)
    kw["messages"] = [*kw["messages"], {"role": "assistant", "content": resp.content},
                      {"role": "user", "content": f"Now call {name} with your result."}]
    resp = await _send(client, kw, direct)   # (the second ask is short: never queued)
    try:
        resp.usage.input_tokens = int(resp.usage.input_tokens or 0) + spent_in
        resp.usage.output_tokens = int(resp.usage.output_tokens or 0) + spent_out
    except Exception:  # noqa: BLE001 - a usage object that can't be updated only under-reports this one retry
        pass
    return resp
