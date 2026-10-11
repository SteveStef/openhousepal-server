"""Talk to OHP: a live, interruptible voice conversation via OpenAI's Realtime API.

The browser connects DIRECTLY to OpenAI over WebRTC using a short-lived ephemeral client
secret minted here - this backend is never in the live audio path (that's OpenAI's current
recommended integration pattern for browser clients, not a design choice made for
convenience). That means this backend's only points of control are: (1) the pre-flight
checks below, before a client secret is ever minted, and (2) the per-tool-call and
per-usage-report endpoints in ai_routes.py, since those are the only requests the browser
still has to make back to us during a session. Everything voice-related shares the exact
same owner-scoped tool layer (ai_tools.py) that the text assistant already uses - there is
no second, independently-trusted data path for OpenAI.

Known platform limitation, not glossed over: OpenAI does not currently expose a way for this
backend to force-terminate an already-connected WebRTC session. The mitigations here (client-
side hard timers in TalkToOHP.tsx, incremental per-response cost reporting via
record_realtime_usage, concurrent-session caps, and the idle-abandonment sweep) are
defense-in-depth, not an absolute guarantee against a single misbehaving forgotten tab.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import AIRealtimeSession, User
from app.services.ai import ai_tools
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, monthly_cost_so_far, record_usage_event, reconcile_budget, reserve_global_and_emergency,
)
from app.services.usage_budget_service import Reservation, usd_to_micros

logger = get_logger(__name__)

_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


def _to_realtime_tools(tool_definitions: list) -> list:
    """Anthropic and OpenAI's function-calling shapes are NOT the same convention, despite
    both being JSON Schema underneath - confirmed against a live 400 from OpenAI
    ("Missing required parameter: 'session.tools[0].type'") while testing this against real
    ai_tools.TOOL_DEFINITIONS. Anthropic wants {name, description, input_schema}; OpenAI's
    Realtime API wants {type: "function", name, description, parameters}. This is the one
    translation step - the actual tool catalog (names, descriptions, schemas, and the
    server-side execution in ai_tools.py) is still exactly one shared definition."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in tool_definitions
    ]

REALTIME_INSTRUCTIONS = """You are the OpenHousePal AI Assistant, talking live with a real estate agent by voice. You have the same access to their OpenHousePal data (leads, properties, open houses, showcases, CMAs, activity) as the text assistant, through the same tools - ground every factual claim in a tool call, never guess or invent a name, date, price, or status. You are read-only: you can find, explain, and recommend, never modify data or send anything on the agent's behalf.

This is speech, not a written report. Keep replies short and conversational - a sentence or two, like a sharp assistant briefing someone in person, not an enumerated list read aloud. If there's more to say, offer it rather than dumping it: "There are a few other leads worth a look too - want me to run through them?" Never print or say an internal id (lead id, listing key, etc.) - use names, and phone numbers when relevant.

Tool results include raw timestamps. Compare them to the actual current time to describe recency accurately - never guess. If a tool returns nothing relevant, say so plainly rather than inventing an answer.

Voice transcription can turn a spoken street number into something else (e.g. "eight forty Harriton" transcribed as "8:40 Harriton"). When you hear what sounds like a number immediately followed by a street name, treat it as an address and search for it that way first - don't reinterpret it as a clock time unless the sentence is clearly about a time ("call me at 8:40")."""


class _ConcurrentSessionLimiter:
    """In-process count of live Talk to OHP sessions per user. Separate from
    ai_rate_limit_service's request-burst limiter - a voice session is long-lived and
    explicitly released on end/abandonment, not auto-released after one request."""

    def __init__(self):
        self._counts: Dict[str, int] = defaultdict(int)

    def acquire(self, owner_id: str, max_concurrent: int):
        if self._counts[owner_id] >= max_concurrent:
            raise AIRateLimitExceeded(
                "realtime_concurrency_limit",
                "You already have a voice session in progress. End it before starting another.",
            )
        self._counts[owner_id] += 1

    def release(self, owner_id: str):
        self._counts[owner_id] = max(0, self._counts[owner_id] - 1)


_session_limiter = _ConcurrentSessionLimiter()


