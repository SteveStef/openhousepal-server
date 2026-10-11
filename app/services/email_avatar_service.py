"""The agent's face in client emails (header + contact card at the bottom).

Agents upload full-size headshots - often several MB - which is far too heavy for an email and
gets clipped/blocked by some clients. Instead every client email points at
/email-avatar/{user_id}.jpg: a tiny (~5 KB) round photo baked onto the exact background it sits on
(dark header band or white card), so it looks round even in Outlook, which ignores border-radius.
No photo (or the photo link is dead) -> the agent's initials in a gold circle, so there is always a
picture. The `v` query param changes when the photo or name changes, so mail clients and Gmail's
image proxy pick up a new headshot instead of a cached old one."""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from io import BytesIO
from typing import Optional

from app.config.logging import get_logger

logger = get_logger(__name__)

DISPLAY = 56           # largest size the templates show it at (px)
SCALE = 3              # rendered at 3x so it stays sharp on phones
BACKGROUNDS = {"dark": (23, 20, 15), "light": (255, 255, 255)}   # header band INK / white card
RING = (201, 162, 77)  # the emails' gold line colour

_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_CACHE_MAX = 300


def _api_base() -> str:
    # Same default as the calendar/unsubscribe links: the public API outside dev. In dev (no API_URL)
    # there's no address a mail client could reach, so no photo link is sent at all.
    if os.getenv("API_URL"):
        return os.getenv("API_URL").rstrip("/")
    return "" if os.getenv("DEV", "false") == "true" else "https://api.openhousepal.com"


def _version(photo_url: str, name: str, focal) -> str:
    return hashlib.sha1(f"{photo_url}|{name}|{focal}".encode()).hexdigest()[:10]


async def agent_email_vars(db, user) -> dict:
    """Everything a client email needs to show who it's from: name, face, title, brokerage, email,
    phone. Senders merge it UNDER their own variables. Never raises - an email must still go out."""
    if user is None:
        return {}
    try:
        from app.services.agent_email_service import agent_public_phone
        from app.services.website import content_adapters
        profile = await content_adapters.get_agent_profile(db, user)
        hub_photo_pos = await _photo_position(db, user)
        name = profile.get("name") or ""
        out = {
            "agent_name": name,
            "agent_email": profile.get("email") or user.email or "",
            "agent_phone": await agent_public_phone(db, user),
            "agent_title": profile.get("title") or "",
            "agent_brokerage": profile.get("brokerage") or "",
        }
        base = _api_base()
        if base:
            v = _version(profile.get("photo_url") or "", name, hub_photo_pos)
            out["agent_photo"] = f"{base}/email-avatar/{user.id}.jpg?bg=light&v={v}"
            out["agent_photo_dark"] = f"{base}/email-avatar/{user.id}.jpg?bg=dark&v={v}"
        return out
    except Exception:  # noqa: BLE001
        logger.warning("agent email vars failed", exc_info=True)
        return {}


async def _photo_position(db, user):
    from sqlalchemy import select
    from app.models.database import AgentHub
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    return ((hub.config or {}).get("profilePhotoPosition") if hub else None)


async def render_avatar(photo_url: Optional[str], name: str, bg: str, focal=None) -> bytes:
    from PIL import Image, ImageDraw
    from app.services.email_signature_service import _cover_crop, _fetch_image, _initials_avatar

    bg_rgb = BACKGROUNDS.get(bg, BACKGROUNDS["light"])
    size = DISPLAY * SCALE
    big = size * 3                                   # supersample the circle so its edge is smooth
    fxy = None
    if isinstance(focal, dict) and isinstance(focal.get("x"), (int, float)) and isinstance(focal.get("y"), (int, float)):
        fxy = (focal["x"] / 100, focal["y"] / 100)
    raw = (await _fetch_image(photo_url)) if photo_url else None
    face = _cover_crop(raw, big, big, focal=fxy) if raw is not None else _initials_avatar(name, big, RING)

    ring_w = 2 * SCALE * 3                           # 2px gold ring at display size
    canvas = Image.new("RGB", (big, big), bg_rgb)
    ring = Image.new("L", (big, big), 0)
    ImageDraw.Draw(ring).ellipse((0, 0, big - 1, big - 1), fill=255)
    canvas.paste(Image.new("RGB", (big, big), RING), (0, 0), ring)
    inner = Image.new("L", (big, big), 0)
    ImageDraw.Draw(inner).ellipse((ring_w, ring_w, big - 1 - ring_w, big - 1 - ring_w), fill=255)
    canvas.paste(face, (0, 0), inner)
    out = canvas.resize((size, size), Image.LANCZOS)
    buf = BytesIO()
    out.save(buf, format="JPEG", quality=86, optimize=True, progressive=True)
    return buf.getvalue()


async def avatar_bytes(db, user_id: str, bg: str) -> Optional[tuple[bytes, str]]:
    """(jpeg, etag) for this agent, or None when there's no such agent."""
    from sqlalchemy import select
    from app.models.database import User
    from app.services.website import content_adapters
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        return None
    profile = await content_adapters.get_agent_profile(db, user)
    photo, name = profile.get("photo_url") or "", profile.get("name") or ""
    focal = await _photo_position(db, user)
    etag = f"{_version(photo, name, focal)}-{bg}"
    body = _CACHE.get(etag)
    if body is None:
        body = await render_avatar(photo or None, name, bg, focal)
        _CACHE[etag] = body
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    else:
        _CACHE.move_to_end(etag)
    return body, etag
