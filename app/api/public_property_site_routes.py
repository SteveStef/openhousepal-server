import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import email_signature_service
from app.api.public_nearby_shared import explore_nearby_response, explore_place_details_response, visitor_device_id
from app.database import get_db
from app.models.database import AgentHub, PropertyWebsite, StudioBrandKit, StudioMediaAsset, User
from app.schemas.property_website import PropertyWebsiteAlertCreate, PropertyWebsiteInquiryCreate, PropertyWebsiteTourRequestCreate
from app.services import property_website_service
from app.services.nearby_amenities_service import is_explore_nearby_available
from app.services.ai.ai_config_service import ai_config_service
from app.services.website import content_adapters, domain_resolution_service
from app.utils.request_ip import get_client_ip

router = APIRouter(prefix="/public/property-sites", tags=["property-websites-public"])
logger = logging.getLogger(__name__)


async def _published_owner(db: AsyncSession, site_id: str):
    result = await db.execute(select(PropertyWebsite).where(PropertyWebsite.id == site_id, PropertyWebsite.status == "PUBLISHED"))
    site = result.scalar_one_or_none()
    if not site:
        return None
    user = (await db.execute(select(User).where(User.id == site.owner_id))).scalar_one_or_none()
    if not _spotlight_owner_live(user):
        return None
    if await _listing_not_showable(db, site):
        return None
    return site, user


async def _listing_not_showable(db: AsyncSession, site: PropertyWebsite) -> bool:
    """Bright IDX rules: a withdrawn / expired / canceled / off-market listing (or one whose seller
    opted out of internet display) can't stay on a public page - its Spotlight closes until the
    home is back on the market. Sold homes stay up. Manual (non-MLS) Spotlights are unaffected."""
    if not site.listing_key:
        return False
    from app.models.database import Property
    from app.services.website.content_adapters import _IDX_NOT_SHOWN_STATUSES
    row = (await db.execute(
        select(Property.home_status, Property.raw_mls_data["InternetEntireListingDisplayYN"].astext)
        .where(Property.listing_key == str(site.listing_key))
    )).first()
    if not row:
        return False
    status_, entire = row
    return status_ in _IDX_NOT_SHOWN_STATUSES or entire == "false"


def _spotlight_owner_live(user) -> bool:
    """Spotlight sites are IDX-only (property_website_routes gates creation the same way); a
    downgrade or ended subscription takes the published sites down - kept, not deleted, so they
    come right back on upgrade/resubscribe."""
    from app.utils.auth import owner_has_access
    return owner_has_access(user, {"IDX"})


