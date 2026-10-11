import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import AgentHub, Blog, BlogEvent, User
from app.services import s3_service, storage_ledger_service
from app.schemas.blogs import (
    BlogLeadCreate,
    BlogAiBrief,
    BlogAiGenerateResponse,
    BlogAiRefineRequest,
    BlogAiRefineResponse,
    BlogAnalyticsResponse,
    BlogCreate,
    BlogEventCreate,
    BlogHubVisibilityUpdate,
    BlogListItem,
    BlogResponse,
    BlogSlugCheckResponse,
    BlogSlugUpdate,
    BlogStatusUpdate,
    BlogUpdate,
    PublicBlogDetail,
    PublicBlogSummary,
)
from app.services import email_signature_service
from app.services import blog_neighborhoods
from app.utils.auth import get_current_active_user, get_current_user_optional, require_studio_design_access, luxe_blog_theme_allowed

router = APIRouter(prefix="/studio/blogs", tags=["blogs"])
logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WORDS_PER_MINUTE = 200


def normalize_slug(slug: str) -> str:
    return slug.strip().lower()


def validate_slug(slug: str) -> str:
    normalized = normalize_slug(slug)
    if not SLUG_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use letters, numbers, and hyphens only. No spaces or unsupported symbols.",
        )
    return normalized


def slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return base or "article"


def estimate_reading_time(blocks: list) -> int:
    word_count = 0
    for block in blocks or []:
        data = block.get("data") if isinstance(block, dict) else None
        if not isinstance(data, dict):
            continue
        # Rich-text blocks store data.html (text is only the legacy plain field) - count both.
        html_value = data.get("html")
        if isinstance(html_value, str) and not data.get("text"):
            word_count += len(re.sub(r"<[^>]+>", " ", html_value).split())
        for key in ("text", "caption", "items"):
            value = data.get(key)
            if isinstance(value, str):
                word_count += len(value.split())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        word_count += len(item.split())
    return max(1, round(word_count / WORDS_PER_MINUTE))


async def get_owned_blog(db: AsyncSession, blog_id: str, owner_id: str) -> Blog:
    result = await db.execute(select(Blog).where(Blog.id == blog_id, Blog.owner_id == owner_id))
    blog = result.scalar_one_or_none()
    if not blog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog not found")
    return blog


async def next_available_slug(db: AsyncSession, owner_id: str, base_slug: str, exclude_id: Optional[str] = None) -> str:
    existing_q = select(Blog.slug).where(Blog.owner_id == owner_id)
    if exclude_id:
        existing_q = existing_q.where(Blog.id != exclude_id)
    existing_slugs = {row[0] for row in (await db.execute(existing_q)).all()}
    slug = base_slug
    suffix = 2
    while slug in existing_slugs:
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


