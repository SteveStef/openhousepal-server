import re
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select
from app.services.showcase_links import share_match
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import AgentForm, AgentHub, AgentHubEvent, AgentMarketArea, CmaReport, Collection, MarketArea, Property, Site, SiteDomain, User
from app.schemas.forms import ExampleProperty, FormProgressUpdate, FormSubmissionCreate, FormSubmissionResponse, PublicAgentFormResponse
from app.services import forms_service
from app.services.areas import geography, market_stats_service, market_insights_service
from app.services.ai.ai_config_service import ai_config_service
from app.services.nearby_amenities_service import is_explore_nearby_available
from app.api.public_nearby_shared import (
    explore_nearby_response as _explore_nearby_response,
    explore_place_details_response as _explore_place_details_response,
    visitor_device_id as _visitor_device_id,
)
from app.services.collections_service import CollectionsService
from app.services import email_signature_service
from app.services import blog_neighborhoods
from app.services import sold_branding
from app.services.website import content_adapters, domain_resolution_service, google_search_service
from app.utils.request_ip import get_client_ip as _client_ip
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public/sites", tags=["website-builder-public"])


@router.get("/photo/{photo_name:path}")
async def get_area_photo(photo_name: str):
    """Server-side proxy for Area photos (see area_enrichment_service._places_photos) - the
    Google API key is attached here, never sent to the browser. `photo_name` is a Places API
    resource path like 'places/XXX/photos/YYY', not user input used for anything but this."""
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    # Only a real Places photo name - anything else (../, other APIs) would spend our key on
    # whatever Google endpoint it pointed at.
    if not api_key or not re.fullmatch(r"places/[A-Za-z0-9_-]+/photos/[A-Za-z0-9_-]+", photo_name or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo unavailable.")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://places.googleapis.com/v1/{photo_name}/media",
                params={"maxWidthPx": 1200, "key": api_key},
                follow_redirects=True,
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo unavailable.")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "image/jpeg"), headers={"Cache-Control": "public, max-age=604800"})


_BLOG_COVER_CACHE: dict[str, tuple[str, bytes]] = {}


@router.get("/blog-cover/{blog_id}")
async def get_blog_cover(blog_id: str, db: AsyncSession = Depends(get_db)):
    """A published article's photo that was saved inline (base64 data: URL) - decoded and shrunk to
    a normal JPEG so public pages can reference it by address (content_adapters.public_image_url)
    instead of carrying half a megabyte of text in every page that lists the article."""
    from app.models.database import Blog
    blog = (await db.execute(select(Blog).where(Blog.id == blog_id, Blog.status == "PUBLISHED"))).scalar_one_or_none()
    raw = content_adapters.blog_photo_raw(blog) if blog else None
    if not raw or not raw.startswith("data:") or "," not in raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo unavailable.")
    key = f"{blog_id}:{len(raw)}:{raw[-32:]}"
    hit = _BLOG_COVER_CACHE.get(blog_id)
    if not hit or hit[0] != key:
        import base64, io
        try:
            data = base64.b64decode(raw.split(",", 1)[1])
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img = img.convert("RGB")
            img.thumbnail((1280, 1280))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=74, optimize=True, progressive=True)
            body = buf.getvalue()
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo unavailable.")
        if len(_BLOG_COVER_CACHE) > 200:
            _BLOG_COVER_CACHE.clear()
        _BLOG_COVER_CACHE[blog_id] = hit = (key, body)
    return Response(content=hit[1], media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def _find_page(pages: list, path: str) -> dict | None:
    """Mirrors the frontend's findPage() in [host]/[[...path]]/page.tsx exactly, so the page the
    backend hydrates is always the same one the frontend is about to render."""
    requested_slug = (path or "").strip("/")
    if requested_slug:
        return next((p for p in pages if p.get("slug") == requested_slug), None)
    return next((p for p in pages if p.get("is_home")), pages[0] if pages else None)


async def _published_site_owner(db: AsyncSession, site_id: str) -> tuple[Site, User] | None:
    """Shared ownership resolution for every site-scoped public sub-resource below (forms,
    blog, properties) - only ever resolves a PUBLISHED site, matching the resolve endpoint's
    own gate, so a draft/unpublished site's data is never reachable through these either."""
    result = await db.execute(select(Site).where(Site.id == site_id, Site.status == "PUBLISHED"))
    site = result.scalar_one_or_none()
    if not site or site.is_enabled is False:
        return None
    user = (await db.execute(select(User).where(User.id == site.owner_id))).scalar_one_or_none()
    if not _idx_owner_live(user):
        return None
    return site, user


def _idx_owner_live(user) -> bool:
    """The IDX website (and everything served under it) only stays live while its owner is on an
    active IDX plan - a downgrade or an ended subscription takes it down (the site itself is kept,
    so it comes straight back on upgrade/resubscribe)."""
    from app.utils.auth import owner_has_access
    return owner_has_access(user, {"IDX"})


def _is_eligible_showcase_example_sale(sale: dict) -> bool:
    status_text = str(sale.get("status") or "").replace("-BRIGHT", "").replace("_", " ").strip().lower()
    return ("sold" in status_text or "closed" in status_text) and (sale.get("price") or 0) >= 100000


def _showcase_example_properties(hub_config: dict, limit: int = 4) -> list[ExampleProperty] | None:
    raw_sales = hub_config.get("recentSales")
    if not isinstance(raw_sales, list):
        return None
    # The agent's own picks apply here too: only Shown sales, over their min price, in their order.
    eligible = [sale for sale in raw_sales if isinstance(sale, dict) and _is_eligible_showcase_example_sale(sale) and sale.get("visible", True) is not False]
    eligible = content_adapters.apply_sales_rules(sorted(eligible, key=lambda s: str(s.get("sold_date") or ""), reverse=True), content_adapters.sales_rules_from_config(hub_config))
    if not eligible:
        return None
    return [
        ExampleProperty(address=sale.get("address") or "", city=sale.get("city"), price=sale.get("price"), image=sale.get("image"))
        for sale in eligible[:limit]
    ]


async def _get_site_form(db: AsyncSession, site_id: str, form_ref: str) -> tuple[Site, User, AgentForm]:
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")
    site, user = owner
    form_result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == user.id,
            AgentForm.is_enabled.is_(True),
            or_(AgentForm.id == form_ref, AgentForm.slug == form_ref),
        )
    )
    form = form_result.scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")
    return site, user, form


async def _get_site_showcase_creator_form(db: AsyncSession, site_id: str) -> tuple[Site, User, AgentForm]:
    """Resolve the IDX site's locked Showcase Request form from the site owner only.

    The browser supplies a site-scoped URL, never an arbitrary agent id. If an older agent account
    has not loaded Agent Hub forms since the default showcase_request was added, reuse the same
    default-form seeding path Agent Hub uses so this remains the canonical AgentForm workflow.
    """
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This showcase creator could not be found.")
    site, user = owner

    result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == user.id,
            AgentForm.form_type == "showcase_request",
            AgentForm.is_enabled.is_(True),
        )
    )
    form = result.scalar_one_or_none()
    if not form:
        from app.api.forms_routes import get_or_create_forms

        forms = await get_or_create_forms(db, user.id)
        form = next((candidate for candidate in forms if candidate.form_type == "showcase_request" and candidate.is_enabled), None)
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This showcase creator could not be found.")
    return site, user, form


