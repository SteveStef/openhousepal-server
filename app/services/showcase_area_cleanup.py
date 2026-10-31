"""Homes that landed in a Showcase from outside the towns it was set to (owner, 11/01).

Two old causes, both fixed: new listings were matched with the school district OR'd into the area
(property_sync_service._discover_matching_collections, fixed 10/31), and a radius left over from
the open house was added on top of the picked towns (property_service, fixed 11/01). Homes either
one put in stayed. This takes them out - quietly (no email) - and keeps every home the client
liked, commented on or asked to tour, and every home the agent added by hand.

A home only counts as outside with real evidence: its own town or township is known and is none
of the picked ones (a home with no town on record stays), or a district was picked and its known
district isn't it. Safe to run again: a clean Showcase has nothing to remove.
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


def _place(name: Optional[str]) -> str:
    return _SUFFIX.sub("", (name or "").split(",")[0].strip().upper())


def _picked(values) -> set[str]:
    return {_place(v) for v in (values or []) if isinstance(v, str) and _place(v)}


def _outside(home, cities: set[str], towns: set[str], districts: set[str]) -> bool:
    city, town, district = _place(home.city), _place(home.township), (home.school_district_name or "").upper()
    if cities or towns:
        if (city and city in cities) or (city and city in towns) or (town and town in towns) or (town and town in cities):
            pass
        elif (cities and city) or (towns and town):
            return True  # its town is known and isn't one they picked
    if districts and district and not any(d in district for d in districts):
        return True
    return False


async def remove_homes_outside_area(db: AsyncSession, collection_id: Optional[str] = None) -> dict:
    """{showcases: n changed, homes: n removed}. Only Showcases with towns or townships picked."""
    stmt = select(CollectionPreferences).join(Collection, Collection.id == CollectionPreferences.collection_id)
    if collection_id:
        stmt = stmt.where(CollectionPreferences.collection_id == collection_id)
    changed = removed = 0
    for prefs in (await db.execute(stmt)).scalars().all():
        cities, towns = _picked(prefs.cities), _picked(prefs.townships)
        if not (cities or towns):
            continue
        districts = _picked(prefs.school_districts)
        cid = prefs.collection_id
        homes = (await db.execute(
            select(Property.id, Property.city, Property.township, Property.school_district_name)
            .join(collection_properties, collection_properties.c.property_id == Property.id)
            .where(collection_properties.c.collection_id == cid)
        )).all()
        out = {h.id for h in homes if _outside(h, cities, towns, districts)}
        if not out:
            continue
        keep = {r[0] for r in (await db.execute(union(
            select(PropertyInteraction.property_id).where(PropertyInteraction.collection_id == cid, or_(
                PropertyInteraction.liked.is_(True), PropertyInteraction.agent_recommended_at.is_not(None))),
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


async def scheduled_remove_homes_outside_area() -> None:
    """Right after start - see main.py."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            done = await remove_homes_outside_area(db)
        if done["homes"]:
            logger.info("Removed homes outside their Showcase's towns: %s", done)
    except Exception:  # noqa: BLE001
        logger.exception("removing homes outside their Showcase's towns failed")
