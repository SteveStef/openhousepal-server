"""A person's star and their Showcase's star are one star (user, 10/28): starring - or un-starring -
the lead does the same to every Showcase of theirs, and a Showcase's star does the same to its lead.
Matched by email, the same rule the Leads page uses to pair a lead with its Showcases
(lead_routes._match_collections). Nothing here commits - the caller does."""
from datetime import datetime, timezone
from typing import List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Collection, Lead
from app.services.leads_service import normalize_email


async def star_showcases(db: AsyncSession, owner_id: str, email: str | None, favorite: bool) -> List[Collection]:
    """Star / un-star every Showcase of this person. Starring one that's asleep wakes it, the same
    as starring it on the Showcases page. Returns the woken ones - pass their ids to
    showcase_sleep.catch_up after the commit."""
    email = normalize_email(email)
    if not email:
        return []
    from app.services import showcase_sleep
    cols = (await db.execute(select(Collection).where(
        Collection.owner_id == owner_id, func.lower(func.trim(Collection.visitor_email)) == email))).scalars().all()
    woken = []
    for col in cols:
        if bool(col.favorited_at) == favorite:
            continue
        col.favorited_at = datetime.now(timezone.utc) if favorite else None
        if favorite and col.status == "INACTIVE":
            await showcase_sleep.agent_changed(db, col, "ACTIVE")
            col.status = "ACTIVE"
            woken.append(col)
    return woken


async def star_lead(db: AsyncSession, owner_id: str, email: str | None, favorite: bool) -> None:
    """Star / un-star the lead this Showcase belongs to."""
    email = normalize_email(email)
    if not email:
        return
    lead = (await db.execute(select(Lead).where(Lead.owner_id == owner_id, Lead.email == email))).scalars().first()
    if lead is not None:
        lead.is_favorite = favorite
