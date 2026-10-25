"""How many homes a Showcase holds (owner, 10/24): "say 100+ and only have a max of 100 in there".

One number for the saved Showcase (repopulate), both live previews (agent editor + the client's
own search window) and the new-listing sync. A search that matches more keeps the best 100 - the
closest for a distance search (PropertyService nearest-first), the newest otherwise - and every
screen says "100+" with a "tighten your search" note instead of a number.

Homes the client or agent acted on (liked, commented, toured, the agent's own picks) are kept on
top of the 100 - a refresh never takes a saved favorite away.
"""
from sqlalchemy import and_, func, or_, select

SHOWCASE_MAX_HOMES = 100
# Asked of the search: a little more than the limit, so "is there more?" is known even after the
# same-house dedupe drops a few rows.
SHOWCASE_FETCH = SHOWCASE_MAX_HOMES + 25

ACTIVE_STATUSES = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT")


def cap_matches(matches: list) -> tuple[list, bool]:
    """The first SHOWCASE_MAX_HOMES matches (already in best-first order) and whether there were more."""
    return matches[:SHOWCASE_MAX_HOMES], len(matches) > SHOWCASE_MAX_HOMES


def at_limit_from_active(active_count: int) -> bool:
    """A saved Showcase is "100+" once its for-sale homes fill the limit (a refresh keeps 100, and
    the sync adds no more past it). Liked / commented homes kept beyond the 100 count too."""
    return active_count >= SHOWCASE_MAX_HOMES


async def make_room_for_new_listing(db, collection_id: str, new_prop) -> bool:
    """A brand-new listing matched a full Showcase (owner, 10/24 - option A): it takes the place of
    the farthest home (distance search) or the oldest-listed home (any other search) that the client
    hasn't liked, commented on or toured and the agent didn't hand-pick. Quiet - no "removed" email.
    True = there is room now (it wasn't full, or a home made way); False = every home is protected."""
    from app.models.database import (CollectionPreferences, Property, PropertyComment, PropertyInteraction,
                                     PropertyTour, collection_properties)
    from app.utils.geo import distance_miles_sql
    if await active_linked_count(db, collection_id) < SHOWCASE_MAX_HOMES:
        return True
    from sqlalchemy import union
    protected = union(
        select(PropertyInteraction.property_id).where(and_(
            PropertyInteraction.collection_id == collection_id,
            or_(PropertyInteraction.liked.is_(True), PropertyInteraction.agent_recommended_at.is_not(None)))),
        select(PropertyComment.property_id).where(PropertyComment.collection_id == collection_id),
        select(PropertyTour.property_id).where(PropertyTour.collection_id == collection_id),
    ).scalar_subquery()
    q = (select(Property.id)
         .join(collection_properties, collection_properties.c.property_id == Property.id)
         .where(and_(collection_properties.c.collection_id == collection_id,
                     or_(*[Property.home_status == s for s in ACTIVE_STATUSES]),
                     Property.id.notin_(protected),
                     Property.id != new_prop.id)))
    prefs = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == collection_id))).scalar_one_or_none()
    radius_only = bool(prefs and prefs.lat is not None and prefs.long is not None and prefs.diameter
                       and not (prefs.cities or prefs.townships or prefs.counties or prefs.county or prefs.search_polygon))
    if radius_only:
        q = q.order_by(distance_miles_sql(Property.latitude, Property.longitude, float(prefs.lat), float(prefs.long)).desc().nulls_first(), Property.id)
    else:
        q = q.order_by(Property.mls_list_date.asc().nulls_first(), Property.id)
    victim = (await db.execute(q.limit(1))).scalar_one_or_none()
    if victim is None:
        return False
    from sqlalchemy import delete
    await db.execute(delete(collection_properties).where(and_(
        collection_properties.c.collection_id == collection_id, collection_properties.c.property_id == victim)))
    return True


async def active_linked_count(db, collection_id: str) -> int:
    """For-sale homes linked to a Showcase right now (the new-listing sync stops adding at the limit)."""
    from app.models.database import Property, collection_properties
    q = (select(func.count()).select_from(collection_properties)
         .join(Property, Property.id == collection_properties.c.property_id)
         .where(and_(collection_properties.c.collection_id == collection_id,
                     or_(*[Property.home_status == s for s in ACTIVE_STATUSES]))))
    return int((await db.execute(q)).scalar() or 0)
