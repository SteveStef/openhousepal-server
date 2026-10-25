"""Studio AI Property Media - orchestration for the /studio/media-lab routes.

Two shapes of work:

* Photo edit (synchronous, OpenAI gpt-image-2). The agent can STACK several effects
  (twilight / lights_on / virtual_staging / enhance) - they're composed into one prompt and
  run as a SINGLE edit, so three stacked effects cost the same as one.
* AI Video (asynchronous, provider-configured image-to-video). The agent picks up to 8 photos;
  each becomes a 5-second clip, and poll_asset() concatenates them (bundled ffmpeg, `-c copy`)
  into one ~40s marketing video once every clip is done.

Cost guardrails mirror cma_ai_service.py: reserve -> hard per-generation cap -> call ->
reconcile the real cost -> record one usage event (so per-user monthly ceilings key off it) ->
persist the asset row. Source photos are only ever fetched by URL, never modified.
"""
import asyncio
import os
import random
import re
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from typing import Any, Optional

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Notification, StudioBrandKit, StudioMediaAsset, User
from app.schemas.studio_media import StudioMediaGenerateRequest, StudioMediaOptions, StudioVideoVariantOptions
from app.services import s3_service
from app.services import storage_ledger_service
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import (
    _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS, AIRateLimitExceeded, classify_provider_error, reconcile_budget,
    record_usage_event, reserve_studio_media_image, reserve_studio_media_video, total_monthly_ai_cost,
)
from app.services.studio_media.prompts import (
    PROMPT_VERSION, REFINE_PROMPT, build_stacked_prompt, build_video_motion_prompt,
    build_video_timelapse_prompt, split_masked_removal, KEEP_EVERYTHING_CLAUSE, KEEP_EXCEPT_DEBRIS_CLAUSE,
)
from app.services.studio_media.providers.base import ProviderError, ImageEditRequest, VideoRequest
from app.services.studio_media.providers.registry import image_provider, video_provider
from app.utils.auth import tier_has_mls

logger = get_logger(__name__)

_IMAGE_EFFECTS = ("twilight", "lights_on", "virtual_staging", "enhance")
# push_in only (2026-09-04, explicit decision): a slow forward glide reads as a gimbal walkthrough
# and is the one move that never exposes the photo's edges (so it never tempts the model to
# invent buildings/scenery beyond frame). pan_left/pan_right dropped from the rotation entirely -
# even after tightening their wording (see prompts.py), they're the two moves that ever risk a
# dramatic/unintended camera move; push_in alone gives one consistent, reliably-cinematic feel
# across every clip instead of a mix. push_out and drone_glide were already retired before this.
_VIDEO_MOTIONS = ("push_in",)
_VIDEO_VARIANT_MOTIONS = ("push_in", "slow_push_in", "pan_left", "pan_right")
_FAL_HAILUO_MODEL_BY_TIER = {
    "fast_pro": "fal-ai/minimax/hailuo-2.3-fast/pro/image-to-video",
    "pro": "fal-ai/minimax/hailuo-2.3/pro/image-to-video",
}
_FAL_HAILUO_PRICE_BY_TIER = {
    "fast_pro": 0.33,
    "pro": 0.49,
}
_SOURCE_MAX_BYTES = 12 * 1024 * 1024
_S3_PREFIX = "studio-media-lab"
_DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
_RUNWAY_CREDIT_USD = 0.01
_PER_CLIP_EST_USD = 0.30  # conservative: gen4_turbo 5s ~= 25 credits = $0.25
_RUNWAY_GEN45_PER_SECOND_EST_USD = 0.12
# MiniMax H3 has no submit-time cost estimate at all (see minimax_video.py) - this is the
# per-second reservation estimate used before a clip is even submitted, a touch above the real
# published rate ($0.13/s at 2K) the same way _PER_CLIP_EST_USD sits a touch above Runway's real
# $0.05/s, so a generation is never under-reserved.
_MINIMAX_PER_SECOND_EST_USD = 0.15


def _clip_cost_estimate_usd(config: dict, duration_seconds: float) -> float:
    """Conservative per-clip cost estimate for whichever render provider is configured, used
    for the pre-flight cost gate and the monthly-budget check BEFORE any clip is submitted -
    see _run_video. Real cost (from the provider's own submit/poll response) always wins once
    known; this is only the upper-bound guess used to decide whether to even start."""
    provider = config.get("studio_media_video_provider")
    if provider == "minimax":
        return round(duration_seconds * _MINIMAX_PER_SECOND_EST_USD, 4)
    if provider == "fal":
        # fal.ai's Hailuo is billed as a flat fee per clip (see fal_video.py's submit()), not
        # per-second - this IS the real published price, read from config (not a hardcoded
        # constant here) so it stays correct across a price change or a model swap with no
        # code deploy.
        return float(config.get("studio_media_video_hailuo_price_per_clip_usd", 0.49))
    if str(config.get("studio_media_video_model") or "").startswith("gen4.5"):
        # gen4.5 bills 12 credits/sec ($1.20 for a 10s clip, confirmed on a real job 2026-09-30) -
        # the gen4_turbo rate below quoted it at half that.
        return round(duration_seconds * _RUNWAY_GEN45_PER_SECOND_EST_USD, 4)
    return round((duration_seconds / 5.0) * _PER_CLIP_EST_USD, 4)


# ByteDance's fal.ai upscaler (the default pipeline's upscale step - see fal_video.py) is cheap
# enough (standard-tier: $0.0072/sec at 1080p, $0.0144/sec at 2K, $0.0288/sec at 4K) that a
# normal ~30s reel's upscale is reliably well under $1 - unlike Runway/Magnific's old $9-13-on-a-
# full-reel pricing, there's no need for a per-call "is this affordable" gate anymore. These are
# only ESTIMATES for the pre-flight budget check (see poll_asset/upscale_video) - the real cost,
# known once the upscale job completes, always wins (and is billed at whichever resolution rate
# actually applied - see fal_video.py's poll()).
_BYTEDANCE_UPSCALE_EST_PER_SECOND_USD_BY_RESOLUTION = {
    "1080p": 0.0072,
    "2k": 0.0144,
    "4k": 0.0288,
}


def _upscale_cost_estimate_per_second_usd(config: dict) -> float:
    """Per-second upscale estimate for whichever render provider/upscale vendor is configured -
    used both by the pre-flight budget check and exposed via GET /studio/media-lab/usage so the
    frontend can show a live 'this will use ~N% of your allowance' estimate as the agent picks
    photos, computed from real config values rather than a hardcoded percentage."""
    if config.get("studio_media_video_provider") == "minimax":
        return 0.0  # MiniMax-H3 already renders natively at 2K - no separate upscale step
    if config.get("studio_media_video_upscale_vendor") == "topaz":
        return float(config.get("studio_media_video_upscale_topaz_price_per_second_usd", 0.06))
    resolution = str(config.get("studio_media_video_upscale_resolution", "2k")).lower()
    price_table = config.get(
        "studio_media_video_upscale_price_per_second_usd_by_resolution",
        _BYTEDANCE_UPSCALE_EST_PER_SECOND_USD_BY_RESOLUTION,
    )
    return float(price_table.get(resolution, price_table.get("2k", _BYTEDANCE_UPSCALE_EST_PER_SECOND_USD_BY_RESOLUTION["2k"])))


def _variant_dict(variant: StudioVideoVariantOptions | dict | None) -> dict:
    if not variant:
        return {}
    if isinstance(variant, dict):
        return {k: v for k, v in variant.items() if v is not None}
    return variant.model_dump(exclude_none=True)


def _config_for_video_variant(config: dict, variant: StudioVideoVariantOptions | dict | None) -> dict:
    """Return a per-job config copy. No variant means the current production defaults are
    untouched; variant model_tier is deliberately limited to fal/Hailuo tiers so ByteDance stays
    constant for the foliage test."""
    data = _variant_dict(variant)
    tier = data.get("model_tier")
    if not tier:
        return config
    if tier not in _FAL_HAILUO_MODEL_BY_TIER:
        return config
    cfg = dict(config)
    cfg["studio_media_video_provider"] = "fal"
    cfg["studio_media_video_model"] = _FAL_HAILUO_MODEL_BY_TIER[tier]
    cfg["studio_media_video_hailuo_price_per_clip_usd"] = _FAL_HAILUO_PRICE_BY_TIER[tier]
    return cfg


def estimate_video_generation_cost_usd(
    config: dict,
    *,
    photo_count: int = 1,
    variant: StudioVideoVariantOptions | dict | None = None,
) -> dict:
    """Provider-aware cost projection for a video job before it is submitted. Used by the
    experiment grid and also stored on variant assets so test output has cost context next to it."""
    cfg = _config_for_video_variant(config, variant)
    count = max(1, int(photo_count or 1))
    per_clip_secs = int(cfg.get("studio_media_video_seconds_per_clip", 5))
    enhance_cost = (
        float(cfg.get("studio_media_video_photo_enhance_cost_usd", 0.25) or 0.0)
        if cfg.get("studio_media_video_photo_enhance_enabled", True) else 0.0
    )
    clip_cost = _clip_cost_estimate_usd(cfg, per_clip_secs)
    upscale_cost = per_clip_secs * _upscale_cost_estimate_per_second_usd(cfg)
    total = round(count * (enhance_cost + clip_cost + upscale_cost), 4)
    return {
        "total_usd": total,
        "per_clip_usd": round(total / count, 4),
        "photo_enhance_usd": round(enhance_cost, 4),
        "render_usd": round(clip_cost, 4),
        "upscale_usd": round(upscale_cost, 4),
        "seconds_per_clip": per_clip_secs,
    }


def _rate_limit_http(exc: AIRateLimitExceeded) -> HTTPException:
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.user_message)


def _image_cost(result, config: dict, fallback: float) -> float:
    if result.provider_cost_usd is not None:
        return round(result.provider_cost_usd, 4)
    u = result.usage or {}
    if not u:
        return fallback
    cost = (
        u.get("text_input", 0) * config["studio_media_image_price_text_input_per_mtok"]
        + u.get("image_input", 0) * config["studio_media_image_price_image_input_per_mtok"]
        + u.get("image_output", 0) * config["studio_media_image_price_image_output_per_mtok"]
    ) / 1_000_000
    return round(cost, 4) if cost > 0 else fallback


_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _exc_text(exc: BaseException) -> str:
    """httpx timeout/connect/transport errors frequently stringify to an EMPTY string (str(exc)
    == "" - a known httpx quirk), which made several logs in this file unreadable at exactly the
    moment they mattered most (e.g. "studio media video assemble failed", error: ""). Falls back
    to repr() (always includes the exception's class name) when str() has nothing to say."""
    return str(exc) or repr(exc)


def _studio_media_block_reason(exc: ProviderError) -> str:
    """Maps a Studio Media ProviderError (Runway/fal/MiniMax/OpenAI image) onto the same
    block_reason vocabulary the chat/tts/CMA pipeline uses (see classify_provider_error), so an
    out-of-credit or rate-limited Studio Media provider shows up in the admin AI dashboard's
    provider-failure alert instead of only ever recording the generic "studio_media_provider_error"
    string, which that alert doesn't recognize. Checks the provider's own structured flags first
    (more reliable than text-matching), then falls back to classify_provider_error's text match
    for failure modes Studio Media's providers don't flag explicitly (overloaded/timeout/auth)."""
    if exc.not_configured:
        return "provider_not_configured"
    if getattr(exc, "out_of_credits", False):
        return "insufficient_credit"
    if getattr(exc, "rate_limited", False):
        return "provider_rate_limited"
    return classify_provider_error(exc)


async def _fetch_source_bytes(url: str) -> tuple[bytes, str]:
    try:
        from app.utils.safe_fetch import safe_get
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            resp = await safe_get(client, url, headers=_FETCH_HEADERS)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Couldn't load that source photo. Try another one.") from exc
    data = resp.content
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That source photo was empty.")
    if len(data) > _SOURCE_MAX_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That source photo is too large (max 12MB).")
    content_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return data, content_type


def _match_source_dimensions(result_bytes: bytes, source_bytes: bytes) -> bytes:
    """gpt-image-2 only ever returns its own fixed sizes (~1024/1536), so an edit of a
    1600x1067 MLS photo can come back a different resolution. Resize the result back to the
    SOURCE photo's exact pixel dimensions (LANCZOS) so every stored edit matches the Bright MLS
    original 1:1. Best-effort - a failure just stores the model's own output unchanged."""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(source_bytes)) as src:
            target = src.size
        out = Image.open(BytesIO(result_bytes)).convert("RGB")
        if out.size == target:
            return result_bytes
        out = out.resize(target, Image.LANCZOS)
        buf = BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media dimension-match skipped", extra={"error": _exc_text(exc)})
        return result_bytes


_S3_PUBLIC_PREFIX = f"https://{s3_service.PUBLIC_URL_HOST}/"


def _crop_watermark_corner(data: bytes, pct: float) -> bytes:
    """Crop `pct` off the left and bottom of a still image (origin kept top-right - same geometry
    the old post-generation video crop used) to remove a Bright MLS lower-left watermark from the
    SOURCE photo before it's ever sent to Hailuo. Re-encoded losslessly (PNG) rather than the old
    JPEG q=92 - this photo is the actual source Hailuo animates, so every generation used to pay a
    JPEG-recompression quality tax before the AI model even saw it. PNG is bigger, but this only
    ever feeds an ephemeral image-to-video call (never the agent's stored photo), so file size
    doesn't matter here the way it would for something served to a browser."""
    from io import BytesIO
    from PIL import Image
    p = max(0.0, min(0.2, float(pct)))
    with Image.open(BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        cropped = im.crop((round(w * p), 0, w, round(h * (1 - p))))
        buf = BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()


_VIDEO_SOURCE_ASPECT = 16 / 9


def _crop_to_video_aspect(data: bytes) -> bytes:
    """Center-crop (NEVER stretch) a source still to the pipeline's own 16:9 aspect, before it's
    ever sent to Hailuo. Added 2026-09-06: Bright MLS photos (and uploads) are routinely 4:3 or
    3:2, not 16:9 - Hailuo doesn't crop a mismatched input itself, it stretches it to fill its own
    16:9 output frame, visibly distorting the whole clip (people/architecture reading too wide or
    too tall). Cropping to match exactly first removes the mismatch entirely, so Hailuo never has
    anything to stretch. Returns the input bytes UNCHANGED (same object) when the aspect already
    matches, so a caller can cheaply check `result is data` to skip a needless re-upload."""
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        cur_ratio = w / h
        if abs(cur_ratio - _VIDEO_SOURCE_ASPECT) < 0.01:
            return data
        if cur_ratio > _VIDEO_SOURCE_ASPECT:
            new_w = round(h * _VIDEO_SOURCE_ASPECT)
            x0 = (w - new_w) // 2
            cropped = im.crop((x0, 0, x0 + new_w, h))
        else:
            new_h = round(w / _VIDEO_SOURCE_ASPECT)
            y0 = (h - new_h) // 2
            cropped = im.crop((0, y0, w, y0 + new_h))
        buf = BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()


async def _runway_fetchable_url(source_url: str, config: Optional[dict] = None) -> str:
    """Runway's promptImage must be an `https://` URL it can fetch. Bright MLS photos are
    plain `http://` (and may block hotlinking), so anything not already one of our own public S3
    objects is mirrored to S3 first - and, in the same pass, has the Bright MLS corner watermark
    cropped off (see studio_media_video_debrand_crop_pct) so Hailuo never sees or animates it,
    rather than burning it into the generated video and cropping+rescaling the video afterward.
    Also center-crops to 16:9 (see _crop_to_video_aspect) so an off-aspect source never gets
    stretched by Hailuo - this applies to BOTH an MLS photo and an agent's own upload (an upload
    can be just as off-aspect as an MLS photo), so an already-S3-hosted upload can no longer just
    skip processing outright the way it used to when the watermark crop was the only reason to
    touch it; it now only skips the final re-upload when nothing actually changed."""
    is_own_upload = source_url.startswith(_S3_PUBLIC_PREFIX)
    data, content_type = await _fetch_source_bytes(source_url)
    changed = False
    crop_pct = float((config or {}).get("studio_media_video_debrand_crop_pct", 0.0) or 0.0)
    if not is_own_upload and crop_pct > 0:
        try:
            data = _crop_watermark_corner(data, crop_pct)
            content_type = "image/png"
            changed = True
        except Exception as exc:  # noqa: BLE001 - best-effort; an un-cropped source is fine too
            logger.warning("studio media watermark crop skipped", extra={"error": _exc_text(exc)})
    try:
        aspect_fixed = _crop_to_video_aspect(data)
        if aspect_fixed is not data:
            data = aspect_fixed
            content_type = "image/png"
            changed = True
    except Exception as exc:  # noqa: BLE001 - best-effort; an unfixed aspect is fine too
        logger.warning("studio media aspect crop skipped", extra={"error": _exc_text(exc)})
    if is_own_upload and not changed:
        return source_url  # untouched upload, already fine - skip the redundant re-upload
    ext = ".png" if content_type == "image/png" else ".jpg"
    try:
        return s3_service.upload_bytes(data, _S3_PREFIX, ext, content_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video source mirror failed", extra={"error": _exc_text(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't prepare that photo for video.") from exc


# ------------------------------------------------------------------------------- entrypoint

# Storage accounting is now the app-wide ledger (storage_ledger_service). These thin wrappers
# keep the old names working for the routes that import them.
async def storage_used_bytes(db: AsyncSession, owner_id: str) -> int:
    return await storage_ledger_service.total_bytes(db, owner_id)


def _storage_cap_bytes(config: dict, plan_tier: str) -> int:
    return storage_ledger_service.cap_bytes(config, plan_tier)


async def _enforce_storage_cap(db: AsyncSession, user: User, config: dict, incoming_bytes: int = 0) -> None:
    await storage_ledger_service.enforce_cap(db, user.id, config, user.plan_tier, incoming_bytes)


async def create_asset(
    db: AsyncSession, user: User, payload: StudioMediaGenerateRequest,
    *, group_id: Optional[str] = None, parent_asset_id: Optional[str] = None,
) -> StudioMediaAsset:
    rid = (payload.client_request_id or "").strip() or None
    if rid:
        existing = await _asset_for_request(db, user.id, rid)
        if existing is None and (user.id, rid) in _INFLIGHT_REQUESTS:
            # The first copy of this same press is still submitting (a video's provider call
            # takes a few seconds before its row exists) - wait for it rather than run twice.
            for _ in range(60):
                await asyncio.sleep(0.5)
                existing = await _asset_for_request(db, user.id, rid)
                if existing is not None or (user.id, rid) not in _INFLIGHT_REQUESTS:
                    break
        if existing is not None:
            return existing
        _INFLIGHT_REQUESTS.add((user.id, rid))
    try:
        return await _create_asset_body(db, user, payload, rid, group_id=group_id, parent_asset_id=parent_asset_id)
    finally:
        if rid:
            _INFLIGHT_REQUESTS.discard((user.id, rid))


# (owner id, client_request_id) pairs whose Generate is mid-submit in this worker.
_INFLIGHT_REQUESTS: set[tuple[str, str]] = set()
# Photo edits running in the background (see _run_image_job) - strong refs so the event loop
# never garbage-collects one mid-run.
_IMAGE_JOBS: set[asyncio.Task] = set()
# A photo edit normally takes 40-90s (a circled removal + its quality re-try + the main pass at
# most ~4 model calls). A row still "processing" long after that lost its worker (a restart or
# deploy mid-run) and is shown as failed instead of spinning forever.
_IMAGE_JOB_STALE_SECONDS = 12 * 60


async def _asset_for_request(db: AsyncSession, owner_id: str, rid: str) -> Optional[StudioMediaAsset]:
    res = await db.execute(select(StudioMediaAsset).where(
        StudioMediaAsset.owner_id == owner_id,
        StudioMediaAsset.options["client_request_id"].astext == rid,
    ).limit(1))
    return res.scalar_one_or_none()


async def expire_stale_image_job(db: AsyncSession, asset: StudioMediaAsset) -> StudioMediaAsset:
    """A photo edit whose background run died (server restart mid-run) - mark it failed so the
    agent sees "try again" instead of an endless spinner. No-op for anything else."""
    if asset.kind != "image" or asset.status != "processing" or not asset.created_at:
        return asset
    from datetime import datetime, timezone
    created = asset.created_at if asset.created_at.tzinfo else asset.created_at.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - created).total_seconds() < _IMAGE_JOB_STALE_SECONDS:
        return asset
    asset.status = "failed"
    asset.error_message = "This edit didn't finish - please try again."
    await db.commit()
    await db.refresh(asset)
    return asset


