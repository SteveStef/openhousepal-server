import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import AIUsageEvent, User
from app.schemas.studio_media import (
    StudioMediaAssetListResponse,
    StudioMediaAssetResponse,
    StudioMediaGenerateRequest,
    StudioMediaRegenerateRequest,
    StudioMediaRenameRequest,
    StudioMediaStagingTagRequest,
    StudioMediaOptions,
    StudioMediaUsageResponse,
    StudioVideoTestGridRequest,
    StudioVideoTestGridResponse,
    StudioVideoTestGridVariant,
    StudioVideoVariantOptions,
    VideoExportRequest,
    VideoOverlayUpdateRequest,
)
from app.services import studio_media_service
from app.services.ai import ai_rate_limit_service
from app.services.ai.ai_config_service import ai_config_service
from app.utils.auth import require_admin_user, require_studio_media_access

_MEDIA_EVENT_TYPES = ("studio_media_image", "studio_media_video")

router = APIRouter(prefix="/studio/media-lab", tags=["studio-media"])
logger = logging.getLogger(__name__)


@router.post("/generate", response_model=StudioMediaAssetResponse, status_code=status.HTTP_201_CREATED)
async def generate(
    payload: StudioMediaGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    return await studio_media_service.create_asset(db, current_user, payload)


@router.post("/video-test-grid", response_model=StudioVideoTestGridResponse, status_code=status.HTTP_201_CREATED)
async def video_test_grid(
    payload: StudioVideoTestGridRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Submit a controlled one-photo foliage/detail experiment grid. This is intentionally an
    opt-in route: normal /generate jobs do not carry video_variant and keep production behavior.
    Admin only - the caller picks the model tiers and the per-clip cost cap (no agent screen uses it)."""
    config = await ai_config_service.get_config(db)
    group_id = str(uuid.uuid4())
    variants: list[StudioVideoTestGridVariant] = []
    seen: set[tuple[str, str, str]] = set()

    for model_tier in payload.model_tiers:
        for motion in payload.motions:
            for prompt_idx, prompt_suffix in enumerate(payload.prompt_suffixes):
                suffix = (prompt_suffix or "").strip()
                key = (model_tier, motion, suffix)
                if key in seen:
                    continue
                seen.add(key)
                prompt_label = "baseline" if not suffix else f"foliage_prompt_{prompt_idx}"
                label = f"{model_tier}-{motion}-{prompt_label}"
                variant_opts = StudioVideoVariantOptions(
                    label=label,
                    model_tier=model_tier,
                    motion=motion,
                    prompt_suffix=suffix or None,
                    max_cost_per_clip_usd=payload.max_cost_per_clip_usd,
                )
                projection = studio_media_service.estimate_video_generation_cost_usd(
                    config, photo_count=1, variant=variant_opts,
                )
                over_budget = projection["per_clip_usd"] > payload.max_cost_per_clip_usd + 0.0001
                if over_budget and not payload.include_over_budget:
                    variants.append(StudioVideoTestGridVariant(
                        label=label,
                        model_tier=model_tier,
                        motion=motion,
                        prompt_suffix=suffix,
                        projected_cost_usd=projection["per_clip_usd"],
                        projected_over_budget=True,
                        skipped=True,
                        asset=None,
                    ))
                    continue

                asset = await studio_media_service.create_asset(
                    db,
                    current_user,
                    StudioMediaGenerateRequest(
                        action="animate",
                        source_photo_url=payload.source_photo_url,
                        source_kind=payload.source_kind,
                        linked_property=payload.linked_property,
                        photos=[payload.source_photo_url],
                        options=StudioMediaOptions(video_variant=variant_opts),
                    ),
                    group_id=group_id,
                )
                variants.append(StudioVideoTestGridVariant(
                    label=label,
                    model_tier=model_tier,
                    motion=motion,
                    prompt_suffix=suffix,
                    projected_cost_usd=projection["per_clip_usd"],
                    projected_over_budget=over_budget,
                    skipped=False,
                    asset=StudioMediaAssetResponse.model_validate(asset),
                ))

    return StudioVideoTestGridResponse(
        group_id=group_id,
        max_cost_per_clip_usd=payload.max_cost_per_clip_usd,
        variants=variants,
    )


@router.get("/usage", response_model=StudioMediaUsageResponse)
async def usage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def totals(since):
        row = (await db.execute(
            select(
                func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0.0),
                func.count(AIUsageEvent.id),
            ).where(
                AIUsageEvent.owner_id == current_user.id,
                AIUsageEvent.event_type.in_(_MEDIA_EVENT_TYPES),
                AIUsageEvent.blocked.is_(False),
                AIUsageEvent.created_at >= since,
            )
        )).one()
        return float(row[0] or 0.0), int(row[1] or 0)

    day_cost, day_count = await totals(start_of_day)
    month_cost, month_count = await totals(start_of_month)

    config = await ai_config_service.get_config(db)
    snap = await ai_rate_limit_service.monthly_ai_budget_snapshot(
        db, current_user.id, config, current_user.plan_tier
    )
    soft_capped = await ai_rate_limit_service.studio_soft_capped(
        db, current_user.id, config, current_user.plan_tier
    )
    storage_used = await studio_media_service.storage_used_bytes(db, current_user.id)
    storage_cap = studio_media_service._storage_cap_bytes(config, current_user.plan_tier)
    per_clip_secs = int(config.get("studio_media_video_seconds_per_clip", 5))
    return StudioMediaUsageResponse(
        today_cost_usd=round(day_cost, 4), month_cost_usd=round(month_cost, 4),
        today_count=day_count, month_count=month_count,
        month_ai_used_usd=snap["used_usd"], month_ai_budget_usd=snap["limit_usd"],
        month_ai_percent=snap["percent"],
        video_est_usd=config.get("studio_media_video_reservation_estimate_usd"),
        image_est_usd=config.get("studio_media_image_reservation_estimate_usd"),
        video_clip_price_usd=studio_media_service._clip_cost_estimate_usd(config, per_clip_secs),
        video_upscale_price_per_second_usd=studio_media_service._upscale_cost_estimate_per_second_usd(config),
        video_seconds_per_clip=per_clip_secs,
        video_photo_enhance_price_usd=(
            float(config.get("studio_media_video_photo_enhance_cost_usd", 0.25) or 0.0)
            if config.get("studio_media_video_photo_enhance_enabled", True) else 0.0
        ),
        studio_soft_capped=soft_capped,
        storage_used_bytes=storage_used, storage_cap_bytes=storage_cap,
    )


@router.post("/upload", response_model=StudioMediaAssetResponse, status_code=status.HTTP_201_CREATED)
async def upload_photo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    """Add a JPG, PNG or WebP photo to the agent's personal upload library. Stored in S3, tracked as a
    kind='upload' asset so it counts toward their storage cap."""
    data = await file.read()
    return await studio_media_service.upload_photo(db, current_user, data, file.content_type or "image/jpeg")


@router.get("/assets", response_model=StudioMediaAssetListResponse)
async def list_assets(
    group_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    kind: str | None = Query(None, description="'upload' for the personal upload library; omit for generated projects"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    assets = await studio_media_service.list_assets(
        db, current_user.id, group_id=group_id, limit=limit,
        kind=kind, exclude_kind=None if kind else "upload",
    )
    return StudioMediaAssetListResponse(assets=assets)


@router.get("/assets/{asset_id}", response_model=StudioMediaAssetResponse)
async def get_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    asset = await studio_media_service.get_owned(db, current_user.id, asset_id)
    # A video still processing gets polled here so the client only ever calls this one endpoint.
    if asset.kind == "video" and asset.status == "processing":
        asset = await studio_media_service.poll_asset(db, current_user, asset)
    return asset


@router.post("/assets/{asset_id}/regenerate", response_model=StudioMediaAssetResponse, status_code=status.HTTP_201_CREATED)
async def regenerate(
    asset_id: str,
    payload: StudioMediaRegenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    parent = await studio_media_service.get_owned(db, current_user.id, asset_id)
    p_opts = dict(parent.options or {})
    is_video = parent.kind == "video"
    gen = StudioMediaGenerateRequest(
        action="animate" if is_video else "stack",
        source_photo_url=parent.source_photo_url,
        source_kind=parent.source_kind,
        linked_property=parent.linked_property,
        options=payload.options or {k: v for k, v in p_opts.items() if k in ("room_type", "style", "enhancements")},
        effects=None if is_video else (payload.effects or p_opts.get("effects") or (parent.action.split("+") if parent.action else None)),
        photos=[c.get("source") for c in (p_opts.get("clips") or []) if c.get("source")] if is_video else None,
    )
    return await studio_media_service.create_asset(
        db, current_user, gen, group_id=parent.group_id, parent_asset_id=parent.id,
    )


@router.patch("/assets/{asset_id}/name", response_model=StudioMediaAssetResponse)
async def rename_asset(
    asset_id: str,
    payload: StudioMediaRenameRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    return await studio_media_service.rename_asset(db, current_user.id, asset_id, payload.name)


@router.patch("/assets/{asset_id}/staging-tag", response_model=StudioMediaAssetResponse)
async def set_staging_tag(
    asset_id: str,
    payload: StudioMediaStagingTagRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    return await studio_media_service.set_staging_tag(db, current_user.id, asset_id, payload.position)


@router.post("/assets/{asset_id}/upscale", response_model=StudioMediaAssetResponse, status_code=status.HTTP_201_CREATED)
async def upscale(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    return await studio_media_service.upscale_video(db, current_user, asset_id)


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    await studio_media_service.delete_asset(db, current_user.id, asset_id)


@router.get("/assets/{asset_id}/download")
async def download_asset(
    asset_id: str,
    ratio: str | None = Query(None, description="Video only: 16:9 | 9:16 | 1:1 | 4:5. Omitted = the 16:9 base."),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    """Streams the generated artefact back with an attachment header. Needed for videos (a
    plain link would navigate to the mp4 instead of saving it) and gives images a clean
    filename too. For a video, `ratio` re-crops the single stored render to another aspect on
    the fly (YouTube / Reels / square / portrait) - no re-render, no new asset."""
    asset = await studio_media_service.get_owned(db, current_user.id, asset_id)
    if not asset.result_url or asset.status != "succeeded":
        return Response(status_code=status.HTTP_409_CONFLICT, content=b"Not ready")
    lower = asset.result_url.lower()
    if asset.kind == "video":
        ext = "webm" if lower.endswith(".webm") else "mp4"
        media_type = "video/webm" if ext == "webm" else "video/mp4"
    else:
        ext = "png"
        media_type = "image/png"

    want_reframe = (
        asset.kind == "video"
        and ratio
        and ratio in studio_media_service.VIDEO_FORMAT_KEYS
        and ratio != "16:9"
        and ext == "mp4"
    )
    if want_reframe:
        content = await studio_media_service.reframe_video_bytes(asset.result_url, ratio)
        suffix = f"-{ratio.replace(':', 'x')}"
    else:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as client:
                r = await client.get(asset.result_url)
                r.raise_for_status()
        except httpx.HTTPError:
            return Response(status_code=status.HTTP_502_BAD_GATEWAY, content=b"Couldn't fetch the file")
        content = r.content
        suffix = ""
        if asset.kind == "image":
            content = studio_media_service.apply_staging_tag(content, (asset.options or {}).get("staging_tag"))

    filename = f"{asset.action}-{asset.id[:8]}{suffix}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/assets/{asset_id}/overlay", response_model=StudioMediaAssetResponse)
async def update_overlay(
    asset_id: str,
    payload: VideoOverlayUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    """Autosaved from the Overlay editor as the agent edits it - a plain options write, no AI
    cost - so a configured title card / branding survives a reload without a regenerate."""
    return await studio_media_service.update_video_overlay(
        db, current_user.id, asset_id,
        title_card=payload.title_card.model_dump() if payload.title_card else None,
        branding=payload.branding.model_dump() if payload.branding else None,
    )


@router.post("/assets/{asset_id}/export")
async def export_asset(
    asset_id: str,
    payload: VideoExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_media_access),
):
    """Same one-render-many-exports model as plain GET /download (ratio re-crops the stored
    master on the fly, no re-render) - plus optionally burning in the title card and/or agent
    branding overlays. Used by the frontend whenever either overlay is enabled, for both the
    in-player "preview this format" fetch and the real Download button; falls back to producing
    the exact same bytes as /download when both overlays are omitted."""
    asset = await studio_media_service.get_owned(db, current_user.id, asset_id)
    content = await studio_media_service.export_video_bytes(
        db, current_user, asset,
        ratio=payload.ratio,
        title_card=payload.title_card.model_dump() if payload.title_card else None,
        branding=payload.branding.model_dump() if payload.branding else None,
    )
    ratio = payload.ratio if payload.ratio in studio_media_service.VIDEO_FORMAT_KEYS else "16:9"
    suffix = f"-{ratio.replace(':', 'x')}" if ratio != "16:9" else ""
    filename = f"{asset.action}-{asset.id[:8]}{suffix}.mp4"
    return Response(
        content=content,
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
