"""Per-agent, cross-app media storage ledger.

Every user-facing media upload or generation in the app writes one `storage_items` row here
(AI Media, Design Studio, Blogs, brand logos, manual listing photos, agent / sale photos).
This is the single source of truth for "how much has this agent stored", enforced against a
per-tier cap. Rows are keyed by S3 URL so any delete path can release its bytes by URL.

record() / release() are best-effort - a ledger hiccup must never break the upload or delete
that triggered it. enforce_cap() is the hard gate, called before an upload is accepted.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import StorageItem

logger = get_logger(__name__)

# Fallback when ai_config has no per-tier value. Applies to every agent.
DEFAULT_CAP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


async def record(db: AsyncSession, owner_id: str, category: str, s3_url: str, size_bytes: int) -> None:
    """Add (or update) one stored file for an agent. Idempotent on s3_url."""
    if not owner_id or not s3_url:
        return
    try:
        stmt = pg_insert(StorageItem).values(
            id=str(uuid.uuid4()), owner_id=owner_id, category=category,
            s3_url=s3_url, size_bytes=int(size_bytes or 0),
        ).on_conflict_do_update(
            index_elements=[StorageItem.s3_url],
            set_={"size_bytes": int(size_bytes or 0), "category": category, "owner_id": owner_id},
        )
        await db.execute(stmt)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("storage_ledger.record failed", extra={"url": s3_url, "error": str(exc)})


async def release(db: AsyncSession, s3_url: Optional[str]) -> None:
    """Drop a file from the ledger (called from every delete path). No-op if it isn't tracked."""
    if not s3_url:
        return
    try:
        await db.execute(sa_delete(StorageItem).where(StorageItem.s3_url == s3_url))
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("storage_ledger.release failed", extra={"url": s3_url, "error": str(exc)})


async def total_bytes(db: AsyncSession, owner_id: str) -> int:
    row = await db.execute(
        select(func.coalesce(func.sum(StorageItem.size_bytes), 0)).where(StorageItem.owner_id == owner_id)
    )
    return int(row.scalar_one() or 0)


async def usage(db: AsyncSession, owner_id: str) -> dict:
    rows = (await db.execute(
        select(StorageItem.category, func.coalesce(func.sum(StorageItem.size_bytes), 0), func.count(StorageItem.id))
        .where(StorageItem.owner_id == owner_id)
        .group_by(StorageItem.category)
    )).all()
    by_category = {cat: {"bytes": int(b or 0), "count": int(c or 0)} for cat, b, c in rows}
    return {
        "used_bytes": sum(v["bytes"] for v in by_category.values()),
        "by_category": by_category,
    }


def cap_bytes(config: dict, plan_tier: Optional[str]) -> int:
    by_tier = (config or {}).get("agent_storage_cap_bytes_by_tier") or {}
    if plan_tier and plan_tier in by_tier:
        return int(by_tier[plan_tier] or DEFAULT_CAP_BYTES)
    return int((config or {}).get("agent_storage_cap_bytes_default") or DEFAULT_CAP_BYTES)


async def enforce_cap(db: AsyncSession, owner_id: str, config: dict, plan_tier: Optional[str],
                      incoming_bytes: int = 0) -> None:
    """Raise 507 if this upload would put the agent over their storage cap."""
    cap = cap_bytes(config, plan_tier)
    if cap <= 0:
        return
    used = await total_bytes(db, owner_id)
    if used + max(0, int(incoming_bytes or 0)) > cap:
        gb = cap / (1024 * 1024 * 1024)
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            f"You've used your {gb:.1f} GB of media storage. Delete some photos, videos, blog "
            f"images or designs to free space, then try again.",
        )
