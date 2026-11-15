"""Smart starting points for the AI writer's interview, read from the article's TITLE and what we know about the AGENT.

Owner, 11/14: "it's not intelligent sometimes ... I feel like I'm repeating myself ... I wish I was selecting more
than typing, based on suggestions the AI sees from the title." Until now every suggestion in the interview was a
fixed list or came from the agent's profile: the "angle" ideas named the agent's first town whatever the title said,
the town and the audience in the title were never ticked for them, and key points / the goal / their take were three
empty boxes.

Owner, later 11/14: "are we using info like [their recent sales, their towns, their profile, their IDX site, their
bio] to make the suggestions much more intelligent, obviously being based on the title first? And having title
suggestions also be very intelligent?" The first build read the title plus the town list only. Now `agent_context`
hands the same small call what we hold about the agent - the towns on their site, the homes they sold (by town, with
the price range), their bio, the articles they already wrote, and today's month - and `title_ideas` offers titles
before they have typed one.

Layers, so the interview is never empty and never slow:
  1. `from_title` - plain code, free, instant: who it is for (the same decision the article engine uses,
     blog_final_checks.reader_sides), which towns the title names (matched against the towns we hold), the closing
     button that fits, tone, length, and the price range the title points at (used to rank the agent's sales).
  2. `suggest` - ONE small call to the copywriter's model (Haiku) that reads the title and the agent's context and
     returns tappable choices: sharper titles, what the reader should walk away with, key points, and opinion starters
     for the agent's own take. About a twentieth of a cent; metered on the agent's allowance like every AI tool,
     inside the copywriter's platform ceilings; cached per title so going back and forth never charges twice. If the
     allowance is spent or the call fails, layer 1 plus a built-in list for that kind of article is returned.
  3. `title_ideas` - the same kind of call for the "Give it a title" screen: six titles built from the agent's towns,
     sales, the month and what they have not written yet. Cached for the day; plain-code ideas without the AI.

What the context may and may not do: it shapes WHICH choices are offered (a point about "my recent Wayne sales" only
when they have Wayne sales; no title they already wrote). It is never restated as a claim - no counts, no years in
business, no track record (grader 11/14: unsupported claims about the agent's experience were a High finding).
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AIUsageEvent, MarketArea, User
from app.services.ai import blog_final_checks as bfc
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_copywrite

logger = logging.getLogger(__name__)

EVENT_TYPE = "blog_writer"
AUDIENCES = ["Buyers", "Sellers", "First-time buyers", "Move-up buyers", "Luxury buyers", "Investors", "Relocating", "Homeowners"]
TONES = ["warm", "professional", "luxury", "straight", "energetic"]
LENGTHS = ["short", "standard", "long"]
CTAS = ["how_can_i_help", "home_value", "showing", "buying", "selling", "make_me_sell", "question"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

_AUDIENCE_WORDS = (
    ("First-time buyers", re.compile(r"first[- ]time|first home|starter home", re.I)),
    ("Move-up buyers", re.compile(r"move[- ]?up|moving up|bigger home|larger home|upsiz|outgrow", re.I)),
    ("Luxury buyers", re.compile(r"\bluxury\b|\bestate homes?\b|million[- ]dollar|\$\s?[1-9]\s?(?:m\b|million)", re.I)),
    ("Investors", re.compile(r"\binvest|\brental propert|\blandlord|\bduplex\b|multi[- ]family|\bflip", re.I)),
    ("Relocating", re.compile(r"\brelocat|\bmoving to\b|\bmove to\b|new to (?:the area|town)|\bthinking about [A-Z]", re.I)),
    ("Homeowners", re.compile(r"\bhomeowners?\b|property tax|\brefinanc|\brenovat|\bremodel|home value|\bequity\b|\bdownsiz|\bmaintenance\b", re.I)),
)
_LONG = re.compile(r"\bguide\b|everything|complete|step[- ]by[- ]step|\bvs\.?\b|\bversus\b|checklist", re.I)
_SHORT = re.compile(r"\bjust sold\b|\bquick\b|\bupdate\b|\bnews\b|\b\d+ (?:tips|things|reasons)\b", re.I)
# The price range a title points at - used to put the agent's sales that fit it first.
_PRICE_IN_TITLE = re.compile(r"(?P<dir>under|below|less than|up to|over|above|more than)?\s*\$\s?(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>k|m|mil|million)?(?![a-z])", re.I)
_LOW_BAND = re.compile(r"first[- ]time|first home|starter|affordable|\bcondos?\b|\btownhomes?\b|\btownhouses?\b|\bdownsiz", re.I)
_HIGH_BAND = re.compile(r"\bluxury\b|\bestates?\b|million[- ]dollar|\bhigh[- ]end\b|\bupscale\b", re.I)


def price_hint(title: str) -> dict:
    """{"band": "low"|"high"|None, "min": dollars|None, "max": dollars|None} - what the title says about price."""
    text = str(title or "")
    out = {"band": None, "min": None, "max": None}
    m = _PRICE_IN_TITLE.search(text)
    if m:
        try:
            n = float(m.group("n").replace(",", ""))
        except ValueError:
            n = 0.0
        unit = (m.group("unit") or "").lower()
        n *= 1_000_000 if unit.startswith("m") else 1_000 if unit == "k" else 1
        if n >= 50_000:                                   # a home price, not "$500 a month"
            way = (m.group("dir") or "").lower()
            if way in ("over", "above", "more than"):
                out["min"] = n
            elif way:
                out["max"] = n
            else:
                out["min"], out["max"] = n * 0.75, n * 1.25
    if out["min"] is None and out["max"] is None:
        out["band"] = "high" if _HIGH_BAND.search(text) else "low" if _LOW_BAND.search(text) else None
    return out


def from_title(title: str, *, preset: Optional[str] = None, known_towns: Optional[list[str]] = None) -> dict:
    """What plain code can tell from the title (and the starting topic the agent tapped). No AI, no cost."""
    text = f"{title or ''} {preset or ''}".strip()
    sides = bfc.reader_sides({"topic": text})
    audiences = [name for name, rx in _AUDIENCE_WORDS if rx.search(text)]
    if "seller" in sides and "Sellers" not in audiences:
        audiences.insert(0, "Sellers")
    if "buyer" in sides and not any(a in audiences for a in ("First-time buyers", "Move-up buyers", "Luxury buyers", "Relocating")) and "Buyers" not in audiences:
        audiences.insert(0, "Buyers")
    # A town counts when its whole name is in the title - longest names first, so "Bryn Mawr" beats "Bryn".
    # Written the way a place name is written (capitalised), so "old city" or "social media" in a sentence isn't a town.
    towns, shown = [], re.sub(r"(?i)social media", " ", str(title or ""))
    for name in sorted({str(t).strip() for t in known_towns or [] if len(str(t).strip()) >= 3}, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", shown) and not any(name.lower() in t.lower() for t in towns):
            towns.append(name)
    if "seller" in sides and "buyer" not in sides:
        cta = "home_value"
    elif "buyer" in sides and "seller" not in sides:
        cta = "buying"
    else:
        cta = "how_can_i_help"
    length = "long" if _LONG.search(text) else "short" if _SHORT.search(text) else "standard"
    return {"audiences": audiences[:3], "areas": towns[:4], "cta": cta, "tone": "luxury" if "Luxury buyers" in audiences else "warm", "length": length,
            "sides": sorted(sides), "price": price_hint(title)}


# ---------------------------------------------------------------------------
# What we know about the agent
# ---------------------------------------------------------------------------

def _price_word(p: float) -> str:
    if p >= 1_000_000:
        return f"${p / 1_000_000:.1f}M".replace(".0M", "M")
    return f"${round(p / 1000):.0f}K"


def _same_town(a: str, b: str) -> bool:
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(a and b) and (a == b or a in b or b in a)


def sales_by_town(sales: list[dict], today: Optional[date] = None, days: int = 730) -> list[dict]:
    """The agent's closed sales of the last two years, grouped by town: how many, the price range, the newest."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    towns: dict[str, dict] = {}
    for s in sales or []:
        city, when = str(s.get("city") or "").strip(), str(s.get("sold_date") or "")[:10]
        try:
            price = float(s.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if not city or len(when) < 7 or when < since:
            continue
        t = towns.setdefault(city.lower(), {"town": city, "count": 0, "prices": [], "newest": ""})
        t["count"] += 1
        t["newest"] = max(t["newest"], when)
        if price > 0:
            t["prices"].append(price)
    out = []
    for t in sorted(towns.values(), key=lambda t: (-t["count"], t["town"])):
        prices = sorted(t.pop("prices"))
        t["low"], t["high"] = (prices[0], prices[-1]) if prices else (None, None)
        t["mid"] = prices[len(prices) // 2] if prices else None
        out.append(t)
    return out


async def agent_context(db: AsyncSession, user: User) -> dict:
    """What we hold about the agent that makes a suggestion fit THEM: their market and bio (profile / Agent Hub), the
    towns on their website, the homes they sold, and the articles they already wrote. Every part is optional."""
    from app.models.database import AgentMarketArea, Blog
    from app.services.ai import ai_blog_writer_service as writer
    today = date.today()
    ctx: dict = {"month": MONTHS[today.month - 1], "month_no": today.month, "year": today.year, "market": "", "about": "", "towns": [], "sales": [], "past_titles": []}
    try:
        config = await writer._hub_config(db, user.id)
        profile = writer._agent_profile(user, config)
        ctx["market"], ctx["about"] = str(profile.get("market") or "").strip(), re.sub(r"\s+", " ", str(profile.get("about") or "")).strip()
        ctx["sales"] = sales_by_town(writer._recent_sales(config, limit=300), today)
    except Exception:
        logger.info("blog suggestions: agent profile / sales lookup skipped", exc_info=True)
    try:
        ctx["towns"] = [n for (n,) in (await db.execute(
            select(MarketArea.name).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
            .where(AgentMarketArea.owner_id == user.id).order_by(AgentMarketArea.created_at))).all()]
        titles = [str(t or "").strip() for (t,) in (await db.execute(
            select(Blog.title).where(Blog.owner_id == user.id).order_by(Blog.created_at.desc()).limit(14))).all()]
        ctx["past_titles"] = [t for t in titles if t and t.lower() != "untitled article"]
    except Exception:
        logger.info("blog suggestions: towns / past articles lookup skipped", exc_info=True)
    return ctx


def context_text(ctx: Optional[dict], title_towns: Optional[list[str]] = None, skip_title: str = "") -> str:
    """The agent's context as a few short lines for the model (about 150-250 tokens)."""
    if not ctx:
        return ""
    lines = [f"- Today: {ctx.get('month')} {ctx.get('year')}."]
    if ctx.get("market"):
        lines.append(f"- Their market: {ctx['market']}.")
    if ctx.get("towns"):
        lines.append("- Towns on their website: " + ", ".join(ctx["towns"][:10]) + ".")
    sales = ctx.get("sales") or []
    # The title's town first, then the towns on their website, then the rest - so the cut never drops the one that matters.
    sales = sorted(sales, key=lambda t: (not any(_same_town(t["town"], x) for x in title_towns or []),
                                         not any(_same_town(t["town"], x) for x in ctx.get("towns") or []), -t["count"], t["town"]))
    if sales:
        lines.append("- Homes they sold in the last two years, by town: " + "; ".join(
            f"{t['town']} {t['count']}" + (f" ({_price_word(t['low'])} to {_price_word(t['high'])})" if t.get("low") and t["low"] != t["high"] else f" ({_price_word(t['low'])})" if t.get("low") else "")
            for t in sales[:7]) + ".")
    else:
        lines.append("- No closed sales of theirs on file.")
    for town in (title_towns or [])[:2]:
        hit = next((t for t in sales if _same_town(t["town"], town)), None)
        lines.append(f"- In {town}, the title's town: they sold {hit['count']} home{'s' if hit['count'] != 1 else ''} in the last two years."
                     if hit else f"- In {town}, the title's town: none of their sales on file.")
    if ctx.get("about"):
        lines.append(f"- Their bio, in their words: \"{ctx['about'][:320]}\"")
    past = [t for t in ctx.get("past_titles") or [] if t.lower() != str(skip_title or "").strip().lower()]
    if past:
        lines.append("- Articles they already wrote: " + "; ".join(f"\"{t[:80]}\"" for t in past[:8]) + ".")
    return "ABOUT THIS AGENT (to make the choices fit them - never restate any of it as a claim):\n" + "\n".join(lines)


def has_sales_in(ctx: Optional[dict], towns: Optional[list[str]]) -> Optional[str]:
    """The title's town the agent has closed sales in (its name), else None."""
    for town in towns or []:
        if any(_same_town(t["town"], town) for t in (ctx or {}).get("sales") or []):
            return town
    return None


# When the AI can't be asked (allowance spent, a slow moment): honest, generic choices for the kind of article.
_FALLBACK_POINTS = {
    "seller": ["What homes like mine are selling for right now", "How fast homes are selling", "What to fix before listing - and what to leave alone",
               "What a seller pays at settlement", "Township or borough steps to start early", "How to time the sale with my next move"],
    "buyer": ["What homes cost here right now", "What the monthly payment looks like", "How fast homes sell and how to compete",
              "What to check at the inspection", "Schools and taxes depend on the exact address", "First steps: preapproval and down payment help"],
    "": ["What homes cost here right now", "How fast homes are selling", "What daily life is like: downtown, parks and getting around",
         "Schools and taxes depend on the exact address", "What has changed this year", "What to do next"],
}
_FALLBACK_GOALS = {
    "seller": ["Know what selling will really involve and cost, and feel ready to start", "Have a clear to-do list before listing"],
    "buyer": ["Know what a home here really costs each month and what to do first", "Feel ready to make a confident offer"],
    "": ["Understand the local market well enough to decide what to do next", "Know whether this area fits what they're looking for"],
}

_TOOL = {
    "name": "suggest_brief",
    "description": "Return tappable starting points for a real estate agent's blog article brief.",
    "input_schema": {"type": "object", "properties": {
        "towns": {"type": "array", "items": {"type": "string"}, "description": "Towns or neighborhoods NAMED in the title, exactly as written. Empty if none."},
        "audiences": {"type": "array", "items": {"type": "string", "enum": AUDIENCES}, "description": "1-2 that the title is clearly written for."},
        "angles": {"type": "array", "items": {"type": "string"}, "description": "3 sharper versions of the title. Same town, same topic. Under 70 characters each."},
        "goals": {"type": "array", "items": {"type": "string"}, "description": "3 options for what the READER will know, decide or feel ready to do after reading - never an instruction to the writer. One plain sentence each, under 20 words, starting with Know / Decide / Feel / Have (e.g. 'Decide whether to list now or wait for spring')."},
        "key_points": {"type": "array", "items": {"type": "string"}, "description": "7 things a reader of THIS article would want covered. Under 12 words each. Topics or questions, never facts or numbers."},
        "takes": {"type": "array", "items": {"type": "string"}, "description": "3 short opinions a local agent might hold about THIS topic, each starting 'My take:'. Opinions only."},
        "tone": {"type": "string", "enum": TONES}, "length": {"type": "string", "enum": LENGTHS}, "cta": {"type": "string", "enum": CTAS},
    }, "required": ["towns", "audiences", "angles", "goals", "key_points", "takes", "tone", "length", "cta"]},
}

_SHARED_RULES = """- Fair housing: never mention or imply family status, age, religion, race, national origin, disability, or "safe" / "family-friendly" / "perfect for".
- Plain language a non-agent understands. No hype words (nestled, hidden gem, dream home, vibrant).
- ABOUT THIS AGENT (when given) tells you who is writing: the towns they work, what they sell and at what prices, how they describe themselves, what they have already written, and today's month. Use it to choose WHAT to offer so it fits them and this moment. Never restate it as a claim: no "I have sold N homes", no years in business, no awards, no prices of theirs."""

_SYSTEM = """You help a licensed US real estate agent fill in the brief for a blog article they are about to have written. You are given the article's title and what we know about the agent. Return choices they can TAP instead of type. Call suggest_brief once.

Rules:
- The TITLE comes first. Everything must fit its town, its reader (buyer, seller, owner, investor, someone relocating) and its question. If the title names a town, use that town - never a different one from their list.
- key_points are topics or questions a reader would want answered (prices and how fast homes sell, costs, steps, what daily life is like, timing). Never state a fact, number, price, rate, date or statistic - the article's research fills those in. For an article about selling a home, one point is the township or borough paperwork to start early and one is what the seller pays at settlement; for buying, one is the monthly cost and one is the first steps.
- If the agent sold homes in the title's town, make one key point about what their own recent sales there show ("What my recent Wayne sales show about pricing"). If they have none there, never offer a point or an opinion about their own sales.
- If the season matters to the title's question, let today's month shape the points and the sharper titles.
- Never refer to the reader's children, school schedules, age or household.
- goals say what the reader should walk away knowing or doing, in plain words.
- takes are opinions the agent may agree with and will edit - practical judgment about this topic ("My take: start the paperwork before the photos"), in the spirit of how their bio says they work. Never a client story, a track record, years in business, a number, or anything about who should live somewhere.
- angles keep the same town and topic; make them clearer and more specific, not clickbait. Never a price or a statistic. Never the title of an article they already wrote.
""" + _SHARED_RULES

_TITLES_TOOL = {
    "name": "suggest_titles",
    "description": "Return blog article titles this real estate agent could write next.",
    "input_schema": {"type": "object", "properties": {"titles": {"type": "array", "description": "6 titles.", "items": {"type": "object", "properties": {
        "title": {"type": "string", "description": "Under 75 characters. Specific: a town of theirs where it fits."},
        "basis": {"type": "string", "enum": ["your_sales", "your_town", "timely", "fresh"],
                  "description": "your_sales = about a town where they sold homes, drawing on those sales; your_town = about a town they work; timely = fits this month; fresh = a useful topic they have not written."},
    }, "required": ["title", "basis"]}}}, "required": ["titles"]},
}

_TITLES_SYSTEM = """You suggest blog article titles for a licensed US real estate agent, from what we know about them. They will tap one and an article gets written around it. Call suggest_titles once with 6 titles.

Rules:
- Each title is a question or a promise a local buyer, seller or homeowner would search for, and names ONE specific place where that fits - only a town from their website's list or one where they sold homes.
- If they tapped a starting topic, all six fit that topic. Otherwise mix them: at least one for sellers, one for buyers, one about living in a town, and one that fits this month and season.
- Put their strongest towns first: where they sold the most homes. A title that leans on their own sales ("What my recent Wayne sales say about pricing") is only for a town where they sold homes.
- Fit the price range they sell in and how their bio says they work (first homes, luxury, relocation, downsizing) without saying so in the title.
- Never repeat or closely rework an article they already wrote.
- Never a price, a statistic, or a number other than the year. No clickbait.
""" + _SHARED_RULES

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 6 * 3600
_FAIR = re.compile(r"\b(?:famil(?:y|ies)|kids?|children|school (?:year|schedule|calendar)s?|safe(?:st|r)?|crime|church|synagogue|mosque|retirees?|empty[- ]nesters?|young professionals?|perfect for|ideal for)\b", re.I)
_HAS_NUMBER = re.compile(r"\d")
_OWN_SALES = re.compile(r"\bmy (?:own |recent |latest |last )*(?:[A-Z][\w'.-]* ){0,3}(?:sales|closings|listings|sold homes)\b|\bI(?:'ve| have)? sold\b|\bhomes I(?:'ve| have)? sold\b", re.I)
# In a title the only number allowed is a year.
_TITLE_NUMBER = re.compile(r"[$%]|\b(?!20\d\d\b)\d{3,}\b")


def _clean(items, limit: int, max_len: int, *, numbers_ok: bool = True, own_sales_ok: bool = True) -> list[str]:
    out = []
    for raw in items if isinstance(items, list) else []:
        s = re.sub(r"\s+", " ", str(raw or "")).strip(" -•")
        if not s or len(s) > max_len or _FAIR.search(s) or (not numbers_ok and _HAS_NUMBER.search(s)) or (not own_sales_ok and _OWN_SALES.search(s)):
            continue
        if s.lower() not in {x.lower() for x in out}:
            out.append(s)
    return out[:limit]


def merge(base: dict, ai: Optional[dict], title: str, *, context: Optional[dict] = None) -> dict:
    """The answer the screen gets: plain-code picks first, the AI's choices added and cleaned. Without the AI,
    a built-in list for that kind of article. With the agent's context: a point about their own sales is offered
    only when they have sales in the title's town - and then always, whatever the model returned."""
    side = "seller" if base["sides"] == ["seller"] else "buyer" if base["sides"] == ["buyer"] else ""
    ai = ai if isinstance(ai, dict) else {}
    audiences = base["audiences"] or [a for a in ai.get("audiences") or [] if a in AUDIENCES][:2]
    areas = list(base["areas"])
    for t in _clean(ai.get("towns"), 4, 40):
        # only a town that really is in the title - the model must not hand back one from the agent's list
        if t.lower() in (title or "").lower() and not any(t.lower() == a.lower() or t.lower() in a.lower() or a.lower() in t.lower() for a in areas):
            areas.append(t)
    sales_town = has_sales_in(context, areas) if context is not None else None
    own_ok = context is None or bool(sales_town)
    past = {t.lower() for t in (context or {}).get("past_titles") or []}
    points = _clean(ai.get("key_points"), 8, 110, numbers_ok=False, own_sales_ok=own_ok) or list(_FALLBACK_POINTS[side])
    if sales_town and not any(_OWN_SALES.search(p) for p in points):
        points = points[:7] + [f"What my recent {sales_town} sales show about pricing"]
    return {
        "audiences": audiences[:3], "areas": areas[:4],
        "angles": [a for a in _clean(ai.get("angles"), 4, 90) if not _TITLE_NUMBER.search(a) and a.lower() not in past][:3],
        "goals": _clean(ai.get("goals"), 3, 160, numbers_ok=False) or _FALLBACK_GOALS[side],
        "key_points": points,
        "takes": [t for t in _clean(ai.get("takes"), 3, 220, numbers_ok=False, own_sales_ok=own_ok) if t.lower().startswith("my take")],
        "tone": ai.get("tone") if ai.get("tone") in TONES and base["tone"] == "warm" else base["tone"],
        "length": base["length"],
        "cta": base["cta"] if base["cta"] != "how_can_i_help" else (ai.get("cta") if ai.get("cta") in CTAS else "how_can_i_help"),
        "price": base.get("price") or {"band": None, "min": None, "max": None},
        "sales_town": sales_town,
        "ai": bool(ai),
    }


# Owner 11/14: "make sure it's very little ... I don't want people to get hit with a lot of usage from it. And if they
# are about to run out ... 97% or greater" - the smart choices are a nicety, so they are the first thing to stop:
#   - at 97% of the month's AI usage they rest (the last 3% is kept for the things the agent presses on purpose);
#   - one agent gets a day's worth of asks (each is a fraction of a cent; the cap is a ceiling of about two cents a
#     day, not a price), then the built-in choices until tomorrow.
# Either way the screen still works - it shows the built-in choices and one quiet line saying why.
PAUSE_AT_PERCENT = 97
DAILY_ASKS = 40
LABELS = ("blog_suggest", "blog_title_ideas")


async def usage_guard(db: AsyncSession, user: User, config: Optional[dict] = None) -> Optional[dict]:
    """Why the smart choices are resting for this agent right now ({"reason", "message"}), or None."""
    from app.services.ai.ai_rate_limit_service import (
        _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS, group_pool, monthly_limit_usd, resets_text, total_monthly_ai_cost, usage_period)
    config = config if config is not None else await ai_config_service.get_config(db)
    share = float(config.get("blog_suggest_pause_percent", PAUSE_AT_PERCENT)) / 100.0
    if user.id not in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        limit = await monthly_limit_usd(db, user.id, config, user.plan_tier)
        resets = None
        if limit and await total_monthly_ai_cost(db, user.id) >= float(limit) * share:
            resets = (await usage_period(db, user.id))[1]
        else:
            pool = await group_pool(db, user.id)
            if pool and pool["used"] >= pool["total"] * share:
                resets = pool["resets"]
        if resets is not None:
            return {"reason": "nearly_out",
                    "message": f"This month's AI usage is almost used up, so smart suggestions are resting to save what's left. "
                               f"These are our standard choices. {resets_text(resets)}"}
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    asked = (await db.execute(select(func.count(AIUsageEvent.id)).where(
        AIUsageEvent.owner_id == user.id, AIUsageEvent.event_type == EVENT_TYPE, AIUsageEvent.created_at >= day,
        AIUsageEvent.tool_names.has_any(pg_array(list(LABELS)))))).scalar() or 0
    if asked >= int(config.get("blog_suggest_daily_asks", DAILY_ASKS)):
        return {"reason": "daily", "message": "That's a full day of smart suggestions - they're back tomorrow. These are our standard choices."}
    return None


async def _guard(db: AsyncSession, user: User) -> Optional[dict]:
    # Inside a savepoint: a failed look-up here must not spoil the request's session (a plain rollback would also
    # wipe the signed-in user off it - found on the first live run, 11/14, when every suggestion fell back).
    try:
        async with db.begin_nested():
            return await usage_guard(db, user)
    except Exception:  # noqa: BLE001 - the guard must never break the screen; the allowance itself is still enforced in _ask
        logger.warning("blog suggestions: usage guard failed", exc_info=True)
        return None


async def _ask(db: AsyncSession, user: User, *, system: str, tool: dict, msg: str, max_tokens: int, label: str) -> tuple[Optional[dict], float]:
    """ONE small metered call on the copywriter's model. (None, 0.0) when the allowance is spent or the call fails -
    the caller then serves its built-in choices; the screen never sees an error."""
    config = await ai_config_service.get_config(db)
    model = config["copywrite_model"]
    try:
        reservation = await reserve_copywrite(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded:
        return None, 0.0
    try:
        from app.services import cma_claude  # one request shape for old and current Claude models
        from app.services.ai.ai_blog_writer_service import _client
        resp = await cma_claude.create(_client(), model=model, max_tokens=max_tokens, system=system, tools=[tool],
                                       tool_choice={"type": "tool", "name": tool["name"]}, messages=[{"role": "user", "content": msg}])
        ai = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == tool["name"] and isinstance(b.input, dict)), None)
        cost = float(ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens) or 0.0)
        await record_usage_event(db, user.id, None, model, resp.usage.input_tokens, resp.usage.output_tokens, cost, 1, False, None,
                                 tool_names=[label], event_type=EVENT_TYPE)
        await reconcile_budget(db, reservation, cost)
        return ai, cost
    except Exception:
        logger.warning("blog suggestions (%s): the AI call failed - built-in choices returned", label, exc_info=True)
        try:
            await reconcile_budget(db, reservation, 0.0)
        except Exception:
            logger.warning("blog suggestions: reservation refund failed", exc_info=True)
        return None, 0.0


