"""Admin-only media storage analytics - per-agent totals, category breakdown, and a month-by-
month "bytes added" history, all read from storage_items (see storage_ledger_service.py, the
single source of truth for stored bytes). Deliberately metadata-only: category + size + created_at,
never the file contents."""
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import StorageItem, User as UserModel
from app.schemas.storage import AdminStorageAgentDetail, AdminStorageAgentSummary, AdminStorageCategoryStat, AdminStorageMonthlyPoint
from app.services.ai.ai_config_service import ai_config_service
from app.services.storage_ledger_service import cap_bytes
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/storage", tags=["admin", "storage"])
logger = get_logger(__name__)


@router.get("/agents", response_model=List[AdminStorageAgentSummary])
async def list_agent_storage_usage(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        stmt = (
            select(
                StorageItem.owner_id,
                UserModel.first_name,
                UserModel.last_name,
                UserModel.email,
                UserModel.plan_tier,
                func.coalesce(func.sum(StorageItem.size_bytes), 0),
                func.count(StorageItem.id),
            )
            .join(UserModel, UserModel.id == StorageItem.owner_id)
            .group_by(StorageItem.owner_id, UserModel.first_name, UserModel.last_name, UserModel.email, UserModel.plan_tier)
            .order_by(func.coalesce(func.sum(StorageItem.size_bytes), 0).desc())
        )
        rows = (await db.execute(stmt)).all()
        config = await ai_config_service.get_config(db)
        return [
            AdminStorageAgentSummary(
                owner_id=owner_id,
                name=f"{first} {last}".strip(),
                email=email,
                plan_tier=plan_tier,
                used_bytes=int(used or 0),
                cap_bytes=cap_bytes(config, plan_tier),
                item_count=int(count or 0),
            )
            for owner_id, first, last, email, plan_tier, used, count in rows
        ]
    except Exception as e:
        logger.error(f"Failed to list agent storage usage: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load agent storage usage")


@router.get("/agents/{owner_id}", response_model=AdminStorageAgentDetail)
async def get_agent_storage_usage_detail(
    owner_id: str,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        user = (await db.execute(select(UserModel).where(UserModel.id == owner_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

        cat_rows = (
            await db.execute(
                select(StorageItem.category, func.coalesce(func.sum(StorageItem.size_bytes), 0), func.count(StorageItem.id))
                .where(StorageItem.owner_id == owner_id)
                .group_by(StorageItem.category)
                .order_by(func.coalesce(func.sum(StorageItem.size_bytes), 0).desc())
            )
        ).all()
        by_category = [AdminStorageCategoryStat(category=cat, bytes=int(b or 0), count=int(c or 0)) for cat, b, c in cat_rows]
        used_bytes = sum(s.bytes for s in by_category)
        item_count = sum(s.count for s in by_category)

        since = (datetime.now(timezone.utc) - timedelta(days=31 * 11)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month = func.to_char(StorageItem.created_at, 'YYYY-MM').label('month')
        monthly_rows = (
            await db.execute(
                select(month, func.coalesce(func.sum(StorageItem.size_bytes), 0))
                .where(StorageItem.owner_id == owner_id, StorageItem.created_at >= since)
                .group_by(month)
                .order_by(month)
            )
        ).all()
        monthly_trend = [AdminStorageMonthlyPoint(month=m, bytes_added=int(b or 0)) for m, b in monthly_rows]

        config = await ai_config_service.get_config(db)
        return AdminStorageAgentDetail(
            owner_id=owner_id,
            name=f"{user.first_name} {user.last_name}".strip(),
            email=user.email,
            plan_tier=user.plan_tier,
            used_bytes=used_bytes,
            cap_bytes=cap_bytes(config, user.plan_tier),
            item_count=item_count,
            by_category=by_category,
            monthly_trend=monthly_trend,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to load agent storage usage detail for {owner_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load agent storage usage detail")
