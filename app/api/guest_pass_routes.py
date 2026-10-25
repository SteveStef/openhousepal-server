"""Public Guest Pass endpoints (see app/services/guest_pass_service.py). The {token} in every path
is the signed link a guest gets right after signing in at an open house."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
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


class GuestPassSeen(BaseModel):
    view: Literal["similar", "cheat", "showcase", "map"]
    key: Optional[str] = Field(default=None, max_length=64)
    # The visit it happened in (the page's own random id) - groups the agent's activity log.
    session: Optional[str] = Field(default=None, max_length=40)


@router.post("/guest-pass/{token}/seen")
async def guest_pass_seen(token: str, body: GuestPassSeen, db: AsyncSession = Depends(get_db)):
    """What they opened on their pass (a similar home, the Cheat Sheet, their Showcase, the map) -
    logged with its time for the agent's activity log (Visitors window), and noted on their open
    follow-up so the agent's alert and suggested text can mention it. Fire-and-forget; each thing
    is noted once, so it needs no rate limit of its own."""
    visitor, oh, host = await gp.resolve(db, token)
    kind = "similar_list" if body.view == "similar" and not body.key else body.view
    await gp.log_event(db, visitor, oh, kind, key=body.key, session=body.session)
    if body.view == "map":
        return {"noted": False}
    return {"noted": await gp.note_activity(db, visitor, oh, host, body.view, body.key)}


class GuestPassPing(BaseModel):
    session: str = Field(max_length=40)


@router.post("/guest-pass/{token}/ping")
async def guest_pass_ping(token: str, body: GuestPassPing, db: AsyncSession = Depends(get_db)):
    """Heartbeat while the Guest Pass is open on the guest's screen (every ~20s) - how long each
    visit lasted and whether they came back another day, for the agent's activity log."""
    visitor, oh, _host = await gp.resolve(db, token)
    await gp.ping(db, visitor, oh, body.session)
    return {"ok": True}


@router.post("/guest-pass/{token}/question")
async def guest_pass_question(token: str, body: GuestPassQuestion, request: Request, db: AsyncSession = Depends(get_db)):
    """"Ask {agent} a quick question" - sent as them, from their sign-in info."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    visitor, oh, host = await gp.resolve(db, token)
    return await gp.ask_question(db, visitor, oh, host, body.message)


# What's nearby on the Guest Pass (user, 10/24) - the same Explore Nearby search as the IDX property
# page and Spotlight (cached, budget-gated, tap-only), around the open house the guest signed in at.
@router.get("/guest-pass/{token}/nearby/availability")
async def guest_pass_nearby_availability(token: str, db: AsyncSession = Depends(get_db)):
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.nearby_amenities_service import is_explore_nearby_available
    _visitor, oh, _host = await gp.resolve(db, token)
    if oh.latitude is None or oh.longitude is None:
        return {"available": False}
    return {"available": await is_explore_nearby_available(db, await ai_config_service.get_config(db))}


@router.get("/guest-pass/{token}/nearby")
async def guest_pass_nearby(token: str, request: Request, response: Response,
                            category: str = Query(..., max_length=40), db: AsyncSession = Depends(get_db)):
    from fastapi import HTTPException
    from app.api.public_nearby_shared import explore_nearby_response, visitor_device_id
    from app.utils.request_ip import get_client_ip
    _visitor, oh, host = await gp.resolve(db, token)
    if oh.latitude is None or oh.longitude is None:
        raise HTTPException(status_code=422, detail="This home doesn't have a map location.")
    return await explore_nearby_response(db, float(oh.latitude), float(oh.longitude), category,
                                         get_client_ip(request), visitor_device_id(request, response), host.id)


@router.get("/guest-pass/{token}/places/{place_id}/details")
async def guest_pass_place_details(token: str, place_id: str, request: Request, response: Response,
                                   db: AsyncSession = Depends(get_db)):
    from app.api.public_nearby_shared import explore_place_details_response, visitor_device_id
    from app.utils.request_ip import get_client_ip
    _visitor, _oh, host = await gp.resolve(db, token)
    return await explore_place_details_response(db, place_id, get_client_ip(request), visitor_device_id(request, response), host.id)
