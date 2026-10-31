"""Admin-only AI usage/cost analytics - who's driving AI cost and what they're doing with
it, so usage can be watched against the per-agent $35/mo subscription economics.

Deliberately metadata-only: every query here reads AIUsageEvent (owner_id, tokens, cost,
tool_call_count, tool_names, blocked, block_reason, created_at) and never touches AIMessage.
tool_names records which tools were called (e.g. "search_leads"), never the question asked
or the answer given - agents are paying customers, not employees, and their questions about
their own leads are private business. See ai_orchestrator_service.py for where tool_names
is populated and AIUsageEvent's docstring in database.py for the same guarantee restated
at the model layer.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import AIUsageEvent, UsageBudgetCounter, User as UserModel
from app.schemas.ai import AIAdminAgentDetail, AIAdminAgentSummary, AIAdminBudgetStatus, AIAdminDailyPoint, AIAdminMonthlyPoint, AIAdminOverview, AIAdminTopConsumer
from app.services.ai.ai_config_service import ai_config_service
from app.services.usage_budget_service import micros_to_usd, usage_snapshot
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/ai", tags=["admin", "ai"])
logger = get_logger(__name__)

# event_type -> which vendor actually gets paid, for the cost-by-provider breakdown. Google
# Places isn't an event_type at all (it has no per-user attribution or AIUsageEvent rows - see
# nearby_amenities_service.py) so its cost comes from usage_budget_counters instead, added
# separately in get_ai_overview.
_EVENT_TYPE_PROVIDER = {
    "chat": "anthropic", "area_narrative": "anthropic", "area_research": "anthropic",
    "cma_ai": "anthropic", "copywrite": "anthropic", "blog_writer": "anthropic",
    "cheat_sheet_downtown": "anthropic", "cheat_sheet_proofread": "anthropic", "school_lookup": "anthropic",
    # cma_reasoning ("Astra") is a reasoning-effort OpenAI model (api.openai.com/v1/responses -
    # see _call_cma_reasoning_model), not Anthropic, despite living in the same CMA pipeline as
    # the (Anthropic) cma_ai comp-analysis call.
    "tts": "openai", "realtime": "openai", "cma_reasoning": "openai",
}

# (bucket, human label, day-cost-limit config key, month-cost-limit config key,
#  day-count-limit config key or None, month-count-limit config key or None) - every Layer-3/
# Places safety-net ceiling shown in the admin overview's global_budget_status. Bucket names
# (usage_budget_counters rows) and their config key names diverge for Places (bucket
# "places_search" vs config "places_explore_search_*"), so this maps each explicitly rather
# than assuming a naming convention. Order matches ai_config_service.py's rough dollar ordering
# (smallest to largest emergency-brake share) so the UI reads top-to-bottom the same way the
# config file's comments explain the layering.
_BUDGET_BUCKETS = [
    ("cma_ai_global", "CMA reports (platform-wide, both AI passes)", "cma_ai_daily_cost_ceiling_usd", "cma_ai_monthly_cost_ceiling_usd", None, None),
    ("chat_global", "Chat (platform-wide)", "chat_global_daily_cost_ceiling_usd", "chat_global_monthly_cost_ceiling_usd", None, None),
    ("copywrite_global", "AI copywriter + compliance check (platform-wide)", "copywrite_global_daily_cost_ceiling_usd", "copywrite_global_monthly_cost_ceiling_usd", None, None),
    ("blog_writer_global", "AI Blog Writer (platform-wide)", "blog_writer_global_daily_cost_ceiling_usd", "blog_writer_global_monthly_cost_ceiling_usd", None, None),
    ("studio_media_image_global", "Studio AI images (platform-wide)", "studio_media_image_global_daily_cost_ceiling_usd", "studio_media_image_global_monthly_cost_ceiling_usd", None, None),
    ("studio_media_video_global", "Studio AI videos (platform-wide)", "studio_media_video_global_daily_cost_ceiling_usd", "studio_media_video_global_monthly_cost_ceiling_usd", None, None),
    ("tts_global", "Read Aloud (platform-wide)", "tts_global_daily_cost_ceiling_usd", "tts_global_monthly_cost_ceiling_usd", None, None),
    ("realtime_global", "Talk to OHP (platform-wide)", "realtime_global_daily_cost_ceiling_usd", "realtime_global_monthly_cost_ceiling_usd", None, None),
    ("area_narrative_global", "Area narrative (platform-wide)", None, "area_narrative_monthly_cost_ceiling_usd", None, None),
    ("area_research_global", "Area research (platform-wide)", "area_research_global_daily_cost_ceiling_usd", "area_research_global_monthly_cost_ceiling_usd", None, None),
    ("places_search", "Google Places - Explore Nearby search", None, None, "places_explore_search_daily_limit", "places_explore_search_monthly_limit"),
    ("places_details", "Google Places - Explore Nearby details", None, None, "places_explore_details_daily_limit", "places_explore_details_monthly_limit"),
    ("platform_emergency", "OpenHousePal emergency brake (all features combined)", "platform_emergency_daily_cost_ceiling_usd", "platform_emergency_monthly_cost_ceiling_usd", None, None),
]

# Google gives a recurring monthly free-call allowance per Places SKU (not a one-time credit) -
# the count limits above are deliberately set 1 call under each, so real Google billing on these
# two buckets stays $0/month as long as nothing else on this Google Cloud project shares the same
# quota. The $ spent shown alongside is real (what it WOULD have cost without the free tier), not
# what's actually billed - surfaced so a spike is visible even while it's still $0 out of pocket.
_PLACES_FREE_TIER_NOTE = {
    "places_search": "Google's free tier: 5,000 calls/mo. Capped at 4,999/mo so real billing stays $0.",
    "places_details": "Google's free tier: 1,000 calls/mo. Capped at 999/mo so real billing stays $0.",
}

# Which AIUsageEvent.event_type a cost-ceiling bucket's calls actually show up under, so
# "remaining budget / this month's real average cost per call" can be shown alongside the ceiling
# itself. Skipped for platform_emergency (spans every feature's mixed cost-per-call, so a single
# average wouldn't mean anything) and the Places buckets (already count-limited, so their own
# day/month_count vs. _count_limit IS the remaining-calls figure, no division needed).
_BUCKET_EVENT_TYPE = {
    "chat_global": "chat", "tts_global": "tts", "realtime_global": "realtime",
    "area_narrative_global": "area_narrative", "area_research_global": "area_research",
    "copywrite_global": "copywrite", "blog_writer_global": "blog_writer", "studio_media_image_global": "studio_media_image",
    "studio_media_video_global": "studio_media_video",
}

# ai_rate_limit_service.classify_provider_error's possible return values that mean "an upstream
# AI provider itself failed" (exhausted credit, outage, auth, rate limit) as opposed to one of
# OpenHousePal's own configured usage ceilings - see that function's docstring for why this
# exists: it's the one failure mode that used to be invisible anywhere but a log line.
_PROVIDER_FAILURE_REASONS = {
    "insufficient_credit", "provider_rate_limited", "provider_overloaded",
    "provider_timeout", "provider_auth_error", "provider_error", "provider_not_configured",
}


_ET = ZoneInfo("America/New_York")

# The rest of event_type -> vendor (the dict above predates these features).
_EVENT_TYPE_PROVIDER.update({
    "area_fair_housing": "anthropic", "cma_ai_recompute": "anthropic",
    "studio_media_image": "openai", "studio_media_video": "fal",
    "external_api": "google", "explore_nearby": "google_places",
})

_CMA_EVENT_TYPES = ("cma_ai", "cma_reasoning", "cma_ai_recompute")

# Blocked rows whose numbers are a guess made BEFORE a call that never happened - not money spent.
# Every other blocked row that carries tokens/cost was really paid for (a blog run refunded to the
# agent, a chat turn that errored part-way, area research that came back empty).
_NOT_SPENT_REASONS = ("cma_cost_cap",)

# Not "calls we stopped": the run happened and its cost was handed back to the agent.
_NOT_A_BLOCK_REASONS = ("refunded_failed_run",)


def _month_start(now: datetime) -> datetime:
    """The 1st of this month, midnight Eastern (the owner's month), as a UTC-comparable time."""
    return now.astimezone(_ET).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(now: datetime) -> datetime:
    return now.astimezone(_ET).replace(hour=0, minute=0, second=0, microsecond=0)


def _real_cost(config: dict):
    """What one ai_usage_events row really cost us. Owner 10/27: Admin's site spend read ~$9
    while CMA alone was ~$20 - every live row written before 2026-10-23 21:39 UTC had its cost
    set to $0 afterwards (tokens kept), refunded blog runs were left out as "blocked", and the
    CMA cap's pre-call guess was counted as spend. So: the saved cost; when that's $0 but the row
    has tokens, the same price the code would have charged (model_pricing, which reproduces every
    non-zeroed CMA row to the cent); $0 for a guess."""
    priced_back = [
        (AIUsageEvent.model == model,
         func.coalesce(AIUsageEvent.input_tokens, 0) * (float(p["input_per_mtok"]) / 1_000_000)
         + func.coalesce(AIUsageEvent.output_tokens, 0) * (float(p["output_per_mtok"]) / 1_000_000))
        for model, p in (config.get("model_pricing") or {}).items()
        if isinstance(p, dict) and "input_per_mtok" in p and "output_per_mtok" in p
    ]
    stored = func.coalesce(AIUsageEvent.estimated_cost_usd, 0.0)
    return case(
        (AIUsageEvent.block_reason.in_(_NOT_SPENT_REASONS), 0.0),
        (stored > 0, stored),
        *priced_back,
        else_=0.0,
    )


def _spent(config: dict, *where):
    total = func.sum(_real_cost(config))
    return func.coalesce(total.filter(*where) if where else total, 0.0)


def _a_real_block():
    return and_(AIUsageEvent.blocked.is_(True),
                or_(AIUsageEvent.block_reason.is_(None), AIUsageEvent.block_reason.notin_(_NOT_A_BLOCK_REASONS)))


async def _places_spend(db: AsyncSession) -> dict:
    """Explore Nearby (Google Places for visitors on agents' pages) - not on any agent's bar, so it
    has no ai_usage_events row; its cost lives only in the places_* budget counters (UTC days)."""
    day_key, month_key = datetime.now(timezone.utc).date().isoformat(), datetime.now(timezone.utc).strftime("%Y-%m")
    out = {"day": 0.0, "month": 0.0, "all": 0.0}
    rows = (await db.execute(
        select(UsageBudgetCounter.period_type, UsageBudgetCounter.period_key, UsageBudgetCounter.cost_micros)
        .where(UsageBudgetCounter.bucket.in_(("places_search", "places_details")))
    )).all()
    for period_type, period_key, micros in rows:
        usd = micros_to_usd(micros or 0)
        if period_type == "month":
            out["all"] += usd
            if period_key == month_key:
                out["month"] += usd
        elif period_key == day_key:
            out["day"] += usd
    return out


async def _top_consumers(db: AsyncSession, kind: str, limit_usd: float, limit: int = 5) -> List[AIAdminTopConsumer]:
    """Today's highest-spending ip_daily/device_daily buckets - see nearby_amenities_service.py's
    per-requester daily ceilings. No user identity to join (a visitor isn't a User row) - this is
    purely 'is someone mid-abuse right now', ranked by how close to their own $3 cap they are."""
    prefix = f"{kind}_daily:"
    day_key = datetime.now(timezone.utc).date().isoformat()
    rows = (
        await db.execute(
            select(UsageBudgetCounter.bucket, UsageBudgetCounter.cost_micros)
            .where(
                UsageBudgetCounter.bucket.like(f"{prefix}%"),
                UsageBudgetCounter.period_type == "day",
                UsageBudgetCounter.period_key == day_key,
                UsageBudgetCounter.cost_micros > 0,
            )
            .order_by(UsageBudgetCounter.cost_micros.desc())
            .limit(limit)
        )
    ).all()
    return [
        AIAdminTopConsumer(kind=kind, identifier=bucket[len(prefix):], spent_today_usd=micros_to_usd(cost_micros), limit_usd=limit_usd)
        for bucket, cost_micros in rows
    ]


async def _agent_site_spend(db: AsyncSession, owner_id: str, config: dict) -> dict:
    """This agent's own IDX/public-site spend (agent_site_daily/agent_total_daily buckets -
    see nearby_amenities_service.py), separate from their AIUsageEvent-tracked feature usage."""
    site = await usage_snapshot(db, f"agent_site_daily:{owner_id}")
    total = await usage_snapshot(db, f"agent_total_daily:{owner_id}")
    return {
        "site_spend_today_usd": site["day_cost_usd"],
        "site_spend_month_usd": site["month_cost_usd"],
        "site_daily_limit_usd": config.get("agent_site_daily_cost_limit_usd"),
        "total_spend_today_usd": total["day_cost_usd"],
        "total_spend_month_usd": total["month_cost_usd"],
        "total_daily_limit_usd": config.get("agent_total_daily_cost_limit_usd"),
    }


async def _cost_by_type(db: AsyncSession, since: datetime, config: dict, owner_id: Optional[str] = None) -> dict:
    stmt = (
        select(AIUsageEvent.event_type, _spent(config))
        .where(AIUsageEvent.created_at >= since)
        .group_by(AIUsageEvent.event_type)
    )
    if owner_id:
        stmt = stmt.where(AIUsageEvent.owner_id == owner_id)
    result = await db.execute(stmt)
    return {event_type: float(cost) for event_type, cost in result.all() if float(cost or 0) > 0}


async def _call_counts_by_type(db: AsyncSession, since: datetime) -> dict:
    """Successful (non-blocked) call counts per event_type since `since` - paired with
    _cost_by_type's dollar totals to get a real 'average cost per call' for the remaining-budget
    estimate on each bucket in _BUCKET_EVENT_TYPE."""
    stmt = (
        select(AIUsageEvent.event_type, func.count(AIUsageEvent.id))
        .where(AIUsageEvent.blocked.is_(False), AIUsageEvent.created_at >= since)
        .group_by(AIUsageEvent.event_type)
    )
    result = await db.execute(stmt)
    return {event_type: int(count) for event_type, count in result.all()}


async def _recent_provider_failures(db: AsyncSession, since: datetime, limit: int = 10) -> List[dict]:
    """Provider-side failures (exhausted credit, outage, auth, rate limit - see
    classify_provider_error) in the last `since` window, grouped by feature+reason so a repeated
    failure reads as one row with a count, not a wall of identical entries."""
    stmt = (
        select(
            AIUsageEvent.event_type, AIUsageEvent.block_reason,
            func.count(AIUsageEvent.id), func.max(AIUsageEvent.created_at),
        )
        .where(
            AIUsageEvent.blocked.is_(True),
            AIUsageEvent.block_reason.in_(_PROVIDER_FAILURE_REASONS),
            AIUsageEvent.created_at >= since,
        )
        .group_by(AIUsageEvent.event_type, AIUsageEvent.block_reason)
        .order_by(func.max(AIUsageEvent.created_at).desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {"event_type": event_type, "reason": reason, "count": int(count), "last_seen": last_seen.isoformat() if last_seen else None}
        for event_type, reason, count, last_seen in rows
    ]


def _eastern(col):
    return func.timezone("America/New_York", col)


async def _monthly_trend(db: AsyncSession, owner_id: str, config: dict, months: int = 12) -> List[AIAdminMonthlyPoint]:
    """Last `months` calendar months (Eastern) of this agent's AI cost/message count, oldest
    first - the "what have they been doing, month over month" history the 30-day daily_trend
    can't show once a month rolls off that window."""
    since = _month_start(datetime.now(timezone.utc)) - timedelta(days=31 * (months - 1))
    since = since.replace(day=1)
    month = func.to_char(_eastern(AIUsageEvent.created_at), 'YYYY-MM').label('month')
    stmt = (
        select(month, _spent(config), func.count(AIUsageEvent.id).filter(AIUsageEvent.blocked.is_(False)))
        .where(AIUsageEvent.owner_id == owner_id, AIUsageEvent.created_at >= since)
        .group_by(month)
        .order_by(month)
    )
    result = await db.execute(stmt)
    return [AIAdminMonthlyPoint(month=m, cost_usd=float(c), messages=int(n)) for m, c, n in result.all()]


async def _daily_trend(db: AsyncSession, since: datetime, config: dict, owner_id: Optional[str] = None) -> List[AIAdminDailyPoint]:
    day = func.date_trunc('day', _eastern(AIUsageEvent.created_at)).label('day')
    stmt = (
        select(day, _spent(config), func.count(AIUsageEvent.id).filter(AIUsageEvent.blocked.is_(False)))
        .where(AIUsageEvent.created_at >= since)
        .group_by(day)
        .order_by(day)
    )
    if owner_id:
        stmt = stmt.where(AIUsageEvent.owner_id == owner_id)
    result = await db.execute(stmt)
    return [
        AIAdminDailyPoint(date=d.strftime('%Y-%m-%d'), cost_usd=float(c), messages=int(n))
        for d, c, n in result.all()
    ]


@router.get("/helper-switch")
async def get_ai_helper_switch(db: AsyncSession = Depends(get_db), admin: UserModel = Depends(require_admin_user)):
    """The one switch for the bottom-right AI helper: on = every paying agent has it (10/30)."""
    from app.services.ai.ai_helper_access import helper_open_to_all
    return {"open_to_all": await helper_open_to_all(db)}


@router.put("/helper-switch")
async def set_ai_helper_switch(payload: dict, db: AsyncSession = Depends(get_db), admin: UserModel = Depends(require_admin_user)):
    from app.services.ai.ai_helper_access import helper_open_to_all, set_helper_open_to_all
    await set_helper_open_to_all(db, bool(payload.get("open_to_all")))
    logger.info("AI helper switch", extra={"event": "ai_helper_switch", "open_to_all": bool(payload.get("open_to_all"))})
    return {"open_to_all": await helper_open_to_all(db)}


@router.get("/overview", response_model=AIAdminOverview)
async def get_ai_overview(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        now = datetime.now(timezone.utc)
        month_start = _month_start(now)   # Eastern, like the rest of Admin
        day_start = _day_start(now)
        window_30d = now - timedelta(days=30)
        config = await ai_config_service.get_config(db)

        # Every cost row counts (blocked ones too - a refunded run was still paid for), priced by
        # _real_cost; plus Explore Nearby, which only the Places counters record.
        month_cost, month_messages, today_cost, all_time_cost, cma_month, cma_today = (
            await db.execute(
                select(
                    _spent(config, AIUsageEvent.created_at >= month_start),
                    func.count(AIUsageEvent.id).filter(AIUsageEvent.blocked.is_(False), AIUsageEvent.created_at >= month_start),
                    _spent(config, AIUsageEvent.created_at >= day_start),
                    _spent(config),
                    _spent(config, AIUsageEvent.created_at >= month_start, AIUsageEvent.event_type.in_(_CMA_EVENT_TYPES)),
                    _spent(config, AIUsageEvent.created_at >= day_start, AIUsageEvent.event_type.in_(_CMA_EVENT_TYPES)),
                )
            )
        ).first()
        places = await _places_spend(db)

        active_agents = (
            await db.execute(
                select(func.count(func.distinct(AIUsageEvent.owner_id))).where(AIUsageEvent.created_at >= window_30d)
            )
        ).scalar_one()

        blocked_month = (
            await db.execute(
                select(func.count(AIUsageEvent.id)).where(_a_real_block(), AIUsageEvent.created_at >= month_start)
            )
        ).scalar_one()

        reason_rows = (
            await db.execute(
                select(AIUsageEvent.block_reason, func.count(AIUsageEvent.id))
                .where(_a_real_block(), AIUsageEvent.created_at >= month_start)
                .group_by(AIUsageEvent.block_reason)
                .order_by(func.count(AIUsageEvent.id).desc())
                .limit(5)
            )
        ).all()

        cost_by_type_month = await _cost_by_type(db, month_start, config)
        if places["month"] > 0:
            cost_by_type_month["explore_nearby"] = places["month"]
        call_counts_month = await _call_counts_by_type(db, month_start)
        cost_by_provider_month: dict = {}
        for event_type, cost in cost_by_type_month.items():
            provider = _EVENT_TYPE_PROVIDER.get(event_type, "other")
            cost_by_provider_month[provider] = cost_by_provider_month.get(provider, 0.0) + cost

        global_budget_status: List[AIAdminBudgetStatus] = []
        for bucket, label, day_cost_key, month_cost_key, day_count_key, month_count_key in _BUDGET_BUCKETS:
            snapshot = await usage_snapshot(db, bucket)
            status_entry = AIAdminBudgetStatus(
                bucket=bucket, label=label,
                day_spent_usd=snapshot["day_cost_usd"], day_limit_usd=config.get(day_cost_key) if day_cost_key else None,
                month_spent_usd=snapshot["month_cost_usd"], month_limit_usd=config.get(month_cost_key) if month_cost_key else None,
                free_tier_note=_PLACES_FREE_TIER_NOTE.get(bucket),
            )
            # "How many more of this can we do?" - this month's REAL average cost per call for the
            # feature this bucket meters, divided into whatever budget is left under its ceiling.
            event_type = _BUCKET_EVENT_TYPE.get(bucket)
            month_limit = config.get(month_cost_key) if month_cost_key else None
            if event_type and month_limit:
                calls = call_counts_month.get(event_type, 0)
                spend = cost_by_type_month.get(event_type, 0.0)
                if calls > 0 and spend > 0:
                    avg_cost = spend / calls
                    remaining_usd = max(0.0, month_limit - snapshot["month_cost_usd"])
                    status_entry.est_remaining_calls_month = int(remaining_usd / avg_cost)
            if day_count_key or month_count_key:
                status_entry.day_count = snapshot["day_count"]
                status_entry.day_count_limit = config.get(day_count_key) if day_count_key else None
                status_entry.month_count = snapshot["month_count"]
                status_entry.month_count_limit = config.get(month_count_key) if month_count_key else None
            global_budget_status.append(status_entry)
        # Every kind of Google Maps call against its free monthly allowance - calls stop at the
        # line and wait for the 1st (google_free_tier.py, owner 10/26).
        from app.services import google_free_tier
        for row in await google_free_tier.month_usage(db):
            global_budget_status.append(AIAdminBudgetStatus(
                bucket=f"google_free:{row['sku']}", label=f"Google free calls - {row['sku'].replace('_', ' ')}",
                day_spent_usd=0.0, month_spent_usd=0.0, month_count=row["used"], month_count_limit=row["stops_at"],
                free_tier_note=f"Google gives {row['free']:,} free a month. Stops at {row['stops_at']:,} and waits for the 1st - never billed.",
            ))

        top_consumers = (
            await _top_consumers(db, "ip", config["ip_daily_cost_limit_usd"])
            + await _top_consumers(db, "device", config["device_daily_cost_limit_usd"])
        )
        top_consumers.sort(key=lambda c: c.spent_today_usd, reverse=True)

        return AIAdminOverview(
            total_cost_month_usd=float(month_cost or 0.0) + places["month"],
            total_cost_today_usd=float(today_cost or 0.0) + places["day"],
            total_cost_all_time_usd=float(all_time_cost or 0.0) + places["all"],
            cma_cost_month_usd=float(cma_month or 0.0),
            cma_cost_today_usd=float(cma_today or 0.0),
            messages_month=int(month_messages or 0),
            active_agents_30d=int(active_agents or 0),
            blocked_count_month=int(blocked_month or 0),
            top_block_reasons=[{"reason": r or "unknown", "count": int(c)} for r, c in reason_rows],
            daily_trend_30d=await _daily_trend(db, window_30d, config),
            cost_by_type_month=cost_by_type_month,
            cost_by_provider_month=cost_by_provider_month,
            global_budget_status=global_budget_status,
            top_google_places_consumers=top_consumers[:10],
            recent_provider_failures=await _recent_provider_failures(db, now - timedelta(hours=24)),
        )
    except Exception as e:
        logger.error(f"Failed to build AI usage overview: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load AI usage overview")


@router.get("/agents", response_model=List[AIAdminAgentSummary])
async def list_agent_ai_usage(
    period: str = "month",
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        now = datetime.now(timezone.utc)
        since = _month_start(now) if period == "month" else datetime(2000, 1, 1, tzinfo=timezone.utc)
        config = await ai_config_service.get_config(db)

        stmt = (
            select(
                AIUsageEvent.owner_id,
                UserModel.first_name,
                UserModel.last_name,
                UserModel.email,
                _spent(config),
                func.count(AIUsageEvent.id).filter(AIUsageEvent.blocked.is_(False)),
                func.coalesce(func.sum(AIUsageEvent.tool_call_count), 0),
                func.count(AIUsageEvent.id).filter(_a_real_block()),
                func.max(AIUsageEvent.created_at),
            )
            .join(UserModel, UserModel.id == AIUsageEvent.owner_id)
            .where(AIUsageEvent.created_at >= since)
            .group_by(AIUsageEvent.owner_id, UserModel.first_name, UserModel.last_name, UserModel.email)
            .order_by(_spent(config).desc())
        )
        rows = (await db.execute(stmt)).all()
        # Scoped to agents with AI feature usage (the join above) - an agent whose only spend is
        # IDX-site Explore Nearby traffic and who has never used chat/CMA AI/etc won't appear
        # here, since there'd be nothing else to show on this particular leaderboard. Their site
        # spend is still fully tracked (usage_budget_counters) and enforced regardless.
        return [
            AIAdminAgentSummary(
                owner_id=owner_id,
                name=f"{first} {last}".strip(),
                email=email,
                cost_usd=float(cost or 0.0),
                messages=int(messages or 0),
                tool_calls=int(tool_calls or 0),
                blocked_count=int(blocked or 0),
                last_active=last_active,
                **await _agent_site_spend(db, owner_id, config),
            )
            for owner_id, first, last, email, cost, messages, tool_calls, blocked, last_active in rows
        ]
    except Exception as e:
        logger.error(f"Failed to list agent AI usage: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load agent AI usage")


@router.get("/agents/{owner_id}", response_model=AIAdminAgentDetail)
async def get_agent_ai_usage_detail(
    owner_id: str,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        user = (await db.execute(select(UserModel).where(UserModel.id == owner_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

        config = await ai_config_service.get_config(db)
        totals = (
            await db.execute(
                select(
                    _spent(config),
                    func.count(AIUsageEvent.id).filter(AIUsageEvent.blocked.is_(False)),
                    func.coalesce(func.sum(AIUsageEvent.tool_call_count), 0),
                    func.count(AIUsageEvent.id).filter(_a_real_block()),
                    func.max(AIUsageEvent.created_at),
                ).where(AIUsageEvent.owner_id == owner_id)
            )
        ).first()
        cost, messages, tool_calls, blocked, last_active = totals

        tool_name_rows = (
            await db.execute(select(AIUsageEvent.tool_names).where(AIUsageEvent.owner_id == owner_id, AIUsageEvent.tool_names.is_not(None)))
        ).scalars().all()
        tool_usage: Counter = Counter()
        for names in tool_name_rows:
            tool_usage.update(names or [])

        window_30d = datetime.now(timezone.utc) - timedelta(days=30)

        return AIAdminAgentDetail(
            owner_id=owner_id,
            name=f"{user.first_name} {user.last_name}".strip(),
            email=user.email,
            cost_usd=float(cost or 0.0),
            messages=int(messages or 0),
            tool_calls=int(tool_calls or 0),
            blocked_count=int(blocked or 0),
            last_active=last_active,
            daily_trend=await _daily_trend(db, window_30d, config, owner_id=owner_id),
            monthly_trend=await _monthly_trend(db, owner_id, config),
            tool_usage=dict(tool_usage),
            cost_by_type=await _cost_by_type(db, datetime(2000, 1, 1, tzinfo=timezone.utc), config, owner_id=owner_id),
            **await _agent_site_spend(db, owner_id, config),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to load AI usage detail for {owner_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load agent AI usage detail")


@router.get("/failures")
async def get_recent_failures(
    days: int = 14,
    include_limits: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    """Every feature failure we already record, newest first - who, which feature, when, and the
    error (see admin_failures_service). `include_limits` also lists requests our own spending /
    abuse limits stopped (not errors)."""
    from app.services.admin_failures_service import recent_failures
    return await recent_failures(db, days=max(1, min(int(days), 90)), include_limits=include_limits)
