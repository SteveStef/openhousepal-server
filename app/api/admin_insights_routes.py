"""Admin Insights - site-wide numbers on what agents and their guests do (admin_insights_service).
Agents are named; their clients and guests never are."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import admin_insights_service as svc
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/insights", tags=["admin"])


@router.get("/{page}")
async def insights(page: str, days: int = Query(30), db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    build = svc.BUILDERS.get(page)
    if build is None:
        raise HTTPException(status_code=404, detail="No such Insights page")
    if days not in svc.PERIODS:
        raise HTTPException(status_code=400, detail=f"days must be one of {svc.PERIODS}")
    return await build(db, days)
