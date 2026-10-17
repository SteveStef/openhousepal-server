"""AI cover photos for blog articles (user, 10/17: "generate the AI cover photo in-app with a
credit estimate and up to 2 revision comments").

One new picture from the article's cover idea, then up to MAX_REVISIONS edits of that picture from
the agent's own comment ("make it dusk", "no snow"). Same model, limits and money guardrails as AI
Media (reserve -> call -> record the real cost -> settle; a failed call is recorded as blocked and
costs nothing). The picture is only a suggestion until the agent taps "Use as cover".
"""
import io
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Blog, User
from app.services import s3_service, storage_ledger_service
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_studio_media_image
from app.services.studio_media.providers.base import ImageEditRequest, ProviderError
from app.services.studio_media.providers.registry import image_provider
from app.services.studio_media_service import _image_cost, _studio_media_block_reason

logger = get_logger(__name__)

MAX_REVISIONS = 2
_PREFIX = "blog-covers"

STYLE = (
    "Photorealistic editorial cover photograph for a local real-estate article. {idea} "
    "Natural light, true-to-life colours, wide landscape composition with the subject centred. "
    "No text, no lettering, no signs with words, no logos, no watermarks, no people."
)
REVISE = (
    "Revise this cover photograph: {comment}. Keep everything else about the picture the same - "
    "composition, style, light. Still no text, no logos, no watermarks, no people."
)


def allowed(user: User, config: dict) -> bool:
    return (config.get("studio_media_image_monthly_generation_limit_by_tier") or {}).get(user.plan_tier) is not None


async def estimate(db: AsyncSession, user: User) -> dict:
    config = await ai_config_service.get_config(db)
    # What a cover really costs (measured 10/17: $0.04 for a 1536x1024 picture) - the AI Media
    # reservation estimate ($0.25) is a deliberately high hold, not the price.
    usd = float(config.get("blog_cover_typical_usd", 0.05) or 0.05)
    limit = (config.get("agent_monthly_ai_budget_usd_by_tier") or {}).get(user.plan_tier)
    return {"usd": round(usd, 4), "percent": round(usd / float(limit) * 100, 2) if limit else None,
            "allowed": allowed(user, config), "max_revisions": MAX_REVISIONS}


def _wide(png: bytes) -> bytes:
    """3:2 from the model -> the 16:9 a cover uses, cut from the middle; saved as a JPEG."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    nh = int(w * 9 / 16)
    if nh < h:
        top = (h - nh) // 2
        im = im.crop((0, top, w, top + nh))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


async def make_cover(db: AsyncSession, user: User, blog: Blog, idea: str | None, comment: str | None) -> dict:
    config = await ai_config_service.get_config(db)
    if not allowed(user, config):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI cover photos are part of plans with AI Media.")
    state = dict((blog.ai_review or {}).get("cover_ai") or {})
    revising = bool(comment and comment.strip())
    if revising:
        if not state.get("last_url"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Make the cover first, then ask for changes.")
        if int(state.get("revisions", 0)) >= MAX_REVISIONS:
            raise HTTPException(status.HTTP_409_CONFLICT, f"That's the {MAX_REVISIONS} changes this cover gets - upload your own photo or use this one.")
    idea = (idea or (blog.ai_review or {}).get("cover_image_idea") or blog.title or "").strip()[:900]
    if not idea and not revising:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Describe the picture you want.")

    provider = image_provider(config["studio_media_image_provider"], config["studio_media_image_model"])
    est = float(config.get("studio_media_image_reservation_estimate_usd", 0.25) or 0.25)
    source = None
    if revising:  # fetched before reserving, so a broken link never holds budget
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(state["last_url"])
                r.raise_for_status()
                source = r.content
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't load the last cover - try again.") from exc
    try:
        reservation = await reserve_studio_media_image(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.user_message)
    try:
        if revising:
            result = await provider.edit(ImageEditRequest(image_bytes=source, image_content_type="image/jpeg",
                                                          prompt=REVISE.format(comment=comment.strip()[:400]), size="1536x1024",
                                                          quality=config.get("studio_media_image_quality", "high")))
        else:
            result = await provider.generate(STYLE.format(idea=idea), size="1536x1024", quality=config.get("studio_media_image_quality", "high"))
    except ProviderError as exc:
        await reconcile_budget(db, reservation, 0.0)
        await record_usage_event(db, user.id, None, provider.model, None, None, 0.0, 0, True,
                                 _studio_media_block_reason(exc), event_type="studio_media_image")
        if exc.not_configured:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI pictures aren't set up yet.") from exc
        logger.warning("blog cover generation failed", extra={"blog_id": blog.id, "error": str(exc) or repr(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't make that picture right now - nothing was charged. Try again in a moment.") from exc

    cost = _image_cost(result, config, est)
    data = _wide(result.image_bytes)
    try:
        url = s3_service.upload_bytes(data, _PREFIX, ".jpg", "image/jpeg")
    except Exception as exc:  # noqa: BLE001
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't save the picture - nothing was charged.") from exc
    await record_usage_event(db, user.id, None, provider.model, None, None, cost, 0, False, None, event_type="studio_media_image")
    await reconcile_budget(db, reservation, cost)

    state.update(last_url=url, revisions=int(state.get("revisions", 0)) + (1 if revising else 0),
                 made=int(state.get("made", 0)) + 1, updated_at=datetime.now(timezone.utc).isoformat())
    if not revising:
        state["revisions"] = 0
    blog.ai_review = {**(blog.ai_review or {}), "cover_ai": state}
    await db.commit()
    await storage_ledger_service.record(db, user.id, "ai_media_image", url, len(data))
    return {"url": url, "revisions_left": MAX_REVISIONS - int(state["revisions"]), "cost_usd": round(cost, 4)}
