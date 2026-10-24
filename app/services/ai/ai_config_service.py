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

    # --- AI copywriter (Design Studio text, Blogs AI mode, Agent Hub bio polish) + the
    # Fair-Housing compliance check. Short single-shot Claude calls on the cheap model. Per-user
    # spend is the shared agent_monthly_ai_budget_usd_by_tier pool (same as CMA / Studio Media);
    # these are the platform-wide + reservation figures. See ai_rate_limit_service.reserve_copywrite.
    "copywrite_model": "claude-haiku-4-5",
    "copywrite_max_output_tokens": 1600,
    "copywrite_reservation_estimate_usd": 0.03,   # worst case ~a few K input + 1600 out on Haiku; trued up after
    "copywrite_global_daily_cost_ceiling_usd": 25.0,
    "copywrite_global_monthly_cost_ceiling_usd": 300.0,
    "copywrite_max_input_chars": 12000,           # hard cap on prompt+context length

    # --- AI Blog Writer (Studio -> Blogs "Write it with AI"): a guided interview -> one structured
    # full-article call, plus per-section refine calls. Uses the stronger model (it IS the product:
    # a finished, publishable article), still metered against the same shared per-agent
    # agent_monthly_ai_budget_usd_by_tier pool. See ai_blog_writer_service.py /
    # ai_rate_limit_service.reserve_blog_writer. Reservations are conservative worst cases
    # (full max_tokens of output), trued up to the real cost after each call.
    "blog_writer_model": "claude-sonnet-5",
    "blog_writer_max_output_tokens": 9000,
    "blog_writer_reservation_estimate_usd": 0.12,   # ~4K in + 9K out on Sonnet 5 worst case; trued up after
    "blog_refine_max_output_tokens": 2500,
    "blog_refine_reservation_estimate_usd": 0.05,
    "blog_writer_global_daily_cost_ceiling_usd": 25.0,
    "blog_writer_global_monthly_cost_ceiling_usd": 300.0,
    "blog_writer_max_input_chars": 16000,

    # --- Blog AI pipeline (Research -> Write -> Fact-check; ai_blog_pipeline_service.py). Two
    # engines so quality vs cost can be compared on real articles:
    #   "max"  - fresh topic research (more web searches), Opus writer, careful fact-check.
    #   "lean" - reuses the town's shared knowledge (IDX research + evidence bank) and only
    #            researches the gaps, Sonnet writer.
    # Billed to the agent's shared monthly AI allowance like every AI tool - there is NO per-article
    # count limit; `blog_pipeline_gate_usd` is the typical full cost, and a run only starts when the
    # agent's remaining allowance covers it (so a run can't push them past their plan's number).
    # The global ceilings are the platform-wide safety net in their own "blog_pipeline_global" bucket.
    "blog_pipeline_engines": {
        "max": {"research_model": "claude-sonnet-5-5", "research_searches": 8, "research_fetches": 3, "research_effort": "medium",
                "writer_model": "claude-opus-5-5", "writer_effort": "high",
                "check_model": "claude-sonnet-5-5", "check_effort": "high", "reuse_bank": False},
        "lean": {"research_model": "claude-sonnet-5-5", "research_searches": 4, "research_effort": "medium",
                 "writer_model": "claude-sonnet-5-5", "writer_effort": "medium",
                 "check_model": "claude-sonnet-5-5", "check_effort": "medium", "reuse_bank": True},
    },
    "blog_pipeline_default_engine": "max",  # product decision 2026-10-01: Premium for everyone
    "blog_pipeline_typical_usd": {"max": 0.9, "lean": 0.3},  # measured on a real article (10/24: 0.93 with the geography/payment checks) - powers the "% of your AI" note
    "blog_pipeline_gate_usd": {"max": 1.2, "lean": 0.5},
    "blog_pipeline_reservation_estimate_usd": {"max": 1.6, "lean": 0.7},
    "blog_pipeline_global_daily_cost_ceiling_usd": 75.0,
    "blog_pipeline_global_monthly_cost_ceiling_usd": 1000.0,
    "blog_pipeline_web_search_cost_usd": 0.01,  # Anthropic: $10 per 1,000 searches
    "blog_pipeline_cache_write_multiplier": 1.25,
    "blog_pipeline_cache_read_multiplier": 0.1,

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
        "claude-opus-5-5": {"input_per_mtok": 4.0, "output_per_mtok": 20.0, "cache_read_multiplier": 0.05},
        "claude-sonnet-5-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0, "cache_read_multiplier": 0.05},
        "gpt-6-astra": {"input_per_mtok": 10.0, "output_per_mtok": 50.0},
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
    # Max Explore Nearby requests (search + details combined) from one visitor (by IP, and
    # separately by device cookie) within any rolling 60-second window - see
    # nearby_amenities_service._check_minute_burst_limit. The day/month $ ceilings below bound
    # total damage; this bounds how FAST a script can get there, independent of dollar amount.
    # 20/minute comfortably covers even an enthusiastic real visitor clicking through every
    # category and several place cards in quick succession, while still throttling a burst.
    "places_explore_minute_request_limit": 20,
    "places_explore_search_estimated_cost_usd": 0.032,
    "places_explore_search_daily_limit": 1000,
    # Capped at 4,999, one below Google's 5,000/month free allowance for Nearby Search Pro - same
    # "never actually cross into billed territory" pattern as places_explore_details_monthly_limit
    # below. See nearby_amenities_service.is_explore_nearby_available, which lets the frontend hide
    # the whole Explore Nearby section proactively once this is reached, rather than only finding out
    # after a visitor clicks a category and gets an error.
    "places_explore_search_monthly_limit": 4999,
    # Requests rating+userRatingCount, which Google classifies as "Atmosphere" data - that puts
    # this specific call in the pricier "Place Details Enterprise + Atmosphere" SKU ($0.025/call,
    # 1,000 free/mo), not the cheaper Pro-tier rate ($0.020) this used to (incorrectly) assume.
    # Only affects the displayed $-would-have-cost estimate, not the hard monthly call cap below,
    # which was already set correctly against the real 1,000/mo free allowance.
    "places_explore_details_estimated_cost_usd": 0.025,
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

    # Per-requester daily cost ceilings on top of the platform-wide places_search/
    # places_details/platform_emergency ceilings above - those protect total platform spend,
    # but say nothing about how fast any ONE requester can burn through the shared daily
    # allowance (a bot doing a million Explore Nearby calls in a row would otherwise exhaust
    # the whole platform's daily budget for every real visitor on every agent's site in well
    # under a minute). See nearby_amenities_service._check_and_reserve_explore_budget_locked -
    # each identity dimension gets its own independent daily bucket, all reserved atomically
    # alongside the platform buckets. agent_site_daily is a strict subset of agent_total_daily
    # (the same site-driven spend is reserved against both) - until other agent-triggered spend
    # (CMA AI, chat, TTS) also reserves against agent_total_daily, the tighter site cap is the
    # one that actually binds first in practice.
    "ip_daily_cost_limit_usd": 3.0,
    "device_daily_cost_limit_usd": 3.0,
    "agent_site_daily_cost_limit_usd": 4.0,
    "agent_total_daily_cost_limit_usd": 5.0,

    # AI-generated CMA analysis (see cma_ai_service.py) - the agent picks a subject + comps
    # (already narrowed/scored by the existing match-score algorithm), then this writes a short
    # per-comp note plus a final value estimate. First vision (photo-reading) use case in the
    # codebase. Upgraded from Haiku 4.5 to Sonnet 5 (2026-09) - the system prompt's dense rule set
    # (never invent a number, exact utility language, cross-check every comp before submitting,
    # appraiser-style reasoning) needs the stronger model's reliability, not just more photos.
    # Per-user layer is now the shared agent_monthly_ai_budget_usd_by_tier pool below (not a
    # CMA-only generation-count ceiling) - see ai_rate_limit_service.reserve_cma_analysis. Global+
    # emergency layer follows the same reserve_global_and_emergency pattern as area_narrative/
    # area_research above.
    "cma_ai_model": "claude-sonnet-5",
    # Dedicated final-valuation authority. Claude still performs the multimodal comp analysis;
    # V10 still computes the deterministic benchmark. This separate text-only pass receives the
    # completed evidence packet and owns the client-facing FMV/range/confidence when its strict
    # output passes deterministic factual QA.
    "cma_reasoning_model": "gpt-6-astra",
    "cma_reasoning_effort": "medium",
    # 12000 (was 6000): high-effort reasoning plus the full JSON was being cut off at 6000. A cut-off
    # response is retried once at 1.75x this budget (cma_ai_service._REASONING_RETRY_TOKEN_MULT).
    "cma_reasoning_max_output_tokens": 12000,
    "cma_reasoning_request_timeout_seconds": 180,
    "cma_reasoning_reservation_estimate_usd": 0.75,
    # Worst single call: 21,000 output tokens (12000 x 1.75) x $50/Mtok = $1.05, plus input
    # (a ~200k-char evidence packet is ~50k tokens x $10/Mtok = $0.50) -> $1.55. Was 1.0, which
    # the doubled output budget alone would have failed. Over this, the CMA uses the V10 benchmark.
    "cma_reasoning_max_cost_per_generation_usd": 1.6,
    # Was 2000 - with 5 comps (MAX_COMPS) each needing a note + strength + full quality rating
    # object, that consistently left no room left for the final subject_estimate field, so every
    # generation truncated (stop_reason "max_tokens") with comps written but no value estimate at
    # all - confirmed across 5 back-to-back generations on 2026-08-22. Output tokens are cheap
    # next to a generation's real cost driver (input tokens from photos, ~100K+) - even this
    # doubled cap adds well under a few cents and stays comfortably under
    # cma_ai_max_cost_per_generation_usd below.
    # Raised 4000 -> 12000 (2026-09-26): a live seller run (~158K input tokens) still hit
    # max_tokens before subject_estimate, forcing the text-only estimate fallback (no photo
    # evidence). Billing is on tokens actually written, so the higher ceiling only costs what a
    # report really needs; worst case adds ~$0.08 at Sonnet 5 output pricing.
    "cma_ai_max_output_tokens": 12000,
    # CMA no longer has its own per-tier generation-count ceiling (2026-09) - spend is gated by the
    # shared agent_monthly_ai_budget_usd_by_tier pool instead (same pool Studio Media draws from),
    # so an agent isn't double-limited by an artificial count on top of their real dollar budget.
    "cma_ai_daily_cost_ceiling_usd": 50.0,
    "cma_ai_monthly_cost_ceiling_usd": 150.0,
    # Worst case: subject + 5 comps (6 properties) x cma_ai_max_photos_per_property_cap photos
    # each = 84 photos, near cma_ai_max_output_tokens output. Trued up via reconcile_budget once
    # the real cost is known - this is a conservative upper bound, not the real cost (projected
    # real cost at Sonnet 5 + the 14-photo cap: ~$0.25-0.28/generation, per Sonnet's 2x Haiku
    # per-Mtok pricing x 1.4x the old 10-photo volume).
    "cma_ai_reservation_estimate_usd": 0.45,
    # Real hard cap enforced in cma_ai_service.py BEFORE calling Claude - computed from the actual
    # photos/text about to be sent (real per-image token counts, not a guess), not just this
    # config number alone. A generation projected to cost more than this is refused outright
    # (reservation refunded, nothing charged) rather than allowed through and merely logged. Sized
    # as a generous sanity backstop against a bug/edge-case (not a day-to-day limiter) - real cost
    # is projected around $0.25-0.28, comfortably under this. Raised 0.5 -> 0.75 alongside the
    # 12000 output cap: the pre-check counts the FULL output cap as worst case, so a photo-heavy
    # report (~200K input = $0.40 + $0.12 output) would otherwise be refused before it starts.
    "cma_ai_max_cost_per_generation_usd": 0.75,
    # 14 total per property (subject + each comp): the first 12 in Bright's own MediaDisplayOrder
    # (the listing agent's own "best of" lead shots) plus the LAST 2 (often different content - a
    # floor plan, an aerial, a misc. angle a pure top-14 slice would miss) - see _photo_urls.
    "cma_ai_default_max_photos_per_property": 14,
    "cma_ai_max_photos_per_property_cap": 14,  # server-side hard ceiling regardless of what the request asks for

    # Studio AI Property Media (see app/services/studio_media_service.py) - twilight / lights-on
    # / virtual-staging / photo-enhancement edits via OpenAI gpt-image-2, plus photo-to-video via
    # Runway gen4.5. Available to IDX and PRO_NO_MLS only. Same three-layer guardrail shape as
    # CMA AI: per-user generation counts (event_type "studio_media_image" / "studio_media_video"),
    # a per-generation hard cost cap, and a platform-wide global+emergency reservation. Provider
    # and model are config so a different model can be swapped in (Veo / FLUX) without code
    # changes - see studio_media/providers/registry.py.
    # NOTE: the dollar figures below are conservative estimates, not measured - revisit against
    # live OpenAI / Runway billing once there's real usage.
    "studio_media_image_provider": "openai",
    "studio_media_image_model": "gpt-image-2",
    # low | medium | high | auto. Real gpt-image-2 cost per 1024px edit: low ~$0.006,
    # medium ~$0.05, high ~$0.17-0.21 (output image tokens dominate). "medium" is the default
    # - a good balance for listing-photo edits; bump to "high" here if quality needs it.
    "studio_media_image_quality": "medium",
    "studio_media_image_size": "auto",      # "auto" keeps the source photo's own dimensions
    # Optional refine passes: after the first edit, feed the result back in N-1 more times with
    # a "polish, change nothing" prompt. 1 = single pass (default). 2 at "low" quality
    # (~$0.006 x2) is a cheap way to try to clean up a rough first pass - but each pass is
    # another chance for the model to drift the architecture, so keep it small.
    "studio_media_image_passes": 1,
    # gpt-image-2 token prices ($ per 1M) - used to bill the REAL cost from each response's
    # usage block instead of a flat estimate. text input / image input / image output.
    "studio_media_image_price_text_input_per_mtok": 5.0,
    "studio_media_image_price_image_input_per_mtok": 8.0,
    "studio_media_image_price_image_output_per_mtok": 30.0,

    # "fal" (Hailuo 2.3 FAST Pro, native 1080p, flat-fee/clip, then one Topaz Starlight Fast 2
    # generative-detail pass on the finished stitched video - see fal_video.py) is the default as
    # of 2026-09: one simple product ("AI Property Video"), real/predictable ~$4.00-4.10 total for
    # a typical 7-photo/~30s video (7 x $0.33 render + ~29.5s x $0.06 Topaz upscale). Switched
    # Fast Pro -> Pro -> back to Fast Pro (2026-09-04/05): Pro's fidelity gain was real but the
    # bigger lever for "does this look impressive" turned out to be the upscale VENDOR (Topaz's
    # generative Starlight models vs. ByteDance's deliberately faithful ones - see
    # studio_media_video_upscale_vendor below), not the render model - so Fast Pro's real ~$1.12/
    # video saving is back on the table without giving up the visible quality win. Requires FAL_KEY.
    # "runway" (gen4_turbo, 720p, then either a free local ffmpeg 4K upscale or - only when it's
    # cheap enough, see poll_asset - a paid Magnific 4K AI-detail pass) and "minimax" (Hailuo H3,
    # native 2K, no upscale step) both still work as available-but-not-default alternatives.
    "studio_media_video_provider": "fal",
    # Unused by the fal provider itself (fal identifies the model by endpoint, not this field) -
    # kept accurate for whichever provider is actually configured above. "gen4_turbo" for runway
    # (~5 credits/sec = $0.05/sec, ~$0.25/5s clip; "gen4.5" is the pricier ~$0.60/clip cinematic
    # option), "MiniMax-H3" for minimax (or "MiniMax-H3-Max", which drops 2K support).
    # Reverted back to "Fast Pro" 2026-09-05: briefly tried plain "Pro", hoping it would reduce
    # the mid-clip hallucination where the model invents an entirely different, fabricated house
    # partway through a clip (see the docstring on build_video_motion_prompt in prompts.py for the
    # full incident notes). Pro reproduced the EXACT same failure, at ~50% higher cost per clip -
    # so there's no quality/reliability case for paying more here. The root cause isn't the model
    # tier; it reproduces on both.
    "studio_media_video_model": "fal-ai/minimax/hailuo-2.3-fast/pro/image-to-video",
    # Real published flat price per Hailuo clip (fal.ai's own pricing page) - $0.33 for "Fast Pro"
    # above, $0.49 for plain "Pro". Config-driven (not a hardcoded constant in
    # fal_video.py/studio_media_service.py) so pricing stays correct if fal changes it, or the
    # model is swapped, without a code deploy - see fal_video.py's submit() and
    # _clip_cost_estimate_usd.
    "studio_media_video_hailuo_price_per_clip_usd": 0.33,
    # Fal's Hailuo endpoint defaults `prompt_optimizer` to TRUE (confirmed against fal's own API
    # docs, 2026-09-05) - it silently rewrites/expands the submitted prompt before generation. Our
    # motion prompts (build_video_motion_prompt / _VIDEO_MOTION_HINTS in prompts.py) are heavily
    # hand-tuned with exact degree caps and explicit "never a zoom/pan/sweep" wording, iterated
    # several times specifically to control how dramatic the camera move reads - an optimizer
    # rewriting that on the way in is a plausible cause of the inconsistent "sometimes barely
    # moves, sometimes swooshes" results seen across renders (see the pan_left/pan_right history
    # above). Off by default so our exact wording reaches the model unmodified; flip to True only
    # to deliberately test whether fal's own rewriting helps.
    "studio_media_video_prompt_optimizer": False,
    "studio_media_video_ratio": "1280:720",
    # AI Video = up to 7 photos, one clip each (random camera move), concatenated server-side
    # (bundled ffmpeg) into one ~35s marketing video, then upscaled once. Briefly lowered to 5
    # (2026-09-05) then reverted back to 7 (2026-09-06, explicit decision) - the shorter runtime
    # wasn't worth losing 2 shots of coverage; 7 x 5s = 35s, still one clip per photo at Hailuo's
    # fixed ~5s/clip.
    "studio_media_video_max_photos": 7,
    "studio_media_video_seconds_per_clip": 5,
    "studio_media_video_duration_seconds": 5,  # legacy alias, kept for any external reader
    # UNUSED since 2026-09-30 - Day -> Twilight now renders on the normal pipeline above (see
    # _run_video). Kept so old config rows still parse. Original note: the timelapse style
    # bypassed the default fal/Hailuo pipeline above for this one style: fal's Hailuo endpoint has
    # no duration parameter at all (fixed ~5s native output regardless of what's requested - see
    # fal_video.py's submit()), so it can never produce the single continuous 10s clip this style
    # needs. Runway's gen4.5 DOES accept a real `duration` field (5 or 10s - see runway_video.py's
    # submit()), so _run_video overrides provider/model/seconds_per_clip to these three keys, and
    # only for this style's own request - every other style still renders on the config above
    # unchanged. One photo in, one clip out (no stitching) - see create_asset's video_style check.
    "studio_media_video_timelapse_provider": "runway",
    "studio_media_video_timelapse_model": "gen4.5",
    "studio_media_video_timelapse_duration_seconds": 10,
    # Bright MLS burns a watermark into the lower-left of synced listing photos. Cropped off this
    # fraction of the left+bottom edges (origin kept top-right) of the SOURCE STILL IMAGE, before
    # it's ever sent to Hailuo - see _runway_fetchable_url. Changed from a post-generation video
    # crop to a pre-generation image crop (2026-09-04): cropping the finished video meant Hailuo
    # spent generation quality animating a watermark that was just going to be cropped out anyway,
    # and the crop+rescale-back-to-size pass was an extra re-encode on every video for no benefit.
    # Cropping the still first means Hailuo never sees the watermark at all and the stitched output
    # needs no debrand pass before the AI upscale. Set to 0 to disable. Applied to every source
    # photo (an uploaded photo has no watermark, but a ~7% edge trim on it is harmless).
    "studio_media_video_debrand_crop_pct": 0.07,
    # Testing toggle (2026-09-05/06) - off again now that the quality-comparison round is done.
    # When on, a finished video also stores a second pre-2K-upscale comparison video, and the
    # Results tab shows a "Testing: compare quality" panel with it. Frontend panel itself was
    # removed 2026-09-06 (no longer needed) - this flag now only controls the extra backend
    # video/S3-upload work, which stays skipped while off.
    "studio_media_video_debug_store_intermediates": False,
    # Local/free fallback output height when the paid AI upscale does not finish. Keep this
    # aligned with the intended ByteDance 2K finish so fallback videos do not unexpectedly land
    # as heavier 4K files from a plain ffmpeg scale.
    "studio_media_video_output_height": 1440,
    # Quality pass applied in the same ffmpeg step that scales the stitched 720p clips up to the
    # output height. All free (no vendor call) - just more CPU and a bigger file - but a real
    # perceptual jump over a plain lanczos resize on Runway's soft gen4_turbo output:
    #   denoise (on the 720p frames, before scaling) -> high-quality upscale -> contrast-adaptive
    #   sharpen (on the 4K frames) -> a proper x264 encode instead of "veryfast / crf 22".
    # Set studio_media_video_enhance to false to fall back to the plain fast resize.
    "studio_media_video_enhance": True,
    "studio_media_video_enhance_denoise": "hqdn3d=2:1.5:4:4",     # "" to skip
    # Pre-sharpen the 720p luma, then contrast-adaptive sharpen after the upscale. Aggressive on
    # purpose - upscaled gen4_turbo footage is soft; dial down (or clear) if it rings/haloes.
    "studio_media_video_enhance_sharpen": "unsharp=5:5:0.9:5:5:0.0,cas=0.8",
    "studio_media_video_scale_flags": "lanczos+accurate_rnd+full_chroma_int",
    # Only used for the local ffmpeg fallback (when Magnific ML upscale is off or fails) - so it
    # doesn't need to be maxed out. `slow`/`crf 17` produced ~100 MB files with no visible gain
    # over the soft 720p source; `medium`/`crf 20` is a saner fallback.
    "studio_media_video_finish_preset": "medium",
    "studio_media_video_finish_crf": 20,
    # ML upscale, run automatically after the stitch (no button) using the SAME provider that
    # rendered the clips (its own submit_video_upscale()/poll() - see fal_video.py/runway_video.py/
    # minimax_video.py) - no separately-configurable upscale provider/model/creativity/flavor
    # anymore now that the default pipeline (fal: Hailuo render + ByteDance upscale) treats
    # upscaling as an integral, always-affordable step rather than an optional paid luxury pass
    # (that gating logic - _estimate_magnific_video_cost_usd et al - only ever existed because
    # Runway/Magnific's 4K creative pass could run $9-13 on a full reel; ByteDance's real cost is
    # ~$0.86 for the same reel, cheap enough to just always run). Still gated by the standard
    # budget-reservation check like everything else. If a provider has nothing to upscale (fal's
    # Hailuo clips are short/fixed-duration, MiniMax-H3 already renders natively at 2K), this
    # simply doesn't fire - see poll_asset.
    # Settled 2026-09-06 as the actual go-to, not a temporary test setting: real side-by-side
    # comparison (enhance-on+upscale-off vs. enhance-off+upscale-on, same photos) came back with
    # enhance-off+upscale-on both cheaper AND better - contradicts the 2026-09-05 A/B/C test's
    # conclusion that the enhance step was a real win, but that was a single-clip test on 2
    # different photos; this was a real full video on the agent's own actual content. Trusting the
    # more direct, real-world result. See studio_media_video_photo_enhance_enabled below (paired).
    "studio_media_video_upscale_enabled": True,
    # Which fal.ai-hosted upscale vendor to use - both are real, both live on fal.ai:
    #   "bytedance" - deliberately faithful/mild, doesn't invent detail not in the source.
    #   "topaz" (Starlight models) - explicitly REBUILDS/invents detail not in the source, the
    #     closer equivalent to the old Runway/Magnific "creative" upscaler's look, at a fraction
    #     of Magnific's per-frame cost.
    # Switched topaz -> bytedance back 2026-09-05, after a real A/B/C test (single-clip, 2 real
    # listing photos) confirmed the "image-first" architecture instead: do the creative
    # detail-injection ONCE per still photo (Magnific Precision v2, see
    # _enhance_photo_for_video/studio_media_video_photo_enhance_* below) before Hailuo ever
    # renders it, rather than paying a generative upscaler to re-invent detail on every one of
    # ~150 video frames. The enhanced-photo + faithful-ByteDance-finish combo won the comparison
    # outright - visibly sharper molding/rug/fabric detail than either the raw-photo baseline or
    # Topaz's Starlight passes, at roughly half Topaz HQ's cost. ByteDance's job now is exactly
    # what its name says: a faithful finish, not a second creative pass - the creativity already
    # happened on the still.
    "studio_media_video_upscale_vendor": "bytedance",    # bytedance | topaz
    # ByteDance's real published standard-tier rate by target resolution - the ONE place this
    # price table is defined; fal_video.py and studio_media_service.py both read it from here
    # instead of carrying their own hardcoded copies (pro tier is 10x any of these - never set it).
    # Only relevant when studio_media_video_upscale_vendor == "bytedance".
    "studio_media_video_upscale_price_per_second_usd_by_resolution": {
        "1080p": 0.0072, "2k": 0.0144, "4k": 0.0288,
    },
    # Topaz Starlight settings (studio_media_video_upscale_vendor == "topaz"). upscale_factor 1.0 =
    # same output size as the input (our own 1080p compact base), just Topaz's generative
    # detail-rebuild applied - not an upscale in the literal resolution sense, per the explicit
    # "no 2K/4K, just Starlight at 1080p" decision.
    # Switched Fast 2 -> HQ (2026-09-05): a real Fast 2 test came back with a visible wavy/faceted
    # texture across the frame - Fast 2 is the cheapest/speed-optimized Starlight tier (fewer
    # diffusion steps), which reads as exactly the kind of per-frame inconsistency that shows up
    # as. HQ ("maximum quality on high-resolution sources") is one of the 4 non-Fast tiers, all
    # priced the same (2x Fast 2) per fal's own model page. Price is fal's real published rate for
    # this tier (Starlight HQ, <=1080p, <=30fps) - Topaz's own API response has no duration field
    # to true this up against later, so it's the deterministic final cost, computed once at submit
    # time from our own known input duration (see fal_video.py's _submit_topaz_upscale).
    "studio_media_video_upscale_topaz_model": "Starlight HQ",
    "studio_media_video_upscale_topaz_factor": 1.0,
    "studio_media_video_upscale_topaz_price_per_second_usd": 0.12,
    # Changed 4k -> 2k (2026-09-04): a real test video came back visibly over-processed ("washed
    # out", over-sharpened) at 4K - a full 4x pixel-count jump from Hailuo's native 1080p source is
    # a lot for the "standard" (adaptive-algorithm, not model-based) tier to do cleanly in one pass.
    # 2K is a gentler upscale from the same source (half the resolution jump, half the cost) and
    # already exceeds what a phone/social-feed screen shows anyway - the actual viewing context
    # this feature is used for.
    # The next 3 keys apply now that studio_media_video_upscale_vendor == "bytedance" is active
    # again (2026-09-05) - kept under the same names the whole time so switching vendors is a
    # one-line change, not a rebuild.
    "studio_media_video_upscale_resolution": "2k",       # 1080p | 2k | 4k
    # "high" applies mild enhancement and preserves source texture (fal's own docs) - switched
    # back to "high" from "medium" alongside the topaz->bytedance switch above: ByteDance's job is
    # now a faithful finishing pass, not a second creative one - the creativity already happened
    # on the still via studio_media_video_photo_enhance_* below, so "medium"'s extra processing on
    # top of that would be redundant/risk compounding into an over-processed look.
    "studio_media_video_upscale_fidelity": "high",       # high | medium
    # fal's ByteDance upscaler ships presets tuned per source-content type: "general", "ugc",
    # "short_series", "aigc" (AI-generated content), "old_film". Our source is ALWAYS Hailuo's own
    # AI-generated output, not real camera footage - "aigc" is the preset actually built for that,
    # rather than "general" (the API default, tuned for arbitrary/unknown source video).
    "studio_media_video_upscale_enhancement_preset": "aigc",
    # Back down 5.00 -> 1.50 (2026-09-05, back on ByteDance): $0.0144/sec at 2K makes even a long
    # ~40s opt-in upscale only ~$0.58 - real headroom above that without carrying Topaz HQ's
    # $0.12/sec sizing around for a vendor that's no longer selected.
    "studio_media_video_upscale_max_cost_usd": 1.50,     # per-call cap for the opt-in /upscale endpoint
    # Per-photo "light enhancement" pass (Magnific Precision v2) run BEFORE Hailuo ever sees the
    # photo - see _enhance_photo_for_video in studio_media_service.py. This is what's actually
    # injecting the creative fine-detail now (crown molding, rug pattern, fabric weave), confirmed
    # via a real A/B/C test (2026-09-05): enhanced-still+Hailuo+ByteDance visibly beat both the
    # raw-photo baseline AND every Topaz Starlight pass tried on the finished video. Deliberately
    # lighter settings than the still-image editor's own studio_media_image_upscale_* defaults
    # (scale_factor 2 is the same - it's the smallest real step, an enum of 2/4/8/16 confirmed via
    # a live probe against Runway's API - but ultra_detail/sharpen/smart_grain are all toned down)
    # since this version only has to survive being re-rendered by Hailuo, not stand alone as a
    # final deliverable image.
    # Settled OFF 2026-09-06 (paired with studio_media_video_upscale_enabled = True above) - a
    # direct real-video comparison found enhance-off+upscale-on both cheaper AND better-looking
    # than enhance-on+upscale-off. Saves $0.25/photo for a result preferred over the pricier
    # option - this is now the actual go-to config, not an experiment.
    "studio_media_video_photo_enhance_enabled": False,
    "studio_media_video_photo_enhance_provider": "runway",
    "studio_media_video_photo_enhance_model": "magnific_precision_upscaler_v2",
    "studio_media_video_photo_enhance_scale_factor": 2,
    "studio_media_video_photo_enhance_ultra_detail": 25,
    "studio_media_video_photo_enhance_sharpen": 10,
    "studio_media_video_photo_enhance_smart_grain": 5,
    "studio_media_video_photo_enhance_flavor": "photo",
    "studio_media_video_photo_enhance_max_wait_s": 90,
    # Real cost confirmed via a live Runway call (25 credits = $0.25) - not an estimate.
    "studio_media_video_photo_enhance_cost_usd": 0.25,
    # Raised from 30 -> 40 (2026-09-04): at the real max_photos=7 x seconds_per_clip=5 = 35s, every
    # full-length video was landing OVER the old 30s ceiling and silently falling back to the free
    # local ffmpeg upscale instead of the real paid ByteDance 4K pass - so the "always run it, it's
    # cheap" intent above was never actually reached for a normal 7-photo video. 40 clears the real
    # 35s max with headroom and still respects Runway's own ~40s video_upscale input ceiling (this
    # value is shared across both providers - see poll_asset/finish_video_from_base). ByteDance
    # itself (fal, the current default) has no documented duration limit, so this is just Runway's
    # real technical ceiling, not a cost heuristic - a 35s upscale is only ~$1.01 either way.
    "studio_media_video_upscale_max_seconds": 40,
    # Runway/Magnific only now (see _finish_video_runway_legacy in studio_media_service.py) - the
    # compact intermediate base Magnific's upscale needs before it can run. fal/Hailuo (the
    # default pipeline) used to build this same kind of base too, despite this key's own comment
    # already saying it shouldn't - fixed 2026-09-05: each Hailuo clip is now upscaled
    # individually, straight from its own native output, with no resize/re-encode step first (see
    # _finish_video_per_clip). Ignored by providers that don't use it.
    "studio_media_video_upscale_base_height": 1080,
    # Still-image edits: OFF by default so edited photos stay at the exact source (Bright MLS)
    # dimensions and remain drop-in uploadable to Bright. When on, the result is sent through
    # Runway's Magnific Precision v2 upscaler (polled inline; falls back to the un-upscaled edit
    # on error/timeout) - which changes the pixel dimensions, so only enable it for non-MLS use.
    "studio_media_image_upscale_enabled": False,
    "studio_media_image_upscale_provider": "runway",
    "studio_media_image_upscale_model": "magnific_precision_upscaler_v2",
    # Fixed 2026-09-05: "resolution"/"strength" aren't real fields on Runway's image_upscale
    # endpoint (confirmed via a live probe - Runway rejected them as unrecognized keys). The real
    # Magnific Precision v2 fields, confirmed the same way: scale_factor (2-16x), ultra_detail
    # (0-100 - the actual detail-injection/creativity dial), sharpen, smart_grain (0-100 each),
    # flavor (sublime | photo | photo_denoiser - "photo" fits real estate).
    "studio_media_image_upscale_scale_factor": 2,
    "studio_media_image_upscale_ultra_detail": 30,
    "studio_media_image_upscale_sharpen": 7,
    "studio_media_image_upscale_smart_grain": 7,
    "studio_media_image_upscale_flavor": "photo",
    "studio_media_image_upscale_estimate_usd": 0.25,
    "studio_media_image_upscale_max_wait_s": 150,        # inline poll budget before falling back
    # Disabled (2026-09-05) - this was Runway/Magnific's real ~30s input ceiling for its
    # video_upscale endpoint (see runway_video.py's own "Input <= 30s" note), set here as a 29.5s
    # safety trim back when Runway was the upscale vendor. It was left in place after the switch
    # to fal/Topaz (which has no such limit - Topaz's real ceiling is 5 minutes) and was silently
    # chopping ~5.5s off the tail of every full 7-photo x 5s = 35s video, which is virtually the
    # WHOLE 7th clip (seconds 30-35) - the "7th photo never shows" bug. 0 = no trim; the real
    # length ceiling now is studio_media_video_upscale_max_seconds (40) above.
    "studio_media_video_max_output_seconds": 0,
    # Per-agent hard cap on total stored media ACROSS THE WHOLE APP - AI Media, Design Studio,
    # Blogs, brand logos, manual listing photos, agent/sale photos - tracked in the storage_items
    # ledger (storage_ledger_service). At/over the cap, any new upload or generation is blocked
    # (507) until the agent deletes something. `_default` applies to every agent; `_by_tier`
    # overrides per plan. 1 GiB = 1073741824, 2 GiB = 2147483648.
    "agent_storage_cap_bytes_default": 2147483648,
    "agent_storage_cap_bytes_by_tier": {"IDX": 2147483648, "PRO_NO_MLS": 2147483648},

    # Per-user generation-count ceilings, by tier (IDX + PRO_NO_MLS are the only tiers with
    # access). A "video" = one full multi-clip generation regardless of photo count. Capped at
    # 10/month per the product decision (each is up to ~$2).
    "studio_media_image_daily_generation_limit_by_tier": {"IDX": 40, "PRO_NO_MLS": 40},
    "studio_media_image_monthly_generation_limit_by_tier": {"IDX": 400, "PRO_NO_MLS": 400},
    "studio_media_video_daily_generation_limit_by_tier": {"IDX": 10, "PRO_NO_MLS": 10},
    "studio_media_video_monthly_generation_limit_by_tier": {"IDX": 10, "PRO_NO_MLS": 10},

    # Conservative upper-bound reserved BEFORE a generation runs, trued up via reconcile_budget
    # once the real cost is known (or refunded on failure). gpt-image-2 "high" 1024px edit is
    # roughly $0.17-0.25; a 5s Runway gen4.5 clip is roughly $0.75-1.25.
    # Reserved before a run, trued up to the REAL cost afterward (openai usage tokens / runway
    # duration x price). Estimates are a touch above the "high"/actual worst case so a run is
    # never under-reserved.
    "studio_media_image_reservation_estimate_usd": 0.25,
    "studio_media_image_max_cost_per_generation_usd": 0.40,
    "studio_media_image_global_daily_cost_ceiling_usd": 40.0,
    "studio_media_image_global_monthly_cost_ceiling_usd": 200.0,

    # Sized for the current default pipeline at the 5-photo cap: per-photo enhancement (5 x $0.25
    # = $1.25) + Hailuo 2.3 FAST PRO render (5 x $0.33 = $1.65) + one ByteDance 2K faithful finish
    # on the real 25s (5 x 5s) video (25 x $0.0144 = $0.36) = a real, predictable total ~$3.26.
    # Both numbers below carry real headroom above that AND above the plain "Pro" render variant's
    # worst case ($1.25 + 5 x $0.49 = $2.45 + $0.36 = $4.06), in case that's ever dialed back up.
    # Runway's own render-only cost (~$1.25/5 photos, possibly plus a Magnific pass) fits
    # comfortably too - but MiniMax-H3 at 2K (~$3.75/5 photos, no separate upscale step, and the
    # enhancement step is fal/Hailuo-specific so wouldn't apply) does NOT fit this cap; raise it if
    # switching studio_media_video_provider back to "minimax". (Recomputed 2026-09-05 for the
    # topaz->bytedance + photo-enhance switch - the previous 6.0/7.0 was sized for Topaz HQ.)
    "studio_media_video_reservation_estimate_usd": 4.5,
    "studio_media_video_max_cost_per_generation_usd": 5.5,
    # Raised 2026-09-04: these two were never rescaled when the per-video cost moved from
    # Runway (~$0.25-0.60/video) to the fal.ai pipeline above (~$3.17-3.35/video, ~10x higher) -
    # the old $40/day ceiling was still sized for the CHEAP era, so it now capped the whole
    # platform at ~11 videos/day combined and was tripping for real (non-abusive) usage within
    # a single day, surfacing as "This AI feature is at its usage limit... across all agents."
    # Real per-agent spend is still tightly capped elsewhere (agent_monthly_ai_budget_usd_by_tier,
    # studio_media_monthly_spend_cap_usd_by_tier) - this is only the platform-wide emergency
    # brake, so it can be generous without weakening per-agent protection.
    "studio_media_video_global_daily_cost_ceiling_usd": 150.0,
    "studio_media_video_global_monthly_cost_ceiling_usd": 1500.0,

    # ---- Per-agent monthly spend budget (across EVERY AI tool combined) ----
    # The sum of every AIUsageEvent.estimated_cost_usd for one owner since the 1st of the
    # month, across all event_types (chat + tts + realtime + cma_ai + studio_media_image +
    # studio_media_video + anything added later). The agent decides how to spend it. This is
    # NOT surfaced to the agent as a dollar figure - the frontend shows only a 0-100%
    # "Monthly AI Usage" meter derived from it (see GET /ai/usage/monthly and
    # ai_rate_limit_service.monthly_ai_budget_snapshot). Enforced (as a generic
    # "reached your AI usage limit for this month" 429) at the chat, CMA and Studio Media
    # reservation points. A tier absent from this dict is simply not gated by the total cap -
    # its own per-feature ceilings still apply. Populated for all five PlanTier values (2026-09,
    # folding CMA in here too, replacing its old standalone generation-count ceiling): NO_MLS/
    # PRO_NO_MLS mirror BASIC/PREMIUM's numbers as that track's equivalents (NO_MLS has no CMA or
    # Studio Media access today, so its $5 is inert - present purely for a consistent tier table).
    "agent_monthly_ai_budget_usd_by_tier": {"NO_MLS": 5.0, "PRO_NO_MLS": 10.0, "BASIC": 5.0, "PREMIUM": 10.0, "IDX": 20.0},
    # Sub-cap: of that monthly budget, the most one agent may spend on Studio AI Media
    # (studio_media_image + studio_media_video combined) - OFF by default (empty dict) as of
    # 2026-09-04 per explicit decision: Studio Media is governed by the same total monthly AI
    # budget as everything else (agent_monthly_ai_budget_usd_by_tier, 90% soft cap below), not a
    # separate slice of it. It was previously $10 (IDX) / $5 (PRO_NO_MLS) - real testing showed a
    # single Hailuo 2.3 Pro video (~$3.85-4) blew through the PRO_NO_MLS number in ~1 video and
    # the IDX one in ~2-3, well before the agent's actual overall budget was anywhere near used.
    # `enforce_studio_media_monthly_cap` is a no-op for any tier with no entry here - re-add a
    # tier's number to bring this sub-cap back if Studio Media spend ever needs its own ceiling
    # separate from the total again.
    "studio_media_monthly_spend_cap_usd_by_tier": {},
    # Soft cap: once this agent has used >= this percent of their TOTAL monthly AI budget
    # (every event_type combined), the expensive AI Studio tools - Media photo + video - stop,
    # while the cheaper tools (CMA, cheat sheets, chat, area narratives) keep working right up
    # to 100%. Protects the remaining ~10% for core day-to-day work instead of letting one
    # batch of videos consume the whole allowance. Set to 100 to disable the soft cap.
    "studio_soft_cap_percent": 90,

    # Per-call USD price for the non-LLM external APIs an agent action can trigger (Google
    # Places / Geocoding / Routes). When one of these fires because of an agent's action, the
    # cost is recorded as an AIUsageEvent (event_type "external_api") against that agent, so it
    # rolls into the same monthly % meter, $20 budget and 90% soft cap as the LLM tools. Calls
    # made by sync/cron jobs (no user) are NOT charged to anyone - they stay on the platform
    # emergency brake only. Figures are list price rounded up; tune against the Google invoice.
    "external_api_prices_usd": {
        "places_nearby": 0.032,
        "places_text": 0.032,
        "places_details": 0.017,
        "places_details_reviews": 0.025,  # Place Details with reviews = Enterprise + Atmosphere SKU
        "geocoding": 0.005,
        "routes_matrix_element": 0.005,
        "static_map": 0.002,
        "census": 0.0,
    },

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

        # The row holds deliberate overrides only. Never snapshot DEFAULT_CONFIG into it:
        # a saved snapshot outranks the code, so later default changes (e.g. the CMA caps)
        # would silently never reach an environment that had already run AI once.
        if setting and setting.value:
            merged = cls._merge(DEFAULT_CONFIG, setting.value)
        else:
            merged = dict(DEFAULT_CONFIG)

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
    def estimate_cost_usd(config: dict, model: str, input_tokens: int, output_tokens: int,
                          cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
        """Anthropic's real bill for one call. `input_tokens` from the API EXCLUDES prompt-cache
        tokens, so callers that use cache_control pass those separately: a 5-minute cache write is
        1.25x the input price, a cache read 0.1x (0.05x on Opus 5.5 / Sonnet 5.5 - see
        cache_read_multiplier in model_pricing)."""
        pricing = config.get("model_pricing", {}).get(model)
        if not pricing:
            return 0.0
        inp = pricing["input_per_mtok"]
        return (
            (input_tokens / 1_000_000) * inp
            + (cache_write_tokens / 1_000_000) * inp * 1.25
            + (cache_read_tokens / 1_000_000) * inp * pricing.get("cache_read_multiplier", 0.1)
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
