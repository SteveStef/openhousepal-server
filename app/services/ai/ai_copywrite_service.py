"""AI copywriter + Fair-Housing compliance check.

One small Claude call per request on the cheap model. Powers:
  - Design Studio: draft the headline / body for a template from listing data or a prompt
  - Blogs "AI mode": draft a full post from the agent's key details or a prompt
  - Agent Hub "About": polish / professionalize the bio the agent typed
  - Listing Spotlight (Cinematic Luxury): suggested marquee/Features highlight phrases, and
    per-chapter section copy scoped to that chapter's own photos/description facts
  - Compliance check: flag Fair-Housing / MLS-risky phrases in any AI (or human) text, with a
    suggested swap - ADVISORY ONLY, the agent accepts or ignores each one.

Spend goes through the same reserve -> call -> reconcile + record_usage_event path as CMA / Studio
Media, so it counts against the agent's shared Monthly AI Usage allowance. event_type="copywrite".
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from fastapi import HTTPException, status

from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_copywrite,
)


def _client():
    # Imported lazily - ai_orchestrator_service pulls ai_tools -> app.api.*, which would be a
    # circular import at module load time here.
    from app.services.ai.ai_orchestrator_service import _get_client
    return _get_client()

logger = logging.getLogger(__name__)

_TONES = {
    "professional": "polished and professional",
    "warm": "warm and personable, first person",
    "energetic": "upbeat and energetic",
    "luxury": "refined, understated luxury",
    "concise": "tight and punchy, no filler",
}

# Purpose -> what to actually produce. Kept short; the shared system prompt does the heavy lifting.
_PURPOSES = {
    "listing_description":
        "Write an MLS-style listing description (about 90-160 words) from the property facts provided. "
        "Lead with the home's strongest concrete features. One paragraph, no headings, no bullet list.",
    "design_blurb":
        "Write the short 'about this home' lines for a printed door hanger / flyer / postcard: 2 sentences, "
        "25-38 words, under 230 characters total. Pick the home's 2-3 most appealing concrete features from the "
        "source material (only what it actually says - never invent). Don't repeat the address, price, bed/bath "
        "count or square footage (those print elsewhere on the piece). It comes from the agent personally: the "
        "first sentence describes the home, the second ends on a short, warm first-person note from the agent "
        "(e.g. 'I'd love to show you the backyard in person.'). Fair Housing: describe the home, never the kind "
        "of person who should live there (no 'perfect for families', 'great for retirees', etc.). No exclamation "
        "marks, no emoji, no hashtags, no quotes around it.",
    "social_caption":
        "Write a short social media caption (2-4 sentences) for this listing or update. "
        "Include 3-6 relevant hashtags on their own line at the end.",
    "email":
        "Write a brief, friendly outreach email (about 60-110 words). Plain text, no subject line unless asked.",
    "sms":
        "Write a single short text message (under 320 characters), friendly and direct, no links unless given one.",
    "blog":
        "Write a complete blog post (about 350-600 words) with a clear title on the first line, then 3-5 short "
        "sections with plain-text subheadings. Informative, locally useful, not salesy.",
    "bio_polish":
        "Rewrite the agent bio provided so it reads more professional and confident, in the FIRST PERSON. "
        "Keep every real fact the agent stated; do not invent credentials, sales numbers, awards, or years of "
        "experience. Similar length to the original and NEVER more than 280 characters in total (it is printed "
        "as at most 3 lines on the agent's brag sheet). Return only the rewritten bio.",
    "design_copy":
        "Write concise marketing copy for a flyer / social graphic: a short punchy headline (one line) then "
        "2-4 lines of supporting text. No hashtags.",
    "cheat_sheet_highlights":
        "Condense the listing description into its strongest, most concrete selling points for an open house "
        "one-page guide, written as ONE flowing paragraph (2-4 sentences, not a list, no bullets, no headings, "
        "no line breaks) - it will be printed under a 'Highlights of [address]' heading, so do not repeat the "
        "address or restate the price/beds/baths/sqft (those are shown separately). Lead with the single most "
        "compelling feature. Plain, confident prose, no marketing fluff or filler adjectives. NEVER more than "
        "350 characters in total - it is printed at most 3 lines on the open house guide, and going over gets "
        "it cut off mid-sentence, so aim short rather than right at the limit. Use only facts present in the "
        "description; never invent a feature, number, or amenity that isn't there. Return only the paragraph.",
    "property_site_highlights":
        "Pull the 6 to 8 most distinctive, specific details from this listing description for a scrolling "
        "highlights marquee on a luxury property website - the kind of short phrases that make this exact "
        "home memorable (a named amenity, a striking architectural detail, a specific number), never generic "
        "real-estate adjectives. Output ONLY a bullet list: each line starts with '- ', 2 to 6 words, title "
        "case, no closing punctuation. No intro line, no heading. Use only facts present in the description - "
        "never invent a detail, number, or amenity that isn't there.",
    "property_site_section":
        "Write 1 to 3 sentences of editorial, magazine-style copy for ONE section of a luxury property "
        "website's chapter-by-chapter story, in the refined, confident tone that style implies. You will be "
        "given the section's name and the full listing description - write ONLY about what that section "
        "covers, using facts already present in the description. If the description has nothing specific to "
        "this section, write an accurate, general sentence about what a room/area of that type typically "
        "offers, WITHOUT inventing any specific feature, number, or amenity not already given elsewhere in "
        "the description. Never restate the price, square footage, bed/bath count, or other headline "
        "statistics - those are shown separately. No heading, no quotes around it, just the sentences.",
    "property_site_story_short":
        "Rewrite this listing description as a short, polished story for a luxury property website: TWO short "
        "paragraphs, 80 to 120 words in total and NEVER more than 130. Lead with the single most compelling thing about the home, then the "
        "few details a buyer will remember (named rooms, standout features, the setting). Cut repetition, lists of "
        "minor items, agent instructions, showing instructions and anything about offers or financing. Refined and "
        "confident, never gushing. Use ONLY facts present in the description - never invent a feature, number, "
        "material or view. Do not restate the price. Separate the two paragraphs with one blank line. Return only "
        "the two paragraphs.",
    "property_site_town":
        "Write a short 'About the town' section for a property website, about the town/area named below. Two short "
        "paragraphs, 70 to 120 words in total: the town's general character and setting, then what is nearby that "
        "people enjoy - a downtown or main street, parks and open space, regional attractions, and the general "
        "commute picture (major roads or regional rail) - ONLY things that are well established and widely known "
        "about this specific place. No statistics, prices, rankings, populations, school quality or crime claims, "
        "no specific restaurants or businesses, and nothing about who lives there. Never use the words \"traditional\", \"family\", \"safe\" or \"exclusive\". If you are not confident about "
        "a detail, leave it out and stay general. Separate the paragraphs with one blank line. Return only the two "
        "paragraphs.",
    "property_site_town_facts":
        "Describe the town/area named below for a luxury property website's 'Life in <town>' section, as SHORT "
        "scannable facts - not paragraphs. Return ONLY a JSON object, no code fence, no commentary, with exactly "
        "these keys: "
        "\"tagline\": one line, 6-12 words, capturing the town's character; "
        "\"known_for\": 3-5 things the town is genuinely known for, each 1-4 words; "
        "\"downtown\": {\"name\": the town's actual downtown / main street / village center name (e.g. 'King Street'), "
        "\"where\": where it is, max 10 words, \"highlights\": 3 short items (2-5 words) about what's there, e.g. "
        "'Independent shops and cafes'} - use {} if the town has no real downtown you are sure of; "
        "\"why_move\": 3 reasons people choose to live here, each max 9 words, about the PLACE (setting, pace, "
        "amenities, access) - never about who lives there; "
        "\"getting_around\": 2-3 items {\"label\": a real destination or line (e.g. 'Center City Philadelphia', "
        "'SEPTA Paoli/Thorndale Line'), \"detail\": max 6 words, e.g. 'about 40 min by car'}; "
        "\"outdoors\": 2-3 well-known parks, trails or preserves nearby (names only); "
        "\"whats_new\": one short line (max 14 words) about something genuinely new or recently opened ONLY if you "
        "are confident it is real and current, otherwise an empty string. "
        "Rules: only well-established, widely known facts about THIS specific place - if unsure about any item, "
        "leave it out (use an empty list or string). No statistics, prices, rankings, populations, school quality "
        "or crime claims, and nothing about who lives there. Never use the words 'traditional', 'family', 'safe' or "
        "'exclusive'.",
    "generic":
        "Write the copy the agent asked for. Keep it concise and ready to use.",
}

_SYSTEM = (
    "You are a real estate marketing copywriter for a licensed US agent. Fair Housing and MLS "
    "advertising rules are mandatory:\n"
    "- Never reference or imply race, color, religion, sex, familial status, national origin, "
    "disability, sexual orientation, or age - of the home, the area, or who would live there.\n"
    "- Do not use: \"family\"/\"families\", \"perfect for\", \"safe\"/\"safe neighborhood\", "
    "\"walking distance\"/\"walk to\", \"exclusive\", \"integrated\", \"traditional\", "
    "\"bachelor\", \"empty nesters\", \"his and hers\", \"master bedroom\" (use \"primary bedroom\"), "
    "or any phrase steering a type of person toward or away.\n"
    "- Describe the PROPERTY and its verifiable features, not the buyer. Schools/among amenities "
    "may be named only as neutral factual proximity, never as a selling point about who belongs.\n"
    "- Invent nothing. Use only facts given. No fabricated numbers, awards, or history.\n"
    "- Spell every street, town, township and other proper noun exactly as given (also inside hashtags).\n"
    "Return only the finished copy as plain text - no markdown (no ** or # headings), no preamble, "
    "no explanation, no quote marks around it."
)

_COMPLIANCE_SYSTEM = (
    "You are a Fair Housing / MLS advertising compliance reviewer for US real estate copy. "
    "Find words or phrases that could violate Fair Housing or MLS advertising rules: references to "
    "or implications about protected classes (race, color, religion, sex, familial status, national "
    "origin, disability, sexual orientation, age); steering language; and known risky phrases "
    "(\"family\"/\"families\", \"perfect for\", \"safe\", \"walking distance\"/\"walk to\", "
    "\"exclusive\", \"integrated\", \"master bedroom\", \"bachelor\", \"empty nesters\"). "
    "For each, return the exact phrase as it appears, a one-line reason, and a compliant "
    "replacement that keeps the original meaning as closely as possible. Do NOT rewrite the whole "
    "text. If nothing is risky, return an empty list."
)

_COMPLIANCE_TOOL = {
    "name": "report_flags",
    "description": "Report each risky phrase found in the text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "phrase": {"type": "string", "description": "The exact risky phrase, copied verbatim from the text."},
                        "issue": {"type": "string", "description": "One short line: why it's a risk."},
                        "suggestion": {"type": "string", "description": "A compliant replacement phrase preserving the meaning."},
                    },
                    "required": ["phrase", "issue", "suggestion"],
                },
            }
        },
        "required": ["flags"],
    },
}


def _require_key():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI writing isn't configured yet (missing ANTHROPIC_API_KEY).",
        )


async def generate(
    db, owner_id: str, plan_tier: str, *,
    purpose: str, context: str = "", tone: Optional[str] = None, instruction: Optional[str] = None,
) -> dict:
    _require_key()
    config = await ai_config_service.get_config(db)
    purpose = purpose if purpose in _PURPOSES else "generic"

    parts = [_PURPOSES[purpose]]
    if tone and tone in _TONES:
        parts.append(f"Tone: {_TONES[tone]}.")
    if instruction:
        parts.append(f"Extra instructions from the agent: {instruction.strip()}")
    if context:
        parts.append("Details / source material:\n" + context.strip())
    user_msg = "\n\n".join(parts)

    if len(user_msg) > config["copywrite_max_input_chars"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That's too much input text - trim it and try again.")

    # Specific local facts (a town's actual main street, real parks, real rail lines) are where the
    # small model invents things - that one purpose runs on the stronger model (~1 cent, once per site).
    model = (config.get("town_facts_model") or "claude-sonnet-5") if purpose == "property_site_town_facts" else config["copywrite_model"]
    try:
        reservation = await reserve_copywrite(db, owner_id, config, plan_tier)
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)

    try:
        client = _client()
        resp = await client.messages.create(
            model=model,
            # The stronger model reasons before it answers, and that counts toward the limit.
            max_tokens=5000 if purpose == "property_site_town_facts" else config["copywrite_max_output_tokens"],
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        # Plain text lands in designs, captions and bios as-is - "**Headline**" would print the stars.
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"(?m)^#{1,6}\s+", "", text)
        cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
        await record_usage_event(
            db, owner_id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
            cost, 0, False, None, event_type="copywrite",
        )
        await reconcile_budget(db, reservation, cost)
        return {"text": text}
    except HTTPException:
        await reconcile_budget(db, reservation, 0.0)
        raise
    except Exception as e:
        logger.exception("copywrite generate failed")
        await reconcile_budget(db, reservation, 0.0)
        # The real reason (bad model name, no credit, provider down...) rides along for the admin
        # warning - agents only ever see the generic message (main.provider_failure_handler).
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The AI writer didn't respond ({purpose}, model {model}): {type(e).__name__}: {str(e)[:300]}")


async def compliance_check(db, owner_id: str, plan_tier: str, *, text: str) -> dict:
    _require_key()
    config = await ai_config_service.get_config(db)
    text = (text or "").strip()
    if not text:
        return {"flags": []}
    if len(text) > config["copywrite_max_input_chars"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That's too much text to check at once.")

    model = config["copywrite_model"]
    try:
        reservation = await reserve_copywrite(db, owner_id, config, plan_tier)
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)

    try:
        client = _client()
        resp = await client.messages.create(
            model=model,
            max_tokens=config["copywrite_max_output_tokens"],
            system=_COMPLIANCE_SYSTEM,
            tools=[_COMPLIANCE_TOOL],
            tool_choice={"type": "tool", "name": "report_flags"},
            messages=[{"role": "user", "content": f"Review this real estate copy:\n\n{text}"}],
        )
        flags = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use" and b.name == "report_flags":
                flags = b.input.get("flags", []) or []
        # keep only flags whose phrase actually appears in the text (guards against paraphrase)
        flags = [f for f in flags if isinstance(f, dict) and f.get("phrase") and f["phrase"] in text][:25]
        # The model sometimes omits a field (e.g. no rewrite needed) - never let one ragged flag
        # fail the whole response's validation.
        flags = [{"phrase": str(f["phrase"]), "issue": str(f.get("issue") or ""), "suggestion": str(f.get("suggestion") or "")} for f in flags]
        cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
        await record_usage_event(
            db, owner_id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
            cost, 0, False, None, event_type="copywrite",
        )
        await reconcile_budget(db, reservation, cost)
        return {"flags": flags}
    except HTTPException:
        await reconcile_budget(db, reservation, 0.0)
        raise
    except Exception:
        logger.exception("compliance_check failed")
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The compliance check didn't respond - try again in a moment.")



# ---------------------------------------------------------------- pasted reviews -> review rows

_REVIEWS_SYSTEM = (
    "You pull client reviews of a real estate agent out of text the agent copied from their own "
    "Zillow, Google, Realtor.com, Homes.com or Facebook page. Return ONLY reviews that are really in "
    "the text. Copy each review's words exactly as written - never rewrite, shorten, fix or invent "
    "anything. Skip the agent's own replies, page menus, ads and listing details. If a star rating "
    "isn't shown for a review, leave rating out. If a date is shown (even '3 months ago' or "
    "'March 2024'), give it as YYYY-MM-DD using the 1st of the month when the day isn't shown, and "
    "today's date for reference is TODAY. Use the reviewer's name as written (often first name and "
    "last initial)."
)
_REVIEWS_TOOL = {
    "name": "save_reviews",
    "description": "Every client review found in the pasted text, in the order they appear.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Reviewer name as shown"},
                        "rating": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Stars, only when shown"},
                        "date": {"type": "string", "description": "YYYY-MM-DD, only when a date is shown"},
                        "body": {"type": "string", "description": "The review text, word for word"},
                    },
                    "required": ["name", "body"],
                },
            },
        },
        "required": ["reviews"],
    },
}
REVIEWS_MAX_INPUT_CHARS = 30000


def _norm_ws(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


async def parse_reviews(db, owner_id: str, plan_tier: str, *, text: str) -> dict:
    """Pasted Zillow / Google / Realtor.com page text -> [{name, rating|None, date|None, body}].
    Only reviews whose words really appear in the paste are returned (the model may not invent or
    reword one). Metered on the shared AI allowance like every other writing tool."""
    _require_key()
    from datetime import date
    config = await ai_config_service.get_config(db)
    text = (text or "").strip()
    if len(text) < 20:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste the reviews first - copy them from your Zillow or Google page.")
    if len(text) > REVIEWS_MAX_INPUT_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That's a lot at once - paste about 30 reviews at a time.")
    model = config["copywrite_model"]
    try:
        reservation = await reserve_copywrite(db, owner_id, config, plan_tier)
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)
    try:
        resp = await _client().messages.create(
            model=model,
            max_tokens=12000,
            system=_REVIEWS_SYSTEM.replace("TODAY", date.today().isoformat()),
            tools=[_REVIEWS_TOOL],
            tool_choice={"type": "tool", "name": "save_reviews"},
            messages=[{"role": "user", "content": f"Here is what I copied:\n\n{text}"}],
        )
        found = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use" and b.name == "save_reviews":
                found = b.input.get("reviews", []) or []
        hay = _norm_ws(text)
        out = []
        for r in found:
            if not isinstance(r, dict):
                continue
            body, name = str(r.get("body") or "").strip(), str(r.get("name") or "").strip()
            if len(body) < 3 or not name:
                continue
            # word-for-word guard: the opening and the ending of the review must both be in the paste
            nb = _norm_ws(body)
            if nb[:60] not in hay or nb[-40:] not in hay:
                continue
            rating = r.get("rating")
            rating = int(rating) if isinstance(rating, (int, float)) and 1 <= int(rating) <= 5 else None
            d = str(r.get("date") or "").strip()
            d = d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d <= date.today().isoformat() else None
            out.append({"name": name[:120], "rating": rating, "date": d, "body": body[:5000]})
        cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
        await record_usage_event(db, owner_id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
                                 cost, 0, False, None, event_type="copywrite")
        await reconcile_budget(db, reservation, cost)
        return {"reviews": out[:100], "dropped": max(0, len(found) - len(out))}
    except HTTPException:
        await reconcile_budget(db, reservation, 0.0)
        raise
    except Exception:
        logger.exception("parse_reviews failed")
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't read those reviews - try again in a moment.")


# ---------------------------------------------------------------- listing photo -> room sorting

PHOTO_ROOMS = (
    "exterior_arrival", "great_room", "kitchen", "dining_room", "primary_suite", "additional_bedrooms",
    "bathrooms", "grounds_backyard", "pool_spa", "porch_patio", "garage_automotive", "office_den",
    "basement_rec", "other",
)
_ROOM_HELP = (
    "exterior_arrival = front of the house, driveway, street view or an aerial of the house; "
    "great_room = living room, family room, great room; kitchen; dining_room; "
    "primary_suite = the largest/main bedroom or its sitting area; additional_bedrooms = any other bedroom; "
    "bathrooms = any bathroom; grounds_backyard = back yard, lawn, gardens, land; pool_spa = pool or hot tub; "
    "porch_patio = deck, patio, porch, terrace; garage_automotive = garage; office_den = office, study, library; "
    "basement_rec = basement, gym, theater, game or bar room; other = foyer, hallway, stairs, laundry, closet, "
    "mudroom, floor plans, maps, community amenities or anything unclear."
)
_SORT_TOOL = {
    "name": "assign_rooms",
    "description": "Report which room each numbered photo shows, the sections that sell this home (each one "
                   "backed by the listing text), anything you need the agent to confirm, and the best photos "
                   "to sit beside the home's written story.",
    "input_schema": {
        "type": "object",
        "properties": {
            "photos": {"type": "array", "items": {"type": "object", "properties": {
                "n": {"type": "integer"}, "room": {"type": "string", "enum": list(PHOTO_ROOMS)},
            }, "required": ["n", "room"]}},
            "sections": {
                "type": "array",
                "description": "3 to 6 sections, best first.",
                "items": {"type": "object", "properties": {
                    "title": {"type": "string", "description": "Short, natural title, e.g. 'The Kitchen', 'Pool & Pavilion', 'Theater & Game Room'."},
                    "type": {"type": "string", "enum": list(PHOTO_ROOMS)},
                    "photos": {"type": "array", "items": {"type": "integer"}, "description": "1-6 photo numbers that show it, best first."},
                }, "required": ["title", "type", "photos"]},
            },
            "questions": {
                "type": "array",
                "description": "0-2 things to ask the agent instead of guessing.",
                "items": {"type": "object", "properties": {
                    "photos": {"type": "array", "items": {"type": "integer"}},
                    "question": {"type": "string", "description": "Under 80 characters."},
                    "choices": {"type": "array", "items": {"type": "string"}, "description": "1-3 short section TITLES to add if the agent agrees (e.g. 'Guest Cottage') - never sentences; a Skip button is always shown."},
                }, "required": ["photos", "question", "choices"]},
            },
            "story_photos": {
                "type": "array", "items": {"type": "integer"},
                "description": "Up to 3 photo numbers, best first, to show beside the home's written story: a "
                               "beautiful wide shot of the house in its setting, an aerial, or the main living "
                               "space. Never a bathroom, closet, laundry, map, floor plan or close-up detail.",
            },
        },
        "required": ["photos", "sections", "questions", "story_photos"],
    },
}
_SORT_RULES = (
    "Then choose the 3 to 6 sections that sell this home, best first. Rules:\n"
    "- Every section must be BOTH clearly visible in the photos AND supported by the listing description "
    "or details. Never invent a room the listing doesn't describe.\n"
    "- Prefer the obvious, easy-to-recognise spaces: the living/great room, the kitchen (dining can join "
    "the kitchen), the grounds, the pool.\n"
    "- A pool or spa section only when a pool or spa is clearly in the photos.\n"
    "- The primary suite only when the description makes a point of it AND you are sure which photos show "
    "it. A basement, theater, gym, wine cellar or similar only when the description calls it out and it is "
    "photographed.\n"
    "- Use 5 or 6 sections only when the home really has that many standout spaces. When several amenities "
    "have just a photo or two each, combine them into one section (e.g. 'Theater & Game Room').\n"
    "- If the description mentions a notable space (a gate house, guest cottage, carriage house...) but you "
    "can't tell which photos show it, don't guess - ask the agent in `questions` with the likely photos.\n"
    "- Ask only about a space the description names that you truly can't find in the photos. Never ask "
    "about something you can see clearly (a photo that plainly shows a pool, kitchen, lawn or living room is "
    "never a question - it belongs in its section), and anything you ask about must NOT also be a section.\n"
    "- Each photo belongs to at most one section.\n"
    "- Never use a floor plan, map or close-up as a section's first photo."
)
SORT_MAX_PHOTOS = 40


def estimate_photo_sort_usd(config: dict, count: int) -> float:
    """~280 input tokens per 512px thumbnail + the prompt; a few output tokens each."""
    n = max(1, min(count, SORT_MAX_PHOTOS))
    return float(ai_config_service.estimate_cost_usd(config, config.get("photo_sort_model") or "claude-sonnet-5", 2600 + n * 280, 300 + n * 14))


async def _thumb(client, url: str) -> Optional[str]:
    import base64
    import io
    from PIL import Image
    try:
        from app.utils.safe_fetch import safe_get
        r = await safe_get(client, url)
        if r.status_code != 200:
            return None
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 - a photo that won't load is just skipped
        return None


async def sort_listing_photos(db, owner_id: str, plan_tier: str, photos: list, listing_text: str = "") -> dict:
    """{"rooms": {url: room}, "highlights": [room, ...], "story_photos": [url, ...], "cost_usd"} -
    one small vision call on 400px thumbnails (about a cent for a whole listing), metered on the
    monthly AI allowance like every copywriter call."""
    import asyncio
    import httpx
    _require_key()
    config = await ai_config_service.get_config(db)
    photos = [p for p in photos if isinstance(p, str) and p.startswith("http")][:SORT_MAX_PHOTOS]
    if not photos:
        return {"rooms": {}, "highlights": [], "sections": [], "questions": [], "story_photos": [], "cost_usd": 0.0}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        thumbs = await asyncio.gather(*[_thumb(client, u) for u in photos])
    content: list = [{"type": "text", "text": (
        "The listing, for context (only describe what it supports):\n" + (listing_text or "(no description)")[:6000]
        + "\n\nReal estate listing photos follow. Room types: " + _ROOM_HELP)}]
    index: dict = {}
    for i, (url, b64) in enumerate(zip(photos, thumbs), start=1):
        if not b64:
            continue
        index[i] = url
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    if not index:
        return {"rooms": {}, "highlights": [], "sections": [], "questions": [], "story_photos": [], "cost_usd": 0.0}
    content.append({"type": "text", "text": "First give every photo number above its room. " + _SORT_RULES
                                            + "\nAlso pick the best story photos. Call assign_rooms."})
    # Sonnet, not the copywriter's Haiku: Haiku on small thumbnails mislabelled rooms (a "primary suite"
    # that wasn't, a pool section without the pool). Still ~2-3 cents a listing.
    model = config.get("photo_sort_model") or "claude-sonnet-5"
    try:
        reservation = await reserve_copywrite(db, owner_id, config, plan_tier)
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)
    try:
        resp = await _client().messages.create(
            model=model, max_tokens=2500, tools=[_SORT_TOOL], tool_choice={"type": "tool", "name": "assign_rooms"},
            messages=[{"role": "user", "content": content}],
        )
        rooms: dict = {}
        highlights: list = []
        story: list = []
        sections: list = []
        questions: list = []

        def urls(nums) -> list:
            out = []
            for n in nums or []:
                try:
                    u = index.get(int(n))
                except (TypeError, ValueError):
                    u = None
                if u and u not in out:
                    out.append(u)
            return out
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use" and b.name == "assign_rooms":
                for item in b.input.get("photos", []) or []:
                    url = index.get(int(item.get("n", 0) or 0))
                    if url and item.get("room") in PHOTO_ROOMS:
                        rooms[url] = item["room"]
                used: set = set()
                for sec in b.input.get("sections", []) or []:
                    shots = [u for u in urls(sec.get("photos")) if u not in used][:6]
                    used.update(shots)
                    title = " ".join(str(sec.get("title") or "").split())[:40]
                    if shots and title and len(sections) < 6:
                        kind = sec.get("type") if sec.get("type") in PHOTO_ROOMS else "other"
                        sections.append({"title": title, "type": kind, "photos": shots})
                        if kind != "other" and kind not in highlights:
                            highlights.append(kind)
                for q in b.input.get("questions", []) or []:
                    shots = urls(q.get("photos"))[:6]
                    choices = [" ".join(str(c).split())[:40] for c in (q.get("choices") or []) if str(c).strip()][:3]
                    if shots and choices and len(questions) < 2:
                        questions.append({"question": str(q.get("question") or "")[:100], "photos": shots, "choices": choices})
                # A question that overlaps a section: for the easy, clear-cut spaces (pool, kitchen,
                # grounds, living) the section wins and the question goes; for the spaces that are
                # easy to get wrong (primary suite, bedrooms, offices, lower level) unsure means ask.
                risky = {"primary_suite", "additional_bedrooms", "office_den", "basement_rec", "other"}
                keep_sections, drop_q = [], set()
                for sec in sections:
                    hit = [i for i, q in enumerate(questions) if set(q["photos"]) & set(sec["photos"])]
                    if hit and sec["type"] in risky:
                        continue
                    drop_q.update(hit)
                    keep_sections.append(sec)
                sections = keep_sections or sections
                questions = [q for i, q in enumerate(questions) if i not in drop_q]
                for n in b.input.get("story_photos", []) or []:
                    try:
                        url = index.get(int(n))
                    except (TypeError, ValueError):
                        url = None
                    if url and url not in story:
                        story.append(url)
        cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
        await record_usage_event(db, owner_id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
                                 cost, 0, False, None, event_type="copywrite")
        await reconcile_budget(db, reservation, cost)
        return {"rooms": rooms, "highlights": highlights[:6], "sections": sections, "questions": questions,
                "story_photos": story[:3], "cost_usd": round(cost, 4)}
    except HTTPException:
        await reconcile_budget(db, reservation, 0.0)
        raise
    except Exception:
        logger.exception("photo sort failed")
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The photo sorter didn't respond - try again in a moment.")
