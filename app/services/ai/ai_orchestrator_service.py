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
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import User
from app.services.ai import ai_actions, ai_showcase_cards, ai_tools, ai_ui_tools
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_prompt_service import SYSTEM_PROMPT

logger = get_logger(__name__)

_client: Optional[anthropic.AsyncAnthropic] = None


_ET = ZoneInfo("America/New_York")
_UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|\+00:00)$")


def _now_et() -> datetime:
    """The agent's wall clock - every Bright MLS agent is on Eastern time."""
    return datetime.now(_ET)


def _ago(then: datetime, now: datetime) -> str:
    s = (now - then).total_seconds()
    if s < 0:
        return "from now" if s < -3600 else "just now"
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hour{'s' if h > 1 else ''} ago"
    d = int(s // 86400)
    return f"{d} day{'s' if d > 1 else ''} ago"


def _eastern_times(v: Any, now: Optional[datetime] = None) -> Any:
    """Every UTC timestamp in a lookup result as the agent's own clock, with how long ago - the model
    did the conversion itself and told an agent a like from 9:50 PM was "2:50 AM UTC, about 15 hours
    ago" (11/02)."""
    now = now or _now_et()
    if isinstance(v, dict):
        return {k: _eastern_times(x, now) for k, x in v.items()}
    if isinstance(v, list):
        return [_eastern_times(x, now) for x in v]
    if isinstance(v, datetime) and v.tzinfo is not None:
        t = v.astimezone(_ET)
    elif isinstance(v, str) and _UTC_STAMP.match(v):
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(_ET)
        except ValueError:
            return v
    else:
        return v
    return f"{t:%a %b} {t.day} {t.year}, {t:%I:%M %p}".replace(" 0", " ") + f" ({_ago(t, now)})"


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
    for item in ((result.get("results") or result.get("matches")) or [])[:3]:
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


PLAN_NAMES = {"NO_MLS": "No-MLS Basic", "PRO_NO_MLS": "No-MLS Premium", "BASIC": "Basic", "PREMIUM": "Premium", "IDX": "IDX"}


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


# "Tap Confirm on the card", "the card below", "tap Save"... - a reply that says a card is there.
# "I'll check the sign-in lists..." as the LAST thing it says, with nothing done (owner, 11/01: waited
# for a search that never ran) - one nudge to actually do it this turn.
_PROMISE = re.compile(r"\b(I'?ll|I will|let me|I'?m going to|going to)\s+(now\s+)?(check(?!\s+(back|in)\b)|look|search|pull|find|go through|review|dig|scan)\b[^.?!]*[.…]?\s*$", re.I)

# What the chat shows while a reply is coming (owner, 11/01: "it should show that it's loading here and
# actually working"): saved per agent (app_cache - any worker can answer the poll), read by GET /ai/progress.
_STEP_WORDS = {
    "find_open_house_guests": "Searching your open house guests", "get_open_house_guests": "Reading the sign-in list",
    "search_open_houses": "Looking through your open houses", "search_leads": "Searching your leads", "get_lead_detail": "Opening their lead",
    "search_properties": "Searching the MLS", "get_property_detail": "Reading the listing", "look_up_agent": "Looking up that agent",
    "get_property_matches": "Matching homes", "get_showcase_info": "Opening the Showcase", "search_showcase_homes": "Reading every listing in the Showcase", "search_homes_for_sale": "Setting up a home search", "get_similar_properties": "Finding similar homes",
    "get_cma_summary": "Reading the CMA", "get_agent_activity_summary": "Reading your activity", "get_priority_summary": "Checking what needs you",
    "show_homes": "Pulling up the homes",
}


def _progress(user_id: str, label: Optional[str]) -> None:
    from app.services import app_cache
    app_cache.save_bg(f"ai_progress:{user_id}", {"label": label, "at": datetime.now(timezone.utc).isoformat()})


_CLAIMS_CARD = re.compile(r"\btap\s+(confirm|save|make it|the button)\b|\bon the card\b|\bcard (below|is waiting|is ready)\b|\bcard is already\b", re.I)


async def _my_listings_and_sales(db: AsyncSession, user: User) -> str:
    """Their own active listings (newest listed first) and recent sales, one line each - so "my newest
    listing" or "my last sale" is known without a lookup. Empty when they have no MLS ID - and on a
    No-MLS plan or before their broker approved them: this IS MLS data (same rule as the lookups)."""
    from app.utils.auth import tier_has_mls
    if not user.mls_id or not user.is_admin and (not tier_has_mls(user.plan_tier) or not user.broker_authorized):
        return ""
    from app.services.website import content_adapters as ca
    lines: list[str] = []
    try:
        listings = await ca.get_active_listings(db, user, limit=12)
        for i, l in enumerate(listings):
            dom = l.get("days_on_market")
            price = f"${float(l['price']):,.0f}" if l.get("price") else ""
            addr = ", ".join(x for x in (l.get("address") or l.get("street_address"), l.get("city")) if x)
            lines.append(f"- {addr} | {l.get('status') or ''} | {price} | {f'{dom} days on market' if dom is not None else 'days on market unknown'} | listing_key {l.get('listing_key')}"
                         + (" | NEWEST" if i == 0 else ""))
        listing_part = ("\nTheir active listings (newest listed first - the first is 'my newest listing'):\n" + "\n".join(lines)) if lines else "\nThey have no active listings right now."
        sales = await ca.get_recent_sales(db, user, limit=8)
        sale_lines = [f"- {s.get('address') if ',' in str(s.get('address') or '') else ', '.join(x for x in (s.get('address'), s.get('city')) if x)} | sold {str(s.get('sold_date') or s.get('close_date') or '')[:10] or '?'} | "
                      f"{('$' + format(float(s['price']), ',.0f')) if s.get('price') else ''} | listing_key {s.get('listing_key')}" for s in sales]
        sales_part = ("\nTheir recent sales (newest first):\n" + "\n".join(sale_lines)) if sale_lines else ""
        return (listing_part + sales_part + "\nUse these exact homes and listing keys - never another unit or a look-alike address. "
                "If the agent says it's the wrong home, pick from this list or ask which one; don't search again.")
    except Exception:  # noqa: BLE001 - the chat still works without it
        logger.warning("ai listings context failed", exc_info=True)
        await db.rollback()
        return ""


# Haiku 5.5 bills a whole request at 5x once its prompt passes 100K tokens, and the fixed part (guide +
# tools) is ~45K. On 11/04 a few turns that piled up big lookups hit 90K-195K and cost 10-15x a normal
# message (owner: "usage is going faster"). One lookup result is capped, and before each call the
# turn's older lookup results are cut down until the conversation part fits (~45K tokens).
_TOOL_RESULT_MAX_CHARS = 40_000
_CALL_BUDGET_CHARS = 180_000


def _fit_messages(messages: List[dict]) -> None:
    def size() -> int:
        # a photo's bytes are priced by the picture, not its text length
        return len(json.dumps([{**m, "content": [b for b in m["content"] if not (isinstance(b, dict) and b.get("type") == "image")]} if isinstance(m.get("content"), list) else m
                               for m in messages], default=str))
    total = size()
    if total <= _CALL_BUDGET_CHARS:
        return
    for m in messages[:-1]:  # the newest results are the ones it's answering from
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), str) and len(b["content"]) > 800:
                total -= len(b["content"]) - 450
                b["content"] = b["content"][:400] + " ... (shortened - already used earlier in this answer)"
                if total <= _CALL_BUDGET_CHARS:
                    return


