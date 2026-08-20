"""
Usage protection for the AI Assistant: burst + concurrency limits (in-memory, per
process) and daily/monthly/cost-ceiling limits (backed by AIUsageEvent, so they're
correct even across a restart). Also owns writing AIUsageEvent rows - the audit
trail and the source of truth these checks query.

Every check here runs before the orchestrator ever calls the model. A rejection
never reaches the Anthropic API, so it costs nothing beyond a DB query.
"""
import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AIUsageEvent
from app.services.usage_budget_service import (
    BucketLimits, BudgetExceeded, Reservation, reconcile as reconcile_budget, reserve as reserve_budget,
)


class AIRateLimitExceeded(Exception):
    """Raised when a request is blocked by a usage limit.
    `reason` is a short machine key logged to AIUsageEvent.block_reason.
    `user_message` is generic and safe to show verbatim - never include internal
    details (limit values, counts, etc.) in it."""

    def __init__(self, reason: str, user_message: str):
        self.reason = reason
        self.user_message = user_message
        super().__init__(reason)


class _InProcessLimiter:
    """Per-process burst + concurrency tracking. Correct for the current
    single-instance deployment (see RUN_LOCAL.md). A multi-worker or multi-instance
    deployment would need this backed by Redis instead - noted here rather than
    built, since Redis isn't part of the stack today."""

    def __init__(self):
        self._request_times: dict[str, deque] = defaultdict(deque)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def check_burst(self, user_id: str, requests_per_minute: int):
        async with self._lock:
            now = time.monotonic()
            window = self._request_times[user_id]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= requests_per_minute:
                raise AIRateLimitExceeded(
                    "burst_limit",
                    "You've sent several requests very quickly. Try again shortly.",
                )
            window.append(now)

    async def acquire_concurrency(self, user_id: str, max_concurrent: int):
        async with self._lock:
            if self._in_flight[user_id] >= max_concurrent:
                raise AIRateLimitExceeded(
                    "concurrency_limit",
                    "You already have a request in progress. Please wait for it to finish.",
                )
            self._in_flight[user_id] += 1

    async def release_concurrency(self, user_id: str):
        async with self._lock:
            self._in_flight[user_id] = max(0, self._in_flight[user_id] - 1)


_limiter = _InProcessLimiter()


async def _count_since(db: AsyncSession, owner_id: str, since: datetime, event_type: Optional[str] = None) -> int:
    stmt = select(func.count(AIUsageEvent.id)).where(
        AIUsageEvent.owner_id == owner_id,
        AIUsageEvent.blocked.is_(False),
        AIUsageEvent.created_at >= since,
    )
    if event_type:
        stmt = stmt.where(AIUsageEvent.event_type == event_type)
    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def _cost_since(db: AsyncSession, owner_id: str, since: datetime, event_type: Optional[str] = None) -> float:
    stmt = select(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0.0)).where(
        AIUsageEvent.owner_id == owner_id,
        AIUsageEvent.blocked.is_(False),
        AIUsageEvent.created_at >= since,
    )
    if event_type:
        stmt = stmt.where(AIUsageEvent.event_type == event_type)
    result = await db.execute(stmt)
    return float(result.scalar_one() or 0.0)


async def monthly_cost_so_far(db: AsyncSession, owner_id: str, event_type: Optional[str] = None) -> float:
    """Spend since the start of the current calendar month for one owner, optionally scoped
    to one event_type ('chat' | 'tts' | 'realtime') so each has an independently-enforceable
    monthly cost ceiling (ai_config_service.py) instead of sharing one combined budget. Shared
    by ai_compaction_service.py, the /ai/speech endpoint, and ai_realtime_service.py."""
    now = datetime.now(timezone.utc)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return await _cost_since(db, owner_id, start_of_month, event_type)


def check_input_length(message: str, config: dict):
    if len(message) > config["max_input_chars"]:
        raise AIRateLimitExceeded(
            "input_too_long",
            f"That message is too long. Please keep messages under {config['max_input_chars']} characters.",
        )


async def reserve_global_and_emergency(
    db: AsyncSession, feature_bucket: str, reservation_estimate_usd: float,
    feature_daily_ceiling_usd: Optional[float], feature_monthly_ceiling_usd: Optional[float],
    config: dict,
) -> Reservation:
    """Layer 3: reserves `reservation_estimate_usd` (a conservative upper bound for one call/
    turn/session - the real cost isn't known until the provider responds) against BOTH this
    feature's own platform-wide ceiling (`feature_bucket`) and the overall "platform_emergency"
    ceiling, atomically and race-free across workers (see usage_budget_service.py) - on top of
    whatever per-user checks the caller already ran. Shared by chat/tts/realtime/area_narrative/
    area_research; Places uses usage_budget_service.reserve directly since its cost per call is
    an exact known constant rather than an estimate needing reconciliation.

    Raises AIRateLimitExceeded (never BudgetExceeded directly), so every existing caller's
    `except AIRateLimitExceeded` handling already covers this. On success, the caller MUST later
    call reconcile_budget(db, reservation, actual_cost_usd) - the real cost once known, or 0.0 to
    fully refund if the call never actually happened (blocked downstream, or failed outright)."""
    buckets: dict[str, BucketLimits] = {
        feature_bucket: (None, None, feature_daily_ceiling_usd, feature_monthly_ceiling_usd),
        "platform_emergency": (
            None, None,
            config["platform_emergency_daily_cost_ceiling_usd"],
            config["platform_emergency_monthly_cost_ceiling_usd"],
        ),
    }
    try:
        return await reserve_budget(db, buckets, reservation_estimate_usd)
    except BudgetExceeded:
        raise AIRateLimitExceeded(
            "platform_capacity",
            "OpenHousePal's AI features are experiencing unusually high demand right now. Please try again shortly.",
        )


