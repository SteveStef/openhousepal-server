"""The chat's "Try asking" ideas, picked from the agent's own account (owner, 11/04: "more tailored to
their situation ... a new listing but no Spotlight, forms filled out they should recap"). Plain
database checks - no AI, so it costs nothing. Most useful first; topped up with plan-fitting how-tos
that change from day to day."""
import random
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import (AgentHub, AgentReview, AgentTask, Blog, Collection, DesignProject, FormSubmission, Lead,
                                 PropertyWebsite, Site, User)
from app.utils.auth import (AGENT_HUB_ACCESS_TIERS, REVIEWS_ACCESS_TIERS, SHOWCASE_ACCESS_TIERS, STUDIO_DESIGN_ACCESS_TIERS,
                            tier_has_mls)

logger = get_logger(__name__)
MAX = 6

# How-tos that fit a plan - fill the rest, a different few each day.
_HOW_TOS = [
    ("How do I run an open house with OpenHousePal?", None),
    ("Remind me to call my newest lead tomorrow at 10", None),
    ("Make me a form for buyers at my open house", None),
    ("Where do I change my email signature?", {"IDX", "PRO_NO_MLS"}),
    ("How do I make a Showcase for a client?", SHOWCASE_ACCESS_TIERS),
    ("How do I set up my Agent Hub?", AGENT_HUB_ACCESS_TIERS),
    ("Write a blog post about my market", STUDIO_DESIGN_ACCESS_TIERS),
    ("Find open houses near me I could host", {"BASIC", "PREMIUM", "IDX"}),
    ("Make an invite for my next open house", None),
]


def _street(addr: str) -> str:
    return (addr or "").split(",")[0].strip().title() or "my newest listing"


async def suggestions_for(db: AsyncSession, user: User) -> List[str]:
    tier = user.plan_tier or ""
    now = datetime.now(timezone.utc)
    out: List[str] = []

    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    try:
        # 1. waiting on them right now
        overdue = await count(select(func.count()).select_from(AgentTask).where(
            AgentTask.owner_id == user.id, AgentTask.completed_at.is_(None), AgentTask.due_at < now))
        if overdue:
            out.append(f"What should I do today? ({overdue} follow-up{'s' if overdue != 1 else ''} waiting)")
        new_answers = await count(select(func.count()).select_from(FormSubmission).where(
            FormSubmission.owner_id == user.id, FormSubmission.is_reviewed.is_(False), FormSubmission.created_at > now - timedelta(days=14)))
        if new_answers >= 2:
            out.append(f"Recap my {new_answers} new form answers")
        elif new_answers == 1:
            out.append("Who filled out my form?")
        new_leads = await count(select(func.count()).select_from(Lead).where(Lead.owner_id == user.id, Lead.created_at > now - timedelta(days=3)))
        if new_leads >= 2:
            out.append(f"Tell me about my {new_leads} newest leads")

        # 2. their newest listing - Spotlight (IDX) and a Just Listed post (Studio plans)
        if user.mls_id and tier_has_mls(tier) and user.broker_authorized:
            from app.services.website import content_adapters as ca
            listings = await ca.get_active_listings(db, user, limit=3)
            if listings:
                newest = listings[0]
                key, street = newest.get("listing_key"), _street(newest.get("address") or newest.get("street_address") or "")
                if tier == "IDX" and key and not await count(select(func.count()).select_from(PropertyWebsite).where(
                        PropertyWebsite.owner_id == user.id, PropertyWebsite.listing_key == key)):
                    out.append(f"Make a Spotlight website for {street}")
                if tier in STUDIO_DESIGN_ACCESS_TIERS and key and not await count(select(func.count()).select_from(DesignProject).where(
                        DesignProject.owner_id == user.id, DesignProject.created_at > now - timedelta(days=45),
                        DesignProject.linked_property["listing_key"].astext == key)):
                    out.append(f"Make me a Just Listed post for {street}")

        # 3. something their plan has that they haven't started
        if tier == "IDX":
            site = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalar_one_or_none()
            if not site or not site.published_at:
                out.append("Help me finish setting up my IDX website")
        if tier in AGENT_HUB_ACCESS_TIERS and not await count(select(func.count()).select_from(AgentHub).where(AgentHub.owner_id == user.id)):
            out.append("How do I set up my Agent Hub?")
        if tier in SHOWCASE_ACCESS_TIERS:
            liked = await count(select(func.count()).select_from(Collection).where(
                Collection.owner_id == user.id, Collection.updated_at > now - timedelta(days=7)))
            if liked:
                out.append("What did my clients like in their Showcases this week?")
        if tier in REVIEWS_ACCESS_TIERS and not await count(select(func.count()).select_from(AgentReview).where(AgentReview.owner_id == user.id)):
            out.append("Help me ask a past client for a review")
        if tier in STUDIO_DESIGN_ACCESS_TIERS and not await count(select(func.count()).select_from(Blog).where(
                Blog.owner_id == user.id, Blog.created_at > now - timedelta(days=30))):
            out.append("Write a blog post about my market")
    except Exception:  # noqa: BLE001 - suggestions are a nicety; the chat opens without them
        logger.warning("ai suggestions failed", exc_info=True)
        await db.rollback()

    out = list(dict.fromkeys(out))[:MAX]
    # top up with how-tos for this plan, a different set each day
    pool = [t for t, plans in _HOW_TOS if (plans is None or tier in plans) and t not in out]
    random.Random(f"{user.id}-{now.date()}").shuffle(pool)
    return out + pool[:MAX - len(out)]
