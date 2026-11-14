"""AI Media "Cinematic Video" - real listing photos moved like a film camera through their own 3D
depth, with room captions, stats, a closing card and the agent's branding. No AI video model:

  1. create   - a draft project exists from the first tap (StudioMediaAsset kind=video,
                action=motion, status=draft); every edit autosaves into options.motion.
  2. analyze  - per photo: a clean copy (MLS badge strip cropped), a depth map (fal
                depth-anything v2, listed $0) and ONE vision call that names each room and spots
                lights / water / sky / trees. The agent then confirms every label.
  3. render   - optional sharpening (fal ESRGAN, ~half a cent a photo), then the video worker
                (frontend/video-worker, Remotion) renders the MP4; we copy it to S3.

AI spend (labels + depth + sharpening) is reserved and recorded like every AI Media call, under
its own event_type "studio_motion" (agent's monthly AI usage bar; never the AI video quota).
Rendering itself is computer time, not AI, and is free to re-run - so every later edit (labels,
theme, transitions, removing sections) re-renders at no AI cost.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.database import Property, StudioMediaAsset, User
from app.services import s3_service, storage_ledger_service
from app.services import video_lambda_service as vl
from app.services.ai.ai_config_service import ai_config_service
from app.utils.auth import tier_has_mls
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, monthly_ai_budget_snapshot, reconcile_budget, record_usage_event, reserve_studio_motion,
    studio_motion_would_refuse,
    studio_soft_capped,
)

logger = get_logger(__name__)

_S3_PREFIX = "studio-motion"
EVENT_TYPE = "studio_motion"
KINDS = ("exterior", "aerial", "pool", "outdoor", "living", "kitchen", "dining", "bedroom", "bathroom", "office", "interior", "detail")
SITUATIONS = ("just_listed", "coming_soon", "open_house", "price_improved", "under_contract", "just_sold",
              "back_on_market", "new_construction", "private_listing", "custom")
THEMES = ("classic", "luxe")
STYLES = ("cinematic", "glide", "bold")
TRANSITIONS = ("cut", "crossfade", "dip", "light", "whip", "zoom")
MOVES = ("auto", "push", "slide_left", "slide_right", "rise", "orbit", "drone")
FORMATS = ("16:9", "9:16", "1:1", "4:5")
MUSIC = ("bright", "elegant", "none")
# Owner 11/12: real recorded-style tracks (ElevenLabs Music, made once) replaced the code-made
# "piano"/"ambient" placeholders. Choices saved before then map to the new ones.
MUSIC_LEGACY = {"piano": "elegant", "ambient": "bright"}
METALS = ("gold", "silver", "bronze")
LENGTHS = (15, 30, 45, 60)
_HEX = "0123456789abcdefABCDEF"
# What an agent can choose to show (step 2). room -> (the camera "kind" it films as, default small line).
# Keep in step with ROOM_OPTIONS in frontend/motion-video/presets.ts. "skip" = floor plans, maps,
# garages, laundry, closets - never offered.
ROOMS: dict[str, tuple[str, str]] = {
    "front": ("exterior", "Curb appeal"), "entry": ("interior", "Welcome home"), "living": ("living", "Living"),
    "family": ("living", "Gathering"), "kitchen": ("kitchen", "The kitchen"), "dining": ("dining", "Dining"),
    "office": ("office", "Work from home"), "primary": ("bedroom", "Retreat"), "primary_bath": ("bathroom", "Spa bath"),
    "bedroom": ("bedroom", "Bedrooms"), "bath": ("bathroom", "Baths"), "basement": ("interior", "Lower level"),
    "bonus": ("interior", "Bonus space"), "yard": ("outdoor", "Outdoors"), "pool": ("pool", "Poolside"),
    "view": ("outdoor", "The setting"), "back": ("exterior", "Rear view"), "aerial": ("aerial", "From above"),
    "detail": ("detail", "Details"), "skip": ("interior", ""),
}
# Every listing photo is offered (owner 11/13: a basement bedroom and the primary suite sat past the
# old first-40 cut, so they never showed under Bedrooms). The AI looks at them in batches.
POOL_MAX = 100
LABEL_BATCH = 40

# MLS photos carry the Bright watermark in the bottom-left corner; a strip off the bottom takes it out.
_BADGE_STRIP = 0.075
_CLEAN_MAX_W = 2400
_FAL_DEPTH = "https://fal.run/fal-ai/image-preprocessors/depth-anything/v2"
_FAL_DEPTH_SHARP = "https://fal.run/fal-ai/imageutils/marigold-depth"
_FAL_ERASE = "https://fal.run/fal-ai/lama"  # paints the MLS badge out (~$0.000575/compute s)
_FAL_ESRGAN = "https://fal.run/fal-ai/esrgan"

_TASKS: dict[str, asyncio.Task] = {}
_PREP: dict[str, asyncio.Task] = {}


def _worker_url() -> str:
    return (os.environ.get("VIDEO_WORKER_URL") or "http://localhost:8095").rstrip("/")


def _worker_headers() -> dict:
    tok = os.environ.get("VIDEO_WORKER_TOKEN") or ""
    return {"x-worker-token": tok} if tok else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _txt(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def _float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _box(v: Any) -> Optional[list[float]]:
    """A [left, top, right, bottom] box in 0-1 photo fractions (where the fireplace's flames are), or None."""
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(a))) for a in list(v)[:4])
    except (TypeError, ValueError):
        return None
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _find_flames(b64: str, box: Any) -> Optional[list[float]]:
    """The AI says where a fire is only roughly, so look
    around its guess for the actual flames - bright, saturated orange/yellow pixels - and box those.
    None when there are no flames there (then nothing flickers)."""
    import base64
    import colorsys
    from PIL import Image
    b = _box(box)
    if not b:
        return None
    try:
        im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    w, h = im.size
    bw, bh = b[2] - b[0], b[3] - b[1]
    # search a window a box out each way (at least 8% of the photo)
    px, py = max(bw, 0.08), max(bh, 0.08)
    wx0, wy0 = int(max(0.0, b[0] - px) * w), int(max(0.0, b[1] - py) * h)
    wx1, wy1 = int(min(1.0, b[2] + px) * w), int(min(1.0, b[3] + py) * h)
    if wx1 - wx0 < 4 or wy1 - wy0 < 4:
        return None
    pix = im.crop((wx0, wy0, wx1, wy1)).load()
    W, H = wx1 - wx0, wy1 - wy0
    # flame pixels: glowing yellow-orange (gas flames are paler), much brighter than wood floors
    hit = [[False] * W for _ in range(H)]
    for j in range(H):
        for i in range(W):
            r, g, bl = pix[i, j]
            hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, bl / 255)
            hit[j][i] = 0.02 <= hh <= 0.13 and ss >= 0.3 and vv >= 0.85
    # the densest spot of them (a fire is a tight cluster; stray bright wood is scattered)
    cell = max(6, w // 48)
    best, bx, by = 0, 0, 0
    for j in range(0, max(1, H - cell), max(2, cell // 3)):
        for i in range(0, max(1, W - cell), max(2, cell // 3)):
            n = sum(hit[jj][ii] for jj in range(j, min(H, j + cell)) for ii in range(i, min(W, i + cell)))
            if n > best:
                best, bx, by = n, i, j
    if best < cell * cell * 0.12:
        return None
    # box the flame pixels around that spot
    r = cell * 3
    xs, ys = [], []
    for j in range(max(0, by - r), min(H, by + cell + r)):
        for i in range(max(0, bx - r), min(W, bx + cell + r)):
            if hit[j][i]:
                xs.append(wx0 + i)
                ys.append(wy0 + j)
    xs.sort(); ys.sort()
    lo, hi = int(len(xs) * 0.03), max(0, int(len(xs) * 0.97) - 1)
    fx0, fx1, fy0, fy1 = xs[lo] / w, xs[hi] / w, ys[lo] / h, ys[hi] / h
    pad = 0.012
    return _box([fx0 - pad, fy0 - pad, fx1 + pad, fy1 + pad])


def _money(v: Optional[float]) -> str:
    try:
        return f"${round(float(v)):,}" if v else ""
    except (TypeError, ValueError):
        return ""


def _pos(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _num(v: Optional[float], decimals: int = 0) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if decimals == 0 or f == int(f):
        return f"{int(round(f)):,}"
    return f"{f:.{decimals}f}".rstrip("0").rstrip(".")


def _situation_for(lp: Optional[dict]) -> str:
    s = str((lp or {}).get("status") or "").lower()
    if "coming" in s:
        return "coming_soon"
    if "contract" in s or "pending" in s:
        return "under_contract"
    if "closed" in s or "sold" in s:
        return "just_sold"
    if (lp or {}).get("original_price") and (lp or {}).get("price") and float(lp["original_price"]) > float(lp["price"]) + 1:
        return "price_improved"
    return "just_listed"


def _new_photo(url: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:10], "source": url, "clean": None, "depth": None, "sharp": None,
        "kind": "interior", "label": "", "kicker": "", "showLabel": True, "move": "auto", "focusX": 0.5,
        "lights": False, "water": False, "sky": False, "trees": False, "fire": False, "fireBox": None, "transitionIn": None,
        "hidden": False, "analyzed": False, "sure": True, "confirmed": False,
    }


_POOL_AI = ("room", "kind", "label", "kicker", "sure", "score", "focusX", "lights", "water", "sky", "trees", "fire", "fireBox", "analyzed")
_FILES = ("clean", "depth", "sharp", "sharpDepth")  # sharpDepth: the depth map has crisp (Marigold) edges


def _new_pool(url: str) -> dict:
    """One photo the project can use (all of the listing's photos, or the agent's uploads). The AI
    sorts these into rooms; the agent's picks become motion.photos."""
    return {"source": url, "room": None, "kind": "interior", "label": "", "kicker": "", "sure": True, "score": 3,
            "focusX": 0.5, "lights": False, "water": False, "sky": False, "trees": False, "fire": False, "fireBox": None, "analyzed": False,
            "clean": None, "depth": None, "sharp": None}


def _ensure_pool(motion: dict) -> list[dict]:
    """Projects made before the pool existed: their own photos are the pool."""
    pool = motion.get("pool")
    if isinstance(pool, list):
        return pool
    out = []
    for p in motion.get("photos") or []:
        q = _new_pool(p.get("source"))
        for k in _POOL_AI + _FILES:
            if k in p:
                q[k] = p[k]
        q["room"] = q.get("room") or None
        out.append(q)
    motion["pool"] = out
    return out


def _photo_from_pool(item: dict, pid: str) -> dict:
    p = _new_photo(item["source"])
    p["id"] = pid
    for k in ("kind", "label", "kicker", "sure", "focusX", "lights", "water", "sky", "trees", "fire", "fireBox", "analyzed", "room") + _FILES:
        if item.get(k) is not None:
            p[k] = item[k]
    if not p.get("kicker") and item.get("room") in ROOMS:
        p["kicker"] = ROOMS[item["room"]][1]
    return p


def default_settings(photos: list[str], lp: Optional[dict], agent: Optional[dict], courtesy: str) -> dict:
    lp = lp or {}
    sit = _situation_for(lp)
    price = lp.get("close_price") if sit == "just_sold" and lp.get("close_price") else lp.get("price")
    town = str(lp.get("city") or "")
    if town and town == town.upper():
        town = town.title()
    city = ", ".join(x for x in [town, str(lp.get("state") or "").upper()] if x)
    addr = str(lp.get("address") or "")
    if addr and addr == addr.upper():
        addr = addr.title()
    return {
        # Every photo is a candidate; the agent picks the length and the rooms first, then the
        # video's photos are chosen from here (motion.photos stays empty until then).
        "pool": [_new_pool(u) for u in photos[:POOL_MAX]], "photos": [], "want": [], "length": 30,
        "theme": "classic", "accent": "#C9A24D", "metal": "gold",
        "style": "cinematic", "transition": "cut", "situation": sit,
        "copy": {"kicker": "", "tagline": "", "title": "", "cta": ""},
        "facts": {
            "address": addr, "cityLine": city, "price": _money(price),
            "wasPrice": _money(lp.get("original_price")) if sit == "price_improved" else "",
            # a 0 from the MLS (often acres on townhomes) is left blank - never "0 Acres" on the video
            "beds": _num(lp.get("beds")) if _pos(lp.get("beds")) else "", "baths": _num(lp.get("baths"), 1) if _pos(lp.get("baths")) else "",
            "sqft": _num(lp.get("sqft")) if _pos(lp.get("sqft")) else "", "acres": _num(lp.get("acres"), 2) if _pos(lp.get("acres")) else "",
            "when": "", "courtesy": courtesy,
        },
        "agent": {
            "name": _txt((agent or {}).get("name"), 60), "brokerage": _txt((agent or {}).get("brokerage"), 80),
            "phone": _txt((agent or {}).get("phone"), 30), "email": _txt((agent or {}).get("email"), 80),
            "photo": (agent or {}).get("photo") or None,
        },
        "sections": {"opening": True, "labels": True, "stats": True, "agent": "opening", "endCard": True},
        "music": "elegant", "format": "16:9", "sharpen": True,  # owner 11/12: Elegant piano is the default
    }


def _when_text(d, start, end) -> str:
    """"Sun, Nov 16 · 1-3 PM" (or "Sun, Nov 16" with no time) for the open house line."""
    def hm(t) -> tuple[str, str]:
        h = t.hour % 12 or 12
        return (f"{h}" if t.minute == 0 else f"{h}:{t.minute:02d}"), ("AM" if t.hour < 12 else "PM")
    day = f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"
    if not start:
        return day
    s, sp = hm(start)
    if not end:
        return f"{day} · {s} {sp}"
    e, ep = hm(end)
    return f"{day} · {s}{'' if sp == ep else ' ' + sp}-{e} {ep}"


async def _next_open_house(db, user: User, lp: Optional[dict]) -> str:
    """The listing's next open house (the agent's Open House Manager events first, then the MLS
    schedule), so picking "Open House" needs no typing (QA 11/12). "" when none is coming up."""
    key = (lp or {}).get("listing_key")
    if not key:
        return ""
    from datetime import date
    from app.models.database import OpenHouseEvent
    from app.services.mls_open_houses import upcoming_mls_open_houses
    today = date.today()
    try:
        ev = (await db.execute(select(OpenHouseEvent).where(
            OpenHouseEvent.listing_key == key, OpenHouseEvent.is_deleted.is_(False),
            OpenHouseEvent.scheduled_date.is_not(None), OpenHouseEvent.scheduled_date >= today,
            (OpenHouseEvent.agent_id == user.id) | (OpenHouseEvent.listing_owner_agent_id == user.id),
        ).order_by(OpenHouseEvent.scheduled_date.asc()).limit(1))).scalars().first()
        if ev:
            return _when_text(ev.scheduled_date, ev.scheduled_start_time, ev.scheduled_end_time)
        prop = (await db.execute(select(Property).where(Property.listing_key == key))).scalar_one_or_none()
        slots = upcoming_mls_open_houses(prop, today) if prop is not None else []
        return _when_text(slots[0]["date"], slots[0]["start"], slots[0]["end"]) if slots else ""
    except Exception:  # noqa: BLE001 - a nice-to-have; the agent can type it
        logger.warning("motion open house lookup failed", exc_info=True)
        return ""


async def _courtesy_line(db, user: User, lp: Optional[dict]) -> str:
    """'Listing courtesy of <office>' when the agent isn't on this listing (MLS display rules)."""
    key = (lp or {}).get("listing_key")
    if not key:
        return ""
    prop = (await db.execute(select(Property).where(Property.listing_key == key))).scalar_one_or_none()
    if not prop:
        return ""
    mine = user.mls_id and user.mls_id in (prop.list_agent_mls_id, prop.co_list_agent_mls_id)
    if mine or not prop.list_office_name:
        return ""
    return f"Listing courtesy of {prop.list_office_name}"


# ------------------------------------------------------------------ create / update

async def create_draft(db, user: User, photos: list[str], source_kind: str, linked_property: Optional[dict],
                       agent: Optional[dict], client_request_id: Optional[str]) -> StudioMediaAsset:
    config = await ai_config_service.get_config(db)
    if not config.get("studio_motion_enabled", True):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Cinematic Video is turned off right now.")
    if client_request_id:
        prev = (await db.execute(select(StudioMediaAsset).where(
            StudioMediaAsset.owner_id == user.id, StudioMediaAsset.action == "motion",
            StudioMediaAsset.options["client_request_id"].astext == client_request_id))).scalars().first()
        if prev:
            return prev
    clean = [u for u in dict.fromkeys(p for p in photos if isinstance(p, str) and p.startswith(("http://", "https://")))][:POOL_MAX]
    if len(clean) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A Cinematic Video needs at least 2 photos.")
    if source_kind == "listing" and not tier_has_mls(user.plan_tier):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your plan uses your own uploaded photos.")
    lp = linked_property if source_kind == "listing" else None
    courtesy = await _courtesy_line(db, user, lp)
    # Phone + email on the agent badge and closing card (owner 11/12) - the same public contact
    # details as their Agent Hub / sign-in sheet, filled in here so the chat's videos get them too.
    agent = dict(agent or {})
    if not agent.get("phone") or not agent.get("email"):
        try:
            from app.services.website import content_adapters
            prof = await content_adapters.get_agent_profile(db, user)
        except Exception:  # noqa: BLE001
            prof = {}
        if not agent.get("phone"):
            from app.services.agent_email_service import agent_public_phone
            agent["phone"] = await agent_public_phone(db, user)
        agent["email"] = agent.get("email") or prof.get("email") or user.email or ""
    settings = default_settings(clean, lp, agent, courtesy)
    settings["facts"]["when"] = await _next_open_house(db, user, lp)
    opts: dict = {"motion": settings, "source_kind": source_kind, "analysis": {"status": "idle"}, "render": {"state": "idle"},
                  "current_hash": settings_hash(settings)}
    if client_request_id:
        opts["client_request_id"] = client_request_id
    asset = StudioMediaAsset(
        owner_id=user.id, group_id=str(uuid.uuid4()), parent_asset_id=None,
        kind="video", action="motion", status="draft",
        source_photo_url=clean[0], source_kind=source_kind, linked_property=lp,
        options=opts, provider="motion", provider_model="remotion-photo-motion",
        result_url=None, prompt=None, prompt_version="motion-v1", cost_usd=0.0,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


def _clean_settings(incoming: dict, current: dict) -> dict:
    """Takes the editor's settings, keeps only known fields, and never lets the browser change
    the server-made files (clean / depth / sharp) of a photo."""
    cur_photos = {p.get("id"): p for p in (current.get("photos") or [])}
    out = dict(current)
    pool = _ensure_pool(out)
    pool_by_src = {it.get("source"): it for it in pool}
    files_by_src = {p.get("source"): p for p in (current.get("photos") or []) if p.get("clean") or p.get("depth")}
    photos = []
    seen: set = set()
    src_photos = incoming["photos"] if isinstance(incoming.get("photos"), list) else (current.get("photos") or [])
    for p in src_photos[:16]:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        if pid in seen:
            continue
        base = cur_photos.get(pid)
        if base and p.get("source") and p.get("source") != base.get("source"):
            base = None  # same id, different photo (a stale tab / reused id): it's a new pick, not the old one
        if not base:
            # A photo picked from the project's own pool (the rooms step, "Show another"). Anything
            # else only arrives through add_photos.
            item = pool_by_src.get(p.get("source"))
            if not item or not (6 <= len(pid) <= 16 and pid.isalnum()):
                continue
            base = _photo_from_pool(item, pid)
            prev = files_by_src.get(item["source"])
            for k in _FILES:
                base[k] = base.get(k) or (prev or {}).get(k)
        seen.add(pid)
        q = dict(base)
        q["kind"] = p.get("kind") if p.get("kind") in KINDS else base.get("kind", "interior")
        q["label"] = _txt(p.get("label"), 48)
        q["kicker"] = _txt(p.get("kicker"), 28)
        q["showLabel"] = bool(p.get("showLabel", True))
        q["move"] = p.get("move") if p.get("move") in MOVES else "auto"
        try:
            q["focusX"] = min(1.0, max(0.0, float(p.get("focusX", base.get("focusX", 0.5)))))
        except (TypeError, ValueError):
            q["focusX"] = 0.5
        for k in ("lights", "water", "sky", "trees", "fire", "hidden", "confirmed"):
            q[k] = bool(p.get(k, base.get(k, False)))
        q["fireBox"] = _box(p.get("fireBox", base.get("fireBox")))
        q["transitionIn"] = p.get("transitionIn") if p.get("transitionIn") in TRANSITIONS else None
        photos.append(q)
    out["photos"] = photos
    pick = lambda key, allowed, dflt: incoming.get(key) if incoming.get(key) in allowed else current.get(key, dflt)  # noqa: E731
    out["theme"] = pick("theme", THEMES, "classic")
    out["style"] = pick("style", STYLES, "cinematic")
    out["transition"] = pick("transition", TRANSITIONS, "cut")
    out["situation"] = pick("situation", SITUATIONS, "just_listed")
    out["metal"] = pick("metal", METALS, "gold")
    out["music"] = pick("music", MUSIC, "elegant")
    out["music"] = MUSIC_LEGACY.get(out["music"], out["music"])
    out["format"] = pick("format", FORMATS, "16:9")
    try:
        ln = int(incoming.get("length", current.get("length") or 30) or 30)
    except (TypeError, ValueError):
        ln = 30
    out["length"] = ln if ln in LENGTHS else 30
    want_in = incoming.get("want", current.get("want") or [])
    out["want"] = [w for w in dict.fromkeys(want_in if isinstance(want_in, list) else []) if w in ROOMS and w != "skip"][:20]
    acc = str(incoming.get("accent") or current.get("accent") or "#C9A24D")
    out["accent"] = acc if len(acc) == 7 and acc.startswith("#") and all(c in _HEX for c in acc[1:]) else "#C9A24D"
    out["sharpen"] = bool(incoming.get("sharpen", current.get("sharpen", True)))
    copy_in = incoming.get("copy") or {}
    out["copy"] = {k: _txt(copy_in.get(k, (current.get("copy") or {}).get(k)), 60) for k in ("kicker", "tagline", "title", "cta")}
    facts_in = incoming.get("facts") or {}
    cur_facts = current.get("facts") or {}
    out["facts"] = {k: _txt(facts_in.get(k, cur_facts.get(k)), 90 if k == "courtesy" else 60)
                    for k in ("address", "cityLine", "price", "wasPrice", "beds", "baths", "sqft", "acres", "when", "courtesy")}
    ag_in = incoming.get("agent") or {}
    cur_ag = current.get("agent") or {}
    photo = ag_in.get("photo", cur_ag.get("photo"))
    out["agent"] = {
        "name": _txt(ag_in.get("name", cur_ag.get("name")), 60), "brokerage": _txt(ag_in.get("brokerage", cur_ag.get("brokerage")), 80),
        "phone": _txt(ag_in.get("phone", cur_ag.get("phone")), 30), "email": _txt(ag_in.get("email", cur_ag.get("email")), 80),
        "photo": photo if isinstance(photo, str) and photo.startswith(("http://", "https://")) else None,
    }
    sec_in = incoming.get("sections") or {}
    cur_sec = current.get("sections") or {}
    out["sections"] = {
        "opening": bool(sec_in.get("opening", cur_sec.get("opening", True))),
        "labels": bool(sec_in.get("labels", cur_sec.get("labels", True))),
        "stats": bool(sec_in.get("stats", cur_sec.get("stats", True))),
        "agent": sec_in.get("agent") if sec_in.get("agent") in ("off", "opening", "always") else cur_sec.get("agent", "opening"),
        "endCard": bool(sec_in.get("endCard", cur_sec.get("endCard", True))),
    }
    return out


async def update_settings(db, owner_id: str, asset_id: str, settings: dict) -> StudioMediaAsset:
    asset = await _owned_motion(db, owner_id, asset_id, lock=True)
    opts = dict(asset.options or {})
    opts["motion"] = _clean_settings(settings or {}, opts.get("motion") or {})
    opts["current_hash"] = settings_hash(opts["motion"])
    asset.options = opts
    flag_modified(asset, "options")
    await db.commit()
    await db.refresh(asset)
    kick_prep(asset)
    return asset


async def add_photos(db, user: User, asset_id: str, photos: list[str]) -> StudioMediaAsset:
    asset = await _owned_motion(db, user.id, asset_id, lock=True)
    config = await ai_config_service.get_config(db)
    opts = dict(asset.options or {})
    motion = dict(opts.get("motion") or {})
    pool = list(_ensure_pool(motion))
    have = {it.get("source") for it in pool} | {p.get("source") for p in motion.get("photos") or []}
    new = [u for u in dict.fromkeys(photos) if isinstance(u, str) and u.startswith(("http://", "https://")) and u not in have]
    if len(pool) + len(new) > POOL_MAX + 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This project already has plenty of photos to choose from.")
    mx = int(config.get("studio_motion_max_photos", 15))
    pool += [_new_pool(u) for u in new]
    motion["pool"] = pool
    # Added photos go straight into the video while there's room (the agent asked for them) - room
    # for the chosen length (3/5/7/9 photos), not just the overall maximum, so a 30-second video
    # never ends up with 8 photos ticked.
    per_length = {15: 3, 30: 5, 45: 7, 60: 9}.get(int(motion.get("length") or 30), 5)
    shown = sum(1 for p in motion.get("photos") or [] if not p.get("hidden"))
    room = max(0, min(mx - len(motion.get("photos") or []), per_length - shown))
    motion["photos"] = list(motion.get("photos") or []) + [_new_photo(u) for u in new[:room]]
    opts["motion"] = motion
    opts["current_hash"] = settings_hash(motion)
    asset.options = opts
    flag_modified(asset, "options")
    await db.commit()
    await db.refresh(asset)
    kick_prep(asset)
    return asset


async def remove_photo(db, owner_id: str, asset_id: str, photo_id: str) -> StudioMediaAsset:
    asset = await _owned_motion(db, owner_id, asset_id, lock=True)
    opts = dict(asset.options or {})
    motion = dict(opts.get("motion") or {})
    keep, gone = [], []
    for p in motion.get("photos") or []:
        (gone if p.get("id") == photo_id else keep).append(p)
    motion["photos"] = keep
    opts["motion"] = motion
    opts["current_hash"] = settings_hash(motion)
    asset.options = opts
    flag_modified(asset, "options")
    await db.commit()
    await db.refresh(asset)
    in_pool = {it.get("source") for it in motion.get("pool") or []}
    for p in gone:
        if p.get("source") in in_pool:
            continue  # its files stay with the pool (it can be picked again); deleted with the project
        for k in ("clean", "depth", "sharp"):
            await asyncio.to_thread(s3_service.delete_by_url, p.get(k))
    return asset


async def _owned_motion(db, owner_id: str, asset_id: str, lock: bool = False) -> StudioMediaAsset:
    q = select(StudioMediaAsset).where(StudioMediaAsset.id == asset_id, StudioMediaAsset.owner_id == owner_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    asset = (await db.execute(q)).scalar_one_or_none()
    if not asset or asset.action != "motion":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return asset


# ------------------------------------------------------------------ costs

async def estimate(db, user: User, photos: int, sharpen: bool, seconds: float = 30) -> dict:
    config = await ai_config_service.get_config(db)
    n = max(1, min(int(photos or 1), int(config.get("studio_motion_max_photos", 15))))
    from app.services.ai.ai_copywrite_service import estimate_photo_sort_usd
    analyze = estimate_photo_sort_usd(config, n) + n * (float(config.get("studio_motion_depth_price_per_photo_usd", 0.0) or 0.0)
                                                        + float(config.get("studio_motion_erase_price_per_photo_usd", 0.0) or 0.0))
    sharp = n * float(config.get("studio_motion_sharpen_price_per_photo_usd", 0.006) or 0.0) if sharpen else 0.0
    snap = await monthly_ai_budget_snapshot(db, user.id, config, user.plan_tier)
    limit = snap.get("limit_usd")
    pct = (lambda v: round(v / float(limit) * 100, 2) if limit else None)
    paid = vl.enabled()
    render = vl.estimate_usd(seconds, config) if paid else 0.0
    would_refuse = await studio_motion_would_refuse(db, user.id, config, user.plan_tier)
    return {
        "render_usd": render, "render_percent": pct(render) if paid else None, "render_paid": paid,
        "render_paused": bool(paid and would_refuse),
        "photos": n, "analyze_usd": round(analyze, 4), "sharpen_usd": round(sharp, 4),
        "analyze_percent": pct(analyze), "sharpen_percent": pct(sharp), "month_percent": snap.get("percent"),
        "soft_capped": await studio_soft_capped(db, user.id, config, user.plan_tier),
        # sharpening will be skipped at Make (any AI limit, not just the soft cap) - the page says so
        "sharpen_paused": would_refuse,
    }


def _rate_http(exc: AIRateLimitExceeded) -> HTTPException:
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.user_message)


# ------------------------------------------------------------------ photo prep (clean copy, depth, sharpen)

async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    from app.utils.safe_fetch import safe_get
    r = await safe_get(client, url, headers={"User-Agent": "Mozilla/5.0 (OpenHousePal media)"})
    r.raise_for_status()
    if not r.content:
        raise ValueError("empty photo")
    return r.content


def _make_clean(data: bytes, crop_badge: bool) -> bytes:
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    w, h = im.size
    if crop_badge:
        im = im.crop((0, 0, w, int(round(h * (1 - _BADGE_STRIP)))))
    if im.width > _CLEAN_MAX_W:
        im = im.resize((_CLEAN_MAX_W, round(_CLEAN_MAX_W * im.height / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, subsampling=0)
    return buf.getvalue()


def _soften_depth(data: bytes, width: int) -> bytes:
    from PIL import Image, ImageFilter
    d = Image.open(io.BytesIO(data)).convert("L")
    d = d.filter(ImageFilter.GaussianBlur(max(1.5, 3.0 * d.width / 1440)))
    buf = io.BytesIO()
    d.save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def _fal(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    key = os.environ.get("FAL_KEY")
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Photo depth isn't set up on this server yet.")
    r = await client.post(url, headers={"Authorization": f"Key {key}"}, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()


def _match_depth(sharp: bytes, base: bytes) -> bytes:
    """Marigold's depth (near = black, crisp edges) in the video's format (near = white): its values
    are matched one-for-one to depth-anything's map of the same photo (same spread of near and far,
    which the camera settings are tuned to), keeping Marigold's crisp outlines. Tested 11/12."""
    from PIL import Image, ImageFilter, ImageOps

    def cdf(h):
        t = float(sum(h)) or 1.0
        out, run = [], 0.0
        for v in h:
            out.append((run + v / 2) / t)
            run += v
        return out

    m = Image.open(io.BytesIO(sharp))
    m = (m.convert("I").point(lambda x: x * (1 / 256)) if m.mode.startswith("I") else m).convert("L")
    inv = ImageOps.invert(m)
    ref = Image.open(io.BytesIO(base)).convert("L")
    cm, cd = cdf(inv.histogram()), cdf(ref.histogram())
    lut, u = [], 0
    for v in range(256):
        while u < 255 and cd[u] < cm[v]:
            u += 1
        lut.append(u)
    out = inv.point(lut).filter(ImageFilter.GaussianBlur(0.7))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=94)
    return buf.getvalue()


async def _fal_image(client: httpx.AsyncClient, url: str, payload: dict) -> bytes:
    out = await _fal(client, url, payload)
    img = (out or {}).get("image") or {}
    if not img.get("url"):
        raise ValueError("no depth map")
    return (await client.get(img["url"], timeout=60)).content


def _badge_inputs(data: bytes) -> tuple[str, str, tuple[int, int]]:
    """The photo and a mask over the Bright badge (bottom-left; ~11% of the width wide, 3.5% of the
    width tall on every Bright photo checked 11/12), as data URIs for LaMa."""
    from PIL import Image, ImageDraw, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rectangle((0, h - round(w * 0.045), round(w * 0.125), h), fill=255)
    a, b = io.BytesIO(), io.BytesIO()
    im.save(a, "JPEG", quality=95)
    mask.save(b, "PNG")
    uri = lambda buf, t: f"data:image/{t};base64," + base64.b64encode(buf.getvalue()).decode()
    return uri(a, "jpeg"), uri(b, "png"), (w, h)


async def _erase_badge(client: httpx.AsyncClient, data: bytes) -> bytes:
    """Paint the Bright badge out with LaMa (~0.1-0.4 cents) instead of cutting a strip off the
    bottom - the owner wants the whole photo kept. Same size out as in."""
    img, mask, size = await asyncio.to_thread(_badge_inputs, data)
    out = await _fal_image(client, _FAL_ERASE, {"image_url": img, "mask_image_url": mask})

    def _fit(b: bytes) -> bytes:  # LaMa pads to a multiple of 8
        from PIL import Image
        im = Image.open(io.BytesIO(b)).convert("RGB")
        if im.size != size:
            im = im.crop((0, 0, min(im.width, size[0]), min(im.height, size[1]))).resize(size, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=95)
        return buf.getvalue()
    return await asyncio.to_thread(_fit, out)


async def _prep_photo(client: httpx.AsyncClient, p: dict, crop_badge: bool, sharp_depth: bool = False, erase_badge: bool = False) -> dict:
    """Clean copy + depth map for one photo (skips whatever it already has). sharp_depth adds
    Marigold's crisp edges (falls back to depth-anything alone if it fails); q["sharpDepth"] says
    which one the photo got - the video lets the camera move further on a crisp one. erase_badge
    paints the MLS badge out (falls back to cutting the strip off); q["erased"] says it did."""
    q = dict(p)
    if not q.get("clean"):
        raw = await _download(client, q["source"])
        if crop_badge and erase_badge:
            try:
                raw = await _erase_badge(client, raw)
                q["erased"], crop_badge = True, False
            except Exception as exc:  # noqa: BLE001 - cutting the strip off still hides it
                logger.warning("motion badge erase failed", extra={"error": str(exc)[:200]})
        clean = await asyncio.to_thread(_make_clean, raw, crop_badge)
        q["clean"] = await asyncio.to_thread(s3_service.upload_bytes, clean, f"{_S3_PREFIX}/photos", ".jpg", "image/jpeg", "public, max-age=31536000, immutable")
    if not q.get("depth"):
        jobs = [_fal_image(client, _FAL_DEPTH, {"image_url": q["clean"]})]
        if sharp_depth:
            jobs.append(_fal_image(client, _FAL_DEPTH_SHARP, {"image_url": q["clean"], "processing_res": 1024}))
        got = await asyncio.gather(*jobs, return_exceptions=True)
        if isinstance(got[0], BaseException):
            raise got[0]
        soft = await asyncio.to_thread(_soften_depth, got[0], 0)
        final, crisp = soft, False
        if sharp_depth and not isinstance(got[1], BaseException):
            try:
                final, crisp = await asyncio.to_thread(_match_depth, got[1], soft), True
            except Exception as exc:  # noqa: BLE001 - a bad Marigold map just means the softer one
                logger.warning("motion sharp depth failed", extra={"error": str(exc)[:200]})
        elif sharp_depth:
            logger.warning("motion sharp depth failed", extra={"error": str(got[1])[:200]})
        q["depth"] = await asyncio.to_thread(s3_service.upload_bytes, final, f"{_S3_PREFIX}/depth", ".jpg", "image/jpeg", "public, max-age=31536000, immutable")
        q["sharpDepth"] = crisp
    return q


async def _sharpen_photo(client: httpx.AsyncClient, p: dict) -> dict:
    q = dict(p)
    if q.get("sharp") or not q.get("clean"):
        return q
    out = await _fal(client, _FAL_ESRGAN, {"image_url": q["clean"], "scale": 2, "model": "RealESRGAN_x4plus", "output_format": "jpeg"})
    img = (out or {}).get("image") or {}
    if not img.get("url"):
        return q  # sharpening is a nice-to-have - the clean copy still renders
    data = (await client.get(img["url"], timeout=120)).content

    def _cap(b: bytes) -> bytes:  # keep within 3200px wide - plenty for 4K-ish crops, light on the worker
        from PIL import Image
        im = Image.open(io.BytesIO(b)).convert("RGB")
        if im.width > 3200:
            im = im.resize((3200, round(3200 * im.height / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=91)
        return buf.getvalue()
    data = await asyncio.to_thread(_cap, data)
    q["sharp"] = await asyncio.to_thread(s3_service.upload_bytes, data, f"{_S3_PREFIX}/sharp", ".jpg", "image/jpeg", "public, max-age=31536000, immutable")
    return q


# ------------------------------------------------------------------ AI labels

_LABEL_TOOL = {
    "name": "describe_photos",
    "description": "Describe each numbered listing photo for a short cinematic listing video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "photos": {"type": "array", "items": {"type": "object", "properties": {
                "n": {"type": "integer"},
                "room": {"type": "string", "enum": list(ROOMS), "description": "front = the street-side exterior; back = the rear exterior; yard = backyard, patio, deck, garden; view = grounds, water or scenic views, community; bonus = gym, sunroom, game room, wine room, theater, mudroom; detail = a close-up feature; skip = floor plans, maps, garages, laundry, closets, utility rooms, unclear photos."},
                "label": {"type": "string", "description": "On-screen caption, 2-6 words, natural and specific to what is visible, e.g. 'Chef's kitchen with island', 'Pool & gardens', 'Primary suite'. Under 34 characters."},
                "kicker": {"type": "string", "description": "1-2 word small line above it, e.g. 'The kitchen', 'Outdoors', 'Retreat'. Under 16 characters."},
                "sure": {"type": "boolean", "description": "False when you are not sure what room this is (the agent will check it)."},
                "score": {"type": "integer", "description": (
                    "How well this photo would show off its room as a slow camera shot in a premium listing video, 1-10. "
                    "9-10: the room's best features fill the frame, close and clear (a kitchen's island, cabinets and counters; a living room's seating and fireplace; a bed and its setting), bright, sharp, level, furnished or staged. "
                    "6-8: good, but the room sits small or far away in the frame, is shot from a doorway or across another room, or is plain. "
                    "3-5: mostly bare walls, doors, closets or a hallway; an empty unfurnished bedroom; an awkward angle. "
                    "1-2: dark, blurry, cluttered, or a tight corner. "
                    "Be critical: most photos are 5-8; give 9-10 only to a room's standout shot. "
                    "Compare photos of the same room with each other and give the best one the highest score - don't give them all the same score.")},
                "drawn": {"type": "boolean", "description": (
                    "True when something was drawn or typed onto the photo after it was taken: a box or outline "
                    "marking part of the home, arrows, words, banners, property lines. Ignore a small MLS logo "
                    "in a corner.")},
                "focus_x": {"type": "number", "description": "Where the main subject sits left-to-right, 0 to 1."},
                "lights": {"type": "boolean", "description": "Warm lights visibly glowing (lit windows, lanterns, lamps) - typical of twilight photos."},
                "water": {"type": "boolean", "description": "A pool, pond or lake is clearly visible."},
                "sky": {"type": "boolean", "description": "Open sky is visible."},
                "trees": {"type": "boolean", "description": "Trees or shrubs are visible outdoors."},
                "fire": {"type": "boolean", "description": "A fire is burning in a fireplace or fire pit - real flames are visible. False for an unlit fireplace."},
                "fire_box": {"type": "array", "items": {"type": "number"}, "description": "Only when fire is true: where the flames are, as [left, top, right, bottom], each 0 to 1 of the photo's width/height (top = 0). Tight around the flames, not the whole fireplace."},
            }, "required": ["n", "room", "label", "kicker", "sure", "score", "drawn", "focus_x", "lights", "water", "sky", "trees", "fire"]}},
            "tagline": {"type": "string", "description": "One short, elegant opening line under 40 characters about the home itself, supported by the photos or listing text. No people, no who-it-suits."},
            "best": {"type": "array", "description": (
                "For every room that has two or more photos: the n of the ONE photo that shows that room best in a "
                "video - the biggest, clearest, closest view of what makes it appealing (the kitchen's island and "
                "cabinets filling the frame beats the kitchen seen from the next room; a bed and its setting beats "
                "a view of doors and a closet)."), "items": {"type": "integer"}},
        },
        "required": ["photos", "tagline", "best"],
    },
}
_LABEL_RULES = (
    "Rules: describe only what is visible or stated in the listing; never invent features. Never use "
    "'master' (say 'primary'). Never describe people, families, ages, religion, or who the home is "
    "suited for (Fair Housing). Prefer warm, specific, editorial wording over generic ('Sunlit living "
    "room' beats 'Living room'). Mark sure=false for bedrooms you can't tell are the primary, bonus "
    "rooms, or anything ambiguous."
)


async def _label_photos(db, user: User, photos: list[dict], listing_text: str, config: dict) -> tuple[dict, float]:
    """Every candidate photo labelled: which room it is, a caption, how good it looks. Up to
    LABEL_BATCH photos per vision call (bigger listings run a few calls side by side), recorded as
    ONE usage event so the room-sorting still counts once toward the agent's video limits."""
    from app.services.ai.ai_copywrite_service import _require_key
    _require_key()
    model = config.get("studio_motion_label_model") or config.get("photo_sort_model") or "claude-sonnet-5"
    batches = [photos[i:i + LABEL_BATCH] for i in range(0, len(photos), LABEL_BATCH)]
    results = await asyncio.gather(*[_label_batch(b, listing_text, model) for b in batches])
    out: dict = {"photos": {}, "tagline": "", "seen": set()}
    tin = tout = 0
    for part, i_tok, o_tok in results:
        out["photos"].update(part["photos"])
        out["seen"] |= part["seen"]
        out["tagline"] = out["tagline"] or part["tagline"]
        tin += i_tok
        tout += o_tok
    if not tin and not tout:
        return out, 0.0
    cost = ai_config_service.estimate_cost_usd(config, model, tin, tout)
    await record_usage_event(db, user.id, None, model, tin, tout, cost, 0, False, None, event_type=EVENT_TYPE)
    return out, float(cost)


async def _label_batch(photos: list[dict], listing_text: str, model: str) -> tuple[dict, int, int]:
    """One vision call over up to LABEL_BATCH photos. Results are keyed by the photo's source URL;
    returns (labels, input tokens, output tokens) - the caller records the usage."""
    from app.services.ai.ai_copywrite_service import _client, _thumb
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        thumbs = await asyncio.gather(*[_thumb(client, p.get("clean") or p["source"]) for p in photos])
    content: list = [{"type": "text", "text": "Listing (context only):\n" + (listing_text or "(no description)")[:5000]
                      + "\n\nListing photos for a cinematic video follow."}]
    index: dict = {}
    for i, (p, b64) in enumerate(zip(photos, thumbs), start=1):
        if not b64:
            continue
        index[i] = p["source"]
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    if not index:
        return {"photos": {}, "tagline": "", "seen": set()}, 0, 0
    content.append({"type": "text", "text": _LABEL_RULES + "\nCall describe_photos."})
    resp = await _client().messages.create(
        model=model, max_tokens=min(12000, 800 + 130 * len(index)), tools=[_LABEL_TOOL], tool_choice={"type": "tool", "name": "describe_photos"},
        messages=[{"role": "user", "content": content}],
    )
    if getattr(resp, "stop_reason", None) == "max_tokens":
        logger.warning("motion photo labels cut off", extra={"photos": len(index), "output_tokens": resp.usage.output_tokens})
    out: dict = {"photos": {}, "tagline": "", "seen": set(index.values())}
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and b.name == "describe_photos":
            for it in b.input.get("photos", []) or []:
                try:
                    src = index.get(int(it.get("n", 0) or 0))
                except (TypeError, ValueError):
                    src = None
                if src:
                    if it.get("drawn"):  # a box/arrow/words on the photo reads as our own design - last pick
                        it["score"] = 1
                    if it.get("fire"):
                        b64 = thumbs[int(it.get("n")) - 1]
                        it["fire_box"] = _find_flames(b64, it.get("fire_box")) if b64 else None
                    out["photos"][src] = it
            out["tagline"] = _txt(b.input.get("tagline"), 60)
            # The AI's pick of each room's best photo goes just above that room's other photos (scores
            # alone tie: a batch scorer hands several photos of one room the same top mark).
            best = set()
            for v in b.input.get("best") or []:
                try:
                    best.add(int(v))
                except (TypeError, ValueError):
                    pass
            by_room: dict = {}
            for it in out["photos"].values():
                by_room.setdefault(it.get("room"), []).append(it)
            for group in by_room.values():
                top = max((_float(x.get("score")) for x in group), default=0.0)
                for it in group:
                    if len(group) > 1 and _float(it.get("n")) in best and not it.get("drawn"):
                        it["score"] = top + 1
                        break
    return out, int(resp.usage.input_tokens or 0), int(resp.usage.output_tokens or 0)


def _apply_label(item: dict, lab: dict) -> None:
    room = lab.get("room") if lab.get("room") in ROOMS else "skip"
    item["room"] = room
    item["kind"] = ROOMS[room][0]
    item["label"] = _txt(lab.get("label"), 48)
    item["kicker"] = _txt(lab.get("kicker"), 28) or ROOMS[room][1]
    item["sure"] = bool(lab.get("sure", True))
    # The AI scores 1-10 (11/12: on 1-5 nearly every kitchen photo tied at 5 and the first - a far
    # doorway shot - won). Stored on the old 1-5 scale in half steps so older projects compare the same.
    try:
        item["score"] = max(0.5, min(5.5, round(float(lab.get("score", 6)) / 2, 1)))  # 5.5 = the AI's best of a room at 10
    except (TypeError, ValueError):
        item["score"] = 3
    try:
        item["focusX"] = min(1.0, max(0.0, float(lab.get("focus_x", 0.5))))
    except (TypeError, ValueError):
        item["focusX"] = 0.5
    for k in ("lights", "water", "sky", "trees"):
        item[k] = bool(lab.get(k, False))
    # flames only move where the AI says they are - a colour match alone would flicker wood floors
    item["fireBox"] = _box(lab.get("fire_box")) if lab.get("fire") else None
    item["fire"] = bool(item["fireBox"])
    item["analyzed"] = True


def _listing_text(lp: Optional[dict]) -> str:
    lp = lp or {}
    bits = [str(lp.get("description") or "")]
    facts = ", ".join(x for x in [
        f"{_num(lp.get('beds'))} beds" if lp.get("beds") else "", f"{_num(lp.get('baths'), 1)} baths" if lp.get("baths") else "",
        f"{_num(lp.get('sqft'))} sq ft" if lp.get("sqft") else "", f"{_num(lp.get('acres'), 2)} acres" if lp.get("acres") else "",
        str(lp.get("property_type") or ""),
    ] if x)
    return (facts + "\n" + "\n".join(bits)).strip()


async def start_analysis(db, user: User, asset_id: str) -> StudioMediaAsset:
    asset = await _owned_motion(db, user.id, asset_id)
    opts = dict(asset.options or {})
    if (opts.get("analysis") or {}).get("status") == "running" and asset_id in _TASKS and not _TASKS[asset_id].done():
        return asset
    motion = dict(opts.get("motion") or {})
    pool = _ensure_pool(motion)
    if not any(not it.get("analyzed") for it in pool):
        # Nothing new to look at (no AI needed) - just make sure the picked photos are getting ready.
        if (opts.get("analysis") or {}).get("status") != "done":
            opts["analysis"] = {"status": "done", "finished_at": _now(), "cost_usd": 0.0}
            opts["motion"] = motion
            asset.options = opts
            flag_modified(asset, "options")
            await db.commit()
            await db.refresh(asset)
        kick_prep(asset)
        return asset
    config = await ai_config_service.get_config(db)
    try:
        reservation = await reserve_studio_motion(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as exc:
        raise _rate_http(exc)
    opts["motion"] = motion
    opts["analysis"] = {"status": "running", "started_at": _now(), "step": "labels", "done": 0,
                        "total": sum(1 for it in pool if not it.get("analyzed"))}
    asset.options = opts
    flag_modified(asset, "options")
    await db.commit()
    await db.refresh(asset)
    _TASKS[asset_id] = asyncio.create_task(_run_analysis(asset_id, user.id, reservation))
    return asset


async def _patch(asset_id: str, fn) -> Optional[StudioMediaAsset]:
    """Read-modify-write of one asset's options in its own short session."""
    async with AsyncSessionLocal() as db:
        # Row lock: the agent's autosave and the background steps (labels, depth, render) write the
        # same options - one at a time, so neither loses the other's change.
        asset = await db.get(StudioMediaAsset, asset_id, with_for_update=True)
        if not asset:
            return None
        opts = dict(asset.options or {})
        fn(asset, opts)
        asset.options = opts
        flag_modified(asset, "options")
        await db.commit()
        await db.refresh(asset)
        return asset


def _needs_prep(motion: dict) -> list[dict]:
    seen, out = set(), []
    for p in motion.get("photos") or []:
        if p.get("hidden") or p.get("prep_failed") or (p.get("clean") and p.get("depth")) or p.get("source") in seen:
            continue
        seen.add(p.get("source"))
        out.append(p)
    return out


def kick_prep(asset: StudioMediaAsset) -> None:
    """Clean copy + depth map for the photos in the video (free - no AI usage), in the background
    as soon as they're picked, so the preview gets its 3D camera and Make starts right away."""
    if asset.id in _PREP and not _PREP[asset.id].done():
        return
    if _needs_prep((asset.options or {}).get("motion") or {}):
        _PREP[asset.id] = asyncio.create_task(_run_prep(asset.id))


async def _run_prep(asset_id: str) -> None:
    try:
        for _ in range(8):  # the agent may pick more photos while this runs - go round again
            async with AsyncSessionLocal() as db:
                asset = await db.get(StudioMediaAsset, asset_id)
                if not asset:
                    return
                motion = dict((asset.options or {}).get("motion") or {})
                crop = (asset.source_kind or "listing") == "listing"
                owner_id = asset.owner_id
                todo = _needs_prep(motion)
                if not todo:
                    break
                config = await ai_config_service.get_config(db)
                owner = await db.get(User, owner_id)
                # crisp depth costs ~half a cent a photo: not once the agent's AI Media is paused
                capped = bool(owner and await studio_soft_capped(db, owner_id, config, owner.plan_tier))
                sharp = bool(config.get("studio_motion_sharp_depth", True)) and not capped
                # painting the badge out: same pause rule (paused = the old strip cut)
                erase = bool(config.get("studio_motion_erase_badge", True)) and not capped
                price = float(config.get("studio_motion_depth_price_per_photo_usd", 0.007) or 0.0)
                erase_price = float(config.get("studio_motion_erase_price_per_photo_usd", 0.004) or 0.0)
            total = len(todo)
            await _patch(asset_id, lambda a, o: o.__setitem__("prep", {"running": True, "done": 0, "total": total}))
            results: dict[str, dict] = {}
            failed: set = set()
            made = {"n": 0, "erased": 0}
            sem = asyncio.Semaphore(4)
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True) as client:
                async def one(p):
                    async with sem:
                        try:
                            had_depth = bool(p.get("depth"))
                            q = await _prep_photo(client, p, crop, sharp, erase)
                            if q.get("erased") and not p.get("clean"):
                                made["erased"] += 1
                            results[p["source"]] = {"clean": q.get("clean"), "depth": q.get("depth")}
                            if not had_depth and q.get("depth"):
                                results[p["source"]]["sharpDepth"] = bool(q.get("sharpDepth"))
                                made["n"] += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("motion photo prep failed", extra={"asset_id": asset_id, "error": str(exc)[:200]})
                            failed.add(p["source"])
                        n = len(results) + len(failed)
                        await _patch(asset_id, lambda a, o: o.__setitem__("prep", {"running": True, "done": n, "total": total}))
                await asyncio.gather(*[one(p) for p in todo])
            spent = round(made["n"] * price, 4)
            erased = round(made["erased"] * erase_price, 4)
            if spent or erased:
                async with AsyncSessionLocal() as db:
                    if spent:
                        await record_usage_event(db, owner_id, None, "fal:depth", None, None, spent, 0, False, None, event_type=EVENT_TYPE)
                    if erased:
                        await record_usage_event(db, owner_id, None, "fal:erase", None, None, erased, 0, False, None, event_type=EVENT_TYPE)

            def apply(a, o):
                m = dict(o.get("motion") or {})
                photos = []
                for p in m.get("photos") or []:
                    if p.get("source") in results:
                        p = {**p, **{k: v for k, v in results[p["source"]].items() if v}}
                    elif p.get("source") in failed:
                        p = {**p, "prep_failed": True}
                    photos.append(p)
                m["photos"] = photos
                m["pool"] = [({**it, **{k: v for k, v in results[it["source"]].items() if v}} if it.get("source") in results else it)
                             for it in _ensure_pool(m)]
                o["motion"] = m
            await _patch(asset_id, apply)
    except Exception:  # noqa: BLE001
        logger.exception("motion prep failed", extra={"asset_id": asset_id})
    finally:
        _PREP.pop(asset_id, None)
        await _patch(asset_id, lambda a, o: o.__setitem__("prep", {"running": False}))


async def _run_analysis(asset_id: str, owner_id: str, reservation) -> None:
    spent = 0.0
    try:
        async with AsyncSessionLocal() as db:
            asset = await db.get(StudioMediaAsset, asset_id)
            user = await db.get(User, owner_id)
            config = await ai_config_service.get_config(db)
            motion = dict((asset.options or {}).get("motion") or {})
            crop = (asset.source_kind or "listing") == "listing"
            lp = asset.linked_property
        pool = _ensure_pool(motion)
        # Photos added straight into the video (older projects, Add photos) are in the pool too.
        need = [it for it in pool if not it.get("analyzed")]
        labels: dict = {"photos": {}, "seen": set()}
        if need:
            async with AsyncSessionLocal() as db:
                user = await db.get(User, owner_id)
                labels, cost = await _label_photos(db, user, need, _listing_text(lp), config)
            spent += cost
        tried = {it["source"] for it in need}

        def apply(asset, opts):
            m = dict(opts.get("motion") or {})
            pool = [dict(it) for it in _ensure_pool(m)]
            for it in pool:
                if it.get("analyzed") or it["source"] not in tried:
                    continue
                lab = (labels.get("photos") or {}).get(it["source"])
                if lab:
                    _apply_label(it, lab)
                else:  # the photo wouldn't load - never offered
                    it.update(room="skip", analyzed=True, sure=False, score=1)
            m["pool"] = pool
            by_src = {it["source"]: it for it in pool}
            photos = []
            for p in m.get("photos") or []:
                it = by_src.get(p.get("source"))
                if it and it.get("analyzed") and not p.get("analyzed") and not p.get("confirmed"):
                    p = {**p, **{k: it.get(k) for k in ("room", "kind", "label", "kicker", "sure", "focusX", "lights", "water", "sky", "trees", "fire", "fireBox")}, "analyzed": True}
                photos.append(p)
            m["photos"] = photos
            copy = dict(m.get("copy") or {})
            if labels.get("tagline") and not copy.get("tagline"):
                copy["tagline"] = labels["tagline"]
            m["copy"] = copy
            opts["motion"] = m
            opts["current_hash"] = settings_hash(m)
            opts["analysis"] = {"status": "done", "finished_at": _now(), "cost_usd": round(spent, 4)}
            asset.cost_usd = round(float(asset.cost_usd or 0) + spent, 4)
        asset = await _patch(asset_id, apply)
        async with AsyncSessionLocal() as db:
            await reconcile_budget(db, reservation, spent)
        if asset:
            kick_prep(asset)
    except Exception as exc:  # noqa: BLE001
        logger.exception("motion analysis failed", extra={"asset_id": asset_id})
        msg = str(exc) if isinstance(exc, RuntimeError) else "We couldn't look at those photos just now. Try again in a moment."
        if isinstance(exc, HTTPException):
            msg = exc.detail
        await _patch(asset_id, lambda a, o: o.__setitem__("analysis", {"status": "failed", "error": msg, "finished_at": _now()}))
        try:
            async with AsyncSessionLocal() as db:
                await reconcile_budget(db, reservation, spent)
        except Exception:  # noqa: BLE001
            pass
    finally:
        _TASKS.pop(asset_id, None)


# ------------------------------------------------------------------ render

def settings_hash(motion: dict) -> str:
    # Only what changes the video itself (not the photo pool, room picks or checkmarks).
    keep = {k: v for k, v in (motion or {}).items() if k not in ("ordered_by_ai", "pool", "want")}
    keep["photos"] = [{k: v for k, v in p.items() if k not in ("confirmed", "sure", "analyzed", "clean", "depth", "sharp", "sharpDepth", "room", "prep_failed", "score")}
                      for p in keep.get("photos") or []]
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:16]


def worker_props(motion: dict, fmt: str) -> dict:
    photos = []
    for p in motion.get("photos") or []:
        src = p.get("sharp") or p.get("clean") or p.get("source")  # sharpened copy whenever there is one (always on, 11/12)
        photos.append({
            "id": p["id"], "src": src, "depth": p.get("depth"), "sharpDepth": bool(p.get("sharpDepth")), "kind": p.get("kind", "interior"),
            "label": p.get("label") or "", "kicker": p.get("kicker") or "", "showLabel": bool(p.get("showLabel", True)),
            "move": p.get("move") or "auto", "focusX": p.get("focusX", 0.5),
            "lights": bool(p.get("lights")), "water": bool(p.get("water")), "sky": bool(p.get("sky")), "trees": bool(p.get("trees")),
            "fire": bool(p.get("fire")), "fireBox": _box(p.get("fireBox")) if p.get("fire") else None,
            "transitionIn": p.get("transitionIn"), "hidden": bool(p.get("hidden")),
        })
    return {
        "photos": photos, "theme": motion.get("theme", "classic"), "accent": motion.get("accent", "#C9A24D"),
        "metal": motion.get("metal", "gold"), "style": motion.get("style", "cinematic"),
        "transition": motion.get("transition", "cut"), "situation": motion.get("situation", "just_listed"),
        "copy": motion.get("copy") or {}, "facts": motion.get("facts") or {}, "agent": motion.get("agent") or {"name": ""},
        "sections": motion.get("sections") or {}, "music": MUSIC_LEGACY.get(motion.get("music") or "elegant", motion.get("music") or "elegant"), "format": fmt,
        "length": motion.get("length") if motion.get("length") in LENGTHS else None,
    }


async def worker_alive() -> bool:
    return (await worker_status())["worker"]


async def worker_status() -> dict:
    """Is the video maker up, and how long would a video take right now (its measured speed + the
    line ahead) - the Make step's "To make" figure (QA 11/12: it was a fixed guess)."""
    if vl.enabled():  # AWS Lambda (owner 11/13): always up; a short line only while every slot is busy
        h = vl.health()
        try:
            making, waiting = await _lambda_line()
            wait = await _line_wait_seconds(making, len(waiting))
        except Exception:  # noqa: BLE001
            making, waiting, wait = [], [], 0
        return {"worker": True, "wait_seconds": round(wait), "sec_per_frame": h["sec_per_frame"], "start_seconds": h["start_seconds"],
                "save_seconds": round(_SAVE_SECS["v"]), "queued": len(waiting),
                "busy": len(making) >= vl.max_videos()}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{_worker_url()}/health")
        if r.status_code != 200:
            return {"worker": False}
        h = r.json()
        return {"worker": True, "wait_seconds": h.get("wait_seconds"), "sec_per_frame": h.get("sec_per_frame"),
                "start_seconds": h.get("start_seconds"), "save_seconds": round(_SAVE_SECS["v"]), "queued": h.get("queued"),
                "busy": bool(h.get("running"))}
    except (httpx.HTTPError, ValueError):
        return {"worker": False}


async def start_render(db, user: User, asset_id: str, sharpen: Optional[bool], fmt: Optional[str]) -> StudioMediaAsset:
    asset = await _owned_motion(db, user.id, asset_id)
    opts = dict(asset.options or {})
    motion = dict(opts.get("motion") or {})
    render = opts.get("render") or {}
    if render.get("state") in ("preparing", "queued", "rendering", "finishing") and asset_id in _TASKS and not _TASKS[asset_id].done():
        return asset
    visible = [p for p in motion.get("photos") or [] if not p.get("hidden")]
    if len(visible) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Keep at least 2 photos in the video.")
    unready = bool(_needs_prep(motion))
    if not await worker_alive():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The video maker is restarting. Try again in a minute.")
    # Owner 11/12: photos are always sharpened - there's no switch any more (the old `sharpen`
    # field is ignored). Out of AI for the month? The video is still made, just without it.
    motion["sharpen"] = True
    target = fmt if fmt in FORMATS else motion.get("format", "16:9")
    main = target == motion.get("format", "16:9")
    config = await ai_config_service.get_config(db)
    need_sharp = [p for p in visible if not p.get("sharp")]
    reservation = None
    skip_sharp = False
    if vl.enabled():
        # Making the video now costs real money (AWS Lambda, owner 11/13) and counts on the agent's
        # monthly AI usage - so it needs AI left this month, like every other AI Media tool.
        try:
            reservation = await reserve_studio_motion(db, user.id, config, user.plan_tier)
        except AIRateLimitExceeded as exc:
            raise _rate_http(exc)
    elif need_sharp:
        if await studio_soft_capped(db, user.id, config, user.plan_tier):
            skip_sharp = True
        else:
            try:
                reservation = await reserve_studio_motion(db, user.id, config, user.plan_tier)
            except AIRateLimitExceeded:
                skip_sharp = True
        if skip_sharp:
            need_sharp = []
    opts["motion"] = motion
    opts["render"] = {
        "state": "preparing" if (need_sharp or unready) else "queued", "progress": 0.0,
        "stage": "Getting your photos ready" if unready else "Sharpening photos" if need_sharp else "Waiting for the video maker",
        "started_at": _now(), "target": target, "main": main, "sharpen_count": len(need_sharp), "hash": settings_hash(motion),
        "skip_sharp": skip_sharp,
    }
    if asset.status in ("draft", "failed") and main:
        asset.status = "processing"
        asset.error_message = None
    asset.options = opts
    flag_modified(asset, "options")
    await db.commit()
    await db.refresh(asset)
    _TASKS[asset_id] = asyncio.create_task(_run_render(asset_id, user.id, reservation, resume_job=None))
    return asset


async def _run_render(asset_id: str, owner_id: str, reservation, resume_job: Optional[str]) -> None:
    spent = 0.0
    try:
        async with AsyncSessionLocal() as db:
            asset = await db.get(StudioMediaAsset, asset_id)
            config = await ai_config_service.get_config(db)
            opts = dict(asset.options or {})
        motion = dict(opts.get("motion") or {})
        render = dict(opts.get("render") or {})
        target = render.get("target") or motion.get("format", "16:9")
        job_id = resume_job
        if not job_id and _needs_prep(motion):
            # 0) photos picked moments ago may still be getting their depth maps (free)
            running = _PREP.get(asset_id)
            if running and not running.done():
                await running
            else:
                await _run_prep(asset_id)
            async with AsyncSessionLocal() as db:
                fresh = await db.get(StudioMediaAsset, asset_id)
                motion = dict(((fresh.options or {}) if fresh else {}).get("motion") or {})
            await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), "stage": "Waiting for the video maker" if render.get("skip_sharp") else "Sharpening photos", "progress": 0.04}))
        if not job_id:
            # 1) sharpen (always - once per photo, kept for every later render; skipped only when the
            # agent's AI for the month ran out, start_render's skip_sharp)
            visible = [p for p in motion.get("photos") or [] if not p.get("hidden")]
            need = [] if render.get("skip_sharp") else [p for p in visible if not p.get("sharp")]
            if need:
                sharpened: dict[str, str] = {}
                sem = asyncio.Semaphore(4)
                async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
                    count = {"n": 0}

                    async def one(p):
                        async with sem:
                            try:
                                q = await _sharpen_photo(client, p)
                                if q.get("sharp"):
                                    sharpened[p["id"]] = q["sharp"]
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("motion sharpen failed", extra={"asset_id": asset_id, "error": str(exc)[:200]})
                            count["n"] += 1
                            prog = 0.12 * count["n"] / len(need)
                            await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), "progress": prog}))
                    await asyncio.gather(*[one(p) for p in need])
                price = float(config.get("studio_motion_sharpen_price_per_photo_usd", 0.006) or 0.0)
                spent = price * len(sharpened)
                if spent:
                    async with AsyncSessionLocal() as db:
                        await record_usage_event(db, owner_id, None, "fal:esrgan", None, None, spent, 0, False, None, event_type=EVENT_TYPE)

                def keep_sharp(a, o):
                    m = dict(o.get("motion") or {})
                    m["photos"] = [{**p, "sharp": sharpened.get(p["id"], p.get("sharp"))} for p in m.get("photos") or []]
                    o["motion"] = m
                    a.cost_usd = round(float(a.cost_usd or 0) + spent, 4)
                a2 = await _patch(asset_id, keep_sharp)
                motion = dict((a2.options or {}).get("motion") or {})
            if reservation is not None and not vl.enabled():
                async with AsyncSessionLocal() as db:
                    await reconcile_budget(db, reservation, spent)
                reservation = None
            # 2) hand it to the video maker (AWS Lambda when set up, else the self-hosted worker)
            props = worker_props(motion, target)
            if vl.enabled():
                job_id = await _lambda_take_turn(asset_id, props)
            else:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(f"{_worker_url()}/jobs", json={"props": props}, headers=_worker_headers())
                    r.raise_for_status()
                    job_id = r.json()["id"]
            await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), "job_id": job_id, "state": "queued",
                                                                          "stage": "Waiting for the video maker", "progress": max(0.12, (o.get("render") or {}).get("progress") or 0)}))
        # 3) follow the worker. Runs on the server, so the agent can close the page, log out or turn
        # their computer off - the bell (and their next daily briefing) tells them when it's ready.
        last = None
        resent = 0
        lam = vl.is_lambda_job(job_id)
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                await asyncio.sleep(5.0 if lam else 2.0)
                if lam:
                    j = await vl.status(job_id)
                    if j.get("busy_check"):
                        continue
                    if j.get("status") == "failed":
                        raise RuntimeError("worker: " + str(j.get("error") or "render failed"))
                    if j.get("status") == "done":
                        break
                    state = "rendering" if j.get("status") == "rendering" else "queued"
                    prog = 0.12 + 0.8 * float(j.get("progress") or 0)
                    eta = j.get("eta_seconds")
                    eta = int(eta) + int(_SAVE_SECS["v"]) if eta is not None else None
                    eta_at = (datetime.now(timezone.utc) + timedelta(seconds=eta)).isoformat() if eta is not None else None
                    prev = (last or {}).get("eta_at")
                    if prev and eta_at:
                        shift = (datetime.fromisoformat(eta_at) - datetime.fromisoformat(prev)).total_seconds()
                        if abs(shift) < max(5, 0.04 * int(eta)):
                            eta_at = prev  # steady countdown
                        elif shift < -20:
                            # never suddenly earlier: come down at most 20 s a check (owner 11/13)
                            eta_at = (datetime.fromisoformat(prev) - timedelta(seconds=20)).isoformat()
                    cur = {"state": state, "progress": round(prog, 2), "stage": "Filming your video" if state == "rendering" else "Starting the cameras",
                           "ahead": 0, "eta_at": eta_at, "wait_seconds": 0, "eta_learned": True}
                    if cur != last:
                        last = cur
                        await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), **cur, "progress": prog}))
                    continue
                try:
                    r = await client.get(f"{_worker_url()}/jobs/{job_id}", headers=_worker_headers())
                except httpx.HTTPError:
                    continue
                if r.status_code == 404:
                    # The video maker restarted and lost its line - hand the video to it again by
                    # itself (photos are already prepared, so this costs nothing) instead of asking
                    # an agent who may have logged off to press Make video again.
                    if resent >= 3:
                        raise RuntimeError("The video maker restarted part-way through. Press Make video again.")
                    resent += 1
                    try:
                        r2 = await client.post(f"{_worker_url()}/jobs", json={"props": worker_props(motion, target)}, headers=_worker_headers())
                        r2.raise_for_status()
                    except httpx.HTTPError:
                        await asyncio.sleep(15)
                        continue
                    job_id = r2.json()["id"]
                    new_id = job_id
                    await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), "job_id": new_id}))
                    continue
                j = r.json()
                if j.get("status") == "failed":
                    raise RuntimeError("worker: " + str(j.get("error") or "render failed"))
                if j.get("status") == "done":
                    break
                state = "rendering" if j.get("status") == "rendering" else "queued"
                prog = 0.12 + 0.8 * float(j.get("progress") or 0)
                ahead = int(j.get("ahead") or 0)
                stage = "Filming your video" if state == "rendering" else (
                    f"In line - {ahead} {'video' if ahead == 1 else 'videos'} ahead of yours" if ahead else "Starting the cameras")
                # When it should be ready - the page counts down to it between checks. Only moved
                # when the estimate shifts by more than a few seconds, so the countdown doesn't jump.
                eta = j.get("eta_seconds")
                if eta is not None:
                    eta = int(eta) + int(_SAVE_SECS["v"])  # + copying the finished file to storage
                eta_at = (datetime.now(timezone.utc) + timedelta(seconds=eta)).isoformat() if eta is not None else None
                prev = (last or {}).get("eta_at")
                if prev and eta_at and abs((datetime.fromisoformat(prev) - datetime.fromisoformat(eta_at)).total_seconds()) < max(5, 0.04 * int(eta)):
                    eta_at = prev
                cur = {"state": state, "progress": round(prog, 2), "stage": stage, "ahead": ahead, "eta_at": eta_at,
                       "wait_seconds": j.get("wait_seconds"), "eta_learned": bool(j.get("learned"))}
                if cur != last:
                    last = cur
                    await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), **cur, "progress": prog}))
            await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), "state": "finishing", "progress": 0.94, "stage": "Saving your video",
                                                                          "eta_at": (datetime.now(timezone.utc) + timedelta(seconds=_SAVE_SECS["v"])).isoformat()}))
            save_t0 = time.time()
            poster = None
            if lam and not await _first_to_finish(job_id):
                # another server process (the every-2-minutes pick-up, or a second copy of the app)
                # is already saving and charging this exact video - leave it to that one (QA 11/13:
                # a local test saved and charged one video three times)
                if reservation is not None:
                    async with AsyncSessionLocal() as db:
                        await reconcile_budget(db, reservation, spent)
                return
            if lam:
                video = await vl.fetch(job_id, j.get("out_key"))  # the cover is taken from the video itself below
            else:
                fr = await client.get(f"{_worker_url()}/jobs/{job_id}/file", headers=_worker_headers(), timeout=300)
                fr.raise_for_status()
                video = fr.content
                try:
                    pr = await client.get(f"{_worker_url()}/jobs/{job_id}/poster", headers=_worker_headers(), timeout=60)
                    if pr.status_code == 200 and pr.content:
                        poster = pr.content
                except httpx.HTTPError:
                    poster = None
        if lam:
            # What AWS charged for this video goes on the agent's monthly AI usage (owner 11/13).
            async with AsyncSessionLocal() as db:
                aws = vl.charge_usd(j.get("remotion_cost_usd") or 0, await ai_config_service.get_config(db))
                if aws:
                    await record_usage_event(db, owner_id, None, "aws:lambda", None, None, aws, 0, False, None, event_type=EVENT_TYPE)
            spent += aws
            if reservation is not None:
                async with AsyncSessionLocal() as db:
                    await reconcile_budget(db, reservation, spent)
                reservation = None
            render_cost = aws

            def add_cost(a, o):
                a.cost_usd = round(float(a.cost_usd or 0) + render_cost, 4)
            await _patch(asset_id, add_cost)
        frames, width = j.get("frames"), j.get("width")
        url = await asyncio.to_thread(s3_service.upload_bytes, video, f"{_S3_PREFIX}/videos", ".mp4", "video/mp4")
        from app.services.studio_media_service import _store_preview_frame
        duration = round((frames or 0) / float(j.get("fps") or 30), 2) if frames else None
        old_urls: list[str] = []
        newly_done = {"v": False}

        def finish(asset, o):
            render = dict(o.get("render") or {})
            if render.get("main", True):
                if asset.result_url:
                    old_urls.append(asset.result_url)
                for v in (o.get("formats") or {}).values():
                    old_urls.append(v.get("url") if isinstance(v, dict) else v)
                # The other shapes they'd made are now out of date - they're re-made by themselves
                # right after this one (QA 11/12: they used to just vanish), no AI, one at a time.
                o["remake_formats"] = [k for k in (o.get("formats") or {}) if k in FORMATS and k != render.get("target")]
                o["formats"] = {}
                asset.result_url = url
                o["duration_seconds"] = duration
                o["rendered_hash"] = render.get("hash")
                o["rendered_at"] = _now()
                if asset.status != "succeeded":
                    newly_done["v"] = True
                asset.status = "succeeded"
                asset.error_message = None
            else:
                fmts = dict(o.get("formats") or {})
                prev = fmts.get(render.get("target"))
                if prev:
                    old_urls.append(prev.get("url") if isinstance(prev, dict) else prev)
                fmts[render.get("target")] = {"url": url, "hash": render.get("hash"), "at": _now()}
                o["formats"] = fmts
            started = render.get("started_at")
            secs = None
            try:
                secs = round(time.time() - datetime.fromisoformat(started).timestamp()) if started else None
            except ValueError:
                pass
            o["render"] = {"state": "done", "progress": 1.0, "stage": "Done", "finished_at": _now(), "seconds": secs,
                           "target": render.get("target"), "main": render.get("main", True), "width": width}
        asset = await _patch(asset_id, finish)
        _SAVE_SECS["v"] = _SAVE_SECS["v"] * 0.6 + (time.time() - save_t0) * 0.4  # learned, for the next countdown
        async with AsyncSessionLocal() as db:
            await storage_ledger_service.record(db, owner_id, "ai_media_video", url, len(video))
            for u in old_urls:
                if u:
                    await storage_ledger_service.release(db, u)
        for u in old_urls:
            await asyncio.to_thread(s3_service.delete_by_url, u)
        if asset and (asset.options or {}).get("render", {}).get("main", True):
            popts = dict(asset.options or {})
            old_prev = popts.get("preview_frame_url")
            info = None
            if poster:
                try:
                    purl = await asyncio.to_thread(s3_service.upload_bytes, poster, f"{_S3_PREFIX}/posters", ".jpg", "image/jpeg")
                    info = (purl, len(poster))
                except Exception:  # noqa: BLE001 - the cover is a nice-to-have
                    info = None
            if not info:
                info = await asyncio.to_thread(_store_preview_frame, video, {"duration_seconds": 6.4})
            if info:
                def keep_prev(a, o):
                    o["preview_frame_url"] = info[0]
                await _patch(asset_id, keep_prev)
                await asyncio.to_thread(s3_service.delete_by_url, old_prev)
        # Only now (the video is safely stored) let the worker drop its copy - a restart before this
        # point resumes and downloads it again instead of losing the finished render.
        if lam:
            await vl.cleanup(job_id)
        else:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(f"{_worker_url()}/jobs/{job_id}", headers=_worker_headers())
            except httpx.HTTPError:
                pass  # the worker clears old files by itself after 3 hours
        if newly_done["v"] and asset:
            async with AsyncSessionLocal() as db:
                fresh = await db.get(StudioMediaAsset, asset_id)
                from app.services.studio_media_service import _notify_video_done
                # Owner 11/12: no separate "ready" email - the bell now, and the agent's daily
                # briefing lists the videos finished since the last one (showcase_digest_service).
                await _notify_video_done(db, fresh, email_ready=False)
        if _TASKS.get(asset_id) is asyncio.current_task():
            _TASKS.pop(asset_id, None)
        await _next_remake(asset_id, owner_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("motion render failed", extra={"asset_id": asset_id})
        detail = str(exc)
        msg = detail if isinstance(exc, RuntimeError) and not detail.startswith("worker:") else "The video couldn't be finished. Press Make video to try again."
        if detail.startswith("worker:"):
            from app.services.admin_alerts import alert_admin
            try:
                await alert_admin("ai_video", "Cinematic Video render failed", detail[:600], owner_id=owner_id)
            except Exception:  # noqa: BLE001
                pass

        def fail(asset, o):
            o["render"] = {**(o.get("render") or {}), "state": "failed", "error": msg, "finished_at": _now()}
            if asset.status == "processing":
                asset.status = "failed" if not asset.result_url else "succeeded"
                asset.error_message = msg if not asset.result_url else None
        await _patch(asset_id, fail)
        if reservation is not None:
            try:
                async with AsyncSessionLocal() as db:
                    await reconcile_budget(db, reservation, spent)
            except Exception:  # noqa: BLE001
                pass
    finally:
        # only clear our own entry - _next_remake may already have started the next shape's task
        if _TASKS.get(asset_id) is asyncio.current_task():
            _TASKS.pop(asset_id, None)


async def _next_remake(asset_id: str, owner_id: str) -> None:
    """After an updated main video: re-make the next out-of-date extra shape (render only - the
    photos are already prepared, so sharpening is never re-run here)."""
    if vl.enabled():
        # each remake is a paid AWS render now - not once the agent's AI for the month is used up
        # (the out-of-date shapes stay listed; Make it re-makes them when there's AI again)
        try:
            async with AsyncSessionLocal() as db:
                u = await db.get(User, owner_id)
                if not u or await studio_motion_would_refuse(db, owner_id, await ai_config_service.get_config(db), u.plan_tier):
                    return
        except Exception:  # noqa: BLE001
            return
    nxt = {"t": None}

    def take(a, o):
        q = [k for k in (o.get("remake_formats") or []) if k in FORMATS]
        motion = o.get("motion") or {}
        if not q or (o.get("render") or {}).get("state") in _ACTIVE:
            o["remake_formats"] = q
            return
        nxt["t"] = q.pop(0)
        o["remake_formats"] = q
        o["render"] = {"state": "queued", "progress": 0.0, "stage": "Waiting for the video maker", "started_at": _now(),
                       "target": nxt["t"], "main": False, "sharpen_count": 0, "hash": settings_hash(motion), "skip_sharp": True}
    try:
        await _patch(asset_id, take)
    except Exception:  # noqa: BLE001
        logger.warning("motion remake queue failed", exc_info=True, extra={"asset_id": asset_id})
        return
    if nxt["t"]:
        _TASKS[asset_id] = asyncio.create_task(_run_render(asset_id, owner_id, None, resume_job=None))


async def _first_to_finish(job_id: str) -> bool:
    """True for exactly one caller per AWS render (an atomic database counter), so a finished video is
    saved and charged once even when two server processes follow the same render."""
    from app.services.usage_budget_service import _period_keys, reserve_bucket
    try:
        async with AsyncSessionLocal() as db:
            first = await reserve_bucket(db, f"motion_finish:{job_id.rsplit(':', 1)[-1]}", "month", _period_keys()[1], 1, 0, 1, None)
            await db.commit()
            return bool(first)
    except Exception:  # noqa: BLE001 - never block a finished video on this check
        logger.warning("motion finish claim failed", exc_info=True)
        return True


_ACTIVE = ("preparing", "queued", "rendering", "finishing")

# ---- the line for AWS Lambda videos (owner 11/13). The account runs only so many Lambdas at once
# (vl.max_videos() videos), so extra videos wait their turn here: first come, first served (render
# "line_at"), each one's page shows how many are ahead and a countdown that moves as the line does.
# Kept in the database (each video's render state), so a restart keeps everyone's place, and it all
# runs on the server - the agent can close the page or log out.
_LINE_LOCK = asyncio.Lock()


async def _lambda_line() -> tuple[list[dict], list[dict]]:
    """(being made on Lambda now, waiting in line - oldest first)."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(StudioMediaAsset.id, StudioMediaAsset.options).where(
            StudioMediaAsset.action == "motion",
            StudioMediaAsset.options["render"]["state"].astext.in_(_ACTIVE),
        ))).all()
    making, waiting = [], []
    for aid, opts in rows:
        r = (opts or {}).get("render") or {}
        job = r.get("job_id") or ""
        if vl.is_lambda_job(job):
            if r.get("state") in ("queued", "rendering"):  # "finishing" = saving, AWS is already free
                making.append({"id": aid, "eta_at": r.get("eta_at"), "frames": r.get("frames")})
        elif r.get("line_at") and not job:
            waiting.append({"id": aid, "line_at": r["line_at"], "frames": r.get("frames")})
    waiting.sort(key=lambda w: (w["line_at"], w["id"]))
    return making, waiting


async def _line_wait_seconds(making: list[dict], ahead_waiting: int) -> float:
    """Seconds until a slot is free for a video with `ahead_waiting` videos in line before it."""
    slots = vl.max_videos()
    now = datetime.now(timezone.utc)
    left = []
    for m in making:
        try:
            left.append(max(0.0, (datetime.fromisoformat(m["eta_at"]) - now).total_seconds()) if m.get("eta_at") else vl.video_seconds(m.get("frames") or 900))
        except ValueError:
            left.append(vl.video_seconds(900))
    left.sort()
    free_now = max(0, slots - len(left))
    queue = [0.0] * free_now + left  # when each slot frees up
    for _ in range(ahead_waiting):  # each video ahead takes the next free slot
        queue.sort()
        t = queue.pop(0)
        queue.append(t + vl.video_seconds(900))
    queue.sort()
    return queue[0] if queue else 0.0


async def _lambda_take_turn(asset_id: str, props: dict) -> str:
    """Wait for a free Lambda slot (first come, first served), then start this video on AWS."""
    frames = vl.frames_estimate(props)

    def join(a, o):
        r = dict(o.get("render") or {})
        r.setdefault("line_at", _now())
        r["frames"] = frames
        o["render"] = r
    a0 = await _patch(asset_id, join)
    run_key = ((a0.options or {}).get("render") or {}).get("started_at") or "" if a0 else ""
    last = None

    async def started_elsewhere() -> Optional[str]:
        # another server process (deploy overlap, a leftover process) already started this video
        async with AsyncSessionLocal() as db:
            a = await db.get(StudioMediaAsset, asset_id)
            job = (((a.options or {}).get("render") or {}).get("job_id") if a else None) or ""
        return job if vl.is_lambda_job(job) else None

    while True:
        other = await started_elsewhere()
        if other:
            return other
        async with _LINE_LOCK:
            making, waiting = await _lambda_line()
            ids = [w["id"] for w in waiting]
            pos = ids.index(asset_id) if asset_id in ids else 0
            if len(making) + pos < vl.max_videos():
                # exactly one process may start this run of this video (atomic database claim)
                from app.services.usage_budget_service import _period_keys, reserve_bucket
                async with AsyncSessionLocal() as db:
                    mine = await reserve_bucket(db, f"motion_start:{asset_id}:{run_key}", "month", _period_keys()[1], 1, 0, 1, None)
                    await db.commit()
                if not mine:
                    await asyncio.sleep(3.0)
                    continue
                job_id = await vl.start(props)
                await _patch(asset_id, lambda a, o: o.__setitem__("render", {**(o.get("render") or {}), "job_id": job_id, "state": "queued",
                                                                              "stage": "Starting the cameras", "ahead": 0}))
                return job_id
            wait = await _line_wait_seconds(making, pos)
        ahead = len(making) + pos
        eta = wait + vl.video_seconds(frames) + _SAVE_SECS["v"]
        eta_at = (datetime.now(timezone.utc) + timedelta(seconds=eta)).isoformat()
        if last and last["ahead"] == ahead and abs((datetime.fromisoformat(last["eta_at"]) - datetime.fromisoformat(eta_at)).total_seconds()) < max(8, 0.05 * eta):
            eta_at = last["eta_at"]  # keep the countdown steady unless the line really moved
        cur = {"state": "queued", "ahead": ahead, "eta_at": eta_at, "wait_seconds": round(wait),
               "stage": f"In line - {ahead} {'video' if ahead == 1 else 'videos'} ahead of yours", "eta_learned": True}
        if cur != last:
            last = cur

            def wait_patch(a, o):
                r = o.get("render") or {}
                if vl.is_lambda_job(r.get("job_id")):
                    return  # already on AWS (started by another process) - never put it back in line
                o["render"] = {**r, **cur}
            await _patch(asset_id, wait_patch)
        await asyncio.sleep(4.0)
# Seconds to copy a finished video to storage - added to the countdown; learned from each save.
_SAVE_SECS = {"v": 20.0}
_RESUMING: set[str] = set()


async def _resume(asset: StudioMediaAsset) -> Optional[StudioMediaAsset]:
    """Picks up a video whose run was cut off by a server restart (deploy). Called by the page's
    poll AND by the every-2-minutes sweep (resume_stalled), so it finishes even if the agent
    logged out. Returns the asset only when it had to be marked failed."""
    if asset.id in _RESUMING or (asset.id in _TASKS and not _TASKS[asset.id].done()):
        return None
    _RESUMING.add(asset.id)
    try:
        render = (asset.options or {}).get("render") or {}

        async def fail(msg: str) -> Optional[StudioMediaAsset]:
            def f(a, o):
                o["render"] = {**(o.get("render") or {}), "state": "failed", "error": msg, "finished_at": _now()}
                if a.status == "processing":
                    a.status = "failed" if not a.result_url else "succeeded"
            return await _patch(asset.id, f)

        try:
            started = datetime.fromisoformat(render["started_at"]).timestamp() if render.get("started_at") else 0
        except ValueError:
            started = 0
        if started and time.time() - started > 6 * 3600:
            return await fail("That run was interrupted. Press Make video again.")
        if render.get("job_id") and render.get("state") != "preparing":
            # the video maker has it (or lost it - _run_render hands it over again)
            _TASKS[asset.id] = asyncio.create_task(_run_render(asset.id, asset.owner_id, None, resume_job=render["job_id"]))
            return None
        # Cut off before the video maker got it (photos being prepared or sharpened): do that part again.
        # Prepared photos are kept, so only what was unfinished runs (and is paid for) again.
        motion = (asset.options or {}).get("motion") or {}
        need_sharp = [] if render.get("skip_sharp") else [p for p in motion.get("photos") or [] if not p.get("hidden") and not p.get("sharp")]
        reservation = None
        if need_sharp:
            try:
                async with AsyncSessionLocal() as db:
                    user = await db.get(User, asset.owner_id)
                    config = await ai_config_service.get_config(db)
                    reservation = await reserve_studio_motion(db, user.id, config, user.plan_tier)
            except AIRateLimitExceeded:
                return await fail("That run was interrupted. Press Make video again.")
        _TASKS[asset.id] = asyncio.create_task(_run_render(asset.id, asset.owner_id, reservation, resume_job=None))
        return None
    finally:
        _RESUMING.discard(asset.id)


async def resume_stalled() -> int:
    """Scheduler job (main.py, every 2 minutes): videos still being made with no task running on
    this server - after a restart - pick back up without anyone opening the page."""
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(StudioMediaAsset).where(
                StudioMediaAsset.action == "motion",
                StudioMediaAsset.options["render"]["state"].astext.in_(_ACTIVE),
            ))).scalars().all()
    except Exception:  # noqa: BLE001
        logger.warning("motion resume sweep failed", exc_info=True)
        return 0
    n = 0
    for a in rows:
        if a.id in _TASKS and not _TASKS[a.id].done():
            continue
        try:
            await _resume(a)
            n += 1
        except Exception:  # noqa: BLE001
            logger.warning("motion resume failed", exc_info=True, extra={"asset_id": a.id})
    if n:
        logger.info("Cinematic videos picked back up", extra={"count": n})
    return n


async def check(db, asset: StudioMediaAsset) -> StudioMediaAsset:
    """Called when the page polls: picks a render or analysis back up if the server restarted
    in the middle (the task lives in this process)."""
    if asset.action != "motion":
        return asset
    opts = asset.options or {}
    render = opts.get("render") or {}
    running = asset.id in _TASKS and not _TASKS[asset.id].done()
    if render.get("state") in _ACTIVE and not running:
        asset = await _resume(asset) or asset
    if render.get("state") not in ("preparing", "queued", "rendering", "finishing"):
        kick_prep(asset)  # picks depth maps back up after a restart
    analysis = opts.get("analysis") or {}
    if analysis.get("status") == "running" and not running:
        def stop(a, o):
            o["analysis"] = {"status": "failed", "error": "That was interrupted. Press Try again."}
        asset = await _patch(asset.id, stop) or asset
    return asset


def files_of(asset: StudioMediaAsset) -> list[str]:
    """Every S3 file a Cinematic Video project owns (for delete)."""
    opts = asset.options or {}
    out = []
    motion = opts.get("motion") or {}
    for p in list(motion.get("photos") or []) + list(motion.get("pool") or []):
        out += [p.get("clean"), p.get("depth"), p.get("sharp")]
    for v in (opts.get("formats") or {}).values():
        out.append(v.get("url") if isinstance(v, dict) else v)
    return list(dict.fromkeys(u for u in out if u))