async def _session_count_since(db: AsyncSession, owner_id: str, since: datetime) -> int:
    stmt = select(func.count(AIRealtimeSession.id)).where(
        AIRealtimeSession.owner_id == owner_id, AIRealtimeSession.started_at >= since
    )
    return (await db.execute(stmt)).scalar_one() or 0


async def create_ephemeral_session(db: AsyncSession, current_user: User, config: dict) -> dict:
    """Runs every pre-flight limit check, then mints a short-lived OpenAI client secret and
    creates the AIRealtimeSession row that tracks it. Raises AIRateLimitExceeded (same shape
    the chat path already uses) if any limit is hit - callers must not proceed. The permanent
    OPENAI_API_KEY is read here and never returned to the caller."""
    _session_limiter.acquire(current_user.id, config["realtime_max_concurrent_sessions_per_user"])
    reservation: Optional[Reservation] = None
    try:
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        daily_count = await _session_count_since(db, current_user.id, start_of_day)
        if daily_count >= config["realtime_daily_session_limit"]:
            raise AIRateLimitExceeded("realtime_daily_limit", "You've reached your voice usage limit for today.")

        monthly_count = await _session_count_since(db, current_user.id, start_of_month)
        if monthly_count >= config["realtime_monthly_session_limit"]:
            raise AIRateLimitExceeded("realtime_monthly_limit", "You've reached your voice usage limit for this month.")

        cost_so_far = await monthly_cost_so_far(db, current_user.id, "realtime")
        if cost_so_far >= config["realtime_monthly_cost_ceiling_usd"]:
            raise AIRateLimitExceeded("realtime_cost_ceiling", "You've reached your voice usage limit for this month.")

        # Layer 3: reserved once for the whole session (worst case realtime_max_session_seconds),
        # trued up in end_realtime_session once the session's real accumulated cost is known -
        # reserved BEFORE minting an OpenAI client secret so a global/emergency ceiling hit never
        # even reaches OpenAI.
        reservation = await reserve_global_and_emergency(
            db, "realtime_global", config["realtime_global_reservation_estimate_usd"],
            config["realtime_global_daily_cost_ceiling_usd"], config["realtime_global_monthly_cost_ceiling_usd"],
            config,
        )

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                _CLIENT_SECRETS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "session": {
                        "type": "realtime",
                        "model": config["realtime_model"],
                        "audio": {"output": {"voice": config["realtime_voice"]}},
                        "instructions": REALTIME_INSTRUCTIONS,
                        "tools": _to_realtime_tools(ai_tools.TOOL_DEFINITIONS),
                        "tool_choice": "auto",
                    },
                },
            )
            response.raise_for_status()
            secret_data = response.json()

        session = AIRealtimeSession(owner_id=current_user.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)

        return {
            "session_id": session.id,
            "client_secret": secret_data.get("value"),
            "expires_at": secret_data.get("expires_at"),
            "model": config["realtime_model"],
            "voice": config["realtime_voice"],
            "max_session_seconds": config["realtime_max_session_seconds"],
            "max_idle_seconds": config["realtime_max_idle_seconds"],
        }
    except Exception:
        _session_limiter.release(current_user.id)
        # The session never actually started (OpenAI call or DB commit above failed) - refund
        # the Layer-3 reservation in full so a failed session-create never eats into the
        # platform-wide global/emergency budgets. `reservation` stays None if the exception
        # happened before the reserve call above ran, in which case there's nothing to refund.
        if reservation is not None:
            await reconcile_budget(db, reservation, 0.0)
        raise


def _realtime_reservation(session: AIRealtimeSession, config: dict) -> Reservation:
    """Reconstructs the Layer-3 reservation made at session start (see create_ephemeral_session)
    for reconciliation at session end. Not persisted on the row itself - the reserved amount is
    always this feature's current reservation-estimate config value, and the period keys are
    derived from session.started_at (set moments after the real reservation call, so only a
    session that happens to straddle a day/month boundary could see a one-period mismatch - an
    acceptable, self-correcting rounding edge case for a conservative estimate, not a real
    budget-safety issue)."""
    started_at = session.started_at or datetime.now(timezone.utc)
    return Reservation(
        buckets=["realtime_global", "platform_emergency"],
        cost_micros=usd_to_micros(config["realtime_global_reservation_estimate_usd"]),
        day_key=started_at.date().isoformat(),
        month_key=started_at.strftime("%Y-%m"),
    )


