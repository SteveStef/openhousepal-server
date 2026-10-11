"""One-time setup flow API - see services/setup_flow_service.py.

Open to any signed-in agent, including one still waiting for broker approval or who hasn't picked
a plan yet: the shared "about you" steps can be done ahead of time, so everything is ready the
moment they're approved. Plan-specific steps go through each feature's own (plan-gated) endpoint.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.services import s3_service, setup_flow_service as flow
from app.utils.auth import get_current_active_user
from app.utils.image_validation import assert_valid_jpeg

router = APIRouter(prefix="/setup", tags=["setup"])


async def _hub(db: AsyncSession, user: User):
    from app.api.agent_hub_routes import _ensure_hub
    hub = await _ensure_hub(db, user)
    if flow.seed_hub_from_user(user, hub):
        await db.commit()
    return hub


@router.get("")
async def get_setup(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    hub = await _hub(db, current_user)
    return await flow.compute(db, current_user, hub)


class InfoPatch(BaseModel):
    profile: Optional[dict[str, Any]] = None
    contact: Optional[dict[str, Any]] = None
    card: Optional[dict[str, Any]] = None
    socialLinks: Optional[list[dict[str, Any]]] = None
    photoPosition: Optional[dict[str, Any]] = None
    look: Optional[dict[str, Any]] = None


@router.patch("/info")
async def patch_info(payload: InfoPatch, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    hub = await _hub(db, current_user)
    await flow.apply_info(db, current_user, hub, payload.model_dump(exclude_none=True))
    await db.commit()
    return await flow.compute(db, current_user, hub)


class StatePatch(BaseModel):
    last_step: Optional[str] = Field(default=None, max_length=40)
    skip: Optional[str] = Field(default=None, max_length=40)
    unskip: Optional[str] = Field(default=None, max_length=40)
    confirm: Optional[str] = Field(default=None, max_length=40)
    # steps the agent has now seen in the flow - later (after an upgrade) the ones they haven't
    # seen yet are marked "New"
    seen: Optional[list[str]] = None


@router.put("/state")
async def put_state(payload: StatePatch, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    st = flow.state_of(current_user)
    known = set(flow.STEP_ORDER)
    if payload.last_step in known:
        st["last_step"] = payload.last_step
    if payload.skip in known and payload.skip not in flow.REQUIRED and payload.skip not in st["skipped"]:
        st["skipped"] = [*st["skipped"], payload.skip]
    if payload.unskip in known:
        st["skipped"] = [s for s in st["skipped"] if s != payload.unskip]
    if payload.confirm in flow.CONFIRM_STEPS and payload.confirm not in st["confirmed"]:
        st["confirmed"] = [*st["confirmed"], payload.confirm]
        st["skipped"] = [s for s in st["skipped"] if s != payload.confirm]
    if payload.seen:
        st["seen"] = sorted(set(st.get("seen") or []) | {s for s in payload.seen if s in known})
    current_user.setup_state = st
    await db.commit()
    hub = await _hub(db, current_user)
    return await flow.compute(db, current_user, hub)


@router.post("/photo")
async def upload_photo(file: UploadFile = File(...), db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Same rules as the Agent Hub photo upload, but open before a plan is active, and it writes
    the hub copy too so every feature shows the new photo at once."""
    from app.api.agent_hub_routes import PHOTO_CONTENT_TYPES, PHOTO_MAX_BYTES
    extension = PHOTO_CONTENT_TYPES.get(file.content_type)
    if not extension:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Please choose a JPG photo.")
    contents = await file.read()
    if len(contents) > PHOTO_MAX_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That photo is too big - please pick one under 5MB.")
    try:
        assert_valid_jpeg(contents)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    old = current_user.photo_url
    url = s3_service.upload_bytes(contents, "agent-photos", extension, file.content_type)
    current_user.photo_url = url
    hub = await _hub(db, current_user)
    cfg = dict(hub.config or {})
    cfg["profilePhotoUrl"] = url
    hub.config = cfg
    await db.commit()
    if old and old != url:
        s3_service.delete_by_url(old)
        from app.services import storage_ledger_service
        await storage_ledger_service.release(db, old)
    from app.services import storage_ledger_service
    await storage_ledger_service.record(db, current_user.id, "agent_photo", url, len(contents))
    return await flow.compute(db, current_user, hub)
