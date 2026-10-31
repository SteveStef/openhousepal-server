import asyncio
import html
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import cast, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict

from fastapi.responses import Response

from app.database import get_db
from app.models.database import AgentHub, AgentHubEvent, FormSubmission, Property, User
from app.services.bright_mls_service import bright_mls_service
from app.services import property_website_service, s3_service, vcard_service
from app.services.website.site_service import owner_links, resolve_owner_public_base_url
from app.utils.auth import luxe_theme_allowed, AGENT_HUB_ACCESS_TIERS, REVIEWS_ACCESS_TIERS, get_current_active_user, get_current_user_optional, require_active_plan, require_agent_hub_access, require_reviews_access, owner_has_access, tier_has_mls
from app.utils.image_validation import assert_valid_jpeg, photo_to_jpeg

router = APIRouter(prefix="/agent-hub", tags=["agent-hub"])
logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RECENT_SALES_CACHE_TTL_SECONDS = 10 * 60
recent_sales_cache: dict[str, tuple[float, list[dict]]] = {}

# JPG only - PNG/WEBP uploads have been unreliable in practice (fail to render in some
# downstream flows), so every photo upload on the site is restricted to JPEG.
PHOTO_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
}
PHOTO_MAX_BYTES = 5 * 1024 * 1024
# The agent's own headshot also takes PNG/WebP (what phones and photo apps often save) - it's
# converted to a real JPEG before it's stored, so everything downstream still only ever sees JPG.
HEADSHOT_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class AgentHubSlugUpdate(BaseModel):
    slug: str


class AgentHubProfileUpdate(BaseModel):
    title: Optional[str] = None
    market: Optional[str] = None
    name: Optional[str] = None
    brokerage: Optional[str] = None


class AgentHubConfigUpdate(BaseModel):
    config: Dict[str, Any]


# The config blob holds the whole page-builder payload (profile text, contact info, social
# links, sales/video/tour/blog carousel data, layout/visibility flags). 300KB is generous for
# that - large enough for dozens of carousel items with images/links, small enough to block
# someone trying to stuff arbitrary data into an unbounded JSONB column.
AGENT_HUB_CONFIG_MAX_BYTES = 300_000


class AgentHubTrackEvent(BaseModel):
    event_type: str  # 'visit' | 'qr_scan' | 'interaction'
    visitor_id: Optional[str] = None


def normalize_slug(slug: str) -> str:
    return slug.strip().lower()


# The app's own top-level pages (frontend proxy.ts RESERVED_APP_SEGMENTS) - a hub link with one of
# these names would be shadowed by that page, so it can't be taken.
HUB_RESERVED_SLUGS = {
    "activity", "agenthub", "admin", "agent-hub", "api", "assistant", "broker-authorization", "cash-needed",
    "checkout", "cma", "dash", "discovery", "leads", "net-sheet", "open-houses", "privacy", "property",
    "settings", "setup", "showcase", "showcases", "studio", "terms", "unsubscribe", "upgrade-required",
    "forgot-password", "login", "register", "reset-password", "open-house", "spotlight-preview", "ohp-site-render",
}


def validate_slug(slug: str) -> str:
    normalized = normalize_slug(slug)
    if not SLUG_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use letters, numbers, and hyphens only. No spaces or unsupported symbols.",
        )
    if normalized in HUB_RESERVED_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That name is used by an OpenHousePal page - please pick another.",
        )
    return normalized


