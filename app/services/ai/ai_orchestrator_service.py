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

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import User
from app.services.ai import ai_actions, ai_tools, ai_ui_tools
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
    "get_property_matches": "Matching homes", "get_showcase_info": "Opening the Showcase", "get_similar_properties": "Finding similar homes",
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
    # Lookups this plan gets + things it can set up for a Confirm tap (ai_actions.py, 10/30).
    from app.services.ai import ai_home_cards
    tools = ai_tools.tools_for(current_user) + ai_actions.action_tools_for(current_user) + ai_ui_tools.UI_TOOL_DEFINITIONS + [ai_home_cards.TOOL]

    # The agent's own listings and sales, known up front (owner, 10/30: it guessed "your newest listing"
    # from days on market, then lost it on a follow-up) - newest first, with each one's listing key so
    # an action uses that exact home (a building has many units).
    mine_text = await _my_listings_and_sales(db, current_user)

    async def _call_model():
        # Second system block is deliberately NOT cached (no cache_control) and rebuilt every
        # call - it's the model's only anchor for "now". Without it, the model has no way to
        # tell whether a tool-returned timestamp like "2026-08-05T09:15:00Z" is from this
        # morning or a week ago, and will guess - see ai_prompt_service.py's recency guidance.
        now_block = {
            "type": "text",
            "text": f"Current date and time: {datetime.now(timezone.utc):%A, %B %d, %Y %I:%M %p} UTC.\n"
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
        return await client.messages.create(
            model=config["model"],
            max_tokens=config["max_output_tokens"],
            system=system_blocks,
            tools=tools,
            messages=messages,
            **ai_config_service.model_options(config["model"], "medium"),
        )

    nudged = False
    promised = False
    _progress(current_user.id, "Thinking")

    async def _run() -> str:
        nonlocal nudged, promised
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
                if not promised and _PROMISE.search((text or "").strip()[-220:]) and usage_state["tool_call_count"] < config["max_tool_calls_per_turn"] - 1:
                    promised = True
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": "(App note: you said you'd look but didn't - do it now with your tools in this same turn (find_open_house_guests searches every sign-in list at once), then answer. Never end a reply on a promise to do something later.)"})
                    _progress(current_user.id, "Still looking")
                    continue
                # In a long chat it sometimes says "tap Confirm on the card" without making one
                # (owner's test, 11/01) - one nudge to make the real card, then whatever it says.
                if not nudged and _CLAIMS_CARD.search(text or "") and not any(r.get("record_type") == "action" for r in all_refs):
                    nudged = True
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": "(App note: no card was made this turn - nothing shows for them to tap. Call the action tool now to set it up, then reply in one short line. If you still need an answer from them first, ask only that.)"})
                    continue
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
                if block.name == ai_home_cards.NAME:
                    # Their listings / sales as small home cards under the reply (owner, 11/01).
                    result, entry = await ai_home_cards.run(db, current_user, args)
                    if entry:
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
                all_refs.extend(_extract_record_refs(block.name, args))
                result = await ai_tools.execute_tool(db, current_user, block.name, args, config)
                all_refs.extend(_extract_result_refs(block.name, result))
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
                    "content": json.dumps(result, default=str),
                })

            if only_ui:
                return said
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

    usage_state.setdefault("estimated_cost_usd", 0.0)  # summed per call in _run
    _howto_button(user_message, all_refs)
    return _no_pretend_cards(text, all_refs), all_refs, usage_state


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
    cleaned = re.sub(r"\n?\[Card:[^\]]*\]", "", text or "").strip()
    return cleaned or text