async def _create_asset_body(
    db: AsyncSession, user: User, payload: StudioMediaGenerateRequest, rid: Optional[str],
    *, group_id: Optional[str] = None, parent_asset_id: Optional[str] = None,
) -> StudioMediaAsset:
    options = payload.options or StudioMediaOptions()
    has_mls = tier_has_mls(user.plan_tier)
    linked_property = payload.linked_property if has_mls else None
    config = await ai_config_service.get_config(db)
    await _enforce_storage_cap(db, user, config)
    group_id = group_id or str(uuid.uuid4())

    if payload.action == "animate":
        photos = [p for p in (payload.photos or []) if isinstance(p, str) and p]
        if not photos and payload.source_photo_url:
            photos = [payload.source_photo_url]
        if not (1 <= len(photos) <= int(config.get("studio_media_video_max_photos", 8))):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Pick between 1 and {config.get('studio_media_video_max_photos', 8)} photos.")
        # One continuous still-camera clip from one photo, not several photos stitched together -
        # several photos' clips would jump between different scenes mid-sunset.
        if options.video_style == "day_to_twilight" and len(photos) != 1:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Day → Twilight timelapse works from exactly one photo.")
        if not has_mls and any("brightmls" in p or not p.startswith("https://") for p in photos):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Your plan can only use uploaded photos here.")
        return await _run_video(db, user, payload, photos, options, config, group_id, parent_asset_id, linked_property)

    # ---- photo edit (stacked or single) ----
    effects = [e for e in (payload.effects or []) if e in _IMAGE_EFFECTS]
    if not effects and payload.action in _IMAGE_EFFECTS:
        effects = [payload.action]
    if not effects:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pick at least one photo effect.")
    if "virtual_staging" in effects and (not options.room_type or not options.style):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Virtual staging needs a room type and a style.")
    if payload.source_kind == "listing" and not has_mls:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Your plan can only use uploaded photos here, not MLS listing photos.")

    prompt = build_stacked_prompt(effects, options.model_dump(exclude_none=True))
    action_label = "+".join(effects)

    # The edit itself runs in the background (user, 10/24: leaving the page mid-generate on a
    # phone showed "Network error" and the agent pressed Generate again - two copies). The
    # project row exists from the first second as "processing", the page and the Media Lab list
    # poll it, and the run carries on whatever the browser does.
    placeholder = StudioMediaAsset(
        owner_id=user.id, group_id=group_id, parent_asset_id=parent_asset_id,
        kind="image", action=action_label, status="processing",
        source_photo_url=payload.source_photo_url, source_kind=payload.source_kind,
        linked_property=linked_property.model_dump() if linked_property else None,
        options={**options.model_dump(exclude_none=True), "effects": effects,
                 **({"client_request_id": rid} if rid else {})},
        prompt=prompt, prompt_version=PROMPT_VERSION,
    )
    db.add(placeholder)
    await db.commit()
    await db.refresh(placeholder)
    task = asyncio.create_task(_run_image_job(
        placeholder.id, user.id, payload, effects, action_label, options, prompt,
        group_id, parent_asset_id, linked_property,
    ))
    _IMAGE_JOBS.add(task)
    task.add_done_callback(_IMAGE_JOBS.discard)
    return placeholder


async def _run_image_job(asset_id, owner_id, payload, effects, action_label, options, prompt,
                         group_id, parent_asset_id, linked_property) -> None:
    """Background half of a photo edit: its own DB session (the request's is gone by now). Any
    error lands on the project row as "failed" + a readable message."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        error: Optional[str] = None
        try:
            user = await s.get(User, owner_id)
            if user is None:
                return
            config = await ai_config_service.get_config(s)
            await _run_image(s, user, payload, effects, action_label, options, config, prompt,
                             group_id, parent_asset_id, linked_property, placeholder_id=asset_id)
            return
        except HTTPException as exc:
            error = str(exc.detail)
        except Exception:  # noqa: BLE001
            logger.exception("studio media image job crashed", extra={"asset_id": asset_id})
            error = "Couldn't finish that edit. Try again in a moment."
        try:
            await s.rollback()
            asset = await s.get(StudioMediaAsset, asset_id)
            if asset is not None and asset.status == "processing":
                asset.status = "failed"
                asset.error_message = error
                await s.commit()
        except Exception:  # noqa: BLE001
            logger.warning("studio media image job: couldn't mark failed", extra={"asset_id": asset_id})


# ------------------------------------------------------------------- circled object removal

def _dilate(mask, px: int):
    """Grow a white-on-black "L" mask by ~px pixels. Done on a shrunk copy - a full-size
    MaxFilter of a 40px reach took ~5s on a 2500px photo (and blocked the server meanwhile)."""
    from PIL import Image, ImageFilter
    w, h = mask.size
    scale = max(1, px // 6)
    small = mask.resize((max(1, w // scale), max(1, h // scale)), Image.BILINEAR).point(lambda v: 255 if v > 0 else 0)
    k = max(3, (px // scale) * 2 + 1)
    small = small.filter(ImageFilter.MaxFilter(k if k % 2 else k + 1))
    return small.resize((w, h), Image.BILINEAR).point(lambda v: 255 if v >= 96 else 0)


def _removal_masks(source_bytes: bytes, areas: list) -> tuple[bytes, Any]:
    """From the agent's circled shapes (fractions of the photo): (the API mask - a PNG the size of
    the photo, transparent where the AI may edit; the soft-edged blend mask used to paste the
    original back everywhere else). Both are widened a little so a quick, loose circle still
    catches the whole object (a bumper edge, a chair leg)."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFilter
    with Image.open(BytesIO(source_bytes)) as src:
        w, h = src.size
    area = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(area)
    for shape in areas:
        draw.polygon([(x * w, y * h) for x, y in shape], fill=255)
        # The object's shadow (user, 10/24: a removed car and chair left their shadows behind -
        # the shadow lay just under the circle, outside the area the AI may touch, so the
        # paste-back put it right back). Every circle also covers a band under its lower edge,
        # where a car's or chair's cast / contact shadow falls on the floor or driveway.
        xs = [x * w for x, _ in shape]
        ys = [y * h for _, y in shape]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        bw, bh = x1 - x0, y1 - y0
        draw.ellipse((x0 - bw * 0.06, y1 - bh * 0.18, x1 + bw * 0.06, min(h, y1 + max(bh * 0.16, h * 0.02))), fill=255)
    area = _dilate(area, max(3, int(min(w, h) * 0.016)))
    api = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    api.putalpha(Image.eval(area, lambda v: 255 - v))
    buf = BytesIO()
    api.save(buf, format="PNG")
    blend = area.filter(ImageFilter.GaussianBlur(max(2, int(min(w, h) * 0.008))))
    return buf.getvalue(), blend


def _paste_back_original(result_bytes: bytes, source_bytes: bytes, blend) -> bytes:
    """The guarantee: only the circled areas come from the AI - every other pixel is the original
    photo, so nothing outside the circles (a garage door opener, trim, a light) can change."""
    from io import BytesIO
    from PIL import Image
    edited = Image.open(BytesIO(_match_source_dimensions(result_bytes, source_bytes))).convert("RGB")
    original = Image.open(BytesIO(source_bytes)).convert("RGB")
    out = Image.composite(edited, original, blend)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _shift(img, dx: float, dy: float):
    from PIL import Image
    return img.transform(img.size, Image.AFFINE, (1, 0, dx, 0, 1, dy), resample=Image.BILINEAR)


def _removal_qc(edit_bytes: bytes, source_bytes: bytes, blend) -> dict:
    """Free (no AI) check of a circled-removal result, run before it's kept (user, 10/24: a
    removal smeared the wall and cut off a pegboard at the circle's edge).
    - align: the model sometimes returns the whole photo nudged a few pixels, so what it painted
      inside the circle no longer lines up with the original pasted back around it (the "cut
      off" seam). Measured outside the circles, where the photo should be unchanged, and the best
      small shift that fixes it is found (`shift`, in full-size pixels).
    - removed: how much the inside of the circles changed. ~0 = the object is still there.
    - sharp: detail inside the circles vs the original just around them. Very low = a blurry
      smear instead of a real floor / wall / driveway.
    `bad` lists what failed; an empty list = keep it."""
    from io import BytesIO
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    original = Image.open(BytesIO(source_bytes)).convert("L")
    w, h = original.size
    edited = Image.open(BytesIO(_match_source_dimensions(edit_bytes, source_bytes))).convert("L")
    sw = 384
    sh = max(1, round(h * sw / w))
    o = original.resize((sw, sh), Image.BILINEAR)
    e = edited.resize((sw, sh), Image.BILINEAR)
    a = blend.resize((sw, sh), Image.BILINEAR).point(lambda v: 255 if v > 24 else 0)
    if not ImageStat.Stat(a).sum[0]:
        return {"bad": [], "shift": [0, 0]}
    outside = ImageChops.invert(a.filter(ImageFilter.MaxFilter(9)))
    inside = a.filter(ImageFilter.MinFilter(5))
    ring = ImageChops.subtract(a.filter(ImageFilter.MaxFilter(21)), a.filter(ImageFilter.MaxFilter(5)))

    def mean_diff(img, mask, ref=None) -> float:
        st = ImageStat.Stat(ImageChops.difference(img, o if ref is None else ref), mask)
        return st.mean[0] if st.count[0] else 0.0

    # Alignment is judged on softened copies - fine grain (and the model's own re-rendered
    # grain) would otherwise swamp a real few-pixel shift.
    ob, eb = o.filter(ImageFilter.GaussianBlur(1.5)), e.filter(ImageFilter.GaussianBlur(1.5))
    align = mean_diff(eb, outside, ob)
    best = (align, 0.0, 0.0)
    if align > 5:
        for step, reach, centre in ((1.0, 6, (0.0, 0.0)), (0.5, 1, None)):
            bx, by = centre if centre is not None else (best[1], best[2])
            n = int(round(reach / step))
            for iy in range(-n, n + 1):
                for ix in range(-n, n + 1):
                    dx, dy = bx + ix * step, by + iy * step
                    if (dx, dy) == (best[1], best[2]):
                        continue
                    m = mean_diff(_shift(eb, dx, dy), outside, ob)
                    if m < best[0] - 0.2:
                        best = (m, dx, dy)
    e_al = _shift(e, best[1], best[2]) if (best[1] or best[2]) else e
    removed = mean_diff(e_al, inside) if ImageStat.Stat(inside).sum[0] else 99.0
    edges_final = Image.composite(e_al, o, a).filter(ImageFilter.FIND_EDGES)
    ring_st = ImageStat.Stat(o.filter(ImageFilter.FIND_EDGES), ring)
    in_st = ImageStat.Stat(edges_final, inside)
    ring_sharp = ring_st.mean[0] if ring_st.count[0] else 0.0
    in_sharp = in_st.mean[0] if in_st.count[0] else 0.0
    sharp = (in_sharp / ring_sharp) if ring_sharp > 2 else 1.0
    bad = []
    if best[0] > 20:
        bad.append("misaligned")
    if removed < 4:
        bad.append("not_removed")
    if sharp < 0.3:
        bad.append("smeared")
    return {
        "bad": bad, "align": round(align, 2), "align_fixed": round(best[0], 2),
        "shift": [round(best[1] * w / sw, 1), round(best[2] * h / sh, 1)],
        "removed": round(removed, 2), "sharp": round(sharp, 3),
    }


def _qc_score(qc: dict) -> float:
    """Lower = better, for keeping the better of two tries."""
    return len(qc.get("bad") or []) * 100 + float(qc.get("align_fixed") or 0) - min(float(qc.get("sharp") or 0), 1.0) * 10


def _apply_shift(edit_bytes: bytes, source_bytes: bytes, shift) -> bytes:
    """The edit moved back into line with the original (see _removal_qc), at full size."""
    dx, dy = (shift or [0, 0])[:2]
    if not dx and not dy:
        return edit_bytes
    from io import BytesIO
    from PIL import Image
    img = Image.open(BytesIO(_match_source_dimensions(edit_bytes, source_bytes))).convert("RGB")
    buf = BytesIO()
    _shift(img, dx, dy).save(buf, format="PNG")
    return buf.getvalue()


# Re-try wording when the first removal failed its check - same job, the failure spelled out.
_REMOVAL_RETRY_NOTE = {
    "not_removed": "The previous attempt left the object in place. It must be completely gone.",
    "smeared": "The previous attempt left a blurry smear. Rebuild the floor, ground, wall and trim there with real, sharp texture that matches the photo around it.",
    "misaligned": "The previous attempt shifted the photo. Keep every pixel outside the marked area exactly where it is, at the same framing and scale.",
}


async def _masked_removal_pass(db, user, provider, config, source_bytes, source_ct, prompt, areas) -> tuple[bytes, float, dict]:
    """Pass 1 for a photo with circled areas: remove inside the circles, paste the original back
    outside them, then a free quality check (_removal_qc). A result that fails it gets ONE more
    try (its own budget reservation - the agent's AI allowance and the platform caps still gate
    it, studio_media_removal_qc_retry turns it off); the better of the two is kept. Returns
    (PNG bytes, total cost, the check's numbers)."""
    try:
        mask_png, blend = await asyncio.to_thread(_removal_masks, source_bytes, areas)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Couldn't read the circled areas - try circling again.") from exc
    retry_on = bool(config.get("studio_media_removal_qc_retry", True))
    total = 0.0
    best_bytes, best_qc = None, None
    tries = 2 if retry_on else 1
    for attempt in range(tries):
        try_prompt = prompt
        if attempt and best_qc:
            try_prompt = prompt + "\n\n" + " ".join(_REMOVAL_RETRY_NOTE[b] for b in best_qc["bad"] if b in _REMOVAL_RETRY_NOTE)
        try:
            raw, cost = await _one_masked_edit(db, user, provider, config, source_bytes, source_ct, try_prompt, mask_png)
        except HTTPException:
            if best_bytes is None:
                raise
            break  # the re-try couldn't run (allowance, provider) - keep the first result
        total += cost
        try:
            qc = await asyncio.to_thread(_removal_qc, raw, source_bytes, blend)
        except Exception as exc:  # noqa: BLE001 - a check that can't run never blocks the edit
            logger.warning("studio media removal QC skipped", extra={"error": _exc_text(exc)})
            qc = {"bad": [], "shift": [0, 0], "error": True}
        qc["attempt"] = attempt + 1
        if qc.get("shift") and any(qc["shift"]):
            raw = await asyncio.to_thread(_apply_shift, raw, source_bytes, qc.get("shift"))
        if best_qc is None or _qc_score(qc) < _qc_score(best_qc):
            best_bytes, best_qc = raw, qc
        if not qc["bad"]:
            break
        logger.info("studio media removal QC flagged", extra={"qc": qc})
    best_qc["tries"] = min(attempt + 1, tries)
    out = await asyncio.to_thread(_paste_back_original, best_bytes, source_bytes, blend)
    return out, total, best_qc


