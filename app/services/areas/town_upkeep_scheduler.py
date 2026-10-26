"""Daily town upkeep - keeps every shared town (MarketArea that at least one agent serves) current
and accurate without anyone doing anything. Runs once a day (app/main.py, 4:15 AM Eastern):

  every day, every town (free):
    - school districts re-checked against Bright listings (content_adapters.check_and_store_public_schools)
    - Fair Housing wording pass with the fixed rules (fair_housing_service, no AI)
  a few towns per day, only to fill in what a town never got (no paid re-checks on a timer -
  owner, 10/26; the live Explore Nearby covers places that change):
    - a town whose place check stopped at Google's free-call line finishes it
    - commute times and FEMA flood share, when missing
    - town map borders (Census) and what's within a walk (OpenStreetMap) every 365 days - free
A town added by an agent is researched (and its places checked once) right away by the normal
add-area path (site_routes) or in a planned batch - never here (no nightly research retries).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.database import AgentMarketArea, MarketArea, User
from app.services.areas import town_places_check

logger = logging.getLogger(__name__)

EXTRAS_EVERY_DAYS = 365
MAX_PAID_TOWNS_PER_DAY = 5
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

    summary = {"towns": 0, "schools_fixed": 0, "wording_fixed": 0, "places_checked": 0, "places_removed": 0, "extras": 0}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(MarketArea, AgentMarketArea.owner_id).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
            .where(MarketArea.enrichment_status.in_(["ENRICHED", "PARTIAL"]))
        )).all()
        towns: dict[str, tuple[MarketArea, str]] = {}
        for area, owner_id in rows:
            towns.setdefault(area.id, (area, owner_id))
        summary["towns"] = len(towns)
        paid = free = 0
        # Oldest-checked first, so every town gets its turn.
        order = sorted(towns.values(), key=lambda t: min(_age_days((t[0].facts or {}).get("_places_verified_at")), _age_days(t[0].last_enriched_at)), reverse=True)
        for area, owner_id in order:
            try:
                if await check_and_store_public_schools(db, area):
                    summary["schools_fixed"] += 1
                summary["wording_fixed"] += await fair_housing_service.fix_area_text(db, area, owner_id, use_ai=False)

                # No research here at all (owner, 10/26): towns are researched when an agent adds
                # them, or in a planned batch - never retried on a nightly drip.

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
                # A town whose place check stopped part-way at Google's free-call line finishes it
                # once the calls are back (the 1st) - filling in, not a re-check on a timer.
                if facts.get("_places_check_waiting"):
                    result = await town_places_check.check_town_places(db, area)
                    paid += 1
                    summary["places_checked"] += result.get("checked", 0)
                    summary["places_removed"] += result.get("removed", 0)
                    if result.get("removed"):  # the write-up must not name a place that was just dropped
                        user = await db.get(User, owner_id)
                        narrative = await area_enrichment_service._generate_narrative(db, area, user) if user else None
                        if narrative:
                            area.narrative = narrative
                            db.add(area)
                            await db.commit()
                    continue
                # No paid re-checks on a timer (owner, 10/26: "forget the checkup") - a town's places
                # are checked once when it's researched; the live Explore Nearby covers what changes.
                # Paid steps here only fill in something the town never got.
                did_paid = False
                if not (facts.get("commute") or {}).get("computed_at"):
                    await town_extras_service.refresh_commute(db, area)
                    did_paid = True
                if not (facts.get("flood") or {}).get("computed_at"):
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
