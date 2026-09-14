import os
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, and_, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models.database import (
    CollectionChange, Collection, Property, PropertyInteraction,
    ScheduledEmail, collection_properties,
)
from app.services.blacklist_service import BlacklistService
from app.config.logging import get_logger

logger = get_logger(__name__)


def count_label(n: int, singular: str, plural: str) -> str:
    """"" when 0, else "n singular/plural" (drives the template {{#if label}} guards)."""
    if not n:
        return ""
    return f"{n} {singular if n == 1 else plural}"


def _format_price(price: Optional[float]) -> str:
    """Full formatted price, e.g. '$750,000'. Empty string when unknown."""
    if not price:
        return ""
    return f"${price:,.0f}"


class DailyDigestService:
    """
    Drains un-notified CollectionChange rows once per day into:
      * one visitor email per changed showcase (template: showcase_daily_digest)
      * one roll-up email per agent across all their changed showcases
        (template: showcase_daily_digest_agent)
    Delivery still flows through the ScheduledEmail queue / EmailSchedulerService.
    """

    @staticmethod
    def _frontend_url() -> str:
        return os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))

    @staticmethod
    def _visitor_subject(new_count: int, drop_count: int, collection_name: str) -> str:
        """Dynamic subject; never mentions price drops when there are none."""
        if new_count and not drop_count:
            return random.choice([
                count_label(new_count, "new home", "new homes") + f" in your {collection_name} showcase",
                f"Your {collection_name} showcase has " + count_label(new_count, "new match", "new matches"),
            ])
        if drop_count and not new_count:
            return random.choice([
                "Price drop on " + count_label(drop_count, "home", "homes") + " you're watching",
                count_label(drop_count, "price drop", "price drops") + f" in your {collection_name} showcase",
            ])
        # both
        return (count_label(new_count, "new home", "new homes")
                + " + " + count_label(drop_count, "price drop", "price drops")
                + " in your showcase")

    @staticmethod
    def _agent_subject(collection_count: int) -> str:
        """Deterministic prefix so agents can filter/label consistently."""
        return "Daily digest: " + count_label(collection_count, "client had showcase updates",
                                               "clients had showcase updates")

    @staticmethod
    def _property_var(prop: Property, change: CollectionChange) -> Dict[str, Any]:
        """Build one entry for the template `properties[]` array."""
        is_drop = change.change_type == "PRICE_DROP"
        old_p = change.old_price
        new_p = change.new_price if change.new_price is not None else prop.price
        savings = (old_p - new_p) if (is_drop and old_p and new_p and old_p > new_p) else 0
        return {
            "is_price_drop": is_drop,
            "street_address": prop.street_address,
            "city": prop.city,
            "state": prop.state,
            "image": prop.img_src or "",
            "beds": prop.bedrooms,
            "baths": prop.bathrooms,
            "sqft": prop.living_area,
            "price": _format_price(prop.price),
            "old_price": _format_price(old_p) if is_drop else "",
            "new_price": _format_price(new_p) if is_drop else "",
            "savings": _format_price(savings) if savings > 0 else "",
        }

    @classmethod
    async def send_daily_digests(cls) -> Dict[str, int]:
        """Entry point for the scheduler. Aggregates open changes and enqueues digests."""
        stats = {"collections": 0, "visitor_emails": 0, "agent_emails": 0}
        frontend_url = cls._frontend_url()
        today = datetime.now(timezone.utc).strftime("%m/%d/%Y")

        async with AsyncSessionLocal() as db:
            try:
                # 1. All pending changes, with their collection (+ owner) and property.
                stmt = (
                    select(CollectionChange, Collection, Property)
                    .join(Collection, CollectionChange.collection_id == Collection.id)
                    .join(Property, CollectionChange.property_id == Property.id)
                    .options(selectinload(Collection.owner))
                    .where(
                        and_(
                            CollectionChange.notified_at.is_(None),
                            Collection.status == 'ACTIVE',
                        )
                    )
                )
                rows = (await db.execute(stmt)).all()
                if not rows:
                    logger.info("Daily digest: no pending showcase changes.")
                    return stats

                # 2. Which (collection, property) pairs did the visitor dislike? Exclude them.
                disliked = await cls._disliked_pairs(db, rows)

                # 3. Group by collection.
                grouped: Dict[str, Dict[str, Any]] = {}
                included_change_ids: List[str] = []
                for change, collection, prop in rows:
                    # Always mark the change handled so it doesn't linger, even if filtered out.
                    included_change_ids.append(change.id)
                    if (collection.id, prop.id) in disliked:
                        continue
                    g = grouped.setdefault(collection.id, {"collection": collection, "props": []})
                    g["props"].append(cls._property_var(prop, change))

                # 4. Build & enqueue emails.
                now = datetime.now(timezone.utc)
                agent_buckets: Dict[str, Dict[str, Any]] = {}

                for cid, g in grouped.items():
                    collection: Collection = g["collection"]
                    props: List[Dict[str, Any]] = g["props"]
                    if not props:
                        continue
                    stats["collections"] += 1

                    new_count = sum(1 for p in props if not p["is_price_drop"])
                    drop_count = sum(1 for p in props if p["is_price_drop"])
                    total_count = (await db.execute(
                        select(func.count()).select_from(collection_properties)
                        .where(collection_properties.c.collection_id == collection.id)
                    )).scalar() or len(props)
                    owner = collection.owner

                    # --- Visitor email (one per changed showcase) ---
                    if collection.visitor_email and getattr(collection, 'notify_visitor', True):
                        if await BlacklistService.is_blacklisted(db, collection.visitor_email):
                            logger.info(f"Digest: skipping blacklisted visitor {collection.visitor_email}")
                        else:
                            visitor_vars = {
                                "recipient_name": collection.visitor_name or "Valued Visitor",
                                "collection_name": collection.name,
                                "collection_link": f"{frontend_url}/showcase/{collection.share_token}",
                                "total_count": total_count,
                                "today_date": today,
                                "agent_name": f"{owner.first_name} {owner.last_name}" if owner else "Your Agent",
                                "agent_email": owner.email if owner else "",
                                "agent_phone": getattr(owner, 'phone', "") if owner else "",
                                "Unsub": f"{frontend_url}/unsubscribe?email={collection.visitor_email}",
                                "new_label": count_label(new_count, "new home", "new homes"),
                                "drop_label": count_label(drop_count, "price drop", "price drops"),
                                "properties": props,
                            }
                            db.add(ScheduledEmail(
                                recipient_email=collection.visitor_email,
                                subject=cls._visitor_subject(new_count, drop_count, collection.name),
                                template_name="showcase_daily_digest",
                                template_variables=visitor_vars,
                                status="PENDING",
                                scheduled_for=now,
                            ))
                            stats["visitor_emails"] += 1

                    # --- Accumulate for the agent roll-up ---
                    if owner and owner.email and getattr(collection, 'notify_agent', True):
                        bucket = agent_buckets.setdefault(owner.id, {"owner": owner, "collections": [],
                                                                      "total_new": 0, "total_drops": 0})
                        bucket["collections"].append({
                            "visitor_name": collection.visitor_name or "",
                            "collection_name": collection.name,
                            "collection_link": f"{frontend_url}/showcases?showcase={collection.id}",
                            "new_label": count_label(new_count, "new", "new"),
                            "drop_label": count_label(drop_count, "price drop", "price drops"),
                            "properties": props,
                        })
                        bucket["total_new"] += new_count
                        bucket["total_drops"] += drop_count

                # --- Agent roll-up emails (one per agent) ---
                for owner_id, bucket in agent_buckets.items():
                    owner = bucket["owner"]
                    collection_count = len(bucket["collections"])
                    agent_vars = {
                        "recipient_name": owner.first_name,
                        "today_date": today,
                        "dashboard_link": f"{frontend_url}/showcases",
                        # Also used by EmailService's plain-text fallback ("View it here")
                        "showcase_link": f"{frontend_url}/showcases",
                        "collection_label": count_label(collection_count, "client showcase", "client showcases"),
                        "total_new_label": count_label(bucket["total_new"], "new listing", "new listings"),
                        "total_drop_label": count_label(bucket["total_drops"], "price drop", "price drops"),
                        "collections": bucket["collections"],
                    }
                    db.add(ScheduledEmail(
                        recipient_email=owner.email,
                        subject=cls._agent_subject(collection_count),
                        template_name="showcase_daily_digest_agent",
                        template_variables=agent_vars,
                        status="PENDING",
                        scheduled_for=now,
                    ))
                    stats["agent_emails"] += 1

                # 5. Mark every processed change as notified (idempotent).
                if included_change_ids:
                    await db.execute(
                        update(CollectionChange)
                        .where(CollectionChange.id.in_(included_change_ids))
                        .values(notified_at=now)
                    )

                await db.commit()
                logger.info(
                    "Daily digest complete",
                    extra={
                        "collections": stats["collections"],
                        "visitor_emails": stats["visitor_emails"],
                        "agent_emails": stats["agent_emails"],
                    },
                )
                return stats

            except Exception as e:
                await db.rollback()
                logger.error("Daily digest failed", exc_info=True, extra={"error": str(e)})
                return stats

    @staticmethod
    async def _disliked_pairs(db: AsyncSession, rows) -> set:
        """Return the set of (collection_id, property_id) the visitor disliked."""
        pair_keys = {(c.collection_id, c.property_id) for c, _col, _p in rows}
        if not pair_keys:
            return set()
        collection_ids = {ck[0] for ck in pair_keys}
        property_ids = {ck[1] for ck in pair_keys}
        stmt = select(PropertyInteraction.collection_id, PropertyInteraction.property_id).where(
            and_(
                PropertyInteraction.collection_id.in_(collection_ids),
                PropertyInteraction.property_id.in_(property_ids),
                PropertyInteraction.disliked == True,
            )
        )
        result = await db.execute(stmt)
        return {(row.collection_id, row.property_id) for row in result.all()}
