"""AI Blog Writer - Studio -> Blogs "Write it with AI".

A guided interview (topic, goal, audience, focus areas, key points, the agent's own take, facts to
cite, tone, length, CTA) becomes ONE structured Claude call that returns a complete, publishable
article: title/subtitle, SEO title + meta description + focus keyword, category + tags, an
"At a glance" summary, H2 sections with lists/pull-quotes, and a closing CTA - converted straight
into the blog editor's own block format and saved as a DRAFT (never published by the AI).

Grounding rules (enforced in the prompt AND checked after):
  - Every number must come from the agent's brief or real data we hold (their Agent Hub recent
    sales, their own active listings, listings they attached). After generation, `_number_audit`
    extracts every figure in the article and returns any that don't appear in that source material
    as `numbers_to_verify`, so the agent sees exactly what to double-check before publishing.
  - Fair Housing / MLS advertising rules are part of the system prompt (same rule set as
    ai_copywrite_service); the editor also offers the existing /ai/compliance-check on the result.

Metering: same reserve -> call -> record_usage_event -> reconcile path as the copywriter / CMA, so
every call counts against the agent's shared Monthly AI Usage allowance
(agent_monthly_ai_budget_usd_by_tier). event_type="blog_writer". `precheck` lets the UI block the
interview up-front with the upgrade/usage message instead of failing after the agent typed it all.
"""
from __future__ import annotations

import html
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentHub, AgentMarketArea, MarketArea, User
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS, monthly_ai_budget_snapshot,
    reconcile_budget, record_usage_event, reserve_blog_writer,
)

logger = logging.getLogger(__name__)

EVENT_TYPE = "blog_writer"

BLOG_CATEGORIES = [
    "Buyers", "Sellers", "Market Updates", "Neighborhoods", "Homeownership",
    "Luxury", "Investments", "Open Houses", "Local Guides", "New Construction", "Real Estate News",
]

_TONES = {
    "warm": "warm, personal and conversational - first person, like a trusted local advisor talking to a neighbor",
    "professional": "polished, confident and professional - first person, clear and authoritative",
    "luxury": "refined, understated luxury - elegant, editorial, never flashy",
    "energetic": "upbeat and energetic - lively, motivating, still credible",
    "straight": "straight-talking and practical - plain language, no fluff, direct advice",
}

_LENGTHS = {
    "short": ("a quick read", "about 550-750 words", 3),
    "standard": ("a standard article", "about 900-1200 words", 4),
    "long": ("an in-depth guide", "about 1500-1900 words", 6),
}

_CTAS = {
    # The agent's premade forms - the article's closing button opens that form (BLOG_CTA_FORMS on
    # the frontend lists the same keys).
    "how_can_i_help": "invite the reader to tell me whether they're buying or selling so I can help - a no-pressure first conversation",
    "home_value": "invite the reader to request a free, personalized home value estimate from me",
    "showing": "invite the reader to schedule a showing of a home with me",
    "buying": "invite buyers to tell me what they're looking for so I can start their home search",
    "selling": "invite owners thinking about selling to tell me about their home so we can plan the sale",
    "make_me_sell": "invite owners who would sell for the right price to tell me their number - no listing, no pressure",
    "question": "invite the reader to send me any question - I answer personally",
    # Older briefs.
    "consultation": "invite the reader to book a no-pressure consultation with me",
    "tour": "invite the reader to reach out to schedule a tour / see homes with me",
    "newsletter": "invite the reader to reach out to get these local updates from me first",
}

_SYSTEM = """You are the ghostwriter for a licensed US real estate agent's blog. You write in the agent's own first-person voice, for their own website, to build local search visibility and trust with real buyers and sellers. Your articles are the best in the industry: specific, genuinely useful, locally grounded, beautifully structured, and never generic filler.

HARD RULES - never break these:
1. NO FABRICATED FACTS. Every number (prices, percentages, days on market, inventory, rates, dates, counts, rankings, school ratings, tax figures, HOA fees) must come from the AGENT BRIEF or REAL DATA sections of the request. If a point would benefit from a statistic you were not given, make the point qualitatively or tell the reader what to ask about - never invent or estimate a figure. Do not invent quotes, clients, stories, awards, years of experience, how long the agent has worked an area, sales volume, credentials, or the age/history of homes and neighborhoods. Mortgage rates, market predictions and legal/tax specifics: speak generally and suggest the reader confirm with the right professional.
2. PLACES. You may mention the towns/areas the agent listed and widely known, long-standing geography you are highly confident about (town names, major roads, rail lines, large public parks). Never invent or name specific businesses, restaurants, events, developments or schools unless the agent named them.
3. FAIR HOUSING / MLS ADVERTISING. Never reference or imply race, color, religion, sex, familial status, national origin, disability, sexual orientation, or age - of an area, a home, or who would or should live there. Do not use: "family"/"families"/"family-friendly", "perfect for", "ideal for [type of person]", "safe"/"safe neighborhood", "walking distance"/"walk to", "exclusive", "integrated", "traditional neighborhood", "bachelor", "empty nesters", "young professionals", "retirees", "his and hers", "master bedroom" (say "primary bedroom"), "crime", or any phrase steering a type of person toward or away from an area. Describe places, homes, amenities and the process - not the people who belong there. Schools may only be named as neutral, factual proximity if the agent named them.
4. The agent's personal take / local story, when given, is the heart of the article - weave it in faithfully in their voice (you may lightly polish wording, never add facts to it). A pull-quote should be a faithful line from it.
5. No hype words ("dream home", "stunning", "nestled", "boasts", "don't miss out"), no clickbait, no emojis, no hashtags. Write like a sharp local expert, not an ad.

CRAFT:
- Title: specific and search-friendly (name the place/topic), 45-70 characters, no colon-stacked clickbait.
- Open with a hook that speaks to the reader's real situation in 2-4 sentences, then deliver.
- H2 section headings that read like a table of contents a reader would scan. Mix short paragraphs (2-4 sentences) with numbered steps or bullet lists where they genuinely help; use at most one pull-quote.
- Be concrete and actionable: what to do, what to ask, what to watch for, in what order.
- SEO: use the focus keyword naturally in the title, the first paragraph, and one heading. seo_title <= 60 characters; meta_description 140-158 characters, written to earn the click.
- Close by summarizing the one thing to remember, then the call to action, in the agent's voice.
- You may use **double asterisks** for occasional bold emphasis inside paragraphs (sparingly).

Deliver the article ONLY by calling the submit_blog_article tool."""

