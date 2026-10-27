"""Today's Rundown - the daily briefing inside the app (services/rundown_service.py)."""
import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import User
from app.services.rundown_service import build_rundown, mark_seen, mark_today_read, todays_status
from app.utils.auth import get_current_active_user

logger = get_logger(__name__)
router = APIRouter()

# Requests that arrive together (the Dash card, the window, several tabs) build the Rundown once,
# and the same copy is handed out for a few seconds (owner, 10/26: four at once on the live site,
# 1-7 s each). "Mark all as read" drops it so the next one is fresh.
RECENT_S = 10.0
_recent: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


@router.get("/api/v1/rundown/today")
async def todays_rundown(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    try:
        lock = _locks.setdefault(current_user.id, asyncio.Lock())
        async with lock:
            hit = _recent.get(current_user.id)
            if hit and time.monotonic() - hit[0] < RECENT_S:
                return hit[1]
            data = await build_rundown(db, current_user)
            _recent[current_user.id] = (time.monotonic(), data)
            if len(_recent) > 500:
                _recent.clear()
            return data
    except Exception as e:  # noqa: BLE001
        logger.error("building today's rundown failed", exc_info=True, extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Couldn't build today's rundown")


@router.post("/api/v1/rundown/today/read")
async def todays_rundown_read(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Mark all as read: today's notifications (the Rundown's own included) - and that counts as
    having looked at today's Rundown."""
    _recent.pop(current_user.id, None)
    marked = await mark_today_read(db, current_user)
    await mark_seen(db, current_user)
    return {"success": True, "marked": marked}


@router.post("/api/v1/rundown/today/seen")
async def todays_rundown_seen(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """The agent opened today's Rundown in the app - the Dash card becomes the header button."""
    _recent.pop(current_user.id, None)
    await mark_seen(db, current_user)
    return {"success": True}


@router.get("/api/v1/rundown/today/status")
async def todays_rundown_status(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """The header "Briefing" button: seen today yet, and how many new things since."""
    return await todays_status(db, current_user)