async def end_realtime_session(db: AsyncSession, session: AIRealtimeSession, reason: str, config: Optional[dict] = None):
    """Idempotent - a session already marked ended (by a prior /end call or the abandonment
    sweep) is left alone rather than double-releasing its concurrency slot or double-reconciling
    its Layer-3 reservation. `config` is optional only so existing tests/callers that predate the
    global-budget reservation still work; every real caller in this codebase passes it so the
    session's actual accumulated cost gets trued up against the reservation made at its start."""
    if session.ended_at:
        return
    session.ended_at = datetime.now(timezone.utc)
    session.end_reason = reason
    db.add(session)
    await db.commit()
    _session_limiter.release(session.owner_id)
    if config is not None:
        await reconcile_budget(db, _realtime_reservation(session, config), session.estimated_cost_usd or 0.0)


async def sweep_abandoned_sessions(db: AsyncSession, config: dict):
    """No server-side way to kill a live WebRTC connection (see module docstring) - this is
    the honest fallback: if a session's heartbeat has gone stale for well beyond the configured
    idle window, we stop counting it against the owner's concurrent-session limit so one
    forgotten/crashed tab doesn't lock voice out for the rest of the month. Piggybacked on
    every heartbeat/end/session-create call rather than a background job, to avoid adding
    scheduler infrastructure for what should be a rare event."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config["realtime_max_idle_seconds"] * 3)
    stmt = select(AIRealtimeSession).where(
        AIRealtimeSession.ended_at.is_(None), AIRealtimeSession.last_heartbeat_at < cutoff
    )
    stale = (await db.execute(stmt)).scalars().all()
    for session in stale:
        await end_realtime_session(db, session, "abandoned", config=config)


def estimate_realtime_cost_usd(config: dict, usage: dict) -> float:
    """Cost from the real `usage` object OpenAI sends on each Realtime `response.done` server
    event (forwarded by the browser to POST /ai/realtime/usage) - actual reported token counts,
    not an estimate, unlike Read Aloud where no usage payload is available at all. Reads
    defensively since the exact input_token_details/output_token_details key names should be
    confirmed against a live session; falls back to treating all input/output tokens as audio
    (the dominant cost driver in a voice session) if the detail breakdown isn't present."""
    pricing = config.get("openai_pricing", {}).get(config["realtime_model"])
    if not pricing or not isinstance(usage, dict):
        return 0.0

    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}

    input_text = input_details.get("text_tokens", 0)
    input_audio = input_details.get("audio_tokens", usage.get("input_tokens", 0) - input_text)
    output_text = output_details.get("text_tokens", 0)
    output_audio = output_details.get("audio_tokens", usage.get("output_tokens", 0) - output_text)

    return (
        (max(input_text, 0) / 1_000_000) * pricing["text_in_per_mtok"]
        + (max(input_audio, 0) / 1_000_000) * pricing["audio_in_per_mtok"]
        + (max(output_text, 0) / 1_000_000) * pricing["text_out_per_mtok"]
        + (max(output_audio, 0) / 1_000_000) * pricing["audio_out_per_mtok"]
    )


async def record_realtime_usage(db: AsyncSession, session: AIRealtimeSession, current_user: User, config: dict, usage: dict) -> bool:
    """Logs one Realtime response's real usage as a normal AIUsageEvent (event_type='realtime')
    and adds it to the session's running total. Returns True if the owner has now hit their
    monthly realtime cost ceiling - the caller (ai_routes.py) tells the frontend to end the
    session after the current turn finishes rather than mid-speech."""
    cost = estimate_realtime_cost_usd(config, usage)

    session.estimated_cost_usd = (session.estimated_cost_usd or 0.0) + cost
    session.last_heartbeat_at = datetime.now(timezone.utc)
    db.add(session)

    await record_usage_event(
        db, current_user.id, None, config["realtime_model"],
        usage.get("input_tokens"), usage.get("output_tokens"), cost, 0, False, None,
        event_type="realtime",
    )
    await db.commit()

    cost_so_far = await monthly_cost_so_far(db, current_user.id, "realtime")
    return cost_so_far >= config["realtime_monthly_cost_ceiling_usd"]
