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
    # "I don't use this" on a connect step (and its undo)
    not_using: Optional[str] = Field(default=None, max_length=40)
    use_again: Optional[str] = Field(default=None, max_length=40)
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
    if payload.not_using in flow.NOT_USING_STEPS and payload.not_using not in st["not_using"]:
        st["not_using"] = [*st["not_using"], payload.not_using]
        st["skipped"] = [s for s in st["skipped"] if s != payload.not_using]
    if payload.use_again:
        st["not_using"] = [s for s in st["not_using"] if s != payload.use_again]
    if payload.seen:
        st["seen"] = sorted(set(st.get("seen") or []) | {s for s in payload.seen if s in known})
    current_user.setup_state = st
    await db.commit()
    hub = await _hub(db, current_user)
    return await flow.compute(db, current_user, hub)


@router.get("/hub-link")
async def get_hub_link(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """The Agent Hub step's "Your link" note: the address everything they share uses (their own
    domain once it's connected, IDX only) and what's left to do."""
    hub = await _hub(db, current_user)
    return await flow.hub_link_info(db, current_user, hub)


class WebsiteSalesPatch(BaseModel):
    on: bool


@router.get("/website-sales")
async def get_website_sales(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Whether the IDX website's Recently Sold sections are showing (the Sales step's "Website")."""
    return await flow.website_sales_state(db, current_user)


@router.put("/website-sales")
async def put_website_sales(payload: WebsiteSalesPatch, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    return await flow.set_website_sales(db, current_user, payload.on)


KIT_PREF_KEYS = ("flyerBrand", "qrDark", "matchScore", "recap", "bragStats")


def _kit_prefs_of(user: User) -> dict:
    raw = (user.setup_state or {}).get("kit_prefs") if isinstance(user.setup_state, dict) else None
    return {k: v for k, v in (raw or {}).items() if k in KIT_PREF_KEYS and isinstance(v, bool)}


@router.get("/kit-prefs")
async def get_kit_prefs(current_user: User = Depends(get_current_active_user)):
    """The open house kit's print choices (sign-in look, Brand, Match %, Recap, Sold Stats), saved
    to the ACCOUNT so every computer / phone starts a kit the same way. Only the choices the agent
    has made are returned - anything missing uses the kit's own default. users.setup_state["kit_prefs"]
    (no migration)."""
    return {"prefs": _kit_prefs_of(current_user)}


class KitPrefsPatch(BaseModel):
    prefs: dict[str, bool]


@router.put("/kit-prefs")
async def put_kit_prefs(payload: KitPrefsPatch, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    chosen = {k: v for k, v in payload.prefs.items() if k in KIT_PREF_KEYS and isinstance(v, bool)}
    if chosen:
        st = dict(current_user.setup_state or {})
        st["kit_prefs"] = {**_kit_prefs_of(current_user), **chosen}
        current_user.setup_state = st
        await db.commit()
    return {"prefs": _kit_prefs_of(current_user)}


class FormDefaultsPatch(BaseModel):
    animated: Optional[bool] = None
    show_branding: Optional[bool] = None
    redirect_to_agent_hub: Optional[bool] = None


@router.get("/form-defaults")
async def get_form_defaults(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """The Forms step's switches, read from every form the agent has (see set_form_defaults)."""
    return await flow.form_defaults_state(db, current_user)


@router.put("/form-defaults")
async def put_form_defaults(payload: FormDefaultsPatch, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Sets animated / agent branding / after-submit on every form now, and on forms made later."""
    return await flow.set_form_defaults(db, current_user, payload.model_dump(exclude_none=True))


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