def _remember(key: str, value: dict) -> None:
    _CACHE[key] = (time.monotonic(), value)
    if len(_CACHE) > 500:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
            _CACHE.pop(k, None)


def _recall(key: str, seconds: int = _CACHE_SECONDS) -> Optional[dict]:
    hit = _CACHE.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < seconds else None


async def suggest(db: AsyncSession, user: User, *, title: str, preset: Optional[str] = None, served_areas: Optional[list[str]] = None,
                  market: Optional[str] = None, context: Optional[dict] = None) -> dict:
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:200]
    if context is not None:
        served_areas = served_areas or context.get("towns") or []
        market = market or context.get("market")
    sale_towns = [t["town"] for t in (context or {}).get("sales") or []]
    names = [n for (n,) in (await db.execute(select(MarketArea.name).where(MarketArea.enrichment_status.in_(("ENRICHED", "PARTIAL"))))).all()]
    base = from_title(title, preset=preset, known_towns=list(served_areas or []) + sale_towns + names)
    if len(title) < 8:
        return {**merge(base, None, title, context=context), "cost_usd": 0.0}
    about = context_text(context, base["areas"], skip_title=title)
    key = hashlib.sha1(f"{user.id}|{title.lower()}|{preset or ''}|{about}".encode("utf-8")).hexdigest()
    hit = _recall(key)
    if hit:
        return {**hit, "cost_usd": 0.0, "cached": True}
    paused = await _guard(db, user)
    if paused:
        return {**merge(base, None, title, context=context), "cost_usd": 0.0, "paused": paused}
    msg = (f"Article title: {title}\n" + (f"Starting topic they tapped: {preset}\n" if preset else "")
           + (about + "\n" if about else
              (f"Their market: {market}\n" if market else "")
              + (f"Towns they work in (context only - use one ONLY if the title names it): {', '.join((served_areas or [])[:10])}\n" if served_areas else "")))
    ai, cost = await _ask(db, user, system=_SYSTEM, tool=_TOOL, msg=msg, max_tokens=1800, label="blog_suggest")
    out = merge(base, ai, title, context=context)
    if ai:
        _remember(key, out)
    return {**out, "cost_usd": round(cost, 6)}


