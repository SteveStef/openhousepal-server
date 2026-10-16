"""Blog AI pipeline - Research -> Write -> Fact-check, with a "Verified" report.

The single-call writer (ai_blog_writer_service.generate_article) can only cite what the agent typed
plus their sales. This pipeline gives the article real, dated, sourced local evidence and then
checks every claim against it:

  1. RESEARCH - start from the town's shared knowledge (town_knowledge_service: the IDX Areas
     research every town already has, a dated Bright MLS sales snapshot, and the town's evidence
     bank), then use Claude web search ONLY for what's missing or topic-specific (property taxes,
     current mortgage rates, recent local developments). Gap findings with real sources go back
     into the town's evidence bank so the next article / Spotlight / IDX page reuses them free.
  2. WRITE - one numbered EVIDENCE LEDGER ([E1]...[En], each with source + date) + the agent's
     brief + the agent's publishing rules (_ENGINE_RULES). The writer must cite ledger ids for
     every factual sentence.
  3. FACT-CHECK - a separate model call reads the article against the same ledger, lists every
     claim with a verdict, flags compliance/geography/experience/staleness problems, scores it,
     and returns exact find/replace fixes that are applied before the draft is saved.

Two engines (config "blog_pipeline_engines") so quality vs cost can be compared on real articles:
"max" (fresh topic research, Opus writer) and "lean" (shared knowledge + gap-only research, Sonnet
writer). Each run is fully independent: nothing one run researches is visible to another unless
`persist_research` is on, and "max" never reads the evidence bank.

Billing: every stage is a separate usage event (event_type "blog_writer") against the agent's
shared monthly AI allowance - no per-article count limit. A run only starts when the remaining
allowance covers the engine's typical full cost (`blog_pipeline_gate_usd`), and the platform-wide
"blog_pipeline_global" ceiling is reserved up front and trued up to the real total at the end.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import User
from app.services.ai import ai_blog_writer_service as writer
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS, reconcile_budget, record_usage_event,
    reserve_global_and_emergency, total_monthly_ai_cost,
)
from app.services.areas import town_knowledge_service as tk

logger = logging.getLogger(__name__)

EVENT_TYPE = "blog_writer"
ENGINES = ("max", "lean")

# ---------------------------------------------------------------------------
# The agent's publishing rules (from the brokerage-grade blog standard the product owner wrote),
# generalized to any agent/market. Shared by the writer; the fact-checker enforces the same list.
# ---------------------------------------------------------------------------

_ENGINE_RULES = """You are an expert real estate blog writer, local market researcher, SEO strategist, compliance-aware editor and fact checker, writing in the first person as the agent named in the request. The goal is not the most impressive-sounding article - it is the most useful, locally knowledgeable, credible, accurate and search-effective article possible: one a knowledgeable local agent would believe was written by another knowledgeable local agent, where every factual statement survives a fact-check. Accuracy comes before writing quality.

EVIDENCE - THE ONLY SOURCE OF FACTS
The request contains an EVIDENCE LEDGER: numbered items [E1], [E2]... each with its source and date. Every statistic, price, count, date, rate, tax figure, school or district statement, named place, business, park or station, and every statement about market conditions must come from a ledger item or from the agent's own brief. Never use a second-hand figure (a news site "citing" Freddie Mac) when the primary figure is in the ledger, and never talk about your own sourcing in the article ("I haven't confirmed...", "my research shows"). If a point would benefit from a fact that is not in the ledger, leave it out, make the point qualitatively, or tell the reader what to verify - never invent, estimate or "round into" a figure. Never turn an assumption into a fact. In `claims`, list every factual sentence with the ledger ids it rests on.

CONFIDENCE
Bright MLS / agent data, government, school-district, Census and Freddie Mac items are VERIFIED - state them plainly. Third-party estimates (Zillow, Redfin, Realtor.com, news, directories) are LIKELY - attribute them ("Zillow's June 2026 estimate puts..."). Anything weaker is UNVERIFIED - leave it out.

DATES
Anything that changes over time (prices, inventory, days on market, rates, taxes, programs, rules) carries its date or window from the ledger: "In the 90 days ending September 9, 2026..." / "Freddie Mac's survey for the week of September 25, 2026...". Never make stale information sound current. Prefer numbers to adjectives, but one sale or one month never proves a trend - use transactions as examples, not universal evidence.

FACT VS INTERPRETATION
State facts plainly; label analysis as analysis ("that limited supply appears to be one factor supporting prices"). No causation the evidence doesn't show. Market labels ("seller's market", "bidding wars are common", "prices are rising", "inventory is extremely low", "homes sell immediately") only when the ledger numbers show it - and then show the numbers.

LOCAL GEOGRAPHY
Mailing address / postal town, municipality or township, county, school district and ZIP code are different things and often don't line up - one postal town can span several townships and school districts. Never assume a ZIP equals a municipality or that a town has one school district; where location matters, say it depends on the specific address. Mention only towns and places that are relevant and in the ledger - never sprinkle town names for SEO.

SCHOOLS
Objective only: name the districts/schools in the ledger and say attendance depends on the address and should be verified with the district. Never call a school or district best, top, excellent, great, highly rated, prestigious, desirable, sought-after or good for families. Rankings or test data only when they are in the ledger, attributed to their source.

FAIR HOUSING
Never reference or imply race, color, religion, sex, familial status, national origin, disability, sexual orientation or age - of an area, a home or who would or should live there. Never use schools, demographics, religion, crime or safety to suggest who belongs somewhere. Never write: family/families/family-friendly, perfect for / ideal for [a type of person], safe / safe neighborhood, exclusive, integrated, traditional neighborhood, bachelor, empty nesters, young professionals, retirees, master bedroom (say primary bedroom), crime. Describe places, homes, amenities and the process - not people.

THE AGENT'S EXPERIENCE
Never invent the agent's experience: no "I recently represented...", "I helped one of my buyers...", "in one of my recent transactions...", "I see this constantly with my clients...", no client stories, years in business, sales volume, awards or credentials unless the agent wrote them in the brief. Ledger items of type agent_sales are the agent's own closed transactions, but WHICH SIDE they represented is not recorded: you may call one "a recent sale of mine" and give its public facts (address, town, price, month), but never say buyer or seller side, never describe negotiations, never reveal client details. When the brief contains the agent's personal take, it is the heart of the article - use it faithfully in their voice and add nothing to it.

OFFERS AND STRATEGY
Protect the client. Never "do whatever it takes to win"; never tell readers to waive inspections or contingencies. Explain options and tradeoffs (price, financing, deposit, settlement timing, inspection, appraisal and mortgage risk, seller priorities) and that risk tolerance differs ("Some buyers may consider modifying inspection terms in highly competitive situations, but the risks should be understood first"). Legal and tax specifics: speak generally and point to the right professional.

