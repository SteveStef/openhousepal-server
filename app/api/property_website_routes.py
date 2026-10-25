import logging

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import PlanTier, User
from app.schemas.property_website import (
    PropertyWebsiteAnalyticsOut, PropertyWebsiteCreate, PropertyWebsiteDraftUpdate, PropertyWebsiteOut,
)
from app.services import property_website_service, spotlight_header_service, spotlight_town_photo_service
from app.utils.auth import require_plan_tier

# Listing Spotlight is IDX-only (matches the homepage pricing card and site_routes.py). Every
# route below manages an agent's own Spotlight sites, so all gate on the IDX tier.
require_spotlight_access = require_plan_tier(PlanTier.IDX)

router = APIRouter(prefix="/studio/property-sites", tags=["property-websites"])
logger = logging.getLogger(__name__)


async def _out(db: AsyncSession, site) -> PropertyWebsiteOut:
    out = PropertyWebsiteOut.model_validate(site)
    out.public_url = await property_website_service.public_url(db, site)
    return out


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Never pulls more than the limit into memory - one byte over is enough to say "too big"."""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"That file is too big - the limit is {max_bytes // (1024 * 1024)}MB.")
    return data


async def _load_owned(db: AsyncSession, site_id: str, owner_id: str):
    site = await property_website_service.get_owned(db, owner_id, site_id)
    if not site:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property website not found")
    return site


@router.get("", response_model=list[PropertyWebsiteOut])
async def list_property_websites(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    sites = await property_website_service.list_for_owner(db, current_user.id)
    outs = []
    for s in sites:
        out = await _out(db, s)
        # Today's MLS status/price on each card (Coming Soon -> Active -> Pending...), not the
        # snapshot frozen when the page was made - same overlay the public page uses.
        out.property_snapshot = await property_website_service.live_property_snapshot(db, s)
        outs.append(out)
    return outs


@router.post("", response_model=PropertyWebsiteOut)
async def create_property_website(
    payload: PropertyWebsiteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    try:
        site = await property_website_service.create_property_website(
            db, current_user, listing_key=payload.listing_key,
            manual_property=payload.manual_property, template_id=payload.template_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return await _out(db, site)


@router.get("/analytics", response_model=PropertyWebsiteAnalyticsOut)
async def get_property_website_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Visitor stats for every Spotlight site this agent owns - declared before /{site_id} so
    "analytics" isn't captured as a site id."""
    return await property_website_service.analytics_for_owner(db, current_user.id)


