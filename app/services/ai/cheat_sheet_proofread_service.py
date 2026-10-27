"""AI proofread of a Cheat Sheet's "What's Nearby" section (user, 10/25: "shouldn't it replace if the
AI finds something?").

After every rule in nearby_amenities_service has run, the AI reads the finished section - plus the
next-best backup for each list - and, searching the web, reports only items it can confirm are wrong:
permanently closed, the wrong kind of place (a preschool as a K-12 school, a VA hospital as the
hospital, a farm stand as a grocery store), renamed (with the current name), or not a public place.
It can't add anything. nearby_amenities_service acts on what it reports: a wrong item is replaced by
that list's backup (which already passed every rule, and which the AI also reviewed), a renamed one
gets its new name only after Google confirms a place by that name at that spot, and an issue without
a source is ignored.

One review per home when its nearby places are first looked up (the same moment its Google lookup
is charged), on the agent's AI usage like the cheat sheet's school lookup; guarded by its own
platform-wide ceiling and the platform emergency brake. With no review (switched off, AI down,
budget reached, timeout) the section prints as the rules built it.

Switched OFF by default (AI setting cheat_sheet_proofread_enabled). Owner's rule, 10/25: run it only
while 10% or more of sheets have errors. Its catches in the first random test (5 of 15) became engine
rules; afterwards 2 of 25 random homes still had one, so it ships off. To keep the engine improving,
switch it on for a test batch now and then and turn what it finds into rules.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

EVENT_TYPE = "cheat_sheet_proofread"
RESERVATION_USD = 0.40               # generous ceiling for one Sonnet call with up to 5 web searches (~8-15 cents real)
TIMEOUT_SECONDS = 75
PROBLEMS = ("closed", "wrong_category", "renamed", "not_a_public_place")

_SYSTEM = (
    "You proofread the 'What's Nearby' section of a printed open-house fact sheet that buyers take home. "
    "Report ONLY items you can confirm are wrong, each with a source:\n"
    "- closed: permanently closed (or the building is now something else);\n"
    "- wrong_category: the wrong kind of place for its heading or tag - e.g. a preschool or daycare under "
    "private schools or with grades it doesn't teach, a VA, children's, psychiatric or long-term-care "
    "hospital as the hospital, a farm stand or convenience store as a grocery store, an eye or "
    "orthopedic practice as everyday medical care, a mall or highway strip as a downtown;\n"
    "- renamed: the place exists but now goes by another name - give current_name;\n"
    "- not_a_public_place: a subdivision, private club, HOA amenity or something that isn't a place a "
    "buyer could go.\n"
    "Do not report something for being far, small, or less good than another option - that's not your "
    "call. Do not guess: search the web to confirm, and if you can't confirm, don't report it. Review the "
    "BACKUP items too (they replace anything you report). Then call submit_review - an empty list is the "
    "normal answer for a correct sheet."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The item's id exactly as given, e.g. 'grocery_stores#1' or 'backup:parks#0'."},
                    "problem": {"type": "string", "enum": list(PROBLEMS)},
                    "current_name": {"type": ["string", "null"], "description": "For renamed: the place's current name."},
                    "explanation": {"type": "string"},
                    "source_url": {"type": "string", "description": "A page that confirms it."},
                },
                "required": ["id", "problem", "explanation", "source_url"],
            },
        },
    },
    "required": ["issues"],
}

HEADINGS = {
    "private_schools": "Nearby private schools",
    "grocery_stores": "Grocery stores",
    "medical_care": "Medical care (first: the hospital; second: everyday care)",
    "parks": "Parks",
    "airports": "Airports",
    "downtown_areas": "Downtown areas",
}


def _line(item_id: str, place) -> str:
    tag = f" [{place.place_type}]" if getattr(place, "place_type", None) else ""
    miles = f", {place.distance_miles} mi" if getattr(place, "distance_miles", None) is not None else ""
    address = f" - {place.address}" if getattr(place, "address", None) else ""
    return f"  {item_id}: {place.name}{tag}{miles}{address}"


def sheet_text(home: str, lists: dict[str, list], backups: dict[str, list]) -> str:
    out = [f"Home: {home}", ""]
    for key, heading in HEADINGS.items():
        items = lists.get(key) or []
        if not items:
            continue
        out.append(heading + ":")
        out += [_line(f"{key}#{i}", p) for i, p in enumerate(items)]
        spare = [(bk, p) for bk, ps in backups.items() if bk.split(":")[0] == key for p in ps]
        if spare:
            out.append("  BACKUPS (used only to replace something you report):")
            out += [_line(f"backup:{bk}#{i}", p) for bk, ps in backups.items() if bk.split(":")[0] == key for i, p in enumerate(ps)]
        out.append("")
    return "\n".join(out)


async def _ask(text: str, config: dict) -> tuple[list[dict], dict]:
    from app.services.ai.ai_orchestrator_service import _get_client
    client = _get_client()
    model = config.get("cheat_sheet_proofread_model") or "claude-sonnet-5-5"
    submit = {"name": "submit_review", "description": "Submit the confirmed problems (often none).", "input_schema": _SCHEMA}
    search = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    usage = {"input": 0, "output": 0, "searches": 0, "model": model}
    messages = [{"role": "user", "content": text}]

    def add(resp):
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        if getattr(resp.usage, "server_tool_use", None):
            usage["searches"] += resp.usage.server_tool_use.web_search_requests or 0

    resp = await client.messages.create(model=model, max_tokens=2500, system=_SYSTEM, tools=[search, submit], tool_choice={"type": "auto"}, messages=messages)
    add(resp)
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_review"), None)
    if not block:
        # A plain retry - Sonnet 5.5 rejects a forced tool_choice ("tool"/"any").
        messages += [{"role": "assistant", "content": resp.content}, {"role": "user", "content": "Call submit_review now (an empty list if nothing is confirmed wrong)."}]
        resp = await client.messages.create(model=model, max_tokens=1200, system=_SYSTEM, tools=[submit], tool_choice={"type": "auto"}, messages=messages)
        add(resp)
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_review"), None)
    if not block:
        raise RuntimeError("no submit_review in the answer")
    issues = (block.input or {}).get("issues") if isinstance(block.input, dict) else []
    return [i for i in issues or [] if isinstance(i, dict)], usage


def _clean(issues: list[dict]) -> list[dict]:
    out = []
    for i in issues:
        source = str(i.get("source_url") or "").strip()
        if i.get("problem") not in PROBLEMS or not source.startswith("http"):
            continue  # no confirming page, no action
        out.append({
            "id": str(i.get("id") or "").strip(),
            "problem": i["problem"],
            "current_name": " ".join(str(i.get("current_name") or "").split())[:80] or None,
            "explanation": " ".join(str(i.get("explanation") or "").split())[:300],
            "source_url": source[:300],
        })
    return out


async def review(db, home: str, lists: dict[str, list], backups: dict[str, list], owner_id: Optional[str] = None) -> Optional[dict]:
    """{"issues": [...], "cost_usd": float} - the confirmed problems, or None when there's no review
    to use. Never raises."""
    if db is None or not any(lists.values()):
        return None
    from app.database import AsyncSessionLocal
    # Budget and usage bookkeeping run on a session of their own: a refused budget reservation rolls
    # its session back, and on the caller's session that expires the Property mid-build (crashed a
    # sheet in testing once the platform budget was reached).
    async with AsyncSessionLocal() as own:
        return await _review(own, home, lists, backups, owner_id)


async def _review(db, home: str, lists: dict[str, list], backups: dict[str, list], owner_id: Optional[str]) -> Optional[dict]:
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_global_and_emergency
    try:
        config = await ai_config_service.get_config(db)
        # Off unless switched on in the AI settings (owner's rule, 10/25: only while 10%+ of sheets
        # have errors). After the engine rules learned from its first catches, 2 of 25 random homes
        # (8%) still had one - so it ships off, ready to switch back on.
        if not config.get("cheat_sheet_proofread_enabled", False):
            return None
        reservation = await reserve_global_and_emergency(
            db, "cheat_sheet_proofread_global", RESERVATION_USD,
            float(config.get("cheat_sheet_proofread_global_daily_cost_ceiling_usd", 10.0)),
            float(config.get("cheat_sheet_proofread_global_monthly_cost_ceiling_usd", 100.0)), config,
        )
    except AIRateLimitExceeded:
        logger.info("cheat sheet proofread skipped - platform AI budget reached")
        return None
    except Exception:  # noqa: BLE001
        logger.warning("cheat sheet proofread budget reservation failed", exc_info=True)
        return None

    cost, usage = 0.0, None
    try:
        raw, usage = await asyncio.wait_for(_ask(sheet_text(home, lists, backups), config), timeout=TIMEOUT_SECONDS)
        cost = ai_config_service.estimate_cost_usd(config, usage["model"], usage["input"], usage["output"]) \
            + usage["searches"] * float(config.get("area_research_web_search_cost_usd", 0.01) or 0.01)
    except Exception as exc:  # noqa: BLE001 - the section stands as the rules built it
        logger.warning(f"cheat sheet proofread failed for {home}: {type(exc).__name__}: {exc}")
        return None
    finally:
        try:
            await reconcile_budget(db, reservation, cost)
        except Exception:  # noqa: BLE001
            logger.warning("cheat sheet proofread budget reconcile failed", exc_info=True)

    # No owner: still written down - the platform paid (owner_id NULL, on no agent's bar).
    if usage:
        try:
            await record_usage_event(db, owner_id, None, usage["model"], usage["input"], usage["output"], round(cost, 5), usage["searches"],
                                     False, None, tool_names=["cheat_sheet_proofread"], event_type=EVENT_TYPE)
        except Exception:  # noqa: BLE001
            logger.warning("cheat sheet proofread cost logging failed", exc_info=True)
    issues = _clean(raw)
    logger.info(f"cheat sheet proofread for {home}: {len(issues)} issue(s) cost=${cost:.4f}")
    return {"issues": issues, "cost_usd": round(cost, 5)}