{state_rules}

STYLE
Confident, conversational, concise, practical, locally informed and professional - a sharp local agent, not an ad agency. Lead with the answer: the opening answers the reader's real question with current context, then says what the article will help them understand - no warm-up. Every paragraph adds new information; short paragraphs; never restate a point in new words (cut the ~20-30% of words that add nothing). Never write: nestled, boasting/boasts, unparalleled, discover the charm, whether you're a..., your dream home awaits, vibrant community, hidden gem, stunning, don't miss out, in today's market, navigating, look no further, it's important to note, emojis, hashtags, clickbait.

SEO
Answer the real question someone types into Google (the search intent). Primary keyword naturally in the title, the first paragraph and one H2; 3-6 secondary terms used naturally, never stuffed. H2s read like a table of contents a reader would scan. Add a "Frequently asked questions" H2 (subheading question + short paragraph answer pairs) when it genuinely helps. seo_title <= 60 characters; meta_description 140-158 characters, written to earn the click. Length: the brief's target - value per paragraph beats word count.

CALL TO ACTION
Natural and specific to the article ("If you're weighing an offer in Villanova, I can show you what has recently sold, what is competing for buyers right now, and how I'd approach a specific home") - never pushy.

BEFORE YOU SUBMIT
Audit every factual sentence: is it in the ledger? current and dated? specific enough? could geography mislead? ZIP vs municipality confused? opinion dressed as fact? fair-housing sensitive? invented agent involvement? stale number? implied causation? Fix it, then submit.

FORMAT
You may use **double asterisks** for occasional bold emphasis inside paragraphs. Deliver the article ONLY by calling the submit_blog_article tool, exactly once."""

_STATE_RULES = {
    "PA": """PENNSYLVANIA PRACTICE (the agent is licensed in Pennsylvania)
Use Pennsylvania terms and practice: Agreement of Sale, mortgage contingency, inspection contingency, appraisal, deposit, settlement and settlement date, title, seller assist, buyer financial information, preapproval, Seller's Property Disclosure Statement, HOA/condominium resale documents, home-sale contingency. Don't import other states' procedures or terms, and don't tell buyers they universally need an attorney for a routine offer.""",
}
_STATE_DEFAULT = """STATE PRACTICE
Use the terminology and practices of the agent's state. Don't import other states' procedures, and avoid state-specific legal claims that aren't in the ledger."""


def _writer_system(state: Optional[str]) -> str:
    return _ENGINE_RULES.replace("{state_rules}", _STATE_RULES.get((state or "").upper(), _STATE_DEFAULT))


# Writer output = the existing article tool + per-claim citations + the agent's quality report.
_WRITER_TOOL = json.loads(json.dumps(writer._ARTICLE_TOOL))
_WRITER_TOOL["input_schema"]["properties"].pop("facts_used", None)
_WRITER_TOOL["input_schema"]["required"] = [r for r in _WRITER_TOOL["input_schema"]["required"] if r != "facts_used"] + ["claims", "review"]
_WRITER_TOOL["input_schema"]["properties"]["claims"] = {
    "type": "array",
    "description": "Every factual sentence or phrase in the article (quoted exactly) and the ledger ids it rests on.",
    "items": {"type": "object", "properties": {
        "text": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["verified", "likely"]},
    }, "required": ["text", "evidence_ids", "confidence"]},
}
_WRITER_TOOL["input_schema"]["properties"]["review"] = {
    "type": "object",
    "description": "Private notes for the agent - never shown in the article.",
    "properties": {
        "search_intent": {"type": "string", "description": "The real question this article answers."},
        "primary_keyword": {"type": "string"},
        "secondary_keywords": {"type": "array", "items": {"type": "string"}},
        "agent_specific_claims": {"type": "array", "items": {"type": "string"}, "description": "Statements based on the agent's own experience or transactions."},
        "improvements": {"type": "array", "items": {"type": "string"}, "description": "Information the agent could add to make it stronger."},
        "image_ideas": {"type": "array", "items": {"type": "object", "properties": {"idea": {"type": "string"}, "alt": {"type": "string"}}, "required": ["idea", "alt"]}},
        "internal_link_ideas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["search_intent", "primary_keyword", "secondary_keywords", "agent_specific_claims", "improvements", "image_ideas"],
}

# ---------------------------------------------------------------------------
# Research stage
# ---------------------------------------------------------------------------

_TOPICS = ["market", "rates", "taxes", "schools", "transit", "downtown", "parks", "dining", "development", "regulation", "other"]

_EVIDENCE_TOOL = {
    "name": "submit_evidence",
    "description": "Submit the verified facts you found, each with the page you read it on.",
    "input_schema": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "town": {"type": "string", "description": "Town it's about, e.g. 'Ardmore, PA', or 'general' for regional/national facts like mortgage rates."},
            "topic": {"type": "string", "enum": _TOPICS},
            "claim": {"type": "string", "description": "One self-contained factual statement with its number, geography and date window."},
            "source_name": {"type": "string"},
            "source_url": {"type": "string", "description": "The exact page you read it on."},
            "as_of": {"type": "string", "description": "Date or period the fact describes, e.g. '2026-09-25' or 'FY 2026-27'."},
            "confidence": {"type": "number", "description": "0-1."},
        }, "required": ["town", "topic", "claim", "source_name", "source_url", "as_of", "confidence"]}},
        "not_found": {"type": "array", "items": {"type": "string"}, "description": "What you looked for and could not verify."},
    }, "required": ["items", "not_found"]},
}

_RESEARCH_SYSTEM = """You are the research desk for a licensed real estate agent's blog. Your job is to find CURRENT, VERIFIABLE facts the article needs - not to write anything.

