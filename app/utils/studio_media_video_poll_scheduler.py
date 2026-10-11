"""APScheduler entry point that advances in-flight AI video generations even when nobody has
the project's page open. Same shape as domain_verification_scheduler.py - registered in
app/main.py's lifespan().

Why this exists: studio_media_service.poll_asset() (the ONLY place a video ever transitions out
of "processing", and the only place the "your video is ready" notification fires) was, until
now, exclusively invoked by GET /studio-media/assets/{id} - i.e. only while an agent has that
exact project's page open and its poll loop running. An agent who starts a render and then
navigates away (explicitly encouraged - a render can take several minutes) leaves that asset
stuck showing "processing" indefinitely even after the underlying provider job (Hailuo render,
ByteDance upscale) actually finished, until/unless someone happens to reopen that specific
project again. This job closes that gap by periodically polling every video asset still marked
"processing" for every agent, regardless of who's looking at it."""
from sqlalchemy import select

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.database import StudioMediaAsset, User
from app.services import studio_media_service

logger = get_logger(__name__)


async def scheduled_studio_media_video_poll():
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(StudioMediaAsset).where(
                    StudioMediaAsset.kind == "video",
                    StudioMediaAsset.status == "processing",
                )
            )
            assets = result.scalars().all()
            if not assets:
                return

            owner_ids = {a.owner_id for a in assets}
            owners = (await db.execute(
                select(User).where(User.id.in_(owner_ids))
            )).scalars().all()
            owner_by_id = {u.id: u for u in owners}

            polled = 0
            finished = 0
            for asset in assets:
                owner = owner_by_id.get(asset.owner_id)
                if not owner:
                    continue  # orphaned row - shouldn't happen given the FK, skip defensively
                try:
                    updated = await studio_media_service.poll_asset(db, owner, asset)
                    polled += 1
                    if updated.status != "processing":
                        finished += 1
                except Exception as e:
                    logger.error(
                        "Background video poll failed for one asset",
                        exc_info=True,
                        extra={"asset_id": asset.id, "error": str(e)},
                    )

            if polled:
                logger.info(
                    "Studio media video poll completed",
                    extra={"event": "studio_media_video_poll", "polled": polled, "finished": finished},
                )
    except Exception as e:
        logger.error("Scheduled studio media video poll failed", exc_info=True, extra={"error": str(e)})
