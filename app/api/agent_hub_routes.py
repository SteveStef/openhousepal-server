import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict

from app.database import get_db
from app.models.database import AgentHub, AgentHubEvent, FormSubmission, Property, User
from app.services.bright_mls_service import bright_mls_service
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/agent-hub", tags=["agent-hub"])
logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RECENT_SALES_CACHE_TTL_SECONDS = 10 * 60
recent_sales_cache: dict[str, tuple[float, list[dict]]] = {}

PHOTO_UPLOAD_DIR = os.path.join("uploads", "agent-photos")
PHOTO_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
PHOTO_MAX_BYTES = 5 * 1024 * 1024


class AgentHubSlugUpdate(BaseModel):
    slug: str


class AgentHubProfileUpdate(BaseModel):
    title: Optional[str] = None
    market: Optional[str] = None


class AgentHubConfigUpdate(BaseModel):
    config: Dict[str, Any]


# The config blob holds the whole page-builder payload (profile text, contact info, social
# links, sales/video/tour/blog carousel data, layout/visibility flags). 300KB is generous for
# that - large enough for dozens of carousel items with images/links, small enough to block
# someone trying to stuff arbitrary data into an unbounded JSONB column.
AGENT_HUB_CONFIG_MAX_BYTES = 300_000


class AgentHubTrackEvent(BaseModel):
    event_type: str  # 'visit' | 'qr_scan'


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
    return clean_sale_status(sale.get("status")) in {"sold", "active", "pending", "under contract"} and (sale.get("price") or 0) >= 100000


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
        try:
            data = await bright_mls_service._make_request(
                "BrightProperties",
                {
                    "$filter": filter_expr,
                    "$top": limit,
                    "$select": select_fields,
                    "$orderby": "ModificationTimestamp desc",
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
    current_user: User = Depends(get_current_active_user),
):
    normalized = validate_slug(slug)
    result = await db.execute(select(AgentHub).where(AgentHub.slug == normalized))
    hub = result.scalar_one_or_none()
    available = hub is None or hub.owner_id == current_user.id

    return {
        "slug": normalized,
        "available": available,
        "owned_by_current_user": bool(hub and hub.owner_id == current_user.id),
    }


@router.get("/public/{slug}")
async def get_public_agent_hub(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    normalized = normalize_slug(slug)
    result = await db.execute(select(AgentHub).where(AgentHub.slug == normalized))
    hub = result.scalar_one_or_none()
    if not hub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This Agent Hub link isn't active.")

    return {"slug": hub.slug, "config": hub.config or {}}


@router.post("/public/{slug}/track", status_code=status.HTTP_204_NO_CONTENT)
async def track_agent_hub_event(
    slug: str,
    payload: AgentHubTrackEvent,
    db: AsyncSession = Depends(get_db),
):
    if payload.event_type not in {"visit", "qr_scan"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid event type.")

    normalized = normalize_slug(slug)
    result = await db.execute(select(AgentHub).where(AgentHub.slug == normalized))
    hub = result.scalar_one_or_none()
    if not hub:
        return None

    db.add(AgentHubEvent(owner_id=hub.owner_id, event_type=payload.event_type))
    await db.commit()
    return None


@router.get("/me/analytics")
async def get_my_agent_hub_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
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


@router.get("/me")
async def get_my_agent_hub(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))
    hub = result.scalar_one_or_none()
    if not hub:
        return None

    return {"id": hub.id, "slug": hub.slug, "status": hub.status, "config": hub.config or {}}


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

    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))
    hub = result.scalar_one_or_none()
    if not hub:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set your Agent Hub URL before saving your page.",
        )

    hub.config = payload.config
    await db.commit()
    return {"config": hub.config}


@router.get("/me/recent-sales")
async def get_my_recent_sales(
    limit: int = Query(24, ge=1, le=48),
    re_license_number: str | None = Query(None, max_length=80),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
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
    for lookup_identifier_type, lookup_identifier in identifiers:
        candidate_sales = await fetch_bright_agent_sales(lookup_identifier, lookup_identifier_type, limit)
        if candidate_sales:
            bright_sales.extend(candidate_sales)

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

    properties = await fetch_properties(filters)

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
    current_user: User = Depends(get_current_active_user),
):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a valid http or https URL.")

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "OpenHousePalBot/1.0"})
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
    current_user: User = Depends(get_current_active_user),
):
    parsed = urlparse(channel_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
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
    current_user: User = Depends(get_current_active_user),
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
    current_user: User = Depends(get_current_active_user),
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
    if payload.title is not None:
        current_user.professional_title = payload.title.strip() or None
    if payload.market is not None:
        current_user.market = payload.market.strip() or None
    await db.commit()
    return {"title": current_user.professional_title, "market": current_user.market}


@router.post("/me/photo")
async def upload_my_photo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    extension = PHOTO_CONTENT_TYPES.get(file.content_type)
    if not extension:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please upload a JPG, PNG, or WEBP image.",
        )

    contents = await file.read()
    if len(contents) > PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo must be 5MB or smaller.",
        )

    os.makedirs(PHOTO_UPLOAD_DIR, exist_ok=True)

    old_photo_url = current_user.photo_url
    filename = f"{uuid.uuid4()}{extension}"
    file_path = os.path.join(PHOTO_UPLOAD_DIR, filename)
    with open(file_path, "wb") as out_file:
        out_file.write(contents)

    photo_url = f"/uploads/agent-photos/{filename}"
    current_user.photo_url = photo_url
    await db.commit()

    if old_photo_url and old_photo_url.startswith("/uploads/agent-photos/"):
        old_path = os.path.join("uploads", "agent-photos", os.path.basename(old_photo_url))
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError as e:
                logger.warning("removing old agent photo failed", extra={"error": str(e)})

    return {"photo_url": photo_url}


@router.delete("/me/photo")
async def delete_my_photo(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    old_photo_url = current_user.photo_url
    current_user.photo_url = None
    await db.commit()

    if old_photo_url and old_photo_url.startswith("/uploads/agent-photos/"):
        old_path = os.path.join("uploads", "agent-photos", os.path.basename(old_photo_url))
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError as e:
                logger.warning("removing old agent photo failed", extra={"error": str(e)})

    return {"photo_url": None}
