"""Admin-only agent activity tracker - what every agent uses, how often, and whether it's working
(open house sign-ins, leads). Agents' activity only: never their clients' names or contact info
(see admin_activity_service)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import admin_activity_service as svc
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/activity", tags=["admin"])


@router.get("")
async def activity_overview(db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    return await svc.overview_and_agents(db)


@router.get("/agents/{agent_id}")
async def activity_agent(agent_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    out = await svc.agent_detail(db, agent_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return out
