import os
import logging
import secrets
from pydantic import BaseModel
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.models.database import AgentMarketArea, MarketArea, Site, SiteDomain, User, PlanTier
from app.schemas.areas import AgentMarketAreaCreate, AgentMarketAreaResponse
from app.schemas.site import (
    SiteDomainCreate,
    SiteDomainResponse,
    SiteDraftUpdate,
    SitePreviewRequest,
    SiteResponse,
)
from app.services.areas import area_enrichment_service, boundary_sources, geocoding
from app.services.website import content_adapters, domain_resolution_service, domain_verification_service, site_service, starter_template
from app.services.website.site_service import InvalidHostname
from app.utils.auth import get_current_active_user, require_plan_tier

router = APIRouter(prefix="/studio/sites", tags=["website-builder"])
logger = logging.getLogger(__name__)
MAX_SERVED_AREAS_PER_AGENT = 15
SERVED_AREA_LIMIT_MESSAGE = "You can add up to 15 cities, towns, neighborhoods, or market areas to your website."


async def _lock_agent_served_areas(db: AsyncSession, owner_id: str) -> None:
    """Serialize this agent's add-area requests so concurrent inserts cannot both pass the cap."""
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"agent_market_areas:{owner_id}"))))


def _validate_served_area_slot(existing_count: int, existing_link: AgentMarketArea | None) -> None:
    if existing_link:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already serve this area.")
    if existing_count >= MAX_SERVED_AREAS_PER_AGENT:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=SERVED_AREA_LIMIT_MESSAGE)


def _domain_response(domain: SiteDomain) -> SiteDomainResponse:
    """instructions is computed, not stored - attached here rather than via ORM attribute
    access so response_model=SiteDomainResponse can't accidentally serve stale/omitted
    instructions if the model is ever returned directly elsewhere."""
    response = SiteDomainResponse.model_validate(domain)
    response.instructions = domain_verification_service.build_instructions(domain.hostname)
    return response