Rules:
- Use the web_search tool. Prefer, in order: county/municipal government, school district websites, state/federal government (Census), Freddie Mac / Federal Reserve, Bright MLS / brokerage research, established real estate data providers, reputable local journalism. Never rely on random blogs, SEO listicles or AI-generated pages when a primary source exists.
- Every item must be a fact you actually read on the page you cite, with its number, geography and date window. If two sources disagree, report the more authoritative one and say so in the claim.
- Keep geography exact: postal town vs township/municipality vs county vs school district vs ZIP. If a fact is for a township or county rather than the town, say which.
- Schools: districts, boundaries, budgets, millage, official policies - never rankings or opinions unless attributed to a named source.
- Never research or report anything about the demographics, religion, ethnicity or "type" of residents, or crime.
- Don't repeat facts listed under ALREADY KNOWN - find what's missing.
- When you're done, call submit_evidence exactly once with everything you verified, and list in not_found what you couldn't verify."""

# ---------------------------------------------------------------------------
# Fact-check stage
# ---------------------------------------------------------------------------

_REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit the fact-check and compliance review.",
    "input_schema": {"type": "object", "properties": {
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "quote": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "verdict": {"type": "string", "enum": ["supported", "partly_supported", "unsupported", "stale", "analysis"]},
        }, "required": ["quote", "evidence_ids", "verdict"]}},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["unsupported_claim", "wrong_number", "stale_data", "wrong_geography", "fair_housing", "school_language",
                                                "invented_experience", "causation", "legal_tax", "generic_ai_phrasing", "repetition", "other"]},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "quote": {"type": "string"},
            "problem": {"type": "string"},
            "find": {"type": "string", "description": "Exact verbatim substring of the article to replace (unique, within one sentence or list item)."},
            "replace": {"type": "string", "description": "Corrected text - qualify, attribute, date, soften or remove. Empty string deletes it. No new facts."},
        }, "required": ["kind", "severity", "quote", "problem", "find", "replace"]}},
        "scores": {"type": "object", "properties": {
            k: {"type": "integer"} for k in ("accuracy", "local_specificity", "usefulness", "voice", "readability", "seo", "compliance")
        }, "required": ["accuracy", "local_specificity", "usefulness", "voice", "readability", "seo", "compliance"]},
        "summary": {"type": "string"},
        "improvements": {"type": "array", "items": {"type": "string"}},
    }, "required": ["claims", "issues", "scores", "summary", "improvements"]},
}

_REVIEW_SYSTEM = """You are the fact-check and compliance editor for a licensed real estate agent's blog - strict, specific and fair. You receive the EVIDENCE LEDGER the writer was allowed to use, the agent's brief, and the finished ARTICLE.