async def _get_site_default_form(db: AsyncSession, site_id: str, form_type: str) -> tuple[Site, User, AgentForm]:
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")
    site, user = owner

    result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == user.id,
            AgentForm.form_type == form_type,
            AgentForm.is_enabled.is_(True),
        )
    )
    form = result.scalar_one_or_none()
    if not form:
        from app.api.forms_routes import get_or_create_forms

        forms = await get_or_create_forms(db, user.id)
        form = next((candidate for candidate in forms if candidate.form_type == form_type and candidate.is_enabled), None)
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")
    if form.form_type == "how_can_i_help" and (form.title == "How Can I Help?" or form.headline == "How Can I Help?"):
        if form.title == "How Can I Help?":
            form.title = "How Can We Help"
        if form.headline == "How Can I Help?":
            form.headline = "How Can We Help"
        await db.commit()
        await db.refresh(form)
    return site, user, form


async def _public_form_response(db: AsyncSession, user: User, form: AgentForm, campaign_token: Optional[str] = None) -> PublicAgentFormResponse:
    from app.api.forms_routes import _agent_hub_is_live
    from app.services.forms_service import active_campaign_for_token, campaign_after_submit
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))
    hub = hub_result.scalar_one_or_none()
    # A campaign link (?c=) can send its visitors somewhere after submitting; the website's own
    # default is no Agent Hub redirect (the site page handles where to go).
    campaign_redirect, after_submit_mode, after_submit_url = campaign_after_submit(
        await active_campaign_for_token(db, form.id, campaign_token), bool(hub and _agent_hub_is_live(user, hub)))
    example_properties = _showcase_example_properties(hub.config or {}) if hub and form.form_type == "showcase_request" else None
    return PublicAgentFormResponse(
        form_id=form.id,
        form_type=form.form_type,
        slug=form.slug,
        title=form.title,
        headline=form.headline,
        subtitle=form.subtitle,
        questions=form.questions,
        success_heading=form.success_heading,
        success_message=form.success_message,
        accent_color=form.accent_color,
        show_branding=form.show_branding,
        dark_mode=form.dark_mode,
        redirect_to_agent_hub=bool(campaign_redirect),
        after_submit_mode=after_submit_mode,
        after_submit_url=after_submit_url,
        property_photo_carousel=form.property_photo_carousel,
        background_effects=getattr(form, "background_effects", True),
        welcome_text_effects=getattr(form, "welcome_text_effects", True),
        branding_layout=form.branding_layout,
        agent_name=f"{user.first_name} {user.last_name}".strip(),
        agent_brokerage=user.brokerage,
        agent_photo_url=user.photo_url,
        agent_title=user.professional_title,
        agent_market=user.market,
        agent_state=getattr(user, "state", None),
        linked_property=form.linked_property,
        example_properties=example_properties,
    )


async def _track_site_form_progress(db: AsyncSession, form: AgentForm, payload: FormProgressUpdate) -> None:
    """Same shared, race-free upsert as the Agent-Hub-scoped progress handler
    (forms_service.record_form_progress) - see its docstring for why this can't be a plain
    SELECT-then-branch."""
    await forms_service.record_form_progress(db, form, payload)


