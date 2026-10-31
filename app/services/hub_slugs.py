"""Agent Hub link names (owner, 10/30): one place for "which hub is this name?" and "is this name free?".

The rule:
  - an agent's CURRENT link and their ONE most recent old link both open their hub (the old one keeps
    every card, signature and printed QR code working after a rename);
  - nobody else can take either of them;
  - renaming again keeps only the newest old link - the one before it is let go (free for anyone).
Every public page that finds a hub by name (hub, forms, blogs, visit counts, own-domain forwarding)
goes through hub_by_slug, so an old link works everywhere the current one does.
"""
from typing import Optional

from sqlalchemy import cast, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentHub


async def hub_by_slug(db: AsyncSession, slug: str) -> Optional[AgentHub]:
    """The hub using this name now, else the hub whose most recent old link it is."""
    name = (slug or "").strip().lower()
    if not name:
        return None
    hub = (await db.execute(select(AgentHub).where(AgentHub.slug == name))).scalar_one_or_none()
    if hub:
        return hub
    return (await db.execute(
        select(AgentHub).where(AgentHub.previous_slugs.op('@>')(cast([name], JSONB)))
    )).scalars().first()


async def slug_held_by_another_agent(db: AsyncSession, slug: str, owner_id: Optional[str]) -> bool:
    """True when another agent's hub uses this name now or as its kept old link."""
    name = (slug or "").strip().lower()
    held = (await db.execute(select(AgentHub.id).where(
        or_(AgentHub.slug == name, AgentHub.previous_slugs.op('@>')(cast([name], JSONB))),
        AgentHub.owner_id != owner_id,
    ))).first()
    return bool(held)


def renamed_previous_slugs(current: str, new: str) -> list[str]:
    """The old-links list after renaming `current` -> `new`: just the link they're leaving
    (nothing when they rename back to the same name)."""
    return [] if current == new else [current]
