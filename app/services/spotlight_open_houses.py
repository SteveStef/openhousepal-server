"""Open house dates an agent typed into their Listing Spotlight ("Add a date yourself").

They live on the Spotlight's draft (draft_data.open_houses, manual=True - see
property_website_service._clean_open_houses). The Open House Manager, Discovery and the dashboard
read them through here, next to Bright's own open house dates, so a date added in the Spotlight
shows up everywhere an open house does (user, 10/24) - not only on the Spotlight page.

A Spotlight can only be made by the listing's own agent (create_property_website checks it), so
these dates are always the listing agent hosting their own listing.
"""
from __future__ import annotations

from datetime import date, time
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PropertyWebsite


def _parse_time(value) -> Optional[time]:
    try:
        return time.fromisoformat(str(value)[:5]) if value else None
    except ValueError:
        return None


def manual_slots(draft: Optional[dict], today: Optional[date] = None) -> List[dict]:
    """The upcoming agent-added dates on one Spotlight draft, soonest first, as
    {"date": date, "start": time | None, "end": time | None} - the same shape as
    mls_open_houses.upcoming_mls_open_houses."""
    today = today or date.today()
    out: List[dict] = []
    for e in (draft or {}).get("open_houses") or []:
        if not isinstance(e, dict) or not e.get("manual"):
            continue
        try:
            d = date.fromisoformat(str(e.get("date"))[:10])
        except ValueError:
            continue
        if d >= today:
            out.append({"date": d, "start": _parse_time(e.get("start_time")), "end": _parse_time(e.get("end_time"))})
    out.sort(key=lambda s: (s["date"], s["start"] or time.min))
    return out


async def spotlight_slots(db: AsyncSession, listing_keys: Iterable[str], today: Optional[date] = None) -> Dict[str, List[dict]]:
    """{listing_key: [{"date", "start", "end", "owner_id"}]} for every listing in `listing_keys`
    that has a Spotlight with agent-added upcoming dates. Same date+start twice (two Spotlights
    for one home) counts once."""
    keys = [k for k in set(listing_keys or []) if k]
    if not keys:
        return {}
    rows = (await db.execute(
        select(PropertyWebsite.listing_key, PropertyWebsite.owner_id, PropertyWebsite.draft_data)
        .where(PropertyWebsite.listing_key.in_(keys))
    )).all()
    out: Dict[str, List[dict]] = {}
    seen: set = set()
    for listing_key, owner_id, draft in rows:
        for slot in manual_slots(draft, today):
            mark = (listing_key, slot["date"], slot["start"])
            if mark in seen:
                continue
            seen.add(mark)
            out.setdefault(listing_key, []).append({**slot, "owner_id": owner_id})
    for slots in out.values():
        slots.sort(key=lambda s: (s["date"], s["start"] or time.min))
    return out