def extract_meta(content: str, property_name: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(property_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(property_name)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(property_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(property_name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1).strip())
    return ""


def extract_title(content: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.IGNORECASE | re.DOTALL)
    return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip()) if match else ""


def clean_sale_status(status_value) -> str:
    status_text = str(status_value or "").replace("-BRIGHT", "").replace("_", " ").strip().lower()
    if "under contract" in status_text:
        return "under contract"
    if "closed" in status_text or "sold" in status_text:
        return "sold"
    if "pending" in status_text:
        return "pending"
    if "active" in status_text:
        return "active"
    return status_text


def is_eligible_recent_sale(sale: dict) -> bool:
    return clean_sale_status(sale.get("status")) == "sold" and (sale.get("price") or 0) >= 100000


def apply_recent_sale_defaults(sale: dict) -> dict:
    return {
        **sale,
        "visible": is_eligible_recent_sale(sale),
    }


def bright_property_to_sale(item: dict) -> dict:
    price = item.get("ClosePrice") or item.get("ListPrice")
    return {
        "id": str(item.get("ListingKey") or item.get("ListingId") or item.get("FullStreetAddress")),
        "address": item.get("FullStreetAddress") or item.get("UnparsedAddress") or "Address unavailable",
        "city": item.get("City") or "",
        "state": item.get("StateOrProvince") or "",
        "price": price,
        "status": item.get("MlsStatus"),
        "image": item.get("ListPictureURL") or item.get("ListPicture2URL") or item.get("ListPicture3URL"),
        "sold_date": item.get("CloseDate") or item.get("ModificationTimestamp"),
        "beds": item.get("BedroomsTotal"),
        "baths": item.get("BathroomsTotalInteger"),
        "acres": item.get("LotSizeAcres"),
        "sqft": item.get("LivingArea"),
    }


async def fetch_bright_agent_sales(identifier: str, identifier_type: str, limit: int) -> list[dict]:
    cache_key = f"{identifier_type}:{identifier}:{limit}".lower()
    cached = recent_sales_cache.get(cache_key)
    if cached and time.time() - cached[0] < RECENT_SALES_CACHE_TTL_SECONDS:
        return cached[1]

    select_fields = ",".join([
        "ListingKey",
        "ListingId",
        "FullStreetAddress",
        "UnparsedAddress",
        "City",
        "StateOrProvince",
        "ListPrice",
        "ClosePrice",
        "CloseDate",
        "MlsStatus",
        "ModificationTimestamp",
        "ListPictureURL",
        "ListPicture2URL",
        "ListPicture3URL",
        "BedroomsTotal",
        "BathroomsTotalInteger",
        "LotSizeAcres",
        "LivingArea",
        "ListAgentFullName",
        "BuyerAgentFullName",
        "CoBuyerAgentFullName",
        "CoListAgentFullName",
        "ListAgentMlsId",
        "ListAgentStateLicenseNumber",
        "BuyerAgentMlsId",
        "BuyerAgentStateLicenseNumber",
        "CoBuyerAgentMlsId",
        "CoBuyerAgentStateLicenseNumber",
        "CoListAgentMlsId",
        "CoListAgentStateLicenseNumber",
        "ListOfficeName",
        "BuyerOfficeName",
        "CoBuyerOfficeName",
        "CoListOfficeName",
    ])
    agent_fields = [
        "BuyerAgentStateLicenseNumber",
        "CoBuyerAgentStateLicenseNumber",
        "ListAgentStateLicenseNumber",
        "CoListAgentStateLicenseNumber",
        "BuyerAgentFullName",
        "CoBuyerAgentFullName",
        "ListAgentFullName",
        "CoListAgentFullName",
        "BuyerOfficeName",
        "CoBuyerOfficeName",
        "ListOfficeName",
        "CoListOfficeName",
    ] if identifier_type == "license" else [
        "BuyerAgentMlsId",
        "CoBuyerAgentMlsId",
        "ListAgentMlsId",
        "CoListAgentMlsId",
    ]
    sales_by_listing = {}

    safe_identifier = identifier.replace("'", "''")

    async def fetch_field(field: str, mode: str = "eq", value: str | None = None) -> list[dict]:
        lookup_value = (value or safe_identifier).replace("'", "''")
        filter_expr = f"{field} eq '{lookup_value}'" if mode == "eq" else f"contains({field}, '{lookup_value}')"
        # Closed-scoped (Bright audit, 10/15): Bright only fills ClosePrice/CloseDate when the
        # query asks for closed listings, and unscoped the `limit` went to active listings too.
        filter_expr = f"{filter_expr} and MlsStatus eq 'CLOSED-BRIGHT'"
        try:
            data = await bright_mls_service._make_request(
                "BrightProperties",
                {
                    "$filter": filter_expr,
                    "$top": limit,
                    "$select": select_fields,
                    "$orderby": "CloseDate desc",
                },
            )
        except Exception as exc:
            logger.warning("Bright Agent Hub sale lookup failed", extra={"field": field, "error": str(exc)})
            return []
        return data.get("value", [])

    field_results = await asyncio.gather(*(fetch_field(field) for field in agent_fields))
    for items in field_results:
        for item in items:
            sale = bright_property_to_sale(item)
            if is_eligible_recent_sale(sale):
                sales_by_listing[sale["id"]] = apply_recent_sale_defaults(sale)

    sorted_sales = sorted(
        sales_by_listing.values(),
        key=lambda sale: str(sale.get("sold_date") or ""),
        reverse=True,
    )[:limit]
    recent_sales_cache[cache_key] = (time.time(), sorted_sales)
    return sorted_sales


@router.get("/slug/check")
async def check_slug(
    slug: str = Query(..., min_length=3, max_length=80),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    if normalize_slug(slug) in HUB_RESERVED_SLUGS:
        return {"slug": normalize_slug(slug), "available": False, "owned_by_current_user": False}
    normalized = validate_slug(slug)
    result = await db.execute(select(AgentHub).where(AgentHub.slug == normalized))
    hub = result.scalar_one_or_none()
    available = hub is None or hub.owner_id == current_user.id

    return {
        "slug": normalized,
        "available": available,
        "owned_by_current_user": bool(hub and hub.owner_id == current_user.id),
    }


async def _resolve_public_hub(slug: str, db: AsyncSession) -> tuple[AgentHub, User]:
    normalized = normalize_slug(slug)
    result = await db.execute(select(AgentHub).where(AgentHub.slug == normalized))
    hub = result.scalar_one_or_none()
    if not hub:
        # Fall back to a slug this hub used to have - keeps already-printed links/QR codes
        # (e.g. an Open House Cheat Sheet) working after the agent rebrands their hub URL.
        fallback_result = await db.execute(
            select(AgentHub).where(AgentHub.previous_slugs.op('@>')(cast([normalized], JSONB)))
        )
        hub = fallback_result.scalar_one_or_none()
    if not hub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This Agent Hub link isn't active.")

    # Gate on the OWNER's current plan tier, not the hub row's existence - a downgrade to BASIC
    # or plain NO_MLS (the tiers with no Agent Hub access, see AGENT_HUB_ACCESS_TIERS) must stop
    # the public page from resolving, but the saved config itself is never touched here, so it comes
    # back exactly as it was the moment the agent upgrades again. Same 404 message as "no such
    # hub" so this doesn't leak which slugs exist versus which are just tier-locked. An ended
    # subscription stops it the same way (owner_has_access also checks the subscription is live).
    owner = await db.get(User, hub.owner_id)
    if not owner_has_access(owner, AGENT_HUB_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This Agent Hub link isn't active.")

    # Every agent gets a hub URL automatically (_ensure_hub - it's the root of their form links),
    # so a hub the agent never actually built must not go public as a blank page. Minimum to go
    # live: their name and at least one way to reach them. The Settings nudges
    # (useSettingsCompleteness) push them to fill exactly these in.
    if not _hub_has_minimum_content(hub.config or {}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This Agent Hub isn't set up yet.")

    return hub, owner


def _hub_has_minimum_content(config: dict) -> bool:
    profile = config.get("profile") or {}
    contact = config.get("contact") or {}
    has_name = bool(str(profile.get("name") or "").strip())
    has_contact = any(str(contact.get(k) or "").strip() for k in ("call", "text", "email"))
    return has_name and has_contact


async def _with_listing_links(db: AsyncSession, owner: User, config: dict, src: str) -> dict:
    """"Smart QR" per listing card, same resolution the email signature's featured-listing banner
    uses (property_website_service.resolve_listing_link): the agent's own published Listing
    Spotlight for that exact property when they have one, else the generic one-page property view.
    `listingLinkUrl` is additive - a card with none falls back to the public Sales page (where
    current listings lead) on the frontend. `src` tags the link for lead attribution."""
    my_listings = config.get("myListings")
    if not (isinstance(my_listings, list) and my_listings):
        return config
    from app.services.visit_source import with_src
    overrides = config.get("listingLinkOverrides") or {}
    resolved_listings = []
    for item in my_listings:
        if not isinstance(item, dict):
            resolved_listings.append(item)
            continue
        item = dict(item)
        listing_key = item.get("id") if item.get("source") != "manual" else None
        fallback_address = item.get("address") if item.get("source") == "manual" else None
        override_key = property_website_service.listing_override_key(listing_key, fallback_address)
        skip_spotlight = overrides.get(override_key) == "property_page"
        item["listingLinkUrl"] = with_src(await property_website_service.resolve_listing_link(db, owner, listing_key, fallback_address, skip_spotlight=skip_spotlight), src)
        resolved_listings.append(item)
    return {**config, "myListings": resolved_listings}


@router.get("/public/{slug}")
async def get_public_agent_hub(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    hub, owner = await _resolve_public_hub(slug, db)
    # A linked YouTube channel refreshes once a day - if it's due, refresh in the background so
    # the next visit shows the new uploads (this page never waits on YouTube).
    from app.services.agent_hub_video_sync import refresh_in_background, wants_sync
    if wants_sync(hub.config):
        refresh_in_background(hub.id)
    # Today's listings + closed sales merged into the saved lists - a new listing or a new sale
    # shows up on its own (see agent_hub_live).
    from app.services.agent_hub_live import live_hub_config
    config = await live_hub_config(db, owner, hub.config or {})
    # Tagged src=hub, so a message sent from that listing's page says it came from the Agent Hub.
    config = await _with_listing_links(db, owner, config, "hub")
    # "My Brand Kit" (the default): the hub wears the agent's Brand Kit look, read live so a
    # Brand Kit change shows here on the next visit (services/brand_look_service).
    from app.services import brand_look_service
    config = brand_look_service.hub_config_with_brand(config, await brand_look_service.get_look(db, owner))
    # The Luxe hub theme is IDX only - after a downgrade the page shows the Classic look (the
    # saved choice stays, so an upgrade brings it straight back).
    if config.get("hubTheme") == "luxe" and not luxe_theme_allowed(owner):
        config = {**config, "hubTheme": "classic"}
    # slug reflects the CURRENT canonical URL even when resolved via an old alias, so callers
    # can redirect the visitor to it.
    return {"slug": hub.slug, "config": config}


async def hub_own_domain_base(db: AsyncSession, slug: str) -> tuple[str | None, str | None]:
    """(canonical hub slug, "https://<own domain>" or None) for openhousepal.com/<slug>'s forward
    to the agent's own domain. The ONE rule is site_service.owner_links().own_domain - the same one
    that moves every link the app hands out (and the setup flow's "Your link" note,
    setup_flow_service.hub_link_info) onto the domain: IDX plan, "use my own domain" on, website
    published and switched on, the domain ACTIVE + primary and resolving in DNS. Plus the hub must
    still be live for its owner (plan/subscription). Anything else - no redirect."""
    normalized = normalize_slug(slug)
    if not SLUG_PATTERN.fullmatch(normalized):
        return None, None
    # Same lookup as _resolve_public_hub (an old, rebranded-away-from slug still counts), minus its
    # "hub has a name + contact" check - the hub's form links forward even before the hub page is built.
    hub = (await db.execute(select(AgentHub).where(AgentHub.slug == normalized))).scalar_one_or_none()
    if not hub:
        hub = (await db.execute(
            select(AgentHub).where(AgentHub.previous_slugs.op('@>')(cast([normalized], JSONB)))
        )).scalars().first()
    if not hub:
        return None, None
    owner = await db.get(User, hub.owner_id)
    if not owner_has_access(owner, AGENT_HUB_ACCESS_TIERS):
        return None, None
    # Only an IDX agent with a website can have one; checked first so this public lookup never
    # creates a site row (owner_links -> get_or_create_site), same as hub_link_info.
    from app.models.database import Site
    if owner.plan_tier != "IDX" or not (await db.execute(select(Site.id).where(Site.owner_id == owner.id))).first():
        return hub.slug, None
    links = await owner_links(db, owner, hub.slug)
    return hub.slug, (links.base if links.own_domain else None)


@router.get("/public/{slug}/own-domain")
async def get_public_hub_own_domain(slug: str, db: AsyncSession = Depends(get_db)):
    """Where openhousepal.com/<slug> forwards to (frontend proxy.ts) - {"own_domain": null} = stay."""
    try:
        canonical, base = await hub_own_domain_base(db, slug)
    except Exception:  # noqa: BLE001 - fail open: the hub just stays on openhousepal.com
        logger.exception("hub own-domain lookup failed for %s", slug)
        canonical, base = None, None
    return {"slug": canonical, "own_domain": base}


@router.get("/public/{slug}/signature-card")
async def get_public_signature_card(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """What the agent's Email Signature "site version" card needs to render on their public pages
    (it closes blog articles). Plans with Email Signature only (IDX, No-MLS Premium) - everyone else
    gets 404 and the article keeps its small contact card.
    Same inputs the Studio's Site Version preview uses: the resolved signature config, the live hub
    config (listings/sales/socials/profile) and the agent's form links for the CTA buttons."""
    from app.models.database import AgentForm, EmailSignature
    from app.services import email_signature_service
    from app.services.agent_hub_live import live_hub_config
    hub, owner = await _resolve_public_hub(slug, db)
    sig = (await db.execute(select(EmailSignature).where(EmailSignature.owner_id == owner.id))).scalar_one_or_none()
    if not email_signature_service.signature_plan_allowed(owner) or not sig or not sig.config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No signature card.")
    forms = (await db.execute(select(AgentForm.form_type, AgentForm.slug, AgentForm.title).where(AgentForm.owner_id == owner.id))).all()
    config = await live_hub_config(db, owner, hub.config or {})
    # Each listing carries its own click destination (Spotlight / property page) so the card's
    # rotating featured listings link to the home itself, not a general page.
    config = await _with_listing_links(db, owner, config, "email")
    return {
        "slug": hub.slug,
        "owner_id": owner.id,
        "config": email_signature_service.resolve_signature_config(sig.config),
        "hub_config": {k: config.get(k) for k in ("profile", "contact", "profilePhotoUrl", "profilePhotoPosition", "socialLinks", "myListings", "recentSales", "salesMinPrice")},
        "forms": [{"form_type": f.form_type, "slug": f.slug, "title": f.title} for f in forms],
        "fallback_photo": getattr(owner, "photo_url", None),
    }


@router.get("/public/{slug}/vcard")
async def get_public_agent_hub_vcard(
    slug: str,
    visitor_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    viewer: Optional[User] = Depends(get_current_user_optional),
):
    """Powers the Agent Hub's "Save Contact" button - generates a .vcf on the fly from exactly
    the fields the agent opted into under Contact Card settings (config.contactCard), so tapping
    it opens the visitor's native contact-save flow on iOS/Android/desktop with no manual entry."""
    hub, owner = await _resolve_public_hub(slug, db)

    config = hub.config or {}
    fields = vcard_service.resolve_contact_card_fields(config)
    photo_base64 = None
    if fields["headshot"]["include"]:
        photo_url = config.get("profilePhotoUrl") or owner.photo_url
        photo_base64 = await vcard_service.fetch_photo_base64(photo_url)

    agent_hub_url = ""
    if fields["agentHubUrl"]["include"]:
        agent_hub_url = (await owner_links(db, owner, hub.slug)).hub()

    from app.services.website_link_service import apply_auto_website
    vcard_text = vcard_service.build_vcard_text(hub, owner, agent_hub_url, photo_base64,
                                                config=await apply_auto_website(db, owner, hub.config or {}, hub.slug))

    # Same anonymous-only rule as every other Agent Hub event - a logged-in viewer (the owner
    # previewing their own card) shouldn't inflate the count.
    if not viewer:
        db.add(AgentHubEvent(owner_id=hub.owner_id, event_type="save_contact", visitor_id=visitor_id))
        await db.commit()

    display_name = ((config.get("profile") or {}).get("name") or "").strip() or f"{owner.first_name} {owner.last_name}".strip() or "contact"
    safe_filename = re.sub(r"[^A-Za-z0-9 _-]", "", display_name).strip() or "contact"
    return Response(
        content=vcard_text.encode("utf-8"),
        media_type="text/vcard",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}.vcf"'},
    )


@router.post("/public/{slug}/track", status_code=status.HTTP_204_NO_CONTENT)
async def track_agent_hub_event(
    slug: str,
    payload: AgentHubTrackEvent,
    db: AsyncSession = Depends(get_db),
    viewer: Optional[User] = Depends(get_current_user_optional),
):
    if payload.event_type not in {"visit", "qr_scan", "interaction", "save_contact"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid event type.")

    # Only count fully anonymous visits - a logged-in viewer (the owner previewing, or any
    # other agent browsing while signed in) is staff traffic, not a real guest.
    if viewer:
        return None

    normalized = normalize_slug(slug)
    result = await db.execute(select(AgentHub).where(AgentHub.slug == normalized))
    hub = result.scalar_one_or_none()
    if not hub:
        return None

    db.add(AgentHubEvent(owner_id=hub.owner_id, event_type=payload.event_type, visitor_id=payload.visitor_id))
    await db.commit()
    return None


@router.get("/me/analytics")
async def get_my_agent_hub_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_agent_hub_access),
):
    since = datetime.now(timezone.utc) - timedelta(days=60)

    events_result = await db.execute(
        select(AgentHubEvent.event_type, AgentHubEvent.created_at)
        .where(AgentHubEvent.owner_id == current_user.id, AgentHubEvent.created_at >= since)
    )
    events = [
        {"type": "visit" if event_type == "visit" else "qr", "timestamp": created_at.isoformat()}
        for event_type, created_at in events_result.all()
    ]

    submissions_result = await db.execute(
        select(FormSubmission.created_at)
        .where(FormSubmission.owner_id == current_user.id, FormSubmission.created_at >= since)
    )
    events.extend({"type": "form", "timestamp": created_at.isoformat()} for (created_at,) in submissions_result.all())

    return events


async def _ensure_hub(db: AsyncSession, user: User) -> AgentHub:
    """Every active agent gets an AgentHub row + URL slug, on every plan - not just the Agent Hub
    tiers. The row is more than the public Agent Hub page: its slug is the root of EVERY public
    form link/QR code (openhousepal.com/{slug}/{form}) and its config holds the Settings profile
    (bio/contact) the kits print. Without this, Basic and No-MLS Basic agents - whose plans
    include Forms - had no slug at all, so their form links pointed at a URL that 404'd. The
    public Agent Hub PAGE stays gated separately (_resolve_public_hub)."""
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    if hub:
        return hub
    base = re.sub(r"[^a-z0-9]+", "-", f"{user.first_name or ''} {user.last_name or ''}".lower()).strip("-") or "agent"
    base = base[:60]
    for index in range(50):
        candidate = base if index == 0 else f"{base}-{index + 1}"
        if candidate in HUB_RESERVED_SLUGS:
            continue
        taken = (await db.execute(
            select(AgentHub.id).where(or_(
                AgentHub.slug == candidate,
                AgentHub.previous_slugs.op('@>')(cast([candidate], JSONB)),
            ))
        )).first()
        if taken:
            continue
        hub = AgentHub(owner_id=user.id, slug=candidate)
        from app.services.setup_flow_service import seed_hub_from_user
        seed_hub_from_user(user, hub)
        db.add(hub)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue
        await db.refresh(hub)
        return hub
    hub = AgentHub(owner_id=user.id, slug=f"{base}-{uuid.uuid4().hex[:6]}")
    db.add(hub)
    await db.commit()
    await db.refresh(hub)
    return hub


@router.get("/me/marketing-moments")
async def get_marketing_moments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """What deserves marketing right now, from the synced MLS mirror (never Bright): the agent's
    on-market listings without a Listing Spotlight yet (newest first, `is_new` = listed in the last
    3 weeks) - Dash's "New listing - make a Spotlight" card and the Spotlight page's suggestions -
    plus their newest listing and latest closed sale for Studio's one-tap shortcuts."""
    from datetime import date
    from app.models.database import PropertyWebsite
    from app.services.agent_hub_live import live_listings, _listing_item, nice
    from app.services.website.content_adapters import get_recent_sales

    # No-MLS plans: the mirror is Bright data - nothing to suggest from it.
    if not tier_has_mls(current_user.plan_tier):
        return {"needs_spotlight": [], "newest_listing": None, "latest_sale": None, "studio_picks": []}

    listings = await live_listings(db, current_user)
    keys_with_site = set((await db.execute(
        select(PropertyWebsite.listing_key).where(PropertyWebsite.owner_id == current_user.id, PropertyWebsite.listing_key.is_not(None))
    )).scalars().all())

    def card(p: Property) -> dict:
        item = _listing_item(p)
        # Bright's count, kept current (a relisted home's DOM isn't "days since first listed")
        from app.utils.listing_dom import property_days_on_market
        days = property_days_on_market(p)
        return {**item, "listing_key": p.listing_key, "listed_at": p.mls_list_date.isoformat() if p.mls_list_date else None,
                "days_on_market": days, "is_new": days is not None and days <= 21, "has_spotlight": p.listing_key in keys_with_site}

    cards = [card(p) for p in listings]
    sales = await get_recent_sales(db, current_user, limit=1)
    latest_sale = None
    if sales:
        s0 = sales[0]
        sold = s0.get("sold_date")
        latest_sale = {
            "id": str(s0.get("listing_key") or s0.get("id")), "address": nice((s0.get("address") or "").split(",")[0]),
            "city": nice(s0.get("city")), "state": s0.get("state") or "", "price": s0.get("price"), "image": s0.get("photo"),
            "sold_date": sold.isoformat() if hasattr(sold, "isoformat") else sold,
            "beds": s0.get("bedrooms"), "baths": s0.get("bathrooms"), "sqft": s0.get("living_area"),
        }
    # Studio's "Fresh from the MLS" picks: every listing from the last 3 weeks and every sale
    # closed in the last 45 days that the agent hasn't marketed yet. One disappears for good once a
    # design is made for it (or the agent dismisses it) - Studio only nudges for the bare minimum.
    from app.models.database import DesignProject
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))).scalar_one_or_none()
    done = {str(k) for k in ((hub.config or {}).get("studioMomentsDone") or [])} if hub else set()
    designed = set()
    for lp in (await db.execute(select(DesignProject.linked_property).where(DesignProject.owner_id == current_user.id))).scalars().all():
        if isinstance(lp, dict):
            if lp.get("listing_key"):
                designed.add(str(lp["listing_key"]))
            if lp.get("address"):
                designed.add(str(lp["address"]).split(",")[0].strip().lower())

    def is_done(key: str, address: str) -> bool:
        return key in done or key in designed or (address or "").split(",")[0].strip().lower() in designed

    picks = []
    for c in cards:
        if c.get("is_new") and not is_done(str(c["listing_key"]), c.get("address") or ""):
            picks.append({**c, "kind": "listing", "key": str(c["listing_key"])})
    recent_sales = await get_recent_sales(db, current_user, limit=8)
    for s0 in recent_sales:
        sold = s0.get("sold_date")
        if isinstance(sold, datetime):
            sold_d = sold.date()
        elif isinstance(sold, date):
            sold_d = sold
        else:
            try:
                sold_d = date.fromisoformat(str(sold)[:10]) if sold else None
            except ValueError:
                sold_d = None
        if not sold_d or (date.today() - sold_d).days > 45:
            continue
        key = str(s0.get("listing_key") or s0.get("id"))
        address = nice((s0.get("address") or "").split(",")[0])
        if is_done(key, address):
            continue
        picks.append({
            "kind": "sale", "key": key, "id": key, "address": address, "city": nice(s0.get("city")), "state": s0.get("state") or "",
            "price": s0.get("price"), "image": s0.get("photo"), "sold_date": sold_d.isoformat(),
            "beds": s0.get("bedrooms"), "baths": s0.get("bathrooms"), "sqft": s0.get("living_area"),
        })
    return {
        "needs_spotlight": [c for c in cards if not c["has_spotlight"]],
        "newest_listing": cards[0] if cards else None,
        "latest_sale": latest_sale,
        "studio_picks": picks,
    }


class StudioMomentDone(BaseModel):
    key: str


@router.post("/me/marketing-moments/done")
async def mark_marketing_moment_done(
    payload: StudioMomentDone,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Studio's recommendation for this listing/sale is handled (a design was made, or the agent
    dismissed it) - it never comes back."""
    hub = await _ensure_hub(db, current_user)
    config = dict(hub.config or {})
    done = [str(k) for k in (config.get("studioMomentsDone") or [])]
    if payload.key and payload.key not in done:
        done = (done + [payload.key[:120]])[-300:]
    config["studioMomentsDone"] = done
    hub.config = config
    await db.commit()
    return {"ok": True}


# /me, /me/config, /me/profile and /me/photo are the agent's own profile - Settings reads and saves
# it through these. Any signed-in agent: one waiting on Bright's approval (or on paying, or whose plan
# ended) still fills in Settings like the setup flow lets them. The public page is what the plan
# gates (owner_has_access on /public/{slug}).
@router.get("/me")
async def get_my_agent_hub(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    hub = await _ensure_hub(db, current_user)
    # Blanks (license, email, photo...) filled from what the agent typed at sign-up.
    from app.services.setup_flow_service import seed_hub_from_user
    if seed_hub_from_user(current_user, hub):
        await db.commit()
    # The editor sees the same live lists the public page shows (new listings / sales included).
    from app.services.agent_hub_live import live_hub_config
    from app.services.agent_hub_video_sync import refresh_in_background, wants_sync
    if wants_sync(hub.config):
        refresh_in_background(hub.id)
    # While the hub follows the Brand Kit the editor sees the brand look in the config (with
    # lookSource 'brand', so a save from the editor keeps following it) - brand_look_service.
    from app.services import brand_look_service
    look = await brand_look_service.get_look(db, current_user)
    config = brand_look_service.hub_config_with_brand(await live_hub_config(db, current_user, hub.config or {}), look)
    return {"id": hub.id, "slug": hub.slug, "status": hub.status, "config": config, "brand_look": look}


@router.put("/me/config")
async def update_my_agent_hub_config(
    payload: AgentHubConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Persists the full Agent Hub page-builder payload so it's visible from any device/session,
    and so it's what actually gets served to public visitors at /agent-hub/public/{slug}."""
    serialized = json.dumps(payload.config)
    if len(serialized.encode("utf-8")) > AGENT_HUB_CONFIG_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This Agent Hub page is too large to save. Try removing some images or links.",
        )

    hub = await _ensure_hub(db, current_user)
    hub.config = payload.config
    await db.commit()
    return {"config": hub.config}


