"""Short, personal Showcase links (user, 10/18: the long /showcase/coll-Ab3dE... link in the invite
email "looks very ugly and honestly not very safe").

    openhousepal.com/showcase/dana-k7q2              (or the agent's own IDX address)
    openhousepal.com/showcase/dana-k7q2/732-buck-ln  (one home inside it)

The client's first name, then 4 random characters - reads like a personal link, and two clients
named Dana never collide. Every place that looks a Showcase up by its share token also accepts the
code (share_match), so every old /showcase/coll-... link keeps working.

A code is minted once per Showcase, in its own committed session BEFORE any link carrying it goes
out - a caller whose own transaction later rolls back can never send a short link that points at
nothing.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from sqlalchemy import or_, select

from app.models.database import Collection

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i - easy to read out loud


def share_match(token: str):
    """WHERE clause for a public Showcase lookup: its long share token or its short code."""
    return or_(Collection.share_token == token, Collection.short_code == token)


def home_slug(street: Optional[str]) -> str:
    """'732 Buck Ln' -> '732-buck-ln' - the end of a one-home Showcase link. Must match
    homeSlug() in frontend lib/showcaseLink.ts."""
    return re.sub(r"[^a-z0-9]+", "-", (street or "").split(",")[0].lower()).strip("-")[:60]


def home_suffix(street: Optional[str]) -> str:
    """'/732-buck-ln' to add after a Showcase path ('' if the home has no street)."""
    slug = home_slug(street)
    return f"/{slug}" if slug else ""


def _name_part(visitor_name: Optional[str]) -> str:
    first = (visitor_name or "").strip().split(" ")[0].lower()
    first = re.sub(r"[^a-z]", "", first)[:10]
    return first if len(first) >= 2 and first not in ("anonymous", "visitor", "guest") else "home"


async def ensure_short_code(collection_id: str) -> Optional[str]:
    """The Showcase's short code, minting (and committing) one the first time. None if not found."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        row = await s.get(Collection, collection_id)
        if row is None:
            return None
        if row.short_code:
            return row.short_code
        base = _name_part(row.visitor_name)
        for _ in range(12):
            code = f"{base}-{''.join(secrets.choice(_ALPHABET) for _ in range(4))}"
            taken = (await s.execute(select(Collection.id).where(Collection.short_code == code))).first()
            if not taken:
                row.short_code = code
                await s.commit()
                return code
        return None


async def showcase_path(collection: Optional[Collection] = None, *, collection_id: Optional[str] = None,
                        share_token: Optional[str] = None, db=None) -> Optional[str]:
    """'/showcase/<code>' for a Showcase (the long '/showcase/<token>' only if a code can't be made).
    Pass the Collection, or its id, or its share token (with db)."""
    if collection is None and share_token and db is not None:
        collection = (await db.execute(select(Collection).where(Collection.share_token == share_token))).scalars().first()
    cid = collection.id if collection is not None else collection_id
    token = collection.share_token if collection is not None else share_token
    code = getattr(collection, "short_code", None) if collection is not None else None
    if not code and cid:
        try:
            code = await ensure_short_code(cid)
        except Exception:  # noqa: BLE001 - never lose the link over this
            code = None
    if code:
        return f"/showcase/{code}"
    return f"/showcase/{token}" if token else None
