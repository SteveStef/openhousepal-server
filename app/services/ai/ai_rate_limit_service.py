"""
Usage protection for the AI Assistant: burst + concurrency limits (in-memory, per
process) and daily/monthly/cost-ceiling limits (backed by AIUsageEvent, so they're
correct even across a restart). Also owns writing AIUsageEvent rows - the audit
trail and the source of truth these checks query.

Every check here runs before the orchestrator ever calls the model. A rejection
never reaches the Anthropic API, so it costs nothing beyond a DB query.
"""
import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AIUsageEvent
from app.utils.auth import MLS_ACCESS_TIERS

logger = logging.getLogger(__name__)
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


# ---------------------------------------------------------------------------
# Per-agent monthly spend budget (every AI tool combined)
# ---------------------------------------------------------------------------
# The "Monthly AI Usage" meter the frontend shows (0-100%, never a dollar figure) and the
# generic monthly-limit block that backs it. One number per agent, spanning EVERY event_type.

_STUDIO_MEDIA_EVENT_TYPES = ("studio_media_image", "studio_media_video")

# Exempt from the *block* only - the meter/snapshot is still computed for them. peterstefanatos
# (the real IDX tester) asked for this removed 2026-09-04 - they want their own account to hit
# the real budget wall like anyone else's, so it's gone. test123@test.com stays exempt for
# unrelated internal QA.
_MONTHLY_BUDGET_UNLIMITED_OWNER_IDS = {
    "c68291e2-1b2c-40d1-85a6-752ac0485acf",  # test123@test.com
}


async def _cost_since_types(db: AsyncSession, owner_id: str, since: datetime, event_types) -> float:
    result = await db.execute(
        select(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0.0)).where(
            AIUsageEvent.owner_id == owner_id,
            AIUsageEvent.blocked.is_(False),
            AIUsageEvent.created_at >= since,
            AIUsageEvent.event_type.in_(tuple(event_types)),
        )
    )
    return float(result.scalar_one() or 0.0)


def _start_of_month() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def total_monthly_ai_cost(db: AsyncSession, owner_id: str) -> float:
    """This owner's spend since the 1st across ALL AI event_types combined."""
    return await _cost_since(db, owner_id, _start_of_month(), event_type=None)


async def studio_media_monthly_cost(db: AsyncSession, owner_id: str) -> float:
    """This owner's Studio AI Media spend (image + video) since the 1st."""
    return await _cost_since_types(db, owner_id, _start_of_month(), _STUDIO_MEDIA_EVENT_TYPES)


_USAGE_LABELS = {
    "blog_writer": "Blog articles",
    "cma_ai": "CMA reports", "cma_reasoning": "CMA reports", "cma_ai_recompute": "CMA reports",
    "copywrite": "Copywriter & wording checks",
    "studio_media_image": "AI Media pictures", "studio_media_video": "AI Media videos",
    "chat": "AI assistant", "tts": "AI assistant", "realtime": "AI assistant",
    "external_api": "Maps & place lookups",
    "school_lookup": "Cheat sheet schools",
    "area_research": "Researching your towns", "area_narrative": "Researching your towns", "area_fair_housing": "Researching your towns",
}