@router.get("/me/listing-routing")
async def get_my_listing_routing(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_agent_hub_access),
):
    """Settings-only visibility into the "smart QR" decision agent_hub_routes.get_public_agent_hub
    and the email signature both make silently: for each of the agent's current listings, is a
    click currently routed to their own Listing Spotlight or the generic one-page property view -
    and, when a Spotlight match exists, is it currently overridden off. This is the one place
    both features' listing links are explained, since they resolve off the same
    AgentHub.config['myListings'] pool and the same override map."""
    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))
    hub = result.scalar_one_or_none()
    if not hub:
        return {"listings": []}

    config = hub.config or {}
    my_listings = [item for item in (config.get("myListings") or []) if isinstance(item, dict)]
    overrides = config.get("listingLinkOverrides") or {}

    rows = []
    for item in my_listings:
        listing_key = item.get("id") if item.get("source") != "manual" else None
        fallback_address = item.get("address") if item.get("source") == "manual" else None
        override_key = property_website_service.listing_override_key(listing_key, fallback_address)
        natural = await property_website_service.describe_listing_link(db, current_user, listing_key, fallback_address)
        overridden = bool(override_key) and natural["source"] == "spotlight" and overrides.get(override_key) == "property_page"
        effective = natural
        if overridden:
            effective = await property_website_service.describe_listing_link(db, current_user, listing_key, fallback_address, skip_spotlight=True)
        rows.append({
            "id": item.get("id"),
            "address": item.get("address"),
            "city": item.get("city"),
            "state": item.get("state"),
            "status": item.get("status"),
            "overrideKey": override_key,
            "spotlightAvailable": natural["source"] == "spotlight",
            "spotlightSlug": natural.get("spotlightSlug"),
            "overridden": overridden,
            "routing": effective,
        })
    return {"listings": rows}


