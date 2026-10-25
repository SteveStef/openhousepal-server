"""Today's Rundown - the daily briefing inside the app (services/rundown_service.py)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import User
from app.services.rundown_service import build_rundown, mark_today_read
from app.utils.auth import get_current_active_user

logger = get_logger(__name__)
router = APIRouter()


@router.get("/api/v1/rundown/today")
async def todays_rundown(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    try:
        return await build_rundown(db, current_user)
    except Exception as e:  # noqa: BLE001
        logger.error("building today's rundown failed", exc_info=True, extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Couldn't build today's rundown")


@router.post("/api/v1/rundown/today/read")
async def todays_rundown_read(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Mark all as read: today's notifications (the Rundown's own included)."""
    return {"success": True, "marked": await mark_today_read(db, current_user)}