async def _load_owned_site(db: AsyncSession, owner_id: str) -> Site:
    result = await db.execute(select(Site).where(Site.owner_id == owner_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")
    return site


async def _load_owned_domain(db: AsyncSession, domain_id: str, owner_id: str) -> SiteDomain:
    result = await db.execute(
        select(SiteDomain).where(SiteDomain.id == domain_id, SiteDomain.owner_id == owner_id)
    )
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    return domain


@router.get("/me", response_model=SiteResponse)
async def get_my_site(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Get-or-create: opening the editor for the first time is what auto-generates the
    subdomain, matching the product's "auto-generated website" framing."""
    site = await site_service.get_or_create_site(db, current_user)
    response = SiteResponse.model_validate(site)
    response.signature_hub_slug = await content_adapters.signature_hub_slug_for(db, current_user)
    return response


@router.get("/me/public-base-url")
async def get_my_public_base_url(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """The single source of truth for "what link do I hand out right now" - sign-in QR codes,
    cheat sheets, forms, and showcases all resolve through the same site_service function this
    calls, so nothing in the product should ever compute or show a second, different link."""
    base_url = await site_service.resolve_owner_public_base_url(db, current_user)
    site = await site_service.get_or_create_site(db, current_user)
    platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    # is_idx_site: base_url is the agent's IDX website (so IDX-only paths like /blog/<slug> exist
    # there) - false when links use openhousepal.com (site switched off / unpublished, or its
    # address doesn't open yet - see resolve_owner_public_base_url).
    on_own_site = site.is_enabled is not False and site.status == "PUBLISHED" and base_url.rstrip("/") != platform
    return {"base_url": base_url, "is_idx_site": on_own_site}


class LinkDomainUpdate(BaseModel):
    use_own_domain: bool


class SiteEnabledUpdate(BaseModel):
    enabled: bool


async def _link_domain_state(db: AsyncSession, user: User) -> dict:
    """Settings -> IDX Website state. Three distinct addresses, never conflated:
      - default_base_url: the free <slug>.openhousepal.com every IDX agent gets automatically
      - custom_domain: a domain the agent uploaded/connected themselves - only once ACTIVE
      - platform_base_url: plain openhousepal.com (used when the IDX site is switched off)
    `use_own_domain` is only ever true when a real custom domain is connected AND the agent
    wants their links on it - the default subdomain is never "your own domain"."""
    import os
    site = await site_service.get_or_create_site(db, user)
    custom = await site_service.connected_custom_domain(db, site)
    platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000"))
    prefers_own = user.links_use_own_domain is not False
    return {
        "site_enabled": site.is_enabled is not False,
        "has_custom_domain": custom is not None,
        "custom_domain": custom,
        "use_own_domain": bool(custom) and prefers_own,
        "own_base_url": f"https://{custom}" if custom else None,
        "default_base_url": f"https://{site_service.default_hostname(site)}",
        "platform_base_url": platform,
        "current_base_url": await site_service.resolve_owner_public_base_url(db, user),
    }


@router.get("/me/link-domain")
async def get_link_domain(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Settings -> IDX Domain: which address new QR codes and copied links use."""
    return await _link_domain_state(db, current_user)


@router.put("/me/link-domain")
async def set_link_domain(
    body: LinkDomainUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Switch new links between the agent's own domain and openhousepal.com. Both keep working;
    active open houses' sign-in QR codes are re-pointed at the chosen one."""
    current_user.links_use_own_domain = body.use_own_domain
    db.add(current_user)
    await db.commit()
    await site_service.regenerate_owner_idx_links(db, current_user)
    return await _link_domain_state(db, current_user)


@router.put("/me/site-enabled")
async def set_site_enabled(
    body: SiteEnabledUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """IDX website on/off (Settings -> IDX Website, and the post-upgrade question). Off = the
    public site isn't served and every link falls back to openhousepal.com; on = back exactly
    as it was. Returns the same state as GET /me/link-domain."""
    await site_service.set_site_enabled(db, current_user, body.enabled)
    return await _link_domain_state(db, current_user)


@router.post("/me/idx-domain-migration/agree")
async def agree_to_idx_domain_migration(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """One-time acknowledgement shown the first time an IDX agent would otherwise silently start
    seeing their own domain appear on newly-created sign-ins/cheat sheets/forms/showcases.
    Idempotent - agreeing twice is a no-op, never overwrites the original timestamp."""
    from datetime import datetime, timezone
    if current_user.idx_domain_migration_agreed_at is None:
        current_user.idx_domain_migration_agreed_at = datetime.now(timezone.utc)
        db.add(current_user)
        await db.commit()
    return {"agreed_at": current_user.idx_domain_migration_agreed_at}


@router.get("/me/content-options")
async def get_my_site_content_options(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Feeds the editor's Form/Showcase pickers ("Form: [Buyer Consultation v]") - a thin list
    of what already exists in Forms/Showcases, not a second management surface. The editor adds
    a *reference* to one of these; editing the form/showcase itself still only happens in
    /studio/forms or /studio/showcases."""
    return {
        "forms": await content_adapters.get_forms_list(db, current_user),
        "showcases": await content_adapters.get_showcases(db, current_user),
    }


@router.get("/me/seo-pages")
async def get_my_seo_pages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Everything the IDX editor's Google tab needs to preview EVERY page as it shows on Google -
    the same facts the public site builds its titles/descriptions from (frontend
    lib/siteSeoAuto.ts): the agent, each neighborhood with its live numbers, each published
    article's own SEO fields, the Featured homes and the review summary."""
    from app.models.database import Blog
    site = await site_service.get_or_create_site(db, current_user)
    profile = await content_adapters.get_agent_profile(db, current_user)
    featured_settings = (site.draft_data or {}).get("featured_settings") or (site.published_snapshot or {}).get("featured_settings") or {}
    posts = (await db.execute(
        select(Blog)
        .where(Blog.owner_id == current_user.id, Blog.status == "PUBLISHED", Blog.idx_visible.is_(True))
        .order_by(Blog.published_at.desc().nullslast())
        .limit(60)
    )).scalars().all()
    reviews = await content_adapters.get_site_reviews(db, current_user, limit=0)
    try:
        featured = await content_adapters.get_featured_page_listings(db, current_user, featured_settings, 12)
    except Exception:
        logger.warning("SEO pages: featured listings lookup failed", exc_info=True)
        featured = []
    return {
        "agent": {k: profile.get(k) for k in ("name", "brokerage", "market", "phone", "title", "bio")},
        "areas": await content_adapters.get_served_areas_summary(db, current_user, 50, with_stats=True),
        "posts": [{
            "id": b.id, "slug": b.slug, "title": b.title, "subtitle": b.subtitle,
            "seo_title": b.seo_title, "meta_description": b.meta_description, "is_indexed": b.is_indexed,
            "cover_image_url": b.cover_image_url,
            "published_at": b.published_at.isoformat() if b.published_at else None,
        } for b in posts],
        "featured": featured,
        "reviews": reviews["summary"] if reviews.get("show") else None,
        # The rating Google may get (None = nothing sent) and why not, for the Google tab's switch.
        "google_rating": content_adapters.google_rating(reviews),
        "review_filter_on": reviews.get("min_rating", 1) > 1,
        "published": site.status == "PUBLISHED",
    }


@router.patch("/me/draft", response_model=SiteResponse)
async def update_my_site_draft(
    payload: SiteDraftUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    site = await _load_owned_site(db, current_user.id)
    data = payload.model_dump(exclude_unset=True)
    draft = dict(site.draft_data or {})
    if "pages" in data:
        draft["pages"] = [page for page in data["pages"]]
    if "theme" in data:
        draft["theme"] = data["theme"]
    if "featured_settings" in data:
        draft["featured_settings"] = data["featured_settings"]
    if "media_settings" in data:
        draft["media_settings"] = data["media_settings"]
    if "seo" in data:
        draft["seo"] = data["seo"]
    site.draft_data = draft
    await db.commit()
    await db.refresh(site)
    return site


@router.post("/me/preview")
async def preview_my_site_blocks(
    payload: SitePreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Hydrates the editor's current in-memory (possibly unsaved) blocks the exact same way the
    public renderer hydrates a published page - see content_adapters.hydrate_page_blocks - so
    WebsiteEditorShell's live preview can show real resolved data (rotating hero photos, agent
    profile, featured listings, communities, etc.) instead of raw unresolved block data, without
    requiring a save first."""
    page = {"blocks": [block.model_dump() for block in payload.blocks]}
    featured_settings = payload.featured_settings.model_dump() if payload.featured_settings else None
    hydrated = await content_adapters.hydrate_page_blocks(db, current_user, page, featured_settings)
    return {"blocks": hydrated["blocks"]}


@router.post("/me/apply-template", response_model=SiteResponse)
async def apply_my_site_template(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Composes Template 1's fixed page/block structure from this agent's real OHP data
    (profile, listings, sales, open houses, blog, served areas, forms - see starter_template.py)
    into the draft. Safe to call repeatedly - never overwrites a block the agent has hand-edited
    since the last apply (see starter_template._merge_page). Draft only, never auto-publishes."""
    site = await _load_owned_site(db, current_user.id)
    site.draft_data = await starter_template.apply_starter_template(db, site, current_user)
    await db.commit()
    await db.refresh(site)
    return site


@router.post("/me/publish", response_model=SiteResponse)
async def publish_my_site(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    site = await _load_owned_site(db, current_user.id)
    return await site_service.publish_site(db, site)


@router.get("/me/domains/health")
async def get_my_domain_health(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """What's wrong with the agent's own domain right now, with the fix (Dash banner + Domain
    screen). Any plan - [] when there's nothing to say (see domain_health_service)."""
    from app.services.website.domain_health_service import domain_issues
    return {"issues": await domain_issues(db, current_user.id)}


@router.get("/me/domains/provider")
async def get_domain_provider(hostname: str, current_user: User = Depends(get_current_active_user)):
    """Which company runs this domain (from its nameservers) - the setup guide opens on its steps."""
    from app.services.website.domain_provider_service import detect_provider
    return {"provider": await detect_provider((hostname or "").strip().lower()[:253])}


@router.get("/me/domains", response_model=List[SiteDomainResponse])
async def get_my_site_domains(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    result = await db.execute(
        select(SiteDomain).where(SiteDomain.owner_id == current_user.id).order_by(SiteDomain.created_at.desc())
    )
    return [_domain_response(d) for d in result.scalars().all()]


@router.post("/me/domains", response_model=SiteDomainResponse, status_code=status.HTTP_201_CREATED)
async def create_my_site_domain(
    payload: SiteDomainCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    site = await _load_owned_site(db, current_user.id)
    try:
        hostname = site_service.validate_custom_hostname(payload.hostname)
    except InvalidHostname as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # "janesmithhomes.com" or "www.janesmithhomes.com" -> the pair: the www address is the website
    # (a CNAME works at every domain company), the bare name forwards to it. A subdomain they
    # chose (homes.janesmith.com) is just itself.
    from app.services.website.domain_verification_service import apex_of, build_instructions, is_www
    pair = len(build_instructions(hostname)) > 1
    main_host = f"www.{apex_of(hostname)}" if pair else hostname
    hosts = [main_host] + ([apex_of(hostname)] if pair else [])

    existing = await db.execute(select(SiteDomain.id).where(SiteDomain.hostname.in_(hosts)))
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That domain is already connected to a site.")

    domain = SiteDomain(
        site_id=site.id,
        owner_id=current_user.id,
        hostname=main_host,
        verification_token=secrets.token_hex(16),
    )
    db.add(domain)
    await db.flush()
    if pair:
        db.add(SiteDomain(site_id=site.id, owner_id=current_user.id, hostname=apex_of(hostname),
                          verification_token=secrets.token_hex(16), redirect_target_domain_id=domain.id))
    await db.commit()
    await db.refresh(domain)
    # Cloudflare can start on the certificate right away; it finishes when their record appears.
    from app.services.website import cloudflare_saas_service
    await cloudflare_saas_service.register(main_host)
    return _domain_response(domain)


@router.post("/me/domains/{domain_id}/check", response_model=SiteDomainResponse)
async def check_my_site_domain(
    domain_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """The "Check Connection" button - runs the same DNS/HTTPS check the background poller
    runs, synchronously, so an agent doesn't have to wait for the next poll cycle after
    fixing their DNS."""
    domain = await _load_owned_domain(db, domain_id, current_user.id)
    domain = await domain_verification_service.verify_domain(db, domain)
    return _domain_response(domain)


@router.post("/me/domains/{domain_id}/primary", response_model=SiteDomainResponse)
async def make_my_site_domain_primary(
    domain_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    site = await _load_owned_site(db, current_user.id)
    domain = await _load_owned_domain(db, domain_id, current_user.id)
    try:
        await site_service.set_primary_domain(db, site, domain, current_user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await db.refresh(domain)
    return _domain_response(domain)



@router.delete("/me/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_site_domain(
    domain_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    domain = await _load_owned_domain(db, domain_id, current_user.id)
    site_id = domain.site_id
    was_primary = domain.is_primary
    # its bare-domain partner goes with it
    partners = (await db.execute(select(SiteDomain).where(SiteDomain.redirect_target_domain_id == domain.id))).scalars().all()
    from app.services.website import cloudflare_saas_service
    for d in [*partners, domain]:  # partners first - they point at the main row
        await cloudflare_saas_service.remove(d.hostname)
        await db.delete(d)
    await db.commit()
    domain_resolution_service.invalidate_site(site_id)
    if was_primary:
        # Canonical hostname just fell back to the OHP subdomain - active kits should follow.
        await site_service.regenerate_owner_idx_links(db, current_user)


async def _enrich_area_background(area_id: str, triggering_user_id: str):
    """Runs after the response is already sent (see POST /me/areas) - opens its own DB session
    since the request-scoped one is gone by the time BackgroundTasks fires, same standalone-
    session pattern as domain_verification_scheduler.py. Never raises into the caller - a
    failed enrichment just leaves the area PENDING/PARTIAL for the next attempt, same as any
    other best-effort background job in this app."""
    async with AsyncSessionLocal() as db:
        area = (await db.execute(select(MarketArea).where(MarketArea.id == area_id))).scalar_one_or_none()
        user = (await db.execute(select(User).where(User.id == triggering_user_id))).scalar_one_or_none()
        if not area or not user:
            return
        try:
            await area_enrichment_service.enrich_area(db, area, user)
        except Exception:
            logger.exception("Area enrichment failed", extra={"area_id": area_id})


@router.get("/me/areas", response_model=List[AgentMarketAreaResponse])
async def get_my_served_areas(
    lite: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """lite=1: names + research status only (the setup town picker polls this every few seconds
    while a town is researched - each town's full facts/boundary is ~50 KB it doesn't need)."""
    result = await db.execute(
        select(AgentMarketArea)
        .where(AgentMarketArea.owner_id == current_user.id)
        .order_by(AgentMarketArea.created_at.desc())
    )
    links = result.scalars().all()
    for link in links:
        await db.refresh(link, attribute_names=["area"])
    if not lite:
        return links
    out = []
    for link in links:
        a = AgentMarketAreaResponse.model_validate(link)
        a.area.facts, a.area.geography, a.area.narrative = {}, {}, None
        out.append(a)
    return out


@router.post("/me/areas", response_model=AgentMarketAreaResponse, status_code=status.HTTP_201_CREATED)
async def add_my_served_area(
    payload: AgentMarketAreaCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Get-or-create the shared MarketArea (instant, zero API calls, if another agent already
    researched this town), link it to this agent, and - only for a genuinely new (or previously
    FAILED) area, and only for whichever concurrent caller wins the atomic claim below - enqueue
    real enrichment in the background so this request returns immediately. The editor polls
    GET /me/areas afterward to see enrichment_status move PENDING -> RESEARCHING -> ENRICHED/PARTIAL.

    area_type='neighborhood' (the "Add Market Area / Neighborhood" flow) resolves the agent's
    free-text name/state to a canonical name, parent city, and centroid BEFORE any DB lookup or
    creation - e.g. "Rittenhouse Square"/"PA" resolves to name="Rittenhouse Square",
    parent_area_name="Philadelphia", state="PA", plus real coordinates - so the stored row is
    canonical from the start rather than whatever free text the agent happened to type, and so
    enrichment never has to lazily geocode it later (see get_or_create_market_area)."""
    name, state = payload.name.strip(), payload.state.strip().upper()
    if not name or len(state) != 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A town name and 2-letter state are required.")

    area_type = payload.area_type if payload.area_type in ("city", "neighborhood") else "city"
    parent_area_name = None
    centroid = None
    geography_definition = None

    if area_type == "neighborhood":
        resolved = await geocoding.resolve_named_place(name, state)
        if not resolved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Couldn't find that market area or neighborhood - check the spelling, or try including the nearest city (e.g. \"Rittenhouse Square, Philadelphia\").",
            )
        name, state = resolved["name"], resolved["state"]
        parent_area_name = resolved.get("parent_city")
        centroid = (resolved["lat"], resolved["lng"])
        geography_definition = (
            await boundary_sources.resolve_named_area_boundary(name, parent_area_name, state)
            or resolved.get("geography")
        )

    area = await area_enrichment_service.get_or_create_market_area(
        db,
        name,
        state,
        area_type=area_type,
        parent_area_name=parent_area_name,
        centroid=centroid,
        geography_definition=geography_definition,
    )

    await _lock_agent_served_areas(db, current_user.id)
    existing_link = (await db.execute(
        select(AgentMarketArea).where(AgentMarketArea.owner_id == current_user.id, AgentMarketArea.area_id == area.id)
    )).scalar_one_or_none()
    existing_count = (await db.execute(
        select(func.count(AgentMarketArea.id)).where(AgentMarketArea.owner_id == current_user.id)
    )).scalar_one()
    _validate_served_area_slot(existing_count, existing_link)

    link = AgentMarketArea(owner_id=current_user.id, area_id=area.id)
    db.add(link)
    await db.commit()
    await db.refresh(link)
    await db.refresh(link, attribute_names=["area"])

    # Atomic claim, not a plain read-then-branch - two agents adding the same brand-new town
    # within the same window could otherwise both see it as needing research and both enqueue a
    # full (now AI-research-costed) enrichment run. This UPDATE only ever succeeds for exactly
    # one of them; only the caller whose claim actually changed a row enqueues the background job.
    # Runs on a fresh SELECT within the same request/session as everything above so
    # `area.enrichment_status` in memory may already be stale by the time this executes - that's
    # fine, the WHERE clause (not the in-memory value) is what decides.
    #
    # Deliberately gated on enrichment_status alone (PENDING/FAILED = genuinely never
    # successfully researched) - NOT on research_version staleness. A future bump to
    # area_research_service.CURRENT_RESEARCH_VERSION must never cause an ordinary agent normally
    # selecting an already-ENRICHED area to silently re-trigger a paid research run; refreshing a
    # stale-version area is an explicit, platform-controlled operation only (admin re-enrich - see
    # admin_routes.re_enrich_area - or a maintenance backfill script; see
    # area_enrichment_service.needs_research, still used by those explicit paths).
    claim = await db.execute(
        update(MarketArea)
        .where(
            MarketArea.id == area.id,
            MarketArea.enrichment_status != "RESEARCHING",
            MarketArea.enrichment_status.in_(["PENDING", "FAILED"]),
        )
        .values(enrichment_status="RESEARCHING")
    )
    await db.commit()
    if claim.rowcount:
        background_tasks.add_task(_enrich_area_background, area.id, current_user.id)
        await db.refresh(link, attribute_names=["area"])  # so the response reflects RESEARCHING, not the stale pre-claim status

    return link


@router.delete("/me/areas/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_served_area(
    link_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.IDX)),
):
    """Removes only this agent's claim to serve the area - the shared MarketArea row (and any
    other agent's link to it) is never touched."""
    link = (await db.execute(
        select(AgentMarketArea).where(AgentMarketArea.id == link_id, AgentMarketArea.owner_id == current_user.id)
    )).scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Area not found")
    await db.delete(link)
    await db.commit()
