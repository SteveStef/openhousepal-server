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
from typing import Optional

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


async def create(client, *, effort: Optional[str] = None, **kwargs):
    """client.messages.create(**kwargs) for any Claude model. On a current model, a call that forced a tool
    is asked once more when the tool call didn't come; the returned response's usage is the total spent."""
    kw, name = prepare(kwargs, effort)
    resp = await client.messages.create(**kw)
    if not name or _has_tool_call(resp, name) or getattr(resp, "stop_reason", None) in ("max_tokens", "refusal"):
        return resp
    spent_in, spent_out = int(resp.usage.input_tokens or 0), int(resp.usage.output_tokens or 0)
    kw["messages"] = [*kw["messages"], {"role": "assistant", "content": resp.content},
                      {"role": "user", "content": f"Now call {name} with your result."}]
    resp = await client.messages.create(**kw)
    try:
        resp.usage.input_tokens = int(resp.usage.input_tokens or 0) + spent_in
        resp.usage.output_tokens = int(resp.usage.output_tokens or 0) + spent_out
    except Exception:  # noqa: BLE001 - a usage object that can't be updated only under-reports this one retry
        pass
    return resp
