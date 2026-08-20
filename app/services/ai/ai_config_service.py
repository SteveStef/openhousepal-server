"""
Configuration for the AI Assistant, backed by a single SystemSettings row
(key "ai_assistant_config") instead of hardcoded constants scattered through the
feature, so limits and the model can be tuned without a deploy.

Falls back to conservative hardcoded defaults if the row doesn't exist yet, and
lazily seeds it on first read.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import SystemSettings

AI_CONFIG_KEY = "ai_assistant_config"

DEFAULT_CONFIG = {
    "model": "claude-haiku-4-5",
    "max_output_tokens": 700,
    "max_input_chars": 12000,

    # Rate limiting
    "requests_per_minute": 5,
    "max_concurrent_requests": 2,
    "daily_message_limit": 75,
    "monthly_message_limit": 1500,
    "monthly_cost_ceiling_usd": 5.0,

    # Platform-wide (ALL agents combined) chat safety ceiling - Layer 3, on top of the per-user
    # limits above. A last-resort circuit breaker, not a day-to-day limiter: sized so it should
    # essentially never trip under normal usage at current scale, only if usage collectively
    # spikes far beyond what any one agent's own $5/mo cap would suggest. Enforced atomically via
    # usage_budget_service.py (DB-backed reservation, race-free across workers), not a simple
    # read-then-compare - see ai_rate_limit_service.check_and_reserve.
    "chat_global_daily_cost_ceiling_usd": 25.0,
    "chat_global_monthly_cost_ceiling_usd": 300.0,
    # Conservative upper-bound cost reserved BEFORE a chat turn runs (worst case: max_iterations
    # model calls each near max_input_chars/max_output_tokens), trued up afterward with the real
    # cost via usage_budget_service.reconcile once actual token usage is known.
    "chat_global_reservation_estimate_usd": 0.03,

    # Orchestration budget (per user turn)
    "max_tool_calls_per_turn": 8,
    "max_iterations": 4,
    "request_timeout_seconds": 60,

    # Conversation replay window (messages, not tokens). Once a conversation's uncovered
    # (not-yet-summarized) message count exceeds this, it's auto-compacted - see
    # ai_compaction_service.py.
    "max_conversation_messages": 20,
    "max_compactions_per_conversation": 3,
    "compaction_max_output_tokens": 300,

    # Server-side hard caps on list-returning tools, applied regardless of what the
    # model asks for. See ai_tools.py.
    "result_limits": {
        "search_properties": 15,
        "search_leads": 20,
        "search_open_houses": 15,
        "get_open_house_guests": 25,
        "get_property_matches": 15,
        "get_similar_properties": 10,
        "list_recent_activity": 25,
    },

    # $ per million tokens, keyed by model id, for estimated_cost_usd bookkeeping.
    "model_pricing": {
        "claude-haiku-4-5": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
        "claude-sonnet-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0},
        "claude-opus-5": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
    },

    # Read Aloud (OpenAI TTS) - converts an already-generated Claude reply to speech.
    # Independent monthly cost ceiling from chat, per-agent, so voice can never eat into (or
    # be capped by) the $5 text budget above.
    "tts_model": "gpt-4o-mini-tts",
    "tts_voice": "alloy",
    "tts_max_chars": 2000,
    "tts_monthly_cost_ceiling_usd": 2.0,
    # Platform-wide TTS safety ceiling - same Layer 3 reasoning as chat's global ceiling above.
    "tts_global_daily_cost_ceiling_usd": 10.0,
    "tts_global_monthly_cost_ceiling_usd": 100.0,
    "tts_global_reservation_estimate_usd": 0.04,  # worst case: tts_max_chars, trued up after synthesis

    # Talk to OHP (OpenAI Realtime) - live voice conversation, own independent monthly cost
    # ceiling and its own session-level limits (browser connects directly to OpenAI over
    # WebRTC, so these are the only points this backend can actually enforce a budget - see
    # ai_realtime_service.py).
    "realtime_model": "gpt-realtime-2.1-mini",
    "realtime_voice": "marin",
    "realtime_max_session_seconds": 600,
    "realtime_max_idle_seconds": 45,
    "realtime_max_concurrent_sessions_per_user": 1,
    "realtime_daily_session_limit": 10,
    "realtime_monthly_session_limit": 150,
    "realtime_max_tool_calls_per_turn": 6,
    "realtime_max_tool_calls_per_session": 30,
    "realtime_monthly_cost_ceiling_usd": 3.0,
    # Platform-wide Realtime voice safety ceiling - same Layer 3 reasoning as chat's above.
    # Reserved once per session at start (realtime_max_session_seconds worst case), trued up
    # once the session ends with its real accumulated cost - see ai_realtime_service.py.
    "realtime_global_daily_cost_ceiling_usd": 15.0,
    "realtime_global_monthly_cost_ceiling_usd": 150.0,
    "realtime_global_reservation_estimate_usd": 0.30,

    # Area narrative generation (Website Builder communities) - one short, fact-grounded
    # paragraph per shared MarketArea, generated once at enrichment time and cached forever
    # after (see app/services/areas/area_enrichment_service.py). Own independent monthly
    # ceiling, same reasoning as tts/realtime above - infrequent (once per NEW town, ever) and
    # cheap, but still tracked/capped rather than left unbounded. Enforced GLOBALLY (all agents
    # combined via usage_budget_service's "area_narrative_global" bucket), never attributed to
    # whichever agent happened to be first to add a given community - a shared MarketArea's
    # enrichment is platform infrastructure, not personal usage, so no one agent's budget should
    # be charged for researching a town on everyone's behalf. AIUsageEvent.owner_id still records
    # who triggered it, for admin reporting - only the ceiling CHECK is unscoped.
    "area_narrative_monthly_cost_ceiling_usd": 2.0,
    "area_narrative_global_reservation_estimate_usd": 0.02,  # worst case: a few KB of facts JSON as input, max_tokens=250 output

    # AI-grounded community research (schools/parks/restaurants/etc + historical market context -
    # see area_research_service.py). FIVE grouped Haiku 4.5 requests per community, each capped at
    # 2-3 categories (schools; transport+medical; grocery+parks; restaurants+shopping+downtown;
    # market context - see _GROUP_SPECS), then a targeted per-category completion pass for
    # anything not genuinely resolved (empty with no credible "actually searched, found nothing"
    # explanation - see area_research_service.category_is_resolved), then - public_schools only,
    # and only if a real evidence conflict remains - one narrowly-scoped Sonnet fallback call.
    # This is a deliberate, multiply-tested choice (see area_research_service module docstring for
    # the full v3->v6 history): the goal shifted from minimizing cost/call-count to genuinely
    # complete pages - "the schema allowed an empty result" is not the same as "we confirmed the
    # emptiness is correct." Per-group/per-solo-call search caps live as code constants in
    # _GROUP_SPECS, not here.
    #
    # THREE independent cost guards, from smallest to largest scope:
    #   1. area_research_max_cost_per_community_usd - hard per-instance cap, checked before every
    #      completion-pass/fallback call within a single community's research job.
    #   2. area_research_global_daily/monthly_cost_ceiling_usd - a platform-wide safety ceiling
    #      ACROSS EVERY agent/community combined, reserved atomically (usage_budget_service.py,
    #      "area_research_global" bucket) BEFORE a job starts, at the size of #1 (the most a
    #      single job could ever spend), then trued up with the real spend once the job finishes.
    #      Deliberately NOT scoped to whichever agent triggered a given community's research
    #      first, and deliberately NOT a renamed/reinterpreted version of an older, much smaller
    #      per-agent-attributed value - this is new headroom sized to comfortably support
    #      pre-seeding many communities and onboarding more agents, not a tight day-to-day limit.
    #   3. Shared canonical MarketArea reuse (get_or_create_market_area) - the primary control on
    #      total VOLUME: the same real-world town is only ever researched once, platform-wide,
    #      no matter how many agents later add it to their own site.
    "area_research_model": "claude-haiku-4-5",
    "area_research_escalation_model": "claude-sonnet-5",
    "area_research_max_cost_per_community_usd": 1.0,
    "area_research_global_daily_cost_ceiling_usd": 50.0,
    "area_research_global_monthly_cost_ceiling_usd": 500.0,
    # $ billed per web_search tool invocation, in addition to normal token cost - Anthropic bills
    # this separately from tokens. This rate should be confirmed against Anthropic's current
    # published pricing; kept as its own config value (not hardcoded in area_research_service.py)
    # so it can be corrected without a code change if it's off.
    "area_research_web_search_cost_usd": 0.01,

    # $ per million tokens for OpenAI voice usage bookkeeping - real, published per-token
    # rates (not a flat per-character guess), used by estimate_tts_cost_usd() and
    # ai_realtime_service.py's incremental usage-based cost tracking.
    "openai_pricing": {
        "gpt-4o-mini-tts": {"input_text_per_mtok": 0.60, "output_audio_per_mtok": 12.0},
        "gpt-realtime-2.1-mini": {
            "text_in_per_mtok": 0.60, "text_out_per_mtok": 2.40,
            "audio_in_per_mtok": 10.0, "audio_out_per_mtok": 20.0,
        },
    },

    # Google Places "Explore Nearby" (public community/property pages - see
    # nearby_amenities_service.py get_explore_nearby/get_explore_place_details). Unauthenticated
    # and visitor-triggered, so unlike every ceiling above there is no per-user layer here at
    # all - these daily/monthly limits ARE the whole protection, enforced atomically via
    # usage_budget_service.py's "places_search"/"places_details" buckets (DB-backed, survives a
    # restart, race-free across workers - the in-memory counter this replaced did not have any
    # of those three properties). Search and Details are billed as separate Places SKUs at
    # different rates, so they get fully independent counters/limits, never combined into one.
    # Moved here from standalone GOOGLE_PLACES_EXPLORE_*_LIMIT env vars so these are tunable the
    # same DB-backed way as every other feature's limits, without a deploy.
    "places_explore_search_estimated_cost_usd": 0.032,
    "places_explore_search_daily_limit": 1000,
    # Capped at 4,999, one below Google's 5,000/month free allowance for Nearby Search Pro - same
    # "never actually cross into billed territory" pattern as places_explore_details_monthly_limit
    # below. See nearby_amenities_service.is_explore_nearby_available, which lets the frontend hide
    # the whole Explore Nearby section proactively once this is reached, rather than only finding out
    # after a visitor clicks a category and gets an error.
    "places_explore_search_monthly_limit": 4999,
    "places_explore_details_estimated_cost_usd": 0.020,
    "places_explore_details_daily_limit": 500,
    # Capped at 999, one below Google's 1,000/month free allowance for Place Details Enterprise, so
    # this feature can never actually cross into billed territory - once the monthly counter hits
    # 999, every further click is blocked (ExploreNearbyBudgetExceeded) until the monthly period
    # rolls over, same as any other budget-exhausted state. The frontend already degrades silently
    # when this fires (selectPlace's catch just stores null - see ExploreNearby.tsx), so a visitor
    # just sees a place with no rating shown, not an error.
    "places_explore_details_monthly_limit": 999,
    # How long a cached Explore Nearby result (a search or one place's rating/review count) stays
    # valid before it's asked of Google again - see ExploreNearbyCache in database.py. This is the
    # real lever on total spend, not just the limits above: without it, every visitor to the same
    # popular listing/area re-pays for an identical answer. Restaurants/shops don't meaningfully
    # change week to week, so a hit here costs nothing and doesn't count against the limits above
    # at all (never reserved, since no Google call happens).
    "places_explore_cache_ttl_days": 7,

    # AI-generated CMA analysis (see cma_ai_service.py) - the agent picks a subject + comps
    # (already narrowed/scored by the existing match-score algorithm), then this writes a short
    # per-comp note plus a final value estimate. First vision (photo-reading) use case in the
    # codebase. Haiku 4.5 chosen over Sonnet for cost - revisit if output quality is too thin.
    # Per-user layer (daily/monthly generation counts, event_type="cma_ai") mirrors chat's own
    # shape - see ai_rate_limit_service.reserve_cma_analysis. Global+emergency layer follows the
    # same reserve_global_and_emergency pattern as area_narrative/area_research above.
    "cma_ai_model": "claude-haiku-4-5",
    "cma_ai_max_output_tokens": 2000,
    "cma_ai_daily_generation_limit_per_user": 15,
    "cma_ai_monthly_generation_limit_per_user": 150,
    "cma_ai_daily_cost_ceiling_usd": 20.0,
    "cma_ai_monthly_cost_ceiling_usd": 150.0,
    # Worst case: subject + 5 comps, cma_ai_max_photos_per_property_cap photos each + a map image
    # per property, near cma_ai_max_output_tokens output. Trued up via reconcile_budget once the
    # real cost is known. Starting estimate, not yet validated against a real generation - revisit
    # once a handful of real calls have run (see the AI-Generated CMA plan's Phase A).
    "cma_ai_reservation_estimate_usd": 0.10,
    "cma_ai_default_max_photos_per_property": 3,
    "cma_ai_max_photos_per_property_cap": 6,  # server-side hard ceiling regardless of what the request asks for

    # The overall OpenHousePal emergency brake - the outermost of all three protection layers,
    # spanning EVERY paid AI/API feature above combined (chat + tts + realtime + area_narrative +
    # area_research + Places search + Places details), enforced atomically via
    # usage_budget_service.py's "platform_emergency" bucket - every reservation call in
    # ai_rate_limit_service.py AND nearby_amenities_service.py reserves against this same bucket,
    # so Places draws from the identical shared budget as the Anthropic/OpenAI features, not a
    # separate one.
    #
    # DAILY ($100) sits above every individual feature's own daily ceiling (highest is area
    # research's $50/day), so no single feature can exhaust the daily brake alone.
    #
    # MONTHLY ($200) is deliberately tighter than that same relationship: it's now BELOW area
    # research's own $500/mo ceiling and chat's own $300/mo ceiling. Those two per-feature
    # numbers are real, unchanged allowances (area research in particular is sized for
    # pre-seeding new towns and onboarding more agents, not a tight day-to-day limit) - but in
    # practice this $200/mo brake will engage before either one could ever be fully spent in a
    # single month, UNLESS essentially nothing else drew on the shared $200 that month. This is a
    # deliberate choice (explicit instruction: lower the monthly brake to $200 without touching
    # the feature-level numbers), not an oversight - flagged here since it means those two
    # feature ceilings' real headroom is smaller in practice than their own numbers suggest.
    "platform_emergency_daily_cost_ceiling_usd": 100.0,
    "platform_emergency_monthly_cost_ceiling_usd": 200.0,
}


class AIConfigService:
    """Process-local cache of the AI config row. Call invalidate_cache() after an
    admin edit to SystemSettings if one is ever added; otherwise a restart picks it up."""

    _cache: Optional[dict] = None

    @classmethod
    async def get_config(cls, db: AsyncSession) -> dict:
        if cls._cache is not None:
            return cls._cache

        stmt = select(SystemSettings).where(SystemSettings.key == AI_CONFIG_KEY)
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()

        if setting and setting.value:
            merged = cls._merge(DEFAULT_CONFIG, setting.value)
        else:
            merged = dict(DEFAULT_CONFIG)
            db.add(SystemSettings(key=AI_CONFIG_KEY, value=merged))
            await db.commit()

        cls._cache = merged
        return merged

    @classmethod
    def invalidate_cache(cls):
        cls._cache = None

    @staticmethod
    def _merge(defaults: dict, override: dict) -> dict:
        """Shallow-merges one level of nested dicts (result_limits, model_pricing) so a
        partially-set override row never drops keys the code expects to find."""
        merged = dict(defaults)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    @staticmethod
    def estimate_cost_usd(config: dict, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = config.get("model_pricing", {}).get(model)
        if not pricing:
            return 0.0
        return (
            (input_tokens / 1_000_000) * pricing["input_per_mtok"]
            + (output_tokens / 1_000_000) * pricing["output_per_mtok"]
        )

    @staticmethod
    def estimate_tts_cost_usd(config: dict, model: str, char_count: int) -> float:
        """OpenAI's POST /v1/audio/speech endpoint returns raw audio bytes only - no usage/
        token-count payload the way Claude's Messages API does - so exact per-request billing
        can't be read back after the call. This estimates it from `openai_pricing`'s real
        published per-token rates (never a flat, unrelated per-character price) plus two
        standard, documented approximations: ~4 characters per input text token (the common
        English-text tokenizer heuristic), and OpenAI's own published audio-token rate of
        ~1,200 tokens per minute of generated speech, applied to an average TTS speaking pace
        of ~150 words/minute (~750 characters/minute) to estimate output audio tokens from the
        input text length."""
        pricing = config.get("openai_pricing", {}).get(model)
        if not pricing:
            return 0.0
        input_tokens = char_count / 4
        minutes_of_audio = char_count / 750
        output_audio_tokens = minutes_of_audio * 1200
        return (
            (input_tokens / 1_000_000) * pricing["input_text_per_mtok"]
            + (output_audio_tokens / 1_000_000) * pricing["output_audio_per_mtok"]
        )


ai_config_service = AIConfigService()
