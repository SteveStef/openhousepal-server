"""New matches across an agent's Showcases (owner, 11/04): the Showcases page's mailbox, the 3 homes on
a lead's page, and the AI helper's short answer. One rule for NEW, the same as the Showcase cards
(collections_service): matched in since the agent last opened that Showcase, and that home not opened
by the agent. Reading this never clears anything - only opening the home or the Showcase does."""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Collection, Property, PropertyComment, PropertyInteraction, PropertyTour, collection_properties
from app.services.collections_service import card_new_since, matched_in_at


def _photo(p: Property) -> Optional[str]:
    return p.img_src or next((u for u in (p.photos or []) if u), None)


async def _activity(db: AsyncSession, collection_ids: Sequence[str]) -> Dict[tuple, dict]:
    """What each client did with each home: viewed, liked, passed, commented, asked for a tour."""
    out: Dict[tuple, dict] = {}
    if not collection_ids:
        return out

    def slot(c, p):
        return out.setdefault((c, p), {"viewed": False, "liked": False, "disliked": False, "comments": 0, "tour": False, "agent_viewed": False})

    rows = await db.execute(select(PropertyInteraction.collection_id, PropertyInteraction.property_id, PropertyInteraction.view_count,
                                   PropertyInteraction.liked, PropertyInteraction.disliked, PropertyInteraction.agent_viewed_at)
                            .where(PropertyInteraction.collection_id.in_(collection_ids)))
    for r in rows.all():
        a = slot(r.collection_id, r.property_id)
        a["viewed"], a["liked"], a["disliked"], a["agent_viewed"] = bool(r.view_count), bool(r.liked), bool(r.disliked), bool(r.agent_viewed_at)
    rows = await db.execute(select(PropertyComment.collection_id, PropertyComment.property_id, func.count())
                            .where(PropertyComment.collection_id.in_(collection_ids))
                            .group_by(PropertyComment.collection_id, PropertyComment.property_id))
    for c, p, n in rows.all():
        slot(c, p)["comments"] = int(n)
    rows = await db.execute(select(PropertyTour.collection_id, PropertyTour.property_id).where(PropertyTour.collection_id.in_(collection_ids)))
    for c, p in rows.all():
        slot(c, p)["tour"] = True
    return out


async def _matches(db: AsyncSession, collections: List[Collection], limit_per: Optional[int] = None) -> List[dict]:
    if not collections:
        return []
    by_id = {c.id: c for c in collections}
    rows = (await db.execute(
        select(collection_properties.c.collection_id, collection_properties.c.added_at, Property)
        .join(Property, Property.id == collection_properties.c.property_id)
        .where(collection_properties.c.collection_id.in_(list(by_id)))
        # newest first; the homes a Showcase started with (no date) last, by when they were listed
        .order_by(case((collection_properties.c.added_at.is_(None), 1), else_=0), collection_properties.c.added_at.desc(), Property.mls_list_date.desc().nullslast())
    )).all()
    acts = await _activity(db, list(by_id))
    out, seen = [], {}
    for r in rows:
        c, p = by_id[r.collection_id], r[2]
        if limit_per is not None and seen.get(c.id, 0) >= limit_per:
            continue
        seen[c.id] = seen.get(c.id, 0) + 1
        a = acts.get((c.id, p.id), {})
        m_in = matched_in_at(c, r.added_at)
        out.append({
            "collection_id": c.id, "client": c.visitor_name or c.name, "client_email": c.visitor_email, "showcase": c.name,
            "property_id": p.id, "listing_key": p.listing_key, "address": p.street_address, "city": p.city, "state": p.state, "status": p.home_status,
            "price": p.price, "beds": p.bedrooms, "baths": p.bathrooms, "sqft": p.living_area, "photo": _photo(p),
            "matched_at": m_in.isoformat() if m_in else None,
            "is_new": bool(m_in and m_in > card_new_since(c) and not a.get("agent_viewed")),
            "activity": {k: a.get(k, 0 if k == "comments" else False) for k in ("viewed", "liked", "disliked", "comments", "tour")},
        })
    return out


async def new_matches(db: AsyncSession, owner_id: str, limit: int = 100) -> dict:
    """Every home the agent hasn't looked at yet, newest first."""
    cols = list((await db.execute(select(Collection).where(Collection.owner_id == owner_id, Collection.status == "ACTIVE"))).scalars().all())
    items = [m for m in await _matches(db, cols) if m["is_new"]]
    items.sort(key=lambda m: m["matched_at"] or "", reverse=True)
    return {"count": len(items), "items": items[:limit], "as_of": datetime.now(timezone.utc).isoformat()}


async def recent_for(db: AsyncSession, collections: List[Collection], limit: int = 3) -> List[dict]:
    """The newest homes in these Showcases, seen or not - a lead's page, or one Showcase for the helper."""
    items = await _matches(db, collections, limit_per=limit)
    items.sort(key=lambda m: (m["matched_at"] is not None, m["matched_at"] or ""), reverse=True)
    return items[:limit]