@router.get("/{site_id}", response_model=PropertyWebsiteOut)
async def get_property_website(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    site = await _load_owned(db, site_id, current_user.id)
    return await _out(db, site)


@router.patch("/{site_id}", response_model=PropertyWebsiteOut)
async def update_property_website_draft(
    site_id: str,
    payload: PropertyWebsiteDraftUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    site = await _load_owned(db, site_id, current_user.id)
    patch = payload.model_dump(exclude_unset=True)
    site = await property_website_service.update_draft(db, site, patch)
    return await _out(db, site)


@router.post("/{site_id}/publish", response_model=PropertyWebsiteOut)
async def publish_property_website(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    site = await _load_owned(db, site_id, current_user.id)
    site = await property_website_service.publish(db, site)
    return await _out(db, site)


@router.post("/{site_id}/publish-when-ready", response_model=PropertyWebsiteOut)
async def publish_property_website_when_ready(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Publish pressed while the editor's first-open setup is still writing (user, 10/24): the
    server publishes as soon as it's done - even if the agent leaves the page (it finishes the
    missing pieces itself). See spotlight_publish_service."""
    from app.services import spotlight_publish_service
    site = await _load_owned(db, site_id, current_user.id)
    site = await spotlight_publish_service.request_publish(db, site)
    return await _out(db, site)


@router.post("/{site_id}/unpublish", response_model=PropertyWebsiteOut)
async def unpublish_property_website(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    from app.services import spotlight_publish_service
    site = await _load_owned(db, site_id, current_user.id)
    site = await spotlight_publish_service.cancel(db, site)  # a waiting "publish when ready" too
    site = await property_website_service.unpublish(db, site)
    return await _out(db, site)


@router.delete("/{site_id}")
async def delete_property_website(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    site = await _load_owned(db, site_id, current_user.id)
    await property_website_service.delete(db, site)
    return {"success": True}


@router.post("/{site_id}/documents")
async def upload_property_website_document(
    site_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Just uploads + returns a URL - the caller still PATCHes it into draft_data.documents
    themselves (same two-step shape as every other Studio upload), so a file only actually
    attaches to the site once the agent hits Save, not the instant it finishes uploading."""
    site = await _load_owned(db, site_id, current_user.id)  # 404s before spending an S3 call on someone else's site
    if len([d for d in (site.draft_data or {}).get("documents") or [] if isinstance(d, dict) and d.get("url")]) >= property_website_service.MAX_DOCUMENTS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A page can have up to {property_website_service.MAX_DOCUMENTS} documents - remove one to add another.")
    data = await _read_capped(file, property_website_service.upload_limit_bytes("document", file.content_type or ""))
    try:
        url = await property_website_service.upload_document(db, current_user, data, file.content_type or "application/octet-stream")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return {"url": url}


@router.post("/{site_id}/floor-plans")
async def upload_property_website_floor_plan(
    site_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    await _load_owned(db, site_id, current_user.id)
    data = await _read_capped(file, property_website_service.upload_limit_bytes("floor_plan", file.content_type or ""))
    try:
        url = await property_website_service.upload_floor_plan(db, current_user, data, file.content_type or "image/jpeg")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return {"url": url}


# ---- "Living photo" header: one AI clip of the main photo, looped (see spotlight_header_service) ----

class HeaderVideoCreate(BaseModel):
    photo_url: str = Field(..., min_length=8)
    # "What was wrong last time?" from a remake - added to the prompt as a plain instruction.
    note: Optional[str] = Field(None, max_length=110)
    style: Literal["glide", "dusk"] = "dusk"  # "glide" still accepted from older pages - both make the same clip now


class HeaderVideoLoop(BaseModel):
    asset_id: str = Field(..., min_length=8)


def _header(site) -> dict:
    return dict((site.draft_data or {}).get("header") or {})


@router.get("/{site_id}/header-video/estimate")
async def header_video_estimate(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """What each header style would use of the agent's monthly AI allowance - shown before they
    say yes. Plus where they stand this month, so the flow can warn before a doomed attempt."""
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_rate_limit_service import monthly_ai_budget_snapshot
    await _load_owned(db, site_id, current_user.id)
    config = await ai_config_service.get_config(db)
    out = spotlight_header_service.estimate(config, current_user.plan_tier)
    snap = await monthly_ai_budget_snapshot(db, current_user.id, config, current_user.plan_tier)
    out["used_percent"] = snap.get("percent")
    # The Room by room photo sorter's price for this listing, same "% of monthly AI" language.
    from app.services.ai.ai_copywrite_service import estimate_photo_sort_usd
    site = await _load_owned(db, site_id, current_user.id)
    rooms_usd = estimate_photo_sort_usd(config, len((site.property_snapshot or {}).get("photos") or []))
    out["rooms"] = {"usd": round(rooms_usd, 4), "percent": round(rooms_usd / float(out["limit_usd"]) * 100, 2) if out.get("limit_usd") else None}
    return out


@router.post("/{site_id}/header-video")
async def create_header_video(
    site_id: str,
    payload: HeaderVideoCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Start the AI clip. It's a normal AI Media video (lands in the agent's library, same
    allowance, same budget checks); the draft remembers it so the flow can pick it back up."""
    from app.schemas.forms import LinkedProperty
    from app.schemas.studio_media import StudioMediaGenerateRequest, StudioMediaOptions, StudioVideoVariantOptions
    from app.services import studio_media_service
    if not spotlight_header_service.LIVING_PHOTO_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The living photo header is coming soon.")
    site = await _load_owned(db, site_id, current_user.id)
    snap = site.property_snapshot or {}
    linked = LinkedProperty(
        listing_key=site.listing_key, address=snap.get("address") or "", city=snap.get("city"),
        state=snap.get("state"), zipcode=snap.get("zipcode"), price=snap.get("price"), beds=snap.get("beds"),
        baths=snap.get("baths"), sqft=snap.get("sqft"), acres=snap.get("lot_size_acres"),
        image_url=payload.photo_url, photos=[payload.photo_url], status=snap.get("status"),
        year_built=snap.get("year_built"),
    )
    request = StudioMediaGenerateRequest(
        action="animate", source_photo_url=payload.photo_url, photos=[payload.photo_url],
        source_kind="listing", linked_property=linked,
        # Exactly a normal Media Lab video (same provider, model, prompt and 2K finish) - only
        # the camera move is fixed to the slowest push-in and the suffix adds the dusk light.
        options=StudioMediaOptions(
            video_style="standard",
            video_variant=StudioVideoVariantOptions(
                label="spotlight_header", motion="slow_push_in",
                prompt_suffix=spotlight_header_service.header_suffix(payload.note),
            ),
        ),
    )
    asset = await studio_media_service.create_asset(db, current_user, request)
    street = (snap.get("address") or "").split(",")[0].title()
    opts = dict(asset.options or {})
    opts["custom_name"] = f"Spotlight header · {street}" if street else "Spotlight header"
    asset.options = opts
    db.add(asset)
    await db.commit()
    header = _header(site)
    header["video"] = {"asset_id": asset.id, "style": "dusk", "photo": payload.photo_url, "url": None, "poster": None}
    await property_website_service.update_draft(db, site, {"header": header})
    return {"asset_id": asset.id, "status": asset.status}


@router.post("/{site_id}/header-video/loop")
async def build_header_loop(
    site_id: str,
    payload: HeaderVideoLoop,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Finished clip -> seamless loop + poster (no AI cost). Cached on the asset, so reusing the
    same clip on another site or template never rebuilds it."""
    from app.services import studio_media_service
    site = await _load_owned(db, site_id, current_user.id)
    asset = await studio_media_service.get_owned(db, current_user.id, payload.asset_id)
    # The 1080p render is all a header needs - if an (older) job is still on its optional AI
    # upscale pass, build the loop from the base render instead of waiting for it.
    source = asset.result_url if asset.status == "succeeded" else (asset.options or {}).get("upscale_base_url")
    if asset.kind != "video" or asset.status == "failed" or not source:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That video isn't finished yet.")
    cached = (asset.options or {}).get("header_loop") or {}
    if cached.get("url"):
        loop = {"url": cached["url"], "poster": cached.get("poster")}
    else:
        try:
            loop = await spotlight_header_service.make_loop(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("header loop failed: %s", str(exc)[:300])
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Couldn't prepare the video for the page. Try again in a moment.")
        opts = dict(asset.options or {})
        opts["header_loop"] = loop
        asset.options = opts
        db.add(asset)
        await db.commit()
    header = _header(site)
    video = dict(header.get("video") or {})
    video.update({"asset_id": asset.id, "url": loop["url"], "poster": loop.get("poster")})
    video.setdefault("photo", asset.source_photo_url)
    header["video"] = video
    await property_website_service.update_draft(db, site, {"header": header})
    return {"url": loop["url"], "poster": loop.get("poster"), "cost_usd": float(getattr(asset, "cost_usd", 0) or 0)}


@router.get("/{site_id}/open-houses")
async def site_open_houses(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """The open houses this page shows right now - the same live list the public page renders:
    Open House Manager events for this home, the MLS open house, and dates the agent added here.
    Past ones drop off on their own; a newly scheduled one appears on its own."""
    site = await _load_owned(db, site_id, current_user.id)
    return {"open_houses": await property_website_service.upcoming_open_houses(db, site, site.draft_data or {})}


@router.get("/{site_id}/town-photos")
async def town_photos(
    site_id: str,
    name: Optional[str] = Query(None, max_length=80),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Credited photos of the town itself (Wikimedia Commons) for "About the town"."""
    site = await _load_owned(db, site_id, current_user.id)
    snap = site.property_snapshot or {}
    return {"photos": await spotlight_town_photo_service.town_photos(name or snap.get("city") or "", snap.get("state"))}


@router.get("/{site_id}/preview")
async def preview_property_website(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """The draft rendered exactly like the live page, for "Preview my page" before publishing.
    site_id comes back as 'preview' so the preview's lead form / view beacon never fire, and
    Explore Nearby stays off (no Google calls from a preview)."""
    from app.api.public_property_site_routes import render_payload
    site = await _load_owned(db, site_id, current_user.id)
    draft = dict(site.draft_data or {})
    payload = await render_payload(db, site, current_user, draft)
    payload["site_id"] = "preview"
    payload["template_id"] = draft.get("template_id") or site.template_id
    # The page's real address, so the preview's QR codes / share links point where the live page will.
    payload["public_url"] = await property_website_service.public_url(db, site)
    content = dict(payload.get("content") or {})
    content["section_visibility"] = {**(content.get("section_visibility") or {}), "explore_nearby": False}
    payload["content"] = content
    return payload



# ---- Room by room: AI sorts the listing photos into rooms (one cheap vision call) ----

_ROOM_ORDER = ["exterior_arrival", "great_room", "kitchen", "dining_room", "primary_suite", "additional_bedrooms",
               "bathrooms", "office_den", "basement_rec", "porch_patio", "pool_spa", "grounds_backyard", "garage_automotive"]


def _listing_text(site) -> str:
    """What the photo AI reads alongside the photos, so every section it picks is one the listing
    itself describes: the full MLS description (else the edited story) + the details."""
    snap = site.property_snapshot or {}
    draft = site.draft_data or {}
    parts = [str(snap.get("description") or draft.get("description_override") or "").strip()]
    facts = [f"{snap.get(k):g} {label}" for k, label in (("beds", "beds"), ("baths", "baths"), ("sqft", "sq ft"),
             ("lot_size_acres", "acres")) if isinstance(snap.get(k), (int, float))]
    if snap.get("year_built"):
        facts.append(f"built {snap['year_built']}")
    if facts:
        parts.append("Details: " + ", ".join(facts))
    features = snap.get("features") if isinstance(snap.get("features"), dict) else {}
    for group, items in features.items():
        if isinstance(items, list) and items:
            parts.append(f"{str(group).replace('_', ' ').title()}: " + ", ".join(str(i) for i in items[:25]))
    return "\n".join(p for p in parts if p)


async def _photo_ai(db: AsyncSession, site, user: User, fresh: bool = False) -> dict:
    """One cheap vision pass over the listing photos, cached on the draft: which room each photo
    shows, the 3-4 spaces that sell this home most, and the best photos for beside the story.
    Room by room and the story photo both read from it, so the agent pays for it once."""
    import hashlib
    from app.services.ai import ai_copywrite_service
    photos = list((site.property_snapshot or {}).get("photos") or [])
    text = _listing_text(site)
    key = "v2-" + hashlib.sha1(("|".join(photos) + text).encode()).hexdigest()[:16]
    cached = (site.draft_data or {}).get("photo_ai") or {}
    if not fresh and cached.get("key") == key and cached.get("rooms"):
        return {**cached, "cost_usd": 0.0, "cached": True}
    result = await ai_copywrite_service.sort_listing_photos(db, user.id, user.plan_tier, photos, text)
    data = {"key": key, "rooms": result.get("rooms") or {}, "highlights": result.get("highlights") or [],
            "sections": result.get("sections") or [], "questions": result.get("questions") or [],
            "story_photos": result.get("story_photos") or []}
    await property_website_service.update_draft(db, site, {"photo_ai": data})
    return {**data, "cost_usd": result.get("cost_usd", 0.0), "cached": False}


@router.post("/{site_id}/rooms/auto")
async def auto_rooms(
    site_id: str,
    fresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """Sections for only the spaces that sell this home - the AI's 3-6 picks, each one backed by the
    listing text (best first, up to 6 photos each) - plus anything it wants the agent to confirm
    rather than guess. Nothing is saved - the editor shows them to the agent first."""
    site = await _load_owned(db, site_id, current_user.id)
    photos = list((site.property_snapshot or {}).get("photos") or [])
    ai = await _photo_ai(db, site, current_user, fresh)
    rooms = ai.get("rooms") or {}
    groups: dict = {}
    for url in photos:  # keep the listing's own photo order inside each room
        room = rooms.get(url)
        if room and room != "other":
            groups.setdefault(room, []).append(url)
    chapters = [{"type": sec["type"], "label": sec["title"], "photos": sec["photos"][:6]}
                for sec in (ai.get("sections") or [])[:6] if sec.get("photos")]
    if not chapters:  # older cached pass (no sections) - the AI's room highlights, then the usual order
        picks = [t for t in (ai.get("highlights") or []) if groups.get(t)]
        if len(picks) < 3:
            picks += [t for t in _ROOM_ORDER if t not in picks and len(groups.get(t, [])) >= 2]
        chapters = [{"type": t, "label": None, "photos": groups[t][:6]} for t in picks[:4]]
    return {"chapters": chapters, "questions": ai.get("questions") or [], "rooms": rooms, "cost_usd": ai.get("cost_usd", 0.0)}


@router.post("/{site_id}/story-photo/auto")
async def auto_story_photo(
    site_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_spotlight_access),
):
    """The AI's pick for the photo beside the story (never the header photo). Saved straight to
    the draft unless the agent already chose one; the editor just shows what it picked."""
    site = await _load_owned(db, site_id, current_user.id)
    draft = site.draft_data or {}
    photos = list((site.property_snapshot or {}).get("photos") or [])
    if not photos:
        return {"story_photo": None, "cost_usd": 0.0}
    ai = await _photo_ai(db, site, current_user)
    hero = draft.get("hero_image") or photos[0]
    pick = next((u for u in ai.get("story_photos") or [] if u != hero), None)
    if not pick:
        rooms = ai.get("rooms") or {}
        pick = next((u for u in photos if u != hero and rooms.get(u) in ("exterior_arrival", "great_room", "grounds_backyard")), None)
    if pick and not draft.get("story_photo"):
        await property_website_service.update_draft(db, site, {"story_photo": pick})
    return {"story_photo": pick, "cost_usd": ai.get("cost_usd", 0.0)}