async def _one_masked_edit(db, user, provider, config, source_bytes, source_ct, prompt, mask_png) -> tuple[bytes, float]:
    """One masked model call under its own budget reservation, settled at its own cost."""
    try:
        reservation = await reserve_studio_media_image(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as exc:
        raise _rate_limit_http(exc)
    est_cost = config["studio_media_image_reservation_estimate_usd"]
    req = ImageEditRequest(
        image_bytes=source_bytes, image_content_type=source_ct, prompt=prompt,
        size=config["studio_media_image_size"], quality=config["studio_media_image_quality"], mask_bytes=mask_png,
    )
    try:
        try:
            result = await provider.edit(req)
        except ProviderError as exc:
            if exc.not_configured:
                raise
            # A model that won't take a mask still gets the job done from the words - the
            # paste-back below keeps everything outside the circles original either way.
            logger.warning("studio media masked edit retried without mask", extra={"error": _exc_text(exc)})
            req.mask_bytes = None
            result = await provider.edit(req)
    except ProviderError as exc:
        await reconcile_budget(db, reservation, 0.0)
        await record_usage_event(db, user.id, None, provider.model, None, None, 0.0, 0,
                                 True, _studio_media_block_reason(exc), event_type="studio_media_image")
        if exc.not_configured:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Image editing isn't configured yet.") from exc
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't generate that edit right now. Try again in a moment.") from exc
    cost = _image_cost(result, config, est_cost)
    await record_usage_event(db, user.id, None, provider.model, None, None, cost, 0,
                             False, None, event_type="studio_media_image")
    await reconcile_budget(db, reservation, cost)
    return result.image_bytes, cost


# ------------------------------------------------------------------------------- photo edit

async def _run_image(db, user, payload, effects, action_label, options, config, prompt,
                     group_id, parent_asset_id, linked_property, placeholder_id=None):
    # Photo first: a link that won't load used to fail AFTER the budget was reserved, and that
    # reservation was never released (the agent's AI allowance shrank for nothing).
    source_bytes, source_ct = await _fetch_source_bytes(payload.source_photo_url)
    provider = image_provider(config["studio_media_image_provider"], config["studio_media_image_model"])
    est_cost = config["studio_media_image_reservation_estimate_usd"]
    quality = config["studio_media_image_quality"]
    passes = max(1, int(config.get("studio_media_image_passes", 1)))

    # Circled "remove these" areas: the removal runs first, on its own, inside the circles only
    # (original pasted back outside them). Whatever else was picked (twilight, staging, sky...)
    # then runs on that cleaned photo - or, if nothing else was picked, the cleaned photo is
    # the result as-is (no whole-photo re-render that could touch anything else).
    cost = 0.0
    masked_bytes = None
    removal_qc = None
    removal_prompt, rest_effects, rest_options = split_masked_removal(effects, options.model_dump(exclude_none=True)) if options.remove_areas else (None, effects, None)
    if removal_prompt:
        masked_bytes, cost, removal_qc = await _masked_removal_pass(db, user, provider, config, source_bytes, source_ct, removal_prompt, options.remove_areas)
        prompt = (removal_prompt + ("\n\n--- then ---\n\n" + build_stacked_prompt(rest_effects, rest_options) if rest_effects else ""))
        if not rest_effects:
            return await _store_image_asset(db, user, payload, effects, action_label, options, config, provider,
                                            prompt, masked_bytes, source_bytes, cost, est_cost, group_id,
                                            parent_asset_id, linked_property, reservation=None, settled_cost=cost,
                                            placeholder_id=placeholder_id,
                                            extra_options={"removal_qc": removal_qc} if removal_qc else None)
        passes = 1  # the second pass is a whole-photo edit; no extra refine passes on top
        keep = KEEP_EXCEPT_DEBRIS_CLAUSE if "yard_cleanup" in (rest_options.get("enhancements") or []) else KEEP_EVERYTHING_CLAUSE
        main_prompt = f"{keep}\n\n{build_stacked_prompt(rest_effects, rest_options)}"
        prompt = f"{removal_prompt}\n\n--- then ---\n\n{main_prompt}"
    else:
        main_prompt = prompt

    try:
        reservation = await reserve_studio_media_image(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as exc:
        raise _rate_limit_http(exc)

    cur_bytes, cur_ct = (masked_bytes, "image/png") if masked_bytes else (source_bytes, source_ct)
    pre_cost = cost
    cost = 0.0
    result = None
    for i in range(passes):
        pass_prompt = main_prompt if i == 0 else REFINE_PROMPT
        try:
            result = await provider.edit(ImageEditRequest(
                image_bytes=cur_bytes, image_content_type=cur_ct, prompt=pass_prompt,
                size=config["studio_media_image_size"], quality=quality,
            ))
        except ProviderError as exc:
            await reconcile_budget(db, reservation, 0.0)
            await record_usage_event(db, user.id, None, provider.model, None, None, 0.0, 0,
                                     True, _studio_media_block_reason(exc), event_type="studio_media_image")
            if exc.not_configured:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                    "Image editing isn't configured yet.") from exc
            logger.warning("studio media image provider error", extra={"error": _exc_text(exc), "pass": i})
            if result is None:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                    "Couldn't generate that edit right now. Try again in a moment.") from exc
            break
        cost += _image_cost(result, config, est_cost)
        cur_bytes, cur_ct = result.image_bytes, result.content_type

    cost = round(cost, 4) or est_cost
    return await _store_image_asset(db, user, payload, effects, action_label, options, config, provider,
                                    prompt, result.image_bytes, source_bytes, pre_cost + cost, est_cost,
                                    group_id, parent_asset_id, linked_property,
                                    reservation=reservation, settled_cost=pre_cost, placeholder_id=placeholder_id,
                                    extra_options={"removal_qc": removal_qc} if removal_qc else None)