_ARTICLE_TOOL = {
    "name": "submit_blog_article",
    "description": "Submit the finished blog article.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subtitle": {"type": "string", "description": "One-sentence dek under the title (90-160 characters)."},
            "seo_title": {"type": "string", "description": "<= 60 characters."},
            "meta_description": {"type": "string", "description": "140-158 characters."},
            "focus_keyword": {"type": "string"},
            "category": {"type": "string", "enum": BLOG_CATEGORIES},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "3-6 short tags."},
            "at_a_glance": {"type": "array", "items": {"type": "string"}, "description": "3-4 one-line takeaways shown in a summary box under the intro."},
            "intro": {"type": "array", "items": {"type": "string"}, "description": "1-3 intro paragraphs."},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "blocks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["paragraph", "subheading", "bulleted_list", "numbered_list", "quote", "callout"]},
                                    "text": {"type": "string", "description": "For paragraph/subheading/quote/callout."},
                                    "items": {"type": "array", "items": {"type": "string"}, "description": "For bulleted_list/numbered_list."},
                                },
                                "required": ["type"],
                            },
                        },
                    },
                    "required": ["heading", "blocks"],
                },
            },
            "closing": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "paragraphs": {"type": "array", "items": {"type": "string"}},
                    "button_label": {"type": "string", "description": "2-5 words for the call-to-action button."},
                },
                "required": ["heading", "paragraphs", "button_label"],
            },
            "cover_image_idea": {"type": "string", "description": "One sentence describing an ideal cover photo (a place or home scene, no people)."},
            "facts_used": {
                "type": "array",
                "description": "Every specific number or verifiable fact used, with where it came from.",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string"},
                        "source": {"type": "string", "enum": ["agent_brief", "recent_sales", "listing", "agent_profile"]},
                    },
                    "required": ["fact", "source"],
                },
            },
        },
        "required": ["title", "subtitle", "seo_title", "meta_description", "focus_keyword", "category", "tags",
                     "at_a_glance", "intro", "sections", "closing", "cover_image_idea", "facts_used"],
    },
}

_CTA_LABELS = {
    "how_can_i_help": "Ready to Buy or Sell?",
    "home_value": "What's My Home Worth?",
    "showing": "Schedule a Showing",
    "buying": "I'm Buying",
    "selling": "I'm Selling",
    "make_me_sell": "Make Me Sell",
    "question": "Ask Me a Question",
    "consultation": "Book a Consultation",
    "tour": "Schedule a Tour",
    "newsletter": "Get Local Updates",
}

# Which premade form the closing button opens, per brief CTA ("question" = the in-article question box).
_CTA_FORMS = {
    "how_can_i_help": "how_can_i_help", "home_value": "home_value", "showing": "showing", "buying": "buying",
    "selling": "selling", "make_me_sell": "make_me_sell", "question": "question",
    "consultation": "how_can_i_help", "tour": "showing",
}


def cta_block_args(brief: dict) -> tuple[str, Optional[str]]:
    """(button label, form type) for the article's closing call to action."""
    key = brief.get("cta") or "how_can_i_help"
    return _CTA_LABELS.get(key, "Contact Me"), _CTA_FORMS.get(key)

_REFINE_SYSTEM = _SYSTEM.split("Deliver the article ONLY")[0] + (
    "You are revising ONE section of an existing article. Keep the article's voice. Do not introduce any number, "
    "statistic, place or claim that is not already in the article or the agent's instruction. Return the revised "
    "section ONLY by calling the submit_section tool."
)

_REFINE_ACTIONS = {
    "rewrite": "Rewrite this section so it reads fresher and more engaging, same length and same facts.",
    "expand": "Expand this section with more practical depth - concrete steps, what to ask, what to watch for - roughly 1.5-2x as long, no new facts or numbers.",
    "shorten": "Tighten this section to about half its length, keeping the most useful points.",
    "more_local": "Make this section feel more locally grounded in the areas the article is about, using only places/facts already in the article.",
    "more_personal": "Make this section warmer and more personal in the agent's first-person voice, without inventing stories or experience.",
    "simplify": "Rewrite this section in plainer language a first-time buyer or seller would understand, and add a short list if it helps.",
    "custom": "Revise this section following the agent's instruction.",
}

_SECTION_TOOL = {
    "name": "submit_section",
    "description": "Submit the revised section.",
    "input_schema": {
        "type": "object",
        "properties": {
            "heading": {"type": "string", "description": "The section heading (keep it unless asked to change it)."},
            "blocks": _ARTICLE_TOOL["input_schema"]["properties"]["sections"]["items"]["properties"]["blocks"],
        },
        "required": ["heading", "blocks"],
    },
}


def _client():
    from app.services.ai.ai_orchestrator_service import _get_client
    return _get_client()


def _require_key():
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI writing isn't configured yet (missing ANTHROPIC_API_KEY).")


# ---------------------------------------------------------------------------
# Context (profile + real data the agent can pull into the interview)
# ---------------------------------------------------------------------------

def _money(value) -> str:
    try:
        return f"${int(float(value)):,}"
    except (TypeError, ValueError):
        return ""


async def _hub_config(db: AsyncSession, owner_id: str) -> dict:
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))).scalar_one_or_none()
    return (hub.config or {}) if hub else {}


def _agent_profile(user: User, config: dict) -> dict:
    profile = config.get("profile") or {}
    return {
        "name": profile.get("name") or f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "title": profile.get("title") or user.professional_title or "",
        "brokerage": profile.get("brokerage") or user.brokerage or "",
        "market": profile.get("market") or user.market or "",
        "about": (profile.get("about") or "")[:600],
    }


def _recent_sales(config: dict, limit: int = 8) -> list[dict]:
    out = []
    for sale in config.get("recentSales") or []:
        if not isinstance(sale, dict) or sale.get("visible") is False:
            continue
        status_text = str(sale.get("status") or "").replace("-BRIGHT", "").replace("_", " ").strip().lower()
        if not ("sold" in status_text or "closed" in status_text):
            continue
        out.append({
            "id": str(sale.get("id") or sale.get("address") or ""),
            "address": (sale.get("address") or "").title(),
            "city": (sale.get("city") or "").title(),
            "state": sale.get("state") or "",
            "price": sale.get("price"),
            "beds": sale.get("beds"),
            "baths": sale.get("baths"),
            "sqft": sale.get("sqft"),
            "sold_date": sale.get("sold_date") or sale.get("soldDate") or "",
            "image_url": sale.get("image") or sale.get("image_url") or None,
        })
    # Newest first, so the interview's "recent" list (and its town filter) starts at the latest sale.
    out.sort(key=lambda s: str(s.get("sold_date") or ""), reverse=True)
    return out[:limit]