async def _assert_site_showcase(db: AsyncSession, site_id: str, share_token: str) -> None:
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found.")
    _site, user = owner
    result = await db.execute(
        select(Collection.id).where(
            share_match(share_token),
            Collection.owner_id == user.id,
            Collection.is_public.is_(True),
            Collection.status == "ACTIVE",
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found.")


async def _assert_site_cma(db: AsyncSession, site_id: str, share_slug: str) -> None:
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CMA not found.")
    _site, user = owner
    result = await db.execute(
        select(CmaReport.id).where(
            CmaReport.share_slug == share_slug,
            CmaReport.owner_id == user.id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CMA not found.")


_MLS_UPDATED_CACHE: dict = {}


@router.get("/mls-updated")
async def mls_data_updated(db: AsyncSession = Depends(get_db)):
    """When our MLS listing data last changed (the newest listing modification we've synced) -
    the "Data last updated" line Bright's IDX rules require under listing content. It used to
    print the visitor's own today's date, which says nothing about the data. Cached 5 minutes."""
    import time
    from sqlalchemy import func as sa_func
    from app.models.database import Property
    hit = _MLS_UPDATED_CACHE.get("v")
    if hit and time.monotonic() - hit[0] < 300:
        return {"updated_at": hit[1]}
    latest = (await db.execute(select(sa_func.max(Property.modification_timestamp)))).scalar()
    value = latest.isoformat() if latest else None
    _MLS_UPDATED_CACHE["v"] = (time.monotonic(), value)
    return {"updated_at": value}


@router.get("/warm-hosts")
async def list_warm_hosts(db: AsyncSession = Depends(get_db)):
    """Every live IDX site's address - the website server opens each one itself right after it
    starts (frontend instrumentation.ts), so no real visitor is ever the first, slow one after a
    deploy. Public on purpose: these are the agents' public website addresses."""
    from app.services.website.site_service import default_hostname
    rows = (await db.execute(
        select(Site).join(User, User.id == Site.owner_id)
        .where(Site.status == "PUBLISHED", Site.is_enabled.isnot(False), User.plan_tier == "IDX")
    )).scalars().all()
    hosts = [default_hostname(s) for s in rows if s.subdomain_slug]
    return {"hosts": hosts[:500]}


@router.get("/subdomain/{slug}/own-domain")
async def get_subdomain_own_domain(slug: str, db: AsyncSession = Depends(get_db)):
    """Where <slug>.openhousepal.com forwards to (frontend proxy.ts) - {"own_domain": null} = stay.
    Same rule as every link the agent hands out (site_service.resolve_owner_public_base_url): only
    an IDX agent's own domain that is ACTIVE + primary, with "use my own domain" on, on a published
    site that's switched on, and that actually opens. Any doubt or error = stay (fail open)."""
    try:
        from app.services.website import site_service
        site = (await db.execute(select(Site).where(Site.subdomain_slug == slug.strip().lower(), Site.status == "PUBLISHED"))).scalar_one_or_none()
        if not site:
            return {"own_domain": None}
        owner = await db.get(User, site.owner_id)
        if not owner or not _idx_owner_live(owner):
            return {"own_domain": None}
        base = await site_service.resolve_owner_public_base_url(db, owner)
        platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
        if not base or base.rstrip("/") == platform or not base.startswith("https://"):
            return {"own_domain": None}
        return {"own_domain": base.rstrip("/")}
    except Exception:  # noqa: BLE001
        logger.exception("subdomain own-domain lookup failed for %s", slug)
        return {"own_domain": None}


@router.get("/resolve")
async def resolve_site(
    host: str = Query(..., min_length=1),
    path: str = Query("", description="Requested page path, e.g. '' for home or 'about'"),
    db: AsyncSession = Depends(get_db),
):
    """The one hot path for every public site page view - called server-to-server from the
    Next.js (sitepublic) route group, never from the visitor's browser (see frontend
    proxy.ts / (sitepublic) route group), so this never needs CORS handling.

    Hydrates OHP-data-backed blocks (listings, sales, open houses, blog posts, agent profile)
    for ONLY the requested page, not every page in the site - see
    content_adapters.hydrate_page_blocks for why that bound matters."""
    resolved = await domain_resolution_service.resolve_hostname(db, host)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published site for that domain.")
    site_owner = (await db.execute(select(User).where(User.id == resolved.owner_id))).scalar_one_or_none()
    if not _idx_owner_live(site_owner):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published site for that domain.")
    # The agent switched their IDX website off (Settings -> IDX Website) - clean 404, same as no
    # site at all. Read live (not from the resolve cache) so the switch takes effect immediately.
    # google_search is read live too: a fresh Search Console code has to show within a minute.
    live = (await db.execute(select(Site.is_enabled, Site.google_search).where(Site.id == resolved.site_id))).first()
    if live is not None and live[0] is False:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published site for that domain.")
    google_search = (live[1] if live is not None else None) or {}

    snapshot = resolved.published_snapshot
    if not resolved.redirect_to and snapshot.get("pages"):
        page = _find_page(snapshot["pages"], path)
        if page:
            user = (await db.execute(select(User).where(User.id == resolved.owner_id))).scalar_one_or_none()
            if user:
                featured_settings = snapshot.get("featured_settings") or {}
                hydrated_page = await content_adapters.hydrate_page_blocks(db, user, page, featured_settings)
                theme = await content_adapters.resolve_theme(db, user, snapshot.get("theme") or {})
                snapshot = {
                    **snapshot,
                    "theme": theme,
                    "pages": [hydrated_page if p is page else p for p in snapshot["pages"]],
                }

    return {
        "site_id": resolved.site_id,
        "subdomain_slug": resolved.subdomain_slug,
        "published_snapshot": snapshot,
        "redirect_to": resolved.redirect_to,
        "canonical_hostname": resolved.canonical_hostname,
        # <meta name="google-site-verification"> on the home page + the IndexNow key file
        # (google_search_service) - getting the site into Google/Bing without agent steps.
        "google_verification": google_search_service.verification_codes(google_search),
        "indexnow_key": google_search_service.indexnow_key(resolved.site_id),
    }


@router.get("/{site_id}/redirects")
async def get_site_old_link(site_id: str, path: str = Query(..., max_length=400), db: AsyncSession = Depends(get_db)):
    """Where an address from the site that used to live on this domain goes now ({target: null}
    when it isn't one) - the public renderer asks only after none of its own pages matched. See
    services/website/old_links_service."""
    from app.services.website import old_links_service
    return {"target": await old_links_service.lookup(db, site_id, path)}


@router.get("/{site_id}/blog")
async def list_site_blog_posts(site_id: str, limit: int = Query(20, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    """The public Media page - real blog posts (always shown, nothing to toggle) plus this
    agent's Agent Hub videos/house tours, gated by the site's own media_settings toggles (see
    schemas.site.MediaSettings) so an agent can turn either off without touching Agent Hub."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    site, user = owner
    media_settings = (site.published_snapshot or {}).get("media_settings") or {}
    posts = await content_adapters.get_blog_posts(db, user, limit)
    hub_media = await content_adapters.get_agent_hub_media(db, user)
    return {
        "posts": posts,
        "videos": hub_media["videos"] if media_settings.get("show_videos", True) else [],
        "tours": hub_media["tours"] if media_settings.get("show_tours", True) else [],
        "channel_url": hub_media.get("channel_url"),
    }


async def _site_blog_blocks(db: AsyncSession, blocks) -> list:
    from app.services.ai import ai_blog_writer_service
    try:
        return await ai_blog_writer_service.fill_spotlight_photos(db, blocks or [])
    except Exception:
        return blocks or []


@router.get("/{site_id}/blog/{slug}")
async def get_site_blog_post(site_id: str, slug: str, db: AsyncSession = Depends(get_db)):
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    post = await content_adapters.get_blog_post_detail(db, user, slug)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found.")
    return {
        "id": post.id,
        "slug": post.slug,
        "title": post.title,
        "subtitle": post.subtitle,
        "cover_image_url": content_adapters.public_image_url(post.cover_image_url, post.id),
        "category": post.category,
        "theme": post.theme,
        "blocks": await _site_blog_blocks(db, post.blocks),
        "seo_title": post.seo_title,
        "meta_description": post.meta_description,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "agent_name": f"{user.first_name} {user.last_name}".strip(),
        "agent_brokerage": user.brokerage,
        "agent_photo_url": user.photo_url,
        "agent_title": user.professional_title,
        "signature_card": bool(getattr(post, "use_signature_card", True)) and await email_signature_service.has_signature_card(db, user),
        "luxe_metal": getattr(post, "luxe_metal", None),
        "area_towns": [t.get("town") for t in ((post.ai_review or {}).get("towns") or []) if isinstance(t, dict) and t.get("town")][:4],
        "neighborhood_links": await blog_neighborhoods.neighborhood_links(db, user, getattr(post, "linked_areas", None)),
    }


@router.get("/{site_id}/properties")
async def list_site_properties(site_id: str, limit: int = Query(48, ge=1, le=48), db: AsyncSession = Depends(get_db)):
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    site, user = owner
    featured_settings = (site.published_snapshot or {}).get("featured_settings") or {}
    return await content_adapters.get_featured_page_listings(db, user, featured_settings, limit)


@router.get("/{site_id}/properties/{listing_key}")
async def get_site_property(site_id: str, listing_key: str, db: AsyncSession = Depends(get_db)):
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    prop = await content_adapters.get_property_detail(db, user, listing_key)
    if not prop:
        # Not one of this agent's own listings - still a legitimate click-through when the
        # Featured page's "filtered" mode is showing full-MLS results (see
        # get_featured_page_listings), which this same route otherwise 404'd on even though the
        # listing is real and was shown on this agent's own page moments earlier.
        prop = await content_adapters.get_public_mls_property_detail(db, listing_key)
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")
    # The agent's own published Listing Spotlight for this exact home (any listing they chose to
    # spotlight, not only ones they list) - the property page shows it as the richer experience.
    await content_adapters.attach_spotlight_urls(db, user, [prop])
    # Only the link preview (share image) reads this - false leaves the agent off a sold home they
    # weren't on (services/sold_branding). The page still shows the site owner's contact card.
    prop["branding_allowed"] = await sold_branding.branding_allowed_for_listing(db, user, listing_key)
    prop["similar"] = await content_adapters.get_similar_listings(db, user, listing_key, prop.get("city"))
    return prop


# A real map search: enough homes to fill the map and list (was 24).
SEARCH_RESULTS_LIMIT = 120


@router.get("/{site_id}/search")
async def search_site_properties(
    site_id: str,
    q: str = Query("", max_length=300),
    min_price: Optional[int] = Query(None, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    min_beds: Optional[int] = Query(None, ge=0, le=20),
    min_baths: Optional[float] = Query(None, ge=0, le=20),
    property_type: Optional[str] = Query(None),
    min_sqft: Optional[int] = Query(None, ge=0),
    max_sqft: Optional[int] = Query(None, ge=0),
    min_acres: Optional[float] = Query(None, ge=0, le=1000),
    min_year_built: Optional[int] = Query(None, ge=1700, le=2100),
    school_district: Optional[str] = Query(None),
    sort: str = Query("newest"),
    include_rentals: bool = Query(False),
    area: Optional[str] = Query(None, max_length=6000, description="A drawn search area: lat,lng;lat,lng;..."),
    db: AsyncSession = Depends(get_db),
):
    """The Search tab's results - searches the full regional MLS mirror, not just this agent's
    own listings (real IDX behavior, see content_adapters.get_search_results). `q` is the top
    search box - a street address (starts with a house number) runs a direct address match,
    otherwise it's parsed deterministically with no AI/LLM call; everything else backs the
    Search tab's own Zillow-style filter bar and takes precedence over `q` when both are set."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    return await content_adapters.get_search_results(
        db, user, q,
        min_price=min_price, max_price=max_price, min_beds=min_beds, min_baths=min_baths,
        property_type=property_type, min_sqft=min_sqft, school_district=school_district, sort=sort,
        include_rentals=include_rentals,
        search_polygon=geography.polygon_from_param(area),
        max_sqft=max_sqft, min_acres=min_acres, min_year_built=min_year_built,
        limit=SEARCH_RESULTS_LIMIT,
    )


@router.get("/{site_id}/search/properties/{listing_key}")
async def get_search_result_property(site_id: str, listing_key: str, db: AsyncSession = Depends(get_db)):
    """Click-through detail for a search result - unlike /{site_id}/properties/{listing_key},
    this is NOT scoped to the site owner's own listings (see
    content_adapters.get_public_mls_property_detail). Still requires the site to be published so
    a draft site can't be used to browse the MLS anonymously."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    prop = await content_adapters.get_public_mls_property_detail(db, listing_key)
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")
    await content_adapters.attach_spotlight_urls(db, user, [prop])
    prop["branding_allowed"] = await sold_branding.branding_allowed_for_listing(db, user, listing_key)
    return prop


@router.get("/{site_id}/areas")
async def list_site_areas(site_id: str, db: AsyncSession = Depends(get_db)):
    """The agent's served-area grid (Communities block / Communities index page) - name/slug/
    photo/teaser plus a live quick-stat (median price, active count) per area, see
    content_adapters.get_served_areas_summary's with_stats docstring."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _site, user = owner
    return await content_adapters.get_served_areas_summary(db, user, with_stats=True)


async def _get_owned_area(db: AsyncSession, site: Site, slug: str) -> MarketArea:
    result = await db.execute(
        select(MarketArea)
        .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == site.owner_id, MarketArea.slug == slug)
    )
    area = result.scalar_one_or_none()
    if not area:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Community page not found.")
    return area


def _area_explore_origin(area: MarketArea) -> tuple[float, float] | None:
    scope = geography.scope_for_market_area(area)
    if scope.get("kind") == "radius":
        center = scope.get("center") or {}
        if center.get("lat") is not None and center.get("lng") is not None:
            return float(center["lat"]), float(center["lng"])
    if scope.get("kind") in {"bounds", "polygon"}:
        bounds = scope.get("bounds") or {}
        if all(bounds.get(k) is not None for k in ("min_lat", "max_lat", "min_lng", "max_lng")):
            return (
                (float(bounds["min_lat"]) + float(bounds["max_lat"])) / 2,
                (float(bounds["min_lng"]) + float(bounds["max_lng"])) / 2,
            )
    if area.centroid_lat is not None and area.centroid_lng is not None:
        return float(area.centroid_lat), float(area.centroid_lng)
    return None


def _parse_compare_slugs(slugs: str) -> list[str]:
    requested: list[str] = []
    for slug in (slugs or "").split(","):
        clean = slug.strip().lower()
        if clean and clean not in requested:
            requested.append(clean)
    if len(requested) != 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose two different neighborhoods to compare.",
        )
    return requested


def _comparison_photo(facts: dict, listings: list[dict]) -> Optional[str]:
    for listing in listings:
        if listing.get("photo"):
            return listing["photo"]
    return None


@router.get("/{site_id}/areas/compare")
async def compare_site_areas(
    site_id: str,
    slugs: str = Query(..., min_length=3, max_length=200),
    db: AsyncSession = Depends(get_db),
):
    """Side-by-side comparison for the public Neighborhoods tab. The URL names existing
    MarketArea slugs, but this endpoint only returns rows joined through this site's
    AgentMarketArea selections, so a visitor cannot compare a global/unserved MarketArea by
    editing the query string. All facts are already stored on MarketArea and live listing data
    uses the same geography.scope_for_market_area path as individual neighborhood pages."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    site, _user = owner
    requested = _parse_compare_slugs(slugs)

    result = await db.execute(
        select(MarketArea)
        .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == site.owner_id, MarketArea.slug.in_(requested))
    )
    areas_by_slug = {area.slug: area for area in result.scalars().all()}
    missing = [slug for slug in requested if slug not in areas_by_slug]
    if missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Neighborhood comparison not found.")

    compared = []
    for slug in requested:
        area = areas_by_slug[slug]
        area_scope = geography.scope_for_market_area(area)
        facts = dict(area.facts or {})
        if "photos" in facts:
            facts["photos"] = content_adapters.resolve_photo_urls(facts["photos"])
        if facts.get("public_schools"):
            facts["public_schools"] = await content_adapters.reconcile_public_schools(db, area, area_scope)
        market_stats = await content_adapters.get_area_live_market_stats(db, area_scope, facts.get("market_stats"))
        facts["market_stats"] = market_stats
        listings, inventory_total = await content_adapters.get_area_inventory(db, area_scope, limit=3)
        from app.services.areas.fair_housing_service import clean_area_payload
        facts, compare_narrative = clean_area_payload(facts, area.narrative)
        facts.pop("_fair_housing", None)
        compared.append({
            "name": area.name,
            "state": area.state,
            "slug": area.slug,
            "county": area.county,
            "area_type": area.area_type,
            "parent_area_name": area.parent_area_name,
            "geography_type": area_scope.get("kind") or "none",
            "facts": content_adapters.fix_mojibake_deep(facts),
            "narrative": content_adapters.fix_mojibake_deep(compare_narrative),
            "photo": _comparison_photo(facts, listings),
            "market_stats": market_stats,
            "inventory": {
                "total": inventory_total,
                "listings": listings,
            },
        })
    return {"areas": compared}


async def _related_for(db: AsyncSession, owner_id: str, area_id: str):
    user = await db.get(User, owner_id)
    return await content_adapters.get_area_related(db, user, area_id, limit=3) if user else []


async def warm_area_caches() -> None:
    """After a restart: compute every published site's town-page numbers once, quietly in the
    background, so the first visitor to each town page isn't the one who waits (see
    content_adapters.cached_market)."""
    import asyncio as _asyncio
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Site.owner_id, MarketArea)
                .join(AgentMarketArea, AgentMarketArea.owner_id == Site.owner_id)
                .join(MarketArea, MarketArea.id == AgentMarketArea.area_id)
                .where(Site.status == "PUBLISHED", Site.is_enabled.isnot(False))
            )).all()
        done = set()
        for owner_id, area in rows:
            if area.id in done:
                continue
            done.add(area.id)
            scope = geography.scope_for_market_area(area)
            facts = dict(area.facts or {})
            districts = [d.get("name") for d in (facts.get("public_schools") or {}).get("districts") or [] if isinstance(d, dict)]
            for key, make in (
                (f"stats:{area.id}", lambda d: content_adapters.get_area_live_market_stats(d, scope, facts.get("market_stats"))),
                (f"trends:{area.id}", lambda d: market_stats_service.get_area_market_trends(d, scope)),
                (f"msales:{area.id}", lambda d: content_adapters.get_area_recent_market_sales(d, scope, 6)),
                (f"insights:{area.id}", lambda d: market_insights_service.get_area_insights(d, scope, districts)),
                (f"munis:{area.id}", lambda d: market_insights_service.get_area_municipalities(d, scope)),
                (f"related:{owner_id}:{area.id}", lambda d: _related_for(d, owner_id, area.id)),
            ):
                try:
                    async with AsyncSessionLocal() as d:
                        content_adapters._market_store(key, await make(d))
                except Exception:
                    logger.warning("area cache warm failed", extra={"key": key}, exc_info=True)
                await _asyncio.sleep(0.2)  # gentle on the database
            try:
                async with AsyncSessionLocal() as d:
                    content_adapters._market_store(f"photo:{area.id}", await content_adapters._area_top_listing_photo(d, scope))
            except Exception:
                logger.warning("area photo warm failed", extra={"area": area.id}, exc_info=True)
        # Each published site's home page (hero + Neighborhoods row) and town list - built from the
        # numbers warmed above, so the first visitor after a deploy gets them at once.
        for owner_id in {o for o, _ in rows}:
            try:
                async with AsyncSessionLocal() as d:
                    owner = await d.get(User, owner_id)
                    if owner:
                        for limit, stats in ((content_adapters._SELECTED_MODE_FETCH_CAP, True), (content_adapters._SELECTED_MODE_FETCH_CAP, False), (15, True)):
                            await content_adapters.get_served_areas_summary(d, owner, limit, with_stats=stats)
            except Exception:
                logger.warning("served areas warm failed", extra={"owner": owner_id}, exc_info=True)
        logger.info("area caches warmed", extra={"areas": len(done)})
    except Exception:
        logger.warning("area cache warm-up failed", exc_info=True)


@router.get("/{site_id}/areas/{slug}")
async def get_site_area(site_id: str, slug: str, db: AsyncSession = Depends(get_db)):
    """A community page (/villanova-pa) - real shared Area facts/narrative, the broader current
    community inventory (full-MLS, geography-scoped, see get_area_inventory - NOT just this agent's own
    listings), plus this agent's own sales/open houses/listings in the area and up to 3 other
    communities they also serve. 404s unless the site's owner actually serves this area - a
    community page is never guessable for an area an agent hasn't claimed, even though the
    underlying MarketArea row is shared/global."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    site, user = owner
    area = await _get_owned_area(db, site, slug)

    area_scope = geography.scope_for_market_area(area)
    facts = dict(area.facts)
    if "photos" in facts:
        facts["photos"] = content_adapters.resolve_photo_urls(facts["photos"])
    stats_seed = facts.get("market_stats")
    facts["market_stats"] = await content_adapters.cached_market(f"stats:{area.id}", lambda d: content_adapters.get_area_live_market_stats(d, area_scope, stats_seed), db)
    # Districts checked against what Bright says about this town's homes (see reconcile_public_schools).
    if facts.get("public_schools"):
        facts["public_schools"] = await content_adapters.reconcile_public_schools(db, area, area_scope)

    # Fair Housing wording, fixed rules, on every page served (the stored text was already checked;
    # this catches anything older than a rule) - fair_housing_service.
    from app.services.areas.fair_housing_service import clean_area_payload
    facts, narrative_clean = clean_area_payload(facts, area.narrative)
    facts.pop("_fair_housing", None)
    hub_rules = await content_adapters.hub_sales_rules(db, user)
    return {
        "name": area.name,
        "state": area.state,
        "slug": area.slug,
        "area_type": area.area_type,
        "parent_area_name": area.parent_area_name,
        "county": area.county,
        "explore_origin": (lambda origin: {"lat": origin[0], "lng": origin[1]} if origin else None)(_area_explore_origin(area)),
        "facts": content_adapters.fix_mojibake_deep(facts),
        "narrative": content_adapters.fix_mojibake_deep(narrative_clean),
        # The Agent Hub's picks (hidden homes, min price, order) apply on town pages too (owner, 10/24).
        "my_listings": content_adapters.drop_hidden_listings(await content_adapters.get_active_listings(db, user, 12, area_scope=area_scope), hub_rules)[:6],
        "sales": content_adapters.apply_sales_rules(await content_adapters.get_recent_sales(db, user, 12, area_scope=area_scope), hub_rules)[:6],
        "open_houses": await content_adapters.get_open_houses(db, user, 4, area_scope=area_scope),
        "related_areas": await content_adapters.cached_market(f"related:{user.id}:{area.id}", lambda d: content_adapters.get_area_related(d, user, area.id, limit=3), db),
        # Live market trends (12 months of closed sales, what's for sale now) and the latest
        # closed sales across the whole MLS - see market_stats_service.get_area_market_trends.
        "market_trends": await content_adapters.cached_market(f"trends:{area.id}", lambda d: market_stats_service.get_area_market_trends(d, area_scope), db),
        "market_sales": await content_adapters.cached_market(f"msales:{area.id}", lambda d: content_adapters.get_area_recent_market_sales(d, area_scope, 6), db),
        # What your money buys / buyer's-or-seller's market / taxes per sq ft (market_insights_service).
        "insights": await content_adapters.cached_market(f"insights:{area.id}", lambda d: market_insights_service.get_area_insights(
            d, area_scope, [x.get("name") for x in (facts.get("public_schools") or {}).get("districts") or [] if isinstance(x, dict)]), db),
        # Which townships this mailing-address town actually spans (Haverford: Lower Merion 67%, Haverford 33%).
        # Real Census borders when the town map has been built (town_map_service), else the MLS field.
        "municipalities": [{"name": m["name"], "share": m["share"]} for m in ((facts.get("boundaries") or {}).get("municipalities") or [])]
        or await content_adapters.cached_market(f"munis:{area.id}", lambda d: market_insights_service.get_area_municipalities(d, area_scope), db),
    }


@router.get("/{site_id}/nearby/availability")
async def get_site_nearby_availability(site_id: str, db: AsyncSession = Depends(get_db)):
    """Read-only, no Google call, no budget reservation - lets the frontend decide whether to render
    the Explore Nearby section at all before a visitor ever clicks a category. Platform-wide (not
    per-site), same as the underlying places_search budget it checks."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    config = await ai_config_service.get_config(db)
    return {"available": await is_explore_nearby_available(db, config)}


@router.get("/{site_id}/areas/{slug}/nearby")
async def get_site_area_nearby(
    site_id: str,
    slug: str,
    request: Request,
    response: Response,
    category: str = Query(..., max_length=40),
    db: AsyncSession = Depends(get_db),
):
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    site, user = owner
    area = await _get_owned_area(db, site, slug)
    origin = _area_explore_origin(area)
    if not origin:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This community does not have a usable map center.")
    device_id = _visitor_device_id(request, response)
    return await _explore_nearby_response(db, origin[0], origin[1], category, _client_ip(request), device_id, user.id)


@router.get("/{site_id}/areas/{slug}/listings")
async def get_site_area_listings(
    site_id: str, slug: str,
    limit: int = Query(24, ge=1, le=48), offset: int = Query(0, ge=0),
    min_price: Optional[int] = None, max_price: Optional[int] = None,
    sort: str = Query("newest", pattern="^(newest|price_desc|price_asc)$"),
    db: AsyncSession = Depends(get_db),
):
    """Paginated "browse all homes in this community" feed backing the community page's Load
    More / full-inventory view - same full-MLS get_area_inventory query as the embedded section
    on get_site_area, just with real offset-based pagination for browsing past the first page."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    site, _user = owner
    area = await _get_owned_area(db, site, slug)
    listings, total = await content_adapters.get_area_inventory(
        db,
        geography.scope_for_market_area(area),
        limit=limit,
        offset=offset,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
    )
    return {"listings": listings, "total": total}


@router.get("/{site_id}/properties/{listing_key}/nearby")
async def get_site_property_nearby(
    site_id: str,
    listing_key: str,
    request: Request,
    response: Response,
    category: str = Query(..., max_length=40),
    db: AsyncSession = Depends(get_db),
):
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _site, user = owner
    result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    prop = result.scalar_one_or_none()
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")
    lat = prop.google_rooftop_latitude if prop.google_rooftop_latitude is not None else prop.latitude
    lng = prop.google_rooftop_longitude if prop.google_rooftop_longitude is not None else prop.longitude
    if lat is None or lng is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This listing does not have a usable map location.")
    device_id = _visitor_device_id(request, response)
    return await _explore_nearby_response(db, float(lat), float(lng), category, _client_ip(request), device_id, user.id)


@router.get("/{site_id}/places/{place_id}/details")
async def get_site_nearby_place_details(
    site_id: str,
    place_id: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _site, user = owner
    device_id = _visitor_device_id(request, response)
    return await _explore_place_details_response(db, place_id, _client_ip(request), device_id, user.id)


@router.get("/{site_id}/profile")
async def get_site_profile(site_id: str, db: AsyncSession = Depends(get_db)):
    """Minimal public profile summary for site-wide chrome (footer contact info, header avatar) -
    the full profile (bio, social links, etc.) is only ever fetched as part of
    hydrate_page_blocks for an about_agent block, not exposed as its own public endpoint."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    from app.services.sales_wording import sales_are_recent
    profile = await content_adapters.get_agent_profile(db, user)
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    forms = await content_adapters.get_forms_list(db, user)
    site_reviews = await content_adapters.get_site_reviews(db, user, limit=0)
    from app.utils.auth import REVIEWS_ACCESS_TIERS, owner_has_access
    # The header/footer's star rating - only while the Reviews page is on.
    review_summary = site_reviews["summary"] if site_reviews["show"] else None
    return {
        "name": profile.get("name"),
        "phone": profile.get("phone"),
        "email": profile.get("email"),
        "brokerage": profile.get("brokerage"),
        "photo_url": profile.get("photo_url"),
        # Site-wide chrome (the luxe footer + header wordmark) shows these too.
        "title": profile.get("title"),
        "market": profile.get("market"),
        "license_number": profile.get("license_number"),
        "social_links": profile.get("social_links") or [],
        # Footer signature card (theme.footer_signature decides whether the page uses it).
        "signature_hub_slug": await content_adapters.signature_hub_slug_for(db, user),
        # "Recently Sold" (footer link) only under $50M in volume - services/sales_wording.
        "sales_recent": sales_are_recent((await content_adapters.get_agent_stats(db, user)).get("sold_volume")),
        # Every page's form buttons (Home value, Buying, Selling, a showing...) open one of these in
        # a window over the page - see components/website-public/SiteFormLauncher.
        "agent_hub_slug": hub.slug if hub else None,
        "forms": [{"slug": f["slug"], "title": f["title"], "form_type": f["form_type"]} for f in forms] if hub else [],
        "rating": review_summary,
        # What the site's Google structured data may use (content_adapters.google_rating).
        "google_rating": content_adapters.google_rating(site_reviews) if owner_has_access(user, REVIEWS_ACCESS_TIERS) else None,
    }


@router.get("/{site_id}/reviews")
async def get_site_reviews(site_id: str, db: AsyncSession = Depends(get_db)):
    """The website's Reviews page (and its menu link): the agent's reviews when they've switched
    "Show on IDX" on - {show: false} otherwise, so the page 404s and the menu leaves it out."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    from app.utils.auth import REVIEWS_ACCESS_TIERS, owner_has_access
    if not owner_has_access(user, REVIEWS_ACCESS_TIERS):
        return {"show": False, "summary": {"count": 0, "average": None}, "reviews": [], "hub_slug": None}
    return await content_adapters.get_site_reviews(db, user)


@router.get("/{site_id}/about")
async def get_site_about(site_id: str, db: AsyncSession = Depends(get_db)):
    """The About page's real content - a live mirror of this agent's Agent Hub (see
    content_adapters.get_agent_hub_snapshot), so an agent's own site can work as their link-tree
    too instead of only openhousepal.com/{slug}."""
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    return await content_adapters.get_agent_hub_snapshot(db, user)


@router.get("/{site_id}/forms/by-type/{form_type}", response_model=PublicAgentFormResponse)
async def get_site_public_form_by_type(
    site_id: str,
    form_type: str,
    track_view: bool = Query(True, description="Set false for metadata/preflight reads that should not count as a visitor view."),
    db: AsyncSession = Depends(get_db),
):
    if form_type != "how_can_i_help":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported default form type.")
    _, user, form = await _get_site_default_form(db, site_id, form_type)
    if track_view:
        try:
            db.add(AgentHubEvent(owner_id=form.owner_id, event_type="form_view"))
            await db.commit()
        except Exception:
            await db.rollback()
    return await _public_form_response(db, user, form)


@router.get("/{site_id}/forms/{form_id}", response_model=PublicAgentFormResponse)
async def get_site_public_form(
    site_id: str,
    form_id: str,
    track_view: bool = Query(True, description="Set false for metadata/preflight reads that should not count as a visitor view."),
    c: Optional[str] = Query(None, description="Campaign token from the link (?c=)."),
    db: AsyncSession = Depends(get_db),
):
    """Site-scoped twin of forms_routes.get_public_form (which requires an Agent Hub slug to
    resolve ownership) - same AgentForm row, same response shape, just reached by an agent's
    Site instead of their Agent Hub. Website Builder does not require Agent Hub to exist."""
    _, user, form = await _get_site_form(db, site_id, form_id)
    if track_view:
        try:
            db.add(AgentHubEvent(owner_id=form.owner_id, event_type="form_view"))
            await db.commit()
        except Exception:
            await db.rollback()

    return await _public_form_response(db, user, form, campaign_token=c)


@router.post("/{site_id}/forms/{form_id}/submit", response_model=FormSubmissionResponse)
async def submit_site_public_form(site_id: str, form_id: str, payload: FormSubmissionCreate, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    """Submits into the exact same pipeline as the Agent-Hub-scoped public form (same
    FormSubmission row, same Lead upsert via forms_service.submit_form) - the website is another
    source feeding the existing OHP lead system, not a separate inbox."""
    _, _, form = await _get_site_form(db, site_id, form_id)
    return await forms_service.submit_form(db, form, payload)


class SiteAskRequest(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    message: str
    address: Optional[str] = None
    came_from: Optional[dict] = None


class GoogleVisitorRequest(BaseModel):
    credential: str
    page_url: Optional[str] = None
    listing_key: Optional[str] = None
    address: Optional[str] = None


@router.get("/{site_id}/google-visitor")
async def google_visitor_config(site_id: str, db: AsyncSession = Depends(get_db)):
    """What the One Tap frame needs: the Google client id (null = One Tap off for this site)."""
    from app.services import google_visitor_service as gv
    if not await _published_site_owner(db, site_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    return {"client_id": gv.client_id()}


@router.post("/{site_id}/google-visitor")
async def google_visitor_signin(site_id: str, payload: GoogleVisitorRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """A visitor tapped "Continue as ..." in Google One Tap on this agent's website (user, 10/18).
    Google's token is checked with Google; the person becomes (or is added to) the agent's lead,
    and the agent hears about a NEW person once (a repeat sign-in only adds a line, at most every
    12 hours). Returns just the name + email the site uses to fill in its forms."""
    from datetime import timedelta
    from app.services.public_rate_limit import check_public_submit
    from app.services import google_visitor_service as gv, leads_service
    from app.services.agent_email_service import absolute, email_agent
    from app.models.database import Lead, LeadEvent, Notification
    check_public_submit(request)
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _site, user = owner
    person = await gv.verify_credential(payload.credential)
    if not person:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google couldn't confirm that sign-in.")
    email = person["email"]
    name = person["name"] or " ".join(x for x in (person["given_name"], person["family_name"]) if x) or email.split("@")[0]
    first = person["given_name"] or name.split(" ")[0]
    address = (payload.address or "").strip()[:200] or None
    if not address and payload.listing_key:
        # The home they were looking at when they signed in.
        row = (await db.execute(select(Property.street_address, Property.city).where(Property.listing_key == payload.listing_key[:80]))).first()
        if row and row[0]:
            from app.services.agent_roundup_service import nice_street
            address = nice_street(row[0], row[1])
    now = datetime.now(timezone.utc)

    existing = (await db.execute(select(Lead).where(Lead.owner_id == user.id, Lead.email == email))).scalars().first()
    recent = None
    if existing is not None:
        recent = (await db.execute(
            select(LeadEvent.id).where(LeadEvent.lead_id == existing.id, LeadEvent.source_type == gv.SOURCE_TYPE,
                                       LeadEvent.occurred_at >= now - gv.REPEAT_WINDOW).limit(1)
        )).first()
    if recent is None:
        lead = await leads_service.upsert_lead(
            db, owner_id=user.id, name=name, email=email, phone=None,
            source_type=gv.SOURCE_TYPE, source_id=site_id,
            summary=("Signed in with Google on your website" + (f" while viewing {address}" if address else ""))[:250],
            occurred_at=now, property_address=address,
            raw_snapshot={"page_url": (payload.page_url or "")[:500] or None, "listing_key": (payload.listing_key or "")[:80] or None,
                          "address": address, "picture": person.get("picture"), "verified_by": "google"},
        )
        if existing is None and lead is not None:
            try:
                link = f"/leads/{lead.id}"
                where = f" while looking at {address}" if address else ""
                db.add(Notification(
                    agent_id=user.id, type="LEAD", reference_type="LEAD", reference_id=lead.id,
                    title=f"New website lead: {name}"[:200],
                    message=f"{name} signed in with Google on your website{where}."[:500],
                    visitor_name=name, link=link, is_read=False, created_at=now,
                ))
                await db.commit()
                email_agent(user, "LEAD", "new_lead_captured", f"New website lead: {name}", {
                    "lead_name": name, "lead_source": "Your website (signed in with Google)", "lead_email": email,
                    "lead_phone": None, "lead_message": None, "property_address": address or "", "link": absolute(link),
                })
            except Exception as e:  # the lead itself is already saved
                logger.warning("google visitor side effects failed", extra={"error": str(e)})
                await db.rollback()
    # Already has a Showcase with this agent (an open house sign-in, a form...): the site shows it as
    # "{first}'s Showcase" right away. Only for a Google-verified email, so nobody can look one up.
    return {"name": name, "first_name": first, "email": email, "showcase": await _visitor_showcase_code(db, user.id, email)}


async def _visitor_showcase_code(db: AsyncSession, owner_id: str, email: str) -> Optional[str]:
    from sqlalchemy import func
    from app.services.showcase_links import ensure_short_code
    try:
        col = (await db.execute(
            select(Collection).where(
                Collection.owner_id == owner_id, func.lower(Collection.visitor_email) == email.strip().lower(),
                Collection.is_public.is_(True), Collection.status == "ACTIVE",
            ).order_by(Collection.updated_at.desc().nullslast(), Collection.created_at.desc()).limit(1)
        )).scalars().first()
        if not col:
            return None
        return col.short_code or await ensure_short_code(col.id) or col.share_token
    except Exception as e:
        logger.warning("visitor showcase lookup failed", extra={"error": str(e)})
        await db.rollback()
        return None


@router.get("/{site_id}/my-showcase/{share_token}")
async def get_site_my_showcase(site_id: str, share_token: str, db: AsyncSession = Depends(get_db)):
    """The header of a visitor's "{first}'s Showcase" tab on the agent's IDX site: whose it is, how
    many homes are in it (and how many are new), and what it's searching for - so the site can
    show it and the Showcase builder can start an edit from it. The Showcase page itself does the rest."""
    from datetime import timedelta
    from sqlalchemy import func
    from app.models.database import CollectionPreferences, collection_properties
    await _assert_site_showcase(db, site_id, share_token)
    col = (await db.execute(select(Collection).where(share_match(share_token)))).scalars().first()
    if not col:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found.")
    homes = (await db.execute(select(func.count()).select_from(collection_properties).where(collection_properties.c.collection_id == col.id))).scalar() or 0
    new = (await db.execute(select(func.count()).select_from(collection_properties).where(
        collection_properties.c.collection_id == col.id, collection_properties.c.added_at.is_not(None),
        collection_properties.c.added_at >= datetime.now(timezone.utc) - timedelta(days=7)))).scalar() or 0
    pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == col.id))).scalars().first()
    types = [t for t, on in (("single_family", getattr(pref, "is_single_family", False)), ("town_house", getattr(pref, "is_town_house", False)),
                             ("condo", getattr(pref, "is_condo", False)), ("multi_family", getattr(pref, "is_multi_family", False))) if on] if pref else []
    name = (col.visitor_name or "").strip()
    return {
        "code": col.short_code or col.share_token,
        "name": name or None,
        "first_name": name.split(" ")[0] if name else None,
        "homes": int(homes),
        "new_this_week": int(new),
        "criteria": {
            "cities": list(pref.cities or []) if pref else [],
            "school_districts": list(pref.school_districts or []) if pref else [],
            "min_price": pref.min_price if pref else None, "max_price": pref.max_price if pref else None,
            "min_beds": pref.min_beds if pref else None, "min_baths": pref.min_baths if pref else None,
            "min_sqft": pref.min_sqft if pref else None,
            "center_address": pref.address if pref and pref.lat is not None and pref.diameter else None,
            "center_lat": pref.lat if pref and pref.diameter else None, "center_lng": pref.long if pref and pref.diameter else None,
            "radius_miles": pref.diameter if pref else None,
            "drawn_area": bool(pref and pref.search_polygon),
            "types": types,
        },
    }


@router.post("/{site_id}/properties/{listing_key}/ask")
async def ask_about_site_property(site_id: str, listing_key: str, payload: SiteAskRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """The quick "Ask a question" box on an IDX property page. The published site names the agent
    (never the visitor), then it's the Listing Page's message path - same email, notification,
    lead (property_page_inquiry) and follow-up - tagged as found on the agent's website."""
    from app.services.public_rate_limit import check_public_submit
    from app.api.properties_routes import AgentMessageRequest, message_agent
    check_public_submit(request)
    owner = await _published_site_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    _, user = owner
    name, message = (payload.name or "").strip(), (payload.message or "").strip()
    email, phone = (payload.email or "").strip(), (payload.phone or "").strip()
    if not name or not message or not (email or phone):
        raise HTTPException(status_code=400, detail="Please add your name, a question, and an email or phone number.")
    came_from = dict(payload.came_from or {})
    came_from.setdefault("src", "site")
    return await message_agent(AgentMessageRequest(
        agent_id=user.id, property_id=listing_key, property_address=(payload.address or "").strip()[:200] or listing_key,
        visitor_name=name[:120], visitor_contact=email or phone, visitor_phone=phone or None,
        message=message[:4000], came_from=came_from,
    ), db)


@router.post("/{site_id}/forms/{form_id}/progress")
async def track_site_public_form_progress(site_id: str, form_id: str, payload: FormProgressUpdate, db: AsyncSession = Depends(get_db)):
    """Best-effort form progress tracking for website-hosted form aliases. Uses the same
    FormViewSession table as the Agent Hub URL so stats stay canonical across both public
    doorways."""
    try:
        _, _, form = await _get_site_form(db, site_id, form_id)
        await _track_site_form_progress(db, form, payload)
        return {"success": True}
    except HTTPException:
        return {"success": True}
    except Exception:
        await db.rollback()
        return {"success": True}


@router.get("/{site_id}/showcase-creator", response_model=PublicAgentFormResponse)
async def get_site_showcase_creator(
    site_id: str,
    track_view: bool = Query(True, description="Set false for metadata/preflight reads that should not count as a visitor view."),
    db: AsyncSession = Depends(get_db),
):
    """Branded IDX doorway into the canonical showcase_request AgentForm.

    This intentionally does not accept a form id or owner id from the visitor-facing URL: the
    published Site resolves the owner, then the owner's existing locked showcase_request form
    drives the same OHP Showcase creation workflow as the Agent Hub QR/link path.
    """
    _, user, form = await _get_site_showcase_creator_form(db, site_id)
    if track_view:
        try:
            db.add(AgentHubEvent(owner_id=form.owner_id, event_type="form_view"))
            await db.commit()
        except Exception:
            await db.rollback()
    return await _public_form_response(db, user, form)


@router.post("/{site_id}/showcase-creator/submit", response_model=FormSubmissionResponse)
async def submit_site_showcase_creator(site_id: str, payload: FormSubmissionCreate, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    """Submit the IDX Showcase Creator through forms_service.submit_form, preserving the single
    FormSubmission/Lead/Notification/Collection pipeline used by existing OHP public forms."""
    _, _, form = await _get_site_showcase_creator_form(db, site_id)
    return await forms_service.submit_form(db, form, payload)


@router.post("/{site_id}/showcase-creator/progress")
async def track_site_showcase_creator_progress(site_id: str, payload: FormProgressUpdate, db: AsyncSession = Depends(get_db)):
    try:
        _, _, form = await _get_site_showcase_creator_form(db, site_id)
        await _track_site_form_progress(db, form, payload)
        return {"success": True}
    except HTTPException:
        return {"success": True}
    except Exception:
        await db.rollback()
        return {"success": True}


@router.get("/{site_id}/showcase/{share_token}")
@router.get("/{site_id}/showcases/{share_token}")
async def get_site_public_showcase(site_id: str, share_token: str, db: AsyncSession = Depends(get_db)):
    """Site-scoped alias for a public Showcase. The collection is still the single OHP
    Collection row served by /collections/shared/{token}; this endpoint only proves the token
    belongs to the owner resolved from the IDX/custom-domain site before the frontend opens the
    existing public Showcase page."""
    await _assert_site_showcase(db, site_id, share_token)
    data = await CollectionsService.get_shared_collection(db, share_token)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showcase not found.")
    return data


@router.get("/{site_id}/cma/{share_slug}")
async def get_site_public_cma(site_id: str, share_slug: str, db: AsyncSession = Depends(get_db)):
    """Site-scoped alias for a public CMA. The CMA remains served by /api/cma/public/{slug};
    this endpoint only proves the slug belongs to the owner resolved from the IDX/custom-domain
    site before the frontend opens the existing public CMA page."""
    await _assert_site_cma(db, site_id, share_slug)
    return {"ok": True}


@router.get("/domain-allowed")
async def domain_allowed(
    domain: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Caddy's on-demand TLS "ask" callback (see deploy/Caddyfile) - called once per hostname
    Caddy hasn't already got a cert for, before it requests one from Let's Encrypt. Returning
    anything other than 200 refuses the cert. This is the actual security boundary that stops
    a stranger pointing a random domain (or a scanner probing random *.openhousepal.com
    subdomains) at our IP from getting us to mint them a certificate or serve them anything:
    a cert is only ever issued for either (a) a subdomain of our own domain matching a real
    published Site, or (b) a hostname that is a VERIFIED/ACTIVE row in our own database, i.e.
    one that already proved DNS control by pointing at our known target (see
    domain_verification_service.check_dns)."""
    hostname = domain.strip().lower()

    slug = domain_resolution_service.subdomain_slug_from_hostname(hostname)
    if slug:
        result = await db.execute(select(Site.id).where(Site.subdomain_slug == slug, Site.status == "PUBLISHED"))
    else:
        result = await db.execute(
            select(SiteDomain.id).where(SiteDomain.hostname == hostname, SiteDomain.status.in_(["VERIFIED", "ACTIVE"]))
        )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Domain not connected.")
    return {"ok": True}
