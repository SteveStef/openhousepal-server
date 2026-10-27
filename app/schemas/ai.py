from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class AIConversationListItem(BaseModel):
    id: str
    title: Optional[str] = None
    created_at: datetime
    last_message_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AIMessageOut(BaseModel):
    id: str
    role: str
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AIConversationOut(BaseModel):
    id: str
    title: Optional[str] = None
    created_at: datetime
    last_message_at: Optional[datetime] = None
    messages: List[AIMessageOut] = []

    class Config:
        from_attributes = True


class AISendMessageRequest(BaseModel):
    message: str
    # True when this message was spoken (mic dictation) and/or the reply will be read aloud -
    # tells the model to answer in one short, plain-spoken sentence instead of its normal
    # bullet-list style, since a long reply is read out loud rather than scanned. See
    # ai_orchestrator_service.run_turn's voice_mode handling.
    voice_mode: bool = False


class AISendMessageResponse(BaseModel):
    conversation_id: str
    user_message: AIMessageOut
    assistant_message: AIMessageOut


class AIInsightItem(BaseModel):
    id: str
    title: str
    detail: str
    severity: str  # 'info' | 'attention' | 'urgent'
    link: Optional[str] = None


class AIInsightsResponse(BaseModel):
    insights: List[AIInsightItem]
    generated_at: datetime


class DigestItem(BaseModel):
    id: str
    kind: str  # 'tour_request' | 'second_visit' | 'disclosure' | 'new_match' | 'liked' | 'disliked' |
               # 'comment' | 'new_lead' | 'form_submission' | 'open_house_signin' | 'upcoming_open_house'
    title: str
    detail: str
    severity: str  # 'urgent' | 'attention' | 'info'
    link: Optional[str] = None
    lead_name: Optional[str] = None
    occurred_at: Optional[datetime] = None


class DailyDigestResponse(BaseModel):
    needs_attention: List[DigestItem]
    today: List[DigestItem]
    generated_at: datetime


class DigestDismissRequest(BaseModel):
    item_id: str


class AISpeechRequest(BaseModel):
    """Read Aloud - text is exactly what Claude already generated (see markdown.tsx's
    stripMarkdown on the frontend); this endpoint only synthesizes it, never re-answers."""
    text: str
    conversation_id: Optional[str] = None


# --- Talk to OHP (OpenAI Realtime). The browser connects directly to OpenAI over WebRTC -
# these are the few requests it still makes back to this backend during a session. See
# ai_realtime_service.py. ---

class AIRealtimeSessionOut(BaseModel):
    session_id: str
    client_secret: Optional[str] = None
    expires_at: Optional[Any] = None
    model: str
    voice: str
    max_session_seconds: int
    max_idle_seconds: int


class AIRealtimeToolCallRequest(BaseModel):
    session_id: str
    tool_name: str
    args: Dict[str, Any] = {}


class AIRealtimeToolCallResponse(BaseModel):
    result: Any
    refs: List[Dict[str, Any]] = []


class AIRealtimeUsageRequest(BaseModel):
    session_id: str
    usage: Dict[str, Any] = {}


class AIRealtimeUsageResponse(BaseModel):
    should_end: bool = False


class AIRealtimeHeartbeatRequest(BaseModel):
    session_id: str


class AIRealtimeEndRequest(BaseModel):
    session_id: str
    reason: str = "client_ended"


# --- Admin AI-usage analytics. Metadata only (cost, token counts, tool names, timing) -
# these schemas never carry AIMessage content. See admin_ai_routes.py. ---

class AIAdminDailyPoint(BaseModel):
    date: str  # YYYY-MM-DD
    cost_usd: float
    messages: int


class AIAdminBudgetStatus(BaseModel):
    """One usage_budget_counters bucket's current standing against its configured ceiling -
    see usage_budget_service.py. Layer 3 (platform-wide) and Places status, all in one shape
    so the admin UI can render every safety-net ceiling the same way."""
    bucket: str
    label: str
    day_spent_usd: float
    day_limit_usd: Optional[float] = None
    month_spent_usd: float
    month_limit_usd: Optional[float] = None
    day_count: Optional[int] = None
    day_count_limit: Optional[int] = None
    month_count: Optional[int] = None
    month_count_limit: Optional[int] = None
    # e.g. "Google's free tier: 5,000 calls/mo - this cap sits 1 call under it, so real Google
    # billing stays $0 as long as nothing else on this Google Cloud project shares the quota."
    free_tier_note: Optional[str] = None
    # "At this month's actual average cost per call, roughly this many more calls fit under the
    # remaining budget before the ceiling is hit" - None when there's no dollar ceiling to divide
    # into (a count-only bucket already shows its own remaining count directly) or no usage yet
    # this month to compute a real average from.
    est_remaining_calls_month: Optional[int] = None