1. In `claims`, list every factual claim (numbers, dates, places, businesses, schools/districts, market statements, rates, taxes, statements about the agent's experience), quoted exactly, with the ledger ids that support it and a verdict: supported (the ledger says it - same number, date window and geography), partly_supported (right idea but a detail is off, unattributed or undated), unsupported (not in the ledger or brief), stale (the ledger date is too old to state as current), analysis (clearly labeled interpretation - fine).
2. In `issues`, report every problem: unsupported_claim, wrong_number, stale_data, wrong_geography (ZIP vs township vs school district confusion, assuming one district, wrong town), fair_housing (protected classes, steering, "family", "safe", "perfect for..."), school_language (any subjective school/district wording), invented_experience (agent involvement, buyer/seller side, clients, years or volume not in the brief), causation (unsupported cause-and-effect), legal_tax (definitive legal/tax advice, advising to waive contingencies), generic_ai_phrasing (filler, hype, cliches like nestled/vibrant/dream home/hidden gem/navigating), repetition, other.
3. Give every issue a minimal fix: `find` = an exact, verbatim substring of the ARTICLE text (copy it character for character, long enough to be unique, inside one sentence or list item), `replace` = the corrected text (qualify, attribute, add the date, soften, or "" to delete). Never add a fact that isn't in the ledger. A figure the ledger only has second-hand (a news site "citing" the primary source) or marks as unconfirmed gets DELETED, not hedged, when the primary figure is also present. The article must never talk about its own sourcing ("I haven't confirmed...", "according to my research", "the ledger") - fix those by deleting or rewriting.
4. Score 1-10 - honestly, a 9-10 must be earned: accuracy, local_specificity (could only have been written about this place), usefulness (answers the reader's real question), voice (sounds like a knowledgeable local agent, not AI), readability, seo, compliance.
5. `summary`: 1-2 plain sentences for the agent. `improvements`: up to 4 specific things the agent could add (their own experience, a photo, a number they have) to make it stronger.

Respond only by calling submit_review, exactly once."""


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _new_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "web_search_requests": 0}


def _add_usage(acc: dict, resp) -> None:
    u = resp.usage
    acc["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    acc["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    acc["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    acc["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        acc["web_search_requests"] += getattr(stu, "web_search_requests", 0) or 0


def _cost(config: dict, model: str, u: dict) -> float:
    p = (config.get("model_pricing") or {}).get(model) or {}
    inp, out = p.get("input_per_mtok", 0.0), p.get("output_per_mtok", 0.0)
    return round(
        (u["input_tokens"] * inp
         + u["cache_creation_input_tokens"] * inp * config.get("blog_pipeline_cache_write_multiplier", 1.25)
         # Cache reads are 0.1x input on most models, 0.05x on Opus 5.5 / Sonnet 5.5 (model_pricing).
         + u["cache_read_input_tokens"] * inp * p.get("cache_read_multiplier", config.get("blog_pipeline_cache_read_multiplier", 0.1))
         + u["output_tokens"] * out) / 1_000_000
        + u["web_search_requests"] * config.get("blog_pipeline_web_search_cost_usd", 0.01),
        6,
    )


def _tool_input(resp, name: str) -> Optional[dict]:
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == name and isinstance(b.input, dict):
            return b.input
    return None


async def _run_tool_call(*, model: str, system: str, user_msg: str, tools: list[dict], tool_name: str, max_tokens: int,
                         effort: Optional[str], usage: dict) -> dict:
    """One model turn that must end in `tool_name` (forced tool_choice isn't available on the 5.5
    models, so this asks, resumes server-tool pauses, and nudges once if the call didn't come)."""
    client = writer._client()
    messages: list[dict] = [{"role": "user", "content": user_msg}]
    extra = {"output_config": {"effort": effort}} if effort else None
    nudged = False
    for _ in range(6):
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools, tool_choice={"type": "auto"}, messages=messages,
        )
        if extra:
            kwargs["extra_body"] = extra
        async with client.messages.stream(**kwargs) as stream:
            resp = await stream.get_final_message()
        _add_usage(usage, resp)
        if resp.stop_reason == "refusal":
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI declined this request - try rewording the topic.")
        payload = _tool_input(resp, tool_name)
        if payload is not None:
            return payload
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue  # server-side tool loop paused - resend and it resumes
        if resp.stop_reason == "max_tokens":
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI ran out of room before finishing - please try again.")
        if nudged:
            break
        nudged = True
        messages.append({"role": "user", "content": f"Now call {tool_name} with your result."})
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI didn't return a result - please try again. You were only charged for what ran.")


class _Meter:
    """Records each stage as its own usage event on the agent's allowance, and keeps the totals."""

    def __init__(self, db: AsyncSession, user: User, config: dict):
        self.db, self.user, self.config = db, user, config
        self.stages: list[dict] = []
        self.total = 0.0

    async def record(self, stage: str, model: str, usage: dict, started: float, *, failed: Optional[str] = None) -> None:
        cost = _cost(self.config, model, usage)
        self.total += cost
        self.stages.append({"stage": stage, "model": model, "cost_usd": round(cost, 4), "seconds": round(time.monotonic() - started, 1),
                            "input_tokens": usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
                            "output_tokens": usage["output_tokens"], "web_searches": usage["web_search_requests"]})
        await record_usage_event(
            self.db, self.user.id, None, model, usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
            usage["output_tokens"], cost, 1, bool(failed), failed, tool_names=[f"blog_pipeline_{stage}"], event_type=EVENT_TYPE,
        )


async def _gate(db: AsyncSession, user: User, config: dict, engine: str) -> None:
    """Only start when the agent's remaining monthly allowance covers a typical full run - the
    allowance is the only limit (no article count), and a run can't push them past it."""
    if user.id in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        return
    limit = (config.get("agent_monthly_ai_budget_usd_by_tier") or {}).get(user.plan_tier)
    if not limit:
        return
    need = float((config.get("blog_pipeline_gate_usd") or {}).get(engine, 1.0))
    if await total_monthly_ai_cost(db, user.id) + need > float(limit):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "There isn't enough AI usage left this month for a full researched article. It resets on the 1st.",
        )


# ---------------------------------------------------------------------------
# Evidence ledger
# ---------------------------------------------------------------------------

def _sale_items(sales: list[dict]) -> list[dict]:
    out = []
    for s in sales:
        bits = [writer._money(s.get("price")) and f"closed at {writer._money(s.get('price'))}",
                s.get("sold_date") and f"on {s['sold_date']}",
                s.get("beds") and f"{s['beds']} bd", s.get("baths") and f"{s['baths']} ba",
                s.get("sqft") and f"{s['sqft']} sqft"]
        out.append({"topic": "agent_sales", "claim": f"{s.get('address')}, {s.get('city')} {', '.join(b for b in bits if b)} - one of the agent's own closed transactions (buyer/seller side not recorded).",
                    "source_name": "Bright MLS (agent's closed sales)", "source_url": None, "as_of": s.get("sold_date"), "origin": "agent_sales", "town": s.get("city")})
    return out


def _listing_items(listings: list[dict]) -> list[dict]:
    return [{"topic": "agent_listing", "claim": f"{writer._fmt_property(p)} - a listing the agent attached.", "source_name": "Bright MLS (agent's listing)",
             "source_url": None, "as_of": datetime.now(timezone.utc).date().isoformat(), "origin": "agent_listing", "town": p.get("city")} for p in listings]


def _build_ledger(items: list[dict], per_town_cap: int = 70) -> tuple[list[dict], str]:
    """Number every item [E1]..[En] and render the ledger text."""
    seen, ledger, per_town = set(), [], {}
    for it in items:
        key = (it.get("claim") or "").strip().lower()
        if not key or key in seen:
            continue
        town = it.get("town") or "general"
        if it.get("origin") in ("idx_research", "evidence_bank") and per_town.get(town, 0) >= per_town_cap:
            continue
        per_town[town] = per_town.get(town, 0) + 1
        seen.add(key)
        ledger.append({**it, "eid": f"E{len(ledger) + 1}"})
    lines = []
    for it in ledger:
        src = it.get("source_name") or "source"
        lines.append(f"[{it['eid']}] ({it.get('topic')} | {src}{' | as of ' + str(it['as_of']) if it.get('as_of') else ''}) {it['claim']}")
    return ledger, "\n".join(lines)


def _article_text(a: dict) -> str:
    lines = [a.get("title") or "", a.get("subtitle") or "", f"SEO title: {a.get('seo_title') or ''}", f"Meta description: {a.get('meta_description') or ''}"]
    lines += [f"- {g}" for g in a.get("at_a_glance") or [] if isinstance(g, str)]
    lines += [p for p in a.get("intro") or [] if isinstance(p, str)]
    for sec in a.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        lines.append(f"\n## {sec.get('heading') or ''}")
        for b in sec.get("blocks") or []:
            if not isinstance(b, dict):
                continue
            if b.get("text"):
                lines.append(("### " if b.get("type") == "subheading" else "") + b["text"])
            lines += [f"- {i}" for i in b.get("items") or [] if isinstance(i, str)]
    closing = a.get("closing") or {}
    if closing.get("heading"):
        lines.append(f"\n## {closing['heading']}")
    lines += [p for p in closing.get("paragraphs") or [] if isinstance(p, str)]
    return "\n".join(l for l in lines if l is not None)


def _apply_fixes(article: dict, issues: list[dict]) -> tuple[int, list[dict]]:
    """Apply the fact-checker's exact find/replace fixes to every text field of the article.
    Returns (applied_count, issues annotated with `fixed`)."""
    skip = {"claims", "review", "category", "cover_image_idea"}
    out = []
    applied = 0

    def walk(node, find, repl):
        hit = False
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in skip:
                    continue
                if isinstance(v, str) and find in v:
                    node[k] = " ".join(v.replace(find, repl).split()) if repl == "" else v.replace(find, repl)
                    hit = True
                elif isinstance(v, (dict, list)):
                    hit = walk(v, find, repl) or hit
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str) and find in v:
                    node[i] = v.replace(find, repl).strip()
                    hit = True
                elif isinstance(v, (dict, list)):
                    hit = walk(v, find, repl) or hit
        return hit

    for issue in issues:
        find = (issue.get("find") or "").strip()
        repl = issue.get("replace") if isinstance(issue.get("replace"), str) else None
        fixed = bool(find) and repl is not None and find != repl and walk(article, find, repl)
        applied += int(fixed)
        out.append({**issue, "fixed": fixed})
    # Drop list items / paragraphs a deletion emptied out.
    article["intro"] = [p for p in article.get("intro") or [] if isinstance(p, str) and p.strip()]
    article["at_a_glance"] = [p for p in article.get("at_a_glance") or [] if isinstance(p, str) and p.strip()]
    for sec in article.get("sections") or []:
        for b in sec.get("blocks") or []:
            if isinstance(b, dict) and isinstance(b.get("items"), list):
                b["items"] = [i for i in b["items"] if isinstance(i, str) and i.strip()]
    return applied, out


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

Progress = Optional[Callable[[str], Awaitable[None]]]


async def precheck(db: AsyncSession, user: User, engine: Optional[str] = None) -> str:
    """Run before queueing a background job, so "not enough AI usage left" is an instant answer.
    Returns the engine that will run."""
    writer._require_key()
    config = await ai_config_service.get_config(db)
    engine = engine if engine in ENGINES else config.get("blog_pipeline_default_engine", "max")
    await _gate(db, user, config, engine)
    return engine


