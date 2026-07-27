import asyncio
import html
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import AgentHub, Property, User
from app.services.bright_mls_service import bright_mls_service
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/agent-hub", tags=["agent-hub"])
logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RECENT_SALES_CACHE_TTL_SECONDS = 10 * 60
recent_sales_cache: dict[str, tuple[float, list[dict]]] = {}


class AgentHubSlugUpdate(BaseModel):
    slug: str


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


@router.get("/me")
async def get_my_agent_hub(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == current_user.id))
    hub = result.scalar_one_or_none()
    if not hub:
        return None

    return {"id": hub.id, "slug": hub.slug, "status": hub.status}


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


@router.get("/youtube/videos")
async def get_youtube_channel_videos(
    channel_url: str = Query(..., min_length=8, max_length=2048),
    current_user: User = Depends(get_current_active_user),
):
    parsed = urlparse(channel_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a valid YouTube URL.")

    channel_id_match = re.search(r"/channel/([^/?#]+)", channel_url)
    channel_id = channel_id_match.group(1) if channel_id_match else ""

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            if not channel_id:
                page = await client.get(channel_url, headers={"User-Agent": "OpenHousePalBot/1.0"})
                page.raise_for_status()
                channel_id_match = (
                    re.search(r'"channelId":"(UC[^"]+)"', page.text)
                    or re.search(r'<meta itemprop="channelId" content="([^"]+)"', page.text)
                    or re.search(r"https://www\.youtube\.com/channel/(UC[^\"/?&]+)", page.text)
                )
                channel_id = channel_id_match.group(1) if channel_id_match else ""

            if not channel_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not find this YouTube channel.")

            feed = await client.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
            feed.raise_for_status()

            videos = []
            for entry in re.findall(r"<entry>(.*?)</entry>", feed.text, flags=re.DOTALL):
                video_id = re.search(r"<yt:videoId>(.*?)</yt:videoId>", entry)
                title = re.search(r"<title>(.*?)</title>", entry, flags=re.DOTALL)
                published = re.search(r"<published>(.*?)</published>", entry)
                if video_id:
                    video_url = f"https://www.youtube.com/watch?v={video_id.group(1)}"
                    try:
                        video_page = await client.get(video_url, headers={"User-Agent": "OpenHousePalBot/1.0"})
                        if re.search(r'<link rel="canonical" href="https://www\.youtube\.com/shorts/', video_page.text):
                            continue
                    except httpx.HTTPError:
                        continue
                    clean_title = html.unescape(re.sub(r"\s+", " ", title.group(1)).strip()) if title else "YouTube Video"
                    videos.append({
                        "id": f"youtube-{video_id.group(1)}",
                        "title": clean_title,
                        "description": published.group(1)[:10] if published else "YouTube",
                        "link": video_url,
                        "image": f"https://img.youtube.com/vi/{video_id.group(1)}/hqdefault.jpg",
                    })
                    if len(videos) >= 6:
                        break
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to read this YouTube channel.")

    return {"channel_url": channel_url, "videos": videos}


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