async def check_and_reserve(db: AsyncSession, owner_id: str, config: dict) -> Reservation:
    """Call once, before starting the orchestration loop. Raises AIRateLimitExceeded
    if any limit is hit. On success, the caller MUST call release(owner_id) in a
    finally block once the request completes, success or failure - AND must reconcile
    the returned Reservation (see reserve_global_and_emergency) once the turn's real
    cost is known, or with 0.0 if the turn never actually ran."""
    await _limiter.check_burst(owner_id, config["requests_per_minute"])

    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    daily_count = await _count_since(db, owner_id, start_of_day, event_type="chat")
    if daily_count >= config["daily_message_limit"]:
        raise AIRateLimitExceeded(
            "daily_limit", "You've reached your AI usage limit for today. It resets tomorrow."
        )

    monthly_count = await _count_since(db, owner_id, start_of_month, event_type="chat")
    if monthly_count >= config["monthly_message_limit"]:
        raise AIRateLimitExceeded(
            "monthly_limit", "You've reached your AI usage limit for this month."
        )

    # Scoped to "chat" only - tts/realtime/area_narrative/area_research each own an
    # independent cost ceiling (see ai_config_service.py) and must never eat into this one.
    monthly_cost = await _cost_since(db, owner_id, start_of_month, event_type="chat")
    if monthly_cost >= config["monthly_cost_ceiling_usd"]:
        raise AIRateLimitExceeded(
            "cost_ceiling", "You've reached your AI usage limit for this month."
        )

    reservation = await reserve_global_and_emergency(
        db, "chat_global", config["chat_global_reservation_estimate_usd"],
        config["chat_global_daily_cost_ceiling_usd"], config["chat_global_monthly_cost_ceiling_usd"],
        config,
    )

    # Concurrency reservation happens last so a rejection above never leaves a stuck slot.
    await _limiter.acquire_concurrency(owner_id, config["max_concurrent_requests"])
    return reservation


async def reserve_cma_analysis(db: AsyncSession, owner_id: str, config: dict) -> Reservation:
    """Per-user layer for AI-generated CMA analysis (see cma_ai_service.py), mirroring
    check_and_reserve's shape but scoped to this feature's own daily/monthly generation-count
    ceilings (cma_ai_daily/monthly_generation_limit_per_user) instead of chat's message counts.
    No burst/concurrency check - a CMA generation is a deliberate, infrequent action an agent
    triggers by hand, not chat-message frequency, so that protection doesn't apply here.
    On success, the caller MUST call reconcile_budget(db, reservation, actual_cost_usd) once the
    real cost is known, or 0.0 to fully refund if the call never happened - same contract as
    reserve_global_and_emergency."""
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    daily_count = await _count_since(db, owner_id, start_of_day, event_type="cma_ai")
    if daily_count >= config["cma_ai_daily_generation_limit_per_user"]:
        raise AIRateLimitExceeded(
            "daily_limit", "You've reached your AI CMA generation limit for today. It resets tomorrow."
        )

    monthly_count = await _count_since(db, owner_id, start_of_month, event_type="cma_ai")
    if monthly_count >= config["cma_ai_monthly_generation_limit_per_user"]:
        raise AIRateLimitExceeded(
            "monthly_limit", "You've reached your AI CMA generation limit for this month."
        )

    return await reserve_global_and_emergency(
        db, "cma_ai_global", config["cma_ai_reservation_estimate_usd"],
        config["cma_ai_daily_cost_ceiling_usd"], config["cma_ai_monthly_cost_ceiling_usd"],
        config,
    )


async def release(owner_id: str):
    await _limiter.release_concurrency(owner_id)


async def record_usage_event(
    db: AsyncSession,
    owner_id: str,
    conversation_id: Optional[str],
    model: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    estimated_cost_usd: Optional[float],
    tool_call_count: int,
    blocked: bool,
    block_reason: Optional[str],
    tool_names: Optional[list] = None,
    event_type: str = "chat",
):
    """Writes one AIUsageEvent row - the security/audit log and the input to every
    check above. Called for every attempt, allowed or blocked. `tool_names` is metadata
    only (which tools were called, e.g. "search_leads") - never the question/answer text,
    which this table never stores. `event_type` ('chat' | 'tts' | 'realtime') is which AI
    feature spent this money - see AIUsageEvent.event_type in database.py."""
    event = AIUsageEvent(
        owner_id=owner_id,
        conversation_id=conversation_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        tool_call_count=tool_call_count,
        tool_names=tool_names or None,
        blocked=blocked,
        block_reason=block_reason,
        event_type=event_type,
    )
    db.add(event)
    await db.commit()