async def monthly_ai_budget_snapshot(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> dict:
    """Powers GET /ai/usage/monthly. `percent` is None for a tier with no configured budget
    (the frontend renders no meter in that case). `used_usd` is returned for the temporary
    exact-dollar readout while testing - the permanent UI is the percentage only."""
    limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(plan_tier)
    media_cap = config.get("studio_media_monthly_spend_cap_usd_by_tier", {}).get(plan_tier)
    used = await total_monthly_ai_cost(db, owner_id)
    media_used = await studio_media_monthly_cost(db, owner_id)
    percent = None if not limit else min(100, int(round(used / limit * 100)))
    # What the last hour added - the meter shows it as its own coloured slice, so an agent can see
    # what the thing they just made actually cost them.
    recent = min(used, await _cost_since(db, owner_id, datetime.now(timezone.utc) - timedelta(hours=1), event_type=None))
    recent_percent = None if not limit else min(percent or 0, int(round(recent / limit * 100)))
    if recent_percent == 0 and recent > 0.0005 and limit:
        recent_percent = 1 if (percent or 0) >= 1 else 0
    # What used it (user, 10/17 - "what's Peter's 12%?"): this month's spend by feature, as a share
    # of the allowance. Refunded / blocked events are left out, same as the total.
    rows = (await db.execute(
        select(AIUsageEvent.event_type, func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0.0))
        .where(AIUsageEvent.owner_id == owner_id, AIUsageEvent.blocked.is_(False), AIUsageEvent.created_at >= _start_of_month())
        .group_by(AIUsageEvent.event_type)
    )).all()
    groups: dict[str, float] = {}
    for event_type, cost in rows:
        label = _USAGE_LABELS.get(event_type or "", "Other AI tools")
        groups[label] = groups.get(label, 0.0) + float(cost or 0.0)
    breakdown = sorted(
        [{"label": k, "usd": round(v, 4), "percent": round(v / limit * 100, 1) if limit else None} for k, v in groups.items() if v > 0.0005],
        key=lambda r: -r["usd"],
    )
    return {
        "breakdown": breakdown,
        "tier": plan_tier,
        "used_usd": round(used, 4),
        "recent_used_usd": round(recent, 4),
        "recent_percent": recent_percent,
        "limit_usd": limit,
        "percent": percent,
        "near_limit": bool(percent is not None and percent >= 80),
        "exhausted": bool(limit and used >= limit),
        "studio_media_used_usd": round(media_used, 4),
        "studio_media_cap_usd": media_cap,
    }


