"""Public, tiny round agent photo for client emails - see email_avatar_service."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import email_avatar_service

router = APIRouter(tags=["email-avatar"])

# Images change only when the photo/name changes, and then the `v` param in the email changes too -
# so the image itself can be cached hard.
_HEADERS = {"Cache-Control": "public, max-age=604800"}


@router.get("/email-avatar/{user_id}.jpg")
async def email_avatar(user_id: str, request: Request, bg: str = "light", db: AsyncSession = Depends(get_db)):
    bg = bg if bg in email_avatar_service.BACKGROUNDS else "light"
    found = await email_avatar_service.avatar_bytes(db, user_id, bg)
    if not found:
        return Response(status_code=404)
    body, etag = found
    headers = {**_HEADERS, "ETag": f'"{etag}"'}
    if f'"{etag}"' in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="image/jpeg", headers=headers)


@router.get("/email-avatar/{user_id}.png")
async def email_avatar_png(user_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The see-through round photo (contact card) - sits right on light AND dark-mode backgrounds."""
    found = await email_avatar_service.avatar_bytes(db, user_id, email_avatar_service.CLEAR)
    if not found:
        return Response(status_code=404)
    body, etag = found
    headers = {**_HEADERS, "ETag": f'"{etag}"'}
    if f'"{etag}"' in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="image/png", headers=headers)
