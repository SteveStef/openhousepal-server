"""Public Guest Pass endpoints (see app/services/guest_pass_service.py). The {token} in every path
is the signed link a guest gets right after signing in at an open house."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import guest_pass_service as gp

router = APIRouter()


class GuestPassRequest(BaseModel):
    kind: Literal["disclosure", "second_visit"]
    # Second tour only: the two times the guest picked, e.g. "Sat, Oct 4 - Afternoon  or  Sun, Oct 5 - Morning".
    preferred_time: Optional[str] = Field(default=None, max_length=200)


class GuestPassQuestion(BaseModel):
    message: str


@router.get("/guest-pass/{token}")
async def get_guest_pass(token: str, db: AsyncSession = Depends(get_db)):
    visitor, oh, host = await gp.resolve(db, token)
    return await gp.payload(db, visitor, oh, host)


@router.get("/guest-pass/{token}/agent-busy")
async def guest_pass_agent_busy(token: str, db: AsyncSession = Depends(get_db)):
    """The agent's busy blocks (start/end only) so the client's time picker only offers times the
    agent is actually free (user, 10/18). [] when no calendar is connected."""
    from app.services import calendar_sync_service
    _visitor, oh, _host = await gp.resolve(db, token)
    return {"busy": await calendar_sync_service.public_busy(db, oh.agent_id, 11)}


@router.post("/guest-pass/{token}/request")
async def guest_pass_request(token: str, body: GuestPassRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """One-tap "request the seller disclosure" / "request a second tour" with their sign-in info."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    visitor, oh, _host = await gp.resolve(db, token)
    return await gp.send_request(db, visitor, oh, body.kind, body.preferred_time)


@router.post("/guest-pass/{token}/showcase")
async def guest_pass_showcase(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Their Showcase link - built now if the sign-in didn't make one."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    visitor, oh, host = await gp.resolve(db, token)
    return await gp.open_showcase(db, visitor, oh, host)


@router.post("/guest-pass/{token}/question")
async def guest_pass_question(token: str, body: GuestPassQuestion, request: Request, db: AsyncSession = Depends(get_db)):
    """"Ask {agent} a quick question" - sent as them, from their sign-in info."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    visitor, oh, host = await gp.resolve(db, token)
    return await gp.ask_question(db, visitor, oh, host, body.message)
