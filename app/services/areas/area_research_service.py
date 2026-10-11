"""AI-grounded community research - the curation/judgment layer described in the community-
enrichment feasibility audit (see the approved plan). This module turns "technically nearby" into
"actually relevant" using Claude's built-in web_search tool (confirmed present in the installed
SDK) rather than inventing facts from the model's own training data.

Architecture (v5, reworked twice after real backfill runs surfaced reliability problems):
  - v3 (one single Haiku request covering all 10 categories in one submit_research call, 10-search
    budget) measured at $0.25/community on Villanova with every category populated. Real backfill
    runs on Ardmore/Bala Cynwyd/Bryn Mawr showed it fails close to half the time: the model does
    the full search budget (real tokens, real searches) but then submits a near-empty structured
    result for most categories anyway, with zero validation drops - it's genuinely not using its
    own research when synthesizing the final answer. Not a token-truncation bug, not a validation
    bug - the working theory was that one big forced JSON tool call covering ten structurally
    different categories is enough synthesis load that the model sometimes takes the easy way out
    on most fields.
  - v4 split that into THREE grouped requests (schools_transport_medical: 4 categories; lifestyle:
    5 categories; market_context: 1 category). Real improvement (Bala Cynwyd 0-1/10 -> 6/10, Bryn
    Mawr -> 5/10) but not fully fixed - and the failure pattern was no longer random: on both test
    towns, the FIRST category declared in a group's schema came back populated and the LATER ones
    in that same group came back empty, in schema-declaration order, every time. That's a real,
    mechanistic clue - the model runs out of "effort" partway through a multi-field structured
    output, favoring whichever field it writes first - not sampling noise the group split alone
    would fix.
  - v5 shrunk every group to at most 2-3 categories, trading call count for reliability rather
    than the reverse (smaller groups also measured CHEAPER overall in the v4 test, since each
    call's injected search-result context shrinks with it, so this wasn't even a cost/reliability
    tradeoff):
      1. schools: public_schools, private_schools (highest-stakes group, gets its own call)
      2. transport_medical: transportation, medical
      3. grocery_parks: grocery, parks
      4. restaurants_shopping_downtown: restaurants, shopping, downtown_centers
      5. market_context: historical/broader market context alone
    Real improvement (Bala Cynwyd 6/10 -> 7/10, Bryn Mawr 5/10 -> 9/10 and ENRICHED), but "the
    schema technically allows an empty result" was still being treated as equivalent to "we
    confirmed there's genuinely nothing here," which isn't true - an empty category could mean
    the model searched and found nothing, or it could mean the same under-population failure mode
    at a smaller scale.
  - v6 (current) is v5's five-group primary pass, unchanged, plus two additions:
      1. `notes` is now a REQUIRED field on every place/market-context schema, and the model is
         explicitly told it must explain a real completed search when returning an empty result -
         see category_is_resolved, which only accepts an empty category as "resolved" if `notes`
         credibly explains why (public_schools is the one exception: a real community always has
         SOME assigned district, so empty is never accepted as resolved there).
      2. Any category that's still NOT resolved after the primary pass gets one focused, single-
         category completion-pass call (run_solo_category_research) - Haiku only, a small search
         budget, the model's full attention on just that one topic instead of splitting it with
         1-2 siblings. If public_schools specifically is still unresolved after its completion
         pass AND this platform's own real MLS evidence names a district the model still isn't
         finding (a genuine, checkable conflict - not just persistent emptiness), ONE narrowly-
         scoped Sonnet fallback call runs for that category alone - the only place Sonnet is used
         anywhere in this pipeline, and only ever for this one high-stakes category.
    A hard per-community cost budget (area_research_max_cost_per_community_usd, default $1.00)
    gates the completion pass and Sonnet fallback - once spending on a community hits that budget,
    remaining unresolved categories are left as-is and logged for review rather than pursued
    further, so this can never spiral on a stubborn town. Still no retry-on-empty within the
    primary pass or a group call, and still no blind whole-job retry - only ever a targeted,
    bounded top-up for what's still missing.
  - This platform's own existing evidence (old Places/NCES results, real MLS school_district_name
    values) is deliberately NOT handed to the model as trusted grounding to research FROM - stale
    or bad existing data would anchor the model toward repeating the same mistake. It's mentioned
    only as a SECONDARY cross-check, after independent web research, with an explicit "this may be
    wrong or stale" caveat - see _GROUP_GROUNDING_TEXT.

Nothing here is ever called from a public page request - only from area_enrichment_service's
background job, same isolation boundary as the rest of that module.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import MarketArea, User
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_orchestrator_service import _get_client
from app.services.ai.ai_rate_limit_service import monthly_cost_so_far, record_usage_event

logger = logging.getLogger(__name__)

AREA_RESEARCH_EVENT_TYPE = "area_research"
# Bumped whenever a prompt/schema/architecture change meaningfully changes what gets researched,
# so a future change (and the staleness check in area_enrichment_service.needs_research) can tell
# "researched under an older pipeline" apart from "confirmed current" instead of treating every
# non-null facts blob as equally trustworthy forever. v6 = v5's five-group primary pass plus a
# targeted per-category completion pass and a narrow public-schools-only Sonnet fallback (see
# module docstring) - bumped from v5 so every area researched before "empty" required a credible
# confirmed-none explanation is re-researched under the stricter, completion-pass-backed pipeline.
CURRENT_RESEARCH_VERSION = 6

# The 10 independently-validated categories, spread across the 3 grouped requests (see
# _GROUP_SPECS). Also the facts storage keys - the category name itself is always the key
# MarketArea.facts is written under, in every group.
RESEARCH_CATEGORIES = (
    "public_schools", "private_schools", "grocery", "parks", "restaurants", "shopping",
    "downtown_centers", "medical", "transportation", "market_context",
)

_RELATIONSHIP_VALUES = ("IN_AREA", "ADJACENT", "NEARBY")

_PLACE_ENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "category": {"type": "string", "description": "A short label for what this is, e.g. 'Full-service supermarket', 'Regional rail station', 'Community park'."},
        "relationship": {"type": "string", "enum": list(_RELATIONSHIP_VALUES), "description": "IN_AREA = physically inside the community. ADJACENT = in an immediately bordering town. NEARBY = a short drive away but genuinely relevant. Never claim IN_AREA for something outside the community's actual boundaries."},
        "city": {"type": "string"},
        "distance_miles": {"type": "number", "description": "Approximate driving distance in miles from the community's center."},
        "source_url": {"type": "string", "description": "A real URL you found this at. Required."},
        "source_type": {"type": "string", "description": "e.g. 'official_website', 'government', 'news', 'business_directory', 'transit_authority'."},
        "description": {"type": "string", "description": "One short factual sentence, only if useful."},
        "confidence": {"type": "number", "description": "0.0-1.0, your confidence this entry is accurate and current."},
    },
    "required": ["name", "relationship", "source_url", "confidence"],
}


def _place_tool_schema(extra_notes_hint: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "items": _PLACE_ENTITY_SCHEMA},
            "notes": {
                "type": "string",
                "description": (
                    f"{extra_notes_hint} REQUIRED if entities is empty: state plainly that you actually searched "
                    "for this category and found nothing relevant (say what you searched for), so an empty list "
                    "here always means a real confirmed absence, never an unexplained gap. Optional if entities "
                    "is non-empty."
                ),
            },
        },
        # notes is required (not just entities) so an empty result always comes with an explicit,
        # checkable statement that a real search concluded there's nothing - see
        # area_research_service.category_is_resolved, which treats an empty entities list with no
        # notes as unresolved (needs a focused completion-pass call) rather than a valid answer.
        "required": ["entities", "notes"],
    }


_SCHOOLS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "districts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "source_url": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "source_url", "confidence"],
            },
            "description": "Every public school district that serves any part of this community. List more than one if the community genuinely crosses district lines - never guess a single district for a mixed-district area.",
        },
        "crosses_district_boundary": {"type": "boolean"},
        "assignment_depends_on_exact_address": {"type": "boolean"},
        "boundary_caveat": {"type": "string", "description": "Plain-language explanation of any attendance-boundary complexity a home buyer should know - required if crosses_district_boundary or assignment_depends_on_exact_address is true."},
        "elementary_schools": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "district": {"type": "string"}, "source_url": {"type": "string"}, "confidence": {"type": "number"}},
            "required": ["name", "district", "source_url", "confidence"],
        }},
        "middle_schools": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "district": {"type": "string"}, "source_url": {"type": "string"}, "confidence": {"type": "number"}},
            "required": ["name", "district", "source_url", "confidence"],
        }},
        "high_schools": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "district": {"type": "string"}, "source_url": {"type": "string"}, "confidence": {"type": "number"}},
            "required": ["name", "district", "source_url", "confidence"],
        }},
    },
    "required": ["districts", "crosses_district_boundary", "assignment_depends_on_exact_address"],
}

_PRIVATE_SCHOOLS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "school_type": {"type": "string", "description": "e.g. 'Independent K-12', 'Catholic', 'Montessori', 'Special education'."},
                "grades_served": {"type": "string"},
                "relationship": {"type": "string", "enum": list(_RELATIONSHIP_VALUES)},
                "city": {"type": "string"},
                "distance_miles": {"type": "number"},
                "source_url": {"type": "string"},
                "description": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["name", "relationship", "source_url", "confidence"],
        }},
        "notes": {
            "type": "string",
            "description": (
                "REQUIRED if entities is empty: state plainly that you actually searched for private schools "
                "in or reasonably near this community and found nothing relevant, so an empty list here is a "
                "real confirmed absence rather than an unexplained gap."
            ),
        },
    },
    "required": ["entities", "notes"],
}

_MARKET_CONTEXT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "stats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "e.g. 'Median sale price, 5-year change'."},
                    "value": {"type": "string"},
                    "geography": {"type": "string", "description": "The EXACT geography this figure covers, e.g. 'Villanova, PA 19085' or 'Delaware County, PA' - never blended across geographies."},
                    "time_period": {"type": "string", "description": "The EXACT time period this figure covers, e.g. '2020-2025' or 'as of Q2 2026'."},
                    "source_url": {"type": "string"},
                    "source_type": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["label", "value", "geography", "time_period", "source_url", "confidence"],
            },
        },
        "notes": {
            "type": "string",
            "description": (
                "REQUIRED if stats is empty: state plainly that you actually searched for historical/broader "
                "market data for this specific community and found nothing reliable enough to report (say what "
                "you searched for), so an empty list always means a real confirmed absence. Optional if stats is "
                "non-empty."
            ),
        },
    },
    "required": ["stats", "notes"],
}


async def _cost_so_far(db: AsyncSession, triggering_user_id: str) -> float:
    return await monthly_cost_so_far(db, triggering_user_id, event_type=AREA_RESEARCH_EVENT_TYPE)


def _estimate_cost(config: dict, model: str, input_tokens: int, output_tokens: int, web_search_requests: int) -> float:
    token_cost = ai_config_service.estimate_cost_usd(config, model, input_tokens, output_tokens)
    # Anthropic bills web_search per search in addition to token cost - this rate should be
    # confirmed against Anthropic's current published pricing before relying on it for a hard
    # budget; kept as its own config value (not hardcoded here) so it can be corrected without a
    # code change. See the enrichment feasibility audit's cost section for this same caveat.
    per_search = config.get("area_research_web_search_cost_usd", 0.01)
    return token_cost + (web_search_requests * per_search)


# ---------------------------------------------------------------------------
# Grouped research (v4, the only path - see module docstring). Three smaller requests per
# community instead of one 10-category mega-request, each with its own narrow schema/prompt/
# search budget. Reuses every per-category tool schema above unmodified, just nested under
# smaller top-level objects, so validate_category_result stays the single source of truth for
# each category's shape regardless of which group researched it.
# ---------------------------------------------------------------------------

_GROUP_GROUNDING_TEXT = (
    "GROUNDING: research the live web independently FIRST and treat current, authoritative sources you find "
    "yourself as primary. You may also be given this platform's own EXISTING evidence for this community (past "
    "Google Places results, an older school-boundary lookup, MLS-derived school district names) - treat that "
    "ONLY as a secondary cross-check AFTER your own research, never as the starting point. That existing data may "
    "be wrong, incomplete, or stale; do not let it anchor or override what your own current web research finds. "
    "Where they disagree, trust your fresh research and note the discrepancy rather than defaulting to the old data.\n\n"
)


def _group_system_prompt(intro: str, category_rules: str, max_searches: int) -> str:
    return (
        f"{intro}\n\n"
        f"SEARCH BUDGET: you have at most {max_searches} web searches for this request - not a shared budget with "
        "any other request, and there is no follow-up pass after this one, so use enough of them to actually cover "
        "every category below with real, current, sourced information.\n\n"
        f"{_GROUP_GROUNDING_TEXT}"
        f"RULES PER CATEGORY:\n{category_rules}\n\n"
        "Every entity/stat you report needs a real source_url you actually found it at - never invent one. "
        "IMPORTANT: if your searches turned up real, usable information for a category, that information MUST "
        "appear in your submission - do not search, find real results, and then submit an empty list for that "
        "category anyway. An empty list should only ever mean you genuinely found nothing worth including after "
        "searching, never a shortcut. Call submit_research exactly once with your complete findings."
    )


_GROUP_SCHOOLS_SCHEMA = {
    "type": "object",
    "properties": {"public_schools": _SCHOOLS_TOOL_SCHEMA, "private_schools": _PRIVATE_SCHOOLS_TOOL_SCHEMA},
    "required": ["public_schools"],
    # private_schools is deliberately not required - a real "none nearby" answer is valid.
}

_GROUP_TRANSPORT_MEDICAL_SCHEMA = {
    "type": "object",
    "properties": {
        "transportation": _place_tool_schema("Any general note about this area's transportation access/car-dependency."),
        "medical": _place_tool_schema("Any general note about medical access for this area."),
    },
    "required": ["transportation", "medical"],
}

_GROUP_GROCERY_PARKS_SCHEMA = {
    "type": "object",
    "properties": {
        "grocery": _place_tool_schema("Any general note, e.g. if this community has very limited grocery access."),
        "parks": _place_tool_schema("Any general note about the area's parks/recreation character."),
    },
    "required": ["grocery", "parks"],
}

_GROUP_RESTAURANTS_SHOPPING_DOWNTOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "restaurants": _place_tool_schema("Any general note about the area's dining character."),
        "shopping": _place_tool_schema("Any general note about the area's shopping character."),
        "downtown_centers": _place_tool_schema("Explicitly state if this community has no traditional downtown of its own."),
    },
    "required": ["restaurants", "shopping", "downtown_centers"],
}

_GROUP_MARKET_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {"market_context": _MARKET_CONTEXT_TOOL_SCHEMA},
    "required": ["market_context"],
}


@dataclass
class _GroupSpec:
    group_key: str
    categories: tuple[str, ...]
    system_prompt: str
    tool_schema: dict
    max_searches: int


# One rule bullet per category - the single source of truth for both the grouped primary-pass
# prompts below (joined per group) and the solo completion-pass prompts (used alone, see
# _solo_system_prompt), so the two paths can never drift out of sync with each other.
_CATEGORY_RULE_TEXT: dict[str, str] = {
    "public_schools": (
        "- Public schools: never assume the closest district serves the community - a community can genuinely "
        "span more than one school district (common for informal/unincorporated places). List every district "
        "that serves any part of it. If assignment depends on the exact address, say so and explain in "
        "boundary_caveat."
    ),
    "private_schools": (
        "- Private schools: genuinely useful nearby options, not just the closest. Classify IN_AREA/ADJACENT/"
        "NEARBY honestly - never imply something is physically in the community when it isn't."
    ),
    "transportation": (
        "- Transportation: real NAMED infrastructure (the actual station/route name) - never a bare nearby "
        "town name standing in for a transit option."
    ),
    "medical": (
        "- Medical: hospitals, major health-system facilities, urgent/emergency care only - never individual "
        "doctors, dentists, or chiropractors. category must be exactly 'HOSPITAL', 'URGENT_CARE', or "
        "'OTHER_MAJOR_MEDICAL'."
    ),
    "grocery": (
        "- Grocery: full-service grocery/supermarkets residents would actually use for their shopping - not "
        "convenience stores, pharmacies, or gas stations."
    ),
    "parks": (
        "- Parks & recreation: real destination parks/trails residents actually use, not every tiny playground."
    ),
    "restaurants": (
        "- Restaurants: a varied, reputation-based selection, not just the six closest. A small community may "
        "have none of its own - that's fine, include genuinely useful nearby options (ADJACENT/NEARBY) rather "
        "than returning an empty list; only leave it empty if you truly find nothing worth recommending "
        "anywhere nearby."
    ),
    "shopping": (
        "- Shopping: real shopping districts/centers/boutique clusters, kept separate from restaurants."
    ),
    "downtown_centers": (
        "- Downtown/town centers: what a resident would actually consider their town center, not a mechanical "
        "'downtown' keyword match. If the community has no traditional downtown of its own, name the real "
        "nearby center(s) residents actually use instead (as an ADJACENT/NEARBY entity) - don't just leave this "
        "empty. Deduplicate aggressively."
    ),
    "market_context": (
        "- Market context: every stat must cite a real source_url you found it at, and must specify the EXACT "
        "geography (e.g. 'Villanova, PA 19085' vs. 'Delaware County, PA' are NOT the same geography) and EXACT "
        "time period it covers. NEVER invent or estimate a number that isn't stated in a source you found - "
        "omit it entirely rather than guess. NEVER combine numbers from two different geographies or time "
        "periods into one implied figure; report disagreeing sources as separate entries instead. Prefer "
        "recognized real-estate market-report publishers, county/regional data, and government housing data "
        "over informal sources."
    ),
}


def _rules_for(*categories: str) -> str:
    return "\n".join(_CATEGORY_RULE_TEXT[c] for c in categories)


# Five smaller, narrower groups (reworked from v4's three - see module docstring: even a
# 4-5-category group showed a consistent pattern of the LAST-declared field(s) in a group coming
# back empty while earlier ones populated correctly, both on Bala Cynwyd and Bryn Mawr, so the fix
# is smaller groups, not just fewer of them). Deliberately optimizing for reliable completeness at
# a similar total cost, not for the fewest possible model calls - smaller groups measured cheaper
# overall anyway, since each call's input context (injected search results) shrinks with it.
_GROUP_SPECS: list[_GroupSpec] = [
    _GroupSpec(
        group_key="schools",
        categories=("public_schools", "private_schools"),
        system_prompt=_group_system_prompt(
            "You are researching public schools and private schools for a real estate community page, covering "
            "TWO categories in this ONE request. This is the highest-stakes category on the whole page - a wrong "
            "answer misleads someone about where their kids would go to school.",
            _rules_for("public_schools", "private_schools"),
            max_searches=4,
        ),
        tool_schema=_GROUP_SCHOOLS_SCHEMA,
        max_searches=4,
    ),
    _GroupSpec(
        group_key="transport_medical",
        categories=("transportation", "medical"),
        system_prompt=_group_system_prompt(
            "You are researching transportation and medical/hospital facilities for a real estate community page, "
            "covering TWO categories in this ONE request.",
            _rules_for("transportation", "medical"),
            max_searches=3,
        ),
        tool_schema=_GROUP_TRANSPORT_MEDICAL_SCHEMA,
        max_searches=3,
    ),
    _GroupSpec(
        group_key="grocery_parks",
        categories=("grocery", "parks"),
        system_prompt=_group_system_prompt(
            "You are researching grocery and parks & recreation for a real estate community page, covering TWO "
            "categories in this ONE request.",
            _rules_for("grocery", "parks"),
            max_searches=3,
        ),
        tool_schema=_GROUP_GROCERY_PARKS_SCHEMA,
        max_searches=3,
    ),
    _GroupSpec(
        group_key="restaurants_shopping_downtown",
        categories=("restaurants", "shopping", "downtown_centers"),
        system_prompt=_group_system_prompt(
            "You are researching restaurants, shopping, and downtown/town centers for a real estate community "
            "page, covering THREE categories in this ONE request.",
            _rules_for("restaurants", "shopping", "downtown_centers"),
            max_searches=4,
        ),
        tool_schema=_GROUP_RESTAURANTS_SHOPPING_DOWNTOWN_SCHEMA,
        max_searches=4,
    ),
    _GroupSpec(
        group_key="market_context",
        categories=("market_context",),
        system_prompt=_group_system_prompt(
            "You are researching HISTORICAL and broader real estate market context for a real estate community "
            "page - price trends over time, year-over-year change, historical inventory/days-on-market trends, "
            "and longer-run sales activity. This platform's own database already provides CURRENT/live statistics "
            "(today's median list price, active inventory, days on market) from real MLS data - do not attempt to "
            "reproduce those; you are filling a genuine gap, since the platform's own listing history doesn't go "
            "back far enough for trend data.",
            _rules_for("market_context"),
            max_searches=3,
        ),
        tool_schema=_GROUP_MARKET_CONTEXT_SCHEMA,
        max_searches=3,
    ),
]


async def _run_group_call(client, model: str, group: _GroupSpec, user_text: str) -> tuple[Optional[dict], dict]:
    """One group's two-step call shape (search with tool_choice="auto", then a bounded forced
    submit_research follow-up reusing the same conversation if the model didn't submit on its
    own) - same shape v3's single mega-call used, just scoped to one group's schema/prompt."""
    submit_tool = {
        "name": "submit_research",
        "description": f"Submit your research findings for: {', '.join(group.categories)}.",
        "input_schema": group.tool_schema,
    }
    web_search_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": group.max_searches}
    messages: list[dict] = [{"role": "user", "content": user_text}]
    usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}

    response = await client.messages.create(
        model=model, max_tokens=3000, system=group.system_prompt,
        tools=[web_search_tool, submit_tool], tool_choice={"type": "auto"}, messages=messages,
    )
    usage["input_tokens"] += response.usage.input_tokens
    usage["output_tokens"] += response.usage.output_tokens
    if response.usage.server_tool_use:
        usage["web_search_requests"] += response.usage.server_tool_use.web_search_requests

    submit_block = next((b for b in response.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_research"), None)
    if not submit_block:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Call submit_research now with your complete findings for this request based on the research above."})
        response2 = await client.messages.create(
            model=model, max_tokens=3000, system=group.system_prompt,
            tools=[submit_tool], tool_choice={"type": "tool", "name": "submit_research"}, messages=messages,
        )
        usage["input_tokens"] += response2.usage.input_tokens
        usage["output_tokens"] += response2.usage.output_tokens
        if response2.usage.server_tool_use:
            usage["web_search_requests"] += response2.usage.server_tool_use.web_search_requests
        submit_block = next((b for b in response2.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_research"), None)

    result = submit_block.input if submit_block and isinstance(submit_block.input, dict) else None
    return result, usage


async def research_community_grouped(
    db: AsyncSession, area: MarketArea, triggering_user: User, secondary_evidence_text: str = "",
) -> tuple[Optional[dict], dict]:
    """The primary research pass (v5 - see module docstring): five independent, narrowly-scoped
    requests per community (see _GROUP_SPECS) instead of one 10-category mega-request. Returns a
    merged dict shaped exactly like a single-request result (top-level keys = category names) so
    every downstream caller (_run_ai_research's validation loop) is unchanged; unresolved
    categories (see category_is_resolved) are picked up afterward by a separate, targeted
    completion pass (run_solo_category_research), not handled here. Runs the group calls
    sequentially, not concurrently - keeps behavior simple/predictable and this already only takes
    a couple minutes total, not worth the added complexity of fan-out for a background job with no
    user waiting on it live.

    total_usage["category_usage"] maps each category to the usage of the group call that produced
    it - an approximation (a group's 2-3 categories share one call's token/search totals, since
    Claude doesn't report usage split by output field) but a real one, unlike just reusing the
    whole job's grand total for every category's metadata."""
    config = await ai_config_service.get_config(db)
    model = config.get("area_research_model", "claude-haiku-4-5")
    client = _get_client()
    user_text = f"Community: {area.name}, {area.state}\n\n{secondary_evidence_text}"

    merged: dict = {}
    total_usage = {"model": model, "input_tokens": 0, "output_tokens": 0, "web_search_requests": 0, "calls": [], "category_usage": {}}

    for group in _GROUP_SPECS:
        try:
            group_result, call_usage = await _run_group_call(client, model, group, user_text)
        except Exception as exc:
            logger.warning("Grouped AI area research call failed", extra={"area_id": area.id, "group": group.group_key, "error": str(exc)})
            group_result, call_usage = None, {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}

        total_usage["input_tokens"] += call_usage["input_tokens"]
        total_usage["output_tokens"] += call_usage["output_tokens"]
        total_usage["web_search_requests"] += call_usage["web_search_requests"]
        total_usage["calls"].append({"group": group.group_key, "result": "ok" if group_result is not None else "no_submission", **call_usage})
        for category in group.categories:
            total_usage["category_usage"][category] = {"model": model, "web_search_requests": call_usage["web_search_requests"], "escalated": False}

        if group_result is None:
            continue
        for category in group.categories:
            # Forced tool-choice guarantees valid JSON and the required top-level keys, but not
            # that every nested value actually matches its schema's declared type - real testing
            # surfaced a case where the model wrote a plain string for a category instead of the
            # expected object, which crashed validate_category_result downstream. Treat anything
            # that isn't a dict as "nothing usable" for that category rather than propagating a
            # malformed value into validation.
            value = group_result.get(category)
            merged[category] = value if isinstance(value, dict) else {}

    total_usage["estimated_cost_usd"] = _estimate_cost(config, model, total_usage["input_tokens"], total_usage["output_tokens"], total_usage["web_search_requests"])
    if not merged:
        return None, total_usage
    return merged, total_usage


_SOLO_SCHEMA_BY_CATEGORY = {
    "public_schools": _SCHOOLS_TOOL_SCHEMA,
    "private_schools": _PRIVATE_SCHOOLS_TOOL_SCHEMA,
    "grocery": _place_tool_schema("Any general note, e.g. if this community has very limited grocery access."),
    "parks": _place_tool_schema("Any general note about the area's parks/recreation character."),
    "restaurants": _place_tool_schema("Any general note about the area's dining character."),
    "shopping": _place_tool_schema("Any general note about the area's shopping character."),
    "downtown_centers": _place_tool_schema("If this community has no traditional downtown of its own, name the real nearby commercial/town center residents actually use instead as an ADJACENT or NEARBY entity - don't just leave it empty."),
    "medical": _place_tool_schema("Any general note about medical access for this area."),
    "transportation": _place_tool_schema("Any general note about this area's transportation access/car-dependency."),
    "market_context": _MARKET_CONTEXT_TOOL_SCHEMA,
}

_SOLO_TOPIC_BY_CATEGORY = {
    "public_schools": "which public school district(s) and specific elementary/middle/high schools serve this community",
    "private_schools": "notable private schools in or reasonably near this community",
    "grocery": "grocery stores residents of this community would realistically use",
    "parks": "parks, trails, natural areas, and recreation facilities relevant to this community",
    "restaurants": "a useful, varied selection of restaurants relevant to this community",
    "shopping": "meaningful shopping destinations for this community",
    "downtown_centers": "the actual nearby commercial/town center(s) someone living in this community would consider 'going into town'",
    "medical": "useful nearby medical facilities - hospitals, major health-system facilities, and urgent/emergency care",
    "transportation": "actual transportation infrastructure serving this community",
    "market_context": "historical and broader real estate market context for this community",
}


def _solo_tool_schema(category: str) -> dict:
    return {"type": "object", "properties": {category: _SOLO_SCHEMA_BY_CATEGORY[category]}, "required": [category]}


def _solo_system_prompt(category: str, rule_text: str, max_searches: int) -> str:
    topic = _SOLO_TOPIC_BY_CATEGORY[category]
    intro = (
        f"A broader research pass for this community already ran and came back without a confirmed answer for "
        f"{topic}. This is a focused, deeper follow-up covering ONLY this one category - give it your full "
        f"attention rather than splitting effort across other topics."
    )
    return _group_system_prompt(intro, rule_text, max_searches)


async def run_solo_category_research(
    db: AsyncSession, area: MarketArea, triggering_user: User, category: str,
    secondary_evidence_text: str = "", model_override: Optional[str] = None, max_searches: int = 3,
) -> tuple[Optional[dict], dict]:
    """Focused single-category completion-pass call, used when the primary grouped pass left a
    category unresolved (see category_is_resolved) - a smaller, more targeted budget than any
    group call gets, and the model's full attention on just one topic rather than splitting it
    across 2-3. Defaults to Haiku; `model_override` is how the narrow public-schools-only Sonnet
    fallback (see module docstring) asks for a stronger model on that one call."""
    config = await ai_config_service.get_config(db)
    model = model_override or config.get("area_research_model", "claude-haiku-4-5")
    client = _get_client()

    rule_text = _CATEGORY_RULE_TEXT[category]
    system_prompt = _solo_system_prompt(category, rule_text, max_searches)
    group = _GroupSpec(group_key=f"solo_{category}", categories=(category,), system_prompt=system_prompt, tool_schema=_solo_tool_schema(category), max_searches=max_searches)
    user_text = f"Community: {area.name}, {area.state}\n\n{secondary_evidence_text}"

    result, usage = await _run_group_call(client, model, group, user_text)
    usage["model"] = model
    usage["estimated_cost_usd"] = _estimate_cost(config, model, usage["input_tokens"], usage["output_tokens"], usage["web_search_requests"])
    return result, usage


# ---------------------------------------------------------------------------
# Validation - never trust the model's own output as final. Reliability controls from the
# approved plan (§G): mandatory source_url, server-side relationship-vs-distance consistency
# (overrides the model's own IN_AREA/ADJACENT/NEARBY label if implausible), aggressive dedup.
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_MAX_MILES_FOR = {"IN_AREA": 3.0, "ADJACENT": 10.0}  # NEARBY has no upper bound enforced here


def _normalize_entity_name(name: str) -> str:
    return _WS_RE.sub(" ", name.strip().lower())


def _has_real_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def validate_place_entities(entities: list[dict]) -> tuple[list[dict], dict]:
    """For every place-shaped category (private_schools, grocery, parks, restaurants, shopping,
    downtown_centers, medical, transportation). Returns (cleaned_entities, drop_counts) - the
    counts are logged, not silently discarded, so a category that's dropping most of its own
    results is visible rather than just quietly thin."""
    dropped = {"no_source_url": 0, "no_name": 0, "duplicate": 0}
    seen: set[str] = set()
    cleaned: list[dict] = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if not name or not str(name).strip():
            dropped["no_name"] += 1
            continue
        if not _has_real_url(e.get("source_url")):
            dropped["no_source_url"] += 1
            continue
        key = _normalize_entity_name(str(name))
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)

        # Server-side override, not just trusting the model's own label - if it claims IN_AREA/
        # ADJACENT but also reports a distance inconsistent with that, downgrade the label rather
        # than publish an implausible "in the community" claim for something miles away.
        rel = e.get("relationship")
        dist = e.get("distance_miles")
        if rel in _MAX_MILES_FOR and isinstance(dist, (int, float)) and dist > _MAX_MILES_FOR[rel]:
            e = {**e, "relationship": "NEARBY", "relationship_overridden": True}
        cleaned.append(e)
    return cleaned, dropped


def validate_school_list(entries: list[dict]) -> tuple[list[dict], dict]:
    """Schools/districts don't carry relationship/distance the way place entities do - just
    source_url + name-dedup enforcement."""
    dropped = {"no_source_url": 0, "no_name": 0, "duplicate": 0}
    seen: set[str] = set()
    cleaned: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if not name or not str(name).strip():
            dropped["no_name"] += 1
            continue
        if not _has_real_url(e.get("source_url")):
            dropped["no_source_url"] += 1
            continue
        key = _normalize_entity_name(str(name))
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        cleaned.append(e)
    return cleaned, dropped


def validate_market_context_stats(stats: list[dict]) -> tuple[list[dict], dict]:
    """Structurally requires geography + time_period + source_url per entry - not optional
    fields - precisely so the model can't quietly merge incompatible sources into one implied
    figure (see the approved plan's market-data-scope correction). Anything missing any of the
    three is dropped rather than published half-attributed."""
    dropped = {"missing_required_field": 0, "duplicate": 0}
    seen: set[str] = set()
    cleaned: list[dict] = []
    for s in stats:
        if not isinstance(s, dict):
            continue
        if not all(s.get(f) and str(s[f]).strip() for f in ("label", "value", "geography", "time_period")) or not _has_real_url(s.get("source_url")):
            dropped["missing_required_field"] += 1
            continue
        key = _normalize_entity_name(f"{s['label']}|{s['geography']}|{s['time_period']}")
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        cleaned.append(s)
    return cleaned, dropped


def validate_category_result(category: str, result: dict) -> tuple[dict, dict]:
    """Dispatches to the right validator and rebuilds the category's stored shape from the
    cleaned entities - the shape written to MarketArea.facts, not the raw model output."""
    if category == "public_schools":
        districts, d_dropped = validate_school_list(result.get("districts") or [])
        elem, e_dropped = validate_school_list(result.get("elementary_schools") or [])
        mid, m_dropped = validate_school_list(result.get("middle_schools") or [])
        high, h_dropped = validate_school_list(result.get("high_schools") or [])
        dropped = {k: d_dropped.get(k, 0) + e_dropped.get(k, 0) + m_dropped.get(k, 0) + h_dropped.get(k, 0) for k in d_dropped}
        cleaned = {
            "districts": districts,
            "crosses_district_boundary": bool(result.get("crosses_district_boundary")),
            "assignment_depends_on_exact_address": bool(result.get("assignment_depends_on_exact_address")),
            "boundary_caveat": result.get("boundary_caveat") or None,
            "elementary_schools": elem,
            "middle_schools": mid,
            "high_schools": high,
        }
        return cleaned, dropped
    if category == "market_context":
        stats, dropped = validate_market_context_stats(result.get("stats") or [])
        cleaned = {"stats": stats}
        if result.get("notes"):
            cleaned["notes"] = result["notes"]
        return cleaned, dropped
    entities, dropped = validate_place_entities(result.get("entities") or [])
    cleaned = {"entities": entities}
    if result.get("notes"):
        cleaned["notes"] = result["notes"]
    return cleaned, dropped


def result_has_content(category: str, cleaned: dict) -> bool:
    """Whether a validated category result actually has real entities/stats in it - true
    "populated," not "resolved" (see category_is_resolved for the latter, which also accepts a
    credibly-confirmed-empty result as done)."""
    if category == "public_schools":
        return bool(cleaned.get("districts") or cleaned.get("elementary_schools") or cleaned.get("middle_schools") or cleaned.get("high_schools"))
    if category == "market_context":
        return bool(cleaned.get("stats"))
    return bool(cleaned.get("entities"))


_MIN_CREDIBLE_NOTE_LENGTH = 15


def category_is_resolved(category: str, cleaned: dict) -> bool:
    """True if this category is genuinely DONE - either populated with real content, or credibly
    confirmed empty (the model explicitly explained, via `notes`, that it actually searched for
    this category and found nothing relevant - not just an empty array the schema happened to
    allow). An unresolved category is what triggers a focused completion-pass call (see
    area_enrichment_service._run_ai_research) - "the schema technically allowed an empty result"
    is not the same as "we established the emptiness is correct," which is exactly the gap this
    closes.

    public_schools is a deliberate exception: a real residential community essentially always has
    SOME assigned public school district, so an empty `districts` list is treated as unresolved
    unconditionally, never "confirmed none" - there's no legitimate version of that emptiness for
    this category the way there is for, say, private schools or a formal downtown."""
    if result_has_content(category, cleaned):
        return True
    if category == "public_schools":
        return False
    notes = cleaned.get("notes")
    return bool(notes and len(str(notes).strip()) >= _MIN_CREDIBLE_NOTE_LENGTH)