async def _store_image_asset(db, user, payload, effects, action_label, options, config, provider, prompt,
                             image_bytes, source_bytes, cost, est_cost, group_id, parent_asset_id, linked_property,
                             reservation=None, settled_cost: float = 0.0, placeholder_id=None,
                             extra_options: Optional[dict] = None):
    """Save a finished photo edit: matched to the source photo's exact size, optional upscale,
    stored, and recorded as the project asset. Settles the spend not settled yet (`settled_cost`
    = already settled, e.g. the circled-removal pass) - against `reservation` when there is one,
    else as a plain usage event - so every AI cost (upscale included) is counted exactly once."""
    # Always store the edit at the source MLS photo's exact aspect first (no crop/distortion).
    final_bytes = _match_source_dimensions(image_bytes, source_bytes)
    try:
        result_url = s3_service.upload_bytes(final_bytes, _S3_PREFIX, ".png", "image/png")
    except Exception as exc:  # noqa: BLE001
        if reservation is not None:
            await reconcile_budget(db, reservation, 0.0)
        logger.warning("studio media image s3 upload failed", extra={"error": _exc_text(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't save the generated image.") from exc

    # ML upscale (Runway/Magnific Precision v2), polled inline. Best-effort: any failure or a
    # timeout just keeps the un-upscaled edit above. Same aspect, more pixels.
    if config.get("studio_media_image_upscale_enabled", False):
        try:
            up_bytes = await _magnific_image_upscale(result_url, config, asset_id=action_label)
            if up_bytes:
                new_url = s3_service.upload_bytes(up_bytes, _S3_PREFIX, ".png", "image/png")
                s3_service.delete_by_url(result_url)
                result_url, final_bytes = new_url, up_bytes
                cost += float(config.get("studio_media_image_upscale_estimate_usd", 0.25) or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("magnific image upscale skipped", extra={"error": _exc_text(exc)})

    cost = round(cost, 4) or est_cost
    unsettled = round(max(0.0, cost - settled_cost), 4)
    if reservation is not None or unsettled > 0:
        await record_usage_event(db, user.id, None, provider.model, None, None, unsettled, 0,
                                 False, None, event_type="studio_media_image")
    if reservation is not None:
        await reconcile_budget(db, reservation, unsettled)

    if placeholder_id:
        # Background run: fill in the "processing" row the agent already sees.
        asset = await db.get(StudioMediaAsset, placeholder_id)
        if asset is None:
            # Deleted while it was still working - nothing to keep (the spend above is real and
            # stays counted; only the file goes).
            s3_service.delete_by_url(result_url)
            raise HTTPException(status.HTTP_410_GONE, "That project was deleted.")
        keep = {k: v for k, v in (asset.options or {}).items() if k in ("client_request_id", "custom_name", "staging_tag")}
        asset.options = {**(options.model_dump(exclude_none=True)), **keep, "effects": effects,
                         "size_bytes": len(final_bytes), **(extra_options or {})}
        asset.status = "succeeded"
        asset.provider, asset.provider_model = provider.name, provider.model
        asset.result_url, asset.prompt, asset.prompt_version, asset.cost_usd = result_url, prompt, PROMPT_VERSION, cost
        asset.error_message = None
    else:
        asset = StudioMediaAsset(
            owner_id=user.id, group_id=group_id, parent_asset_id=parent_asset_id,
            kind="image", action=action_label, status="succeeded",
            source_photo_url=payload.source_photo_url, source_kind=payload.source_kind,
            linked_property=linked_property.model_dump() if linked_property else None,
            options={**(options.model_dump(exclude_none=True)), "effects": effects, "size_bytes": len(final_bytes),
                     **(extra_options or {})},
            provider=provider.name, provider_model=provider.model,
            result_url=result_url, prompt=prompt, prompt_version=PROMPT_VERSION, cost_usd=cost,
        )
        db.add(asset)
    await db.commit()
    await db.refresh(asset)
    await storage_ledger_service.record(db, user.id, "ai_media_image", result_url, len(final_bytes))
    return asset


# ------------------------------------------------------------------------------- AI video

async def _enhance_photo_for_video(image_url: str, config: dict) -> str:
    """Runs the cropped source still through Magnific Precision v2 (light settings -
    studio_media_video_photo_enhance_*) before it's ever handed to Hailuo. Confirmed via a real
    side-by-side test (2026-09-05, single-clip A/B on 2 real listing photos) to noticeably sharpen
    fine architectural detail - crown molding, rug pattern, fabric weave - in the finished video
    versus feeding Hailuo the raw photo directly, without warping straight lines. Best-effort:
    disabled, a failure, or a timeout all just return the original photo unchanged - this is a
    quality add-on, never a hard dependency of video generation actually starting."""
    if not config.get("studio_media_video_photo_enhance_enabled", True):
        return image_url
    try:
        up_bytes = await _magnific_image_upscale(image_url, config, key_prefix="studio_media_video_photo_enhance")
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video photo-enhance skipped", extra={"error": _exc_text(exc)})
        return image_url
    if not up_bytes:
        return image_url
    try:
        return s3_service.upload_bytes(up_bytes, _S3_PREFIX, ".png", "image/png")
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video photo-enhance upload failed", extra={"error": _exc_text(exc)})
        return image_url


async def _run_video(db, user, payload, photos, options, config, group_id, parent_asset_id, linked_property):
    variant = options.video_variant
    variant_data = _variant_dict(variant)
    config = _config_for_video_variant(config, variant)
    # Day -> Twilight used to route to Runway gen4.5 for a true 10s clip. Dropped 2026-09-30: a
    # real job ignored "camera still" (the house morphed) and billed $4.10 with Runway's 4K
    # upscale. It now renders exactly like every other Media Lab video (Hailuo ~6s clip + the
    # ByteDance 2K finish, ~$0.41) - only the prompt differs (build_video_timelapse_prompt).
    # create_asset still allows exactly 1 photo for this style.
    projected_cost = estimate_video_generation_cost_usd(config, photo_count=len(photos))
    variant_cap = variant_data.get("max_cost_per_clip_usd")
    projected_over_cap = (
        bool(variant_cap) and projected_cost["per_clip_usd"] > float(variant_cap) + 0.0001
    )

    # Generation ceiling: render clips + the one upscale pass that (for the default fal pipeline)
    # always runs right after, not a render-only estimate - ByteDance's real cost is cheap/
    # predictable enough now (unlike Runway/Magnific's old $9-13-on-a-full-reel pricing) that it's
    # no longer treated as a separately-gated optional extra, see poll_asset.
    max_cost = config["studio_media_video_max_cost_per_generation_usd"]
    per_clip_secs = int(config.get("studio_media_video_seconds_per_clip", 5))
    expected_duration = len(photos) * per_clip_secs
    enhance_cost_per_photo = (
        float(config.get("studio_media_video_photo_enhance_cost_usd", 0.25) or 0.0)
        if config.get("studio_media_video_photo_enhance_enabled", True) else 0.0
    )
    generation_ceiling = (
        len(photos) * (_clip_cost_estimate_usd(config, per_clip_secs) + enhance_cost_per_photo)
        + expected_duration * _upscale_cost_estimate_per_second_usd(config)
    )
    if generation_ceiling > max_cost + 0.01:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That's more photos than the video limit allows.")

    # enforce_agent_monthly_budget (inside reserve_studio_media_video below) only blocks once
    # the agent is ALREADY at/over their monthly budget - it can't see that THIS video's own
    # worst-case cost would tip them over. Check that here, before committing to anything.
    budget_limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(user.plan_tier)
    if budget_limit and user.id not in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        spent = await total_monthly_ai_cost(db, user.id)
        if spent + generation_ceiling > float(budget_limit) + 0.01:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "That many photos would use more AI budget than you have left this month. "
                "Try fewer photos, or wait until your budget resets on the 1st.",
            )

    try:
        reservation = await reserve_studio_media_video(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as exc:
        raise _rate_limit_http(exc)

    provider = video_provider(config["studio_media_video_provider"], config["studio_media_video_model"])
    ratio = config["studio_media_video_ratio"]

    clips: list[dict] = []
    total_cost = 0.0
    try:
        for idx, photo in enumerate(photos):
            if idx > 0:
                # Space the per-photo submits out - firing 7 image_to_video calls back-to-back
                # is a fast way to trip Runway's 429 rate limit.
                await asyncio.sleep(0.5)
            img_url = await _runway_fetchable_url(photo, config)
            enhanced_url = await _enhance_photo_for_video(img_url, config)
            enhance_cost = (
                float(config.get("studio_media_video_photo_enhance_cost_usd", 0.25) or 0.0)
                if enhanced_url != img_url else 0.0
            )
            prompt_suffix = str(variant_data.get("prompt_suffix") or "")
            prompt_limit = 2000 if provider.name == "fal" else 1000
            if options.video_style == "day_to_twilight":
                # Not a camera move - stored as the clip's "motion" purely as informational
                # metadata (Results tab / regenerate), never read back against VideoVariantMotion.
                motion = "day_to_twilight"
                prompt = build_video_timelapse_prompt(prompt_suffix, max_chars=prompt_limit)
            else:
                requested_motion = variant_data.get("motion")
                motion = requested_motion if requested_motion in _VIDEO_VARIANT_MOTIONS else random.choice(_VIDEO_MOTIONS)
                prompt = build_video_motion_prompt(motion, prompt_suffix, max_chars=prompt_limit)
            submit = await provider.submit(VideoRequest(
                image_url=enhanced_url, prompt=prompt,
                ratio=ratio, duration_seconds=per_clip_secs,
            ), config=config)
            clip_cost = submit.estimated_cost_usd if submit.estimated_cost_usd is not None else _clip_cost_estimate_usd(config, per_clip_secs)
            total_cost += clip_cost + enhance_cost
            clips.append({
                "job_id": submit.job_id, "motion": motion, "source": photo,
                "status": "processing", "cost_usd": round(clip_cost + enhance_cost, 4), "output_url": None,
                "enhanced": enhanced_url != img_url, "prompt": prompt,
                # The EXACT image Hailuo received (post watermark-crop, post optional Magnific
                # enhance) - not the same as "source" above, which is the original un-cropped
                # photo. Cheap to always keep (just a URL) so the Results tab can show it.
                "video_source_url": enhanced_url,
            })
    except HTTPException:
        # A photo that wouldn't load / prepare part-way through: settle the reservation at what
        # the clips already submitted cost instead of leaving it held.
        await reconcile_budget(db, reservation, total_cost)
        raise
    except ProviderError as exc:
        await reconcile_budget(db, reservation, 0.0)
        await record_usage_event(db, user.id, None, provider.model, None, None, 0.0, 0,
                                 True, _studio_media_block_reason(exc), event_type="studio_media_video")
        if exc.not_configured:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Video generation isn't configured yet.") from exc
        if getattr(exc, "out_of_credits", False):
            logger.error("studio media video: provider out of credits")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "AI Video is temporarily unavailable. Please try again later or contact support.") from exc
        if getattr(exc, "rate_limited", False):
            logger.warning("studio media video: provider rate-limited")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "OpenHousePal AI is busy right now. Wait a minute, then try again.") from exc
        logger.warning("studio media video submit error", extra={"error": _exc_text(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "OpenHousePal AI couldn't start that video right now. Try again in a moment.") from exc

    asset_options = {
        **(options.model_dump(exclude_none=True)),
        "clips": clips, "photo_count": len(photos),
        "seconds_per_clip": per_clip_secs,
        "duration_seconds": per_clip_secs * len(photos),
        "projected_cost_usd": projected_cost["total_usd"],
        "projected_cost_per_clip_usd": projected_cost["per_clip_usd"],
        "projected_cost_breakdown": projected_cost,
        "projected_over_variant_cap": projected_over_cap,
    }
    if getattr(payload, "client_request_id", None):
        asset_options["client_request_id"] = payload.client_request_id
    if variant_data:
        asset_options["video_variant_effective"] = {
            **variant_data,
            "provider": provider.name,
            "provider_model": provider.model,
            "upscale_vendor": config.get("studio_media_video_upscale_vendor"),
            "upscale_resolution": config.get("studio_media_video_upscale_resolution"),
        }

    asset = StudioMediaAsset(
        owner_id=user.id, group_id=group_id, parent_asset_id=parent_asset_id,
        kind="video", action="animate", status="processing",
        source_photo_url=photos[0], source_kind=payload.source_kind,
        linked_property=linked_property.model_dump() if linked_property else None,
        options=asset_options,
        provider=provider.name, provider_model=provider.model,
        provider_job_id=clips[0]["job_id"],
        prompt=None, prompt_version=PROMPT_VERSION,
        cost_usd=round(total_cost, 4),
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    # Reservation is trued up in poll_asset / _settle_video once every clip finishes.
    return asset


async def upscale_video(db: AsyncSession, user: User, asset_id: str) -> StudioMediaAsset:
    """Opt-in: push a finished video through an AI upscale pass and store the result as a NEW
    child asset. The original is untouched. Always uses the fal/ByteDance provider regardless of
    which provider originally rendered the video - ByteDance's upscaler takes any video_url, so
    this works the same whether the source came from fal, Runway, or MiniMax. Polled the same way
    a render is (single synthetic 'upscale' clip), settled in poll_asset."""
    config = await ai_config_service.get_config(db)
    if not config.get("studio_media_video_upscale_enabled", True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "AI upscaling is turned off right now.")
    await _enforce_storage_cap(db, user, config)

    src = await get_owned(db, user.id, asset_id)
    if src.kind != "video" or src.status != "succeeded" or not src.result_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only a finished video can be upscaled.")
    if (src.action or "").endswith("+hd"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That video has already been upscaled.")

    # Check duration up front instead of burning a call + a reservation on something that can't
    # work. duration_seconds is stored at render time; fall back to photo_count x seconds_per_clip.
    _opts = src.options or {}
    dur = _opts.get("duration_seconds") or (
        int(_opts.get("photo_count") or 0) * int(_opts.get("seconds_per_clip") or 5)
    )
    max_dur = int(config.get("studio_media_video_upscale_max_seconds", 30))
    if dur and dur > max_dur:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"AI upscale works on videos up to {max_dur} seconds. This one is ~{dur}s — "
            f"regenerate it with {max_dur // int(_opts.get('seconds_per_clip') or 5)} photos or fewer, then upscale.",
        )

    # Precompute the real, duration-scaled cost BEFORE calling fal so an unaffordable request
    # never gets submitted (and billed) in the first place - ByteDance's real cost isn't known
    # until the job completes, this is a conservative pre-flight estimate only.
    max_cost = config.get("studio_media_video_upscale_max_cost_usd", 2.5)
    pre_est = round(float(dur or 0) * _upscale_cost_estimate_per_second_usd(config), 4)
    if pre_est > max_cost + 0.01:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"AI upscale on a ~{int(dur or 0)}s video would cost about ${pre_est:.2f}, above the ${max_cost:.2f} per-video cap.",
        )

    try:
        reservation = await reserve_studio_media_video(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as exc:
        raise _rate_limit_http(exc)

    provider = video_provider("fal", config["studio_media_video_model"])
    est_cost = config["studio_media_video_reservation_estimate_usd"]
    try:
        # Go through submit_video_upscale (not the fixed-signature submit_upscale() wrapper) so
        # the configured resolution/fidelity/enhancement_preset actually reach fal - the wrapper
        # takes no kwargs and was silently always requesting 4K regardless of config.
        # duration_seconds is needed for Topaz's cost calc (its response has no duration field) -
        # harmless to always pass, ByteDance's branch just ignores it.
        submit = await provider.submit_video_upscale(src.result_url, config=config, duration_seconds=dur)
    except ProviderError as exc:
        await reconcile_budget(db, reservation, 0.0)
        await record_usage_event(db, user.id, None, provider.model, None, None, 0.0, 0,
                                 True, _studio_media_block_reason(exc), event_type="studio_media_video")
        if exc.not_configured:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Video upscaling isn't configured yet.") from exc
        if getattr(exc, "out_of_credits", False):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "AI upscaling is temporarily unavailable. Please try again later.") from exc
        if getattr(exc, "rate_limited", False):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "OpenHousePal AI is busy right now. Wait a minute, then try again.") from exc
        logger.warning("studio media upscale submit error", extra={"asset_id": src.id, "error": _exc_text(exc)})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "OpenHousePal AI couldn't process that right now. Try again in a moment.",
        ) from exc

    # fal never reports a cost at submit time (real cost isn't known until the job completes, see
    # poll()) - fall back to the upscale-scoped pre_est computed above, not the generation-sized
    # est_cost reservation, which would be a much larger number and trip this check spuriously.
    clip_cost = submit.estimated_cost_usd if submit.estimated_cost_usd is not None else pre_est
    max_cost = config.get("studio_media_video_upscale_max_cost_usd", 2.5)
    if clip_cost > max_cost + 0.01:
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That upscale would cost more than the per-video cap.")

    asset = StudioMediaAsset(
        owner_id=user.id, group_id=src.group_id, parent_asset_id=src.id,
        kind="video", action=f"{src.action or 'animate'}+hd", status="processing",
        source_photo_url=src.source_photo_url, source_kind=src.source_kind,
        linked_property=src.linked_property,
        options={
            "clips": [{"job_id": submit.job_id, "status": "processing", "kind": "upscale",
                       "cost_usd": round(clip_cost, 4), "output_url": None}],
            "upscale_of": src.id, "photo_count": (src.options or {}).get("photo_count"),
        },
        provider=provider.name, provider_model=provider.model,
        provider_job_id=submit.job_id,
        prompt=None, prompt_version=PROMPT_VERSION, cost_usd=round(clip_cost, 4),
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


# Every re-encode in this pipeline is HD (1080p) or above, so BT.709 (the correct color space for
# HD/2K/4K web video - BT.601 is for SD) applies throughout. Tagged explicitly on every output
# instead of leaving primaries/transfer/matrix/range unspecified and letting a decoder (or the
# ByteDance upscaler's own decode step) guess - an unspecified or mismatched tag, not an actual
# pixel-value change, is a classic cause of a washed-out/low-contrast look after a re-encode.
# "-color_range tv" is the correct limited (16-235) range for H.264 web video, matching what every
# clip in this pipeline already is - this makes it explicit, it doesn't convert anything.
_BT709_COLOR_FLAGS = [
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    "-color_range", "tv",
]


def _concat_mp4(clip_paths: list[str], out_path: str) -> None:
    """Stitch the Runway clips into one file. Try stream-copy first (instant, lossless - all
    clips share codec/res/fps); fall back to a re-encode if the copy muxer complains."""
    ffmpeg = _ffmpeg_exe()
    list_path = out_path + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{p.replace(chr(39), chr(92) + chr(39))}'\n")
    base = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path]
    copy_cmd = base + ["-c", "copy", "-movflags", "+faststart", out_path]
    if subprocess.run(copy_cmd, capture_output=True).returncode == 0 and os.path.getsize(out_path) > 0:
        return
    reenc = base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", *_BT709_COLOR_FLAGS, "-movflags", "+faststart", out_path]
    r = subprocess.run(reenc, capture_output=True)
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"ffmpeg concat failed: {r.stderr.decode('utf-8', 'ignore')[:400]}")


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _extract_preview_frame_bytes(video_bytes: bytes, *, at_seconds: float = 2.5) -> Optional[bytes]:
    """Best-effort representative JPEG still for side-by-side video QA. Failures never block
    delivery of the video itself."""
    tmpdir = tempfile.mkdtemp(prefix="preview_")
    try:
        src = os.path.join(tmpdir, "source.mp4")
        out = os.path.join(tmpdir, "preview.jpg")
        with open(src, "wb") as f:
            f.write(video_bytes)
        cmd = [
            _ffmpeg_exe(), "-y", "-ss", f"{max(0.1, at_seconds):.3f}", "-i", src,
            "-frames:v", "1", "-q:v", "2", out,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            logger.warning("studio media preview frame failed", extra={
                "error": r.stderr.decode("utf-8", "ignore")[:400],
            })
            return None
        with open(out, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media preview frame skipped", extra={"error": _exc_text(exc)})
        return None
    finally:
        _rmtree_quiet(tmpdir)


def _store_preview_frame(video_bytes: bytes, opts: dict) -> Optional[tuple[str, int]]:
    seconds = float(opts.get("duration_seconds") or 5.0)
    at_seconds = min(max(1.0, seconds / 2.0), max(1.0, seconds - 0.25))
    frame = _extract_preview_frame_bytes(video_bytes, at_seconds=at_seconds)
    if not frame:
        return None
    try:
        url = s3_service.upload_bytes(frame, _S3_PREFIX, ".jpg", "image/jpeg")
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media preview upload failed", extra={"error": _exc_text(exc)})
        return None
    opts["preview_frame_url"] = url
    opts["preview_frame_second"] = round(at_seconds, 2)
    opts["preview_frame_size_bytes"] = len(frame)
    return url, len(frame)


def _probe_video_size(path: str) -> Optional[tuple[int, int]]:
    """Real pixel size of a video file, parsed from ffmpeg's own `-i` stderr stream-info line
    (imageio_ffmpeg only bundles the ffmpeg binary, not ffprobe). Used to skip a redundant scale
    pass when the stitched source already matches the target size - e.g. fal/Hailuo's native
    1080p output feeding the 1080p AI-upscale base needs no scale at all. Returns None (caller
    then assumes a scale IS needed - the safe default) if the probe fails for any reason."""
    try:
        r = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, timeout=15)
        text = r.stderr.decode("utf-8", "ignore")
        m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video size probe failed", extra={"error": _exc_text(exc)})
    return None


def _probe_video_duration_seconds(path: str) -> Optional[float]:
    """Real duration parsed from ffmpeg's own `-i` stderr (same no-ffprobe-bundled constraint as
    _probe_video_size above). Needed by _composite_overlay: `-shortest` alone does NOT reliably
    bound the output when one input is `-loop 1` on a still image - confirmed live (2026-09-05),
    it ran for minutes past an 8s source and produced a truncated multi-hundred-MB file - so the
    overlay composite passes an explicit `-t` from this instead of trusting `-shortest` alone."""
    try:
        r = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, timeout=15)
        text = r.stderr.decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video duration probe failed", extra={"error": _exc_text(exc)})
    return None


def _probe_video_size_bytes(video_bytes: bytes) -> Optional[tuple[int, int]]:
    tmpdir = tempfile.mkdtemp(prefix="probe_")
    try:
        src = os.path.join(tmpdir, "source.mp4")
        with open(src, "wb") as f:
            f.write(video_bytes)
        return _probe_video_size(src)
    finally:
        _rmtree_quiet(tmpdir)


def _trim_copy(src_path: str, out_path: str, trim_seconds: float) -> bool:
    """Lossless stream-copy trim (-c copy -t) - no re-encode, no quality loss, no color-tag risk -
    for when the ONLY thing needed is cutting the tail off an already-correct video. May land up
    to one keyframe interval short/long of the exact requested length (stream copy can't cut on a
    non-keyframe boundary); fine for this feature's ~0.5s trim. Returns False on any failure so
    the caller can fall back to a real re-encode."""
    cmd = [_ffmpeg_exe(), "-y", "-i", src_path, "-t", f"{trim_seconds:.3f}",
           "-c", "copy", "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


# One render, exported to any of these on download (center-crop from the 16:9 base, then scale
# to the platform's standard size - no re-render, no new asset row). "16:9" is the stored base.
_VIDEO_FORMATS: dict[str, tuple[int, int, int, int]] = {
    # key:   (aspect_w, aspect_h, out_w, out_h)
    "16:9": (16, 9, 1280, 720),    # YouTube / landscape (the base - served as-is)
    "9:16": (9, 16, 1080, 1920),   # Reels / TikTok / Shorts / Stories
    "1:1":  (1, 1, 1080, 1080),    # Instagram / Facebook feed (square)
    "4:5":  (4, 5, 1080, 1350),    # Instagram / Facebook feed (portrait)
}
VIDEO_FORMAT_KEYS = tuple(_VIDEO_FORMATS)


def _run_vf(
    src_path: str, out_path: str, vf: str, trim_seconds: Optional[float] = None,
    *, preset: str = "veryfast", crf: int = 22,
) -> None:
    """Re-encode src -> out applying one ffmpeg video filter. Runway clips carry no audio, so
    -an keeps the output clean and small. `trim_seconds` caps output length (ffmpeg -t) -
    a no-op if the source is already shorter. `preset`/`crf` tune the x264 encode (the finish
    pass uses a slower, higher-quality setting than the fast download-time reframes)."""
    cmd = [_ffmpeg_exe(), "-y", "-i", src_path, "-vf", vf]
    if trim_seconds and trim_seconds > 0:
        cmd += ["-t", f"{trim_seconds:.3f}"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        *_BT709_COLOR_FLAGS,
        "-an", "-movflags", "+faststart", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"ffmpeg filter failed: {r.stderr.decode('utf-8', 'ignore')[:400]}")


def _base_size(config: dict) -> tuple[int, int]:
    """16:9 dimensions the assembled base is stored at, when a scale pass is actually needed (see
    _assemble_final - fal/Hailuo's native 1080p output often already matches the target and needs
    none). Bumps to `studio_media_video_output_height` (1080 by default). Width follows the
    height at 16:9, kept even for yuv420p."""
    try:
        h = int(config.get("studio_media_video_output_height", 1080) or 1080)
    except (TypeError, ValueError):
        h = 1080
    h = max(720, min(2160, h))
    w = round(h * 16 / 9)
    return (w - (w % 2), h - (h % 2))


def _finish_vf(config: dict, ow: int, oh: int) -> str:
    """The finish/assembly filter chain: optional denoise (runs before scaling, cheap + more
    effective) -> high-quality upscale to `ow`x`oh` -> optional contrast-adaptive sharpen (on the
    upscaled frames). Denoise + sharpen + scale flags are all DB-tunable;
    `studio_media_video_enhance=false` gives the old plain lanczos resize. No de-brand crop here
    any more (2026-09-04) - the Bright MLS watermark is cropped off the SOURCE STILL IMAGE before
    generation instead, see _runway_fetchable_url, so Hailuo never renders it in the first place."""
    parts: list[str] = []
    enhance = bool(config.get("studio_media_video_enhance", True))
    if enhance:
        dn = str(config.get("studio_media_video_enhance_denoise") or "").strip()
        if dn:
            parts.append(dn)

    flags = str(config.get("studio_media_video_scale_flags") or "lanczos").strip() or "lanczos"
    parts.append(f"scale={ow}:{oh}:flags={flags}")

    if enhance:
        sh = str(config.get("studio_media_video_enhance_sharpen") or "").strip()
        if sh:
            parts.append(sh)

    parts.append("setsar=1")
    return ",".join(parts)


async def _download_and_concat(clips: list[dict], tmpdir: str) -> str:
    """Fetch every finished Runway clip and stitch them into one mp4. Returns its path."""
    paths: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
        for i, c in enumerate(clips):
            r = await client.get(c["output_url"])
            r.raise_for_status()
            p = os.path.join(tmpdir, f"c{i:02d}.mp4")
            with open(p, "wb") as f:
                f.write(r.content)
            paths.append(p)
    stitched = os.path.join(tmpdir, "stitched.mp4")
    if len(paths) == 1:
        os.replace(paths[0], stitched)
    else:
        _concat_mp4(paths, stitched)
    return stitched


def _assemble_final(
    stitched: str, tmpdir: str, config: dict, asset, *,
    out_wh: Optional[tuple[int, int]], enhance: bool,
) -> tuple[bytes, float]:
    """One ffmpeg finish pass on the stitched reel: (optional denoise/upscale/sharpen) -> trim.
    `out_wh` None = the configured 4K base size; give a smaller size for the compact base handed
    to the AI upscale step. `enhance` toggles the denoise+sharpen+slow-encode quality pass vs. a
    plain fast resize. Returns (bytes, actual seconds). Never raises - a failed filter pass just
    returns the un-processed stitch.

    Minimizes re-encoding on purpose (2026-09-04 - feeding the ByteDance upscale step a clean,
    minimally-processed source matters for its output quality): no de-brand crop here any more
    (done pre-generation, see _finish_vf); `needs_scale` is decided from the STITCHED FILE'S OWN
    real resolution (probed via ffmpeg, not assumed) so a video that's already at the target size
    - e.g. fal/Hailuo's native 1080p feeding the 1080p AI-upscale base - triggers no scale pass at
    all; and a trim-only case (no scale, no enhance) uses a lossless stream-copy cut instead of a
    full re-encode."""
    base_w, base_h = out_wh if out_wh else _base_size(config)
    src_size = _probe_video_size(stitched)
    needs_scale = src_size is None or src_size != (base_w, base_h)
    raw_secs = int((asset.options or {}).get("duration_seconds") or 0)
    max_out_secs = float(config.get("studio_media_video_max_output_seconds", 0) or 0)
    trim = max_out_secs if (max_out_secs and raw_secs and raw_secs > max_out_secs) else None
    final_secs = min(raw_secs, max_out_secs) if trim else raw_secs
    out_path = stitched

    trimmed_via_copy = False
    if not (needs_scale or enhance) and trim:
        copied = os.path.join(tmpdir, "trimmed.mp4")
        if _trim_copy(stitched, copied, trim):
            out_path = copied
            trimmed_via_copy = True

    if not trimmed_via_copy and (needs_scale or trim or enhance):
        cleaned = os.path.join(tmpdir, "assembled.mp4")
        vf = _finish_vf({**config, "studio_media_video_enhance": enhance}, base_w, base_h)
        if enhance:
            preset = str(config.get("studio_media_video_finish_preset", "medium") or "medium")
            try:
                crf = int(config.get("studio_media_video_finish_crf", 19))
            except (TypeError, ValueError):
                crf = 19
        else:
            preset, crf = "veryfast", 21
        try:
            _run_vf(stitched, cleaned, vf, trim_seconds=trim, preset=preset, crf=crf)
            out_path = cleaned
        except Exception as exc:  # noqa: BLE001
            logger.warning("studio media assemble finish pass skipped", extra={"asset_id": asset.id, "error": _exc_text(exc)})
            final_secs = raw_secs
    with open(out_path, "rb") as f:
        return f.read(), float(final_secs)


def _reframe_vf(ratio_key: str) -> str:
    """Center-crop the 4K base to `ratio_key`'s aspect, then downscale to that platform's
    standard size (1080-class - Reels/TikTok/feed all cap at 1080 on the short side, so there's
    no point going bigger). A light contrast-adaptive sharpen keeps the downscale crisp."""
    aw, ah, ow, oh = _VIDEO_FORMATS[ratio_key]
    return (
        f"crop='if(gte(iw/ih,{aw}/{ah}),ih*{aw}/{ah},iw)'"
        f":'if(gte(iw/ih,{aw}/{ah}),ih,iw*{ah}/{aw})'"
        f",scale={ow}:{oh}:flags=lanczos+accurate_rnd+full_chroma_int,cas=0.3,setsar=1"
    )


async def reframe_video_bytes(result_url: str, ratio_key: str) -> bytes:
    """Download the stored 16:9 base and return it re-cropped to `ratio_key`. On-the-fly, not
    persisted - the point is one render exported to whatever format the agent wants."""
    if ratio_key not in _VIDEO_FORMATS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown video format.")
    tmpdir = tempfile.mkdtemp(prefix="reframe_")
    try:
        src = os.path.join(tmpdir, "base.mp4")
        out = os.path.join(tmpdir, "out.mp4")
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
            r = await client.get(result_url)
            r.raise_for_status()
        with open(src, "wb") as f:
            f.write(r.content)
        _run_vf(src, out, _reframe_vf(ratio_key), preset="fast", crf=20)
        with open(out, "rb") as f:
            return f.read()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media reframe failed", extra={"ratio": ratio_key, "error": _exc_text(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't export that format. Try again in a moment.") from exc
    finally:
        _rmtree_quiet(tmpdir)


# ---------------------------------------------------------------------------------------------
# Video overlays: an intro "title card" (preset + property stats, 0-5s, fades out) and a separate
# always-on agent-branding corner badge - both burned in only at export/download time (Pillow PNG
# + one ffmpeg composite pass), never touching the stored master render, exactly like the
# ratio reframe above. See VideoOverlayUpdateRequest/VideoExportRequest in schemas/studio_media.py
# and export_video_bytes below.
# ---------------------------------------------------------------------------------------------

_FONT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts", "Inter-Variable.ttf")
_FONT_OPSZ_MAX = 32.0  # Inter's variable "optical size" axis - max is the display-weight instance

_TITLE_CARD_PRESET_TITLES = {
    "just_listed": "Just Listed",
    "just_sold": "Just Sold",
    "coming_soon": "Coming Soon",
}
# One preset-colored accent (a short rule above the title + the stat pills' outline) - the one
# flourish that ties the card to whichever status it's announcing without repeating the title text.
_TITLE_CARD_PRESET_ACCENTS = {
    "just_listed": (201, 162, 77),   # brand gold - matches the app's own accent
    "just_sold": (74, 176, 110),     # green - "sold" reads as a positive/closed status
    "coming_soon": (94, 150, 219),   # blue - distinct from both listed/sold
    "custom": (201, 162, 77),
}
_TITLE_CARD_SECONDS = 5.0
_TITLE_CARD_FADE_SECONDS = 1.0


def _inter_font(size: float, weight: int):
    """Inter is a single variable-font file (opsz + wght axes) - one file covers every weight
    this feature needs, no separate static .ttf per weight to bundle/keep in sync."""
    from PIL import ImageFont
    f = ImageFont.truetype(_FONT_PATH, max(1, round(size)))
    try:
        f.set_variation_by_axes([_FONT_OPSZ_MAX, weight])
    except Exception:  # noqa: BLE001 - falls back to the font's default instance, still renders
        pass
    return f


def _text_size(draw, text: str, font) -> tuple[float, float]:
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0], b[3] - b[1]


