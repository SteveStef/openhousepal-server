"""Which nearby downtowns a Cheat Sheet lists - the AI half (user, 10/25: "add the AI into it").

The AI gets the home's town plus OUR measurements for every nearby town candidate (what kind of
place the Census says it is, how far, how many restaurants and shops sit around its center) and
answers, searching the web when unsure, with the downtowns locals would actually name - "Marlton",
not the township "Evesham"; Bridge Street in Phoenixville, not a mall. nearby_amenities_service
then checks every name it gives back against that same data (a real cluster of restaurants and
shops at that spot, within a real drive) before anything prints, so a wrong answer can't reach the
sheet; with no usable answer the sheet keeps the data-only list.

One answer per town, saved for DOWNTOWN_AI_TTL_DAYS and shared by every home in it. Guarded by the
area-research budget + the platform emergency brake, and charged to the AI usage bar of the agent
whose cheat sheet asked first (user, 10/25: "all things that use AI usage should charge the agent's
usage bar") - later homes in that town reuse the saved answer for free.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

EVENT_TYPE = "cheat_sheet_downtown"
DOWNTOWN_AI_TTL_DAYS = 180
DOWNTOWN_AI_CACHE_VERSION = 2
DOWNTOWN_AI_RESERVATION_USD = 0.25   # a generous ceiling for one Sonnet call with up to 3 web searches (~5-10 cents real)
KINDS = ("downtown", "village_center", "shopping_center")

_SYSTEM = (
    "You finish the 'Nearby Downtown Areas' list on a printed open-house fact sheet that buyers take home, "
    "so every name must be right. A downtown is a main street or village center with shops and restaurants "
    "that locals mean by 'going into town' (Bridge Street in Phoenixville, State Street in Media).\n"
    "You get two lists we measured around the home: towns that PASSED our check (a real cluster of "
    "restaurants and shops at the town's center point) and towns that FAILED it (few businesses at that "
    "point - though for a few, the point may simply miss the main street). Your job:\n"
    "1. Start from the PASSED list. Drop any that isn't a real downtown or village center: a shopping-mall "
    "area, a highway strip, an office park, a college campus, or a whole township's name.\n"
    "2. Use the name locals use - the village, not the township around it (Marlton, not Evesham).\n"
    "3. Add at most 2 real downtowns we missed (a FAILED one whose main street our point missed, or one "
    "in neither list), only within about 8 miles' drive. Never trade a close, well-known downtown for a "
    "farther one.\n"
    "4. If the home's own town has a real downtown, it must be on the list.\n"
    "5. Only when there's no real downtown within about 10 miles, list the main nearby commercial center "
    "with kind shopping_center.\n"
    "Search the web when unsure about a place. Never invent a place. Up to 5, nearest first. Then call "
    "submit_downtowns."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "downtowns": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The town / village name locals use, e.g. 'Phoenixville' - no 'Downtown' prefix."},
                    "state": {"type": "string", "description": "Two-letter state, e.g. 'PA'."},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "main_street": {"type": ["string", "null"], "description": "Its main shopping street, if you know it for sure."},
                },
                "required": ["name", "state", "kind"],
            },
        },
    },
    "required": ["downtowns"],
}


def cache_key(city: str, state: str) -> str:
    town = re.sub(r"[^a-z0-9 ]", "", (city or "").lower()).strip()
    return f"downtown_ai:v{DOWNTOWN_AI_CACHE_VERSION}:{(state or '').upper()}:{town}"


def _table(candidates: list[dict], passed: bool) -> str:
    rows = [f"- {c['name']} ({c['kind']}): {c['miles']} mi straight-line, restaurants+shops at its center: "
            f"{'n/a' if c['score'] is None else c['score']} of 40" for c in candidates if bool(c.get("passed")) == passed]
    return "\n".join(rows) or "(none)"


async def _ask(city: str, state: str, zipcode: Optional[str], candidates: list[dict], config: dict) -> tuple[list[dict], dict]:
    from app.services.ai.ai_orchestrator_service import _get_client
    client = _get_client()
    # Sonnet, not Haiku: tested 10/25 on 16 towns, Haiku dropped Marlton for a Marlton home and traded
    # Bryn Mawr (2.7 mi) for Ambler (14 mi). Per word it's 2x Haiku; the web searches are most of the cost.
    model = config.get("downtown_ai_model") or "claude-sonnet-5-5"
    text = (
        f"Home: {city.title()}, {state}{' ' + zipcode if zipcode else ''}\n\n"
        f"PASSED our check:\n{_table(candidates, True)}\n\n"
        f"FAILED our check:\n{_table(candidates, False)}\n\n"
        "Which downtowns should the fact sheet list for this home?"
    )
    submit = {"name": "submit_downtowns", "description": "Submit the downtowns for the fact sheet.", "input_schema": _SCHEMA}
    search = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    usage = {"input": 0, "output": 0, "searches": 0, "model": model}
    messages = [{"role": "user", "content": text}]

    def add(resp):
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        if getattr(resp.usage, "server_tool_use", None):
            usage["searches"] += resp.usage.server_tool_use.web_search_requests or 0

    resp = await client.messages.create(model=model, max_tokens=1500, system=_SYSTEM, tools=[search, submit], tool_choice={"type": "auto"}, messages=messages)
    add(resp)
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_downtowns"), None)
    if not block:
        messages += [{"role": "assistant", "content": resp.content}, {"role": "user", "content": "Call submit_downtowns now with your answer."}]
        # A plain retry - Sonnet 5.5 rejects a forced tool_choice ("tool"/"any").
        resp = await client.messages.create(model=model, max_tokens=800, system=_SYSTEM, tools=[submit], tool_choice={"type": "auto"}, messages=messages)
        add(resp)
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_downtowns"), None)
    picks = (block.input or {}).get("downtowns") if block and isinstance(block.input, dict) else []
    return [p for p in picks or [] if isinstance(p, dict)], usage


def _clean(picks: list[dict]) -> list[dict]:
    out = []
    for p in picks:
        name = re.sub(r"^downtown\s+", "", " ".join(str(p.get("name") or "").split()), flags=re.IGNORECASE).strip()
        if not 2 <= len(name) <= 60:
            continue
        street = " ".join(str(p.get("main_street") or "").split())[:60] or None
        out.append({"name": name, "state": str(p.get("state") or "").upper()[:2], "kind": p.get("kind") if p.get("kind") in KINDS else "downtown",
                    "main_street": street})
    return out[:5]


async def downtown_picks(db, city: Optional[str], state: Optional[str], zipcode: Optional[str], candidates: list[dict]) -> Optional[list[dict]]:
    """The AI's downtowns for homes in this town - saved answer when there is one, else a new one.
    `candidates`: [{name, kind, miles, score, passed}] from nearby_amenities_service. None when there's no
    answer to use (no town, budget reached, AI failed) - the caller then keeps its data-only list.
    Never raises."""
    if db is None or not city or not state:
        return None
    from app.database import AsyncSessionLocal
    # Cache and budget bookkeeping run on a session of their own: a refused budget reservation (or a
    # failed cache write) rolls its session back, and on the caller's session that expires the
    # Property mid-build.
    async with AsyncSessionLocal() as own:
        return await _downtown_picks(own, city, state, zipcode, candidates)


async def _downtown_picks(db, city: str, state: str, zipcode: Optional[str], candidates: list[dict]) -> Optional[list[dict]]:
    from app.services.nearby_amenities_service import _read_explore_cache, _write_explore_cache
    key = cache_key(city, state)
    try:
        saved = await _read_explore_cache(db, key, DOWNTOWN_AI_TTL_DAYS)
        if isinstance(saved, dict) and isinstance(saved.get("picks"), list):
            return saved["picks"]
    except Exception:  # noqa: BLE001
        logger.warning("downtown AI cache read failed", exc_info=True)
        await db.rollback()

    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, reconcile_budget, reserve_global_and_emergency
    try:
        config = await ai_config_service.get_config(db)
        reservation = await reserve_global_and_emergency(
            db, "area_research_global", DOWNTOWN_AI_RESERVATION_USD,
            config["area_research_global_daily_cost_ceiling_usd"], config["area_research_global_monthly_cost_ceiling_usd"], config,
        )
    except AIRateLimitExceeded:
        logger.info("downtown AI skipped - platform AI budget reached")
        return None
    except Exception:  # noqa: BLE001
        logger.warning("downtown AI budget reservation failed", exc_info=True)
        return None

    cost, usage = 0.0, None
    try:
        raw, usage = await asyncio.wait_for(_ask(city, state, zipcode, candidates, config), timeout=60)
        cost = ai_config_service.estimate_cost_usd(config, usage["model"], usage["input"], usage["output"]) \
            + usage["searches"] * float(config.get("area_research_web_search_cost_usd", 0.01) or 0.01)
    except Exception as exc:  # noqa: BLE001 - the data-only list is a fine fallback
        logger.warning(f"downtown AI failed for {city}, {state}: {type(exc).__name__}: {exc}")
        return None
    finally:
        try:
            await reconcile_budget(db, reservation, cost)
        except Exception:  # noqa: BLE001
            logger.warning("downtown AI budget reconcile failed", exc_info=True)

    from app.services.usage_owner import current_usage_owner
    owner_id = current_usage_owner()
    if owner_id and usage:
        from app.services.ai.ai_rate_limit_service import record_usage_event
        try:
            await record_usage_event(db, owner_id, None, usage["model"], usage["input"], usage["output"], round(cost, 5), usage["searches"],
                                     False, None, tool_names=["cheat_sheet_downtown"], event_type=EVENT_TYPE)
        except Exception:  # noqa: BLE001
            logger.warning("downtown AI cost logging failed", exc_info=True)
            await db.rollback()

    picks = _clean(raw)
    try:
        await _write_explore_cache(db, key, {"picks": picks, "checked_at": datetime.now(timezone.utc).isoformat(), "cost_usd": round(cost, 5)})
    except Exception:  # noqa: BLE001
        logger.warning("downtown AI cache write failed", exc_info=True)
        await db.rollback()
    logger.info(f"downtown AI for {city}, {state}: {[p['name'] for p in picks]} cost=${cost:.4f}")
    return picks
