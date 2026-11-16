import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.hub_slugs import hub_by_slug
from app.database import get_db
from app.models.database import AgentHub, Blog, BlogEvent, Notification, User
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
from app.utils.auth import get_current_active_user, get_current_user_optional, require_studio_design_access, luxe_blog_theme_allowed, luxe_upgrade_label

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
        review = blog.ai_review or {}
        if review.get("status") == "running":
            item.ai_state = "failed" if _job_is_stale(review) else "running"
            if item.ai_state == "running" and review.get("mode") == "overnight":
                item.ai_state = "overnight"
                item.ai_ready_at = (review.get("overnight") or {}).get("likely_at")
        elif review.get("status") in ("interview", "failed") and review.get("new"):
            item.ai_state = review["status"]
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


_LENGTH_COST = {"short": 0.8, "standard": 1.0, "long": 1.3}


def _middle_cost(costs: list) -> Optional[float]:
    """The middle of what recent articles cost - None until there are three to go by."""
    from statistics import median
    costs = [float(c) for c in costs if c and float(c) > 0]
    return round(float(median(costs)), 4) if len(costs) >= 3 else None


async def _article_estimate(db: AsyncSession, config: dict, engine: str, length: str) -> tuple[float, float]:
    """(usd to write it now, usd to write it overnight) for the "about N% of your monthly AI" notes.
    Owner 11/14: "tell the agent exactly how much usage it works". The note used to be one fixed figure in the
    settings (0.90) while real articles were costing 1.08-1.23 after the research step started reading its sources
    properly. Now it is the middle of what the last dozen finished articles of each kind really cost (any agent,
    past 30 days), and the fixed figure only until there are three to go by."""
    from datetime import timedelta
    from sqlalchemy import text
    from app.services.ai import ai_blog_pipeline_service
    scale = _LENGTH_COST.get(length, 1.0)
    now_usd = night_usd = None
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "select (ai_review->>'run_cost_usd')::float as usd, coalesce(ai_review->>'overnight', 'false') = 'true' as night "
                "from blogs where ai_review->>'status' = 'done' and ai_review->>'engine' = :engine "
                "and ai_review->>'run_cost_usd' is not null and ai_review->>'run_finished_at' > :since "
                "order by ai_review->>'run_finished_at' desc limit 60"),
                {"engine": engine, "since": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()})).all()
        now_usd = _middle_cost([r[0] for r in rows if not r[1]][:12])
        night_usd = _middle_cost([r[0] for r in rows if r[1]][:12])
    except Exception:  # noqa: BLE001 - the note falls back to the fixed figure; it never blocks the screen
        logger.warning("blog estimate: could not read recent article costs", exc_info=True)
    usd = (now_usd or float((config.get("blog_pipeline_typical_usd") or {}).get(engine, 0.9))) * scale
    night = night_usd * scale if night_usd else ai_blog_pipeline_service.overnight_usd(config, usd)
    return round(usd, 5), round(min(night, usd), 5)


WORST_CASE_MARGIN = 1.3      # until three real articles can be measured: a heavy brief ran about 1.27x the middle (11/14)


async def _article_worst(db: AsyncSession, engine: str, length: str, usd: float) -> float:
    """The most a Save usage article is likely to use - the top of the range the agent sees BEFORE creating (owner
    11/15: "worst case scenario spend"). If the slower line is busy and the time cap finishes every step the fast
    way, the article costs what a "Write it now" article costs - and a heavy one costs more than the middle one. So:
    the HIGHEST of the last dozen "Write it now" articles (30 days), never less than the middle figure."""
    from datetime import timedelta
    from sqlalchemy import text
    scale = _LENGTH_COST.get(length, 1.0)
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "select (ai_review->>'run_cost_usd')::float as usd, coalesce(ai_review->>'overnight', 'false') = 'true' as night "
                "from blogs where ai_review->>'status' = 'done' and ai_review->>'engine' = :engine "
                "and ai_review->>'run_cost_usd' is not null and ai_review->>'run_finished_at' > :since "
                "order by ai_review->>'run_finished_at' desc limit 60"),
                {"engine": engine, "since": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()})).all()
        seen = [float(r[0]) for r in rows if not r[1] and r[0] and float(r[0]) > 0][:12]
        if len(seen) >= 3:
            return round(max(usd, max(seen) * scale), 5)
    except Exception:  # noqa: BLE001 - the note falls back to the margin; it never blocks the screen
        logger.warning("blog estimate: could not read recent article costs for the worst case", exc_info=True)
    return round(usd * WORST_CASE_MARGIN, 5)