async def run_turn(
    db: AsyncSession,
    current_user: User,
    conversation_history: List[Dict[str, Any]],
    user_message: str,
    voice_mode: bool = False,
    images: Optional[List[dict]] = None,
) -> Tuple[str, List[dict], Dict[str, Any]]:
    """Runs one user turn to completion: sends the message, executes any tool calls
    the model makes (bounded by config), and returns (assistant_text, record_refs, usage)."""
    config = await ai_config_service.get_config(db)
    client = _get_client()

    messages: List[dict] = list(conversation_history) + [{"role": "user", "content": [*images, {"type": "text", "text": user_message}] if images else user_message}]
    all_refs: List[dict] = []
    # One Showcase card per Showcase the reply read (ai_showcase_cards) - added under the reply at the end.
    showcase_cards: Dict[str, dict] = {}
    usage_state = {"model": config["model"], "input_tokens": 0, "output_tokens": 0, "tool_call_count": 0, "tool_names": []}
    # Lookups this plan gets + things it can set up for a Confirm tap (ai_actions.py, 10/30).
    from app.services.ai import ai_home_cards
    tools = ai_tools.tools_for(current_user) + ai_actions.action_tools_for(current_user) + ai_ui_tools.UI_TOOL_DEFINITIONS + [ai_home_cards.TOOL, ai_showcase_cards.MARK_TOOL]

    # The agent's own listings and sales, known up front (owner, 10/30: it guessed "your newest listing"
    # from days on market, then lost it on a follow-up) - newest first, with each one's listing key so
    # an action uses that exact home (a building has many units).
    mine_text = await _my_listings_and_sales(db, current_user)

    async def _call_model():
        _fit_messages(messages)
        # Second system block is deliberately NOT cached (no cache_control) and rebuilt every
        # call - it's the model's only anchor for "now". Without it, the model has no way to
        # tell whether a tool-returned timestamp like "2026-08-05T09:15:00Z" is from this
        # morning or a week ago, and will guess - see ai_prompt_service.py's recency guidance.
        now_block = {
            "type": "text",
            # Eastern, the agent's own clock (every Bright MLS agent is on it) - in UTC the helper told
            # an agent at 9:50 PM Monday that it was Tuesday, and read a like from 9:50 PM as "2:50" (11/02).
            "text": f"Current date and time for the agent: {_now_et():%A, %B %d, %Y %I:%M %p} Eastern time (UTC{_now_et():%z}). "
                    "Times in lookup results are already on their clock with how long ago (“Mon Nov 2 2026, 9:50 PM (2 hours ago)”) - use them as they are; "
                    "any left ending in Z or +00:00 are UTC - convert those. Just say the time (“at 9:50 PM”, “earlier tonight”, “yesterday” - today's evening is “tonight”, never “last night”); "
                    "never write “UTC” or “Eastern”, and never open an answer by stating today's date or what “this week” covers.\n"
                    # Their plan, so how-to answers never offer something it doesn't include (10/30).
                    f"This agent is on the {PLAN_NAMES.get(current_user.plan_tier, current_user.plan_tier)} plan"
                    f"{'' if current_user.broker_authorized else ' and their broker has NOT approved them yet (Broker Authorization in the top menu) - MLS features stay locked until then'}. "
                    "Only suggest features this plan includes (see the plan table in the guide); for anything else, say which plan has it. "
                    + (" This is a No-MLS plan: it has NO home/MLS data at all - no listing searches, comps, Showcases, CMAs, Discovery or Manager. "
                       "Never offer or describe those as theirs, never say listings will 'come from the MLS'; their own homes and sales are only the ones they typed in. "
                       if current_user.plan_tier in ("NO_MLS", "PRO_NO_MLS") else "")
                    + f"The agent is {current_user.first_name} {current_user.last_name}. Never decide yourself whether a listing or sale is theirs from names in lookups (MLS names often differ, e.g. middle names) - call the action tool; it checks by MLS ID and tells you. "
                    "A Confirm card exists ONLY when you call an action tool in this turn - never type a card, “[Card: …]” or “tap Confirm” without calling the tool. "
                    "AI usage is ONLY ever a percent of their monthly allowance - never say a dollar amount, cost or the allowance's size. "
                    "For 'what should I do / anything I should do', call get_things_to_do and answer from it in that order (overdue follow-ups first, then what's waiting on them) - at most 3 things, one short line each, and a show_link button to the first one's page (/leads/tasks for follow-ups); unseen Showcase matches are never the answer. "
                    "Don't keep bringing up an earlier topic (a wrong design, an old alert) once it's been answered."
                    + mine_text,
        }
        system_blocks = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            now_block,
        ]
        if voice_mode:
            system_blocks.append(VOICE_MODE_BLOCK)
        # A whole-Showcase check answers with a list of homes - room for it, or the reply stopped
        # mid-address (11/08, 100 homes). Only after that tool ran, and only in voice-free replies.
        long_list = not voice_mode and any(n in usage_state["tool_names"] for n in ("search_showcase_homes", "search_homes_for_sale"))
        return await client.messages.create(
            model=config["model"],
            max_tokens=config["max_output_tokens"] * (2 if long_list else 1),
            system=system_blocks,
            tools=tools,
            messages=messages,
            **ai_config_service.model_options(config["model"], "medium"),
        )

    nudged = False
    promised = False
    listed = False
    homes_nudged = False
    seen_homes: dict = {}  # listing_key -> "602 holywell" for every home a lookup returned this turn
    _progress(current_user.id, "Thinking")

    async def _run() -> str:
        nonlocal nudged, promised, listed, homes_nudged
        for _ in range(config["max_iterations"]):
            response = await _call_model()
            usage_state["input_tokens"] += response.usage.input_tokens
            usage_state["output_tokens"] += response.usage.output_tokens
            # The cached system prompt is billed too (write 1.25x, read 0.1x/0.05x) but isn't in
            # input_tokens - counted separately so the meter matches Anthropic's bill.
            usage_state["cache_write_tokens"] = usage_state.get("cache_write_tokens", 0) + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
            usage_state["cache_read_tokens"] = usage_state.get("cache_read_tokens", 0) + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
            # Priced per call: Haiku 5.5's long-prompt rate card goes by ONE request's prompt
            # length. Pricing the turn's summed tokens put any turn with a lookup (2 calls x ~60K)
            # over 100K and on the 5x card (11/01 probe: $0.0099 logged for a $0.0020 turn).
            usage_state["estimated_cost_usd"] = usage_state.get("estimated_cost_usd", 0.0) + ai_config_service.estimate_cost_usd(
                config, usage_state["model"], response.usage.input_tokens, response.usage.output_tokens,
                getattr(response.usage, "cache_creation_input_tokens", 0) or 0, getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            )

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
                # The homes are on the Showcase card - a reply that lists them anyway gets one nudge to
                # mark the not-real ones on the card and say it short (owner, 11/08: never a text list).
                if showcase_cards and not listed and ai_showcase_cards.lists_homes(text) and usage_state["tool_call_count"] < config["max_tool_calls_per_turn"] - 1:
                    listed = True
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": ai_showcase_cards.LIST_NUDGE})
                    _progress(current_user.id, "Tidying the answer")
                    continue
                if not promised and _PROMISE.search((text or "").strip()[-220:]) and usage_state["tool_call_count"] < config["max_tool_calls_per_turn"] - 1:
                    promised = True
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": "(App note: you said you'd look but didn't - do it now with your tools in this same turn (find_open_house_guests searches every sign-in list at once), then answer. Never end a reply on a promise to do something later.)"})
                    _progress(current_user.id, "Still looking")
                    continue
                # In a long chat it sometimes says "tap Confirm on the card" without making one
                # (owner's test, 11/01) - one nudge to make the real card, then whatever it says.
                # ("...on the card" about the Home search card the last reply showed is true - no nudge.)
                if not nudged and _CLAIMS_CARD.search(text or "") and not showcase_cards and "Home search card" not in user_message and not any(r.get("record_type") in ("action", "homes", "hosting", "home_search", "showcase_homes") for r in all_refs):
                    # (11/09: a fresh chat's Home search card wasn't counted, so the reminder made it say
                    # "the search card is already on your reply from my last message")
                    nudged = True
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": "(App note: no card was made this turn - nothing shows for them to tap. Call the action tool now to set it up, then reply in one short line. If you still need an answer from them first, ask only that.)"})
                    continue
                # Homes it names show as property cards, never a text list (owner, 11/09). Two or more
                # written out -> one nudge to pick them with show_homes and say it short; one home named
                # -> its card is added under the reply.
                # (11/09 test: it made the cards AND wrote all 9 out above them - a reply with cards that
                # still lists 3+ homes gets one nudge to say it in a line or two.)
                other_cards = ("showcase_homes", "home_search", "hosting")
                has_cards = any(r.get("record_type") == "homes" for r in all_refs)
                if seen_homes and text and not showcase_cards and not any(r.get("record_type") in other_cards for r in all_refs):
                    named = ai_home_cards.mentioned(text, seen_homes)
                    if len(named) >= (3 if has_cards else 2) and not homes_nudged and usage_state["tool_call_count"] < config["max_tool_calls_per_turn"] - 1:
                        homes_nudged = True
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append({"role": "user", "content": ai_home_cards.SHORTEN if has_cards else ai_home_cards.NUDGE})
                        _progress(current_user.id, "Tidying the answer")
                        continue
                    if named and not has_cards:
                        _, entry = await ai_home_cards.run(db, current_user, {"which": "homes", "listing_keys": named[:40]})
                        if entry:
                            all_refs.append(entry)
                return text or "I wasn't able to come up with a response. Please try rephrasing your question."

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})
            said = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
            # Only buttons (choices / Go there) on a reply that's already written: show them with
            # it and stop - another model call would just repeat the text.
            only_ui = said and all(b.name in ai_ui_tools.UI_TOOL_NAMES or b.name == ai_home_cards.NAME for b in tool_use_blocks)

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
                _progress(current_user.id, _STEP_WORDS.get(block.name) or ("Setting up the card" if ai_actions.is_action(block.name) else "Looking that up"))
                if block.name == ai_showcase_cards.MARK_TOOL["name"]:
                    # Matches that aren't real ("a pool table") - shown apart on this reply's card instead of listed in text.
                    result = ai_showcase_cards.mark_doubtful(showcase_cards, args)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)})
                    continue
                if block.name == ai_home_cards.NAME:
                    # Their listings / sales as small home cards under the reply (owner, 11/01).
                    result, entry = await ai_home_cards.run(db, current_user, args)
                    if entry:
                        # one card per kind of list a reply (asked again after a nudge -> it replaces the first)
                        all_refs[:] = [r for r in all_refs if not (r.get("record_type") == "homes" and r.get("which") == entry.get("which"))]
                        all_refs.append(entry)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)})
                    continue
                if block.name in ai_ui_tools.UI_TOOL_NAMES:
                    # Choices / "Go there" buttons shown under the reply - nothing to look up or change.
                    result, entry = ai_ui_tools.run(block.name, args)
                    if entry:
                        all_refs.append(entry)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    continue
                if ai_actions.is_action(block.name):
                    # Only SETS IT UP - the agent's Confirm tap on the card runs it.
                    usage_state["actions"] = usage_state.get("actions", 0) + 1
                    if usage_state["actions"] > ai_actions.MAX_ACTIONS_PER_TURN:
                        result, entry = {"error": "too_many", "message": "Set up at most 3 things at once - ask the agent to confirm these first."}, None
                    else:
                        result, entry = await ai_actions.propose(db, current_user, block.name, args)
                    if entry:
                        all_refs.append(entry)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)})
                    continue
                result = await ai_tools.execute_tool(db, current_user, block.name, args, config)
                if isinstance(result, (dict, list)) and block.name not in ai_showcase_cards.CARD_TOOLS:
                    ai_home_cards.collect(result, seen_homes)
                # The AI reading of listings a tool did (ai_home_reader) counts on this answer's usage.
                if isinstance(result, dict) and isinstance(result.get("_ai_cost"), dict):
                    spent = result.pop("_ai_cost")
                    usage_state["estimated_cost_usd"] = usage_state.get("estimated_cost_usd", 0.0) + float(spent.get("usd") or 0)
                    usage_state["input_tokens"] += int(spent.get("in") or 0)
                    usage_state["output_tokens"] += int(spent.get("out") or 0)
                # "Homes for sale in <town> with ..." shows the Home search card (ai_home_search).
                # One card per reply: a second call (it asked again with a change) replaces the first.
                if block.name == "search_homes_for_sale" and isinstance(result, dict) and isinstance(result.get("card"), dict):
                    all_refs[:] = [r for r in all_refs if r.get("record_type") != "home_search"]
                    all_refs.append(result.pop("card"))
                # An id it guessed wrong (the tool said not found) isn't kept - the next message is told
                # the ids this reply used (ai_routes._ids_note), so a wrong one would be repeated.
                if not (isinstance(result, dict) and result.get("error")):
                    all_refs.extend(_extract_record_refs(block.name, args))
                all_refs.extend(_extract_result_refs(block.name, result))
                if block.name in ai_showcase_cards.CARD_TOOLS and isinstance(result, dict) and not result.get("error"):
                    result = await _showcase_card(db, current_user, block.name, args, result, showcase_cards)
                # Homes to host come back under the reply as the Dash's Discovery cards (owner, 11/01:
                # "just like as if I was on the discovery tab" - newest first, each with its weekend
                # calendar and Request to host, See more, Browse all of Discovery). The model sums up.
                if block.name == "find_homes_to_host" and isinstance(result, dict):
                    keys = result.pop("card_keys", None) or []
                    if keys and result.get("homes"):
                        all_refs.append({"tool": block.name, "record_type": "hosting", "record_id": str(uuid.uuid4()),
                                         "listing_keys": keys, "search_area": result.get("search_area")})
                        result = {**result, "shown_as_cards": True, "note": (
                            "These homes already show under your reply as Discovery cards, newest first, each with its weekend calendar "
                            "and a Request to host button (that opens the calendar to pick a day and time), plus See more and Browse all of "
                            "Discovery - so never list them in text. Reply in two short lines: what their Discovery filter is (search_area) "
                            "and how many homes match, then that they can tap Request to host on any of them, or tell you a different "
                            "area, price or beds to change their Discovery filter and see other homes.")}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(_eastern_times(result), default=str),
                })

            if only_ui:
                said = _no_narration(said)
                if said:
                    return said
                # it only narrated the cards ("I'll show them as cards") - one more call writes the real line
            for tr in tool_results:
                if isinstance(tr.get("content"), str) and len(tr["content"]) > _TOOL_RESULT_MAX_CHARS:
                    tr["content"] = tr["content"][:_TOOL_RESULT_MAX_CHARS] + ' ... (cut to fit - ask about one item for the rest)'
            messages.append({"role": "user", "content": tool_results})

        return "I wasn't able to finish researching that within my turn budget. Try asking a more specific question, or split it into smaller questions."

    try:
        text = await asyncio.wait_for(_run(), timeout=config["request_timeout_seconds"])
    except asyncio.TimeoutError:
        raise AIOrchestratorError(
            "timeout", "That took longer than one answer is allowed. Try asking about fewer things at once, or one lead or home at a time.", usage=usage_state
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

    usage_state.setdefault("estimated_cost_usd", 0.0)  # summed per call in _run
    for cid, card in showcase_cards.items():
        # A "Remove homes" card for the same Showcase already lists every home - one list, not two.
        if not any(r.get("tool") == "remove_showcase_homes" and (r.get("payload") or {}).get("collection_id") == cid for r in all_refs):
            all_refs.append(card)
    _howto_button(user_message, all_refs)
    # Word matches only (not enough chat usage left for AI reading): always said, never left to the model.
    if any(c.get("words_only") for c in showcase_cards.values()) and not re.search(r"chat usage|allowance", text or "", re.I):
        text = ("You're almost out of chat usage this month, so this was a quick word search instead of AI reading every listing - "
                "some homes may be missed.\n\n" + (text or ""))
    return _no_pretend_cards(text, all_refs), all_refs, usage_state


async def _showcase_card(db, user, tool: str, args: dict, result: dict, cards: Dict[str, dict]) -> dict:
    """A tool read a client's Showcase: its homes show as the Showcase card under the reply (owner,
    11/08: never a text list), and the model is told so. Several reads of the same Showcase in one
    reply ("a pool or a finished basement") make one card with every match."""
    cid = result.get("collection_id") or args.get("collection_id")
    if not cid:
        return result
    searched = tool == "search_showcase_homes"
    matched = ai_showcase_cards.matched_from(result) if searched else None
    try:
        card = cards.get(cid)
        if card is None:
            card = await ai_showcase_cards.card_ref(db, user, cid, matched, result.get("looked_for") if searched else None)
            if card is None:
                return result
            cards[cid] = card
        elif searched:
            card["searched"] = True
            for pid, why in (matched or {}).items():
                card["matched"].setdefault(pid, why)
            card["looked_for"] = list(dict.fromkeys((card.get("looked_for") or []) + (result.get("looked_for") or [])))[:12]
        # The AI's "maybe" homes (only a hint) show greyed, last, with why.
        if result.get("ai_read") is False:
            card["words_only"] = True   # not enough chat usage left for AI reading - the card says so
        for m in result.get("matches") or []:
            if m.get("maybe") and m.get("property_id"):
                card.setdefault("doubtful", {})[m["property_id"]] = m["maybe"]
    except Exception:  # noqa: BLE001 - the answer still works without its card
        logger.warning("showcase card failed", exc_info=True)
        return result
    name = card["collection"]["client"] or "their"
    note = ai_showcase_cards.CARD_NOTE.format(name=name, what="the matches first with why each matched" if searched else "newest first",
                                              count_word="matched" if searched else "homes are in it")
    if searched:
        note += (" Use judgment on each match's quote: 'could add a bedroom', 'no pool', a lower-level in-law suite or a builder's other "
                 "floor plans aren't real matches - say so in a few words. A home that isn't a match only didn't mention it. If they "
                 "then want the others taken out, use remove_showcase_homes with these matches' listing_keys as keep_listing_keys.")
    total = len(card["homes"])
    note += (f" The Showcase has {total} home{'s' if total != 1 else ''} in all right now (the card shows every one) - when you say how "
             "many are in it, use that number, never the length of a list above.")
    # The card has every match - the model only needs a few to name, not all of them (saves chat usage).
    trimmed = {**result}
    if searched and len(result.get("matches") or []) > 8:
        trimmed["matches"] = [m for m in result["matches"] if not m.get("maybe")][:6] + [m for m in result["matches"] if m.get("maybe")][:2]
        trimmed["more_on_the_card"] = len(result["matches"]) - len(trimmed["matches"])
    if trimmed.get("matches"):   # the highlight marks are for the card only
        from app.services.ai.ai_home_reader import plain
        trimmed["matches"] = [{**m, "found": [plain(f) for f in m.get("found") or []]} if m.get("found") else m for m in trimmed["matches"]]
    return {**trimmed, "shown_as_card": True, "showcase_home_count": total, "note": note}


# "How do I make / create / start X?" always ends with a button into X's create screen (owner,
# 10/31-11/01: the model added one only some of the time). Added by the app, not the model - free.
_HOWTO = re.compile(r"\b(how (do|can|would|should) i|how to|where (do|can) i|help me|walk me through|show me how)\b.*\b(make|create|start|set ?up|build|add|send|write|do|get)\b", re.I)
_CREATE_LINKS = [
    (r"showcase", "Create a Showcase", "/showcases?new=1"),
    (r"spotlight|listing (web)?site|property (web)?site", "Make a Spotlight", "/studio/property-sites?new=1"),
    (r"\bcma\b|comps|market analysis|home value report", "Create a CMA", "/studio/cma?new=1"),
    (r"blog|article", "Write a blog post", "/studio/blogs?new=1"),
    (r"invite|invitation|rsvp|event", "Make an invite", "/studio/forms?view=invites&newInvite=1"),
    (r"\bform\b|sign-?up form|questionnaire", "Make a form", "/studio/forms?new=1"),
    (r"flyer|postcard|door hanger|post\b|instagram|social|design|mailer|just listed|just sold|coming soon", "Make a design", "/studio/design?new=1"),
    (r"open house kit|\bkit\b|sign-?in sheet|open house", "Start an Open House Kit", "/open-houses?newKit=1"),
    (r"signature", "Open Email Signature", "/studio/email-signature"),
    (r"review", "Open Reviews", "/studio/reviews"),
    (r"town|neighborhood|website", "Open your website", "/studio/website"),
    (r"follow-?up|reminder|task", "Open Follow-ups", "/leads/tasks"),
    (r"brand|colou?rs?|logo", "Open Branding", "/studio/brand-kit"),
]


def _howto_button(user_message: str, refs: List[dict]) -> None:
    msg = (user_message or "").split("\n\n")[-1]   # past any "(App note...)" prefix
    if not _HOWTO.search(msg) or any(r.get("record_type") in ("link", "action") for r in refs):
        return
    for pattern, label, page in _CREATE_LINKS:
        if re.search(pattern, msg, re.I):
            _, entry = ai_ui_tools.run("show_link", {"label": label, "page": page})
            if entry:
                refs.append(entry)
            return


def _no_pretend_cards(text: str, refs: List[dict]) -> str:
    """A card exists only when an action tool ran this turn (owner, 10/30: the reply TYPED
    "[Card: ... waiting for them to tap Confirm]" and there was no button). Typed card text is
    removed, and a "tap the button" reply with no card says so plainly instead of leaving them hunting."""
    import re
    # (A "the card didn't come through" add-on used to follow any "tap Confirm" with no card - it fired on
    # replies that were only asking a question first, owner 10/31, so it's gone; the prompt rule covers it.)
    cleaned = _no_narration(re.sub(r"\n?\[Card:[^\]]*\]", "", text or "").strip())
    return cleaned or text


# A sentence about how the chat works rather than the answer ("The sales list is already in my context,
# so I'll show those as cards" - 11/02). Agents never need to hear it.
_NARRATION = re.compile(r"[^.!?\n]*\b(?:in my context|my context|as (?:home |small )?cards|show (?:them|those|these) (?:as|in) cards|"
                        r"I can show them|I'll show (?:them|those|these)\b)[^.!?\n]*[.!?]?[ \t]*", re.I)


def _no_narration(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", _NARRATION.sub("", text or "")).strip()
