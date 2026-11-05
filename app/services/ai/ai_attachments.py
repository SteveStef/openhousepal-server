"""Photos dropped into the chat or onto a card (owner, 11/05). Each is kept as a Media Lab upload, so the
storage limit, the size limit and the agent's upload library all apply, and it can go anywhere the chat
takes a photo. Every address read back is checked against the agent's own uploads, so a card can never
be pointed at someone else's file or an outside address.
"""
import base64
import io
from typing import Optional

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.models.database import StudioMediaAsset, User
from app.services import s3_service, studio_media_service
from app.services.ai.ai_actions import ActionError
from app.services.ai.ai_route_bridge import RouteError, call_as_agent

logger = get_logger(__name__)
_MAX_PER_MESSAGE = 6
_SEE_SIZE = 1024   # what the chat looks at; the file itself stays full size


async def add(db: AsyncSession, user: User, data: bytes, content_type: str) -> dict:
    asset = await studio_media_service.upload_photo(db, user, data, content_type)
    png_url = None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        # A logo needs its see-through background, which the JPG copy loses.
        try:
            png_url = s3_service.upload_bytes(data, "ai-attachments", ".png", "image/png")
            asset.options = {**(asset.options or {}), "png_url": png_url}
            flag_modified(asset, "options")
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("keeping the PNG copy failed", exc_info=True)
    return {"id": asset.id, "url": asset.result_url, "png_url": png_url}


async def owned(db: AsyncSession, owner_id: str, ids: Optional[list]) -> list:
    if not ids:
        return []
    rows = (await db.execute(select(StudioMediaAsset).where(StudioMediaAsset.id.in_([str(i) for i in ids[:_MAX_PER_MESSAGE]]), StudioMediaAsset.owner_id == owner_id,
                                                            StudioMediaAsset.kind == "upload"))).scalars().all()
    return list(rows)


def note(attached: list) -> str:
    """What the chat keeps in the message; the thread shows the photo in place of this line."""
    return "".join(f"\n[Photo attached: {a.result_url}]" for a in attached)


async def _fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def image_blocks(attached: list) -> list:
    """Smaller copies for this one turn, so it can see what's in the photo. A photo it can't read
    doesn't stop the message."""
    from PIL import Image
    out = []
    for a in attached:
        try:
            img = Image.open(io.BytesIO(await _fetch(a.result_url))).convert("RGB")
            img.thumbnail((_SEE_SIZE, _SEE_SIZE))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            out.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}})
        except Exception:  # noqa: BLE001
            logger.warning("couldn't read an attached photo", extra={"asset_id": a.id}, exc_info=True)
    return out


async def post_file(db: AsyncSession, user: User, url: str, path: str, keep_png: bool = False) -> None:
    """Sends one of the agent's own uploads to a page's upload route (a logo, a headshot)."""
    a = (await db.execute(select(StudioMediaAsset).where(StudioMediaAsset.owner_id == user.id, StudioMediaAsset.kind == "upload", StudioMediaAsset.result_url == url))).scalars().first()
    if a is None:
        a = next((x for x in (await db.execute(select(StudioMediaAsset).where(StudioMediaAsset.owner_id == user.id, StudioMediaAsset.kind == "upload")
                                               .order_by(StudioMediaAsset.created_at.desc()).limit(200))).scalars().all() if (x.options or {}).get("png_url") == url), None)
    if a is None:
        raise ActionError("I couldn't find that photo - drop it in again.")
    src = ((a.options or {}).get("png_url") if keep_png else None) or a.result_url
    name, kind = ("logo.png", "image/png") if src.endswith(".png") else ("photo.jpg", "image/jpeg")
    try:
        await call_as_agent(user, "POST", path, files={"file": (name, await _fetch(src), kind)})
    except RouteError as e:
        raise ActionError(str(e)) from e
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "I couldn't read that photo - drop it in again.") from e
