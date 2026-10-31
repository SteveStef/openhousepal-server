"""Homes that landed in a Showcase outside what it searches for (owner, 11/01).

Old causes, all fixed: new listings were matched with the school district OR'd into the area,
a radius left over from the open house was added on top of the picked towns, and new listings
were never checked against sq ft, acres, max beds/baths or counties (property_sync_service /
property_service, fixed 10/31-11/01). Homes they put in stayed. This takes them out once - quietly
(no email) - and never touches a home anyone already looked at or did anything with (owner, 11/01):
viewed by the client or the agent, liked, passed on, commented on, asked to tour, added by hand.

A home only counts as outside with real evidence - the fact it breaks is on record (a home with no
town, size or year on record stays):
- its town/township isn't one of the picked ones, or its district isn't the picked district
- its beds, baths, sq ft or year built are outside the Showcase's limits (not for land, farms or
  commercial - the search doesn't hold those to them either), or its lot size is
- it's a kind of home the Showcase didn't pick (or a rental / commercial space when none were)
Price is left alone on purpose: prices move after a home is added (a drop below the min is still
news to the client). It runs once per server (system_settings DONE_KEY); safe to run again anyway.
"""
import logging
import re
from typing import Optional

from sqlalchemy import and_, delete, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    Collection, CollectionPreferences, Property, PropertyComment, PropertyInteraction, PropertyTour, collection_properties,
)

logger = logging.getLogger(__name__)

_SUFFIX = re.compile(r"\s+(TOWNSHIP|TWP|BOROUGH|BORO|CITY|TOWN)$")
_EXEMPT = {"LAND", "FARM", "COMMERCIAL", "OTHER"}          # no bed/bath/size limits in the search
_NOT_FOR_SALE = {"RESIDENTIAL_LEASE", "COMMERCIAL"}
_TYPE_FLAGS = (("is_single_family", ("SINGLE_FAMILY",)), ("is_town_house", ("TOWNHOUSE",)), ("is_condo", ("CONDO",)),
               ("is_multi_family", ("MULTI_FAMILY",)), ("is_lot_land", ("LAND", "FARM")), ("is_farm", ("FARM",)),
               ("is_commercial", ("COMMERCIAL",)), ("is_apartment", ("RESIDENTIAL_LEASE",)))


def _place(name: Optional[str]) -> str:
    return _SUFFIX.sub("", (name or "").split(",")[0].strip().upper())


def _picked(values) -> set[str]:
    return {_place(v) for v in (values or []) if isinstance(v, str) and _place(v)}


def _limit(value, lo, hi) -> bool:
    """True when a known value breaks a set limit (0 / None = no limit)."""
    if value is None:
        return False
    return bool((lo and lo > 0 and value < lo) or (hi and hi > 0 and value > hi))


def _outside(home, p, cities: set[str], towns: set[str], districts: set[str]) -> bool:
    city, town, district = _place(home.city), _place(home.township), (home.school_district_name or "").upper()
    if cities or towns:
        if (city and city in cities) or (city and city in towns) or (town and town in towns) or (town and town in cities):
            pass
        elif (cities and city) or (towns and town):
            return True  # its town is known and isn't one they picked
    if districts and district and not any(d in district for d in districts):
        return True
    kind = (home.home_type or "").upper()
    picked_types = {t for flag, ts in _TYPE_FLAGS if getattr(p, flag, None) for t in ts}
    if kind and ((picked_types and kind not in picked_types) or (not picked_types and kind in _NOT_FOR_SALE)):
        return True
    if _limit(home.lot_size_acres, p.min_acres, p.max_acres):
        return True
    if kind not in _EXEMPT and (
            _limit(home.bedrooms, p.min_beds, p.max_beds) or _limit(home.bathrooms, p.min_baths, p.max_baths)
            or _limit(home.living_area, p.min_sqft, p.max_sqft) or _limit(home.year_built, p.min_year_built, p.max_year_built)):
        return True
    return False


async def remove_homes_outside_area(db: AsyncSession, collection_id: Optional[str] = None) -> dict:
    """{showcases: n changed, homes: n removed}."""
    stmt = select(CollectionPreferences).join(Collection, Collection.id == CollectionPreferences.collection_id)
    if collection_id:
        stmt = stmt.where(CollectionPreferences.collection_id == collection_id)
    changed = removed = 0
    for prefs in (await db.execute(stmt)).scalars().all():
        cities, towns = _picked(prefs.cities), _picked(prefs.townships)
        districts = _picked(prefs.school_districts)
        cid = prefs.collection_id
        homes = (await db.execute(
            select(Property.id, Property.city, Property.township, Property.school_district_name, Property.home_type,
                   Property.bedrooms, Property.bathrooms, Property.living_area, Property.year_built, Property.lot_size_acres)
            .join(collection_properties, collection_properties.c.property_id == Property.id)
            .where(collection_properties.c.collection_id == cid)
        )).all()
        out = {h.id for h in homes if _outside(h, prefs, cities, towns, districts)}
        if not out:
            continue
        keep = {r[0] for r in (await db.execute(union(
            select(PropertyInteraction.property_id).where(PropertyInteraction.collection_id == cid, or_(
                PropertyInteraction.liked.is_(True), PropertyInteraction.disliked.is_(True),
                PropertyInteraction.view_count > 0, PropertyInteraction.last_viewed_at.is_not(None),
                PropertyInteraction.agent_viewed_at.is_not(None), PropertyInteraction.agent_recommended_at.is_not(None))),
            select(PropertyComment.property_id).where(PropertyComment.collection_id == cid),
            select(PropertyTour.property_id).where(PropertyTour.collection_id == cid),
        ))).all()}
        out -= keep
        if not out:
            continue
        await db.execute(delete(collection_properties).where(and_(
            collection_properties.c.collection_id == cid, collection_properties.c.property_id.in_(list(out)))))
        changed += 1
        removed += len(out)
    await db.commit()
    return {"showcases": changed, "homes": removed}


DONE_KEY = "showcase_cleanup_1101"


async def scheduled_remove_homes_outside_area() -> None:
    """Right after start - see main.py. Once: the matching that let these in is fixed."""
    from app.database import AsyncSessionLocal
    from app.models.database import SystemSettings
    try:
        async with AsyncSessionLocal() as db:
            if (await db.execute(select(SystemSettings).where(SystemSettings.key == DONE_KEY))).scalar_one_or_none():
                return
            done = await remove_homes_outside_area(db)
            db.add(SystemSettings(key=DONE_KEY, value=done))
            await db.commit()
        logger.info("Removed homes outside their Showcase's search (one time): %s", done)
    except Exception:  # noqa: BLE001
        logger.exception("removing homes outside their Showcase's search failed")