async def enforce_agent_monthly_budget(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> None:
    """Raises AIRateLimitExceeded (generic message - no numbers) when this agent has spent
    their whole monthly AI budget across every tool. No-op for an exempt owner or a tier with
    no configured budget. Call before reserving spend on any metered AI feature."""
    if owner_id in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        return
    limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(plan_tier)
    if not limit:
        return
    if await total_monthly_ai_cost(db, owner_id) >= limit:
        raise AIRateLimitExceeded(
            "agent_monthly_budget",
            "You've reached your AI usage limit for this month. It resets on the 1st.",
        )


async def studio_soft_capped(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> bool:
    """True once this agent has used >= studio_soft_cap_percent of their total monthly AI
    budget - the point where AI Studio Media (photo + video) pauses but every cheaper tool
    keeps working. Cheap read; also used by GET /studio/media-lab/usage to grey the button
    out proactively. Never true for an exempt owner or a tier with no configured budget."""
    if owner_id in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        return False
    limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(plan_tier)
    if not limit:
        return False
    pct = config.get("studio_soft_cap_percent", 90)
    if pct >= 100:
        return False
    return await total_monthly_ai_cost(db, owner_id) >= limit * (pct / 100.0)


async def enforce_studio_soft_cap(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> None:
    """Raise AIRateLimitExceeded if this agent is past the soft cap - the expensive AI Studio
    tools stop here, the rest of the budget is reserved for CMAs / cheat sheets / chat. Call
    at the Studio Media reservation points only."""
    if await studio_soft_capped(db, owner_id, config, plan_tier):
        raise AIRateLimitExceeded(
            "studio_soft_cap",
            "You've used most of this month's AI allowance, so AI Studio photo and video are "
            "paused until it resets on the 1st. CMAs, cheat sheets and chat still work.",
        )


async def enforce_studio_media_monthly_cap(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> None:
    """Silent sub-cap: blocks a Studio AI Media generation once this agent's media spend
    (image + video) has used up the media slice of their monthly budget. Deliberately raises
    the SAME generic monthly-limit message as enforce_agent_monthly_budget so the agent never
    sees a media-specific figure."""
    if owner_id in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        return
    cap = config.get("studio_media_monthly_spend_cap_usd_by_tier", {}).get(plan_tier)
    if not cap:
        return
    if await studio_media_monthly_cost(db, owner_id) >= cap:
        raise AIRateLimitExceeded(
            "studio_media_monthly_cap",
            "You've reached your AI usage limit for this month. It resets on the 1st.",
        )


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
    except BudgetExceeded as exc:
        # exc.bucket tells us WHICH ceiling actually tripped - this feature's own platform-wide
        # daily/monthly ceiling (e.g. all agents' Studio Media video spend combined) vs. the
        # unrelated cross-feature "platform_emergency" brake. These used to always raise the same
        # generic message regardless of which one it was - now only the genuinely platform-wide
        # case gets that wording; the feature-specific one says so, without exposing a dollar
        # figure either way.
        if exc.bucket == "platform_emergency":
            raise AIRateLimitExceeded(
                "platform_capacity",
                "OpenHousePal's AI features are experiencing unusually high demand right now. Please try again shortly.",
            )
        raise AIRateLimitExceeded(
            "feature_capacity",
            "This AI feature is at its usage limit for right now across all agents. Please try again shortly.",
        )


async def check_and_reserve(db: AsyncSession, owner_id: str, config: dict, plan_tier: Optional[str] = None) -> Reservation:
    """Call once, before starting the orchestration loop. Raises AIRateLimitExceeded
    if any limit is hit. On success, the caller MUST call release(owner_id) in a
    finally block once the request completes, success or failure - AND must reconcile
    the returned Reservation (see reserve_global_and_emergency) once the turn's real
    cost is known, or with 0.0 if the turn never actually ran."""
    await _limiter.check_burst(owner_id, config["requests_per_minute"])
    await enforce_agent_monthly_budget(db, owner_id, config, plan_tier)

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


async def reserve_cma_analysis(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> Reservation:
    """Per-user layer for AI-generated CMA analysis (see cma_ai_service.py) - repeatable, so used
    for both the initial generation and Phase C's recompute-valuation action.

    No CMA-specific daily/monthly generation-count ceiling (removed 2026-09) - an agent's real
    dollar budget (agent_monthly_ai_budget_usd_by_tier, shared with every other metered AI tool,
    Studio Media included) is the actual per-user limiter now, enforced by
    enforce_agent_monthly_budget below; a separate artificial count on top of that would just
    double-gate the same thing. MLS_ACCESS_TIERS is still checked here as defense-in-depth against
    a caller reaching this for a tier with no CMA access (the route-level require_mls_access guard
    is the real gate; a tier failing this check would be a genuine misconfiguration, not the normal
    NO_MLS/PRO_NO_MLS path).

    No burst/concurrency check - a CMA generation is a deliberate, infrequent action an agent
    triggers by hand, not chat-message frequency, so that protection doesn't apply here.
    On success, the caller MUST call reconcile_budget(db, reservation, actual_cost_usd) once the
    real cost is known, or 0.0 to fully refund if the call never happened - same contract as
    reserve_global_and_emergency."""
    if plan_tier not in MLS_ACCESS_TIERS:
        raise AIRateLimitExceeded(
            "plan_tier", "Your plan doesn't include AI CMA analysis."
        )

    await enforce_agent_monthly_budget(db, owner_id, config, plan_tier)

    return await reserve_global_and_emergency(
        db, "cma_ai_global", config["cma_ai_reservation_estimate_usd"],
        config["cma_ai_daily_cost_ceiling_usd"], config["cma_ai_monthly_cost_ceiling_usd"],
        config,
    )


# ---------------------------------------------------------------------------
# CMA "Create AI Analysis" start gate: one run per report, and in-flight runs count against
# the agent's monthly allowance (user, 10/21: "if someone starts 2 at the same time but only
# has room for one, the second is refused cleanly instead of both running").
# ---------------------------------------------------------------------------
# enforce_agent_monthly_budget only sees spend that has already been RECORDED (AIUsageEvent
# rows land as each paid pass finishes, minutes into a run). So two runs started together both
# read the same "used" figure and both passed, and the same report could be started twice. This
# gate runs in the route BEFORE the background run is spawned: under one lock it refuses a
# report that is already running, and counts every other run this agent has in flight at its
# worst-case cost (cma_ai_max_cost_per_generation_usd) on top of what's already used. A single
# run's rule is unchanged (it starts whenever used < allowance). In-process, like
# _InProcessLimiter above - correct for the single-worker deployment (Dockerfile runs one uvicorn
# worker); the background runs themselves live in this process too, so a restart clears both.

class CmaGenerationAlreadyRunning(Exception):
    user_message = "This report's AI analysis is already running - it will finish on its own."


# Same generous window as cma_service._GENERATION_STALE_AFTER_SECONDS: a hold older than this
# belongs to a run that hung or crashed without its finally, so it stops counting.
_CMA_HOLD_STALE_AFTER_SECONDS = 1800


class _CmaGenerationHolds:
    def __init__(self):
        # report_id -> (owner_id, held_usd, monotonic start)
        self.holds: dict[str, tuple[str, float, float]] = {}
        self.lock = asyncio.Lock()

    def prune(self) -> None:
        now = time.monotonic()
        for rid in [rid for rid, (_, _, t) in self.holds.items() if now - t > _CMA_HOLD_STALE_AFTER_SECONDS]:
            self.holds.pop(rid, None)


_cma_holds = _CmaGenerationHolds()


def cma_generation_hold_usd(config: dict) -> float:
    """What one in-flight CMA run is counted at until it finishes - its hard per-run cap."""
    return float(config.get("cma_ai_max_cost_per_generation_usd") or 0.75)


async def hold_cma_generation(db: AsyncSession, owner_id: str, report_id: str, config: dict, plan_tier: str) -> float:
    """Claim the right to start one CMA AI run for `report_id`. Raises CmaGenerationAlreadyRunning
    (route -> 409) if that report already has a run in flight, or AIRateLimitExceeded (route ->
    429) if the agent's monthly allowance has no room once their other in-flight runs are counted.
    On success the caller MUST call release_cma_generation(report_id) when the run ends, however
    it ends. Nothing is spent or recorded here - the real reservations still happen inside the run
    (reserve_cma_analysis / reserve_cma_reasoning)."""
    if plan_tier not in MLS_ACCESS_TIERS:
        raise AIRateLimitExceeded("plan_tier", "Your plan doesn't include AI CMA analysis.")
    hold = cma_generation_hold_usd(config)
    async with _cma_holds.lock:
        _cma_holds.prune()
        if report_id in _cma_holds.holds:
            raise CmaGenerationAlreadyRunning()
        limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(plan_tier)
        if limit and owner_id not in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
            in_flight = sum(h for (o, h, _) in _cma_holds.holds.values() if o == owner_id)
            used = await total_monthly_ai_cost(db, owner_id)
            if used >= limit:
                raise AIRateLimitExceeded(
                    "agent_monthly_budget",
                    "You've reached your AI usage limit for this month. It resets on the 1st.",
                )
            if used + in_flight >= limit:
                raise AIRateLimitExceeded(
                    "agent_monthly_budget_in_flight",
                    "Your other CMA's AI analysis is still running, and this month's AI allowance only has "
                    "room for that one. This one wasn't started and nothing was used - try again once it finishes.",
                )
        _cma_holds.holds[report_id] = (owner_id, hold, time.monotonic())
    return hold


async def release_cma_generation(report_id: str) -> None:
    async with _cma_holds.lock:
        _cma_holds.holds.pop(report_id, None)


async def reserve_cma_reasoning(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> Reservation:
    """Reserve the dedicated final-valuation reasoning pass independently of Claude analysis."""
    if plan_tier not in MLS_ACCESS_TIERS:
        raise AIRateLimitExceeded(
            "plan_tier", "Your plan doesn't include AI CMA analysis."
        )
    await enforce_agent_monthly_budget(db, owner_id, config, plan_tier)
    return await reserve_global_and_emergency(
        db, "cma_ai_global", config["cma_reasoning_reservation_estimate_usd"],
        config["cma_ai_daily_cost_ceiling_usd"], config["cma_ai_monthly_cost_ceiling_usd"],
        config,
    )


async def reserve_copywrite(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> Reservation:
    """Per-user layer for the AI copywriter + the Fair-Housing compliance check (short single
    Claude calls - listing descriptions, captions, blog drafts, bio polish, phrase flags).

    Same shape as reserve_cma_analysis: the real per-user limiter is the shared
    agent_monthly_ai_budget_usd_by_tier pool (enforce_agent_monthly_budget), not a per-feature
    count. Available on every active tier (a NO_MLS agent still writes captions and a bio). No
    burst/concurrency gate - these are deliberate button presses, not chat frequency. Caller MUST
    reconcile_budget(db, reservation, actual_cost_usd) after, or 0.0 to refund."""
    await enforce_agent_monthly_budget(db, owner_id, config, plan_tier)
    return await reserve_global_and_emergency(
        db, "copywrite_global", config["copywrite_reservation_estimate_usd"],
        config["copywrite_global_daily_cost_ceiling_usd"], config["copywrite_global_monthly_cost_ceiling_usd"],
        config,
    )


async def reserve_blog_writer(db: AsyncSession, owner_id: str, config: dict, plan_tier: str, *, refine: bool = False) -> Reservation:
    """Per-user layer for the AI Blog Writer (full-article generation, and per-section refine when
    `refine`). Same shape as reserve_copywrite: the real per-user limiter is the shared
    agent_monthly_ai_budget_usd_by_tier pool (enforce_agent_monthly_budget), then a Layer-3
    platform-wide + emergency reservation in the feature's own "blog_writer_global" bucket.
    Caller MUST reconcile_budget(db, reservation, actual_cost_usd) after, or 0.0 to refund."""
    await enforce_agent_monthly_budget(db, owner_id, config, plan_tier)
    estimate_key = "blog_refine_reservation_estimate_usd" if refine else "blog_writer_reservation_estimate_usd"
    return await reserve_global_and_emergency(
        db, "blog_writer_global", config[estimate_key],
        config["blog_writer_global_daily_cost_ceiling_usd"], config["blog_writer_global_monthly_cost_ceiling_usd"],
        config,
    )


async def _reserve_studio_media(
    db: AsyncSession, owner_id: str, config: dict, plan_tier: str,
    *, event_type: str, daily_key: str, monthly_key: str,
    bucket: str, estimate_key: str, daily_ceiling_key: str, monthly_ceiling_key: str,
) -> Reservation:
    """Shared body for the image and video variants below - same shape as reserve_cma_analysis:
    per-user daily/monthly generation-count ceilings scoped to this feature's own event_type,
    then a global + platform-emergency cost reservation. No burst/concurrency check (a media
    generation is a deliberate, infrequent click, not chat-message frequency).
    On success the caller MUST reconcile_budget(db, reservation, actual_cost_usd) once known,
    or 0.0 to fully refund if the generation never happened."""
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    daily_limit = config[daily_key].get(plan_tier)
    monthly_limit = config[monthly_key].get(plan_tier)
    if daily_limit is None or monthly_limit is None:
        raise AIRateLimitExceeded("plan_tier", "Your plan doesn't include the AI Media tool.")

    # Spend gates: soft cap first (expensive Studio tools stop ~90%, the rest of the budget is
    # kept for CMAs / cheat sheets / chat), then the whole-account monthly AI budget, then the
    # Studio Media slice of it.
    await enforce_studio_soft_cap(db, owner_id, config, plan_tier)
    await enforce_agent_monthly_budget(db, owner_id, config, plan_tier)
    await enforce_studio_media_monthly_cap(db, owner_id, config, plan_tier)

    daily_count = await _count_since(db, owner_id, start_of_day, event_type=event_type)
    if daily_count >= daily_limit:
        raise AIRateLimitExceeded(
            "daily_limit", "You've reached your AI Media generation limit for today. It resets tomorrow."
        )

    monthly_count = await _count_since(db, owner_id, start_of_month, event_type=event_type)
    if monthly_count >= monthly_limit:
        raise AIRateLimitExceeded(
            "monthly_limit", "You've reached your AI Media generation limit for this month."
        )

    return await reserve_global_and_emergency(
        db, bucket, config[estimate_key],
        config[daily_ceiling_key], config[monthly_ceiling_key], config,
    )


async def reserve_studio_media_image(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> Reservation:
    """Per-user + global layer for a Studio AI Media still-image edit (gpt-image-2)."""
    return await _reserve_studio_media(
        db, owner_id, config, plan_tier,
        event_type="studio_media_image",
        daily_key="studio_media_image_daily_generation_limit_by_tier",
        monthly_key="studio_media_image_monthly_generation_limit_by_tier",
        bucket="studio_media_image_global",
        estimate_key="studio_media_image_reservation_estimate_usd",
        daily_ceiling_key="studio_media_image_global_daily_cost_ceiling_usd",
        monthly_ceiling_key="studio_media_image_global_monthly_cost_ceiling_usd",
    )


async def reserve_studio_media_video(db: AsyncSession, owner_id: str, config: dict, plan_tier: str) -> Reservation:
    """Per-user + global layer for a Studio AI Media photo-to-video generation (Runway gen4.5).
    Reserved at submit time at the video reservation estimate; trued up when poll_asset sees
    the job finish (or refunded if it fails)."""
    return await _reserve_studio_media(
        db, owner_id, config, plan_tier,
        event_type="studio_media_video",
        daily_key="studio_media_video_daily_generation_limit_by_tier",
        monthly_key="studio_media_video_monthly_generation_limit_by_tier",
        bucket="studio_media_video_global",
        estimate_key="studio_media_video_reservation_estimate_usd",
        daily_ceiling_key="studio_media_video_global_daily_cost_ceiling_usd",
        monthly_ceiling_key="studio_media_video_global_monthly_cost_ceiling_usd",
    )


async def release(owner_id: str):
    await _limiter.release_concurrency(owner_id)


_DEFAULT_EXTERNAL_PRICES = {
    "places_nearby": 0.032, "places_text": 0.032, "places_details": 0.017, "places_details_reviews": 0.025,
    "geocoding": 0.005, "routes_matrix_element": 0.005, "static_map": 0.002, "census": 0.0,
}


async def record_external_api_cost(
    db: AsyncSession,
    owner_id: Optional[str],
    *,
    kind: str,
    calls: int = 1,
    cost_usd: Optional[float] = None,
    config: Optional[dict] = None,
    blocked: bool = False,
) -> None:
    """Attribute one agent-triggered external (non-LLM) API call - Google Places / Geocoding /
    Routes - to that agent's monthly AI usage, so it flows into total_monthly_ai_cost -> the
    % meter, the $20 monthly budget and the 90% soft cap, exactly like the LLM tools.

    No-op when owner_id is None: that's a sync/cron/background call with no user to charge, and
    per the product decision those stay on the platform emergency brake only. Best-effort - a
    logging failure must never break the feature that made the call."""
    if not owner_id:
        return
    try:
        if cost_usd is None:
            prices = ((config or {}).get("external_api_prices_usd") if config else None) or _DEFAULT_EXTERNAL_PRICES
            cost_usd = float(prices.get(kind, 0.0)) * max(1, int(calls))
        await record_usage_event(
            db, owner_id, None, f"google:{kind}", None, None, round(cost_usd, 6), 0,
            blocked, None, event_type="external_api",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_external_api_cost failed", extra={"kind": kind, "error": str(exc)})


def classify_provider_error(exc: Exception) -> str:
    """Buckets a raw upstream AI-provider exception into one of a small set of machine keys for
    AIUsageEvent.block_reason, so a provider-side outage of any kind - not just a rate limit we
    imposed ourselves - shows up on the admin AI usage dashboard instead of only ever existing as
    a line in a log file. Deliberately string-matches the exception text rather than importing
    each provider SDK's exception classes here, since this needs to classify Anthropic, OpenAI,
    and any future provider's errors alike from one place.
    'insufficient_credit' is the one that actually broke CMA generation on 2026-09-14 (Anthropic
    account ran out of credit balance) and had been completely invisible to this dashboard until
    this function existed - every call site that catches a raw provider exception should record
    one of these instead of silently only logging and refunding the budget reservation."""
    text = str(exc).lower()
    if "credit balance" in text or "insufficient_quota" in text or "exceeded your current quota" in text:
        return "insufficient_credit"
    if "rate_limit" in text or "rate limit" in text or "429" in text:
        return "provider_rate_limited"
    if "overloaded" in text or "529" in text or "503" in text:
        return "provider_overloaded"
    if "timeout" in text or "timed out" in text:
        return "provider_timeout"
    if "authentication" in text or "invalid x-api-key" in text or "401" in text:
        return "provider_auth_error"
    return "provider_error"


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
    return event