class ListingLinkOverrideUpdate(BaseModel):
    override_key: str
    override: str  # "auto" | "property_page"


@router.put("/me/listing-routing")
async def update_my_listing_routing(
    payload: ListingLinkOverrideUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_agent_hub_access),
):
    """Lets an agent opt a specific listing out of the Spotlight takeover (send visitors to the
    plain one-page property view even though a matching Spotlight exists) - e.g. a Spotlight
    that's still a work in progress. Only "property_page" is a meaningful stored value; "auto"
    just deletes the override and reverts to the default (Spotlight wins when one matches)."""
    if payload.override not in ("auto", "property_page"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid override value.")

    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))
    hub = result.scalar_one_or_none()
    if not hub:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Set your Agent Hub URL first.")

    config = dict(hub.config or {})
    overrides = dict(config.get("listingLinkOverrides") or {})
    if payload.override == "auto":
        overrides.pop(payload.override_key, None)
    else:
        overrides[payload.override_key] = payload.override
    config["listingLinkOverrides"] = overrides
    hub.config = config
    await db.commit()
    return {"listingLinkOverrides": overrides}


class OpenHouseRequestThemeUpdate(BaseModel):
    accent_color: str
    dark_mode: bool = False


@router.put("/me/open-house-request-theme")
async def update_open_house_request_theme(
    payload: OpenHouseRequestThemeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Theme (accent color + dark mode) for the 'Request Seller Disclosure' / 'Schedule a Second
    Visit' guest pages reached from Cheat Sheet QR codes - these are fixed-field pages (not
    AgentForm-backed, no question editor), so this is a small targeted merge into the same
    AgentHub.config JSONB blob the page builder uses, rather than a whole new settings table.
    Open to every active plan: the Cheat Sheet is part of every plan's Open House Kit."""
    hub = await _ensure_hub(db, current_user)

    config = dict(hub.config or {})
    config["openHouseRequestTheme"] = {"accentColor": payload.accent_color, "darkMode": payload.dark_mode}
    hub.config = config
    await db.commit()
    return config["openHouseRequestTheme"]