@router.get("/resolve")
async def resolve_property_site(
    host: str = Query(..., min_length=1),
    path: str = Query("", description="Requested path - only used to disambiguate a custom-domain /listings/<slug> hit"),
    db: AsyncSession = Depends(get_db),
):
    resolved = await domain_resolution_service.resolve_property_site(db, host, path)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published property website for that address.")

    site = (await db.execute(select(PropertyWebsite).where(PropertyWebsite.id == resolved.property_website_id))).scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published property website for that address.")
    user = (await db.execute(select(User).where(User.id == site.owner_id))).scalar_one_or_none()
    if not _spotlight_owner_live(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published property website for that address.")
    if await _listing_not_showable(db, site):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This home is no longer on the market.")

    payload = await render_payload(db, site, user, site.published_snapshot or {}, resolved.canonical_hostname)
    # The tab icon the agent uploaded for their own website (IDX -> Google tab) - their Spotlights
    # use it too, so everything on their own domains carries the same icon.
    try:
        from app.models.database import Site
        own = (await db.execute(select(Site).where(Site.owner_id == site.owner_id))).scalars().first()
        seo = ((own.published_snapshot or {}).get("seo") or {}) if own is not None and own.is_enabled is not False else {}
        if isinstance(payload, dict) and seo.get("favicon_url"):
            payload["agent_icon"] = seo["favicon_url"]
    except Exception:
        pass
    return payload


_BRIGHT_PHOTO_ID = re.compile(r"/bright/images/[\d/]+/(\d+)_\d+_\d+_")


def _sharpest_photos(content: dict, live_photos: list) -> dict:
    """Photos the agent picked (header, story photo, room-by-room) are saved as the exact Bright
    link of the day. Bright serves every photo at several sizes under the same photo id, so swap
    any picked link for the listing's current copy of that photo - the 2048px HD one once the
    sync stores HD links - so a picked photo is never sharper or blurrier than the gallery."""
    best = {}
    for url in live_photos:
        m = _BRIGHT_PHOTO_ID.search(url or "")
        if m:
            best[m.group(1)] = url

    def up(url):
        if not isinstance(url, str):
            return url
        m = _BRIGHT_PHOTO_ID.search(url)
        return best.get(m.group(1), url) if m else url

    if not best:
        return content
    out = dict(content)
    for key in ("hero_image", "story_photo"):
        if out.get(key):
            out[key] = up(out[key])
    if isinstance(out.get("chapters"), list):
        out["chapters"] = [{**ch, "photos": [up(u) for u in (ch.get("photos") or [])]} if isinstance(ch, dict) else ch
                           for ch in out["chapters"]]
    return out


async def render_payload(db: AsyncSession, site: PropertyWebsite, user, draft: dict, canonical_hostname=None) -> dict:
    """Everything a Spotlight template renders from - shared by the live page (published
    snapshot) and the editor's "Preview my page" (the unpublished draft)."""
    video = None
    if draft.get("video_asset_id"):
        asset = (await db.execute(
            select(StudioMediaAsset).where(StudioMediaAsset.id == draft["video_asset_id"], StudioMediaAsset.owner_id == user.id)
        )).scalar_one_or_none()
        if asset and asset.status == "succeeded" and asset.result_url:
            video = {"url": asset.result_url}

    brand_kit = (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id))).scalar_one_or_none()
    agent_profile = await content_adapters.get_agent_profile(db, user)

    # The branding on a Spotlight site is always the OpenHousePal account that built and published
    # it - not the MLS snapshot's list_agent_full_name. The Search Any Address tab (mirroring
    # Studio Media) lets an agent build a site from ANY active listing, not just their own, and in
    # that case the raw MLS listing agent could be a completely different person (a colleague, a
    # competitor, a stale record) - this site is this account's own marketing tool regardless of
    # who the MLS record names, so it always shows the real person who made the page.
    default_agent = {
        "name": agent_profile["name"],
        "title": agent_profile["title"],
        "phone": agent_profile["phone"],
        "text_phone": agent_profile.get("text_phone") or agent_profile["phone"],
        "email": agent_profile["email"],
        "brokerage": agent_profile["brokerage"],
        "license_number": agent_profile["license_number"],
        "photo_url": agent_profile["photo_url"],
        "bio": agent_profile.get("bio") or "",
        "social_links": agent_profile.get("social_links") or [],
    }
    # "Visit my Agent Hub" - whenever the public hub page would actually load (same rule as
    # agent_hub_routes._resolve_public_hub: a name + a way to reach them; the status column is
    # not what gates the public page).
    from app.api.agent_hub_routes import _hub_has_minimum_content
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    if hub is not None and hub.slug and _hub_has_minimum_content(hub.config or {}):
        from app.services.website.site_service import owner_links
        default_agent["hub_url"] = (await owner_links(db, user, hub.slug)).hub()
    snapshot = site.property_snapshot or {}
    agents = property_website_service.merge_agents(default_agent, draft.get("agents") or [], snapshot.get("list_agent_full_name"))

    # Live listing facts + live open house schedule - see live_property_snapshot /
    # upcoming_open_houses for why neither is read from the frozen snapshot/draft alone.
    content = {**draft, "open_houses": await property_website_service.upcoming_open_houses(db, site, draft)}
    # The Open Houses section follows the schedule: sites made before any open house was booked
    # saved it as off, which kept every later open house hidden. Unless the agent switched it off
    # themselves (Sections step marks that), it shows whenever there's an upcoming one.
    vis = dict(content.get("section_visibility") or {})
    if content["open_houses"] and vis.get("open_houses") is False and not vis.get("open_houses_by_agent"):
        vis["open_houses"] = True
        content["section_visibility"] = vis
    # Living photo headers are paused ("coming soon") - pages that had one show the still photo.
    from app.services import spotlight_header_service
    header = content.get("header") if isinstance(content.get("header"), dict) else None
    if header and header.get("mode") == "video" and not spotlight_header_service.LIVING_PHOTO_ENABLED:
        content["header"] = {**header, "mode": "photo"}

    live = await property_website_service.live_property_snapshot(db, site)
    content = _sharpest_photos(content, live.get("photos") or [])

    return {
        "site_id": site.id,
        "slug": site.slug,
        "template_id": site.template_id,
        "property": live,
        "content": content,
        "video": video,
        "agents": agents,
        "brand_kit": {
            "primary_color": brand_kit.primary_color if brand_kit else "#111827",
            "secondary_color": brand_kit.secondary_color if brand_kit else "#C9A24D",
            "logo_url": (brand_kit.personal_logo_url or brand_kit.brokerage_logo_url) if brand_kit else None,
        },
        "canonical_hostname": canonical_hostname,
        # The agent's Email Signature card closes the page when their plan has it and one is saved.
        "signature_hub_slug": hub.slug if hub is not None and hub.slug and await email_signature_service.has_signature_card(db, user) else None,
    }


