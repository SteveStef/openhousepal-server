"""What goes in the middle of an agent's QR codes (user, 10/17).

Default for everyone: the OpenHousePal house logo. IDX agents can swap it for their own Brand Kit
logo or their initials (Settings -> Profile, or the setup flow's brand step). Old printed codes keep
working either way - the centre is only decoration over the code's error-correction margin, the link
inside never changes - and every QR drawn from then on (kits, designs, forms, blogs, kiosk) uses the
new look.

GET /qr-brand/{user_id} is public (the logo is on their public pages anyway) so the guest-facing
pages that draw a code (kiosk sign-in screen, blog pages) can ask for their agent's choice. The logo
comes back as a small PNG data URL from our own API, so the browser can draw it into a canvas
without cross-origin trouble."""
from __future__ import annotations

import base64
import io
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import StudioBrandKit, User
from app.utils.auth import get_current_active_user, owner_has_access

logger = get_logger(__name__)
router = APIRouter(tags=["qr-brand"])

_LOGO_CACHE: dict[str, tuple[str, Optional[str]]] = {}   # logo url -> data url


def _initials(user: User) -> str:
    parts = [p for p in (f"{user.first_name or ''} {user.last_name or ''}").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


async def _logo_data_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    hit = _LOGO_CACHE.get(url)
    if hit:
        return hit[1]
    data_url = None
    try:
        from PIL import Image
        from app.services.email_signature_service import _fetch_image
        img = await _fetch_image(url)
        if img is not None:
            img = img.convert("RGBA")
            img.thumbnail((256, 256))
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 - no logo = the house logo, never an error
        logger.warning("qr logo fetch failed", exc_info=True)
    if len(_LOGO_CACHE) > 500:
        _LOGO_CACHE.clear()
    _LOGO_CACHE[url] = (url, data_url)
    return data_url


async def qr_brand_for(db: AsyncSession, user: Optional[User]) -> dict:
    """{"kind": "ohp" | "logo" | "initials", "initials"?, "logo"?, "choice", "allowed"}."""
    if user is None:
        return {"kind": "ohp", "choice": "ohp", "allowed": False}
    allowed = owner_has_access(user, {"IDX"})
    choice = (user.qr_logo or "ohp") if allowed else "ohp"
    out = {"kind": "ohp", "choice": user.qr_logo or "ohp", "allowed": allowed, "initials": _initials(user)}
    if choice == "initials":
        out["kind"] = "initials"
    elif choice == "logo":
        kit = (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id))).scalar_one_or_none()
        logo = await _logo_data_url(kit.personal_logo_url if kit else None)
        if logo:
            out.update(kind="logo", logo=logo)
    return out


@router.get("/qr-brand/{user_id}")
async def get_qr_brand(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id[:64])
    if user is None:
        raise HTTPException(status_code=404, detail="Not found")
    b = await qr_brand_for(db, user)
    return {k: v for k, v in b.items() if k in ("kind", "initials", "logo")}


class QrBrandUpdate(BaseModel):
    choice: Literal["ohp", "logo", "initials"]


@router.get("/me/qr-brand")
async def my_qr_brand(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    kit = (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == current_user.id))).scalar_one_or_none()
    return {**(await qr_brand_for(db, current_user)), "has_logo": bool(kit and kit.personal_logo_url)}


@router.put("/me/qr-brand")
async def set_my_qr_brand(payload: QrBrandUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    user = await db.get(User, current_user.id)
    if payload.choice != "ohp" and not owner_has_access(user, {"IDX"}):
        raise HTTPException(status_code=403, detail="Your own logo or initials in your QR codes comes with the IDX plan.")
    user.qr_logo = payload.choice
    await db.commit()
    kit = (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id))).scalar_one_or_none()
    return {**(await qr_brand_for(db, user)), "has_logo": bool(kit and kit.personal_logo_url)}