def _fit_font(draw, text: str, max_w: float, base_size: float, min_size: float, weight: int):
    """Shrinks a title/subtitle down (never below min_size) until it fits max_w, so an agent's
    own long custom title never overflows the frame."""
    size = base_size
    while size > min_size:
        f = _inter_font(size, weight)
        w, _ = _text_size(draw, text, f)
        if w <= max_w:
            return f
        size -= 2
    return _inter_font(min_size, weight)


def _draw_stat_pill(draw, xy: tuple[float, float], text: str, font, *, pad_x: float, pad_y: float,
                     outline: tuple[int, int, int, int]) -> float:
    """Draws one rounded-rect, text-only stat badge ("4 bd", "5,437 sqft", ...). Returns its
    total width so the caller can lay out the next one."""
    x, y = xy
    w, h = _text_size(draw, text, font)
    box = (x, y, x + w + pad_x * 2, y + h + pad_y * 2)
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) / 2, fill=(255, 255, 255, 48), outline=outline, width=2)
    b = draw.textbbox((0, 0), text, font=font)
    draw.text((x + pad_x, y + pad_y - b[1]), text, font=font, fill=(255, 255, 255, 255))
    return w + pad_x * 2


def _fmt_count(v) -> Optional[str]:
    """A whole number prints bare (4 -> "4"); anything else keeps one decimal (2.5 baths)."""
    if v is None:
        return None
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _title_card_fields(cfg: dict, linked_property: Optional[dict]) -> tuple[str, str, str, list[str]]:
    """Resolves (address, location, price, [stat pill strings]) from a title_card options dict -
    preferring the real linked listing, falling back to whatever the agent typed manually (the
    only source for an upload-sourced video with no linked_property, or to override a listing's
    real value). A toggle OFF always wins, regardless of what's typed/available. Price is kept
    separate from the bd/ba/sqft stats - it gets its own prominent line under the address rather
    than sitting in the small stat-pill row (matches the frontend preview's layout)."""
    lp = linked_property or {}
    manual = cfg.get("manual") or {}

    def field(show_key: str, manual_key: str, listing_value: Optional[str]) -> str:
        if not cfg.get(show_key, True):
            return ""
        typed = str(manual.get(manual_key) or "").strip()
        return typed or str(listing_value or "").strip()

    address = field("show_address", "address", lp.get("address"))
    location = field("show_location", "location", ", ".join(x for x in [lp.get("city"), lp.get("state")] if x))
    beds = field("show_beds", "beds", _fmt_count(lp.get("beds")))
    baths = field("show_baths", "baths", _fmt_count(lp.get("baths")))
    sqft = field("show_sqft", "sqft", f"{int(lp['sqft']):,}" if lp.get("sqft") else None)
    price_raw = field("show_price", "price", f"{int(lp['price']):,}" if lp.get("price") else None)
    price = (price_raw if price_raw.startswith("$") else f"${price_raw}") if price_raw else ""

    stats: list[str] = []
    if beds:
        stats.append(f"{beds} bd")
    if baths:
        stats.append(f"{baths} ba")
    if sqft:
        stats.append(f"{sqft} sqft")
    return address, location, price, stats


