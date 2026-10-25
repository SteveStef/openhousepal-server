import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import StudioBrandKit, User
from app.schemas.studio import BrandKitResponse, BrandKitUpdate, MediaUploadResponse
from app.services import s3_service, storage_ledger_service
from app.services.ai.ai_config_service import ai_config_service
from app.utils.auth import get_current_active_user, require_active_plan
from app.utils.image_validation import assert_valid_jpeg, clean_logo_png

router = APIRouter(prefix="/studio", tags=["studio"])
logger = logging.getLogger(__name__)

# JPG only - PNG/WEBP uploads have been unreliable in practice, so every photo upload on the
# site (brand kit logos, blog covers/media, design assets) is restricted to JPEG.
IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
}
IMAGE_MAX_BYTES = 10 * 1024 * 1024

LOGO_FIELDS = {"personal", "team", "brokerage"}


async def get_or_create_brand_kit(db: AsyncSession, owner_id: str) -> StudioBrandKit:
    result = await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == owner_id))
    kit = result.scalar_one_or_none()
    if not kit:
        kit = StudioBrandKit(owner_id=owner_id)
        db.add(kit)
        await db.commit()
        await db.refresh(kit)
    return kit


@router.get("/brand-kit", response_model=BrandKitResponse)
async def get_brand_kit(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    return _kit_out(await get_or_create_brand_kit(db, current_user.id), current_user)


def _kit_out(kit: StudioBrandKit, user: User) -> BrandKitResponse:
    """The kit plus its brand LOOK (theme / metal / Ivory-Midnight - services/brand_look_service)."""
    from app.services import brand_look_service
    out = BrandKitResponse.model_validate(kit)
    out.look = brand_look_service.look_of(user, kit)
    return out


@router.put("/brand-kit", response_model=BrandKitResponse)
async def update_brand_kit(
    payload: BrandKitUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    from app.services import brand_look_service
    from app.utils.auth import luxe_theme_allowed
    kit = await get_or_create_brand_kit(db, current_user.id)
    data = payload.model_dump(exclude_unset=True)
    look = data.pop("look", None)
    old_secondary = kit.secondary_color
    for field, value in data.items():
        setattr(kit, field, value)
    if "secondary_color" in data:
        # live invitations made in the old brand color follow the new one (Branding center)
        await brand_look_service.follow_brand_color_change(db, current_user, old_secondary, kit.secondary_color)
    if look:
        # Luxe is IDX only (same rule as the Hub / Signature / Studio Luxe).
        if look.get("theme") == "luxe" and not luxe_theme_allowed(current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Luxe is included with the IDX plan.")
        brand_look_service.save_look(current_user, {k: v for k, v in look.items() if v is not None})
    await db.flush()
    # Every feature still on "My Brand Kit" picks the change up (the signature is written here;
    # the hub, forms and IDX website read it live).
    await brand_look_service.propagate(db, current_user)
    await db.commit()
    await db.refresh(kit)
    return _kit_out(kit, current_user)


@router.get("/brand-kit/usage")
async def brand_kit_usage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Branding center: which features wear the brand color right now."""
    from app.services import brand_look_service
    return await brand_look_service.usage(db, current_user)


@router.post("/brand-kit/apply-all")
async def brand_kit_apply_all(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Branding center's "Use this color for all features": every feature back on My Brand Kit."""
    from app.services import brand_look_service
    out = await brand_look_service.apply_everywhere(db, current_user)
    await db.commit()
    return out


@router.post("/brand-kit/logo/{slot}", response_model=BrandKitResponse)
async def upload_brand_logo(
    slot: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    if slot not in LOGO_FIELDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown logo slot")

    # Logos (only) may also be PNG - nearly every logo has a see-through background, and a JPG
    # put a white box behind it on dark designs. Photos everywhere else stay JPG-only.
    if file.content_type not in ("image/jpeg", "image/png"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Please upload a JPG or PNG logo.")

    contents = await file.read()
    if len(contents) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Image must be 10MB or smaller.")
    try:
        if file.content_type == "image/png":
            contents = clean_logo_png(contents)
            extension, content_type = ".png", "image/png"
        else:
            assert_valid_jpeg(contents)
            extension, content_type = ".jpg", "image/jpeg"
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    kit = await get_or_create_brand_kit(db, current_user.id)
    old_url = getattr(kit, f"{slot}_logo_url")
    url = s3_service.upload_bytes(contents, "studio-media", extension, content_type)
    setattr(kit, f"{slot}_logo_url", url)
    await db.commit()
    await db.refresh(kit)

    s3_service.delete_by_url(old_url)
    await storage_ledger_service.release(db, old_url)
    await storage_ledger_service.record(db, current_user.id, "brand_logo", url, len(contents))

    return _kit_out(kit, current_user)


@router.delete("/brand-kit/logo/{slot}", response_model=BrandKitResponse)
async def delete_brand_logo(
    slot: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    if slot not in LOGO_FIELDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown logo slot")
    kit = await get_or_create_brand_kit(db, current_user.id)
    old_url = getattr(kit, f"{slot}_logo_url")
    setattr(kit, f"{slot}_logo_url", None)
    await db.commit()
    await db.refresh(kit)

    s3_service.delete_by_url(old_url)
    await storage_ledger_service.release(db, old_url)

    return _kit_out(kit, current_user)


@router.post("/media", response_model=MediaUploadResponse)
async def upload_studio_media(
    file: UploadFile = File(...),
    category: str = "studio_media",  # 'blog' | 'design' | 'studio_media'
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Shared media upload for Design Studio & Blogs (cover images, article images, design photos).
    category='brand_logo' (the IDX website's own logo) may also be a PNG, same as Brand Kit logos."""
    logo = category == "brand_logo"
    if file.content_type not in (("image/jpeg", "image/png") if logo else ("image/jpeg",)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Please upload a JPG or PNG logo." if logo else "Please upload a JPG image.")

    contents = await file.read()
    if len(contents) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Image must be 10MB or smaller.")
    try:
        if file.content_type == "image/png":
            contents = clean_logo_png(contents)
            extension = ".png"
        else:
            assert_valid_jpeg(contents)
            extension = ".jpg"
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    cfg = await ai_config_service.get_config(db)
    await storage_ledger_service.enforce_cap(db, current_user.id, cfg, getattr(current_user, "plan_tier", None), len(contents))
    url = s3_service.upload_bytes(contents, "studio-media", extension, file.content_type)
    cat = category if category in ("blog", "design", "brand_logo") else "studio_media"
    await storage_ledger_service.record(db, current_user.id, cat, url, len(contents))
    return MediaUploadResponse(url=url)


@router.get("/storage")
async def get_my_storage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Per-agent media storage across the whole app (AI Media, Design, Blogs, logos, listing &
    agent photos). Powers the Settings storage widget and the AI Media page's storage bar."""
    cfg = await ai_config_service.get_config(db)
    u = await storage_ledger_service.usage(db, current_user.id)
    cap = storage_ledger_service.cap_bytes(cfg, getattr(current_user, "plan_tier", None))
    used = u["used_bytes"]
    return {
        "used_bytes": used,
        "cap_bytes": cap,
        "percent": min(100, round(used / cap * 100)) if cap else 0,
        "by_category": u["by_category"],
    }


# ---------------------------------------------------------------- listing blurb (design description)

from pydantic import BaseModel, Field  # noqa: E402


class ListingBlurbRequest(BaseModel):
    listing_key: str | None = None
    address: str | None = None
    city: str | None = None
    remarks: str | None = Field(default=None, max_length=8000)
    facts: str | None = Field(default=None, max_length=600)


class ListingBlurbSave(BaseModel):
    listing_key: str | None = None
    address: str | None = None
    city: str | None = None
    text: str = Field(max_length=2000)


@router.post("/listing-blurb")
async def listing_blurb(payload: ListingBlurbRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    """The short lines a design prints about this home - this agent's saved ones, else written once by AI."""
    from app.services import listing_blurb_service as svc
    key = svc.blurb_key(payload.listing_key, payload.address, payload.city)
    if not key:
        return {"text": "", "source": "none"}
    return await svc.get_or_write(db, current_user.id, current_user.plan_tier, key=key, remarks=payload.remarks or "", facts=payload.facts or "")


@router.put("/listing-blurb")
async def save_listing_blurb(payload: ListingBlurbSave, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    """The agent edited the lines in a design - keep their wording for this home's next designs."""
    from app.services import listing_blurb_service as svc
    key = svc.blurb_key(payload.listing_key, payload.address, payload.city)
    if not key:
        return {"text": payload.text, "source": "none"}
    return await svc.save_edit(db, current_user.id, key, payload.text)