async def run_pipeline(db: AsyncSession, user: User, brief: dict, *, engine: Optional[str] = None, persist_research: bool = True,
                       on_stage: Progress = None) -> dict:
    """Research -> write -> fact-check. Returns generate_article's shape plus `verification`.
    `on_stage(name)` is awaited as each stage starts: "gathering", "research", "write", "fact_check", "finishing"."""
    writer._require_key()
    config = await ai_config_service.get_config(db)
    engine = engine if engine in ENGINES else config.get("blog_pipeline_default_engine", "max")
    spec = config["blog_pipeline_engines"][engine]
    await _gate(db, user, config, engine)
    try:
        reservation = await reserve_global_and_emergency(
            db, "blog_pipeline_global", float(config["blog_pipeline_reservation_estimate_usd"][engine]),
            config["blog_pipeline_global_daily_cost_ceiling_usd"], config["blog_pipeline_global_monthly_cost_ceiling_usd"], config,
        )
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)

    meter = _Meter(db, user, config)
    t0 = time.monotonic()
    try:
        result = await _run(db, user, brief, engine, spec, config, meter, persist_research, on_stage)
    except Exception:
        await reconcile_budget(db, reservation, meter.total)
        raise
    await reconcile_budget(db, reservation, meter.total)
    result["verification"].update({"cost_usd": round(meter.total, 4), "stages": meter.stages, "seconds": round(time.monotonic() - t0, 1)})
    result["usage"] = {"engine": engine, "cost_usd": round(meter.total, 4), "stages": meter.stages}
    return result


async def _stage(on_stage: Progress, name: str) -> None:
    if on_stage:
        try:
            await on_stage(name)
        except Exception:  # progress is cosmetic - never fail the article over it
            logger.warning("blog pipeline: progress update failed", exc_info=True)


async def _served_areas(db: AsyncSession, user: User, limit: int = 5):
    from sqlalchemy import select
    from app.models.database import AgentMarketArea, MarketArea
    return (await db.execute(
        select(MarketArea).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id).where(AgentMarketArea.owner_id == user.id).limit(limit)
    )).scalars().all()