def _hub_videos(config: dict) -> list[dict]:
    out = []
    for key, kind in (("videoLinks", "video"), ("houseTourLinks", "tour")):
        for item in config.get(key) or []:
            link = (item.get("link") or "").strip()
            if link:
                out.append({"id": str(item.get("id") or link), "title": item.get("title") or "", "url": link, "image": item.get("image"), "kind": kind})
    return out[:40]


MAX_SPOTLIGHT_TOWNS = 5


async def town_card_for_area(db: AsyncSession, area: MarketArea) -> dict:
    """A Town Spotlight card for ANY researched town - not just the agent's IDX served areas
    (user, 10/17: a non-IDX agent can spotlight a town too). The research is shared: once one
    agent's article researched Villanova, every later article gets it from the database for free."""
    from app.services.website.content_adapters import (
        _area_hover_facts, _public_school_districts, get_area_live_market_stats, reconcile_public_schools, resolve_photo_urls,
    )
    from app.services.areas import geography
    scope = geography.scope_for_market_area(area)
    facts = area.facts or {}
    stats = await get_area_live_market_stats(db, scope, facts.get("market_stats"))
    checked = await reconcile_public_schools(db, area, scope) if facts.get("public_schools") else None
    items = ((resolve_photo_urls(facts.get("photos")) or {}).get("items") or [])
    photos = [{"url": it["url"], "credit": it.get("attribution") or it.get("author") or "Google Maps"} for it in items[:4] if it.get("url")]
    return {
        "slug": area.slug, "name": area.name, "state": area.state, "county": area.county,
        "photo": photos[0]["url"] if photos else None, "photos": photos,
        "summary": _objective_summary((area.narrative or {}).get("summary")),
        "median_price": stats.get("median_price"), "active_count": stats.get("active_count"),
        "min_price": stats.get("min_price"), "max_price": stats.get("max_price"), "price_range_basis": stats.get("price_range_basis"),
        "school_districts": _public_school_districts({"public_schools": checked} if checked else facts, stats.get("school_districts") or []),
        "nearby": _area_hover_facts(facts, stats),
        "as_of": datetime.now(timezone.utc).strftime("%B %Y"),
    }