# ---------------------------------------------------------------------------
# Title ideas - before the agent has typed anything
# ---------------------------------------------------------------------------

def _season_title(month: int, town: str) -> str:
    if month in (11, 12, 1):
        return f"Should you sell your {town} home before or after the holidays?"
    if month in (2, 3, 4):
        return f"Getting your {town} home ready for the spring market"
    if month in (5, 6, 7):
        return f"Selling in {town} this summer: what to expect"
    return f"Is fall a good time to sell your {town} home?"


def code_titles(ctx: Optional[dict]) -> list[dict]:
    """Title ideas from plain code - what the screen shows when the AI can't be asked. Built from the agent's own
    towns (where they sold the most first), the month, and what they have not written yet."""
    ctx = ctx or {}
    site = list(ctx.get("towns") or [])
    on_site = lambda t: any(_same_town(t, s) for s in site)  # noqa: E731
    sales = ctx.get("sales") or []
    # Their strongest towns first: where they sold homes AND that they put on their website, then the rest of the
    # website's towns, then other towns they sold in. Without a website list, the towns they sold in.
    sold = [t["town"] for t in sorted(sales, key=lambda t: (not on_site(t["town"]), -t["count"], t["town"]))]
    towns = [t for t in sold if on_site(t) or not site] + [t for t in site if not any(_same_town(t, s) for s in sold)] + [t for t in sold if site and not on_site(t)]
    if not towns:
        return []
    pick = lambda i: towns[i % len(towns)]  # noqa: E731
    month, year = ctx.get("month") or MONTHS[date.today().month - 1], ctx.get("year") or date.today().year
    prices = sorted(t["mid"] for t in sales if t.get("mid") for _ in range(t["count"]))
    upper = bool(prices) and prices[len(prices) // 2] >= 1_000_000          # an agent who sells million-dollar homes is not offered "your first home"
    ideas = []
    if sold:
        ideas.append({"title": f"What my recent {sold[0]} sales say about pricing right now", "basis": "your_sales"})
    ideas += [
        {"title": _season_title(int(ctx.get("month_no") or date.today().month), pick(0)), "basis": "timely"},
        {"title": f"{pick(1)} market update: {month} {year}", "basis": "timely"},
        {"title": f"Buying a home in {pick(2)}: what to check before you offer" if upper else f"Buying your first home in {pick(2)}: where to start", "basis": "your_town"},
        {"title": f"Living in {pick(3)}: a local guide", "basis": "your_town"},
        {"title": f"Moving to {pick(4)}: what to know first", "basis": "fresh"},
    ]
    past = {t.lower() for t in ctx.get("past_titles") or []}
    return [i for i in ideas if i["title"].lower() not in past][:6]


def clean_titles(items, ctx: Optional[dict]) -> list[dict]:
    past = {t.lower() for t in (ctx or {}).get("past_titles") or []}
    sold = [t["town"] for t in (ctx or {}).get("sales") or []]
    out: list[dict] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip(" -•\"")
        if not (12 <= len(title) <= 95) or _FAIR.search(title) or _TITLE_NUMBER.search(title) or title.lower() in past:
            continue
        if any(title.lower() == o["title"].lower() for o in out):
            continue
        leans_on_sales = bool(_OWN_SALES.search(title))
        in_sold_town = any(re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", title, re.I) for t in sold)
        if leans_on_sales and not in_sold_town:
            continue                                   # "my recent Radnor sales" from an agent with no Radnor sales
        basis = raw.get("basis") if raw.get("basis") in ("your_sales", "your_town", "timely", "fresh") else "fresh"
        if basis == "your_sales" and not in_sold_town:
            basis = "your_town"
        out.append({"title": title, "basis": basis})
    return out[:6]


async def title_ideas(db: AsyncSession, user: User, *, preset: Optional[str] = None, template_title: Optional[str] = None,
                      context: Optional[dict] = None) -> dict:
    """Six titles for the "Give it a title" screen, built from the agent's context. One small call per agent per day
    (per starting topic); plain-code ideas when the AI can't be asked or the agent has no towns on file yet."""
    ctx = context if context is not None else await agent_context(db, user)
    fallback = code_titles(ctx)
    about = context_text(ctx)
    if not (ctx.get("towns") or ctx.get("sales") or ctx.get("market")):
        return {"titles": fallback, "ai": False, "cost_usd": 0.0}
    key = hashlib.sha1(f"titles|{user.id}|{preset or ''}|{template_title or ''}|{date.today().isoformat()}|{about}".encode("utf-8")).hexdigest()
    hit = _recall(key, 24 * 3600)
    if hit:
        return {**hit, "cost_usd": 0.0, "cached": True}
    paused = await _guard(db, user)
    if paused:
        return {"titles": fallback, "ai": False, "cost_usd": 0.0, "paused": paused}
    msg = ((f"Starting topic they tapped: {preset}" + (f" - \"{template_title}\"" if template_title else "") + "\n" if preset or template_title else "")
           + about + "\n")
    ai, cost = await _ask(db, user, system=_TITLES_SYSTEM, tool=_TITLES_TOOL, msg=msg, max_tokens=900, label="blog_title_ideas")
    titles = clean_titles((ai or {}).get("titles"), ctx)
    out = {"titles": titles if len(titles) >= 3 else fallback, "ai": len(titles) >= 3}
    if out["ai"]:
        _remember(key, out)
    return {**out, "cost_usd": round(cost, 6)}