async def _run(db: AsyncSession, user: User, brief: dict, engine: str, spec: dict, config: dict, meter: _Meter, persist: bool,
               on_stage: Progress = None) -> dict:
    await _stage(on_stage, "gathering")
    hub_config = await writer._hub_config(db, user.id)
    profile = writer._agent_profile(user, hub_config)
    sales = []
    if brief.get("include_recent_sales"):
        wanted = set(brief.get("sale_ids") or [])
        sales = [s for s in writer._recent_sales(hub_config, limit=300) if not wanted or s["id"] in wanted][:10]
    listings = [p for p in (brief.get("listings") or []) if isinstance(p, dict)][:6]
    state = (user.state if hasattr(user, "state") and user.state else None) or next((s.get("state") for s in sales if s.get("state")), None) or "PA"

    # --- 1. What we already know (free) ---------------------------------------------------
    areas = await tk.find_areas(db, slugs=list(brief.get("area_slugs") or []), names=list(brief.get("areas") or []), state=state)
    areas = areas[:4]
    # A general article ("buying a home in the Philly suburbs", "PA closing costs") names no town:
    # the agent's own served towns become REGIONAL context - a few dated market numbers and the
    # basics per town for examples - and research focuses on state/county-level facts instead.
    region_words = ("suburb", "county", "main line", "region", "area", "metro", "valley", "shore", "greater ", "philly", "philadelphia")
    named = [str(a) for a in (brief.get("areas") or [])]
    regional = not areas and (not named or all(any(w in n.lower() for w in region_words) for n in named))
    if regional:
        areas = list(await _served_areas(db, user, 5))
    dossiers = [await tk.town_dossier(db, a, include_bank=bool(spec.get("reuse_bank")) or regional) for a in areas]
    if regional:
        for d in dossiers:
            d["items"] = [i for i in d["items"] if i["origin"] == "mls_live" or i["topic"] in ("schools", "transit", "downtown")][:14]
    known_items = [it for d in dossiers for it in d["items"]]
    # State-wide facts (transfer tax, buyer programs, rules, latest rates) - shared by every
    # article; Premium still re-verifies the ones the topic leans on.
    try:
        state_known = await tk.state_items(db, state)
    except Exception:
        logger.warning("blog pipeline: state facts lookup failed", exc_info=True)
        state_known = []
    known_items += state_known
    try:
        rate_items = await tk.freddie_mac_rate_items()
    except Exception:
        logger.warning("blog pipeline: Freddie Mac PMMS fetch failed", exc_info=True)
        rate_items = []

    # --- 1b. Research what's missing (web search) ----------------------------------------
    await _stage(on_stage, "research")
    started = time.monotonic()
    usage = _new_usage()
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    town_lines = []
    for d in dossiers:
        known = [f"  - {i['claim']}" for i in d["items"] if i["origin"] != "idx_research" or i["topic"] in ("schools", "market", "transit", "downtown")][:30]
        town_lines.append(f"{d['town']}: we hold {len(d['items'])} sourced facts (topics: {', '.join(f'{k} {v}' for k, v in d['covered'].items())}). "
                          f"Gaps: {', '.join(d['gaps']) or 'none'}.\n  ALREADY KNOWN:\n" + "\n".join(known))
    unknown_towns = [] if regional else [t for t in (brief.get("areas") or []) if not any(t.split(",")[0].strip().lower() == a.name.lower() for a in areas)]
    general = ("\n\nThis article is GENERAL (not about one town) - the towns above are only the agent's service area. Research the "
               f"state, county and regional facts it needs ({state} transfer/recording taxes and how they're split, the state's "
               "first-time-buyer programs, county-level facts, statewide rules and timelines), not town trivia."
               if regional else "")
    # Nothing stored for the region and no service towns (e.g. a new agent writing about "the Philly
    # suburbs"): the research step first pins down what the region actually is, from sources.
    region_label = ", ".join(named) or profile.get("market") or ""
    if regional and not dossiers and region_label:
        general += (f"\n\nWe hold NO stored data for '{region_label}'. First establish from authoritative sources what it commonly refers "
                    "to (which counties and major municipalities), then gather a few dated, sourced basics for it: counties and their "
                    "seats, the main school districts, regional rail/transit lines, and any official county housing or market figures.")
    rates_note = ("\n\nMortgage rates are already supplied from Freddie Mac's own published survey - do NOT research mortgage rates."
                  if rate_items else "")
    if engine == "max":
        task = ("Research this article FRESH and thoroughly: the specific question it answers, the most recent local developments "
                "(last 12 months) that matter to it, property-tax millage for the relevant township(s)/school district(s), current mortgage "
                "rates (Freddie Mac PMMS latest week) if buying, selling or the market is discussed, and re-verify any already-known fact the "
                "article will lean on that may have changed.")
    else:
        task = ("Fill ONLY the gaps: property-tax millage for the relevant township(s)/school district(s) if missing, the current 30-year "
                "mortgage rate (Freddie Mac PMMS latest week) if buying, selling or the market is discussed, and at most one fact specific "
                "to this topic that the known facts don't cover. Keep it tight.")
    research_msg = (
        f"Today is {today}. Agent: {profile['name']} ({profile['brokerage']}), market: {profile['market'] or state}.\n"
        f"ARTICLE TOPIC: {brief.get('topic', '').strip()}\n"
        + (f"Goal: {brief['goal']}\n" if brief.get("goal") else "")
        + (f"Audience: {', '.join(brief['audiences'])}\n" if brief.get("audiences") else "")
        + (f"Key points: {brief['key_points']}\n" if brief.get("key_points") else "")
        + (f"\nREGION the article covers: {', '.join(named)}" if regional and named else "")
        + (f"\n{state} STATE-WIDE FACTS ALREADY KNOWN (don't re-research unless they may have changed):\n" + "\n".join(f"  - {i['claim']} ({i.get('as_of')})" for i in state_known[:40]) + "\n" if state_known else "")
        + "\nTOWNS:\n" + ("\n\n".join(town_lines) if town_lines else "(no towns with stored research)")
        + (f"\n\nTowns with NO stored research (research their basics: municipality/township, county, school district(s), rail/transit, downtown): {', '.join(unknown_towns)}" if unknown_towns else "")
        + f"\n\nYOUR TASK: {task}{general}{rates_note}"
    )
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": int(spec["research_searches"]),
              "user_location": {"type": "approximate", "region": "Pennsylvania" if state == "PA" else state, "country": "US"}},
             _EVIDENCE_TOOL]
    if spec.get("research_fetches"):
        tools.insert(1, {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": int(spec["research_fetches"])})
    research_items: list[dict] = []
    not_found: list[str] = []
    try:
        found = await _run_tool_call(model=spec["research_model"], system=_RESEARCH_SYSTEM, user_msg=research_msg, tools=tools,
                                     tool_name="submit_evidence", max_tokens=16000, effort=spec.get("research_effort"), usage=usage)
        for it in found.get("items") or []:
            if isinstance(it, dict) and it.get("claim") and it.get("source_url"):
                research_items.append({"topic": it.get("topic") or "other", "claim": it["claim"].strip(), "source_name": it.get("source_name") or tk._domain(it.get("source_url")),
                                       "source_url": it["source_url"], "as_of": it.get("as_of"), "confidence": it.get("confidence"), "town": it.get("town") or "general",
                                       "origin": "web_research"})
        not_found = [x for x in found.get("not_found") or [] if isinstance(x, str)]
        await meter.record("research", spec["research_model"], usage, started)
    except HTTPException:
        # Research is an enrichment - the article can still be written from what we hold.
        await meter.record("research", spec["research_model"], usage, started, failed="research_incomplete")
        logger.warning("blog pipeline: research stage returned nothing", exc_info=True)
    except Exception:
        await meter.record("research", spec["research_model"], usage, started, failed="research_error")
        logger.warning("blog pipeline: research stage failed", exc_info=True)

    banked = 0
    if persist and research_items:
        town_names = {a.name.lower() for a in areas}
        state_level = [i for i in research_items if (i.get("town") or "general").split(",")[0].strip().lower() not in town_names]
        try:
            banked += await tk.save_state_items(db, state, state_level)
        except Exception:
            logger.warning("blog pipeline: state facts save failed", exc_info=True)
        for a in areas:
            mine = [i for i in research_items if (i.get("town") or "").split(",")[0].strip().lower() == a.name.lower()]
            try:
                banked += await tk.save_to_bank(db, a, mine)
            except Exception:
                logger.warning("blog pipeline: evidence bank save failed for %s", a.slug, exc_info=True)

    brief_facts = []
    if (brief.get("facts") or "").strip():
        brief_facts.append({"topic": "agent_brief", "claim": brief["facts"].strip(), "source_name": "The agent (brief)", "source_url": None,
                            "as_of": datetime.now(timezone.utc).date().isoformat(), "origin": "agent", "town": "general"})
    # MLS + agent data first so they get the lowest ids, then fresh research, then stored research.
    ordered = ([i for i in known_items if i["origin"] == "mls_live"] + rate_items + _sale_items(sales) + _listing_items(listings) + brief_facts
               + research_items + [i for i in known_items if i["origin"] == "state_bank"]
               + [i for i in known_items if i["origin"] not in ("mls_live", "state_bank")])
    ledger, ledger_text = _build_ledger(ordered)

    # --- 2. Write --------------------------------------------------------------------------
    spotlight: list[dict] = []
    if brief.get("area_slugs"):
        try:
            cards = {c["slug"]: c for c in await writer.area_spotlight_cards(db, user)}
            spotlight = [cards[s] for s in list(brief["area_slugs"])[:3] if s in cards]
        except Exception:
            logger.warning("blog pipeline: spotlight cards failed", exc_info=True)
    brief_msg, _ = writer._brief_text(brief, profile, [], [], None)
    brief_msg = brief_msg.split("== REAL DATA ==")[0].rstrip()
    write_msg = (
        f"{brief_msg}\n\nToday's date: {today}.\n"
        + ("A Town Spotlight card for " + ", ".join(c["name"] for c in spotlight) + " (photo, live numbers, districts) is inserted after the first section automatically - reference it rather than listing every figure.\n" if spotlight else "")
        + ("This is a REGIONAL article, not about one town. The town items in the ledger are the agent's service area - use a few as concrete, dated examples where they genuinely help the reader; never force town names in or imply one town represents the region.\n" if regional else "")
        + f"\n== EVIDENCE LEDGER ({len(ledger)} items - the only facts you may use; cite ids in `claims`) ==\n{ledger_text}"
        + (f"\n\nResearch could not verify (do not state these): {'; '.join(not_found[:8])}" if not_found else "")
    )
    await _stage(on_stage, "write")
    started = time.monotonic()
    usage = _new_usage()
    try:
        article = await _run_tool_call(model=spec["writer_model"], system=_writer_system(state), user_msg=write_msg, tools=[_WRITER_TOOL],
                                       tool_name=_WRITER_TOOL["name"], max_tokens=32000, effort=spec.get("writer_effort"), usage=usage)
    except Exception as exc:
        await meter.record("write", spec["writer_model"], usage, started, failed="write_failed")
        if isinstance(exc, HTTPException):
            raise
        logger.exception("blog pipeline: write stage failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer didn't respond - try again in a moment.")
    await meter.record("write", spec["writer_model"], usage, started)
    article = writer._normalize_article(article)
    if not article.get("title") or not article.get("sections"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer returned an incomplete draft - please try again.")
    writer_review = writer._coerce(article.get("review"), dict)
    writer_claims = [c for c in writer._coerce(article.get("claims"), list) if isinstance(c, dict)]

    # --- 3. Fact-check ------------------------------------------------------------------------
    draft_text = _article_text(article)
    check_msg = (
        f"Agent: {profile['name']} - {profile['brokerage']} - {profile['market'] or state}. Today's date: {today}.\n"
        f"Agent's brief:\n{brief_msg}\n\n== EVIDENCE LEDGER ==\n{ledger_text}\n\n== ARTICLE ==\n{draft_text}"
    )
    await _stage(on_stage, "fact_check")
    started = time.monotonic()
    usage = _new_usage()
    review: dict = {}
    try:
        review = await _run_tool_call(model=spec["check_model"], system=_REVIEW_SYSTEM, user_msg=check_msg, tools=[_REVIEW_TOOL],
                                      tool_name="submit_review", max_tokens=24000, effort=spec.get("check_effort"), usage=usage)
        await meter.record("fact_check", spec["check_model"], usage, started)
    except Exception:
        await meter.record("fact_check", spec["check_model"], usage, started, failed="fact_check_failed")
        logger.warning("blog pipeline: fact-check failed - saving with the unchecked report", exc_info=True)
    await _stage(on_stage, "finishing")
    issues = [i for i in writer._coerce(review.get("issues"), list) if isinstance(i, dict)]
    applied, issues = _apply_fixes(article, issues)

    # --- Assemble (same block layout as the single-call writer) --------------------------------
    blocks = writer.article_to_blocks(article, *writer.cta_block_args(brief))
    if spotlight:
        heads = [i for i, b in enumerate(blocks) if b["type"] == "heading"]
        at = heads[1] if len(heads) > 1 else (heads[0] if heads else min(2, len(blocks)))
        blocks[at:at] = [writer.area_spotlight_block(c) for c in spotlight]
    if listings:
        cards = [{"id": writer._bid("property_card"), "type": "property_card", "data": {"property": p, "displaySize": "large"}} for p in listings]
        closing_idx = max((i for i, b in enumerate(blocks) if b["type"] == "heading"), default=len(blocks))
        blocks[closing_idx:closing_idx] = cards
    blocks = writer.place_video(blocks, brief)
    full_text = "\n".join([article.get("title", ""), article.get("subtitle", ""), article.get("meta_description", ""), writer.blocks_to_text(blocks)])
    source_text = ledger_text + "\n" + brief_msg

    by_eid = {it["eid"]: it for it in ledger}
    claims = [c for c in writer._coerce(review.get("claims"), list) if isinstance(c, dict)]
    cited = {e for c in (claims or writer_claims) for e in (c.get("evidence_ids") or []) if e in by_eid}
    sources, seen_src = [], set()
    for eid in sorted(cited, key=lambda e: int(e[1:])):
        it = by_eid[eid]
        key = it.get("source_url") or it.get("source_name")
        if key and key not in seen_src:
            seen_src.add(key)
            sources.append({"name": it.get("source_name"), "url": it.get("source_url"), "as_of": it.get("as_of"), "topic": it.get("topic")})
    verdicts = [c.get("verdict") for c in claims]
    open_issues = [i for i in issues if not i["fixed"] and i.get("severity") in ("high", "medium")]
    data_dates = [d["sales_snapshot"].get("data_through") for d in dossiers if (d.get("sales_snapshot") or {}).get("data_through")]
    category = article.get("category") if article.get("category") in writer.BLOG_CATEGORIES else (brief.get("category_hint") or None)

    verification = {
        "engine": engine,
        "models": {"research": spec["research_model"], "write": spec["writer_model"], "fact_check": spec["check_model"]},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checked": bool(review),
        "claims_checked": len(claims),
        "claims_supported": verdicts.count("supported") + verdicts.count("analysis"),
        "claims_partly": verdicts.count("partly_supported"),
        "claims_unsupported": verdicts.count("unsupported") + verdicts.count("stale"),
        "issues_found": len(issues),
        "issues_fixed": applied,
        "open_issues": [{k: i.get(k) for k in ("kind", "severity", "quote", "problem")} for i in open_issues][:12],
        "fixed_issues": [{k: i.get(k) for k in ("kind", "severity", "quote", "problem", "replace")} for i in issues if i["fixed"]][:20],
        "sources": sources[:30],
        "source_count": len(sources),
        "evidence_count": len(ledger),
        "research_found": len(research_items),
        "research_not_found": not_found[:8],
        "research_banked": banked,
        "towns": [{"town": d["town"], "facts": len(d["items"]), "gaps": d["gaps"]} for d in dossiers],
        "regional": regional,
        "data_as_of": max(data_dates) if data_dates else datetime.now(timezone.utc).date().isoformat(),
        "scores": review.get("scores") or {},
        "summary": review.get("summary") or "",
        "improvements": [x for x in (writer._coerce(review.get("improvements"), list) + writer._coerce(writer_review.get("improvements"), list)) if isinstance(x, str)][:8],
        "seo": {"search_intent": writer_review.get("search_intent"), "primary_keyword": writer_review.get("primary_keyword") or article.get("focus_keyword"),
                "secondary_keywords": writer._coerce(writer_review.get("secondary_keywords"), list)[:8]},
        "agent_claims": [x for x in writer._coerce(writer_review.get("agent_specific_claims"), list) if isinstance(x, str)][:8],
        "image_ideas": [x for x in writer._coerce(writer_review.get("image_ideas"), list) if isinstance(x, dict)][:5],
        "numbers_to_verify": writer.number_audit(full_text, source_text),
    }
    return {
        "title": (article.get("title") or "").strip()[:200],
        "subtitle": (article.get("subtitle") or "").strip()[:300],
        "seo_title": (article.get("seo_title") or "").strip()[:70],
        "meta_description": (article.get("meta_description") or "").strip()[:320],
        "focus_keyword": (article.get("focus_keyword") or "").strip()[:80],
        "category": category,
        "tags": [t.strip()[:40] for t in (article.get("tags") or []) if isinstance(t, str) and t.strip()][:8],
        "blocks": blocks,
        "linked_properties": listings,
        "cover_image_idea": (article.get("cover_image_idea") or "").strip(),
        "facts_used": [{"fact": c.get("text", "")[:300], "source": ", ".join(c.get("evidence_ids") or [])} for c in writer_claims][:30],
        "numbers_to_verify": verification["numbers_to_verify"],
        "verification": verification,
    }


# ---------------------------------------------------------------------------
# State facts pack - the state-wide facts most articles lean on, researched once into
# shared_knowledge ("state:XX") so every agent's article reuses them free. Platform-paid (not on
# any agent's allowance); run when a state's pack is empty or stale.
# ---------------------------------------------------------------------------

_STATE_PACK_TASK = {
    "PA": ("Research and verify CURRENT Pennsylvania-wide facts a real estate blog needs, each from the primary source where possible: "
           "(1) Pennsylvania realty transfer tax - the 1% state rate, typical local (municipal + school district) rates in the Philadelphia "
           "suburbs, Philadelphia's own rate, and how buyer/seller customarily split it; "
           "(2) PHFA programs for buyers (Keystone Home Loan, Keystone Advantage / down payment and closing cost assistance) with current limits; "
           "(3) the Pennsylvania Real Estate Seller Disclosure Law (what sellers must disclose, the Seller's Property Disclosure Statement); "
           "(4) Pennsylvania settlement basics (title insurance is customary, who typically pays what); "
           "(6) property tax basics in Montgomery, Delaware and Chester counties (millage, reassessment status) with sources; "
           "(7) Pennsylvania homestead/farmstead exclusion basics. Use the town value 'general' for every item."),
}


_STATE_PACK_TASK.update({
    "NJ": ("Research and verify CURRENT New Jersey-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) the New Jersey Realty Transfer Fee (paid by the seller - rate tiers) and the buyer-paid 'mansion tax' on homes over $1M (current tiers); "
           "(2) NJHMFA first-time homebuyer programs (First-Time Homebuyer Mortgage, Down Payment Assistance) with current limits; "
           "(3) attorney review - the three-business-day attorney review period for contracts prepared by licensees; "
           "(4) New Jersey seller disclosure requirements; "
           "(5) property tax basics (NJ has among the highest effective rates - cite the official source), and the ANCHOR / Stay NJ property tax relief programs; "
           "(6) settlement customs (title insurance, who typically pays what). Use the town value 'general' for every item."),
    "DE": ("Research and verify CURRENT Delaware-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) Delaware realty transfer tax - the state rate, typical county/municipal rates, how buyer/seller customarily split it, and the first-time buyer reduction; "
           "(2) DSHA homebuyer programs (e.g. Welcome Home, first-time buyer assistance) with current limits; "
           "(3) Delaware Seller's Disclosure requirements; (4) attorney-conducted settlements in Delaware; "
           "(5) property tax basics for New Castle, Kent and Sussex counties (assessment / reassessment status); "
           "(6) settlement customs. Use the town value 'general' for every item."),
    "MD": ("Research and verify CURRENT Maryland-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) Maryland state transfer tax and recordation tax, typical county transfer/recordation taxes (Montgomery, Baltimore County, Anne Arundel, Howard, Prince George's), "
           "how buyer/seller customarily split them, and the first-time buyer exemption from the state transfer tax share; "
           "(2) Maryland Mortgage Program (MMP) first-time buyer programs and down payment assistance with current limits; "
           "(3) the Maryland Residential Property Disclosure and Disclaimer Statement; (4) the Homestead Tax Credit; "
           "(5) property tax basics (state + county rates, triennial assessments); (6) settlement customs. Use the town value 'general' for every item."),
    "VA": ("Research and verify CURRENT Virginia-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) Virginia recordation tax (state and local), the grantor's tax paid by the seller, and the regional congestion relief fee in Northern Virginia; "
           "(2) Virginia Housing first-time homebuyer programs and down payment assistance grants with current limits; "
           "(3) the Virginia Residential Property Disclosure Act (buyer-beware notice); "
           "(4) property tax basics for Fairfax, Loudoun, Arlington and Prince William counties; "
           "(5) settlement customs (settlement agents, title insurance). Use the town value 'general' for every item."),
    "DC": ("Research and verify CURRENT District of Columbia facts a real estate blog needs, from primary sources where possible: "
           "(1) DC deed recordation tax and transfer tax rates (and the higher rate tier), and how buyer/seller customarily split them; "
           "(2) DC HPAP (Home Purchase Assistance Program) and DC Open Doors / DCHFA programs with current limits; "
           "(3) the DC Seller's Disclosure Statement requirement; (4) DC TOPA basics as they affect buyers of tenant-occupied homes; "
           "(5) DC residential property tax rate and the Homestead Deduction; (6) settlement customs. Use the town value 'general' for every item."),
    "WV": ("Research and verify CURRENT West Virginia-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) West Virginia real estate excise (transfer) tax - the state rate and the county add-on, and who customarily pays; "
           "(2) WVHDF homebuyer programs (Homeownership Program, down payment assistance) with current limits; "
           "(3) West Virginia seller disclosure practice; (4) property tax basics (classes, assessment at 60%), Homestead Exemption; "
           "(5) settlement customs (attorney-conducted closings). Use the town value 'general' for every item."),
})


async def seed_state_facts(db: AsyncSession, state: str = "PA", *, model: str = "claude-sonnet-5-5", searches: int = 14, fetches: int = 6) -> dict:
    task = _STATE_PACK_TASK.get(state.upper())
    if not task:
        return {"added": 0, "error": "no pack defined for this state"}
    usage = _new_usage()
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches}, _EVIDENCE_TOOL]
    found = await _run_tool_call(model=model, system=_RESEARCH_SYSTEM, user_msg=f"Today is {today}.\n\nYOUR TASK: {task}", tools=tools,
                                 tool_name="submit_evidence", max_tokens=24000, effort="medium", usage=usage)
    items = [{"topic": i.get("topic") or "other", "claim": i["claim"].strip(), "source_name": i.get("source_name"), "source_url": i.get("source_url"),
              "as_of": i.get("as_of"), "confidence": i.get("confidence")} for i in (found.get("items") or []) if isinstance(i, dict) and i.get("claim") and i.get("source_url")]
    added = await tk.save_state_items(db, state, items)
    config = await ai_config_service.get_config(db)
    cost = _cost(config, model, usage)
    logger.info("state facts pack seeded", extra={"state": state, "added": added, "cost_usd": cost, "searches": usage["web_search_requests"]})
    return {"added": added, "found": len(items), "not_found": found.get("not_found") or [], "cost_usd": round(cost, 4), "usage": usage}
