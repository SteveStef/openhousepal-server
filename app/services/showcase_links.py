"""Short, personal Showcase links (user, 10/18: the long /showcase/coll-Ab3dE... link in the invite
email "looks very ugly and honestly not very safe").

    openhousepal.com/s/dana-k7q2      (or the agent's own IDX address)

The client's first name, then 4 random characters - reads like a personal link, and two clients
named Dana never collide. The short link redirects to the Showcase (app/s/[code]); every old
/showcase/coll-... link keeps working.

A code is minted once per Showcase, in its own committed session BEFORE any link carrying it goes
out - a caller whose own transaction later rolls back can never send a short link that points at
nothing.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from sqlalchemy import select

from app.models.database import Collection

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i - easy to read out loud


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
    """'/s/<code>' for a Showcase (the long '/showcase/<token>' only if a code can't be made).
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
        return f"/s/{code}"
    return f"/showcase/{token}" if token else None