@router.get("/me/recent-sales")
async def get_my_recent_sales(
    limit: int = Query(24, ge=1, le=48),
    re_license_number: str | None = Query(None, max_length=80),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    # No-MLS agents (BOTH No-MLS tiers - PRO_NO_MLS is the one that actually reaches this, since
    # it has Agent Hub) have no Bright data rights: never issue the live Bright request for them
    # (see fetch_bright_agent_sales below) - their sales are the ones they add by hand, stored in
    # the hub config. Open to every active plan (not just Agent Hub tiers) because Basic's Brag
    # Sheet falls back to these auto-pulled Bright sales - Basic has no Agent Hub to curate in.
    if not tier_has_mls(current_user.plan_tier):
        return []

    entered_license_number = (re_license_number or "").strip()
    mls_id = (current_user.mls_id or "").strip()
    full_name = " ".join(part for part in [current_user.first_name.strip(), current_user.last_name.strip()] if part).strip()
    brokerage = (current_user.brokerage or "").strip()
    lookup_candidates = [
        ("license", entered_license_number),
        ("mls", mls_id),
        ("name", full_name),
        ("brokerage", brokerage),
    ]
    identifiers = []
    for identifier_type, identifier in lookup_candidates:
        if not identifier:
            continue
        identifiers.append((identifier_type, identifier))
        if identifier_type in {"name", "brokerage"}:
            for part in re.split(r"\s+", identifier):
                cleaned_part = part.strip()
                if len(cleaned_part) >= 3:
                    identifiers.append((identifier_type, cleaned_part))
    filters = []

    if identifiers:
        identifier_keys = [
            "BuyerAgentMlsId",
            "BuyerAgentStateLicenseNumber",
            "CoBuyerAgentMlsId",
            "CoBuyerAgentStateLicenseNumber",
            "ListAgentMlsId",
            "ListAgentStateLicenseNumber",
            "CoListAgentMlsId",
            "CoListAgentStateLicenseNumber",
            "BuyerAgentFullName",
            "CoBuyerAgentFullName",
            "ListAgentFullName",
            "CoListAgentFullName",
            "BuyerOfficeName",
            "CoBuyerOfficeName",
            "ListOfficeName",
            "CoListOfficeName",
        ]
        for identifier_type, identifier in identifiers:
            normalized_identifier = re.sub(r"\W+", "", identifier)
            for key in identifier_keys:
                filters.append(Property.raw_mls_data[key].astext.ilike(f"%{identifier}%"))
            if normalized_identifier and normalized_identifier != identifier:
                for key in identifier_keys:
                    filters.append(Property.raw_mls_data[key].astext.ilike(f"%{normalized_identifier}%"))
            filters.append(Property.listing_id.ilike(f"%{identifier}%"))
            if identifier_type == "name":
                name_parts = [part for part in re.split(r"\s+", identifier) if part]
                for part in name_parts:
                    for key in ["BuyerAgentFullName", "CoBuyerAgentFullName", "ListAgentFullName", "CoListAgentFullName"]:
                        filters.append(Property.raw_mls_data[key].astext.ilike(f"%{part}%"))
            if identifier_type == "brokerage":
                for key in ["BuyerOfficeName", "CoBuyerOfficeName", "ListOfficeName", "CoListOfficeName"]:
                    filters.append(Property.raw_mls_data[key].astext.ilike(f"%{identifier}%"))

    if not filters:
        return []

    bright_sales: list[dict] = []

    async def _gather_bright_sales() -> list[dict]:
        out: list[dict] = []
        for lookup_identifier_type, lookup_identifier in identifiers:
            candidate_sales = await fetch_bright_agent_sales(lookup_identifier, lookup_identifier_type, limit)
            if candidate_sales:
                out.extend(candidate_sales)
        return out

    # Overall 10s ceiling on the live Bright leg: each identifier fans out to several Bright
    # calls and there can be ~5 identifiers, so a slow Bright could otherwise hang the Agent Hub
    # page for a minute. On timeout, fall straight through to the local `properties` fallback
    # below. Real fix = serve recent sales from our synced DB only, the way
    # content_adapters.get_recent_sales already does for the public site (see "Do Once in
    # Production").
    try:
        bright_sales = await asyncio.wait_for(_gather_bright_sales(), timeout=6)
    except asyncio.TimeoutError:
        logger.warning(
            "Agent Hub recent-sales Bright lookup exceeded 6s overall - using local DB fallback",
            extra={"user_id": current_user.id},
        )
        bright_sales = []

    if bright_sales:
        deduped_sales = {}
        for sale in bright_sales:
            deduped_sales[sale["id"]] = sale
        bright_sales = sorted(
            deduped_sales.values(),
            key=lambda sale: str(sale.get("sold_date") or ""),
            reverse=True,
        )[:limit]
        logger.info(
            "Agent Hub recent sales from Bright",
            extra={
                "bright_count": len(bright_sales),
                "has_entered_license": bool(entered_license_number),
                "has_mls_id": bool(mls_id),
                "has_name": bool(full_name),
                "has_brokerage": bool(brokerage),
            },
        )
        return bright_sales

    async def fetch_properties(active_filters: list) -> list[Property]:
        result = await db.execute(
            select(Property)
            .where(or_(*active_filters))
            .order_by(Property.mls_list_date.desc().nullslast(), Property.updated_at.desc().nullslast())
            .limit(limit)
        )
        return result.scalars().all()

    # This local fallback query ORs ~200 unindexed raw_mls_data JSONB substring matches over a
    # ~300k-row table, so it can also run long until the proper fix lands (promote buyer/co-buyer
    # agent ids to real indexed columns in the sync - see "Do Once in Production"). Cap it so the
    # Agent Hub page never hangs; an empty result beats a spinner.
    try:
        properties = await asyncio.wait_for(fetch_properties(filters), timeout=6)
    except asyncio.TimeoutError:
        logger.warning(
            "Agent Hub recent-sales local fallback query exceeded 6s - returning empty",
            extra={"user_id": current_user.id},
        )
        return []

    sales_by_listing = {}
    for property in properties:
        sale = {
            "id": property.id,
            "address": property.full_address or property.street_address,
            "city": property.city,
            "state": property.state,
            "price": property.price,
            "status": property.home_status,
            "image": property.img_src or (property.photos[0] if isinstance(property.photos, list) and property.photos else None),
            "sold_date": property.modification_timestamp or property.mls_list_date,
        }
        if is_eligible_recent_sale(sale):
            sales_by_listing[property.listing_id or property.listing_key or property.id] = apply_recent_sale_defaults(sale)

    logger.info(
        "Agent Hub recent sales merge",
        extra={
            "local_count": len(properties),
            "has_entered_license": bool(entered_license_number),
            "has_mls_id": bool(mls_id),
            "used_account_fallback": bool(entered_license_number and properties),
        },
    )

    return sorted(
        sales_by_listing.values(),
        key=lambda sale: str(sale.get("sold_date") or ""),
        reverse=True,
    )[:limit]


@router.get("/link-preview")
async def get_link_preview(
    url: str = Query(..., min_length=8, max_length=2048),
    current_user: User = Depends(require_agent_hub_access),
):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a valid http or https URL.")

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            from app.utils.safe_fetch import safe_get
            response = await safe_get(client, url, headers={"User-Agent": "OpenHousePalBot/1.0"})
            response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to read this link.")

    content = response.text[:500000]
    title = extract_meta(content, "og:title") or extract_meta(content, "twitter:title") or extract_title(content)
    image = extract_meta(content, "og:image") or extract_meta(content, "twitter:image")
    if image:
        image = urljoin(str(response.url), image)

    return {"title": title, "image": image}


async def _fetch_recent_youtube_videos(
    client: httpx.AsyncClient,
    feed_url: str,
    max_videos: int = 15,
    fallback_title: str = "YouTube Video",
) -> list[dict]:
    feed = await client.get(feed_url)
    feed.raise_for_status()

    entries = []
    for entry in re.findall(r"<entry>(.*?)</entry>", feed.text, flags=re.DOTALL):
        video_id_match = re.search(r"<yt:videoId>(.*?)</yt:videoId>", entry)
        title_match = re.search(r"<title>(.*?)</title>", entry, flags=re.DOTALL)
        published_match = re.search(r"<published>(.*?)</published>", entry)
        if video_id_match:
            entries.append((video_id_match.group(1), title_match, published_match))

    # Checking each entry for "is this a Short" requires a live request per video.
    # Doing that sequentially for up to 15+ entries was slow enough to time out -
    # run them concurrently (bounded, so we don't hammer YouTube) instead.
    semaphore = asyncio.Semaphore(5)

    async def is_short(video_id: str) -> bool:
        async with semaphore:
            try:
                video_page = await client.get(f"https://www.youtube.com/watch?v={video_id}", headers={"User-Agent": "OpenHousePalBot/1.0"})
                return bool(re.search(r'<link rel="canonical" href="https://www\.youtube\.com/shorts/', video_page.text))
            except httpx.HTTPError:
                return False

    short_flags = await asyncio.gather(*(is_short(video_id) for video_id, _, _ in entries))

    videos = []
    for (video_id, title_match, published_match), short in zip(entries, short_flags):
        if short:
            continue
        clean_title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else fallback_title
        videos.append({
            "id": f"youtube-{video_id}",
            "title": clean_title,
            "description": published_match.group(1)[:10] if published_match else "YouTube",
            "link": f"https://www.youtube.com/watch?v={video_id}",
            "image": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        })
        if len(videos) >= max_videos:
            break

    return videos


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


async def _fetch_channel_grid_videos(client: httpx.AsyncClient, channel_url: str, max_videos: int = 15) -> list[dict]:
    # The RSS feed (`feeds/videos.xml?channel_id=...`) only ever exposes the channel's
    # most recent 15 uploads *of any kind*, and on channels that post a lot of Shorts
    # most of those 15 get filtered out by the Shorts check - leaving far fewer than 15
    # real videos with no way to page further back via RSS. The channel's own "Videos"
    # tab already excludes Shorts (they live under a separate "Shorts" tab), so scraping
    # it directly gives us up to ~30 guaranteed-non-Short videos instead.
    # Strip query strings / fragments (e.g. the "?si=..." tracking param YouTube's
    # mobile share sheet adds) before appending "/videos" - otherwise it gets appended
    # *after* the query string, producing a URL that 404s or serves an unparseable page.
    parsed_url = urlparse(channel_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    normalized = re.sub(r"/(videos|shorts|streams|playlists|featured|community|about)/?$", "", base_url.rstrip("/"))
    page = await client.get(f"{normalized}/videos", headers={"User-Agent": BROWSER_USER_AGENT})
    page.raise_for_status()

    data_match = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", page.text, flags=re.DOTALL)
    if not data_match:
        return []

    try:
        data = json.loads(data_match.group(1))
    except json.JSONDecodeError:
        return []

    tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
    videos_tab = None
    for tab in tabs:
        tab_renderer = tab.get("tabRenderer")
        if not tab_renderer:
            continue
        tab_url = tab_renderer.get("endpoint", {}).get("commandMetadata", {}).get("webCommandMetadata", {}).get("url", "")
        if tab_url.endswith("/videos"):
            videos_tab = tab_renderer
            break
    if not videos_tab:
        return []

    grid_items = videos_tab.get("content", {}).get("richGridRenderer", {}).get("contents", [])
    videos = []
    for item in grid_items:
        lockup = item.get("richItemRenderer", {}).get("content", {}).get("lockupViewModel")
        if not lockup or lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        video_id = lockup.get("contentId")
        if not video_id:
            continue
        title = lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {}).get("content")
        videos.append({
            "id": f"youtube-{video_id}",
            "title": title or "YouTube Video",
            "description": "YouTube",
            "link": f"https://www.youtube.com/watch?v={video_id}",
            "image": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        })
        if len(videos) >= max_videos:
            break
    return videos


async def _resolve_channel_id(client: httpx.AsyncClient, channel_url: str) -> str:
    channel_id_match = re.search(r"/channel/([^/?#]+)", channel_url)
    if channel_id_match:
        return channel_id_match.group(1)

    page = await client.get(channel_url, headers={"User-Agent": "OpenHousePalBot/1.0"})
    page.raise_for_status()
    # Prefer the canonical/meta tags - they always describe the page's own
    # channel. The generic "channelId":"UC..." pattern can match the first
    # channel id referenced *anywhere* on the page (e.g. a related-channel
    # suggestion), which silently pulled videos from the wrong channel.
    channel_id_match = (
        re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[^"]+)"', page.text)
        or re.search(r'<meta itemprop="channelId" content="([^"]+)"', page.text)
        or re.search(r'"channelId":"(UC[^"]+)"', page.text)
    )
    return channel_id_match.group(1) if channel_id_match else ""


@router.get("/youtube/videos")
async def get_youtube_channel_videos(
    channel_url: str = Query(..., min_length=8, max_length=2048),
    current_user: User = Depends(require_agent_hub_access),
):
    parsed = urlparse(channel_url)
    # YouTube only - the channel page is fetched server-side, so any other host is refused.
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (host == "youtube.com" or host.endswith(".youtube.com")):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a valid YouTube URL.")

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            videos = await _fetch_channel_grid_videos(client, channel_url)
            if videos:
                logger.info("Agent Hub youtube/videos: grid scrape returned %d videos", len(videos))
            else:
                # Structure changed or this channel didn't parse - fall back to the
                # older RSS-feed approach rather than failing outright.
                logger.warning("Agent Hub youtube/videos: grid scrape returned 0, falling back to RSS for %s", channel_url)
                channel_id = await _resolve_channel_id(client, channel_url)
                if channel_id:
                    videos = await _fetch_recent_youtube_videos(client, f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
                logger.warning("Agent Hub youtube/videos: RSS fallback returned %d videos", len(videos))
    except httpx.HTTPError as exc:
        logger.warning("Agent Hub youtube/videos: request failed for %s: %s", channel_url, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to read this YouTube channel.")

    if not videos:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not find this YouTube channel.")

    return {"channel_url": channel_url, "videos": videos}


@router.get("/youtube/playlist-videos")
async def get_youtube_playlist_videos(
    playlist_url: str = Query(..., min_length=8, max_length=2048),
    current_user: User = Depends(require_agent_hub_access),
):
    parsed = urlparse(playlist_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a valid YouTube URL.")

    playlist_id_match = re.search(r"[?&]list=([^&#]+)", playlist_url)
    playlist_id = playlist_id_match.group(1) if playlist_id_match else ""
    if not playlist_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not find a playlist in this link.")

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            videos = await _fetch_recent_youtube_videos(
                client,
                f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}",
                fallback_title="House Tour",
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to read this YouTube playlist.")

    return {"playlist_url": playlist_url, "videos": videos}


@router.put("/me/slug")
async def update_my_agent_hub_slug(
    payload: AgentHubSlugUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    slug = validate_slug(payload.slug)
    slug_result = await db.execute(select(AgentHub).where(AgentHub.slug == slug))
    existing_slug_hub = slug_result.scalar_one_or_none()
    if existing_slug_hub and existing_slug_hub.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Agent Hub URL is already being used.",
        )

    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))
    hub = result.scalar_one_or_none()

    if hub:
        if hub.slug != slug:
            # Keep the old slug resolving (see get_public_agent_hub) so any link/QR already
            # printed with it - e.g. an Open House Cheat Sheet - doesn't 404 after a rebrand.
            previous = list(hub.previous_slugs or [])
            if hub.slug not in previous:
                previous.append(hub.slug)
            previous = [s for s in previous if s != slug]
            hub.previous_slugs = previous
        hub.slug = slug
    else:
        hub = AgentHub(owner_id=current_user.id, slug=slug)
        db.add(hub)

    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Agent Hub URL is already being used.",
        )

    return {"id": hub.id, "slug": hub.slug, "status": hub.status}


@router.patch("/me/profile")
async def update_my_profile(
    payload: AgentHubProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Keeps the User row's name/brokerage/title/market in sync with whatever the agent last
    saved in Settings or the Agent Hub editor's Profile section (config.profile) - public lead
    forms read agent_name/agent_brokerage/etc straight from this User row, not from config, so
    without this the form-facing info would silently go stale the moment someone edited their
    name or brokerage anywhere other than account signup."""
    if payload.title is not None:
        current_user.professional_title = payload.title.strip() or None
    if payload.market is not None:
        current_user.market = payload.market.strip() or None
    if payload.name is not None:
        name_parts = payload.name.strip().split(None, 1)
        current_user.first_name = name_parts[0] if name_parts else ""
        current_user.last_name = name_parts[1] if len(name_parts) > 1 else ""
    if payload.brokerage is not None:
        current_user.brokerage = payload.brokerage.strip() or None
    await db.commit()
    return {
        "title": current_user.professional_title,
        "market": current_user.market,
        "name": f"{current_user.first_name} {current_user.last_name}".strip(),
        "brokerage": current_user.brokerage,
    }


@router.post("/me/photo")
async def upload_my_photo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if file.content_type not in HEADSHOT_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please upload a JPG, PNG or WebP photo.",
        )

    contents = await file.read()
    if len(contents) > PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo must be 5MB or smaller.",
        )
    try:
        contents = photo_to_jpeg(contents)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    old_photo_url = current_user.photo_url
    photo_url = s3_service.upload_bytes(contents, "agent-photos", ".jpg", "image/jpeg")
    current_user.photo_url = photo_url
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))).scalar_one_or_none()
    if hub:
        cfg = dict(hub.config or {})
        cfg["profilePhotoUrl"] = photo_url
        hub.config = cfg
    await db.commit()

    s3_service.delete_by_url(old_photo_url)
    from app.services import storage_ledger_service
    await storage_ledger_service.release(db, old_photo_url)
    await storage_ledger_service.record(db, current_user.id, "agent_photo", photo_url, len(contents))

    return {"photo_url": photo_url}


@router.post("/me/sale-photo")
async def upload_my_sale_photo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    # Every plan (not just Agent Hub plans): Settings > Recent Sales / My Listings and the kit's
    # Brag Sheet "Add a sale" are on every plan - plain No-MLS and Basic add sales by hand too.
    current_user: User = Depends(require_active_plan),
):
    """Uploads a photo for a manually-added Recent Sales entry. Separate from /me/photo since
    that endpoint overwrites the user's single profile photo_url - sale photos are their own
    files and must not touch that field or get cleaned up when the profile photo changes."""
    extension = PHOTO_CONTENT_TYPES.get(file.content_type)
    if not extension:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please upload a JPG image.",
        )

    contents = await file.read()
    if len(contents) > PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo must be 5MB or smaller.",
        )
    try:
        assert_valid_jpeg(contents)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    photo_url = s3_service.upload_bytes(contents, "agent-sale-photos", extension, file.content_type)
    from app.services import storage_ledger_service
    await storage_ledger_service.record(db, current_user.id, "agent_sale_photo", photo_url, len(contents))
    return {"photo_url": photo_url}


@router.delete("/me/photo")
async def delete_my_photo(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    old_photo_url = current_user.photo_url
    current_user.photo_url = None
    # the hub keeps its own copy of the photo URL for the public page - clear it with the account's
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))).scalar_one_or_none()
    if hub and (hub.config or {}).get("profilePhotoUrl") in (old_photo_url, "/home.jpg"):
        cfg = dict(hub.config or {})
        cfg.pop("profilePhotoUrl", None)
        hub.config = cfg
    await db.commit()

    s3_service.delete_by_url(old_photo_url)
    from app.services import storage_ledger_service
    await storage_ledger_service.release(db, old_photo_url)

    return {"photo_url": None}


# ===========================================================================
# Client reviews - collected through the agent's public review link, shown on
# the Agent Hub, and (when the rating is high) nudged onward to Google / Zillow.
# ===========================================================================

class ReviewSubmit(BaseModel):
    reviewer_name: str
    reviewer_email: Optional[str] = None
    rating: int
    body: str


class ReviewOut(BaseModel):
    id: str
    reviewer_name: str
    rating: int
    body: str
    created_at: datetime
    # 'direct' (left on our review page) | 'google' | 'zillow' | 'realtor' | 'homes' | 'facebook' | 'other'
    source: str = "direct"

    class Config:
        from_attributes = True


class ReviewAdminOut(ReviewOut):
    reviewer_email: Optional[str] = None
    status: str
    source: str = "direct"
    posted_google_at: Optional[datetime] = None
    posted_zillow_at: Optional[datetime] = None


class ReviewStatusUpdate(BaseModel):
    status: str  # 'published' | 'hidden'


class ReviewSettingsUpdate(BaseModel):
    google_place_id: Optional[str] = None
    google_review_url: Optional[str] = None  # a pasted "share your review form" link (g.page/... etc)
    zillow_review_url: Optional[str] = None
    show_on_hub: Optional[bool] = None       # show the "Client Reviews" section on the public Agent Hub (default off)
    show_on_idx: Optional[bool] = None       # show the Reviews page (+ menu link) on the agent's IDX website (default off)
    min_rating: Optional[int] = None         # 4 = only 4-5 star reviews show anywhere public; 1 = all published


def _review_dests(hub: "AgentHub") -> dict:
    r = (hub.config or {}).get("reviews") or {}
    place_id = (r.get("google_place_id") or "").strip()
    google_url = (r.get("google_review_url") or "").strip()
    zillow = (r.get("zillow_review_url") or "").strip()
    # A pasted Google review link wins; otherwise build one from the Place ID (either works).
    resolved_google = google_url or (f"https://search.google.com/local/writereview?placeid={place_id}" if place_id else None)
    return {
        "google_place_id": place_id or None,
        "google_review_url": resolved_google,
        "zillow_review_url": zillow or None,
    }


def _summarize(reviews: list) -> dict:
    pub = [rv for rv in reviews if rv.status == "published"]
    if not pub:
        return {"count": 0, "average": None}
    return {"count": len(pub), "average": round(sum(rv.rating for rv in pub) / len(pub), 2)}


async def _hub_and_owner(db: AsyncSession, slug: str) -> tuple["AgentHub", User]:
    from app.models.database import AgentReview  # local import: keep module import graph flat
    normalized = normalize_slug(slug)
    hub = (await db.execute(select(AgentHub).where(AgentHub.slug == normalized))).scalar_one_or_none()
    if not hub:
        hub = (await db.execute(
            select(AgentHub).where(AgentHub.previous_slugs.op('@>')(cast([normalized], JSONB)))
        )).scalar_one_or_none()
    if not hub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This Agent Hub link isn't active.")
    owner = await db.get(User, hub.owner_id)
    if not owner_has_access(owner, REVIEWS_ACCESS_TIERS):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This Agent Hub link isn't active.")
    return hub, owner


@router.get("/public/{slug}/reviews")
async def public_list_reviews(slug: str, db: AsyncSession = Depends(get_db)):
    """Published reviews + summary + which external destinations are configured. Used by both the
    public review page and the Agent Hub's reviews section."""
    from app.models.database import AgentReview
    from app.services.review_display import review_prefs, shown_reviews, summarize
    hub, owner = await _hub_and_owner(db, slug)
    rows = (await db.execute(
        select(AgentReview).where(AgentReview.owner_id == owner.id).order_by(AgentReview.created_at.desc())
    )).scalars().all()
    cfg = (hub.config or {})
    prefs = review_prefs(cfg)
    # Only what the agent chose to show (e.g. 4 stars and up) - the count and average included.
    shown = shown_reviews(rows, prefs["min_rating"])
    prof = cfg.get("profile") or {}
    photo = cfg.get("profilePhotoUrl") or owner.photo_url or None
    return {
        "agent_name": (prof.get("name") or f"{owner.first_name} {owner.last_name}").strip(),
        "agent_first_name": (owner.first_name or (prof.get("name") or "").split(" ")[0] or "").strip(),
        "agent_photo_url": photo,
        "agent_title": prof.get("title") or owner.professional_title or None,
        "agent_brokerage": prof.get("brokerage") or owner.brokerage or None,
        "show_on_hub": prefs["show_on_hub"],
        "min_rating": prefs["min_rating"],
        "summary": summarize(shown),
        "reviews": [ReviewOut.model_validate(r) for r in shown][:50],
        "destinations": _review_dests(hub),
    }


@router.post("/public/{slug}/reviews", status_code=status.HTTP_201_CREATED)
async def public_submit_review(slug: str, payload: ReviewSubmit, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    from app.models.database import AgentReview, Notification

    hub, owner = await _hub_and_owner(db, slug)
    name = (payload.reviewer_name or "").strip()
    body = (payload.body or "").strip()
    if not name or not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Please add your name and a few words.")
    rating = max(1, min(5, int(payload.rating or 0)))
    # Review-funnel gate (same idea the paid reputation tools use): 4-5 stars are published and
    # nudged onward to Google / Zillow; 1-3 stars come in as private feedback (status "hidden")
    # so a bad day doesn't auto-post to the agent's public hub. The agent can still publish it by
    # hand from Settings -> Reviews.
    is_public = rating >= 4
    review = AgentReview(
        owner_id=owner.id,
        reviewer_name=name[:120],
        reviewer_email=(payload.reviewer_email or "").strip()[:200] or None,
        rating=rating,
        body=body[:4000],
        source="direct",
        status="published" if is_public else "hidden",
    )
    db.add(review)
    await db.flush()  # populate review.id before the Notification references it
    db.add(Notification(
        agent_id=owner.id,
        type="NEW_REVIEW",
        reference_type="REVIEW",
        reference_id=review.id,
        title=f"New {rating}-star review" if is_public else f"Private feedback ({rating}-star)",
        message=f"{name}: {body[:80]}" + ("…" if len(body) > 80 else ""),
        link="/settings#reviews",
        is_read=False,
    ))
    await db.commit()
    await db.refresh(review)
    from app.services.agent_email_service import absolute, email_agent
    email_agent(owner, "NEW_REVIEW", "new_review_received",
                f"New {rating}-star review from {name}" if is_public else f"Private feedback from {name} ({rating}-star)",
                {"reviewer_name": name, "rating": rating, "stars": "\u2605" * rating + "\u2606" * (5 - rating), "review_body": body[:1500],
                 "is_public": is_public, "link": absolute("/studio/reviews")})

    # On their lead's activity, if they're already one of the agent's leads.
    from app.services.leads_service import log_activity
    await log_activity(
        db, owner_id=owner.id, email=review.reviewer_email, source_type="review_left", source_id=review.id,
        summary=(f"Left you a {rating}-star review" if is_public
                 else f"Left you a {rating}-star review (kept private - not posted anywhere)"),
        raw_snapshot={"rating": rating, "message": body[:1500], "is_public": is_public},
    )
    await db.refresh(review)  # the log commits - reload before reading it again below

    # Unhappy private feedback gets a same-day "reach out and make it right" follow-up.
    if not is_public:
        from app.services import tasks_service
        from app.services.leads_service import find_lead_by_contact
        lead = await find_lead_by_contact(db, owner.id, review.reviewer_email, None)
        from types import SimpleNamespace
        who = lead or SimpleNamespace(name=name, email=review.reviewer_email, phone=None)
        await tasks_service.create_auto_followup(
            db,
            owner_id=owner.id,
            rule="private_feedback",
            source_id=review.id,
            title=f"Follow up with {name[:80]}",
            context=f"Left a {rating}-star review through your review link - it stayed private, not posted anywhere",
            # An alert, not a text: what happened, their words, and what to do.
            notes=tasks_service._alert_notes("private_feedback", who, SimpleNamespace(rating=rating, body=body, property_address=None)),
            message=None,
            occurred_at=datetime.now(timezone.utc),
            lead_id=lead.id if lead else None,
        )

    dests = _review_dests(hub)
    # Only nudge onward for a genuinely positive review AND only to a destination the agent set up.
    onward = dests if is_public else {"google_review_url": None, "zillow_review_url": None, "google_place_id": None}
    return {"id": review.id, "rating": rating, "is_public": is_public, "destinations": onward}


@router.post("/public/{slug}/reviews/{review_id}/posted", status_code=status.HTTP_204_NO_CONTENT)
async def public_mark_review_posted(
    slug: str, review_id: str, where: str = Query(..., pattern="^(google|zillow)$"),
    db: AsyncSession = Depends(get_db),
):
    """Best-effort: the reviewer clicked "I posted it" for Google or Zillow on the public flow."""
    from app.models.database import AgentReview
    _, owner = await _hub_and_owner(db, slug)
    review = (await db.execute(
        select(AgentReview).where(AgentReview.id == review_id, AgentReview.owner_id == owner.id)
    )).scalar_one_or_none()
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found.")
    now = datetime.now(timezone.utc)
    if where == "google":
        review.posted_google_at = now
    else:
        review.posted_zillow_at = now
    await db.commit()


@router.get("/reviews")
async def list_my_reviews(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    from app.models.database import AgentReview
    rows = (await db.execute(
        select(AgentReview).where(AgentReview.owner_id == current_user.id).order_by(AgentReview.created_at.desc())
    )).scalars().all()
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))).scalar_one_or_none()
    r = ((hub.config if hub else {}) or {}).get("reviews") or {}
    from app.services.review_display import review_prefs
    prefs = review_prefs((hub.config if hub else {}) or {})
    return {
        "summary": _summarize(rows),
        "reviews": [ReviewAdminOut.model_validate(x) for x in rows],
        "settings": {
            "google_place_id": r.get("google_place_id") or "",
            "google_review_url": r.get("google_review_url") or "",
            "zillow_review_url": r.get("zillow_review_url") or "",
            **prefs,
        },
    }


@router.patch("/reviews/settings")
async def update_review_settings(
    payload: ReviewSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))).scalar_one_or_none()
    if not hub:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set up your Agent Hub first.")
    cfg = dict(hub.config or {})
    rev = dict(cfg.get("reviews") or {})
    if payload.google_place_id is not None:
        rev["google_place_id"] = payload.google_place_id.strip()
    if payload.google_review_url is not None:
        rev["google_review_url"] = payload.google_review_url.strip()
    if payload.zillow_review_url is not None:
        rev["zillow_review_url"] = payload.zillow_review_url.strip()
    if payload.show_on_hub is not None:
        rev["showOnHub"] = bool(payload.show_on_hub)
    if payload.show_on_idx is not None:
        rev["showOnIdx"] = bool(payload.show_on_idx)
    if payload.min_rating is not None:
        rev["minRating"] = 4 if payload.min_rating >= 4 else 1
    cfg["reviews"] = rev
    hub.config = cfg
    await db.commit()
    from app.services.review_display import review_prefs
    return {
        "google_place_id": rev.get("google_place_id") or "",
        "google_review_url": rev.get("google_review_url") or "",
        "zillow_review_url": rev.get("zillow_review_url") or "",
        **review_prefs(cfg),
    }