class AIAdminTopConsumer(BaseModel):
    """One ip_daily/device_daily budget bucket's standing today - the 'who's about to hit
    their $3 cap right now' abuse-watch list. No user identity to join against (a visitor
    IP/device isn't a User row), so this is intentionally anonymous - just the raw
    identifier, useful for spotting a single bad actor mid-spree."""
    kind: str  # "ip" | "device"
    identifier: str
    spent_today_usd: float
    limit_usd: float


class AIAdminOverview(BaseModel):
    total_cost_month_usd: float
    total_cost_today_usd: float = 0.0
    total_cost_all_time_usd: float
    # CMA (comp analysis + final valuation + recompute) - the real cost rows, Eastern day/month.
    cma_cost_today_usd: float = 0.0
    cma_cost_month_usd: float = 0.0
    messages_month: int
    active_agents_30d: int
    blocked_count_month: int
    top_block_reasons: List[Dict[str, Any]]  # [{"reason": "daily_limit", "count": 3}, ...]
    daily_trend_30d: List[AIAdminDailyPoint]
    cost_by_type_month: Dict[str, float] = {}  # {"chat": 12.3, "tts": 1.1, "realtime": 2.4}
    cost_by_provider_month: Dict[str, float] = {}  # {"anthropic": 12.3, "openai": 1.1, "google_places": 4.5}
    global_budget_status: List[AIAdminBudgetStatus] = []  # Layer-3 + Places ceilings, current standing
    top_google_places_consumers: List[AIAdminTopConsumer] = []  # today's closest-to-capped IPs/devices
    # Provider-side failures (not our own rate limits) in the last 24h - an exhausted Anthropic/
    # OpenAI credit balance, a provider outage, auth failure, etc. See
    # ai_rate_limit_service.classify_provider_error. [{"event_type": "cma_ai",
    # "reason": "insufficient_credit", "count": 2, "last_seen": "2026-09-14T21:01:31Z"}, ...]
    recent_provider_failures: List[Dict[str, Any]] = []


class AIAdminAgentSummary(BaseModel):
    owner_id: str
    name: str
    email: str
    cost_usd: float
    messages: int
    tool_calls: int
    blocked_count: int
    last_active: Optional[datetime] = None
    # This agent's own IDX/public-site spend (Explore Nearby, attributed via agent_site_daily/
    # agent_total_daily buckets - see nearby_amenities_service.py) - separate from cost_usd
    # above, which is their own AI feature usage (chat/tts/cma/etc via AIUsageEvent).
    site_spend_today_usd: float = 0.0
    site_spend_month_usd: float = 0.0
    site_daily_limit_usd: Optional[float] = None
    total_spend_today_usd: float = 0.0
    total_spend_month_usd: float = 0.0
    total_daily_limit_usd: Optional[float] = None


class AIAdminMonthlyPoint(BaseModel):
    month: str  # YYYY-MM
    cost_usd: float
    messages: int


class AIAdminAgentDetail(AIAdminAgentSummary):
    daily_trend: List[AIAdminDailyPoint]
    monthly_trend: List[AIAdminMonthlyPoint] = []  # last 12 calendar months - "what have they been doing" history
    tool_usage: Dict[str, int]  # {"search_leads": 42, "get_cma_summary": 11, ...}
    cost_by_type: Dict[str, float] = {}  # {"chat": 4.2, "tts": 0.3, "realtime": 1.1}


class MonthlyAIUsageResponse(BaseModel):
    """GET /ai/usage/monthly - powers the "Monthly AI Usage" meter. `percent` is null for a
    tier with no configured monthly budget (frontend renders no meter). `used_usd` is for the
    temporary exact-dollar readout during testing; the permanent UI is the percentage only."""
    tier: str
    used_usd: float
    limit_usd: Optional[float] = None
    percent: Optional[int] = None
    recent_used_usd: float = 0.0  # spent in the last hour (part of used_usd)
    recent_percent: Optional[int] = None  # that hour's share of the budget (part of percent)
    near_limit: bool = False
    exhausted: bool = False
    studio_media_used_usd: float = 0.0
    studio_media_cap_usd: Optional[float] = None
    # This month's spend by feature: [{label, usd, percent}] - the meter's "What used it".
    breakdown: list = []


class CopywriteRequest(BaseModel):
    purpose: str  # listing_description | social_caption | email | sms | blog | bio_polish | design_copy | cheat_sheet_highlights | property_site_highlights | property_site_section | property_site_story_short | property_site_town | property_site_town_facts | generic
    context: str = ""            # listing facts, current draft, key points, etc.
    tone: Optional[str] = None   # professional | warm | energetic | luxury | concise
    instruction: Optional[str] = None  # free-text extra ask / prompt


class CopywriteResponse(BaseModel):
    text: str
    # What this one call cost, so a page can show "used X% of your monthly AI" afterwards (10/24).
    cost_usd: Optional[float] = None


class ComplianceCheckRequest(BaseModel):
    text: str


class ComplianceFlag(BaseModel):
    phrase: str
    issue: str
    suggestion: str


class ComplianceCheckResponse(BaseModel):
    flags: List[ComplianceFlag] = []