def _render_title_card_png(cfg: dict, linked_property: Optional[dict], out_w: int, out_h: int) -> bytes:
    """Pillow-rendered RGBA PNG (transparent except the card itself) for the 0-5s intro title
    card: a gradient scrim, a preset-accented rule, the big title, an address/location subtitle,
    and up to 4 text-only stat pills (wrapping to a second row rather than shrinking illegibly
    small or overflowing the frame edge). `cfg["position"]` ("top" | "middle" | "bottom", default
    "bottom") picks where the whole card sits - the content block is measured first so "middle"
    can be centered exactly rather than guessed at. Sized exactly to out_w x out_h so the same
    layout works for every export ratio (see _VIDEO_FORMATS). Composited onto the video by
    _composite_overlay(timed=True) - never persisted to the stored master render."""
    import math
    from io import BytesIO
    from PIL import Image, ImageDraw

    preset = cfg.get("preset") or "just_listed"
    accent = _TITLE_CARD_PRESET_ACCENTS.get(preset, _TITLE_CARD_PRESET_ACCENTS["just_listed"])
    title = str(cfg.get("title") or _TITLE_CARD_PRESET_TITLES.get(preset, "Just Listed")).strip()
    address, location, price, stats = _title_card_fields(cfg, linked_property)
    position = cfg.get("position") or "bottom"
    # "middle" gets no scrim at all (no dark band behind the text) - only top/bottom anchor
    # against a photo edge where a band actually helps. A soft drop-shadow behind each line of
    # text stands in for it there instead, so centered text over a bright photo still reads.
    shadow = position == "middle"

    def draw_text(xy: tuple[float, float], text: str, font, fill: tuple[int, int, int, int]) -> None:
        x, y = xy
        if shadow:
            draw.text((x + 2, y + 2.5), text, font=font, fill=(0, 0, 0, 190))
        draw.text((x, y), text, font=font, fill=fill)

    img = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = out_w / 2
    # Caps the type scale to the frame's shorter effective dimension, so a wide 16:9 export
    # doesn't blow the title up huge relative to its own (much shorter) height.
    unit = min(out_w, out_h * 9 / 16)

    # Measurement pass - textbbox/textlength don't paint anything, so every size needed to lay
    # the card out (and, for "middle", to center it) can be resolved before any pixel is drawn.
    bar_w, bar_h = unit * 0.11, unit * 0.012
    title_f = _fit_font(draw, title, out_w * 0.86, unit * 0.145, unit * 0.06, 900)
    tw, th = _text_size(draw, title, title_f)

    subtitle = "  ·  ".join(p for p in [address, location] if p)
    sub_f = sw = sh = None
    if subtitle:
        sub_f = _fit_font(draw, subtitle, out_w * 0.86, unit * 0.052, unit * 0.03, 600)
        sw, sh = _text_size(draw, subtitle, sub_f)

    # Price is its own prominent standout line under the address - not another small stat pill -
    # so it gets a much bigger, bolder size than the subtitle, in the preset accent color.
    price_f = pw = ph = None
    if price:
        price_f = _fit_font(draw, price, out_w * 0.8, unit * 0.09, unit * 0.045, 900)
        pw, ph = _text_size(draw, price, price_f)

    pill_f = pad_x = pad_y = gap = row_gap = None
    rows: list[list[str]] = []
    if stats:
        pill_f = _inter_font(unit * 0.042, 700)
        pad_x, pad_y, gap, row_gap = unit * 0.045, unit * 0.026, unit * 0.03, unit * 0.028
        max_row_w = out_w * 0.9

        def row_width(row: list[str]) -> float:
            return sum(_text_size(draw, s, pill_f)[0] + pad_x * 2 for s in row) + gap * max(0, len(row) - 1)

        rows = [stats]
        if row_width(stats) > max_row_w and len(stats) > 1:
            mid = math.ceil(len(stats) / 2)
            rows = [stats[:mid], stats[mid:]]

    stats_h = sum((pill_f.size + pad_y * 2 + row_gap) for _ in rows) if stats else 0.0
    content_h = (
        bar_h + unit * 0.03 + th + unit * 0.045
        + (sh + unit * 0.02 if subtitle else 0.0)
        + (ph + unit * 0.05 if price else unit * 0.02)
        + stats_h
    )

    # Scrim height used to be a flat 50% of out_h regardless of aspect ratio - fine on a tall
    # portrait export (plenty of room relative to the actual text block), but on a wide landscape
    # export out_h is much smaller while the text is still sized off `unit` (capped to a
    # portrait-equivalent height), so the fixed-50%-of-height band could end up SHORTER than the
    # actual content block - text spilling out past the dark band, unreadable over the plain
    # photo. Fixed 2026-09-06: size the band to the real measured content height instead, so it
    # always fits regardless of orientation.
    scrim_h = min(out_h, round(content_h + unit * 0.18))
    if not shadow:
        # Scrim band - same height regardless of top/bottom placement; only where it sits and
        # which edge is darkest changes, so text always reads over any photo without a hard edge.
        scrim_top = 0 if position == "top" else out_h - scrim_h
        grad = Image.new("L", (1, scrim_h), 0)
        for y_ in range(scrim_h):
            t = y_ / max(1, scrim_h - 1)
            a = 225 * ((1 - t) ** 1.5) if position == "top" else 225 * (t ** 1.5)
            grad.putpixel((0, y_), round(a))
        grad = grad.resize((out_w, scrim_h))
        black = Image.new("RGBA", (out_w, scrim_h), (4, 6, 12, 255))
        img.paste(black, (0, scrim_top), grad)
    else:
        scrim_top = round((out_h - scrim_h) / 2)
        # "middle" placement has no scrim band (a band only helps anchored against a photo edge),
        # but text sitting directly on a bright, busy part of the photo can still get lost - dim
        # the WHOLE frame a little (not just a band) so the centered text pops regardless of what
        # the photo behind it looks like. Mirrored in the CSS preview's own middle-position layer.
        dim = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 90))
        img.paste(dim, (0, 0), dim)

    if position == "top" or position == "bottom":
        # Same inset from the scrim's own outer edge either way, now that the band is sized to
        # the content with even top/bottom breathing room - top and bottom converge to one
        # formula instead of needing a separate hand-tuned constant for "bottom".
        y = scrim_top + unit * 0.09
    else:
        y = scrim_top + max(unit * 0.04, (scrim_h - content_h) / 2)

    draw.rounded_rectangle((cx - bar_w / 2, y, cx + bar_w / 2, y + bar_h), radius=bar_h / 2, fill=(*accent, 255))
    y += bar_h + unit * 0.03

    tb = draw.textbbox((0, 0), title, font=title_f)
    draw_text((cx - tw / 2, y - tb[1]), title, title_f, (255, 255, 255, 255))
    y += th + unit * 0.045

    if subtitle:
        sb = draw.textbbox((0, 0), subtitle, font=sub_f)
        draw_text((cx - sw / 2, y - sb[1]), subtitle, sub_f, (255, 255, 255, 235))
        y += sh + unit * 0.02

    if price:
        y += unit * 0.03
        pb = draw.textbbox((0, 0), price, font=price_f)
        draw_text((cx - pw / 2, y - pb[1]), price, price_f, (*accent, 255))
        y += ph + unit * 0.02
    else:
        y += unit * 0.02

    for row in rows:
        total_w = sum(_text_size(draw, s, pill_f)[0] + pad_x * 2 for s in row) + gap * max(0, len(row) - 1)
        x = cx - total_w / 2
        for s in row:
            w = _draw_stat_pill(draw, (x, y), s, pill_f, pad_x=pad_x, pad_y=pad_y, outline=(*accent, 210))
            x += w + gap
        y += pill_f.size + pad_y * 2 + row_gap

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _render_branding_png(
    user: User, brand_kit: Optional[StudioBrandKit], out_w: int, out_h: int, *, position: str = "top-left",
) -> Optional[bytes]:
    """Pillow-rendered RGBA PNG for the small always-on agent-branding corner badge: a headshot
    (falling back to the brand kit's logo, then plain initials) + name + brokerage - the same
    "agent photo/name/brokerage" data Forms already shows, and the same personal_logo_url-or-
    brokerage_logo_url fallback CMA/site already use (see cma_service.py). Independent of the
    title card - composited by _composite_overlay(timed=False) so it's present for the whole
    clip. `position` ("top-left" | "bottom-left" | "top-middle") is the agent's OWN placement
    choice - it used to be silently derived from the title card's position (auto-moved to avoid
    a collision), which meant picking a title-card position moved the badge too. Fixed
    2026-09-06: fully independent field now, see BrandingOptions.position. Returns None (never
    raises) when there's no name/brokerage to show at all."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageOps

    name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
    brokerage = (user.brokerage or "").strip()
    if not name and not brokerage:
        return None

    avatar_url = user.photo_url or (
        brand_kit and (brand_kit.personal_logo_url or brand_kit.team_logo_url or brand_kit.brokerage_logo_url)
    )
    avatar_img = None
    if avatar_url:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True) as client:
                from app.utils.safe_fetch import safe_get
                r = await safe_get(client, avatar_url, headers=_FETCH_HEADERS)
                r.raise_for_status()
            avatar_img = Image.open(BytesIO(r.content))
        except Exception as exc:  # noqa: BLE001 - falls back to plain initials, never blocks export
            logger.warning("studio media branding avatar fetch failed", extra={"error": _exc_text(exc)})
            avatar_img = None

    img = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    unit = min(out_w, out_h * 9 / 16)
    margin = unit * 0.045

    avatar_d = round(unit * 0.115)
    pad = unit * 0.028
    text_offset = avatar_d + unit * 0.032
    # Fit name/brokerage to whatever width is actually left on the canvas (mirrors the title
    # card's own _fit_font use) so a long name never silently draws past the frame edge instead
    # of just wrapping to a smaller size. Worst-case width reservation - same regardless of
    # where the chip actually sits (left-anchored or centered).
    max_text_w = out_w - margin - text_offset - margin
    name_f = _fit_font(draw, name, max_text_w, unit * 0.034, unit * 0.02, 700) if name else _inter_font(unit * 0.034, 700)
    brok_f = _fit_font(draw, brokerage, max_text_w, unit * 0.028, unit * 0.017, 500) if brokerage else _inter_font(unit * 0.028, 500)

    # Its own self-contained dark chip (not the title card's scrim) so the badge stays legible
    # for the WHOLE clip, including the time after the title card has already faded away.
    # `position` is the agent's own independent placement choice (see BrandingOptions.position) -
    # "top-left"/"bottom-left" anchor to the left margin like before; "top-middle" centers the
    # whole chip horizontally instead.
    nw, nh = _text_size(draw, name, name_f) if name else (0, 0)
    bw, bh = _text_size(draw, brokerage, brok_f) if brokerage else (0, 0)
    chip_w = min(text_offset + max(nw, bw) + pad, out_w - margin * 2)
    chip_h = avatar_d + pad * 1.4

    x0 = (out_w - chip_w) / 2 if position == "top-middle" else margin
    y0 = out_h - chip_h - margin if position == "bottom-left" else margin
    draw.rounded_rectangle((x0, y0, x0 + chip_w, y0 + chip_h), radius=chip_h / 2, fill=(8, 10, 16, 165))

    text_x0 = x0 + text_offset
    cy = y0 + chip_h / 2
    av_x, av_y = x0 + pad * 0.35, cy - avatar_d / 2
    if avatar_img is not None:
        try:
            flat = avatar_img
            if flat.mode in ("RGBA", "LA", "P"):
                # A see-through PNG logo goes on white - converting straight to RGB turned its
                # transparent background black.
                rgba = flat.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[3])
            av = ImageOps.fit(flat.convert("RGB"), (avatar_d, avatar_d), method=Image.LANCZOS)
            mask = Image.new("L", (avatar_d, avatar_d), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_d, avatar_d), fill=255)
            circle = Image.new("RGBA", (avatar_d, avatar_d))
            circle.paste(av, (0, 0), mask)
            img.paste(circle, (round(av_x), round(av_y)), circle)
        except Exception as exc:  # noqa: BLE001
            logger.warning("studio media branding avatar render failed", extra={"error": _exc_text(exc)})
            avatar_img = None
    if avatar_img is None:
        draw.ellipse((av_x, av_y, av_x + avatar_d, av_y + avatar_d), fill=(201, 162, 77, 255))
        initials = "".join(w[0] for w in name.split()[:2]).upper() or "•"
        iw, ih = _text_size(draw, initials, name_f)
        ib = draw.textbbox((0, 0), initials, font=name_f)
        draw.text((av_x + avatar_d / 2 - iw / 2, av_y + avatar_d / 2 - ih / 2 - ib[1]), initials, font=name_f, fill=(17, 24, 39, 255))

    ty = cy - (nh + (bh + unit * 0.006 if brokerage else 0)) / 2
    if name:
        nb = draw.textbbox((0, 0), name, font=name_f)
        draw.text((text_x0, ty - nb[1]), name, font=name_f, fill=(255, 255, 255, 255))
    if brokerage:
        bb = draw.textbbox((0, 0), brokerage, font=brok_f)
        ty2 = ty + nh + unit * 0.006
        draw.text((text_x0, ty2 - bb[1]), brokerage, font=brok_f, fill=(230, 230, 235, 210))

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _composite_overlay(video_bytes: bytes, png_bytes: bytes, *, timed: bool) -> bytes:
    """One ffmpeg pass compositing a Pillow-rendered RGBA PNG onto a video. `timed=True` (the
    title card) fades the PNG's alpha out over the last _TITLE_CARD_FADE_SECONDS before
    _TITLE_CARD_SECONDS and stops overlaying it entirely after that; `timed=False` (the agent
    branding badge) keeps it present for the whole clip. `-loop 1` makes the still PNG an
    "infinite" video input - an explicit `-t` (from _probe_video_duration_seconds, NOT `-shortest`
    alone - see that function's docstring for why) is what actually bounds the output to the real
    video's own length. Never persisted to the master render - called only from
    export_video_bytes."""
    tmpdir = tempfile.mkdtemp(prefix="overlay_")
    try:
        src = os.path.join(tmpdir, "src.mp4")
        png_path = os.path.join(tmpdir, "card.png")
        out = os.path.join(tmpdir, "out.mp4")
        with open(src, "wb") as f:
            f.write(video_bytes)
        with open(png_path, "wb") as f:
            f.write(png_bytes)
        duration = _probe_video_duration_seconds(src)
        if timed:
            fade_start = max(0.0, _TITLE_CARD_SECONDS - _TITLE_CARD_FADE_SECONDS)
            filter_complex = (
                f"[1:v]format=rgba,fade=t=out:st={fade_start:.2f}:d={_TITLE_CARD_FADE_SECONDS:.2f}:alpha=1[ov];"
                f"[0:v][ov]overlay=0:0:enable='lte(t,{_TITLE_CARD_SECONDS:.2f})'[vout]"
            )
        else:
            filter_complex = "[0:v][1:v]overlay=0:0[vout]"
        cmd = [
            _ffmpeg_exe(), "-y", "-i", src, "-loop", "1", "-i", png_path,
            "-filter_complex", filter_complex, "-map", "[vout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            *_BT709_COLOR_FLAGS,
        ]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-shortest", "-movflags", "+faststart", out]
        # `-t` above is what actually bounds this (see _probe_video_duration_seconds) - this
        # timeout is only a last-resort safety net for the rare case duration couldn't be probed
        # and it's relying on `-shortest` alone, so a stuck encode can't hang the request forever.
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg overlay timed out") from exc
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError(f"ffmpeg overlay failed: {r.stderr.decode('utf-8', 'ignore')[:400]}")
        with open(out, "rb") as f:
            return f.read()
    finally:
        _rmtree_quiet(tmpdir)


async def _fetch_master_video_bytes(result_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
        r = await client.get(result_url)
        r.raise_for_status()
    return r.content


async def export_video_bytes(
    db: AsyncSession, user: User, asset: StudioMediaAsset, *,
    ratio: Optional[str], title_card: Optional[dict], branding: Optional[dict],
) -> bytes:
    """One render, exported with either/both overlays burned in - never touches the stored
    master. `ratio` reuses reframe_video_bytes exactly like plain /download; branding (if
    enabled) is composited first, the title card second (drawn last, on top). Both overlays are
    independent/optional, so with both omitted this produces the exact same bytes as plain
    /download?ratio= - see studio_media_routes.export_asset."""
    if asset.kind != "video" or not asset.result_url or asset.status != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT, "Not ready")
    ratio_key = ratio if ratio in _VIDEO_FORMATS else "16:9"
    data = (
        await reframe_video_bytes(asset.result_url, ratio_key) if ratio_key != "16:9"
        else await _fetch_master_video_bytes(asset.result_url)
    )
    _, _, out_w, out_h = _VIDEO_FORMATS[ratio_key]

    if branding and branding.get("enabled"):
        brand = (await db.execute(
            select(StudioBrandKit).where(StudioBrandKit.owner_id == asset.owner_id)
        )).scalar_one_or_none()
        # branding.position is the agent's own independent placement choice (see
        # BrandingOptions.position) - this used to be auto-derived from the title card's own
        # position (to dodge a collision), which meant picking a title-card position silently
        # moved the badge too. Fixed 2026-09-06: no more coupling: the agent picks both
        # placements separately and is responsible for choosing ones that don't collide.
        png = await _render_branding_png(user, brand, out_w, out_h, position=branding.get("position", "top-left"))
        if png:
            data = _composite_overlay(data, png, timed=False)

    if title_card and title_card.get("enabled"):
        png = _render_title_card_png(title_card, asset.linked_property, out_w, out_h)
        data = _composite_overlay(data, png, timed=True)

    return data


async def update_video_overlay(
    db: AsyncSession, owner_id: str, asset_id: str,
    *, title_card: Optional[dict], branding: Optional[dict],
) -> StudioMediaAsset:
    """Merges the overlay editor's draft into asset.options - a plain DB write (no AI cost), so
    a configured title card / branding survives a reload without a regenerate."""
    asset = await get_owned(db, owner_id, asset_id)
    opts = dict(asset.options or {})
    if title_card is not None:
        opts["title_card"] = title_card
    if branding is not None:
        opts["branding"] = branding
    asset.options = opts
    await db.commit()
    await db.refresh(asset)
    return asset


async def set_staging_tag(db: AsyncSession, owner_id: str, asset_id: str, position: str) -> StudioMediaAsset:
    """Where (if anywhere) the 'Virtually Staged' pill should be burned into this photo's
    DOWNLOADED copy - see apply_staging_tag. Stored in options, same pattern as custom_name."""
    asset = await get_owned(db, owner_id, asset_id)
    opts = dict(asset.options or {})
    opts["staging_tag"] = position
    asset.options = opts
    await db.commit()
    await db.refresh(asset)
    return asset


def _render_staging_tag_png(position: str, out_w: int, out_h: int) -> bytes:
    """A single rounded, text-only 'VIRTUALLY STAGED' pill, sized/positioned as a fraction of the
    image so it looks right regardless of the source photo's own resolution. Same Inter-font +
    rounded-pill visual language as _draw_stat_pill above. Transparent RGBA canvas, composited by
    apply_staging_tag - never drawn directly onto the stored master."""
    from io import BytesIO
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = "VIRTUALLY STAGED"
    font = _inter_font(max(12, out_w * 0.02), 700)
    pad_x, pad_y = out_w * 0.016, out_h * 0.011
    w, h = _text_size(draw, text, font)
    box_w, box_h = w + pad_x * 2, h + pad_y * 2
    margin = out_w * 0.025
    x = (out_w - box_w) / 2 if position == "bottom-middle" else out_w - box_w - margin
    y = out_h - box_h - margin
    draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=box_h / 2, fill=(17, 24, 39, 215))
    b = draw.textbbox((0, 0), text, font=font)
    draw.text((x + pad_x, y + pad_y - b[1]), text, font=font, fill=(255, 255, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def apply_staging_tag(image_bytes: bytes, position: Optional[str]) -> bytes:
    """Burns the 'Virtually Staged' pill into a DOWNLOADED copy of the image - the stored master
    (asset.result_url) is never touched, exactly like the video title-card/branding overlays are
    only ever composited at export time. `position` of None/"off" is a no-op (returns the input
    unchanged), which is also the default, so this is fully opt-in per project."""
    if position not in ("bottom-right", "bottom-middle"):
        return image_bytes
    try:
        from io import BytesIO
        from PIL import Image
        base = Image.open(BytesIO(image_bytes)).convert("RGBA")
        tag = Image.open(BytesIO(_render_staging_tag_png(position, base.width, base.height)))
        out = Image.alpha_composite(base, tag).convert("RGB")
        buf = BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - never block a download over a cosmetic tag
        logger.warning("studio media staging tag composite failed", extra={"error": _exc_text(exc)})
        return image_bytes


async def rename_asset(db: AsyncSession, owner_id: str, asset_id: str, name: str) -> StudioMediaAsset:
    """Agent-given display name for a finished project. Stored in options.custom_name (same
    approach as title_card/branding) rather than a new schema column - no migration needed."""
    asset = await get_owned(db, owner_id, asset_id)
    opts = dict(asset.options or {})
    trimmed = name.strip()[:120]
    if trimmed:
        opts["custom_name"] = trimmed
    else:
        opts.pop("custom_name", None)
    asset.options = opts
    await db.commit()
    await db.refresh(asset)
    return asset


async def poll_asset(db: AsyncSession, user: User, asset: StudioMediaAsset) -> StudioMediaAsset:
    """Thin wrapper around _poll_asset_body: a video render can take up to ~15 minutes (Hailuo
    render + the ByteDance finishing pass), and the agent is explicitly encouraged to close the
    tab/site rather than babysit it. This is the one place that transition from processing -> a
    terminal state is ever observed (the body returns early on any non-"processing" asset), so
    it's also the one place a "your video is ready" notification can fire exactly once."""
    was_processing = asset.kind == "video" and asset.status == "processing"
    asset = await _poll_asset_body(db, user, asset)
    if was_processing and asset.status in ("succeeded", "failed"):
        await _notify_video_done(db, asset)
    return asset


async def _notify_video_done(db: AsyncSession, asset: StudioMediaAsset) -> None:
    """Fires the Activity-bell notification once a video leaves "processing". Guarded by a DB
    existence check (not just the caller's own state) so a rare concurrent poll from two tabs
    can't double-notify."""
    try:
        exists = (await db.execute(
            select(Notification.id).where(
                Notification.reference_type == "STUDIO_MEDIA_ASSET",
                Notification.reference_id == asset.id,
            ).limit(1)
        )).scalar_one_or_none()
        if exists:
            return
        address = (asset.linked_property or {}).get("address") if asset.linked_property else None
        succeeded = asset.status == "succeeded"
        subject = address or "your property"
        db.add(Notification(
            agent_id=asset.owner_id,
            type="STUDIO_MEDIA_VIDEO_READY" if succeeded else "STUDIO_MEDIA_VIDEO_FAILED",
            reference_type="STUDIO_MEDIA_ASSET",
            reference_id=asset.id,
            title="Your AI property video is ready" if succeeded else "Your AI property video failed",
            message=(
                f"The AI video for {subject} finished rendering — it's in your projects now."
                if succeeded else
                (asset.error_message or "The AI video couldn't be finished. Try again.")
            ),
            property_address=address,
            link=f"/studio/media/{asset.id}",
            is_read=False,
        ))
        await db.commit()
        from app.services.agent_email_service import absolute, email_agent
        owner = await db.get(User, asset.owner_id)
        if succeeded:
            email_agent(owner, "STUDIO_MEDIA_VIDEO_READY", "studio_media_ready", "Your AI property video is ready",
                        {"media_type": "AI property video", "property_address": address, "link": absolute(f"/studio/media/{asset.id}")})
        else:
            email_agent(owner, "STUDIO_MEDIA_VIDEO_FAILED", "studio_media_failed", "Your AI property video couldn't be finished",
                        {"media_type": "AI property video", "property_address": address, "reason": asset.error_message,
                         "link": absolute(f"/studio/media/{asset.id}")})
    except Exception as exc:  # noqa: BLE001 - never let a notification failure sink the poll
        logger.warning("studio media video notification failed", extra={"asset_id": asset.id, "error": _exc_text(exc)})
        await db.rollback()


async def _poll_asset_body(db: AsyncSession, user: User, asset: StudioMediaAsset) -> StudioMediaAsset:
    """AI video only. Polls every pending clip; when all are done, downloads + concatenates
    them into the final MP4 and settles the cost. Idempotent once terminal."""
    if asset.kind != "video" or asset.status != "processing":
        return asset
    opts = dict(asset.options or {})
    clips = [dict(c) for c in (opts.get("clips") or [])]
    if not clips:
        return asset

    config = await ai_config_service.get_config(db)
    # The asset's OWN recorded provider, not the current global config - a render/upscale started
    # under one provider must keep polling that same provider even if the config's default has
    # since changed (e.g. an admin flips studio_media_video_provider while this asset is in
    # flight); the job_id only means anything to the provider that issued it.
    provider = video_provider(asset.provider or config["studio_media_video_provider"],
                              asset.provider_model or config["studio_media_video_model"])
    est_cost = config["studio_media_video_reservation_estimate_usd"]

    changed = False
    for c in clips:
        if c["status"] != "processing":
            continue
        try:
            poll = await provider.poll(c["job_id"], config=config)
        except ProviderError as exc:
            logger.warning("studio media clip poll error", extra={"asset_id": asset.id, "error": _exc_text(exc)})
            continue  # transient
        if poll.status == "processing":
            continue
        changed = True
        if poll.status == "failed":
            c["status"] = "failed"
            c["error"] = (poll.error or "clip failed")[:200]
        else:
            c["status"] = "succeeded"
            c["output_url"] = poll.output_url
            # Runway reports its real cost at submit time (already on c["cost_usd"]). MiniMax
            # (and any provider that only knows real cost once the job finishes) reports it here
            # instead - true it up now rather than leaving the submit-time estimate on the books.
            if poll.provider_cost_usd is not None:
                c["cost_usd"] = round(poll.provider_cost_usd, 4)

    if changed:
        opts["clips"] = clips
        asset.options = opts

    if any(c["status"] == "failed" for c in clips):
        asset.status = "failed"
        asset.error_message = "One of the video clips failed. Try again with different photos."
        # The agent sees the line above; the provider's real reason (credit, policy, outage) goes to the admin.
        from app.services.admin_alerts import alert_admin
        await alert_admin("ai_video", "AI video clip failed", "; ".join(str(c.get("error") or "") for c in clips if c["status"] == "failed")[:600],
                          owner_id=asset.owner_id)
        # _settle_video(..., 0.0, ...) already refunds this generation's platform-wide reservation
        # and records a $0 usage event - cost_usd must match that (was left at the pre-flight
        # estimate set at creation time otherwise), or the agent sees a non-zero price tag on a
        # video that was never actually billed against their budget.
        asset.cost_usd = 0.0
        await _settle_video(db, asset, 0.0, est_cost, block_reason="provider_error")
        await db.commit()
        await db.refresh(asset)
        return asset

    if any(c["status"] == "processing" for c in clips):
        if changed:
            await db.commit()
            await db.refresh(asset)
        return asset

    # Runway/Magnific still upscales the whole stitched video as one job (see
    # _finish_video_runway_legacy) - fal/Hailuo (the default pipeline) upscales each clip
    # individually before stitching instead, see _finish_video_per_clip's docstring for why.
    if asset.provider == "runway":
        return await _finish_video_runway_legacy(db, asset, opts, clips, config, est_cost)
    return await _finish_video_per_clip(db, asset, opts, clips, config, est_cost)


async def _finish_video_runway_legacy(db, asset, opts: dict, clips: list[dict], config: dict, est_cost: float) -> StudioMediaAsset:
    """Runway/Magnific only. Every render clip has already succeeded (see _poll_asset_body) -
    stitch them, then upscale the WHOLE stitched video as one Magnific job (Magnific's creative
    upscale doesn't have a clean equivalent to fal's per-clip flow this file uses for Hailuo -
    keeping Runway's existing, working behavior untouched rather than risking it on an unverified
    rewrite for a provider that isn't the default pipeline)."""
    # --- AI upscale, phase 2: a base was assembled + submitted, poll that task now ---
    upscale_job = opts.get("upscale_job")
    if upscale_job and not asset.result_url:
        try:
            mprov = video_provider(asset.provider, asset.provider_model or config["studio_media_video_model"])
            mpoll = await mprov.poll(upscale_job, config=config)
        except ProviderError as exc:
            logger.warning("ai video upscale poll (transient)", extra={"asset_id": asset.id, "error": _exc_text(exc)})
            return asset
        if mpoll.status == "processing":
            return asset
        if mpoll.status == "succeeded" and mpoll.output_url:
            # Pure I/O (fetch fal's finished file, re-upload to our own S3) - a single transient
            # blip here used to permanently downgrade the video to the ffmpeg fallback even though
            # the expensive AI upscale had already succeeded. Retry a couple times first; only
            # fall back once this keeps failing.
            r = None
            store_exc: Optional[Exception] = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=15.0), follow_redirects=True) as client:
                        r = await client.get(mpoll.output_url)
                        r.raise_for_status()
                    result_url = s3_service.upload_bytes(r.content, _S3_PREFIX, ".mp4", "video/mp4")
                    store_exc = None
                    break
                except Exception as exc:  # noqa: BLE001
                    store_exc = exc
                    if attempt < 2:
                        logger.warning("ai upscale result store failed, retrying", extra={
                            "asset_id": asset.id, "attempt": attempt + 1, "error": _exc_text(exc),
                        })
                        await asyncio.sleep(2.0 * (attempt + 1))
            if store_exc is not None:
                logger.warning("ai upscale result store failed -> ffmpeg fallback", extra={"asset_id": asset.id, "error": _exc_text(store_exc)})
                return await _finish_video_from_base(db, asset, opts, config, est_cost)
            render_cost = round(sum(float(c.get("cost_usd") or 0.0) for c in clips), 4)
            # The provider's own reported cost (from poll()'s real usage/duration once the job
            # finished) is authoritative - there's no reliable estimate fallback worth guessing at
            # here, unlike the old Magnific per-frame formula, since fal's real cost is only known
            # after completion. Missing just means it wasn't billed as a metered add-on.
            fallback_upscale_cost = (
                float(opts.get("duration_seconds") or 0.0) * _upscale_cost_estimate_per_second_usd(config)
            )
            upscale_cost = float(opts.get("upscale_cost_usd") or mpoll.provider_cost_usd or fallback_upscale_cost or 0.0)
            cost = round(render_cost + upscale_cost, 4) or (asset.cost_usd or est_cost)
            asset.status = "succeeded"
            asset.result_url = result_url
            asset.cost_usd = cost
            final_size = _probe_video_size_bytes(r.content)
            opts["size_bytes"] = len(r.content)
            opts["upscaled_by"] = asset.provider
            opts["upscale_completed"] = True
            opts["upscale_cost_usd"] = round(upscale_cost, 4)
            opts["upscale_vendor"] = config.get("studio_media_video_upscale_vendor")
            opts["upscale_resolution"] = config.get("studio_media_video_upscale_resolution")
            if final_size:
                opts["final_width"], opts["final_height"] = final_size
            preview_info = _store_preview_frame(r.content, opts)
            asset.options = opts
            await _settle_video(db, asset, cost, est_cost, block_reason=None)
            await db.commit()
            await db.refresh(asset)
            await storage_ledger_service.record(db, asset.owner_id, "ai_media_video", result_url, len(r.content))
            if preview_info:
                await storage_ledger_service.record(db, asset.owner_id, "ai_media_video_preview", preview_info[0], preview_info[1])
            return asset
        # fal reported the upscale job itself as failed (not a network blip - a real terminal
        # ERROR status). That can still be a transient fal-side hiccup, and the upscale is cheap
        # (~$0.86/video) relative to the quality difference, so resubmit once against the same
        # already-uploaded base before giving up to the local ffmpeg fallback.
        retry_count = int(opts.get("upscale_retry_count") or 0)
        base_url = opts.get("upscale_base_url")
        if retry_count < 1 and base_url:
            logger.warning("ai video upscale failed, resubmitting once", extra={"asset_id": asset.id, "error": mpoll.error})
            try:
                sub = await mprov.submit_video_upscale(
                    base_url, config=config, duration_seconds=float(opts.get("duration_seconds") or 0),
                )
                opts["upscale_job"] = sub.job_id
                opts["upscale_retry_count"] = retry_count + 1
                if sub.estimated_cost_usd is not None:
                    opts["upscale_cost_usd"] = sub.estimated_cost_usd
                asset.options = opts
                await db.commit()
                await db.refresh(asset)
                return asset  # stays "processing" - next poll checks the resubmitted job
            except Exception as exc:  # noqa: BLE001
                logger.warning("ai video upscale resubmit failed -> ffmpeg fallback", extra={"asset_id": asset.id, "error": _exc_text(exc)})
        else:
            logger.warning("ai video upscale failed -> ffmpeg fallback", extra={"asset_id": asset.id, "error": mpoll.error})
        return await _finish_video_from_base(db, asset, opts, config, est_cost)

    # --- All render clips ready ---
    tmpdir = tempfile.mkdtemp(prefix="reel_")
    try:
        stitched = await _download_and_concat(clips, tmpdir)

        # AI upscale on: assemble a compact 1080p base, upload it, hand it to the SAME provider
        # that rendered the clips (its own submit_video_upscale() - fal's ByteDance step or
        # Runway's Magnific). Phase 2 above finishes it on the next poll. Any failure here falls
        # through to the free local ffmpeg pass below. No cost-affordability gate anymore - that
        # only existed because Runway/Magnific's 4K creative pass could run $9-13 on a full reel;
        # the default fal pipeline's ByteDance upscale is cheap/predictable (~$0.86 for a normal
        # ~30s reel) and gated by the standard budget-reservation check like everything else
        # instead. MiniMax-H3 already renders natively at 2K, so there's nothing to upscale there.
        can_upscale = (
            config.get("studio_media_video_upscale_enabled", False)
            and asset.provider in ("runway", "fal")
            and not opts.get("upscale_job")
            # A Spotlight header loop is shown at <=1600px - the 1080p render is already enough,
            # and Runway's upscale alone can cost several times the clip (and take an hour).
            and not opts.get("skip_ai_upscale")
        )
        if can_upscale:
            gate_secs = float((asset.options or {}).get("duration_seconds") or 0)
            gate_max_secs = int(config.get("studio_media_video_upscale_max_seconds", 30))
            if gate_secs and gate_secs > gate_max_secs:
                # A real technical ceiling (Runway's video_upscale rejects long input), not a
                # cost heuristic - still worth skipping the call rather than let it fail outright.
                can_upscale = False
                logger.info("ai video upscale skipped (over duration ceiling) -> local ffmpeg 4K enhance",
                            extra={"asset_id": asset.id, "duration_s": gate_secs, "max_s": gate_max_secs})
        if can_upscale:
            try:
                bh = int(config.get("studio_media_video_upscale_base_height", 1080) or 1080)
                bw = (bh * 16 // 9); bw -= bw % 2
                base_bytes, final_secs = _assemble_final(stitched, tmpdir, config, asset, out_wh=(bw, bh), enhance=False)
                base_url = s3_service.upload_bytes(base_bytes, _S3_PREFIX, ".mp4", "video/mp4")
                mprov = video_provider(asset.provider, asset.provider_model or config["studio_media_video_model"])
                # Pass the whole config dict rather than individually-named kwargs - a prior
                # version passed `resolution=`, a name fal_video.py's submit_video_upscale never
                # actually read, so it silently landed in **kwargs and every automatic upscale ran
                # at the hardcoded 4k default regardless of studio_media_video_upscale_resolution.
                # duration_seconds is needed for Topaz's cost calc (its response has no duration
                # field) - harmless to always pass, ByteDance's branch just ignores it.
                # One retry before giving up to the ffmpeg fallback - fal_video.py's own
                # _request_with_retry already covers transient HTTP failures inside a single call,
                # but a submit that still fails once is worth trying again before permanently
                # downgrading quality for this video.
                sub = None
                submit_exc: Optional[Exception] = None
                for attempt in range(2):
                    try:
                        sub = await mprov.submit_video_upscale(base_url, config=config, duration_seconds=final_secs)
                        submit_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        submit_exc = exc
                        if attempt == 0:
                            logger.warning("ai upscale submit failed, retrying once", extra={"asset_id": asset.id, "error": _exc_text(exc)})
                            await asyncio.sleep(2.0)
                if submit_exc is not None:
                    raise submit_exc
                opts["upscale_job"] = sub.job_id
                opts["upscale_base_url"] = base_url
                opts["upscale_cost_usd"] = sub.estimated_cost_usd
                opts["upscale_requested_resolution"] = config.get("studio_media_video_upscale_resolution")
                opts["upscale_vendor"] = config.get("studio_media_video_upscale_vendor")
                if final_secs:
                    opts["duration_seconds"] = round(final_secs, 1)
                asset.options = opts
                await db.commit()
                await db.refresh(asset)
                return asset  # stays "processing" - phase 2 polls the upscale job next tick
            except Exception as exc:  # noqa: BLE001 - fall back to the local enhance pass
                logger.warning("ai upscale submit failed -> local ffmpeg 4K enhance", extra={"asset_id": asset.id, "error": _exc_text(exc)})

        final_bytes, final_secs = _assemble_final(
            stitched, tmpdir, config, asset, out_wh=None,
            enhance=bool(config.get("studio_media_video_enhance", True)),
        )
        result_url = s3_service.upload_bytes(final_bytes, _S3_PREFIX, ".mp4", "video/mp4")
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video assemble failed", extra={"asset_id": asset.id, "error": _exc_text(exc)})
        asset.status = "failed"
        asset.error_message = "The finished video couldn't be assembled. Try again."
        from app.services.admin_alerts import alert_admin
        await alert_admin("ai_video", "AI video couldn't be assembled", _exc_text(exc)[:600], owner_id=asset.owner_id)
        asset.cost_usd = 0.0  # match the $0 refund/usage-event settled below, not the stale pre-flight estimate
        await _settle_video(db, asset, 0.0, est_cost, block_reason="studio_media_store_failed")
        await db.commit()
        await db.refresh(asset)
        return asset
    finally:
        _rmtree_quiet(tmpdir)

    cost = round(sum(float(c.get("cost_usd") or 0.0) for c in clips), 4) or (asset.cost_usd or est_cost)
    asset.status = "succeeded"
    asset.result_url = result_url
    asset.cost_usd = cost
    final_size = _probe_video_size_bytes(final_bytes)
    opts["size_bytes"] = len(final_bytes)
    opts["upscaled_by"] = "ffmpeg"
    opts["upscale_completed"] = False
    opts["upscale_vendor"] = opts.get("upscale_vendor") or "ffmpeg"
    if final_size:
        opts["final_width"], opts["final_height"] = final_size
    if final_secs:
        opts["duration_seconds"] = round(final_secs, 1)  # actual stored length after any trim
    preview_info = _store_preview_frame(final_bytes, opts)
    asset.options = opts
    await _settle_video(db, asset, cost, est_cost, block_reason=None)
    await db.commit()
    await db.refresh(asset)
    await storage_ledger_service.record(db, asset.owner_id, "ai_media_video", result_url, len(final_bytes))
    if preview_info:
        await storage_ledger_service.record(db, asset.owner_id, "ai_media_video_preview", preview_info[0], preview_info[1])
    return asset


async def _finish_video_per_clip(db, asset, opts: dict, clips: list[dict], config: dict, est_cost: float) -> StudioMediaAsset:
    """fal/Hailuo (the default pipeline) and MiniMax. Every render clip has already succeeded (see
    _poll_asset_body) - upscale each clip INDIVIDUALLY before stitching, rather than stitching
    first and upscaling the whole multi-scene video as one job (the old approach, still used by
    _finish_video_runway_legacy for Runway).

    Why this changed (2026-09-05): the whole-video upscale fed ByteDance a hard scene cut baked
    into the middle of one continuous stream. ByteDance's temporal-smoothing model would
    occasionally blend/ghost across that cut - in the worst observed case, fabricating a visible
    transition into a house that was never actually photographed. Upscaling each clip alone keeps
    every scene temporally isolated, so there's nothing for the upscaler to blend across a cut.

    Also: each clip's own raw Hailuo `output_url` (already a clean, publicly-fetchable fal-hosted
    file) is handed to ByteDance directly - no local re-encode/"packaging" step first. Hailuo's
    native output doesn't need any pre-resize to be a valid ByteDance input, so that intermediate
    pass was pure unnecessary quality loss (and, on the old whole-video path, was ALSO being
    applied for fal despite that path's own comment claiming fal never needed it - see the
    removed studio_media_video_upscale_base_height usage below, now Runway-only).

    Fallback is per-clip too: if one clip's ByteDance job fails (after one resubmit), that ONE
    clip just keeps its clean native Hailuo 1080p output instead of a locally re-processed
    version - never a reason to touch, let alone regenerate, any other clip."""
    mprov = video_provider(asset.provider, asset.provider_model or config["studio_media_video_model"])
    per_clip_secs = float(config.get("studio_media_video_seconds_per_clip", 5) or 5)
    can_upscale = (
        config.get("studio_media_video_upscale_enabled", False)
        and asset.provider in ("runway", "fal")  # minimax already renders natively at 2K
    )

    changed = False
    for c in clips:
        if c.get("final_url"):
            continue
        if not can_upscale:
            c["final_url"] = c["output_url"]
            c["upscaled"] = False
            changed = True
            continue
        if not c.get("upscale_job"):
            try:
                sub = await mprov.submit_video_upscale(c["output_url"], config=config, duration_seconds=per_clip_secs)
                c["upscale_job"] = sub.job_id
                if sub.estimated_cost_usd is not None:
                    c["upscale_cost_usd"] = sub.estimated_cost_usd
            except Exception as exc:  # noqa: BLE001 - keep this clip's clean native Hailuo output
                logger.warning("per-clip upscale submit failed -> keeping native Hailuo clip",
                               extra={"asset_id": asset.id, "error": _exc_text(exc)})
                c["final_url"] = c["output_url"]
                c["upscaled"] = False
                c["upscale_cost_usd"] = 0.0  # never actually ran - don't bill a stale estimate
            changed = True
            continue
        try:
            mpoll = await mprov.poll(c["upscale_job"], config=config)
        except ProviderError as exc:
            logger.warning("per-clip upscale poll (transient)", extra={"asset_id": asset.id, "error": _exc_text(exc)})
            continue
        if mpoll.status == "processing":
            continue
        changed = True
        if mpoll.status == "succeeded" and mpoll.output_url:
            c["final_url"] = mpoll.output_url
            c["upscaled"] = True
            if mpoll.provider_cost_usd is not None:
                c["upscale_cost_usd"] = round(mpoll.provider_cost_usd, 4)
            continue
        # This clip's ByteDance job genuinely failed - cheap enough (~$0.01-0.02/clip) to
        # resubmit once before settling for the clip's clean native Hailuo output.
        retry_count = int(c.get("upscale_retry_count") or 0)
        if retry_count < 1:
            try:
                sub = await mprov.submit_video_upscale(c["output_url"], config=config, duration_seconds=per_clip_secs)
                c["upscale_job"] = sub.job_id
                c["upscale_retry_count"] = retry_count + 1
                if sub.estimated_cost_usd is not None:
                    c["upscale_cost_usd"] = sub.estimated_cost_usd
                continue  # no final_url yet - still in progress
            except Exception as exc:  # noqa: BLE001
                logger.warning("per-clip upscale resubmit failed -> keeping native Hailuo clip",
                               extra={"asset_id": asset.id, "error": _exc_text(exc)})
        else:
            logger.warning("per-clip upscale failed -> keeping native Hailuo clip",
                           extra={"asset_id": asset.id, "error": mpoll.error})
        c["final_url"] = c["output_url"]
        c["upscaled"] = False
        c["upscale_cost_usd"] = 0.0  # the job that would have earned this estimate never completed

    if changed:
        opts["clips"] = clips
        asset.options = opts

    if any(not c.get("final_url") for c in clips):
        await db.commit()
        await db.refresh(asset)
        return asset  # stays "processing" - one or more clips still upscaling

    # --- every clip finalized (ByteDance 2K, or its own clean native Hailuo clip) - stitch now ---
    tmpdir = tempfile.mkdtemp(prefix="reel_")
    try:
        stitched = await _download_and_concat_normalized(clips, tmpdir)
        # Each clip already got its own quality pass (or a deliberate, honest fallback) above -
        # running the local denoise/sharpen enhance pass again here would just be redundant lossy
        # re-processing of footage that's already as good as this pipeline can make it. Only run
        # it when upscaling wasn't attempted at all for this asset (minimax, or upscaling turned
        # off), matching what the old whole-video path did for those same two cases.
        final_bytes, final_secs = _assemble_final(
            stitched, tmpdir, config, asset, out_wh=None,
            enhance=(not can_upscale) and bool(config.get("studio_media_video_enhance", True)),
        )
        result_url = s3_service.upload_bytes(final_bytes, _S3_PREFIX, ".mp4", "video/mp4")
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio media video assemble failed", extra={"asset_id": asset.id, "error": _exc_text(exc)})
        asset.status = "failed"
        asset.error_message = "The finished video couldn't be assembled. Try again."
        from app.services.admin_alerts import alert_admin
        await alert_admin("ai_video", "AI video couldn't be assembled", _exc_text(exc)[:600], owner_id=asset.owner_id)
        asset.cost_usd = 0.0
        await _settle_video(db, asset, 0.0, est_cost, block_reason="studio_media_store_failed")
        await db.commit()
        await db.refresh(asset)
        return asset
    finally:
        _rmtree_quiet(tmpdir)

    render_cost = round(sum(float(c.get("cost_usd") or 0.0) for c in clips), 4)
    upscale_cost = round(sum(float(c.get("upscale_cost_usd") or 0.0) for c in clips), 4)
    cost = round(render_cost + upscale_cost, 4) or (asset.cost_usd or est_cost)
    asset.status = "succeeded"
    asset.result_url = result_url
    asset.cost_usd = cost
    final_size = _probe_video_size_bytes(final_bytes)
    opts["size_bytes"] = len(final_bytes)
    all_upscaled = all(c.get("upscaled") for c in clips)
    any_upscaled = any(c.get("upscaled") for c in clips)

    # TEMPORARY testing aid (studio_media_video_debug_store_intermediates) - also stitch each
    # clip's raw, pre-upscale native Hailuo output into a second comparison video, stored
    # alongside the real result so the Results tab can play both back to back. Zero AI cost (pure
    # local ffmpeg) and never allowed to block or fail the real video if it errors.
    if config.get("studio_media_video_debug_store_intermediates", False) and any_upscaled:
        preup_tmpdir = tempfile.mkdtemp(prefix="reel_preupscale_")
        try:
            preup_clips = [{"final_url": c["output_url"]} for c in clips]
            preup_stitched = await _download_and_concat_normalized(preup_clips, preup_tmpdir)
            with open(preup_stitched, "rb") as f:
                preup_bytes = f.read()
            opts["preupscale_result_url"] = s3_service.upload_bytes(preup_bytes, _S3_PREFIX, ".mp4", "video/mp4")
            opts["preupscale_size_bytes"] = len(preup_bytes)
        except Exception as exc:  # noqa: BLE001 - comparison aid only, never blocks the real result
            logger.warning("pre-upscale comparison video failed (non-fatal)", extra={"asset_id": asset.id, "error": _exc_text(exc)})
        finally:
            _rmtree_quiet(preup_tmpdir)

    opts["upscaled_by"] = asset.provider if all_upscaled else ("mixed" if any_upscaled else "none")
    opts["upscale_completed"] = all_upscaled
    opts["upscale_cost_usd"] = upscale_cost
    opts["upscale_vendor"] = config.get("studio_media_video_upscale_vendor")
    opts["upscale_resolution"] = config.get("studio_media_video_upscale_resolution")
    if final_size:
        opts["final_width"], opts["final_height"] = final_size
    if final_secs:
        opts["duration_seconds"] = round(final_secs, 1)
    preview_info = _store_preview_frame(final_bytes, opts)
    asset.options = opts
    await _settle_video(db, asset, cost, est_cost, block_reason=None)
    await db.commit()
    await db.refresh(asset)
    await storage_ledger_service.record(db, asset.owner_id, "ai_media_video", result_url, len(final_bytes))
    if preview_info:
        await storage_ledger_service.record(db, asset.owner_id, "ai_media_video_preview", preview_info[0], preview_info[1])
    return asset


async def _download_and_concat_normalized(clips: list[dict], tmpdir: str) -> str:
    """Like _download_and_concat, but for _finish_video_per_clip, where clips can legitimately
    come back at different resolutions - most at ByteDance's 2K, but a rare clip that kept its
    fallback native-Hailuo output stays at Hailuo's own (lower) native size. ffmpeg's concat
    demuxer requires every input to already share the same resolution, so any clip that doesn't
    match the majority size gets one light lanczos scale pass to match before concatenation -
    the ONLY per-clip processing this pipeline applies outside of the ByteDance upscale itself,
    and only for the clips that actually need it."""
    paths: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
        for i, c in enumerate(clips):
            r = await client.get(c["final_url"])
            r.raise_for_status()
            p = os.path.join(tmpdir, f"c{i:02d}.mp4")
            with open(p, "wb") as f:
                f.write(r.content)
            paths.append(p)
    stitched = os.path.join(tmpdir, "stitched.mp4")
    if len(paths) == 1:
        os.replace(paths[0], stitched)
        return stitched
    sizes = [_probe_video_size(p) for p in paths]
    known = [s for s in sizes if s]
    if known:
        target = Counter(known).most_common(1)[0][0]
        for i, s in enumerate(sizes):
            if s and s != target:
                fixed = os.path.join(tmpdir, f"c{i:02d}_fit.mp4")
                vf = f"scale={target[0]}:{target[1]}:flags=lanczos+accurate_rnd+full_chroma_int,setsar=1"
                try:
                    _run_vf(paths[i], fixed, vf, preset="veryfast", crf=20)
                    paths[i] = fixed
                except Exception as exc:  # noqa: BLE001 - concat below may still fail; logged either way
                    logger.warning("per-clip resolution normalize failed", extra={"error": _exc_text(exc)})
    _concat_mp4(paths, stitched)
    return stitched


async def _finish_video_from_base(db, asset, opts: dict, config: dict, est_cost: float) -> StudioMediaAsset:
    """Fallback when the AI upscale fails: take the compact base we already uploaded and run the
    local ffmpeg 4K enhance pass on it instead. Last resort serves the base as-is."""
    base_url = opts.get("upscale_base_url")
    clips = opts.get("clips") or []
    render_cost = round(sum(float(c.get("cost_usd") or 0.0) for c in clips), 4)
    tmpdir = tempfile.mkdtemp(prefix="upscalefb_")
    try:
        if not base_url:
            raise RuntimeError("no upscale base url on asset")
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
            r = await client.get(base_url)
            r.raise_for_status()
        src = os.path.join(tmpdir, "base.mp4")
        with open(src, "wb") as f:
            f.write(r.content)
        final_bytes, _secs = _assemble_final(src, tmpdir, config, asset, out_wh=None, enhance=True)
        result_url = s3_service.upload_bytes(final_bytes, _S3_PREFIX, ".mp4", "video/mp4")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai upscale fallback assemble failed", extra={"asset_id": asset.id, "error": _exc_text(exc)})
        if base_url:
            asset.status = "succeeded"
            asset.result_url = base_url
            asset.cost_usd = render_cost or (asset.cost_usd or est_cost)
            opts["upscaled_by"] = "none"
            asset.options = opts
            await _settle_video(db, asset, asset.cost_usd, est_cost, block_reason=None)
            await db.commit()
            await db.refresh(asset)
            return asset
        asset.status = "failed"
        asset.error_message = "The finished video couldn't be assembled. Try again."
        from app.services.admin_alerts import alert_admin
        await alert_admin("ai_video", "AI video couldn't be assembled", _exc_text(exc)[:600], owner_id=asset.owner_id)
        asset.cost_usd = 0.0  # match the $0 refund/usage-event settled below, not the stale pre-flight estimate
        await _settle_video(db, asset, 0.0, est_cost, block_reason="studio_media_store_failed")
        await db.commit()
        await db.refresh(asset)
        return asset
    finally:
        _rmtree_quiet(tmpdir)
    cost = render_cost or (asset.cost_usd or est_cost)
    asset.status = "succeeded"
    asset.result_url = result_url
    asset.cost_usd = cost
    final_size = _probe_video_size_bytes(final_bytes)
    opts["size_bytes"] = len(final_bytes)
    opts["upscaled_by"] = "ffmpeg"
    opts["upscale_completed"] = False
    opts["upscale_vendor"] = opts.get("upscale_vendor") or "ffmpeg"
    if final_size:
        opts["final_width"], opts["final_height"] = final_size
    preview_info = _store_preview_frame(final_bytes, opts)
    asset.options = opts
    await _settle_video(db, asset, cost, est_cost, block_reason=None)
    await db.commit()
    await db.refresh(asset)
    await storage_ledger_service.record(db, asset.owner_id, "ai_media_video", result_url, len(final_bytes))
    if preview_info:
        await storage_ledger_service.record(db, asset.owner_id, "ai_media_video_preview", preview_info[0], preview_info[1])
    return asset


async def _magnific_image_upscale(
    image_url: str, config: dict, *, asset_id: str = "", key_prefix: str = "studio_media_image_upscale",
) -> Optional[bytes]:
    """Submit `image_url` to Runway/Magnific Precision v2 and poll inline up to the configured
    budget. Returns the upscaled PNG bytes, or None if it didn't finish in time / failed.
    `key_prefix` picks which family of config keys to read - the opt-in still-image editor
    (studio_media_image_upscale_*, the default) and the AI Video per-photo enhancement step
    (studio_media_video_photo_enhance_*, see _enhance_photo_for_video) share this same submit+poll
    logic but are tuned independently, so each gets its own settings under its own prefix."""
    prov = video_provider(
        config.get(f"{key_prefix}_provider", "runway"),
        config.get(f"{key_prefix}_model", "magnific_precision_upscaler_v2"),
    )
    sub = await prov.submit_image_upscale(
        image_url,
        model=config.get(f"{key_prefix}_model", "magnific_precision_upscaler_v2"),
        scale_factor=float(config.get(f"{key_prefix}_scale_factor", 2) or 2),
        ultra_detail=int(config.get(f"{key_prefix}_ultra_detail", 30) or 30),
        sharpen=int(config.get(f"{key_prefix}_sharpen", 7) or 7),
        smart_grain=int(config.get(f"{key_prefix}_smart_grain", 7) or 7),
        flavor=config.get(f"{key_prefix}_flavor", "photo"),
    )
    deadline = time.monotonic() + float(config.get(f"{key_prefix}_max_wait_s", 150) or 150)
    out_url: Optional[str] = None
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            p = await prov.poll(sub.job_id)
        except ProviderError as exc:
            logger.warning("magnific image poll (transient)", extra={"asset": asset_id, "error": _exc_text(exc)})
            continue
        if p.status == "succeeded" and p.output_url:
            out_url = p.output_url
            break
        if p.status == "failed":
            logger.warning("magnific image upscale failed", extra={"asset": asset_id, "error": p.error})
            return None
    if not out_url:
        return None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
        r = await client.get(out_url)
        r.raise_for_status()
    return r.content


def _rmtree_quiet(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


async def _settle_video(
    db: AsyncSession, asset: StudioMediaAsset, actual_cost: float, reserved_estimate: float,
    *, block_reason: Optional[str],
) -> None:
    from app.services.usage_budget_service import Reservation, usd_to_micros, _period_keys, reconcile as _reconcile
    day_key, month_key = _period_keys()
    pseudo = Reservation(
        buckets=["studio_media_video_global", "platform_emergency"],
        cost_micros=usd_to_micros(reserved_estimate), day_key=day_key, month_key=month_key,
    )
    await _reconcile(db, pseudo, actual_cost)
    await record_usage_event(
        db, asset.owner_id, None, asset.provider_model, None, None, actual_cost, 0,
        block_reason is not None, block_reason, event_type="studio_media_video",
    )


# ------------------------------------------------------------------------------- queries

async def get_owned(db: AsyncSession, owner_id: str, asset_id: str) -> StudioMediaAsset:
    res = await db.execute(select(StudioMediaAsset).where(
        StudioMediaAsset.id == asset_id, StudioMediaAsset.owner_id == owner_id))
    asset = res.scalar_one_or_none()
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return asset


async def list_assets(db: AsyncSession, owner_id: str, *, group_id: Optional[str], limit: int,
                      kind: Optional[str] = None, exclude_kind: Optional[str] = None) -> list[StudioMediaAsset]:
    q = select(StudioMediaAsset).where(StudioMediaAsset.owner_id == owner_id)
    if group_id:
        q = q.where(StudioMediaAsset.group_id == group_id)
    if kind:
        q = q.where(StudioMediaAsset.kind == kind)
    if exclude_kind:
        q = q.where(StudioMediaAsset.kind != exclude_kind)
    q = q.order_by(StudioMediaAsset.created_at.desc()).limit(limit)
    return list((await db.execute(q)).scalars().all())


async def upload_photo(db: AsyncSession, user: User, data: bytes, content_type: str) -> StudioMediaAsset:
    """Store an agent-uploaded photo (JPG, PNG or WebP - PNG/WebP are saved as a high-quality JPG,
    the format every AI Media step expects) as a tracked `kind='upload'` asset: it lands in S3,
    counts toward the per-agent storage cap, and shows in the media page's personal upload
    library. Enforces the storage cap up front."""
    config = await ai_config_service.get_config(db)
    await _enforce_storage_cap(db, user, config)
    if len(data) > _SOURCE_MAX_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Image must be 12MB or smaller.")
    try:
        from app.utils.image_validation import photo_to_jpeg
        data = photo_to_jpeg(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Please upload a JPG, PNG or WebP photo.") from exc
    if len(data) > _SOURCE_MAX_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That photo is over 12MB once converted - try a smaller one.")
    try:
        url = s3_service.upload_bytes(data, _S3_PREFIX, ".jpg", "image/jpeg")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't save that photo.") from exc
    asset = StudioMediaAsset(
        owner_id=user.id, group_id=str(uuid.uuid4()), parent_asset_id=None,
        kind="upload", action="upload", status="succeeded",
        source_photo_url=url, source_kind="upload", linked_property=None,
        options={"size_bytes": len(data)},
        provider="upload", provider_model=None,
        result_url=url, prompt=None, prompt_version=PROMPT_VERSION, cost_usd=0.0,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    await storage_ledger_service.record(db, user.id, "ai_media_upload", url, len(data))
    return asset


async def delete_asset(db: AsyncSession, owner_id: str, asset_id: str) -> None:
    asset = await get_owned(db, owner_id, asset_id)
    result_url = asset.result_url
    preview_url = (asset.options or {}).get("preview_frame_url")
    await db.delete(asset)
    await db.commit()
    s3_service.delete_by_url(result_url)  # never touches source_photo_url
    s3_service.delete_by_url(preview_url)
    await storage_ledger_service.release(db, result_url)
    await storage_ledger_service.release(db, preview_url)