async def _slow_minutes(db: AsyncSession, engine: str) -> Optional[int]:
    """How long Save usage mode has really been taking: the middle of the last dozen such articles (30 days), with a
    third added and rounded up to five minutes, so "likely ready by" is a time we usually beat. None until there
    are three to go by (the service's own figure is used then)."""
    from datetime import timedelta
    from statistics import median
    from sqlalchemy import text
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "select ai_review->>'run_started_at', ai_review->>'run_finished_at' from blogs where ai_review->>'status' = 'done' "
                "and ai_review->>'engine' = :engine and coalesce(ai_review->>'overnight', 'false') = 'true' "
                "and ai_review->>'run_finished_at' > :since order by ai_review->>'run_finished_at' desc limit 12"),
                {"engine": engine, "since": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()})).all()
        took = []
        for started, finished in rows:
            try:
                took.append((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds() / 60)
            except (TypeError, ValueError):
                pass
        if len(took) < 3:
            return None
        return max(10, int(-(-(median(took) * 1.33) // 5) * 5))
    except Exception:  # noqa: BLE001
        logger.warning("blog estimate: could not read recent save-usage run times", exc_info=True)
        return None


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
    # Overnight (owner 11/14): the same article through the half-price queue - what it uses, and when it is likely ready.
    usd, night = await _article_estimate(db, config, engine, length)
    worst = await _article_worst(db, engine, length, usd)
    from app.services.ai.ai_rate_limit_service import monthly_limit_usd
    limit = await monthly_limit_usd(db, current_user.id, config, current_user.plan_tier)
    from app.services.ai import blog_overnight_service
    return {"usd": round(usd, 5), "percent": round(usd / float(limit) * 100, 2) if limit else None,
            "overnight": {"usd": round(night, 5), "percent": round(night / float(limit) * 100, 2) if limit else None,
                          "usd_max": round(worst, 5), "percent_max": round(worst / float(limit) * 100, 2) if limit else None,
                          **blog_overnight_service.times(config, await _slow_minutes(db, engine))}}


@router.post("/ai/suggest")
async def blog_ai_suggest(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Tappable starting points for the writer's interview, read from the article's title (owner 11/14: "I wish I
    was selecting more than typing, based on suggestions the AI sees from the title"). Free picks (audience, the
    town in the title, closing button) plus one very small AI call for the angle / goal / key points / take choices -
    see blog_suggest_service. Never an error for the screen: without AI it returns the built-in choices."""
    from app.services.ai import blog_suggest_service
    title = str(payload.get("title") or "")[:200]
    preset = str(payload.get("preset") or "")[:80] or None
    # What we hold about the agent (owner 11/14: "are we using their recent sales, towns, profile, bio?"): the towns
    # on their site, the homes they sold by town, their bio, the articles they already wrote, today's month.
    context = await blog_suggest_service.agent_context(db, current_user)
    try:
        return await blog_suggest_service.suggest(db, current_user, title=title, preset=preset, context=context)
    except Exception:
        logger.warning("blog suggestions failed - built-in choices returned", exc_info=True)
        await db.rollback()
        return {**blog_suggest_service.merge(blog_suggest_service.from_title(title, preset=preset, known_towns=context.get("towns")), None, title, context=context), "cost_usd": 0.0}


@router.post("/ai/title-ideas")
async def blog_ai_title_ideas(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Titles to tap on the "Give it a title" screen, built from the agent's own towns, sales, bio, the month and what
    they have not written yet (owner 11/14: "having title suggestions also be very intelligent"). One very small AI
    call per agent per day; plain-code ideas when the AI can't be asked. Never an error for the screen."""
    from app.services.ai import blog_suggest_service
    preset = str(payload.get("preset") or "")[:80] or None
    template_title = str(payload.get("template_title") or "")[:120] or None
    context = await blog_suggest_service.agent_context(db, current_user)
    try:
        return await blog_suggest_service.title_ideas(db, current_user, preset=preset, template_title=template_title, context=context)
    except Exception:
        logger.warning("blog title ideas failed - built-in ideas returned", exc_info=True)
        await db.rollback()
        return {"titles": blog_suggest_service.code_titles(context), "ai": False, "cost_usd": 0.0}


@router.post("/ai/town-card")
async def blog_town_card(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Town Spotlight for a town outside the agent's served areas (any plan - user, 10/17). Uses
    the shared town database: a town someone already researched comes back instantly and free;
    a new one is researched once (in the background) and then serves everyone. Poll until
    status == "ready"."""
    from sqlalchemy import update
    from app.models.database import MarketArea
    from app.services.areas import area_enrichment_service
    from app.services.ai import ai_blog_writer_service
    from app.services.areas.town_knowledge_service import best_area_by_name
    name = str(payload.get("name") or "").split(",")[0].strip()[:80]
    typed_state = str(payload.get("state") or "").strip().upper()
    if not typed_state:
        tail = str(payload.get("name") or "").split(",")[1:]
        typed_state = tail[0].strip().upper()[:2] if tail else ""
    agent_state = (current_user.state or "PA").strip().upper()[:2]
    state = typed_state or agent_state
    if len(name) < 2 or len(state) != 2:
        raise HTTPException(status_code=422, detail="Type the town's name.")
    from app.services.ai import ai_blog_pipeline_service as _pipe
    if _pipe.is_county(name):
        # A county gets no Town Spotlight and is never made into a town record (11/15): the article uses the agent's
        # own towns inside it as its examples.
        return {"status": "none", "kind": "county", "name": name}
    # A town we already know by this name (any state unless one was typed, any kind - a Philly
    # neighborhood too) is the one meant; only a brand-new name becomes a new town in the agent's
    # state (CMA/blog test 11/13: "Cherry Hill" from a PA agent had made "Cherry Hill, PA").
    area = await best_area_by_name(db, name, state=typed_state or None, prefer_state=agent_state)
    if area is None:
        area = await area_enrichment_service.get_or_create_market_area(db, name, state)
    if area.enrichment_status == "ENRICHED":
        card = await ai_blog_writer_service.town_card_for_area(db, area)
        if not card.get("photo"):  # a real landmark photo for the preview (the AI picture waits for the article)
            await ai_blog_writer_service.ensure_spotlight_photos(db, current_user, [card], allow_ai=False)
        return {"status": "ready", "card": card}
    claim = await db.execute(
        update(MarketArea).where(MarketArea.id == area.id, MarketArea.enrichment_status.in_(["PENDING", "FAILED"]))
        .values(enrichment_status="RESEARCHING")
    )
    await db.commit()
    if claim.rowcount:
        import asyncio
        from app.api.site_routes import _enrich_area_background
        task = asyncio.create_task(_enrich_area_background(area.id, current_user.id))
        _AI_JOBS.add(task)
        task.add_done_callback(_AI_JOBS.discard)
    return {"status": "researching", "slug": area.slug, "name": area.name}


@router.get("/ai/cover-estimate")
async def estimate_blog_cover(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """What one AI cover picture (or one change to it) costs, as a share of the monthly AI."""
    from app.services import blog_cover_service
    return await blog_cover_service.estimate(db, current_user)


@router.post("/{blog_id}/ai-cover")
async def make_blog_cover(
    blog_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Make the article's cover picture with AI ({"idea": ...}), or change the last one
    ({"comment": ...}, up to 2 times). Returns the picture; "Use as cover" saves it."""
    from app.services import blog_cover_service
    blog = await get_owned_blog(db, blog_id, current_user.id)
    return await blog_cover_service.make_cover(db, current_user, blog, payload.get("idea"), payload.get("comment"))


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


_CHECKED_TEXT_BLOCKS = {"paragraph", "heading", "subheading", "quote", "callout", "bulleted_list", "numbered_list"}


def _checked_text_hash(blocks) -> str:
    """A fingerprint of the WORDS the fact-check read (text blocks only - moving a photo, a card or the look is
    not an edit). Engine audit 11/14: the green "Verified" badge stayed exactly the same after the agent rewrote
    the article by hand, though nothing they typed had been checked."""
    import hashlib
    from app.services.ai import ai_blog_writer_service
    rows = [b if isinstance(b, dict) else b.model_dump() for b in blocks or []]
    text = ai_blog_writer_service.blocks_to_text([b for b in rows if b.get("type") in _CHECKED_TEXT_BLOCKS])
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


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
                      "cover_image_idea": result.get("cover_image_idea") or None,
                      "text_hash": _checked_text_hash(result["blocks"]), "edited": False} if review else None


# ---------------------------------------------------------------------------
# Researched articles (Research -> Write -> Fact-check, ai_blog_pipeline_service) run in the
# background - Premium takes a few minutes. The article row itself carries the job state in
# blogs.ai_review ({"status": "running", "stage": ...} -> the Verified report when done), so any
# worker can answer the progress poll and a restart mid-run times out cleanly.
# ---------------------------------------------------------------------------

_AI_JOBS: set = set()  # strong refs so running tasks aren't garbage-collected
_AI_JOB_STALE_SECONDS = 15 * 60


def _job_is_stale(review: dict) -> bool:
    if (review or {}).get("mode") == "overnight":
        # An overnight run waits for hours by design - it is only "stale" past its own deadline.
        from app.services.ai import blog_overnight_service
        return blog_overnight_service.is_stale(review)
    try:
        started = datetime.fromisoformat(review.get("stage_at") or review.get("started_at"))
    except Exception:
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > _AI_JOB_STALE_SECONDS


# Every run ends on its own, with a clear answer, well before the progress poll would give up on it
# (owner 11/13: a live article sat on "Gathering" for 15 minutes while the database was overloaded,
# then failed - and the stuck run kept going in the background). Gathering is free database
# look-ups that take seconds; minutes there means the database is struggling, so the run stops
# early instead of making the agent wait. Nothing is charged for a run that is stopped.
_AI_JOB_TIME_LIMIT_SECONDS = 14 * 60
_AI_JOB_STAGE_LIMITS = {"gathering": 3 * 60}
_AI_JOB_POLL_SECONDS = 5
_AI_JOB_BUSY_ERROR = ("Our servers were too busy to look up your towns just now, so the AI never started writing. "
                      "Nothing was charged and your answers are saved - please try again in a few minutes.")
_AI_JOB_SLOW_ERROR = "The AI writer took too long - please try again. Nothing was charged for this try."


def _same_run(review: Optional[dict], job: dict) -> bool:
    """The article still belongs to this run - not given up by the progress poll, not replaced by a
    newer run, not reopened as a draft. A run never writes over anything else."""
    return bool(job.get("started_at")) and (review or {}).get("started_at") == job.get("started_at")


async def _run_blog_ai_job(blog_id: str, user_id: str, brief: dict, engine: str, overnight: bool = False, resume: bool = False) -> None:
    """`overnight`: the same run through the half-price queue (blog_overnight_service) - hours, not minutes,
    and able to carry on after a restart (`resume`: this is such a pick-up)."""
    from app.services.ai import blog_overnight_service as night
    if not overnight:
        return await _run_blog_ai_job_inner(blog_id, user_id, brief, engine)
    night._RUNNING.add(blog_id)
    try:
        return await _run_blog_ai_job_inner(blog_id, user_id, brief, engine, overnight=True, resume=resume)
    except night.Superseded:
        # Given up, replaced, deleted or picked up by another copy of the server - that run's owner finishes it.
        logger.info("overnight article: this process no longer owns the run", extra={"blog_id": blog_id})
    finally:
        night._RUNNING.discard(blog_id)


async def _run_blog_ai_job_inner(blog_id: str, user_id: str, brief: dict, engine: str, overnight: bool = False, resume: bool = False) -> None:
    import asyncio
    import time
    from sqlalchemy import update
    from app.database import AsyncSessionLocal
    from app.services.ai import ai_blog_pipeline_service
    from app.services.ai import blog_overnight_service as night

    error = None
    result = None
    journal = None
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        blog = await db.get(Blog, blog_id)
        if not user or not blog:
            return
        # This job IS the run: each stage write says "running" even if a stray autosave got in
        # before the job's first read (see save_blog_ai_brief).
        job = {**dict(blog.ai_review or {}), "status": "running"}
        stage_clock = {"name": job.get("stage") or "gathering", "at": time.monotonic()}

        async def save_overnight() -> None:
            """The run's own memory goes on the row with every step - and tells this process when the run is no
            longer its own."""
            saved = await db.execute(update(Blog).where(
                Blog.id == blog_id, Blog.ai_review["started_at"].astext == str(job.get("started_at")),
                Blog.ai_review["status"].astext == "running",
                Blog.ai_review["overnight"]["worker"].astext == night.WORKER).values(ai_review=dict(job)))
            await db.commit()
            if not saved.rowcount:
                raise night.Superseded()

        if overnight:
            job["overnight"] = dict(job.get("overnight") or {})
            journal = night.Journal(job["overnight"], save_overnight, resumed=resume)
            # Set before the run's task is created, so the task carries it.
            ai_blog_pipeline_service.TRANSPORT.set(night.BatchTransport(journal, db))

        async def on_stage(name: str) -> None:
            stage_clock.update(name=name, at=time.monotonic())
            job.update(stage=name, stage_at=datetime.now(timezone.utc).isoformat())
            if overnight:
                job["overnight"]["lease_at"] = job["stage_at"]
                return await save_overnight()
            await db.execute(update(Blog).where(Blog.id == blog_id, Blog.ai_review["started_at"].astext == str(job.get("started_at")))
                             .values(ai_review=dict(job)))
            await db.commit()

        run = asyncio.create_task(ai_blog_pipeline_service.run_pipeline(
            db, user, brief, engine=engine, on_stage=on_stage, **({"overnight": True, "journal": journal} if overnight else {})))
        began = time.monotonic()
        stopped = None
        # An overnight run ends itself at its deadline (the queue wait checks it); this is only the backstop.
        time_limit = (night.DEADLINE_HOURS * 3600 + 1800) if overnight else _AI_JOB_TIME_LIMIT_SECONDS
        while not run.done():
            await asyncio.wait({run}, timeout=_AI_JOB_POLL_SECONDS)
            if run.done():
                break
            now = time.monotonic()
            limit = _AI_JOB_STAGE_LIMITS.get(stage_clock["name"])
            if limit and now - stage_clock["at"] > limit:
                stopped = _AI_JOB_BUSY_ERROR
            elif now - began > time_limit:
                stopped = night.SLOW_ERROR if overnight else _AI_JOB_SLOW_ERROR
            if stopped:
                logger.warning("AI blog job stopped", extra={"blog_id": blog_id, "stage": stage_clock["name"], "seconds": round(now - began)})
                run.cancel()
                try:
                    await run
                except BaseException:
                    pass
                break
        if stopped:
            error = stopped
        else:
            try:
                result = run.result()
            except HTTPException as exc:
                error = str(exc.detail)
            except Exception:
                logger.exception("AI blog job failed", extra={"blog_id": blog_id})
                error = "The AI writer hit a problem - please try again. Nothing was charged for this try."
        if stopped:
            # The cancelled look-up may have left this connection mid-query - drop it, not reuse it.
            try:
                await db.invalidate()
            except Exception:
                pass

    # The ending is written on a fresh connection, so the run's long transaction is over either way.
    async with AsyncSessionLocal() as db:

        async def give_back() -> None:
            # An overnight run knows exactly which charges were its own (hours may have passed, with other
            # articles written in between); a normal run gives back what was charged since it started.
            if journal is not None:
                await night.refund_events(db, journal.event_ids())
            else:
                await ai_blog_pipeline_service.refund_since(db, user_id, job.get("started_at"))

        if stopped:
            await give_back()
        blog = await db.get(Blog, blog_id)
        if not blog:
            return
        mine = _same_run(blog.ai_review, job) and (
            not overnight or ((blog.ai_review or {}).get("overnight") or {}).get("worker") == night.WORKER)
        if overnight and _same_run(blog.ai_review, job) and not mine:
            return   # another copy of the server picked this run up - it finishes it
        if not mine:
            logger.warning("AI blog job finished after it was given up - not written", extra={"blog_id": blog_id, "failed": bool(error)})
            if not error:
                await give_back()
            return
        if error:
            blog.ai_review = {**job, "status": "failed", "error": error}
        else:
            if job.get("new"):
                blog.slug = await next_available_slug(db, blog.owner_id, slugify(result["title"]), exclude_id=blog.id)
            _apply_ai_result(blog, result)
            # What this run used and how long it took - the writer's "receipt" (user, 10/24).
            blog.ai_review = {**(blog.ai_review or {}),
                              "run_cost_usd": float((result.get("usage") or {}).get("cost_usd") or 0.0),
                              # What the agent typed stays with the finished article: it is the only record of which
                              # first-person lines are the agent's own words (11/14 grader: an outside reader can't tell).
                              "brief": job.get("brief") or None,
                              "run_started_at": job.get("started_at"),
                              "run_finished_at": datetime.now(timezone.utc).isoformat()}
            logger.info("AI blog draft generated", extra={"blog_id": blog.id, "owner_id": user_id, "engine": engine, "cost_usd": result["usage"]["cost_usd"]})
        # The agent may have left the page (user, 10/17: "work in the background and alert the agent
        # when it's done") - a bell alert either way. It clears itself once seen (the progress poll
        # marks it read - see _mark_ai_alert_seen).
        title = (blog.title or "your article")[:120]
        db.add(Notification(
            agent_id=user_id, type="BLOG_AI_FAILED" if error else "BLOG_AI_READY", reference_type="BLOG", reference_id=blog.id,
            title=(f"Couldn't finish \u201c{title}\u201d" if error else f"Your article is ready: \u201c{title}\u201d")[:200],
            message=(f"{error} Your answers are saved - open it to try again." if error else "Open it to pick its look and publish when you're happy.")[:500],
            link=f"/studio/blogs/{blog.id}", is_read=False, created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        if overnight:
            # The agent walked away on purpose: an email too, every time (owner 11/14: "it will alert them").
            await _email_overnight_result(db, user_id, blog.id, title, error,
                                          None if error else float((result.get("usage") or {}).get("cost_usd") or 0.0))
    # Started from the AI helper's chat? Its card and chat hear about it too (owner, 11/01) - a no-op
    # otherwise (ai_notify only answers what it was asked to watch).
    from app.services.ai import ai_notify
    await ai_notify.finished(
        user_id, f"blog:{blog_id}", not error,
        f"Your blog draft \u201c{title}\u201d is ready. Give it a read, pick its look and publish when you're happy - nothing is live until you do."
        if not error else f"The blog draft about {{label}} didn't finish: {error}",
        link=f"/studio/blogs/{blog_id}", link_label="Open the draft")


async def _email_overnight_result(db: AsyncSession, user_id: str, blog_id: str, title: str, error: Optional[str], cost_usd: Optional[float]) -> None:
    try:
        from app.services.agent_email_service import absolute, email_agent
        from app.services.ai.ai_config_service import ai_config_service
        from app.services.ai.ai_rate_limit_service import monthly_limit_usd
        owner = await db.get(User, user_id)
        if owner is None:
            return
        used_line = None
        if cost_usd:
            limit = await monthly_limit_usd(db, user_id, await ai_config_service.get_config(db), owner.plan_tier)
            if limit:
                used_line = f"It used about {max(0.1, round(cost_usd / float(limit) * 100, 1)):g}% of this month's AI usage."
        email_agent(owner, None, "blog_article_failed" if error else "blog_article_ready",
                    (f"We couldn't finish your article: {title}" if error else f"Your article is ready: {title}")[:150],
                    {"article_title": title, "link": absolute(f"/studio/blogs/{blog_id}"), "reason": error, "used_line": used_line},
                    always=True)
    except Exception:  # noqa: BLE001 - never sink a finished article on its notice
        logger.warning("overnight article email failed", exc_info=True, extra={"blog_id": blog_id})


async def _finish_overnight_failed(blog_id: str, owner_id: str, error: str) -> None:
    """End an overnight run nobody can finish (past its deadline, or it kept dying): refund what it charged,
    keep the agent's answers, and tell them - bell and email."""
    from app.database import AsyncSessionLocal
    from app.services.ai import blog_overnight_service as night
    async with AsyncSessionLocal() as db:
        blog = await db.get(Blog, blog_id)
        review = dict((blog.ai_review if blog else None) or {})
        if not blog or review.get("status") != "running" or review.get("mode") != "overnight":
            return
        ov = review.get("overnight") or {}
        await night.refund_events(db, [i for ids in (ov.get("events") or {}).values() for i in ids])
        blog = await db.get(Blog, blog_id)
        blog.ai_review = {**{k: v for k, v in review.items() if k != "overnight"}, "status": "failed", "error": error}
        title = (blog.title or "your article")[:120]
        db.add(Notification(
            agent_id=owner_id, type="BLOG_AI_FAILED", reference_type="BLOG", reference_id=blog.id,
            title=f"Couldn't finish \u201c{title}\u201d"[:200], message=f"{error} Your answers are saved - open it to try again."[:500],
            link=f"/studio/blogs/{blog.id}", is_read=False, created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        await _email_overnight_result(db, owner_id, blog.id, title, error, None)


async def _mark_ai_alert_seen(db: AsyncSession, blog_id: str, owner_id: str) -> None:
    from sqlalchemy import update
    await db.execute(update(Notification).where(
        Notification.agent_id == owner_id, Notification.reference_id == blog_id,
        Notification.type.in_(("BLOG_AI_READY", "BLOG_AI_FAILED")), Notification.is_read.is_(False),
    ).values(is_read=True, read_at=datetime.now(timezone.utc)))


def _brief_store(brief: dict) -> dict:
    """The interview answers kept on the article so a draft can be finished later - capped so a
    runaway paste can't bloat the row."""
    import json
    out = {k: v for k, v in (brief or {}).items() if k not in ("engine", "blog_id", "overnight")}
    return out if len(json.dumps(out, default=str)) <= 40_000 else {k: out.get(k) for k in ("topic", "goal", "audiences", "areas", "tone", "length", "cta")}


@router.put("/ai/brief-draft")
async def save_blog_ai_brief(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Autosave of the AI writer's interview (user, 10/17: "save every single step ... you can exit
    out and it saves everything you put into the flow as a draft to finish later"). The first save
    creates a DRAFT article (titled with the topic); later saves update it. Opening that draft from
    Blogs puts the agent back in the writer, on the step they left."""
    brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else {}
    topic = str(brief.get("topic") or "").strip()
    try:
        step = max(0, min(3, int(payload.get("step") or 0)))
    except (TypeError, ValueError):
        step = 0
    blog = await get_owned_blog(db, payload["blog_id"], current_user.id) if payload.get("blog_id") else None
    if blog is not None:
        current = blog.ai_review or {}
        if current.get("status") not in ("interview", "failed") or not current.get("new"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This article is no longer an unfinished AI draft.")
    if not topic:
        if blog is None:
            return {"blog_id": None}
        topic = blog.title
    now = datetime.now(timezone.utc).isoformat()
    if blog is None:
        blog = Blog(owner_id=current_user.id, slug=await next_available_slug(db, current_user.id, slugify(topic)), title=topic[:200], status="DRAFT")
        if payload.get("category_hint"):
            blog.category = str(payload["category_hint"])[:80]
        db.add(blog)
    else:
        # Check-and-write in one statement: an autosave that read the draft just before "Write my
        # article" started the job must not land after it and flip the running job back to an
        # unfinished draft (blog test 11/13 - the list said "Unfinished AI draft" while the AI wrote,
        # and a second Write would have started a second paid run).
        from sqlalchemy import update
        saved = await db.execute(
            update(Blog).where(Blog.id == blog.id, Blog.ai_review["status"].astext.in_(("interview", "failed")))
            .values(title=topic[:200], ai_review={"status": "interview", "new": True, "brief": _brief_store(brief),
                                                  "step": step, "saved_at": now})
        )
        await db.commit()
        if not saved.rowcount:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This article is no longer an unfinished AI draft.")
        return {"blog_id": blog.id, "saved_at": now}
    blog.ai_review = {"status": "interview", "new": True, "brief": _brief_store(brief), "step": step, "saved_at": now}
    await db.commit()
    await db.refresh(blog)
    return {"blog_id": blog.id, "saved_at": now}


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
    overnight = bool(payload.overnight)
    engine = await ai_blog_pipeline_service.precheck(db, current_user, payload.engine)
    existing = await get_owned_blog(db, payload.blog_id, current_user.id) if payload.blog_id else None
    now = datetime.now(timezone.utc).isoformat()
    is_new = existing is None
    if existing:
        current = existing.ai_review or {}
        if current.get("status") == "running" and not _job_is_stale(current):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The AI is already writing this article.")
        blog = existing
        previous = current if current.get("status") == "done" else None
        # An autosaved interview (or a run that failed) - still a brand-new article, never written.
        is_new = bool(current.get("new")) and current.get("status") in ("interview", "failed", "running")
        if is_new:
            blog.title = payload.topic.strip()[:200]
    else:
        topic = payload.topic.strip()
        blog = Blog(owner_id=current_user.id, slug=await next_available_slug(db, current_user.id, slugify(topic)), title=topic[:200], status="DRAFT")
        if payload.category_hint:
            blog.category = payload.category_hint
        db.add(blog)
        previous = None
    brief = payload.model_dump()
    brief["listings"] = [p.model_dump(exclude_none=True) for p in payload.listings]
    brief.pop("overnight", None)
    blog.ai_review = {"status": "running", "stage": "gathering", "engine": engine, "started_at": now, "stage_at": now,
                      "new": is_new, "previous": previous, "brief": _brief_store(payload.model_dump(mode="json"))}
    extra: dict = {}
    if overnight:
        import json
        from app.services.ai import blog_overnight_service
        from app.services.ai.ai_config_service import ai_config_service
        from app.services.ai.ai_rate_limit_service import monthly_limit_usd
        config = await ai_config_service.get_config(db)
        # The run reads its brief back from the row, so a run picked up after a restart reads the very same one.
        state = blog_overnight_service.new_state(brief, config, await _slow_minutes(db, engine))
        brief = json.loads(json.dumps(state["brief"]))
        blog.ai_review = {**blog.ai_review, "mode": "overnight", "overnight": state}
        now_usd, usd = await _article_estimate(db, config, engine, payload.length)
        now_usd = await _article_worst(db, engine, payload.length, now_usd)        # the top of the range: the worst case
        limit = await monthly_limit_usd(db, current_user.id, config, current_user.plan_tier)
        extra = {"mode": "overnight", "likely_at": state["likely_at"], "latest_at": state["latest_at"],
                 "cap_minutes": state["cap_minutes"], "max_minutes": state["cap_minutes"] + blog_overnight_service.FAST_FINISH_MINUTES,
                 # what it uses: this at best (every step answered in the slower line), `percent_max` at worst (the
                 # time cap finished it the fast way)
                 "percent": round(usd / float(limit) * 100, 2) if limit else None,
                 "percent_max": round(now_usd / float(limit) * 100, 2) if limit else None}
    await db.commit()
    await db.refresh(blog)

    task = asyncio.create_task(_run_blog_ai_job(blog.id, current_user.id, brief, engine, overnight=overnight))
    _AI_JOBS.add(task)
    task.add_done_callback(_AI_JOBS.discard)
    return {"blog_id": blog.id, "status": "running", "stage": "gathering", "engine": engine, **extra}


async def _writer_probe(user: User, names: list[str], slugs: list[str]) -> dict:
    """The run's first, free step on a short leash: find the towns and pull the first one's numbers,
    each database look-up allowed 20 seconds (the slowest, a whole-table count, takes ~10 s when it
    isn't cached yet). Slower than that means the database is overloaded and a real run would sit on
    "Gathering" - better to say so before the agent presses Write. It also warms the caches the
    real run reads (in this server process)."""
    import asyncio
    import time
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    from app.services.areas import town_knowledge_service as tk
    from app.utils.auth import tier_has_mls

    out = {"busy": False, "unknown": [], "seconds": 0.0, "outside": [], "counties": []}
    t0 = time.monotonic()

    async def probe() -> None:
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(text("SET LOCAL statement_timeout = 20000"))
                state = (getattr(user, "state", None) or "PA").strip().upper()[:2]
                areas = list(await tk.find_areas(db, slugs=slugs[:5])) if slugs else []
                from app.services.ai import ai_blog_pipeline_service as _pipe
                out["counties"] = [str(n).split(",")[0].strip() for n in names[:5] if _pipe.is_county(n)]
                for raw in [n for n in names[:5] if not _pipe.is_county(n)]:
                    # the same town lookup the article run itself uses (ai_blog_pipeline_service -> find_areas)
                    found = await tk.find_areas(db, names=[str(raw)], state=state)
                    a = found[0] if found else None
                    if a:
                        areas.append(a)
                    # Typing a town makes an empty record for it right away (the Spotlight look-up),
                    # so "on file" means researched - anything else gets the "new to us" note.
                    if not a or a.enrichment_status not in ("ENRICHED", "PARTIAL"):
                        out["unknown"].append(str(raw).split(",")[0].strip())
                # towns in a state the agent doesn't work in -> the writer asks whether they are licensed there
                out["outside"] = (await _pipe.out_of_area_states(db, user, {(a.state or "").upper() for a in areas if a.state}))[0]
                known = [a for a in areas if a.enrichment_status in ("ENRICHED", "PARTIAL")] or areas
                if known:
                    t_dossier = time.monotonic()
                    looked = await tk.town_dossier(db, known[0], include_bank=False, mls=tier_has_mls(user.plan_tier))
                    # The town's sales look-up swallows its own time-outs (it returns no numbers instead). Coming back
                    # empty after most of the 20-second limit is "busy", not "ready" (11/15: the card said ready, the
                    # log said the look-up had timed out).
                    if tier_has_mls(user.plan_tier) and not (looked or {}).get("sales_snapshot") and time.monotonic() - t_dossier > 15:
                        out["busy"] = True
            except Exception as exc:
                msg = str(exc).lower()
                if "statement timeout" in msg or "canceling statement" in msg:
                    out["busy"] = True
                else:  # our own bug must never block writing - log it and let the run go ahead
                    logger.warning("blog writer preflight probe failed", exc_info=True)
            finally:
                try:
                    await db.rollback()
                except Exception:
                    pass

    try:
        await asyncio.wait_for(probe(), timeout=45)
    except asyncio.TimeoutError:
        out["busy"] = True
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


@router.post("/ai/preflight")
async def blog_ai_preflight(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """"Ready to write?" - checked on the writer's last step and again on Write (owner 11/13: "the
    flow catches possible issues before they send it and have to wait forever"). Free and quick:
    the AI allowance and MLS data (the same check Write does), the towns, and whether the database
    is answering fast enough for the run's first step. `ready` is false only for something that
    would stop the run; towns we've never covered are a note, not a stop."""
    from app.services.ai import ai_blog_pipeline_service
    checks: list[dict] = []
    try:
        await ai_blog_pipeline_service.precheck(db, current_user, payload.get("engine"))
        checks.append({"key": "allowance", "level": "ok", "title": "Enough AI usage left this month"})
    except HTTPException as exc:
        checks.append({"key": "allowance", "level": "block", "title": "Can't write this one right now", "detail": str(exc.detail)})
    names = [str(a)[:80] for a in (payload.get("areas") or []) if str(a).strip()][:5]
    slugs = [str(s)[:120] for s in (payload.get("area_slugs") or []) if str(s).strip()][:5]
    probe = await _writer_probe(current_user, names, slugs)
    if probe["busy"]:
        checks.append({"key": "servers", "level": "block", "title": "Our servers are very busy right now",
                       "detail": "Writing would stall on its first step. Your answers are saved - check again in a few minutes."})
    else:
        checks.append({"key": "servers", "level": "ok", "title": "Our servers are ready"})   # how long it takes is shown with each way of writing it
    if probe["unknown"]:
        towns = ", ".join(probe["unknown"])
        checks.append({"key": "towns", "level": "warn", "title": f"New to us: {towns}",
                       "detail": "The AI researches it from scratch - a little slower, with fewer local numbers. Check the spelling if that's a typo."})
    elif probe.get("counties") and not slugs and len(probe["counties"]) == len(names):
        checks.append({"key": "towns", "level": "ok", "title": f"A county article: {probe['counties'][0]}",
                       "detail": "Your own towns inside the county are its examples. We don't hold county-wide totals, so each town's own numbers are used."})
    elif names or slugs:
        checks.append({"key": "towns", "level": "ok", "title": "Your towns are on file"})
    if probe.get("outside"):
        where = ai_blog_pipeline_service.state_names(probe["outside"])
        checks.append({"key": "license", "level": "ask", "title": f"Are you licensed in {where}?", "states": probe["outside"],
                       "detail": f"This town is outside the states you work in. If you aren't licensed in {where}, the article will say so and offer to "
                                 "introduce the reader to an agent who is, instead of offering your own services there."})
    logger.info("blog writer preflight", extra={"owner_id": current_user.id, "busy": probe["busy"], "seconds": probe["seconds"]})
    return {"ready": not any(c["level"] == "block" for c in checks), "checks": checks, "seconds": probe["seconds"]}


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
        state, review = "failed", {**review, "error": "The AI writer took too long - please try again. Nothing was charged for this try."}
        # The run died part-way (e.g. a restart) - give back what its finished steps charged.
        from app.services.ai import ai_blog_pipeline_service
        if review.get("mode") == "overnight":
            from app.services.ai import blog_overnight_service
            ov = review.get("overnight") or {}
            await blog_overnight_service.refund_events(db, [i for ids in (ov.get("events") or {}).values() for i in ids])
            blog = await get_owned_blog(db, blog_id, current_user.id)
        else:
            await ai_blog_pipeline_service.refund_since(db, current_user.id, review.get("started_at"))
    if state == "interview":
        return {"status": "interview", "brief": review.get("brief"), "step": review.get("step")}
    if state == "running":
        out = {"status": "running", "stage": review.get("stage"), "engine": review.get("engine"), "started_at": review.get("started_at")}
        if review.get("mode") == "overnight":
            from app.services.ai import blog_overnight_service
            out.update(blog_overnight_service.public_state(review))
        return out
    if state == "failed":
        error = review.get("error") or "The AI writer could not finish this draft."
        # A brand-new article stays a DRAFT holding the agent's answers (open it to try again - user,
        # 10/17); a rewrite keeps its old content and report.
        if review.get("new"):
            blog.ai_review = {"status": "failed", "new": True, "error": error, "brief": review.get("brief") or {},
                              "step": 3, "saved_at": review.get("stage_at")}
        else:
            blog.ai_review = review.get("previous")
        await _mark_ai_alert_seen(db, blog.id, current_user.id)
        await db.commit()
        return {"status": "failed", "error": error, "brief": review.get("brief")}
    await _mark_ai_alert_seen(db, blog.id, current_user.id)
    await db.commit()
    await db.refresh(blog)
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"The Luxe article theme is part of the {luxe_upgrade_label(current_user)} plan.")

    old_cover_url = blog.cover_image_url
    for field, value in data.items():
        setattr(blog, field, value)
    # The fact-check covered the words the AI wrote. Once the agent changes them the report says so
    # (and says "verified" again if they put the text back).
    review = blog.ai_review if isinstance(blog.ai_review, dict) else None
    if "blocks" in data and review and review.get("status") == "done" and review.get("text_hash"):
        edited = _checked_text_hash(data["blocks"]) != review["text_hash"]
        if bool(review.get("edited")) != edited:
            blog.ai_review = {**review, "edited": edited}

    await db.commit()
    await db.refresh(blog)

    # A changed cover image orphaned the old file (in S3 and in the storage ledger) - same
    # release-old-then-record-new pattern as brand logos/agent photos.
    if "cover_image_url" in data and old_cover_url and old_cover_url != blog.cover_image_url:
        await asyncio.to_thread(s3_service.delete_by_url, old_cover_url)
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
        await asyncio.to_thread(s3_service.delete_by_url, cover_url)
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
    hub = await hub_by_slug(db, normalize_slug(agent_slug))  # current link or its kept old one
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


async def article_site_url(db: AsyncSession, agent: User, blog: Blog) -> Optional[str]:
    """The article's address on the agent's live IDX website (only when it's shown there). Then that
    copy is the one Google lists; the openhousepal.com copy points at it (owner, 10/30)."""
    if not blog.idx_visible:
        return None
    base, _ = await blog_neighborhoods.website_base_url(db, agent)
    return f"{base}/blog/{blog.slug}" if base else None


async def _article_contact(db: AsyncSession, agent: User) -> dict:
    from app.services.website import content_adapters
    return await content_adapters.article_contact(db, agent)


async def _public_blocks(db: AsyncSession, blocks) -> list:
    from app.services.ai import ai_blog_writer_service
    try:
        return await ai_blog_writer_service.fill_spotlight_photos(db, blocks or [])
    except Exception:
        logger.warning("spotlight photo fill failed", exc_info=True)
        return blocks or []


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
    hub = await hub_by_slug(db, normalize_slug(agent_slug))  # current link or its kept old one
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

    # The author reading their own article (signed in) isn't a reader - anyone else counts
    # (owner, 11/13 - utils/own_traffic).
    from app.utils.own_traffic import is_own_traffic
    if track and not is_own_traffic(viewer, blog.owner_id):
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
        blocks=await _public_blocks(db, blog.blocks),
        linkedProperties=blog.linked_properties or [],
        seoTitle=blog.seo_title,
        metaDescription=blog.meta_description,
        socialTitle=blog.social_title,
        socialDescription=blog.social_description,
        socialImageUrl=blog.social_image_url,
        isIndexed=blog.is_indexed,
        siteUrl=await article_site_url(db, agent, blog),
        publishedAt=blog.published_at,
        readingTimeMinutes=estimate_reading_time(blog.blocks),
        agentName=f"{agent.first_name} {agent.last_name}".strip(),
        agentBrokerage=agent.brokerage,
        agentPhotoUrl=agent.photo_url,
        agentId=agent.id,
        agentTitle=agent.professional_title,
        agentMarket=agent.market,
        **{f"agent{k.title().replace('_', '')}": v for k, v in (await _article_contact(db, agent)).items()},
        # A Luxe article whose author left IDX shows in the default look (the choice is kept).
        theme=blog.theme if blog.theme != "luxe_plus" or luxe_blog_theme_allowed(agent) else "luxe",
        previewImageUrl=blog_preview_image(blog),
        related=await _related_articles(db, blog),
        signatureCard=bool(blog.use_signature_card) and await email_signature_service.has_signature_card(db, agent),
        areaTowns=_article_towns(blog),
        neighborhoodLinks=await blog_neighborhoods.neighborhood_links(db, agent, blog.linked_areas),
        luxeMetal=blog.luxe_metal,
        accentColor=blog.accent_color,
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
    await check_public_submit(request)
    name = (payload.name or "").strip()[:120]
    email = (payload.email or "").strip()[:200] or None
    phone = (payload.phone or "").strip()[:40] or None
    message = (payload.message or "").strip()[:2000] or None
    if not name or not (email or phone):
        raise HTTPException(status_code=422, detail="Add your name and an email or phone number.")
    from app.services.forms_service import _EMAIL_RE
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    hub = await hub_by_slug(db, normalize_slug(agent_slug))  # current link or its kept old one
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
    owner_ref: bool = False,
):
    # A "view" ping comes from the IDX website's article page; like the Agent Hub read, the agent
    # checking their own article isn't a reader - nor are their own clicks on it.
    if owner_ref:
        return
    hub = await hub_by_slug(db, normalize_slug(agent_slug))  # current link or its kept old one
    if not hub:
        return
    from app.utils.own_traffic import is_own_traffic
    if is_own_traffic(viewer, hub.owner_id):
        return

    blog_result = await db.execute(
        select(Blog).where(Blog.owner_id == hub.owner_id, Blog.slug == normalize_slug(blog_slug))
    )
    blog = blog_result.scalar_one_or_none()
    if not blog:
        return

    db.add(BlogEvent(blog_id=blog.id, owner_id=blog.owner_id, event_type=payload.event_type, meta=payload.meta))
    await db.commit()