@router.get("/me", response_model=List[BlogListItem])
async def get_my_blogs(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    query = select(Blog).where(Blog.owner_id == current_user.id)
    if status_filter:
        query = query.where(Blog.status == status_filter.upper())
    result = await db.execute(query.order_by(Blog.updated_at.desc().nullslast(), Blog.created_at.desc()))
    blogs = result.scalars().all()

    view_counts: dict[str, int] = {}
    if blogs:
        counts_result = await db.execute(
            select(BlogEvent.blog_id, func.count(BlogEvent.id))
            .where(BlogEvent.blog_id.in_([b.id for b in blogs]), BlogEvent.event_type == "view")
            .group_by(BlogEvent.blog_id)
        )
        view_counts = dict(counts_result.all())

    items = []
    for blog in blogs:
        item = BlogListItem.model_validate(blog)
        item.view_count = view_counts.get(blog.id, 0)
        item.preview_image_url = blog_preview_image(blog)
        items.append(item)
    return items


def blog_preview_image(blog: Blog) -> Optional[str]:
    """The picture a card shows for this article: its cover, else the first photo inside it."""
    if blog.cover_image_url:
        return blog.cover_image_url
    for block in blog.blocks or []:
        data = (block or {}).get("data") or {}
        kind = (block or {}).get("type")
        if kind == "image" and data.get("url"):
            return data["url"]
        if kind == "gallery" and data.get("images"):
            return data["images"][0]
        if kind == "property_card" and (data.get("property") or {}).get("image_url"):
            return data["property"]["image_url"]
    for prop in blog.linked_properties or []:
        if isinstance(prop, dict) and prop.get("image_url"):
            return prop["image_url"]
    return blog.social_image_url or None


@router.post("", response_model=BlogResponse, status_code=status.HTTP_201_CREATED)
async def create_blog(
    payload: BlogCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    title = payload.title.strip() or "Untitled Article"
    base_slug = slugify(title)
    slug = await next_available_slug(db, current_user.id, base_slug)

    blog = Blog(
        owner_id=current_user.id,
        slug=slug,
        title=title,
        category=payload.category,
        # 'luxe_plus' (the Luxe article theme) is IDX + No-MLS Premium only - anyone else starts on the default.
        theme=payload.theme if payload.theme != "luxe_plus" or luxe_blog_theme_allowed(current_user) else "luxe",
        blocks=[block.model_dump() for block in payload.starter_blocks] if payload.starter_blocks else [],
    )
    db.add(blog)
    await db.commit()
    await db.refresh(blog)
    return blog


# ---------------------------------------------------------------------------
# AI Blog Writer - guided interview -> full article (see ai_blog_writer_service.py). Metered on
# the agent's shared Monthly AI Usage allowance; the AI only ever saves DRAFTS.
# ---------------------------------------------------------------------------

@router.get("/linkable-areas")
async def get_linkable_areas(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """The IDX neighborhood pages an article can link to - or why it can't yet (reason:
    no_plan / no_site / site_off / not_published / no_areas) so the blog flow can say what to do."""
    return await blog_neighborhoods.linkable_areas(db, current_user)


@router.get("/ai/estimate")
async def estimate_blog_ai(
    length: str = "standard",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Roughly what one AI-written article costs (same model + pricing as the real call), for the
    "about X% of your monthly AI" note in the new-article flow before the agent spends it."""
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    # The researched article (Premium by default): its measured typical cost, scaled by length.
    engine = config.get("blog_pipeline_default_engine", "max")
    typical = float((config.get("blog_pipeline_typical_usd") or {}).get(engine, 0.65))
    usd = typical * {"short": 0.8, "standard": 1.0, "long": 1.3}.get(length, 1.0)
    limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(current_user.plan_tier)
    return {"usd": round(usd, 5), "percent": round(usd / float(limit) * 100, 2) if limit else None}


@router.get("/ai/context")
async def get_blog_ai_context(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Pre-fill for the interview (profile, served areas, real sales/listings) + whether this
    month's AI allowance can cover a generation, so the UI can block up-front."""
    from app.services.ai import ai_blog_writer_service
    return await ai_blog_writer_service.build_context(db, current_user)


@router.post("/ai/generate", response_model=BlogAiGenerateResponse)
async def generate_blog_with_ai(
    payload: BlogAiBrief,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    from app.services.ai import ai_blog_writer_service
    if not payload.topic.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Tell the AI what this article is about.")
    existing = await get_owned_blog(db, payload.blog_id, current_user.id) if payload.blog_id else None

    brief = payload.model_dump()
    brief["listings"] = [p.model_dump(exclude_none=True) for p in payload.listings]
    if payload.engine:
        from app.services.ai import ai_blog_pipeline_service
        result = await ai_blog_pipeline_service.run_pipeline(db, current_user, brief, engine=payload.engine)
    else:
        result = await ai_blog_writer_service.generate_article(db, current_user, brief)

    if existing:
        blog = existing
        blog.title = result["title"] or blog.title
    else:
        blog = Blog(
            owner_id=current_user.id,
            slug=await next_available_slug(db, current_user.id, slugify(result["title"])),
            title=result["title"] or "Untitled Article",
            status="DRAFT",
        )
        db.add(blog)
    _apply_ai_result(blog, result)
    await db.commit()
    await db.refresh(blog)
    logger.info("AI blog draft generated", extra={"blog_id": blog.id, "owner_id": current_user.id, **result["usage"]})
    return BlogAiGenerateResponse(
        blog=BlogResponse.model_validate(blog),
        facts_used=result["facts_used"],
        numbers_to_verify=result["numbers_to_verify"],
        cover_image_idea=result["cover_image_idea"] or None,
        verification=result.get("verification"),
    )


def _apply_ai_result(blog: Blog, result: dict) -> None:
    """Write a finished AI draft (single-call writer or pipeline) onto the article."""
    blog.title = result["title"] or blog.title
    blog.subtitle = result["subtitle"] or None
    blog.category = result["category"]
    blog.tags = result["tags"]
    blog.blocks = result["blocks"]
    blog.seo_title = result["seo_title"] or None
    blog.meta_description = result["meta_description"] or None
    blog.focus_keyword = result["focus_keyword"] or None
    blog.social_title = result["title"] or None
    blog.social_description = result["meta_description"] or None
    if result["linked_properties"]:
        blog.linked_properties = result["linked_properties"]
    review = result.get("verification")
    blog.ai_review = {**review, "status": "done", "facts_used": result.get("facts_used") or [],
                      "cover_image_idea": result.get("cover_image_idea") or None} if review else None


# ---------------------------------------------------------------------------
# Researched articles (Research -> Write -> Fact-check, ai_blog_pipeline_service) run in the
# background - Premium takes a few minutes. The article row itself carries the job state in
# blogs.ai_review ({"status": "running", "stage": ...} -> the Verified report when done), so any
# worker can answer the progress poll and a restart mid-run times out cleanly.
# ---------------------------------------------------------------------------

_AI_JOBS: set = set()  # strong refs so running tasks aren't garbage-collected
_AI_JOB_STALE_SECONDS = 15 * 60


def _job_is_stale(review: dict) -> bool:
    try:
        started = datetime.fromisoformat(review.get("stage_at") or review.get("started_at"))
    except Exception:
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > _AI_JOB_STALE_SECONDS


async def _run_blog_ai_job(blog_id: str, user_id: str, brief: dict, engine: str) -> None:
    from sqlalchemy import update
    from app.database import AsyncSessionLocal
    from app.services.ai import ai_blog_pipeline_service

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        blog = await db.get(Blog, blog_id)
        if not user or not blog:
            return
        job = dict(blog.ai_review or {})

        async def on_stage(name: str) -> None:
            job.update(stage=name, stage_at=datetime.now(timezone.utc).isoformat())
            await db.execute(update(Blog).where(Blog.id == blog_id).values(ai_review=dict(job)))
            await db.commit()

        error = None
        try:
            result = await ai_blog_pipeline_service.run_pipeline(db, user, brief, engine=engine, on_stage=on_stage)
        except HTTPException as exc:
            error = str(exc.detail)
        except Exception:
            logger.exception("AI blog job failed", extra={"blog_id": blog_id})
            error = "The AI writer hit a problem - please try again. You were only charged for what ran."
        blog = await db.get(Blog, blog_id)
        if not blog:
            return
        await db.refresh(blog)
        if error:
            blog.ai_review = {**job, "status": "failed", "error": error}
        else:
            if job.get("new"):
                blog.slug = await next_available_slug(db, blog.owner_id, slugify(result["title"]), exclude_id=blog.id)
            _apply_ai_result(blog, result)
            logger.info("AI blog draft generated", extra={"blog_id": blog.id, "owner_id": user_id, "engine": engine, "cost_usd": result["usage"]["cost_usd"]})
        await db.commit()


@router.post("/ai/generate-job")
async def start_blog_ai_job(
    payload: BlogAiBrief,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Start a researched article in the background; poll GET /ai/jobs/{blog_id}. A new article is
    created right away as a DRAFT (its title is the topic until the AI names it); "Draft with AI"
    on an existing article only replaces its content once the draft is finished."""
    import asyncio
    from app.services.ai import ai_blog_pipeline_service
    if not payload.topic.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Tell the AI what this article is about.")
    engine = await ai_blog_pipeline_service.precheck(db, current_user, payload.engine)
    existing = await get_owned_blog(db, payload.blog_id, current_user.id) if payload.blog_id else None
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        current = existing.ai_review or {}
        if current.get("status") == "running" and not _job_is_stale(current):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The AI is already writing this article.")
        blog = existing
        previous = current if current.get("status") == "done" else None
    else:
        topic = payload.topic.strip()
        blog = Blog(owner_id=current_user.id, slug=await next_available_slug(db, current_user.id, slugify(topic)), title=topic[:200], status="DRAFT")
        if payload.category_hint:
            blog.category = payload.category_hint
        db.add(blog)
        previous = None
    blog.ai_review = {"status": "running", "stage": "gathering", "engine": engine, "started_at": now, "stage_at": now,
                      "new": existing is None, "previous": previous}
    await db.commit()
    await db.refresh(blog)

    brief = payload.model_dump()
    brief["listings"] = [p.model_dump(exclude_none=True) for p in payload.listings]
    task = asyncio.create_task(_run_blog_ai_job(blog.id, current_user.id, brief, engine))
    _AI_JOBS.add(task)
    task.add_done_callback(_AI_JOBS.discard)
    return {"blog_id": blog.id, "status": "running", "stage": "gathering", "engine": engine}


@router.get("/ai/jobs/{blog_id}")
async def get_blog_ai_job(
    blog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    blog = await get_owned_blog(db, blog_id, current_user.id)
    review = blog.ai_review or {}
    state = review.get("status") or "done"
    if state == "running" and _job_is_stale(review):
        state, review = "failed", {**review, "error": "The AI writer took too long - please try again. You were only charged for what ran."}
    if state == "running":
        return {"status": "running", "stage": review.get("stage"), "engine": review.get("engine"), "started_at": review.get("started_at")}
    if state == "failed":
        error = review.get("error") or "The AI writer could not finish this draft."
        # A brand-new article that never got written is removed; a rewrite keeps its old content and report.
        if review.get("new"):
            await db.delete(blog)
        else:
            blog.ai_review = review.get("previous")
        await db.commit()
        return {"status": "failed", "error": error}
    return {"status": "done", "result": BlogAiGenerateResponse(
        blog=BlogResponse.model_validate(blog),
        facts_used=review.get("facts_used") or [],
        numbers_to_verify=review.get("numbers_to_verify") or [],
        cover_image_idea=review.get("cover_image_idea"),
        verification=review or None,
    ).model_dump(mode="json")}


@router.post("/{blog_id}/ai-finished", response_model=BlogResponse)
async def mark_blog_ai_finished(
    blog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """The agent went through (or skipped) the AI writer's Finish step for this article - the
    editor stops offering it."""
    blog = await get_owned_blog(db, blog_id, current_user.id)
    if blog.ai_review:
        blog.ai_review = {**blog.ai_review, "finished": True}
        await db.commit()
        await db.refresh(blog)
    return BlogResponse.model_validate(blog)


@router.post("/ai/refine-section", response_model=BlogAiRefineResponse)
async def refine_blog_section_with_ai(
    payload: BlogAiRefineRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Section-level rewrite/expand/shorten/etc. Returns replacement blocks; the editor swaps them
    in (and autosaves) - nothing is written here."""
    from app.services.ai import ai_blog_writer_service
    article_blocks = [b.model_dump() for b in (payload.article_blocks or payload.section_blocks)]
    result = await ai_blog_writer_service.refine_section(
        db, current_user,
        title=payload.title,
        section_blocks=[b.model_dump() for b in payload.section_blocks],
        article_text=ai_blog_writer_service.blocks_to_text(article_blocks),
        action=payload.action,
        instruction=payload.instruction,
    )
    return BlogAiRefineResponse(blocks=result["blocks"], numbers_to_verify=result["numbers_to_verify"])


@router.get("/{blog_id}", response_model=BlogResponse)
async def get_blog(
    blog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    return await get_owned_blog(db, blog_id, current_user.id)


@router.put("/{blog_id}", response_model=BlogResponse)
async def update_blog(
    blog_id: str,
    payload: BlogUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    blog = await get_owned_blog(db, blog_id, current_user.id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("theme") == "luxe_plus" and not luxe_blog_theme_allowed(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The Luxe article theme is part of the IDX plan.")

    old_cover_url = blog.cover_image_url
    for field, value in data.items():
        setattr(blog, field, value)

    await db.commit()
    await db.refresh(blog)

    # A changed cover image orphaned the old file (in S3 and in the storage ledger) - same
    # release-old-then-record-new pattern as brand logos/agent photos.
    if "cover_image_url" in data and old_cover_url and old_cover_url != blog.cover_image_url:
        s3_service.delete_by_url(old_cover_url)
        await storage_ledger_service.release(db, old_cover_url)

    return blog


@router.patch("/{blog_id}/status", response_model=BlogResponse)
async def update_blog_status(
    blog_id: str,
    payload: BlogStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    blog = await get_owned_blog(db, blog_id, current_user.id)
    now = datetime.now(timezone.utc)

    blog.status = payload.status
    if payload.status == "SCHEDULED":
        if not payload.scheduled_at:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Choose a date and time to schedule this article.")
        blog.scheduled_at = payload.scheduled_at
    elif payload.status == "PUBLISHED":
        blog.published_at = blog.published_at or now
        blog.scheduled_at = None
    elif payload.status in ("DRAFT", "UNPUBLISHED", "ARCHIVED"):
        blog.scheduled_at = None

    await db.commit()
    await db.refresh(blog)
    return blog


@router.patch("/{blog_id}/agent-hub-visibility", response_model=BlogResponse)
async def update_agent_hub_visibility(
    blog_id: str,
    payload: BlogHubVisibilityUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    blog = await get_owned_blog(db, blog_id, current_user.id)
    if payload.agent_hub_visible is not None:
        blog.agent_hub_visible = payload.agent_hub_visible
    if payload.agent_hub_featured is not None:
        blog.agent_hub_featured = payload.agent_hub_featured
    if payload.idx_visible is not None:
        blog.idx_visible = payload.idx_visible
    if payload.use_signature_card is not None:
        blog.use_signature_card = payload.use_signature_card
    if payload.linked_areas is not None:
        blog.linked_areas = [s for s in payload.linked_areas if isinstance(s, str) and s][:12]
    await db.commit()
    await db.refresh(blog)
    return blog


@router.get("/{blog_id}/slug/check", response_model=BlogSlugCheckResponse)
async def check_blog_slug(
    blog_id: str,
    slug: str = Query(..., min_length=1, max_length=120),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    normalized = validate_slug(slug)
    result = await db.execute(
        select(Blog).where(Blog.owner_id == current_user.id, Blog.slug == normalized, Blog.id != blog_id)
    )
    return BlogSlugCheckResponse(slug=normalized, available=result.scalar_one_or_none() is None)


@router.put("/{blog_id}/slug", response_model=BlogResponse)
async def update_blog_slug(
    blog_id: str,
    payload: BlogSlugUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    blog = await get_owned_blog(db, blog_id, current_user.id)
    normalized = validate_slug(payload.slug)

    existing = await db.execute(
        select(Blog).where(Blog.owner_id == current_user.id, Blog.slug == normalized, Blog.id != blog_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already have an article using this link.")

    blog.slug = normalized
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already have an article using this link.")

    await db.refresh(blog)
    return blog


@router.get("/{blog_id}/analytics", response_model=BlogAnalyticsResponse)
async def get_blog_analytics(
    blog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    await get_owned_blog(db, blog_id, current_user.id)
    result = await db.execute(
        select(BlogEvent.event_type, func.count(BlogEvent.id))
        .where(BlogEvent.blog_id == blog_id)
        .group_by(BlogEvent.event_type)
    )
    counts = dict(result.all())
    return BlogAnalyticsResponse(
        views=counts.get("view", 0),
        link_clicks=counts.get("link_click", 0),
        property_clicks=counts.get("property_click", 0),
        contact_clicks=counts.get("contact_click", 0),
        video_plays=counts.get("video_play", 0),
        qr_scans=counts.get("qr_scan", 0),
        lead_submissions=counts.get("lead_submit", 0),
    )


@router.delete("/{blog_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_blog(
    blog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Permanent delete - only reachable from Studio -> Blogs. Use /status (ARCHIVED/UNPUBLISHED) elsewhere."""
    blog = await get_owned_blog(db, blog_id, current_user.id)
    cover_url = blog.cover_image_url
    await db.delete(blog)
    await db.commit()

    if cover_url:
        s3_service.delete_by_url(cover_url)
        await storage_ledger_service.release(db, cover_url)


# ---------------------------------------------------------------------------
# Public (visitor-facing, no auth)
# ---------------------------------------------------------------------------

async def _blog_owner_live(db: AsyncSession, owner_id: str) -> bool:
    """Public articles only stay up while their agent is on a plan with Blogs (same as the Agent
    Hub and forms going offline when a subscription ends) - the posts come straight back on resubscribe."""
    from app.utils.auth import owner_has_access, STUDIO_DESIGN_ACCESS_TIERS
    owner = await db.get(User, owner_id)
    return owner is not None and owner_has_access(owner, STUDIO_DESIGN_ACCESS_TIERS)


@router.get("/public/{agent_slug}", response_model=List[PublicBlogSummary])
async def list_public_blogs(
    agent_slug: str,
    category: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_slug(agent_slug)))
    hub = hub_result.scalar_one_or_none()
    if not hub:
        return []

    if not await _blog_owner_live(db, hub.owner_id):
        return []
    query = select(Blog).where(Blog.owner_id == hub.owner_id, Blog.status == "PUBLISHED", Blog.agent_hub_visible.is_(True))
    if category:
        query = query.where(Blog.category == category)
    result = await db.execute(query.order_by(Blog.published_at.desc().nullslast()))
    blogs = result.scalars().all()

    view_counts: dict[str, int] = {}
    if blogs:
        counts_result = await db.execute(
            select(BlogEvent.blog_id, func.count(BlogEvent.id))
            .where(BlogEvent.blog_id.in_([b.id for b in blogs]), BlogEvent.event_type == "view")
            .group_by(BlogEvent.blog_id)
        )
        view_counts = dict(counts_result.all())

    return [
        PublicBlogSummary(
            slug=blog.slug,
            title=blog.title,
            subtitle=blog.subtitle,
            # Cover, else the first photo inside the article - list cards always get a picture when one exists.
            coverImageUrl=blog_preview_image(blog),
            category=blog.category,
            publishedAt=blog.published_at,
            readingTimeMinutes=estimate_reading_time(blog.blocks),
            viewCount=view_counts.get(blog.id, 0),
            agentHubVisible=blog.agent_hub_visible,
            agentHubFeatured=blog.agent_hub_featured,
        )
        for blog in blogs
    ]


@router.get("/public/{agent_slug}/{blog_slug}", response_model=PublicBlogDetail)
async def get_public_blog(
    agent_slug: str,
    blog_slug: str,
    # Server-side reads for link previews / page titles pass track=false - otherwise every real read
    # counted twice and every link-preview bot added a fake one.
    track: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    viewer: Optional[User] = Depends(get_current_user_optional),
):
    hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_slug(agent_slug)))
    hub = hub_result.scalar_one_or_none()
    if not hub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This article could not be found.")

    blog_result = await db.execute(
        select(Blog).where(Blog.owner_id == hub.owner_id, Blog.slug == normalize_slug(blog_slug), Blog.status == "PUBLISHED")
    )
    blog = blog_result.scalar_one_or_none()
    if not blog or not await _blog_owner_live(db, hub.owner_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This article could not be found.")

    agent_result = await db.execute(select(User).where(User.id == hub.owner_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This article could not be found.")

    # Only count fully anonymous requests as a "read" - any logged-in viewer (the owner
    # previewing, or another agent browsing while signed in) is staff traffic, not a real guest.
    if track and not viewer:
        try:
            db.add(BlogEvent(blog_id=blog.id, owner_id=blog.owner_id, event_type="view"))
            await db.commit()
        except Exception as e:
            logger.warning("recording blog view failed", extra={"error": str(e)})
            await db.rollback()

    return PublicBlogDetail(
        slug=blog.slug,
        title=blog.title,
        subtitle=blog.subtitle,
        coverImageUrl=blog.cover_image_url,
        category=blog.category,
        tags=blog.tags or [],
        blocks=blog.blocks or [],
        linkedProperties=blog.linked_properties or [],
        seoTitle=blog.seo_title,
        metaDescription=blog.meta_description,
        socialTitle=blog.social_title,
        socialDescription=blog.social_description,
        socialImageUrl=blog.social_image_url,
        isIndexed=blog.is_indexed,
        publishedAt=blog.published_at,
        readingTimeMinutes=estimate_reading_time(blog.blocks),
        agentName=f"{agent.first_name} {agent.last_name}".strip(),
        agentBrokerage=agent.brokerage,
        agentPhotoUrl=agent.photo_url,
        agentTitle=agent.professional_title,
        agentMarket=agent.market,
        # A Luxe article whose author left IDX shows in the default look (the choice is kept).
        theme=blog.theme if blog.theme != "luxe_plus" or luxe_blog_theme_allowed(agent) else "luxe",
        previewImageUrl=blog_preview_image(blog),
        related=await _related_articles(db, blog),
        signatureCard=bool(blog.use_signature_card) and await email_signature_service.has_signature_card(db, agent),
        areaTowns=_article_towns(blog),
        neighborhoodLinks=await blog_neighborhoods.neighborhood_links(db, agent, blog.linked_areas),
        luxeMetal=blog.luxe_metal,
        forms=await _public_forms(db, blog.owner_id),
    )


async def _public_forms(db: AsyncSession, owner_id: str) -> list:
    """The agent's switched-on forms, for the article's closing buttons."""
    from app.models.database import AgentForm
    rows = (await db.execute(
        select(AgentForm.form_type, AgentForm.slug, AgentForm.title)
        .where(AgentForm.owner_id == owner_id, AgentForm.is_enabled.is_(True))
        .order_by(AgentForm.order_index)
    )).all()
    return [{"formType": f.form_type, "slug": f.slug, "title": f.title} for f in rows]


def _article_towns(blog: Blog) -> list:
    """Towns the article is about (its AI research) - the signature card leads with sales and
    listings there. A regional article ("the Main Line") still names its towns, so it keeps them."""
    review = blog.ai_review or {}
    return [t.get("town") for t in (review.get("towns") or []) if isinstance(t, dict) and t.get("town")][:4]


async def _related_articles(db: AsyncSession, blog: Blog, limit: int = 3) -> list:
    """Up to `limit` of the agent's other published articles - same category first, then newest."""
    others = (await db.execute(
        select(Blog).where(Blog.owner_id == blog.owner_id, Blog.status == "PUBLISHED", Blog.id != blog.id, Blog.agent_hub_visible.is_(True))
        .order_by(Blog.published_at.desc().nullslast()).limit(12)
    )).scalars().all()
    others = sorted(others, key=lambda b: 0 if (blog.category and b.category == blog.category) else 1)[:limit]
    return [
        PublicBlogSummary(
            slug=b.slug, title=b.title, subtitle=b.subtitle, coverImageUrl=b.cover_image_url or blog_preview_image(b),
            category=b.category, publishedAt=b.published_at, readingTimeMinutes=estimate_reading_time(b.blocks),
        )
        for b in others
    ]


@router.post("/public/{agent_slug}/{blog_slug}/lead", status_code=status.HTTP_201_CREATED)
async def submit_blog_lead(
    agent_slug: str,
    blog_slug: str,
    payload: BlogLeadCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """A reader's question from an article's Contact Form block -> a real lead (Leads, bell, the
    agent's new-lead email, an automatic follow-up and their CRM), tagged with the article."""
    from app.services.public_rate_limit import check_public_submit
    from app.services import leads_service
    from app.services.agent_email_service import absolute, email_agent
    from app.models.database import Notification
    check_public_submit(request)
    name = (payload.name or "").strip()[:120]
    email = (payload.email or "").strip()[:200] or None
    phone = (payload.phone or "").strip()[:40] or None
    message = (payload.message or "").strip()[:2000] or None
    if not name or not (email or phone):
        raise HTTPException(status_code=422, detail="Add your name and an email or phone number.")
    from app.services.forms_service import _EMAIL_RE
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    hub = (await db.execute(select(AgentHub).where(AgentHub.slug == normalize_slug(agent_slug)))).scalar_one_or_none()
    blog = (await db.execute(
        select(Blog).where(Blog.owner_id == hub.owner_id, Blog.slug == normalize_slug(blog_slug), Blog.status == "PUBLISHED")
    )).scalar_one_or_none() if hub else None
    owner = await db.get(User, hub.owner_id) if hub else None
    if not blog or not owner or not await _blog_owner_live(db, owner.id):
        raise HTTPException(status_code=404, detail="This article could not be found.")
    lead = await leads_service.upsert_lead(
        db, owner_id=owner.id, name=name, email=email, phone=phone,
        source_type="blog_inquiry", source_id=blog.id,
        summary=f"Asked a question on your article \u201c{blog.title}\u201d",
        occurred_at=datetime.now(timezone.utc),
        raw_snapshot={"message": message, "blog_id": blog.id, "blog_title": blog.title, "blog_slug": blog.slug},
    )
    try:
        db.add(BlogEvent(blog_id=blog.id, owner_id=owner.id, event_type="lead_submit"))
        link = f"/leads/{lead.id}" if lead is not None else "/leads"
        db.add(Notification(
            agent_id=owner.id, type="LEAD", reference_type="LEAD", reference_id=lead.id if lead is not None else blog.id,
            title=f"New question from your blog: {name}"[:200], message=f"{name} asked about \u201c{blog.title}\u201d."[:500],
            visitor_name=name, link=link, is_read=False, created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        email_agent(owner, "LEAD", "new_lead_captured", f"New blog lead: {name}", {
            "lead_name": name, "lead_source": "Blog article", "lead_email": email, "lead_phone": phone,
            "lead_message": message, "property_address": blog.title, "link": absolute(link),
        })
    except Exception as e:  # the lead itself is already saved
        logger.warning("blog lead side effects failed", extra={"error": str(e)})
        await db.rollback()
    return {"sent": True}


@router.post("/public/{agent_slug}/{blog_slug}/event", status_code=status.HTTP_204_NO_CONTENT)
async def track_blog_event(
    agent_slug: str,
    blog_slug: str,
    payload: BlogEventCreate,
    db: AsyncSession = Depends(get_db),
    viewer: Optional[User] = Depends(get_current_user_optional),
):
    # A "view" ping comes from the IDX website's article page; like the Agent Hub read, a
    # signed-in viewer (the agent checking their own article) isn't a reader.
    if payload.event_type == "view" and viewer:
        return
    hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_slug(agent_slug)))
    hub = hub_result.scalar_one_or_none()
    if not hub:
        return

    blog_result = await db.execute(
        select(Blog).where(Blog.owner_id == hub.owner_id, Blog.slug == normalize_slug(blog_slug))
    )
    blog = blog_result.scalar_one_or_none()
    if not blog:
        return

    db.add(BlogEvent(blog_id=blog.id, owner_id=blog.owner_id, event_type=payload.event_type, meta=payload.meta))
    await db.commit()
