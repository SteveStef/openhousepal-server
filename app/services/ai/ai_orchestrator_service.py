"""
Manual tool-use loop for the AI Assistant against the Anthropic Messages API.

Deliberately not the beta SDK Tool Runner and not Managed Agents (which grants
filesystem/bash access - the wrong trust model for this feature). This loop
owns every security-relevant budget explicitly and auditably: max iterations,
max tool calls per turn, and a wall-clock timeout. See the implementation plan.

Every call to run_turn() either returns (text, record_refs, usage) or raises
AIOrchestratorError carrying whatever usage was accumulated before the failure -
the caller (ai_routes.py) logs an AIUsageEvent either way.
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import User
from app.services.ai import ai_tools
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_prompt_service import SYSTEM_PROMPT

logger = get_logger(__name__)

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # resolves ANTHROPIC_API_KEY from env
    return _client


class AIOrchestratorError(Exception):
    """A safe-to-show-the-user failure (budget exceeded, timeout, upstream error).
    `reason` is logged to AIUsageEvent.block_reason. `user_message` is generic and
    safe to show verbatim - never include upstream API details in it. `usage`
    carries whatever token/tool-call counts were accumulated before the failure,
    so even a failed turn is still logged with real numbers where available."""

    def __init__(self, reason: str, user_message: str, usage: Optional[dict] = None):
        self.reason = reason
        self.user_message = user_message
        self.usage = usage or {}
        super().__init__(reason)


# Tool-call arg keys that identify a specific OpenHousePal record, used to build the
# "why" transparency links the frontend renders next to an assistant reply. Pulled
# from the tool call itself rather than asked of the model, so it's always accurate.
_RECORD_ARG_KEYS = {
    "lead_id": "lead",
    "listing_key": "property",
    "open_house_id": "open_house",
    "report_id": "cma",
}


def _extract_record_refs(tool_name: str, args: dict) -> List[dict]:
    refs = []
    for key, record_type in _RECORD_ARG_KEYS.items():
        value = args.get(key)
        if value:
            refs.append({"tool": tool_name, "record_type": record_type, "record_id": value})
    return refs


def _extract_result_refs(tool_name: str, result: Any) -> List[dict]:
    """Refs pulled from what a tool RETURNED, not just what the model passed in - so a
    showcase or a specific matched property gets a "why" link even though the model never
    passed their ids as arguments (e.g. get_property_matches(lead_id=...) returns properties
    the model didn't ask for by listing_key). Capped so one tool call can't flood a reply
    with tags."""
    if not isinstance(result, dict):
        return []
    refs = []
    collection_id = result.get("collection_id")
    if collection_id:
        refs.append({"tool": tool_name, "record_type": "showcase", "record_id": collection_id})
    for item in (result.get("results") or [])[:3]:
        if isinstance(item, dict) and item.get("listing_key"):
            refs.append({"tool": tool_name, "record_type": "property", "record_id": item["listing_key"]})
    if tool_name == "get_priority_summary":
        # get_priority_summary's items carry a server-built `link` (from ai_insights_service.py,
        # never the model) rather than a bare id - parse the one shape that points at a single
        # record (/leads/{id}) so this tool gets the same "why" link chips search_leads/
        # get_property_matches already do, instead of the model naming a lead with no link at all.
        for item in (result.get("priorities") or [])[:3]:
            link = isinstance(item, dict) and item.get("link")
            match = link and re.fullmatch(r"/leads/([^/]+)", link)
            if match:
                refs.append({"tool": tool_name, "record_type": "lead", "record_id": match.group(1)})
    return refs


VOICE_MODE_BLOCK = {
    "type": "text",
    "text": (
        "The agent is talking to you by voice right now (spoken input, and/or the reply will be "
        "read aloud) - answer in ONE short, plain-spoken sentence or two, the way you'd actually "
        "say it out loud. No bullet lists, no bold/markdown, no restating every detail - lead with "
        "the direct answer (a count, a name, a yes/no) and stop. If there's more the agent would "
        "want, ask a short follow-up question instead of listing it all out. Example: instead of "
        "listing two properties with full specs, say \"There are two - one in Bryn Mawr and one in "
        "Villanova. Which one?\""
    ),
}


async def run_turn(
    db: AsyncSession,
    current_user: User,
    conversation_history: List[Dict[str, Any]],
    user_message: str,
    voice_mode: bool = False,
) -> Tuple[str, List[dict], Dict[str, Any]]:
    """Runs one user turn to completion: sends the message, executes any tool calls
    the model makes (bounded by config), and returns (assistant_text, record_refs, usage)."""
    config = await ai_config_service.get_config(db)
    client = _get_client()

    messages: List[dict] = list(conversation_history) + [{"role": "user", "content": user_message}]
    all_refs: List[dict] = []
    usage_state = {"model": config["model"], "input_tokens": 0, "output_tokens": 0, "tool_call_count": 0, "tool_names": []}

    async def _call_model():
        # Second system block is deliberately NOT cached (no cache_control) and rebuilt every
        # call - it's the model's only anchor for "now". Without it, the model has no way to
        # tell whether a tool-returned timestamp like "2026-08-05T09:15:00Z" is from this
        # morning or a week ago, and will guess - see ai_prompt_service.py's recency guidance.
        now_block = {
            "type": "text",
            "text": f"Current date and time: {datetime.now(timezone.utc):%A, %B %d, %Y %I:%M %p} UTC.",
        }
        system_blocks = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            now_block,
        ]
        if voice_mode:
            system_blocks.append(VOICE_MODE_BLOCK)
        return await client.messages.create(
            model=config["model"],
            max_tokens=config["max_output_tokens"],
            system=system_blocks,
            tools=ai_tools.TOOL_DEFINITIONS,
            messages=messages,
            **ai_config_service.model_options(config["model"], "medium"),
        )

    async def _run() -> str:
        for _ in range(config["max_iterations"]):
            response = await _call_model()
            usage_state["input_tokens"] += response.usage.input_tokens
            usage_state["output_tokens"] += response.usage.output_tokens
            # The cached system prompt is billed too (write 1.25x, read 0.1x/0.05x) but isn't in
            # input_tokens - counted separately so the meter matches Anthropic's bill.
            usage_state["cache_write_tokens"] = usage_state.get("cache_write_tokens", 0) + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
            usage_state["cache_read_tokens"] = usage_state.get("cache_read_tokens", 0) + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
                return text or "I wasn't able to come up with a response. Please try rephrasing your question."

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in tool_use_blocks:
                usage_state["tool_call_count"] += 1
                if usage_state["tool_call_count"] > config["max_tool_calls_per_turn"]:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"error": "budget_exceeded", "message": "Tool call budget for this turn is exhausted."}),
                        "is_error": True,
                    })
                    continue

                args = block.input if isinstance(block.input, dict) else {}
                usage_state["tool_names"].append(block.name)
                all_refs.extend(_extract_record_refs(block.name, args))
                result = await ai_tools.execute_tool(db, current_user, block.name, args, config)
                all_refs.extend(_extract_result_refs(block.name, result))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

            messages.append({"role": "user", "content": tool_results})

        return "I wasn't able to finish researching that within my turn budget. Try asking a more specific question, or split it into smaller questions."

    try:
        text = await asyncio.wait_for(_run(), timeout=config["request_timeout_seconds"])
    except asyncio.TimeoutError:
        raise AIOrchestratorError(
            "timeout", "That request took too long to process. Please try a simpler question.", usage=usage_state
        ) from None
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        logger.error("Anthropic API error: %s", e)
        raise AIOrchestratorError(
            "api_error", "The AI assistant is temporarily unavailable. Please try again shortly.", usage=usage_state
        ) from e
    except Exception as e:
        # Catch-all so a misconfiguration (e.g. missing ANTHROPIC_API_KEY) or any other
        # unexpected SDK-level failure fails safely with a generic message and a logged
        # AIUsageEvent, instead of leaking as an unhandled 500 with no audit trail.
        logger.exception("Unexpected AI orchestrator failure: %s", e)
        raise AIOrchestratorError(
            "unexpected_error", "The AI assistant is temporarily unavailable. Please try again shortly.", usage=usage_state
        ) from e

    usage_state["estimated_cost_usd"] = ai_config_service.estimate_cost_usd(
        config, usage_state["model"], usage_state["input_tokens"], usage_state["output_tokens"],
        usage_state.get("cache_write_tokens", 0), usage_state.get("cache_read_tokens", 0),
    )
    return text, all_refs, usage_state
