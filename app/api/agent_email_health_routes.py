"""Agent email health: the open pixel + tracked button on emails TO agents, and the agent's own
banner controls (agent_email_health_service)."""
import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import agent_email_health_service as health
from app.utils.auth import get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent-email-health"])

_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, private", "Pragma": "no-cache"}


@router.get("/e/a/{log_id}.gif")
async def agent_open(log_id: str, db: AsyncSession = Depends(get_db)):
    try:
        await health.record_open(db, log_id[:64])
    except Exception:  # noqa: BLE001 - the image always loads
        logger.warning("agent open tracking failed", exc_info=True)
    return Response(content=_GIF, media_type="image/gif", headers=_NO_CACHE)


@router.get("/e/a/{log_id}/go")
async def agent_click(log_id: str, to: Optional[str] = Query(None), db: AsyncSession = Depends(get_db)):
    url = health.safe_target(to)   # only ever back into the app
    try:
        await health.record_open(db, log_id[:64], click_to=url)
    except Exception:  # noqa: BLE001 - the link always works
        logger.warning("agent click tracking failed", exc_info=True)
    return RedirectResponse(url, status_code=302, headers=_NO_CACHE)


@router.get("/me/email-health")
async def my_email_health(db: AsyncSession = Depends(get_db), user=Depends(get_current_active_user)):
    return await health.status(db, user)


@router.post("/me/email-health/dismiss")
async def dismiss_email_health(db: AsyncSession = Depends(get_db), user=Depends(get_current_active_user)):
    return await health.dismiss(db, user)


@router.post("/me/email-health/restore")
async def restore_email_health(db: AsyncSession = Depends(get_db), user=Depends(get_current_active_user)):
    return await health.restore(db, user)


@router.post("/me/email-health/test")
async def test_email_health(db: AsyncSession = Depends(get_db), user=Depends(get_current_active_user)):
    return await health.send_test(db, user)
