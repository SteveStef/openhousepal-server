"""Last-resort assigned-school lookup for the Cheat Sheet (user, 10/17: "these cheat sheets need to
have the schools" - 1330 N Avignon Dr had none).

Order for each level (elementary / middle / high):
1. Bright MLS's own field for the listing.
2. Official NCES attendance boundaries (nces_school_assignment_service).
3. ONLY if a level is still empty: Claude with web search reads the school district's own
   boundary / school-finder pages (and similar sources) for this exact address. A school is
   accepted only when a source states the assignment for this address or its attendance area -
   otherwise the level stays empty. Never "the nearest school".

The answer is saved on the listing (properties.nearby_amenities["ai_schools"]), so a home is looked
up at most once (re-checked after 180 days). The few cents it costs go on the agent's AI usage
("Cheat sheet schools"), and the printed sheet already says to verify assignments with the district.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

EVENT_TYPE = "school_lookup"
RECHECK_AFTER = timedelta(days=180)
LEVELS = {"elementary": "Elementary School", "middle": "Middle School", "high": "High School"}

_SYSTEM = (
    "You find the PUBLIC schools a specific home is assigned to (its attendance zone), for a printed "
    "open-house fact sheet. Search the web - prefer the school district's own attendance-boundary / "
    "school-finder / feeder pattern pages, then the district or county, then reputable listing sites "
    "that show assigned schools for this exact address. Rules: only give a school when a source states "
    "it is assigned to this address or the attendance area this address is in; if the district has a "
    "single school at a level (e.g. one high school for the whole district), that school counts. "
    "Never guess from distance, never give a private or charter school, never rate or describe schools. "
    "When you are not sure for a level, leave it null. Then call submit_schools."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        level: {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": "string", "description": "The school's official name, e.g. 'Gladwyne Elementary School'."},
                "source_url": {"type": "string"},
                "confident": {"type": "boolean", "description": "True only if a source states this assignment for this address/attendance area."},
            },
            "required": ["name", "confident"],
        }
        for level in LEVELS
    },
    "required": list(LEVELS),
}


def _address_line(prop) -> str:
    street = getattr(prop, "street_address", None) or getattr(prop, "unparsed_address", None) or ""
    tail = " ".join(x for x in [getattr(prop, "state", None), getattr(prop, "zipcode", None)] if x)
    return ", ".join(x for x in [street, getattr(prop, "city", None), tail] if x)


async def _ask(prop, missing: list[str], config: dict) -> tuple[dict, dict]:
    from app.services.ai.ai_orchestrator_service import _get_client
    client = _get_client()
    model = config.get("school_lookup_model") or config.get("area_research_model") or "claude-haiku-4-5"
    district = getattr(prop, "school_district_name", None)
    known = {lvl: v for lvl, v in (("elementary", getattr(prop, "elementary_school", None)), ("middle", getattr(prop, "middle_or_junior_school", None)), ("high", getattr(prop, "high_school", None))) if v}
    text = (
        f"Home: {_address_line(prop)}\n"
        + (f"School district: {district}\n" if district else "")
        + (f"Already known: {', '.join(f'{LEVELS[k]}: {v}' for k, v in known.items())}\n" if known else "")
        + f"Find the assigned public {', '.join(LEVELS[m].lower() for m in missing)} for this home."
    )
    submit = {"name": "submit_schools", "description": "Submit the assigned schools found.", "input_schema": _SCHEMA}
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
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_schools"), None)
    if not block:
        messages += [{"role": "assistant", "content": resp.content}, {"role": "user", "content": "Call submit_schools now with what you found (null for any level you are not sure of)."}]
        resp = await client.messages.create(model=model, max_tokens=800, system=_SYSTEM, tools=[submit], tool_choice={"type": "tool", "name": "submit_schools"}, messages=messages)
        add(resp)
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_schools"), None)
    result = block.input if block and isinstance(block.input, dict) else {}
    return result, usage


def _clean(name) -> Optional[str]:
    if not isinstance(name, str):
        return None
    n = " ".join(name.split()).strip()
    return n if 3 <= len(n) <= 90 else None


async def fill_missing_schools(db, prop, schools: list, owner_id: Optional[str] = None) -> list:
    """`schools` = the NearbyPlace list from Bright/NCES. Returns it with any still-missing levels
    filled from the (saved, else new) web lookup. Never raises."""
    from app.models.property import NearbyPlace
    try:
        have = {getattr(s, "place_type", None) for s in schools}
        missing = [lvl for lvl, label in LEVELS.items() if label not in have]
        if not missing or not getattr(prop, "street_address", None):
            return schools
        store = dict(prop.nearby_amenities or {}) if isinstance(prop.nearby_amenities, dict) else {}
        saved = store.get("ai_schools") or {}
        fresh = False
        try:
            fresh = bool(saved.get("checked_at")) and datetime.now(timezone.utc) - datetime.fromisoformat(saved["checked_at"]) < RECHECK_AFTER
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            from app.services.ai.ai_config_service import ai_config_service
            from app.services.ai.ai_rate_limit_service import record_usage_event
            config = await ai_config_service.get_config(db)
            try:
                found, usage = await asyncio.wait_for(_ask(prop, missing, config), timeout=60)
            except Exception as exc:  # noqa: BLE001 - a sheet without schools beats a broken sheet
                logger.warning("AI school lookup failed", extra={"listing_key": getattr(prop, "listing_key", None), "error": str(exc) or repr(exc)})
                return schools
            cost = ai_config_service.estimate_cost_usd(config, usage["model"], usage["input"], usage["output"]) \
                + usage["searches"] * float(config.get("area_research_web_search_cost_usd", 0.01) or 0.01)
            if owner_id:
                try:
                    await record_usage_event(db, owner_id, None, usage["model"], usage["input"], usage["output"], round(cost, 5), usage["searches"],
                                             False, None, tool_names=["cheat_sheet_schools"], event_type=EVENT_TYPE)
                except Exception:  # noqa: BLE001
                    logger.warning("AI school lookup cost logging failed", exc_info=True)
            levels = {}
            for lvl in LEVELS:
                entry = found.get(lvl) if isinstance(found, dict) else None
                if isinstance(entry, dict) and entry.get("confident") is True and _clean(entry.get("name")):
                    levels[lvl] = {"name": _clean(entry.get("name")), "source_url": str(entry.get("source_url") or "")[:300]}
            saved = {"checked_at": datetime.now(timezone.utc).isoformat(), "levels": levels, "cost_usd": round(cost, 5)}
            # Saved on the listing so the next sheet for this home doesn't search again.
            current = dict(prop.nearby_amenities or {}) if isinstance(prop.nearby_amenities, dict) else {}
            current["ai_schools"] = saved
            prop.nearby_amenities = current
            flag_modified(prop, "nearby_amenities")
            await db.commit()
            logger.info("AI school lookup", extra={"listing_key": getattr(prop, "listing_key", None), "found": list(levels), "cost_usd": round(cost, 4)})
        out = list(schools)
        for lvl in missing:
            got = (saved.get("levels") or {}).get(lvl)
            if got and got.get("name"):
                out.append(NearbyPlace(name=got["name"], place_type=LEVELS[lvl]))
        order = list(LEVELS.values())
        out.sort(key=lambda s: order.index(s.place_type) if s.place_type in order else 9)
        return out
    except Exception:  # noqa: BLE001
        logger.warning("fill_missing_schools failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return schools