@router.post("/{site_id}/view")
async def record_property_site_view(
    site_id: str,
    request: Request,
    response: Response,
    owner_ref: bool = Query(False, description="Set by the beacon when the page was opened from Studio - the owner's own view, never counted"),
    db: AsyncSession = Depends(get_db),
):
    """Fire-and-forget visit beacon from the public Spotlight template. Always issues/refreshes
    the visitor cookie so the browser keeps a stable id; only records a row for a real outside
    visit to a published site (see property_website_service.record_view)."""
    device_id = visitor_device_id(request, response)
    recorded = False
    if not owner_ref:
        recorded = await property_website_service.record_view(db, site_id, device_id)
    return {"ok": True, "recorded": recorded}


@router.post("/{site_id}/inquiry")
async def submit_property_site_inquiry(site_id: str, payload: PropertyWebsiteInquiryCreate, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)  # bots can't flood the agent's leads/inbox
    owner = await _published_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found.")
    site, _user = owner
    await property_website_service.submit_inquiry(
        db, site, name=f"{payload.first_name} {payload.last_name}".strip(),
        email=payload.email, phone=payload.phone, message=payload.message, came_from=payload.came_from,
    )
    return {"success": True}


@router.post("/{site_id}/alerts")
async def subscribe_property_site_alerts(site_id: str, payload: PropertyWebsiteAlertCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """A guest asks to hear about price changes and new open houses for this home."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)  # bots can't flood the agent's leads/inbox
    owner = await _published_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found.")
    email = payload.email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Please enter a valid email.")
    from app.services import spotlight_alerts_service
    site, _user = owner
    await spotlight_alerts_service.subscribe(db, site, name=payload.name.strip(), email=email, came_from=payload.came_from)
    return {"success": True}


@router.get("/alerts/unsubscribe")
async def unsubscribe_property_site_alerts(token: str = Query(..., min_length=8, max_length=80), db: AsyncSession = Depends(get_db)):
    from app.services import spotlight_alerts_service
    address = await spotlight_alerts_service.unsubscribe(db, token)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That link has expired.")
    return {"success": True, "address": address}


@router.get("/{site_id}/agent-busy")
async def property_site_agent_busy(site_id: str, days: int = 14, db: AsyncSession = Depends(get_db)):
    """The agent's busy blocks (start/end only) so the client's time picker only offers times the
    agent is actually free (user, 10/18). [] when no calendar is connected."""
    from app.services import calendar_sync_service
    owner = await _published_owner(db, site_id)
    if not owner:
        return {"busy": []}
    site, _user = owner
    return {"busy": await calendar_sync_service.public_busy(db, site.owner_id, days)}


@router.post("/{site_id}/tour-request")
async def submit_property_site_tour_request(site_id: str, payload: PropertyWebsiteTourRequestCreate, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)  # bots can't flood the agent's leads/inbox
    owner = await _published_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found.")
    site, _user = owner
    await property_website_service.submit_tour_request(
        db, site, name=f"{payload.first_name} {payload.last_name}".strip(),
        email=payload.email, phone=payload.phone, tour_type=payload.tour_type,
        requested_date=payload.requested_date, preferred_time=payload.preferred_time, notes=payload.notes,
        came_from=payload.came_from,
    )
    return {"success": True}


def _site_origin(site: PropertyWebsite) -> tuple[float, float] | None:
    """Reads straight from property_snapshot (not a Property row lookup) so this works for a
    manual/non-MLS listing (NO_MLS tier) exactly the same as an MLS-linked one - the snapshot
    already carries real lat/lng for both, same fields resolve_property_site already returns."""
    snapshot = site.property_snapshot or {}
    lat, lng = snapshot.get("latitude"), snapshot.get("longitude")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


@router.get("/{site_id}/nearby/availability")
async def get_property_site_nearby_availability(site_id: str, db: AsyncSession = Depends(get_db)):
    """Read-only, no Google call - lets the frontend decide whether to render Explore Nearby at
    all before a visitor ever clicks a category. Platform-wide budget check, same as the
    website-builder public site's identical endpoint (public_site_routes.py)."""
    owner = await _published_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found.")
    config = await ai_config_service.get_config(db)
    return {"available": await is_explore_nearby_available(db, config)}


@router.get("/{site_id}/nearby")
async def get_property_site_nearby(
    site_id: str,
    request: Request,
    response: Response,
    category: str = Query(..., max_length=40),
    db: AsyncSession = Depends(get_db),
):
    owner = await _published_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found.")
    site, user = owner
    origin = _site_origin(site)
    if not origin:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This listing does not have a usable map location.")
    device_id = visitor_device_id(request, response)
    return await explore_nearby_response(db, origin[0], origin[1], category, get_client_ip(request), device_id, user.id)


@router.get("/{site_id}/places/{place_id}/details")
async def get_property_site_nearby_place_details(
    site_id: str,
    place_id: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    owner = await _published_owner(db, site_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found.")
    _site, user = owner
    device_id = visitor_device_id(request, response)
    return await explore_place_details_response(db, place_id, get_client_ip(request), device_id, user.id)