async def _landmark_photos(area: MarketArea) -> dict:
    """A real photo of something that matters in the town (its landmarks, main street, park) when
    the town search itself came back without photos - Google Places, stored as photo NAMES (the
    key never reaches a page; public_site_routes proxies them)."""
    import os
    import httpx
    from app.services.areas.area_enrichment_service import PLACES_TEXT_URL
    key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not key:
        return {}
    # "historic sites and landmarks" finds the places a town is known for (gardens, historic
    # houses, parks); "attractions"/"famous places" mostly returned restaurants and malls.
    body: dict = {"textQuery": f"historic sites and landmarks in {area.name}, {area.state}", "maxResultCount": 12}
    if area.centroid_lat is not None and area.centroid_lng is not None:
        # Inside the town (about 4 km each way) - a bias alone let Valley Forge in as "Radnor".
        lat, lng = area.centroid_lat, area.centroid_lng
        body["locationRestriction"] = {"rectangle": {"low": {"latitude": lat - 0.036, "longitude": lng - 0.047},
                                                     "high": {"latitude": lat + 0.036, "longitude": lng + 0.047}}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(PLACES_TEXT_URL, json=body, headers={
                "Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.displayName,places.photos,places.userRatingCount,places.primaryType",
            })
            r.raise_for_status()
            places = r.json().get("places") or []
    except Exception as exc:  # noqa: BLE001
        logger.info("landmark photo lookup failed", extra={"area": area.slug, "error": str(exc) or repr(exc)})
        return {}
    # The places people actually know (most Google reviews) first, and a wide photo of each.
    skip = ("restaurant", "cafe", "bar", "store", "shopping", "mall", "playground", "gym", "bank", "gas_station", "diner", "pizza", "bakery")
    places = [p for p in places if not any(k in (p.get("primaryType") or "") for k in skip)]
    places.sort(key=lambda p: -(p.get("userRatingCount") or 0))
    items = []
    for place in places:
        shots = [ph for ph in (place.get("photos") or []) if ph.get("name")]
        wide = [ph for ph in shots if (ph.get("widthPx") or 0) >= (ph.get("heightPx") or 0) * 1.2]
        for ph in (wide or shots)[:1]:
            if ph.get("name"):
                items.append({"photo_name": ph["name"], "source": "google_places_landmark",
                              "attribution": ", ".join(a.get("displayName", "") for a in (ph.get("authorAttributions") or []) if a.get("displayName")) or "Google",
                              "caption": (place.get("displayName") or {}).get("text")})
        if len(items) >= 4:
            break
    return {"items": items, "source": "google_places_landmark", "fetched_at": datetime.now(timezone.utc).isoformat()} if items else {}


async def _ai_town_photo(db: AsyncSession, user: User, area: MarketArea) -> dict:
    """Last resort only (no town photo, no landmark photo): one AI picture of the kind of street
    the town is known for, saved on the shared town record so it's made once, ever. Recorded
    against the agent whose article needed it (a few cents); any failure just means no photo."""
    from app.services import s3_service
    from app.services.ai.ai_rate_limit_service import record_usage_event
    from app.services.studio_media.providers.registry import image_provider
    from app.services.studio_media_service import _image_cost
    from app.services.blog_cover_service import _wide
    config = await ai_config_service.get_config(db)
    try:
        provider = image_provider(config["studio_media_image_provider"], config["studio_media_image_model"])
        result = await provider.generate(
            f"Photorealistic photograph of a typical residential street in {area.name}, {area.state}, USA - the homes, "
            "trees and setting characteristic of the area, in soft daylight. No text, no signs with words, no logos, no people.",
            size="1536x1024", quality="medium",
        )
        url = s3_service.upload_bytes(_wide(result.image_bytes), "town-photos", ".jpg", "image/jpeg")
        await record_usage_event(db, user.id, None, provider.model, None, None, _image_cost(result, config, 0.04), 0, False, None, event_type="studio_media_image")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai town photo failed", extra={"area": area.slug, "error": str(exc) or repr(exc)})
        return {}
    return {"items": [{"url": url, "attribution": "Illustration", "source": "ai_generated"}], "source": "ai_generated",
            "fetched_at": datetime.now(timezone.utc).isoformat()}


async def ensure_spotlight_photos(db: AsyncSession, user: User, cards: list[dict], allow_ai: bool = True) -> None:
    """Every town in a Town Spotlight shows a picture (user, 10/17 - one came out blank): the town's
    own photos, else a real landmark photo, else (last) one AI picture - saved on the shared town
    record either way, so the next article anywhere gets it instantly."""
    from app.services.website.content_adapters import resolve_photo_urls
    for card in cards:
        if card.get("photos") or card.get("photo"):
            continue
        area = (await db.execute(select(MarketArea).where(MarketArea.slug == card.get("slug")))).scalar_one_or_none()
        if area is None:
            continue
        found = await _landmark_photos(area) or (await _ai_town_photo(db, user, area) if allow_ai else {})
        if not found:
            continue
        area.facts = {**(area.facts or {}), "photos": found}
        await db.commit()
        items = ((resolve_photo_urls(found) or {}).get("items") or [])
        card["photos"] = [{"url": it["url"], "credit": it.get("attribution") or "Google Maps"} for it in items[:4] if it.get("url")]
        card["photo"] = card["photos"][0]["url"] if card["photos"] else None


_PHOTO_LOOKUPS: set[str] = set()


async def _landmark_backfill(slug: str) -> None:
    """Background: a real landmark photo for a town with none (no AI here - this runs from a
    public page view). Saved on the shared town record; the next view of the article shows it."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            area = (await db.execute(select(MarketArea).where(MarketArea.slug == slug))).scalar_one_or_none()
            if area is None or ((area.facts or {}).get("photos") or {}).get("items"):
                return
            found = await _landmark_photos(area)
            if found:
                area.facts = {**(area.facts or {}), "photos": found}
                await db.commit()
    except Exception:
        logger.warning("landmark backfill failed", exc_info=True)


async def fill_spotlight_photos(db: AsyncSession, blocks: list) -> list:
    """Published articles whose Town Spotlight was saved without a picture (user, 10/17 - Radnor)
    show the town's shared photos once there are some. Never calls out while the page waits:
    a town with no photos at all gets a landmark lookup in the background, once."""
    import asyncio
    from app.services.website.content_adapters import resolve_photo_urls
    missing = [b for b in (blocks or []) if isinstance(b, dict) and b.get("type") == "area_spotlight"
               and isinstance((b.get("data") or {}).get("area"), dict)
               and not (b["data"]["area"].get("photos") or b["data"]["area"].get("photo"))]
    if not missing:
        return blocks
    slugs = {b["data"]["area"].get("slug") for b in missing if b["data"]["area"].get("slug")}
    areas = {a.slug: a for a in (await db.execute(select(MarketArea).where(MarketArea.slug.in_(slugs)))).scalars().all()} if slugs else {}
    out = []
    for b in blocks:
        if b in missing:
            area = areas.get(b["data"]["area"].get("slug"))
            items = ((resolve_photo_urls((area.facts or {}).get("photos")) or {}).get("items") or []) if area is not None else []
            photos = [{"url": it["url"], "credit": it.get("attribution") or "Google Maps"} for it in items[:4] if it.get("url")]
            if photos:
                b = {**b, "data": {**b["data"], "area": {**b["data"]["area"], "photos": photos, "photo": photos[0]["url"]}}}
            elif area is not None and area.slug not in _PHOTO_LOOKUPS:
                _PHOTO_LOOKUPS.add(area.slug)
                task = asyncio.create_task(_landmark_backfill(area.slug))
                _BACKGROUND.add(task)
                task.add_done_callback(_BACKGROUND.discard)
        out.append(b)
    return out


_BACKGROUND: set = set()


async def spotlight_for_brief(db: AsyncSession, user: User, slugs: list) -> list[dict]:
    """The Town Spotlight cards an article asked for (up to MAX_SPOTLIGHT_TOWNS), each with a photo."""
    wanted = [s for s in (slugs or []) if isinstance(s, str)][:MAX_SPOTLIGHT_TOWNS]
    if not wanted:
        return []
    cards = {c["slug"]: c for c in await area_spotlight_cards(db, user, extra_slugs=wanted)}
    out = [cards[s] for s in wanted if s in cards]
    try:
        await ensure_spotlight_photos(db, user, out)
    except Exception:
        logger.warning("spotlight photo fallback failed", exc_info=True)
    return out


async def area_spotlight_cards(db: AsyncSession, user: User, extra_slugs: Optional[list] = None) -> list[dict]:
    """Every town the agent serves, as a Town Spotlight snapshot: photo(s), summary, live median
    price / homes for sale / days on market, school districts and what's nearby - the same verified
    Areas research + Bright MLS stats the IDX community pages use, dated so the article can say
    "as of"."""
    from app.services.website.content_adapters import get_served_areas_summary
    from app.services.website.content_adapters import resolve_photo_urls
    cards = await get_served_areas_summary(db, user, limit=15, with_stats=True)
    rows = (await db.execute(
        select(MarketArea).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id).where(AgentMarketArea.owner_id == user.id)
    )).scalars().all()
    by_slug = {a.slug: a for a in rows}
    as_of = datetime.now(timezone.utc).strftime("%B %Y")
    out = []
    for c in cards:
        area = by_slug.get(c.get("slug"))
        photos = []
        if area is not None:
            items = ((resolve_photo_urls((area.facts or {}).get("photos")) or {}).get("items") or [])
            for it in items[:4]:
                if it.get("url"):
                    photos.append({"url": it["url"], "credit": it.get("attribution") or it.get("author") or "Google Maps"})
        # Licensed place photos only - an MLS listing photo belongs to that listing's agent /
        # photographer, so it's never pulled into an article (IDX display rules don't cover blogs).
        out.append({
            "slug": c.get("slug"), "name": c.get("name"), "state": c.get("state"), "county": c.get("county"),
            "photo": photos[0]["url"] if photos else None, "photos": photos,
            "summary": _objective_summary(c.get("summary")),
            "median_price": c.get("median_price"), "active_count": c.get("active_count"),
            "min_price": c.get("min_price"), "max_price": c.get("max_price"),
            "school_districts": c.get("school_districts") or [],
            "nearby": c.get("hover_facts") or {},
            "as_of": as_of,
            "price_range_basis": c.get("price_range_basis"),
        })
    # Towns outside the agent's served areas that the article asked for - researched, shared rows.
    have = {c["slug"] for c in out}
    extra = [x for x in (extra_slugs or []) if x and x not in have]
    if extra:
        rows = (await db.execute(select(MarketArea).where(MarketArea.slug.in_(extra), MarketArea.enrichment_status == "ENRICHED"))).scalars().all()
        for area in rows:
            out.append(await town_card_for_area(db, area))
    return out


_SUBJECTIVE_SCHOOL = re.compile(r"\b(school|schools|district|districts)\b", re.I)
_SUBJECTIVE_WORDS = re.compile(r"\b(best|top|top-rated|highly|regarded|acclaimed|excellent|great|prestigious|desirable|sought-after|award-winning|renowned|outstanding|family-friendly|premier|elite)\b", re.I)


def _objective_summary(text: Optional[str]) -> Optional[str]:
    """Town summaries go into articles under the agent's name - drop any sentence that rates
    schools ("highly regarded schools", "top districts"); objective district names are shown
    separately. Fair-housing / school-language rule."""
    if not text:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [p for p in parts if not (_SUBJECTIVE_SCHOOL.search(p) and _SUBJECTIVE_WORDS.search(p))]
    return " ".join(kept).strip() or None


def _fmt_area(card: dict) -> str:
    bits = [f"{card.get('name')}, {card.get('state')}" + (f" ({card['county']} County)" if card.get("county") else "")]
    if card.get("median_price"):
        bits.append(f"median active list price ${int(card['median_price']):,}")
    if card.get("active_count") is not None:
        bits.append(f"{card['active_count']} homes currently for sale")
    nearby = card.get("nearby") or {}
    if nearby.get("market"):
        bits.append("; ".join(nearby["market"]))
    sds = [d.get("name") if isinstance(d, dict) else str(d) for d in (card.get("school_districts") or [])]
    if sds:
        bits.append("school districts serving parts of the area: " + ", ".join(x for x in sds if x))
    for key, label in (("downtown", "downtown"), ("parks", "parks"), ("transportation", "transit"), ("restaurants_shopping", "dining/shopping")):
        if nearby.get(key):
            bits.append(f"{label}: " + ", ".join(nearby[key]))
    if card.get("summary"):
        bits.append(f"summary: {card['summary']}")
    return " | ".join(bits) + f" (data as of {card.get('as_of')})"


def area_spotlight_block(card: dict) -> dict:
    return {"id": _bid("area_spotlight"), "type": "area_spotlight", "data": {"area": card}}


async def build_context(db: AsyncSession, user: User) -> dict:
    """What the interview screen pre-fills from: the agent's name/brokerage/market, the towns they
    serve (Website Builder areas), their recent sales and their active listings - all real data
    they can choose to reference - plus whether their AI allowance can cover a generation."""
    config = await _hub_config(db, user.id)
    try:
        from app.services import agent_hub_video_sync
        hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalars().first() if agent_hub_video_sync.wants_sync(config) else None
        if hub is not None:
            if not (config.get("videoLinks") or []):
                import asyncio
                await asyncio.wait_for(agent_hub_video_sync.sync_hub_videos(db, hub), timeout=8)
                config = hub.config or {}
            else:
                agent_hub_video_sync.refresh_in_background(hub.id)
    except Exception:  # YouTube slow/unreachable: carry on without suggestions (session left clean)
        logger.info("blog context: channel video sync skipped", exc_info=True)
        await db.rollback()
    areas_result = await db.execute(
        select(MarketArea.name).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == user.id).order_by(AgentMarketArea.created_at)
    )
    listings: list[dict] = []
    try:
        from app.services.website.content_adapters import get_active_listings
        for card in await get_active_listings(db, user, limit=30):
            listings.append({
                "id": card.get("listing_key") or "",
                "address": card.get("address") or "",
                "city": card.get("city") or "",
                "price": card.get("price"),
                "beds": card.get("beds"),
                "baths": card.get("baths"),
                "sqft": card.get("sqft"),
                "status": card.get("status") or "",
                "image_url": card.get("photo") or None,
                "state": card.get("state") or None,
            })
    except Exception:  # listings are an optional enrichment - never block the writer on them
        logger.warning("blog writer: active listings lookup failed", exc_info=True)

    area_cards: list[dict] = []
    try:
        area_cards = await area_spotlight_cards(db, user)
    except Exception:  # town data is an optional enrichment - never block the writer on it
        logger.warning("blog writer: area cards lookup failed", exc_info=True)

    ai_config = await ai_config_service.get_config(db)
    snapshot = await monthly_ai_budget_snapshot(db, user.id, ai_config, user.plan_tier)
    exhausted = bool(snapshot.get("exhausted")) and user.id not in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS
    return {
        "agent": _agent_profile(user, config),
        "served_areas": [name for (name,) in areas_result.all()],
        # Every sale - the interview filters them by the towns picked and pages them 10 at a time.
        "recent_sales": _recent_sales(config, limit=300),
        "listings": listings,
        # The agent's own Agent Hub videos + house tours - offered for the article's video slot and
        # its "videos about the area" closing showcase.
        "videos": _hub_videos(config),
        "area_cards": area_cards,
        "usage_percent": snapshot.get("percent"),
        "ai_available": not exhausted,
        "blocked_message": "You've reached your AI usage limit for this month. It resets on the 1st - or upgrade your plan for more AI credits." if exhausted else None,
    }


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _fmt_property(p: dict) -> str:
    bits = [p.get("address") or "", p.get("city") or ""]
    head = ", ".join(b for b in bits if b)
    facts = []
    if p.get("price"):
        facts.append(_money(p["price"]))
    if p.get("beds"):
        facts.append(f"{p['beds']} bd")
    if p.get("baths"):
        facts.append(f"{p['baths']} ba")
    if p.get("sqft"):
        try:
            facts.append(f"{int(float(p['sqft'])):,} sqft")
        except (TypeError, ValueError):
            pass
    if p.get("sold_date"):
        facts.append(f"sold {p['sold_date']}")
    if p.get("status"):
        facts.append(str(p["status"]))
    return f"{head} - {', '.join(facts)}" if facts else head


def _video(brief: dict) -> Optional[dict]:
    v = brief.get("video")
    if isinstance(v, dict) and str(v.get("url") or "").strip():
        return {"url": str(v["url"]).strip(), "title": str(v.get("title") or "").strip()[:160]}
    return None


def _video_line(brief: dict) -> Optional[str]:
    v = _video(brief)
    if not v:
        return None
    named = f'titled "{v["title"]}"' if v["title"] else "(no title given)"
    return (
        f"A video of the agent's will be embedded in this article - {named}. Give it a home: make ONE of the H2 "
        "sections the natural place for it (its heading should connect to what the video is about), and in that "
        "section tell the reader they can watch the video just below. Only describe the video by what its title says - "
        "never invent what it shows."
    )


_WORD = re.compile(r"[a-z0-9']{3,}")
_STOP = {"the", "and", "for", "with", "you", "your", "what", "how", "this", "that", "from", "are", "our", "into", "about", "tour", "video"}


def place_video(blocks: list[dict], brief: dict) -> list[dict]:
    """Embed the brief's video at the end of the section written for it: the section that tells the
    reader to watch it (or, failing that, the one sharing the most words with the video's title),
    else right after the intro. Leaves the blocks alone when there's no video."""
    v = _video(brief)
    if not v:
        return blocks
    heads = [i for i, b in enumerate(blocks) if b.get("type") == "heading"]
    title_words = {w for w in _WORD.findall(v["title"].lower()) if w not in _STOP}

    def text(b: dict) -> str:
        return re.sub(r"<[^>]+>", " ", str((b.get("data") or {}).get("html") or (b.get("data") or {}).get("text") or "")).lower()

    best, best_score = None, 0.0
    for n, h in enumerate(heads[:-1] if len(heads) > 1 else heads):  # never the closing section
        end = heads[n + 1] if n + 1 < len(heads) else len(blocks)
        body = " ".join(text(b) for b in blocks[h + 1:end])
        head = text(blocks[h])
        score = (6 if re.search(r"\b(video|watch)\b", body) else 0)
        score += 2 * len(title_words & set(_WORD.findall(head))) + len(title_words & set(_WORD.findall(body)))
        if score > best_score:
            best, best_score = (h, end), score
    if best:
        at = best[1]
    else:
        at = heads[0] if heads else min(1, len(blocks))
    block = {"id": _bid("youtube"), "type": "youtube", "data": {"url": v["url"], "title": v["title"]}}
    return blocks[:at] + [block] + blocks[at:]


def _brief_text(brief: dict, profile: dict, sales: list[dict], listings: list[dict], areas: Optional[list[dict]] = None) -> tuple[str, str]:
    """Returns (user_message, source_material) - the latter is the concatenated real-data/brief text
    the number audit checks the article against."""
    tone = _TONES.get(brief.get("tone") or "warm", _TONES["warm"])
    length_label, words, max_sections = _LENGTHS.get(brief.get("length") or "standard", _LENGTHS["standard"])
    cta_key = brief.get("cta") or "how_can_i_help"
    cta = _CTAS.get(cta_key) or f"close with this call to action from the agent: {cta_key}"

    agent_lines = [
        f"Name: {profile['name']}",
        profile["title"] and f"Title: {profile['title']}",
        profile["brokerage"] and f"Brokerage: {profile['brokerage']}",
        profile["market"] and f"Market: {profile['market']}",
        profile["about"] and f"About (their own words): {profile['about']}",
    ]
    brief_lines = [
        f"Topic: {brief.get('topic', '').strip()}",
        brief.get("goal") and f"What the reader should walk away knowing/doing: {brief['goal'].strip()}",
        brief.get("audiences") and f"Written for: {', '.join(brief['audiences'])}",
        brief.get("areas") and f"Focus areas: {', '.join(brief['areas'])}",
        brief.get("key_points") and f"Key points to cover:\n{brief['key_points'].strip()}",
        brief.get("personal_take") and f"The agent's personal take / local story (weave this in faithfully):\n{brief['personal_take'].strip()}",
        brief.get("facts") and f"Facts and numbers the agent provided (the ONLY statistics you may cite):\n{brief['facts'].strip()}",
        brief.get("category_hint") and f"Preferred category: {brief['category_hint']}",
        _video_line(brief),
    ]
    data_lines = []
    if sales:
        data_lines.append("The agent's own recent sales (real, from their Agent Hub):\n" + "\n".join(f"- {_fmt_property(s)}" for s in sales))
    if listings:
        data_lines.append("Listings the agent attached (real):\n" + "\n".join(f"- {_fmt_property(p)}" for p in listings))
    if areas:
        data_lines.append(
            "Verified town data (OpenHousePal Areas research + live Bright MLS stats - you may cite these, with the as-of date; "
            "a Town Spotlight card with these numbers is inserted into the article automatically, so reference it rather than repeating every figure):\n"
            + "\n".join(f"- {_fmt_area(a)}" for a in areas)
        )

    agent_block = "\n".join(l for l in agent_lines if l)
    brief_block = "\n\n".join(l for l in brief_lines if l)
    data_block = "\n\n".join(data_lines) if data_lines else "(none - do not cite any specific sales, prices or market statistics beyond the brief)"
    today = datetime.now(timezone.utc).strftime("%B %Y")

    msg = (
        f"Write {length_label} ({words}, {max(3, max_sections - 1)}-{max_sections} H2 sections) for my blog.\n"
        f"Tone: {tone}.\nCall to action: {cta}.\nCurrent month: {today}.\n\n"
        f"== AGENT ==\n{agent_block}\n\n== AGENT BRIEF ==\n{brief_block}\n\n== REAL DATA ==\n{data_block}"
    )
    source = "\n".join([agent_block, brief_block, "\n".join(data_lines), today])
    return msg, source


# ---------------------------------------------------------------------------
# Output -> editor blocks
# ---------------------------------------------------------------------------

def _bid(kind: str) -> str:
    return f"{kind}-ai-{uuid.uuid4().hex[:10]}"


_BOLD = re.compile(r"\*\*(.+?)\*\*")


_MD_LINK = re.compile(r"\[([^\[\]]{1,140})\]\((https?://[^\s()]+)\)")


def _rich(text: str, links: Optional[set] = None) -> str:
    """Plain model text -> the editor's sanitized inline HTML (escape first, then **bold**, then
    [anchor](url) links - only to URLs in `links`, the article's own sources; any other link
    keeps its words and loses the URL)."""
    escaped = html.escape((text or "").strip(), quote=False)
    out = _BOLD.sub(r"<strong>\1</strong>", escaped).replace("\n", "<br>")

    def link(m):
        url = html.unescape(m.group(2))
        if links and url in links:
            return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{m.group(1)}</a>'
        return m.group(1)
    return _MD_LINK.sub(link, out)


def _plain(text: str) -> str:
    return _MD_LINK.sub(r"\1", _BOLD.sub(r"\1", (text or "").strip()))


def _coerce(value, kind):
    """The model occasionally returns a nested object/array as a JSON-encoded string inside the
    tool input - decode it rather than failing the whole (already paid-for) draft."""
    if isinstance(value, str) and kind in (dict, list):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return [value] if kind is list and value.strip() else ({} if kind is dict else [])
    return value if isinstance(value, kind) else ({} if kind is dict else [])


def _normalize_article(article: dict) -> dict:
    for key in ("intro", "at_a_glance", "tags", "facts_used", "sections"):
        article[key] = _coerce(article.get(key), list)
    if len(article["tags"]) == 1 and isinstance(article["tags"][0], str) and "," in article["tags"][0]:
        article["tags"] = [t.strip() for t in article["tags"][0].split(",") if t.strip()]
    article["closing"] = _coerce(article.get("closing"), dict)
    article["closing"]["paragraphs"] = _coerce(article["closing"].get("paragraphs"), list)
    sections = []
    for section in article["sections"]:
        section = _coerce(section, dict)
        section["blocks"] = _coerce(section.get("blocks"), list)
        sections.append(section)
    article["sections"] = sections
    return article


def _convert_blocks(raw_blocks: list, links: Optional[set] = None) -> list[dict]:
    out = []
    for b in _coerce(raw_blocks, list):
        b = _coerce(b, dict)
        if not b:
            continue
        kind = b.get("type")
        if kind in ("bulleted_list", "numbered_list"):
            items = [_plain(i) for i in _coerce(b.get("items"), list) if isinstance(i, str) and i.strip()]
            if items:
                out.append({"id": _bid(kind), "type": kind, "data": {"items": items}})
        elif kind in ("paragraph", "subheading", "quote", "callout"):
            text = b.get("text") or ""
            if text.strip():
                out.append({"id": _bid(kind), "type": kind, "data": {"html": _rich(text, links if kind in ("paragraph", "callout") else None)}})
    return out


def article_to_blocks(article: dict, default_label: str = "Contact Me", form_type: Optional[str] = None, links: Optional[set] = None) -> list[dict]:
    """`links`: URLs the article may link inline (the pipeline passes its sources)."""
    blocks: list[dict] = []
    for para in article.get("intro") or []:
        if isinstance(para, str) and para.strip():
            blocks.append({"id": _bid("paragraph"), "type": "paragraph", "data": {"html": _rich(para, links)}})
    glance = [g for g in (article.get("at_a_glance") or []) if isinstance(g, str) and g.strip()]
    if glance:
        body = "<strong>At a glance</strong><br>" + "<br>".join(f"&bull; {_rich(g)}" for g in glance)
        blocks.append({"id": _bid("callout"), "type": "callout", "data": {"html": body}})
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("heading") or "").strip()
        if heading:
            blocks.append({"id": _bid("heading"), "type": "heading", "data": {"html": _rich(heading)}})
        blocks.extend(_convert_blocks(section.get("blocks"), links))
    closing = article.get("closing") or {}
    if closing.get("heading"):
        blocks.append({"id": _bid("heading"), "type": "heading", "data": {"html": _rich(closing["heading"])}})
    for para in closing.get("paragraphs") or []:
        if isinstance(para, str) and para.strip():
            blocks.append({"id": _bid("paragraph"), "type": "paragraph", "data": {"html": _rich(para, links)}})
    # A button that opens one of the agent's forms wears that form's own name (not the AI's wording).
    label = default_label if form_type else (closing.get("button_label") or default_label)
    cta = {"label": label.strip()[:40], "action": "contact"}
    if form_type:
        cta["formType"] = form_type
    blocks.append({"id": _bid("cta"), "type": "cta", "data": cta})
    blocks.append({"id": _bid("agent_profile"), "type": "agent_profile", "data": {}})
    return blocks


def blocks_to_text(blocks: list) -> str:
    """Editor blocks -> plain text (for refine context and the number audit)."""
    lines = []
    for b in blocks or []:
        data = (b.get("data") if isinstance(b, dict) else None) or {}
        if data.get("items"):
            lines.extend(f"- {i}" for i in data["items"] if isinstance(i, str))
        text = data.get("html") or data.get("text") or ""
        if text:
            text = re.sub(r"<br\s*/?>", "\n", text)
            lines.append(html.unescape(re.sub(r"<[^>]+>", "", text)))
        if data.get("label"):
            lines.append(str(data["label"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Number audit - the "no fabricated statistics" safety net
# ---------------------------------------------------------------------------

_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|million|mil|m\b|k\b|thousand)?", re.IGNORECASE)


def _norm_number(token: str) -> Optional[float]:
    t = token.lower().strip()
    mult = 1.0
    if "million" in t or "mil" in t or re.search(r"\dm$", t.replace(" ", "")):
        mult = 1_000_000.0
    elif "thousand" in t or re.search(r"\dk$", t.replace(" ", "")):
        mult = 1_000.0
    digits = re.sub(r"[^\d.]", "", t.split(" ")[0] if " " in t else t)
    digits = digits.rstrip(".")
    if not digits:
        return None
    try:
        return round(float(digits) * mult, 2)
    except ValueError:
        return None


def _numbers_in(text: str) -> set[float]:
    found = set()
    for m in _NUM.finditer(text or ""):
        n = _norm_number(m.group(0))
        if n is not None:
            found.add(n)
    return found


def number_audit(article_text: str, source_text: str) -> list[str]:
    """Figures in the article that don't appear anywhere in the brief / real data. Small counts
    (1-12, e.g. "3 steps") and the current/next year are ignored - everything else is surfaced for
    the agent to confirm before publishing."""
    allowed = _numbers_in(source_text)
    year = datetime.now(timezone.utc).year
    flagged: list[str] = []
    seen = set()
    for m in _NUM.finditer(article_text or ""):
        raw = m.group(0).strip()
        n = _norm_number(raw)
        if n is None or n in allowed:
            continue
        if n <= 12 and not raw.startswith("$") and "%" not in raw and "percent" not in raw.lower():
            continue
        if n in (year, year + 1, year - 1) and not raw.startswith("$"):
            continue
        # rounding tolerance for prices ("$1.9 million" vs 1,900,000; "$2.4M" vs 2,435,000)
        if n >= 10_000 and any(a >= 10_000 and abs(a - n) / a <= 0.03 for a in allowed):
            continue
        if raw not in seen:
            seen.add(raw)
            flagged.append(raw)
    return flagged[:20]


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

async def _metered_call(db: AsyncSession, user: User, *, system: str, tool: dict, user_msg: str, max_tokens: int, refine: bool):
    _require_key()
    config = await ai_config_service.get_config(db)
    if len(user_msg) > config["blog_writer_max_input_chars"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That's a lot of detail - trim your answers a little and try again.")
    model = config["blog_writer_model"]
    try:
        reservation = await reserve_blog_writer(db, user.id, config, user.plan_tier, refine=refine)
    except AIRateLimitExceeded as e:
        await record_usage_event(db, user.id, None, model, None, None, None, 0, True, e.reason, event_type=EVENT_TYPE)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)

    try:
        client = _client()
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        logger.exception("blog writer call failed")
        from app.services.ai.ai_rate_limit_service import classify_provider_error
        await record_usage_event(db, user.id, None, model, None, None, 0.0, 0, True, classify_provider_error(exc), event_type=EVENT_TYPE)
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer didn't respond - try again in a moment. Nothing was charged.")

    cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
    await record_usage_event(
        db, user.id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
        cost, 1, False, None, tool_names=[tool["name"]], event_type=EVENT_TYPE,
    )
    await reconcile_budget(db, reservation, cost)

    payload = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == tool["name"]), None)
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer returned an incomplete draft - please try again.")
    usage = {"model": model, "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
             "cost_usd": cost, "stop_reason": resp.stop_reason}
    return payload, usage


async def generate_article(db: AsyncSession, user: User, brief: dict) -> dict:
    config = await _hub_config(db, user.id)
    profile = _agent_profile(user, config)
    sales = []
    if brief.get("include_recent_sales"):
        wanted = set(brief.get("sale_ids") or [])
        sales = [s for s in _recent_sales(config, limit=300) if not wanted or s["id"] in wanted][:10]
    listings = [p for p in (brief.get("listings") or []) if isinstance(p, dict)][:6]
    spotlight: list[dict] = []
    if brief.get("area_slugs"):
        try:
            spotlight = await spotlight_for_brief(db, user, list(brief.get("area_slugs") or []))
        except Exception:
            logger.warning("blog writer: area spotlight lookup failed", exc_info=True)

    user_msg, source = _brief_text(brief, profile, sales, listings, spotlight)
    ai_config = await ai_config_service.get_config(db)
    article, usage = await _metered_call(
        db, user, system=_SYSTEM, tool=_ARTICLE_TOOL, user_msg=user_msg,
        max_tokens=ai_config["blog_writer_max_output_tokens"], refine=False,
    )
    article = _normalize_article(article)
    if not article.get("title") or not article.get("sections"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer returned an incomplete draft - please try again.")

    blocks = article_to_blocks(article, *cta_block_args(brief))
    if spotlight:
        # Town Spotlight sections right after the opening section - the verified local snapshot.
        heads = [i for i, b in enumerate(blocks) if b["type"] == "heading"]
        at = heads[1] if len(heads) > 1 else (heads[0] if heads else min(2, len(blocks)))
        blocks[at:at] = [area_spotlight_block(c) for c in spotlight]
    if listings:
        # Attached listings render as real property cards just before the closing section.
        cards = [{"id": _bid("property_card"), "type": "property_card", "data": {"property": p, "displaySize": "large"}} for p in listings]
        closing_idx = max((i for i, b in enumerate(blocks) if b["type"] == "heading"), default=len(blocks))
        blocks[closing_idx:closing_idx] = cards
    blocks = place_video(blocks, brief)
    full_text = "\n".join([article.get("title", ""), article.get("subtitle", ""), article.get("meta_description", ""), blocks_to_text(blocks)])
    category = article.get("category") if article.get("category") in BLOG_CATEGORIES else (brief.get("category_hint") or None)
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
        "facts_used": [f for f in (article.get("facts_used") or []) if isinstance(f, dict) and f.get("fact")][:30],
        "numbers_to_verify": number_audit(full_text, source),
        "usage": usage,
    }


async def refine_section(db: AsyncSession, user: User, *, title: str, section_blocks: list, article_text: str,
                         action: str, instruction: Optional[str]) -> dict:
    action = action if action in _REFINE_ACTIONS else "custom"
    if action == "custom" and not (instruction or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Tell the AI how to change this section.")
    section_text = blocks_to_text(section_blocks)
    if not section_text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This section is empty - write a line or two first.")
    config = await _hub_config(db, user.id)
    profile = _agent_profile(user, config)
    user_msg = (
        f"Agent: {profile['name']}{' - ' + profile['brokerage'] if profile['brokerage'] else ''}"
        f"{' - ' + profile['market'] if profile['market'] else ''}\n"
        f"Article title: {title}\n\n== FULL ARTICLE (context only) ==\n{article_text[:9000]}\n\n"
        f"== SECTION TO REVISE ==\n{section_text}\n\n== TASK ==\n{_REFINE_ACTIONS[action]}"
        + (f"\nAgent's instruction: {instruction.strip()}" if (instruction or "").strip() else "")
    )
    ai_config = await ai_config_service.get_config(db)
    result, usage = await _metered_call(
        db, user, system=_REFINE_SYSTEM, tool=_SECTION_TOOL, user_msg=user_msg,
        max_tokens=ai_config["blog_refine_max_output_tokens"], refine=True,
    )
    blocks = []
    heading = (result.get("heading") or "").strip()
    first = section_blocks[0] if section_blocks else None
    if heading and first and isinstance(first, dict) and first.get("type") in ("heading", "subheading"):
        blocks.append({"id": first.get("id") or _bid("heading"), "type": first["type"], "data": {**(first.get("data") or {}), "html": _rich(heading), "text": None}})
    blocks.extend(_convert_blocks(result.get("blocks")))
    for b in blocks:
        if isinstance(b.get("data"), dict):
            b["data"] = {k: v for k, v in b["data"].items() if v is not None}
    return {
        "blocks": blocks,
        "numbers_to_verify": number_audit(blocks_to_text(blocks), article_text + "\n" + (instruction or "")),
        "usage": usage,
    }
