"""Showcase mailboxes (owner, 11/07). The Showcases page's Activity mailbox and one client's own mailbox
read the same quiet signals here: opening their Showcase, looking at a home's details, and opening or
clicking a Showcase email. These never count as unread - the agent has nothing to act on - so the
Activity button's number stays the likes, comments and requests it has always been."""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ClientEmail, Collection, Lead, LeadEvent, Notification, Property, collection_properties

SHOWCASE_EMAIL_TEMPLATES = {"showcase_daily_digest", "showcase_weekly_digest", "showcase_ready", "showcase_shared"}
VISIT_TYPES = ("showcase_opened", "showcase_view")
EMAIL_TYPES = ("email_opened", "email_clicked")
KIND = {"showcase_opened": "SHOWCASE_OPENED", "showcase_view": "SHOWCASE_VIEW", "email_opened": "SHOWCASE_EMAIL", "email_clicked": "SHOWCASE_EMAIL"}


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _street(address: Optional[str]) -> str:
    return (address or "").split(",")[0].strip().lower()


async def _photos(db: AsyncSession, collection_ids: Sequence[str], addresses: set) -> Dict[str, str]:
    """Street -> photo, from the homes in these Showcases."""
    if not addresses or not collection_ids:
        return {}
    rows = await db.execute(
        select(Property.street_address, Property.img_src)
        .join(collection_properties, collection_properties.c.property_id == Property.id)
        .where(collection_properties.c.collection_id.in_(list(collection_ids)), func.lower(Property.street_address).in_(addresses)))
    return {_street(s): img for s, img in rows.all() if s and img}


async def quiet_activity(db: AsyncSession, owner_id: str, collections: List[Collection], since: datetime, limit: int = 80) -> List[dict]:
    """Visits and Showcase email opens, newest first, for these Showcases."""
    if not collections:
        return []
    by_id = {c.id: c for c in collections}
    by_email: Dict[str, Collection] = {}
    for c in sorted(collections, key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc)):
        if c.visitor_email:
            by_email[_norm(c.visitor_email)] = c   # the newest Showcase for that address wins
    rows = (await db.execute(
        select(LeadEvent, Lead.email).join(Lead, Lead.id == LeadEvent.lead_id)
        .where(Lead.owner_id == owner_id, LeadEvent.occurred_at >= since, LeadEvent.source_type.in_(VISIT_TYPES + EMAIL_TYPES))
        .order_by(LeadEvent.occurred_at.desc()).limit(limit * 6)
    )).all()
    email_ids = [e.source_id for e, _ in rows if e.source_type in EMAIL_TYPES and e.source_id]
    templates: Dict[str, str] = {}
    if email_ids:
        templates = dict((await db.execute(select(ClientEmail.id, ClientEmail.template).where(ClientEmail.id.in_(email_ids)))).all())
    picked = []
    for ev, email in rows:
        if ev.source_type in EMAIL_TYPES:
            if templates.get(ev.source_id) not in SHOWCASE_EMAIL_TEMPLATES:
                continue
            col = by_id.get(ev.collection_id) or by_email.get(_norm(email))
        else:
            col = by_id.get(ev.collection_id)
        if col is not None:
            picked.append((ev, col))
        if len(picked) >= limit:
            break
    photos = await _photos(db, list(by_id), {_street(e.property_address) for e, _ in picked if e.property_address})
    return [{
        "id": f"ev-{ev.id}", "type": KIND[ev.source_type], "title": ev.summary, "message": ev.summary,
        "collection_id": col.id, "showcase_name": col.visitor_name or col.name, "property_id": None,
        "property_address": ev.property_address, "photo": photos.get(_street(ev.property_address)),
        "link": f"/showcases?showcase={col.id}", "is_read": True, "created_at": ev.occurred_at.isoformat(),
    } for ev, col in picked]


def merged(notes: List[dict], quiet: List[dict], limit: int = 120) -> List[dict]:
    return sorted(notes + quiet, key=lambda a: a["created_at"], reverse=True)[:limit]


async def mailbox(db: AsyncSession, collection: Collection, activity_types: Sequence[str]) -> dict:
    """One client's mailbox: their newest homes and everything they did, all in one place."""
    from app.services.new_matches_service import recent_for
    since = datetime.now(timezone.utc) - timedelta(days=180)
    homes = await recent_for(db, [collection], limit=30)
    notes = (await db.execute(
        select(Notification).where(Notification.agent_id == collection.owner_id, Notification.collection_id == collection.id,
                                   Notification.type.in_(activity_types), Notification.created_at >= since)
        .order_by(Notification.created_at.desc()).limit(80))).scalars().all()
    pids = {n.property_id for n in notes if n.property_id}
    photos = dict((await db.execute(select(Property.id, Property.img_src).where(Property.id.in_(pids)))).all()) if pids else {}
    note_rows = [{
        "id": n.id, "type": n.type, "title": n.title, "message": n.message, "collection_id": n.collection_id,
        "showcase_name": n.visitor_name or n.collection_name, "property_id": n.property_id, "property_address": n.property_address,
        "photo": photos.get(n.property_id), "link": n.link, "is_read": n.is_read, "created_at": n.created_at.isoformat(),
    } for n in notes]
    quiet = await quiet_activity(db, collection.owner_id, [collection], since)
    return {"homes": homes, "activity": merged(note_rows, quiet), "new_matches": sum(1 for h in homes if h.get("is_new"))}