@router.post("/reviews/import-google")
async def import_google_reviews(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    """Pull the agent's existing Google Business reviews (up to 5 - Google's hard cap) into
    OpenHousePal as reviews, so a new account isn't showing an empty testimonials wall. Deduped
    on author + text so re-running it is safe. Needs the Google Place ID set in review settings."""
    from app.models.database import AgentReview
    from app.services import google_reviews_service

    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))).scalar_one_or_none()
    place_id = (((hub.config if hub else {}) or {}).get("reviews") or {}).get("google_place_id") if hub else None
    if not place_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add your Google Place ID in review settings first.")
    if not google_reviews_service.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google reviews import isn't configured on this server.")

    try:
        data = await google_reviews_service.fetch_reviews(place_id)
    except google_reviews_service.GoogleReviewsError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    # One Place Details call with reviews (Google's Enterprise + Atmosphere SKU) - the agent's
    # own click, so it goes on their AI usage like every other paid lookup they trigger.
    from app.services.ai.ai_rate_limit_service import record_external_api_cost
    from app.services.ai.ai_config_service import ai_config_service
    await record_external_api_cost(db, current_user.id, kind="places_details_reviews", config=await ai_config_service.get_config(db))

    existing = (await db.execute(
        select(AgentReview).where(AgentReview.owner_id == current_user.id, AgentReview.source == "google")
    )).scalars().all()
    seen = {(r.reviewer_name.strip().lower(), (r.body or "")[:120].strip().lower()) for r in existing}

    imported = 0
    for rv in data["reviews"]:
        key = (rv["author"].strip().lower(), rv["text"][:120].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        db.add(AgentReview(
            owner_id=current_user.id,
            reviewer_name=rv["author"],
            reviewer_email=None,
            rating=rv["rating"],
            body=rv["text"],
            source="google",
            status="published",
            created_at=rv["published_at"] or datetime.now(timezone.utc),
        ))
        imported += 1
    if imported:
        await db.commit()

    return {
        "imported": imported,
        "skipped": len(data["reviews"]) - imported,
        "available": len(data["reviews"]),
        "google_total": data.get("total"),
        "note": "Google's API only returns up to 5 reviews - that's their limit, not ours." if (data.get("total") or 0) > 5 else None,
    }


REVIEW_SOURCES = {"google", "zillow", "realtor", "homes", "facebook", "other"}


class ReviewPasteIn(BaseModel):
    text: str


class ReviewImportItem(BaseModel):
    name: str
    rating: int
    body: str
    date: Optional[str] = None   # YYYY-MM-DD - when the client wrote it


class ReviewImportIn(BaseModel):
    source: str
    reviews: list[ReviewImportItem]
    # The agent confirms these are real reviews from their own clients, copied from their own profile.
    confirm_real: bool = False


def _review_key(name: str, body: str) -> tuple[str, str]:
    return (re.sub(r"\s+", " ", name or "").strip().lower(), re.sub(r"\s+", " ", body or "").strip().lower()[:120])


@router.post("/reviews/parse")
async def parse_pasted_reviews(
    payload: ReviewPasteIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    """Read reviews out of text the agent copied from their Zillow / Google / Realtor.com page.
    Nothing is saved - the agent checks the list (and fills in any missing stars) first."""
    from app.models.database import AgentReview
    from app.services.ai import ai_copywrite_service
    result = await ai_copywrite_service.parse_reviews(db, current_user.id, current_user.plan_tier, text=payload.text)
    existing = (await db.execute(select(AgentReview.reviewer_name, AgentReview.body).where(AgentReview.owner_id == current_user.id))).all()
    seen = {_review_key(n, b) for n, b in existing}
    for r in result["reviews"]:
        r["already_here"] = _review_key(r["name"], r["body"]) in seen
    return result


@router.post("/reviews/import")
async def import_reviews(
    payload: ReviewImportIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    """Save reviews the agent brought over from another site (pasted + checked, or typed in one at
    a time). They count toward the Agent Hub rating and show a small "via Zillow" style label.
    Deduped on name + text against everything already here, so importing twice is safe."""
    from app.models.database import AgentReview
    source = (payload.source or "").strip().lower()
    if source not in REVIEW_SOURCES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pick where these reviews came from.")
    if not payload.confirm_real:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please confirm these are real reviews from your clients.")
    if not payload.reviews or len(payload.reviews) > 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add between 1 and 100 reviews at a time.")
    existing = (await db.execute(select(AgentReview.reviewer_name, AgentReview.body).where(AgentReview.owner_id == current_user.id))).all()
    seen = {_review_key(n, b) for n, b in existing}
    now = datetime.now(timezone.utc)
    imported = skipped = 0
    for r in payload.reviews:
        name, body = (r.name or "").strip()[:120], (r.body or "").strip()[:5000]
        if not name or len(body) < 3 or not (1 <= int(r.rating) <= 5):
            skipped += 1
            continue
        key = _review_key(name, body)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        when = now
        if r.date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.date.strip()):
            try:
                when = min(now, datetime.fromisoformat(r.date.strip() + "T12:00:00+00:00"))
            except ValueError:
                pass
        db.add(AgentReview(owner_id=current_user.id, reviewer_name=name, reviewer_email=None, rating=int(r.rating), body=body,
                           source=source, status="published", created_at=when))
        imported += 1
    if imported:
        await db.commit()
    return {"imported": imported, "skipped": skipped}


@router.patch("/reviews/{review_id}")
async def update_my_review(
    review_id: str, payload: ReviewStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    from app.models.database import AgentReview
    review = (await db.execute(
        select(AgentReview).where(AgentReview.id == review_id, AgentReview.owner_id == current_user.id)
    )).scalar_one_or_none()
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found.")
    if payload.status not in ("published", "hidden"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid status.")
    review.status = payload.status
    await db.commit()
    return {"id": review.id, "status": review.status}


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_review(
    review_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviews_access),
):
    from app.models.database import AgentReview
    review = (await db.execute(
        select(AgentReview).where(AgentReview.id == review_id, AgentReview.owner_id == current_user.id)
    )).scalar_one_or_none()
    if review:
        await db.delete(review)
        await db.commit()
