"""Daily town upkeep - keeps every shared town (MarketArea that at least one agent serves) current
and accurate without anyone doing anything. Runs once a day (app/main.py, 4:15 AM Eastern):

  every day, every town (free):
    - school districts re-checked against Bright listings (content_adapters.check_and_store_public_schools)
    - Fair Housing wording pass with the fixed rules (fair_housing_service, no AI)
  when due, a few towns per day (small paid lookups, spread out):
    - places re-verified against Google every 120 days (closed / wrong-town places come off)
    - commute times and FEMA flood share every 365 days (or when missing)
    - town map borders (Census) and what's within a walk (OpenStreetMap) every 365 days - free
    - full re-research every 365 days (new restaurants, shops, parks...), then the AI Fair Housing
      pass over the new text - same budgets/ceilings as first-time research
The daily caps keep spend smooth; a town added by an agent is researched right away by the normal
add-area path (site_routes), not here.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.database import AgentMarketArea, MarketArea, User

logger = logging.getLogger(__name__)

PLACES_EVERY_DAYS = 120
EXTRAS_EVERY_DAYS = 365
RESEARCH_EVERY_DAYS = 365
MAX_PAID_TOWNS_PER_DAY = 5
MAX_RESEARCH_PER_DAY = 2
MAX_FREE_MAP_TOWNS_PER_DAY = 3


def _age_days(iso) -> float:
    if not iso:
        return 1e9
    try:
        dt = iso if isinstance(iso, datetime) else datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return 1e9


async def run_town_upkeep() -> dict:
    from app.services.areas import area_enrichment_service, fair_housing_service, town_extras_service, town_map_service
    from app.services.website.content_adapters import check_and_store_public_schools

    summary = {"towns": 0, "schools_fixed": 0, "wording_fixed": 0, "places_checked": 0, "places_removed": 0, "extras": 0, "researched": 0}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(MarketArea, AgentMarketArea.owner_id).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
            .where(MarketArea.enrichment_status.in_(["ENRICHED", "PARTIAL"]))
        )).all()
        towns: dict[str, tuple[MarketArea, str]] = {}
        for area, owner_id in rows:
            towns.setdefault(area.id, (area, owner_id))
        summary["towns"] = len(towns)
        paid = researched = free = 0
        # Oldest-checked first, so every town gets its turn.
        order = sorted(towns.values(), key=lambda t: min(_age_days((t[0].facts or {}).get("_places_verified_at")), _age_days(t[0].last_enriched_at)), reverse=True)
        for area, owner_id in order:
            try:
                if await check_and_store_public_schools(db, area):
                    summary["schools_fixed"] += 1
                summary["wording_fixed"] += await fair_housing_service.fix_area_text(db, area, owner_id, use_ai=False)

                if researched < MAX_RESEARCH_PER_DAY and _age_days(area.last_enriched_at) > RESEARCH_EVERY_DAYS:
                    user = await db.get(User, owner_id)
                    if user:
                        area = await area_enrichment_service.enrich_area(db, area, user)
                        researched += 1
                        summary["researched"] += 1
                        continue  # enrich_area runs every check below itself

                facts = area.facts or {}
                # Free (Census borders, OpenStreetMap) - yearly, no paid cap; a few towns a day keeps
                # the free servers happy.
                if free < MAX_FREE_MAP_TOWNS_PER_DAY and (
                        _age_days((facts.get("boundaries") or {}).get("computed_at")) > EXTRAS_EVERY_DAYS
                        or _age_days((facts.get("walk") or {}).get("computed_at")) > EXTRAS_EVERY_DAYS):
                    await town_map_service.refresh_boundaries(db, area)
                    await town_map_service.refresh_walkability(db, area)
                    free += 1
                    facts = area.facts or {}

                if paid >= MAX_PAID_TOWNS_PER_DAY:
                    continue
                did_paid = False
                if _age_days(facts.get("_places_verified_at")) > PLACES_EVERY_DAYS:
                    r = await town_extras_service.verify_places(db, area)
                    summary["places_checked"] += r.get("checked", 0)
                    summary["places_removed"] += r.get("removed", 0)
                    did_paid = True
                if _age_days((facts.get("commute") or {}).get("computed_at")) > EXTRAS_EVERY_DAYS:
                    await town_extras_service.refresh_commute(db, area)
                    did_paid = True
                if _age_days((facts.get("flood") or {}).get("computed_at")) > EXTRAS_EVERY_DAYS:
                    await town_extras_service.refresh_flood(db, area)
                    did_paid = True
                if did_paid:
                    paid += 1
                    summary["extras"] += 1
            except Exception:
                logger.exception("town upkeep failed for %s", area.slug)
                await db.rollback()
    logger.info("Town upkeep finished", extra={"event": "town_upkeep", **summary})
    return summary


async def scheduled_town_upkeep() -> None:
    try:
        await run_town_upkeep()
    except Exception:
        logger.exception("Town upkeep job crashed")
