"""Single Property Websites - a standalone cinematic marketing site generated from one listing.

Mirrors app.services.website.site_service's draft/publish shape (see publish_property_website),
but one owner can have many of these (unlike the one-per-owner Site), so slug generation has to
stay globally unique against BOTH tables - see _unique_property_slug.
"""
import os
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    Notification, OpenHouseEvent, Property, PropertyWebsite, PropertyWebsiteView, Site, SiteDomain,
    StudioBrandKit, StudioMediaAsset, User,
)
from app.services import leads_service, s3_service, storage_ledger_service
from app.services.ai.ai_config_service import ai_config_service
from app.services.mls_open_houses import upcoming_mls_open_houses
from app.services.website import domain_resolution_service
from app.services.website.site_service import RESERVED_SLUGS, SLUG_PATTERN, _slugify

TEMPLATE_IDS = {"cinematic_luxury", "editorial_estate"}
DEFAULT_TEMPLATE_ID = "cinematic_luxury"

# Standard chapter/section types the photo-mapping editor offers, in the agent-facing order they
# appear as picker options (actual on-page order is whatever the agent arranges in `chapters`,
# not this list). "custom" isn't itself a chapter - it's the type a freeform agent-named chapter
# carries (see MAX_CUSTOM_CHAPTERS below).
CHAPTER_TYPES: dict[str, str] = {
    "exterior_arrival": "Exterior / Arrival",
    "great_room": "Living Room / Great Room",
    "kitchen": "Kitchen",
    "dining_room": "Dining Room",
    "primary_suite": "Primary Suite",
    "additional_bedrooms": "Additional Bedrooms",
    "bathrooms": "Bathrooms",
    "grounds_backyard": "Grounds / Backyard",
    "pool_spa": "Pool & Spa",
    "porch_patio": "Porch / Patio",
    "garage_automotive": "Garage / Automotive",
    "office_den": "Office / Den",
    "basement_rec": "Basement / Rec Room",
    "custom": "Custom",
}
MAX_HIGHLIGHTS = 8
MAX_NOTABLE_FACTS = 3
MAX_CUSTOM_CHAPTERS = 3
MAX_PHOTOS_PER_CHAPTER = 12

_DOCUMENT_S3_PREFIX = "property-site-documents"
_FLOOR_PLAN_S3_PREFIX = "property-site-floor-plans"
_DOCUMENT_MAX_BYTES = 25 * 1024 * 1024  # generous for a brochure/disclosure PDF packet
_FLOOR_PLAN_MAX_BYTES = 12 * 1024 * 1024  # matches the existing image upload cap elsewhere
_ALLOWED_DOCUMENT_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
_ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png"}


def _extension_for(content_type: str) -> str:
    if content_type == "application/pdf":
        return ".pdf"
    if content_type == "image/png":
        return ".png"
    return ".jpg"


async def _unique_property_slug(db: AsyncSession, base: str) -> str:
    """Same collision-avoidance shape as site_service._unique_subdomain_slug, but checks BOTH
    Site.subdomain_slug and PropertyWebsite.slug - the two tables share one DNS namespace under
    OHP_BASE_DOMAIN, so a property site can never collide with an agent's whole-site subdomain
    (or another property site's) even though they live in separate tables."""
    base = _slugify(base)
    candidate = base
    suffix = 2
    while True:
        if candidate not in RESERVED_SLUGS and SLUG_PATTERN.fullmatch(candidate):
            site_hit = (await db.execute(select(Site.id).where(Site.subdomain_slug == candidate))).scalar_one_or_none()
            pw_hit = (await db.execute(select(PropertyWebsite.id).where(PropertyWebsite.slug == candidate))).scalar_one_or_none()
            if not site_hit and not pw_hit:
                return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


_FEATURE_CATEGORY_KEYWORDS = {
    "lifestyle": ("pool", "spa", "sauna", "gym", "wine", "theater", "sport", "tennis", "gourmet", "home office", "elevator"),
    "community": ("hoa", "association", "gated", "golf", "clubhouse", "concierge", "waterfront community"),
}


def _categorize_features(p: Property) -> dict:
    """Groups the raw MLS feature JSONB fields (already flat string lists) into the
    interior/exterior/lifestyle/community/other buckets the template shows, by keyword match
    against a short list - real MLS feature strings are short human phrases ("Pool - In Ground",
    "Home Theater"), not codes, so a substring match on a handful of keywords catches the
    lifestyle/community-worthy ones without needing an MLS field-code lookup table."""
    interior = [str(v) for v in (p.interior_features or []) if v] + [str(v) for v in (p.appliances or []) if v]
    exterior = [str(v) for v in (p.exterior_features or []) if v]
    other = [str(v) for v in (p.lot_features or []) if v] + [str(v) for v in (p.view or []) if v]
    if p.association_amenities:
        other.extend(str(v) for v in p.association_amenities if v)

    lifestyle: list[str] = []
    community: list[str] = []
    kept_interior: list[str] = []
    kept_exterior: list[str] = []
    kept_other: list[str] = []
    for bucket, target in ((interior, kept_interior), (exterior, kept_exterior), (other, kept_other)):
        for feature in bucket:
            low = feature.lower()
            if any(kw in low for kw in _FEATURE_CATEGORY_KEYWORDS["lifestyle"]):
                lifestyle.append(feature)
            elif any(kw in low for kw in _FEATURE_CATEGORY_KEYWORDS["community"]):
                community.append(feature)
            else:
                target.append(feature)

    return {
        "interior": kept_interior[:20],
        "exterior": kept_exterior[:20],
        "lifestyle": lifestyle[:12],
        "community": community[:12],
        "other": kept_other[:12],
    }


def _build_property_snapshot(p: Property) -> dict:
    return {
        "listing_key": p.listing_key,
        "mls_number": p.listing_id,
        "address": p.street_address or p.full_address,
        "unparsed_address": p.unparsed_address,
        "city": p.city,
        "state": p.state,
        "zipcode": p.zipcode,
        "latitude": p.google_rooftop_latitude if p.google_rooftop_latitude is not None else p.latitude,
        "longitude": p.google_rooftop_longitude if p.google_rooftop_longitude is not None else p.longitude,
        "price": p.price,
        "original_list_price": p.original_list_price,
        "status": p.home_status,
        "beds": p.bedrooms,
        "baths": p.bathrooms,
        "sqft": p.living_area,
        "lot_size": p.lot_size,
        "lot_size_acres": p.lot_size_acres,
        "year_built": p.year_built,
        "home_type": p.home_type,
        "property_type": p.mls_property_type,
        "architectural_style": p.architectural_style if isinstance(p.architectural_style, list) else [],
        "description": p.description,
        "photos": p.photos if isinstance(p.photos, list) else [],
        "county": p.county,
        "township": p.township,
        "subdivision_name": p.subdivision_name,
        "school_district_name": p.school_district_name,
        "elementary_school": p.elementary_school,
        "middle_or_junior_school": p.middle_or_junior_school,
        "high_school": p.high_school,
        "features": _categorize_features(p),
        "garage_spaces": p.garage_spaces,
        "has_garage": p.has_garage,
        "stories": p.stories,
        "has_association": p.has_association,
        "association_fee": p.association_fee,
        "association_fee_frequency": p.association_fee_frequency,
        "list_agent_full_name": p.list_agent_full_name,
        "list_agent_email": p.list_agent_email,
        "list_agent_phone": p.list_agent_preferred_phone,
        "list_office_name": p.list_office_name,
        "list_office_phone": p.list_office_phone,
    }


_EMPTY_FEATURES = {"interior": [], "exterior": [], "lifestyle": [], "community": [], "other": []}


def normalize_manual_snapshot(manual: dict) -> dict:
    """A hand-entered (non-MLS) listing arrives in the picker's LinkedProperty shape (`acres`,
    a single `image_url`, maybe `photos: null`) rather than _build_property_snapshot's shape -
    map it onto the same keys the templates read so a manual Spotlight renders its lot size and
    cover photo instead of silently dropping them. Idempotent, so it's also safe to run on an
    already-stored snapshot at render time (older manual sites were saved un-normalized)."""
    snap = {**(manual or {})}
    photos = snap.get("photos") if isinstance(snap.get("photos"), list) else []
    photos = [u for u in photos if isinstance(u, str) and u]
    if not photos and isinstance(snap.get("image_url"), str) and snap["image_url"]:
        photos = [snap["image_url"]]
    snap["photos"] = photos
    if snap.get("lot_size_acres") is None and snap.get("acres") is not None:
        snap["lot_size_acres"] = snap.get("acres")
    if snap.get("original_list_price") is None and snap.get("original_price") is not None:
        snap["original_list_price"] = snap.get("original_price")
    features = snap.get("features") if isinstance(snap.get("features"), dict) else {}
    snap["features"] = {k: [str(v) for v in (features.get(k) or []) if v] for k in _EMPTY_FEATURES}
    snap["listing_key"] = None
    return snap


_OFF_MARKET = re.compile(r"closed|sold|expired|withdrawn|cancel", re.I)


async def _find_property_by_key(db: AsyncSession, listing_key: str) -> Optional[Property]:
    """The MLS listing for a key (ownership is checked by the caller - a Spotlight is only for the
    agent's own current listings since 10/19)."""
    result = await db.execute(select(Property).where(Property.listing_key == listing_key))
    return result.scalar_one_or_none()


async def _default_draft_data(db: AsyncSession, owner: User, snapshot: dict) -> dict:
    """Auto-populates the reusable-across-templates content: hero image, photo order, an AI Video
    if one already exists for this property, and section visibility defaults (everything on
    except sections with nothing to show). Template-specific layout/color choices live in the
    template component itself, not here - see the frontend template renderer."""
    photos = snapshot.get("photos") or []
    video_asset_id = None
    if snapshot.get("listing_key"):
        result = await db.execute(
            select(StudioMediaAsset)
            .where(
                StudioMediaAsset.owner_id == owner.id,
                StudioMediaAsset.kind == "video",
                StudioMediaAsset.status == "succeeded",
                StudioMediaAsset.linked_property["listing_key"].astext == snapshot["listing_key"],
            )
            .order_by(StudioMediaAsset.created_at.desc())
        )
        video = result.scalars().first()
        video_asset_id = video.id if video else None

    open_houses: list[dict] = []
    if snapshot.get("listing_key"):
        result = await db.execute(
            select(OpenHouseEvent).where(
                OpenHouseEvent.listing_key == snapshot["listing_key"],
                OpenHouseEvent.agent_id == owner.id,
                OpenHouseEvent.is_deleted.is_(False),
                OpenHouseEvent.scheduled_date.is_not(None),
                OpenHouseEvent.scheduled_date >= date.today(),
            ).order_by(OpenHouseEvent.scheduled_date.asc())
        )
        open_houses = [
            {
                "id": e.id,
                "date": e.scheduled_date.isoformat() if e.scheduled_date else None,
                "start_time": e.scheduled_start_time.isoformat() if e.scheduled_start_time else None,
                "end_time": e.scheduled_end_time.isoformat() if e.scheduled_end_time else None,
            }
            for e in result.scalars().all()
        ]

    # A brand-new site has no categorized features and no highlights yet either, so this is
    # `False` the moment the site is created either way - it's here mainly so the key exists with
    # the right *shape* of default (matching gallery/video/open_houses right next to it) rather
    # than a hardcoded `True` that used to render as an empty section. The live page still
    # recomputes this from whatever's actually in the data at render time (see hasFeatures in
    # CinematicLuxuryTemplate.tsx) - an agent adding Highlights later turns the section back on
    # without ever having to touch this toggle.
    has_any_features = any(snapshot.get("features", {}).get(k) for k in ("interior", "exterior", "lifestyle", "community", "other"))

    return {
        "hero_image": photos[0] if photos else None,
        "photo_order": list(range(len(photos))),
        "description_override": None,
        "featured_amenities": [],
        "notable_facts": [],
        "highlights": [],
        "chapters": [],
        "video_asset_id": video_asset_id,
        "virtual_tour_url": None,
        "floor_plans": [],
        "documents": [],
        "agents": [],
        "open_houses": open_houses,
        "colors": None,
        "section_visibility": {
            "story": True, "features": has_any_features, "gallery": bool(photos), "video": bool(video_asset_id),
            "virtual_tour": False, "floor_plans": False, "location": True, "open_houses": True,
            "documents": False, "agents": True,
        },
    }


async def create_property_website(
    db: AsyncSession, owner: User, *, listing_key: Optional[str] = None,
    manual_property: Optional[dict] = None, template_id: str = DEFAULT_TEMPLATE_ID,
) -> PropertyWebsite:
    if template_id not in TEMPLATE_IDS:
        template_id = DEFAULT_TEMPLATE_ID

    # A Spotlight is the listing agent's own page for their own listing (user, 10/19): only the
    # agent's current listings (listing or co-listing agent; hand-entered ones count) - never
    # another agent's home, a sold one, or a typed-in address.
    if not listing_key:
        raise ValueError("Pick one of your current listings - a Spotlight is for your own listings.")
    prop = await _find_property_by_key(db, listing_key)
    if not prop:
        raise ValueError("That listing could not be found.")
    from app.utils.plan_ids import listing_owner_key
    if listing_owner_key(owner) not in {prop.list_agent_mls_id, prop.co_list_agent_mls_id}:
        raise ValueError("A Spotlight can only be made for your own listing.")
    if _OFF_MARKET.search(prop.home_status or ""):
        raise ValueError("That listing is no longer on the market - a Spotlight is for your current listings.")
    snapshot = _build_property_snapshot(prop)

    slug_base = snapshot.get("address") or "property"
    slug = await _unique_property_slug(db, slug_base)
    draft = await _default_draft_data(db, owner, snapshot)

    site = PropertyWebsite(
        owner_id=owner.id, listing_key=listing_key, template_id=template_id, slug=slug,
        property_snapshot=snapshot, draft_data=draft, published_snapshot={},
    )
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return site


async def list_for_owner(db: AsyncSession, owner_id: str) -> list[PropertyWebsite]:
    result = await db.execute(
        select(PropertyWebsite).where(PropertyWebsite.owner_id == owner_id).order_by(PropertyWebsite.created_at.desc())
    )
    return list(result.scalars().all())


async def get_owned(db: AsyncSession, owner_id: str, site_id: str) -> Optional[PropertyWebsite]:
    result = await db.execute(
        select(PropertyWebsite).where(PropertyWebsite.id == site_id, PropertyWebsite.owner_id == owner_id)
    )
    return result.scalar_one_or_none()


def _uploaded_urls(draft: dict) -> set:
    """Every S3 URL this draft currently references from an agent-uploaded document or floor
    plan (never the MLS-sourced hero/gallery photos, which live in property_snapshot, not
    draft_data, and were never uploaded through this service to begin with)."""
    urls = {d.get("url") for d in (draft.get("documents") or []) if isinstance(d, dict) and d.get("url")}
    urls |= {f.get("image_url") for f in (draft.get("floor_plans") or []) if isinstance(f, dict) and f.get("image_url")}
    return urls


def _clean_chapters(chapters: list) -> list:
    """Silently caps counts/lengths rather than rejecting the save outright - same convention as
    _categorize_features' [:20]/[:12] slices above. A chapter with no `type` or an unknown one
    falls back to "custom" so a stale/older client payload never crashes rendering."""
    cleaned = []
    custom_count = 0
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        chapter_type = chapter.get("type") if chapter.get("type") in CHAPTER_TYPES else "custom"
        if chapter_type == "custom":
            custom_count += 1
            if custom_count > MAX_CUSTOM_CHAPTERS:
                continue
        photos = [p for p in (chapter.get("photos") or []) if isinstance(p, str)][:MAX_PHOTOS_PER_CHAPTER]
        cleaned.append({
            "id": str(chapter.get("id") or f"{chapter_type}-{len(cleaned)}"),
            "type": chapter_type,
            "label": str(chapter.get("label") or CHAPTER_TYPES.get(chapter_type, "Custom"))[:60],
            "enabled": bool(chapter.get("enabled", True)),
            "photos": photos,
            "copy": str(chapter.get("copy") or "")[:1200],
        })
    return cleaned


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


def _clean_open_houses(events: list) -> list:
    """Agent-entered open house dates (the ones the Open House Manager doesn't already know
    about - see upcoming_open_houses). Keeps only well-formed YYYY-MM-DD / HH:MM values so the
    public page's calendar links never get a half-typed date."""
    cleaned = []
    for e in events or []:
        if not isinstance(e, dict) or not _DATE_RE.match(str(e.get("date") or "")):
            continue
        start = str(e.get("start_time") or "")[:8]
        end = str(e.get("end_time") or "")[:8]
        cleaned.append({
            "id": str(e.get("id") or f"manual-{e['date']}-{start}"),
            "date": e["date"],
            "start_time": start if _TIME_RE.match(start) else None,
            "end_time": end if _TIME_RE.match(end) else None,
            "manual": True,
        })
    return cleaned[:12]


def _clean_town_facts(raw: Any) -> Optional[dict]:
    """The structured "Life in <town>" facts - short strings and short lists only."""
    if not isinstance(raw, dict):
        return None
    def txt(v: Any, n: int) -> str:
        return str(v or "").strip()[:n]
    def lst(v: Any, count: int, n: int) -> list:
        return [txt(x, n) for x in (v if isinstance(v, list) else []) if txt(x, n)][:count]
    downtown = raw.get("downtown") if isinstance(raw.get("downtown"), dict) else {}
    around = []
    for item in raw.get("getting_around") if isinstance(raw.get("getting_around"), list) else []:
        if isinstance(item, dict) and txt(item.get("label"), 80):
            around.append({"label": txt(item.get("label"), 80), "detail": txt(item.get("detail"), 60)})
    return {
        "tagline": txt(raw.get("tagline"), 140),
        "known_for": lst(raw.get("known_for"), 6, 50),
        "downtown": {"name": txt(downtown.get("name"), 60), "where": txt(downtown.get("where"), 90),
                     "highlights": lst(downtown.get("highlights"), 4, 60)},
        "why_move": lst(raw.get("why_move"), 4, 90),
        "getting_around": around[:3],
        "outdoors": lst(raw.get("outdoors"), 4, 60),
        "whats_new": txt(raw.get("whats_new"), 140),
    }


def _url(value, limit: int = 1000) -> Optional[str]:
    v = str(value or "").strip()
    return v[:limit] if v.startswith("https://") or v.startswith("http://") else None


def _clean_header(raw) -> Optional[dict]:
    """The page header: mode 'photo' (the main photo), 'cycle' (a slideshow of the listing
    photos) or 'video' (the AI living photo); motion 'in' | 'out' | 'both' | 'none'."""
    if not isinstance(raw, dict):
        return None
    video = raw.get("video") if isinstance(raw.get("video"), dict) else None
    clean_video = {
        "asset_id": str(video.get("asset_id") or "")[:64] or None,
        "style": video.get("style") if video.get("style") in ("glide", "dusk") else "glide",
        "photo": _url(video.get("photo")),
        "url": _url(video.get("url")),
        "poster": _url(video.get("poster")),
        # The agent looked at it on Review and settled the header - Review stops asking.
        "reviewed": bool(video.get("reviewed")),
    } if video and video.get("asset_id") else None
    mode = raw.get("mode") if raw.get("mode") in ("photo", "cycle", "video") else "photo"
    if mode == "video" and not (clean_video and clean_video["url"]):
        mode = "photo"  # never publish a video header that isn't ready
    # The main photo's framing (editor's "Frame your photo"): the spot kept centered, 0-100 of the
    # photo, and how far the slow zoom travels. Missing = centered, the default zoom.
    frame = raw.get("frame") if isinstance(raw.get("frame"), dict) else None

    def _num(v, lo, hi, default):
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return default

    # x / y = the computer's spot; px / py = the phone's own spot (a tall phone header often needs
    # a different one - user, 10/15), the computer's when never set. desk_set / phone_set = the
    # agent confirmed each one (the editor asks for both).
    clean_frame = {
        "x": _num(frame.get("x"), 0, 100, 50),
        "y": _num(frame.get("y"), 0, 100, 50),
        "zoom": _num(frame.get("zoom"), 1.0, 1.3, 1.12),
    } if frame else None
    if clean_frame is not None:
        clean_frame["px"] = _num(frame.get("px"), 0, 100, clean_frame["x"])
        clean_frame["py"] = _num(frame.get("py"), 0, 100, clean_frame["y"])
        clean_frame["desk_set"] = bool(frame.get("desk_set"))
        clean_frame["phone_set"] = bool(frame.get("phone_set"))
    return {
        "mode": mode,
        "motion": raw.get("motion") if raw.get("motion") in ("in", "out", "both", "none") else "out",
        "video": clean_video,
        "frame": clean_frame,
    }


def _clean_town_photo(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or not _url(raw.get("url")):
        return None
    return {"url": _url(raw.get("url")), "credit": str(raw.get("credit") or "")[:140],
            "caption": str(raw.get("caption") or "")[:120], "source_url": _url(raw.get("source_url"))}


async def update_draft(db: AsyncSession, site: PropertyWebsite, patch: dict) -> PropertyWebsite:
    draft = dict(site.draft_data or {})
    before = _uploaded_urls(draft) if ("documents" in patch or "floor_plans" in patch) else set()
    if "highlights" in patch:
        patch["highlights"] = [str(h)[:120] for h in (patch["highlights"] or []) if str(h or "").strip()][:MAX_HIGHLIGHTS]
    if "notable_facts" in patch:
        patch["notable_facts"] = [str(f)[:60] for f in (patch["notable_facts"] or []) if str(f or "").strip()][:MAX_NOTABLE_FACTS]
    if "chapters" in patch:
        patch["chapters"] = _clean_chapters(patch["chapters"])
    if "template_id" in patch and patch["template_id"] not in TEMPLATE_IDS:
        patch.pop("template_id")
    if "open_houses" in patch:
        patch["open_houses"] = _clean_open_houses(patch["open_houses"])
    if "story_mode" in patch and patch["story_mode"] not in ("short", "original", None):
        patch.pop("story_mode")
    if "story_short" in patch:
        patch["story_short"] = str(patch["story_short"] or "")[:4000] or None
    if "town_story" in patch:
        town = patch["town_story"] if isinstance(patch["town_story"], dict) else None
        patch["town_story"] = {
            "name": str(town.get("name") or "")[:80],
            "body": str(town.get("body") or "")[:3000],
            "hidden": bool(town.get("hidden")),
            "facts": _clean_town_facts(town.get("facts")),
            "photo": _clean_town_photo(town.get("photo")),
            "photo_off": bool(town.get("photo_off")),  # the agent chose "no photo" - don't auto-pick one
        } if town else None
    if "header" in patch:
        patch["header"] = _clean_header(patch["header"])
    if "story_photo" in patch:
        patch["story_photo"] = _url(patch["story_photo"])
    draft.update(patch)
    site.draft_data = draft
    await db.commit()
    await db.refresh(site)
    # A document/floor-plan removed from the draft (not just replaced by a different upload) -
    # release its storage-ledger bytes and delete the S3 object, same as removing any other
    # tracked upload elsewhere in the app. Never touches a URL still referenced after the edit.
    removed = before - _uploaded_urls(draft)
    for url in removed:
        s3_service.delete_by_url(url)
        await storage_ledger_service.release(db, url)
    return site


async def publish(db: AsyncSession, site: PropertyWebsite) -> PropertyWebsite:
    site.published_snapshot = deepcopy(site.draft_data)
    # Switching templates is a draft edit like any other (see update_draft) - it only reaches the
    # live page when the agent publishes, same as every other change.
    if (site.draft_data or {}).get("template_id") in TEMPLATE_IDS:
        site.template_id = site.draft_data["template_id"]
    site.status = "PUBLISHED"
    site.published_at = datetime.now(timezone.utc)
    db.add(site)
    await db.commit()
    await db.refresh(site)
    domain_resolution_service.invalidate_property_website(site.id)
    return site


async def unpublish(db: AsyncSession, site: PropertyWebsite) -> PropertyWebsite:
    site.status = "DRAFT"
    db.add(site)
    await db.commit()
    await db.refresh(site)
    domain_resolution_service.invalidate_property_website(site.id)
    return site


async def delete(db: AsyncSession, site: PropertyWebsite) -> None:
    site_id = site.id
    uploaded = _uploaded_urls(site.draft_data or {}) | _uploaded_urls(site.published_snapshot or {})
    await db.delete(site)
    await db.commit()
    domain_resolution_service.invalidate_property_website(site_id)
    for url in uploaded:
        s3_service.delete_by_url(url)
        await storage_ledger_service.release(db, url)


async def upload_document(db: AsyncSession, owner: User, data: bytes, content_type: str) -> str:
    """Uploads a brochure/disclosure/feature-sheet file (PDF or image), charges it to the
    agent's own storage cap exactly like every other upload in the app, and returns its public
    (CDN-fronted) URL - see s3_service.PUBLIC_URL_HOST for the CloudFront layer already in front
    of S3, so repeated visitor downloads don't hit S3 per-request."""
    if content_type not in _ALLOWED_DOCUMENT_CONTENT_TYPES:
        raise ValueError("Please upload a PDF, JPG, or PNG.")
    if len(data) > _DOCUMENT_MAX_BYTES:
        raise ValueError("File must be 25MB or smaller.")
    config = await ai_config_service.get_config(db)
    await storage_ledger_service.enforce_cap(db, owner.id, config, owner.plan_tier, len(data))
    try:
        url = s3_service.upload_bytes(data, _DOCUMENT_S3_PREFIX, _extension_for(content_type), content_type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't save that file.") from exc
    await storage_ledger_service.record(db, owner.id, "property_site_document", url, len(data))
    return url


async def upload_floor_plan(db: AsyncSession, owner: User, data: bytes, content_type: str) -> str:
    if content_type not in _ALLOWED_IMAGE_CONTENT_TYPES:
        raise ValueError("Please upload a JPG or PNG.")
    if len(data) > _FLOOR_PLAN_MAX_BYTES:
        raise ValueError("Image must be 12MB or smaller.")
    config = await ai_config_service.get_config(db)
    await storage_ledger_service.enforce_cap(db, owner.id, config, owner.plan_tier, len(data))
    try:
        url = s3_service.upload_bytes(data, _FLOOR_PLAN_S3_PREFIX, _extension_for(content_type), content_type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't save that image.") from exc
    await storage_ledger_service.record(db, owner.id, "property_site_floor_plan", url, len(data))
    return url


async def public_url(db: AsyncSession, site: PropertyWebsite) -> str:
    """The link shown to (and copied by) the agent for this site - matches whichever hostname
    domain_resolution_service.resolve_property_site would actually resolve back to this site:
    the owner's own connected, ACTIVE custom domain if they have one (same one their whole-agent
    Site uses), else the default <slug>.OHP_BASE_DOMAIN subdomain."""
    result = await db.execute(
        select(SiteDomain.hostname)
        .join(Site, Site.id == SiteDomain.site_id)
        # An IDX site the agent switched off (Settings -> IDX Website) means links fall back to
        # openhousepal.com, same rule as frontend/lib/publicResourceLinks.ts.
        .where(Site.owner_id == site.owner_id, SiteDomain.status == "ACTIVE", SiteDomain.is_primary.is_(True), Site.is_enabled.is_(True))
    )
    custom_hostname = result.scalars().first()
    # "Use my own domain" switched off (Settings -> IDX Website): links stay on openhousepal.com.
    if custom_hostname:
        from app.models.database import User
        prefers_own = (await db.execute(select(User.links_use_own_domain).where(User.id == site.owner_id))).scalar_one_or_none()
        if prefers_own is False:
            custom_hostname = None
    # Local development: the public hostnames don't resolve to this machine, so hand out the
    # frontend's own host-render route (what proxy.ts rewrites those hostnames to in production).
    frontend_base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "")).rstrip("/")
    if frontend_base and ("localhost" in frontend_base or "127.0.0.1" in frontend_base):
        if custom_hostname:
            return f"{frontend_base}/ohp-site-render/{custom_hostname}/listings/{site.slug}"
        return f"{frontend_base}/ohp-site-render/{site.slug}.{domain_resolution_service.OHP_BASE_DOMAIN}"
    from app.services.website.site_service import host_resolves
    host = custom_hostname or f"{site.slug}.{domain_resolution_service.OHP_BASE_DOMAIN}"
    path = f"/listings/{site.slug}" if custom_hostname else ""
    if not await host_resolves(host):
        # The address doesn't open on the internet yet (no wildcard DNS for *.openhousepal.com,
        # or a custom domain still pointing elsewhere) - the same page on openhousepal.com.
        platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "https://openhousepal.com")).rstrip("/")
        return f"{platform}/ohp-site-render/{host}{path}"
    return f"https://{host}{path}"


def _normalize_address(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def listing_override_key(listing_key: Optional[str], fallback_address: Optional[str]) -> Optional[str]:
    """The identity a per-listing routing override is stored under in
    AgentHub.config['listingLinkOverrides'] - same matching rule describe_listing_link itself
    uses (listing_key for an MLS-sourced listing, normalized address for a manual one), so an
    override set from the Settings routing panel always lands on the same listing the resolver
    would have matched a Spotlight to."""
    if listing_key:
        return f"lk:{listing_key}"
    if fallback_address:
        normalized = _normalize_address(fallback_address)
        return f"addr:{normalized}" if normalized else None
    return None


async def describe_listing_link(
    db: AsyncSession, owner: User, listing_key: Optional[str], fallback_address: Optional[str] = None,
    *, skip_spotlight: bool = False,
) -> dict:
    """Where a click on one of the agent's own active listings should go - the same "smart QR"
    behavior the Similar Properties Sheet's per-comp QR codes use, now shared by the Agent Hub
    and the email signature: the agent's own published Listing Spotlight for this EXACT property
    when they've made one (a much richer page - full photo set, chapters, floor plans), otherwise
    the generic one-page property view every comp QR already links to
    (frontend/app/property/[propertyId]/[agentId] - photo, price, specs, and a Schedule Showing /
    Message Agent form). Matches on listing_key for an MLS-sourced listing; falls back to a
    normalized address match for a manually-entered one, since neither side has a real
    listing_key to compare there.

    Returns {"source": "spotlight" | "property_page" | "none", "url": str | None, ...}. The
    Settings "Listing Link Routing" panel calls this directly (instead of resolve_listing_link)
    so it can show the agent which branch was taken and surface the matched Spotlight; every
    other caller just wants the plain URL, via resolve_listing_link below.

    skip_spotlight=True forces the property_page/none branches even when a Spotlight match
    exists - how an agent's per-listing "always use the standard page" override is applied."""
    if not skip_spotlight:
        query = select(PropertyWebsite).where(PropertyWebsite.owner_id == owner.id, PropertyWebsite.status == "PUBLISHED")
        if listing_key:
            query = query.where(PropertyWebsite.listing_key == listing_key)
        elif fallback_address:
            query = query.where(PropertyWebsite.listing_key.is_(None))
        else:
            query = None
        if query is not None:
            result = await db.execute(query.order_by(PropertyWebsite.updated_at.desc()))
            sites = list(result.scalars().all())
            if not listing_key and fallback_address:
                norm = _normalize_address(fallback_address)
                sites = [s for s in sites if _normalize_address((s.property_snapshot or {}).get("address") or "") == norm]
            if sites:
                site = sites[0]
                return {
                    "source": "spotlight",
                    "url": await public_url(db, site),
                    "spotlightId": site.id,
                    "spotlightSlug": site.slug,
                }
    if listing_key:
        frontend_base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000"))
        return {"source": "property_page", "url": f"{frontend_base}/property/{listing_key}/{owner.id}"}
    return {"source": "none", "url": None}


async def resolve_listing_link(
    db: AsyncSession, owner: User, listing_key: Optional[str], fallback_address: Optional[str] = None,
    *, skip_spotlight: bool = False,
) -> Optional[str]:
    """Thin wrapper around describe_listing_link for callers that only need the URL (Agent Hub
    listing cards, the email signature banner) - see describe_listing_link for the full
    resolution rule. Returns None if there's nothing sensible to link to (a manual listing with
    no matching Spotlight - the generic property page needs a real listing_key, there's no page
    built for arbitrary hand-entered data)."""
    info = await describe_listing_link(db, owner, listing_key, fallback_address, skip_spotlight=skip_spotlight)
    return info["url"]


async def submit_inquiry(db: AsyncSession, site: PropertyWebsite, *, name: str, email: Optional[str], phone: Optional[str],
                         message: Optional[str], came_from: Optional[dict] = None) -> None:
    from app.services.visit_source import clean_came_from
    address = _nice_address(site)
    snapshot = {"message": message, "property_website_id": site.id}
    if came_from is not None:
        snapshot["came_from"] = clean_came_from(came_from)
    lead = await leads_service.upsert_lead(
        db, owner_id=site.owner_id, name=name, email=email, phone=phone,
        source_type="property_website_inquiry", source_id=site.id,
        summary=f"Asked about {address} on your Spotlight",
        occurred_at=datetime.now(timezone.utc), property_address=address,
        raw_snapshot=snapshot,
    )
    await _notify_owner(
        db, site, lead, title=f"New Spotlight inquiry: {name or email}",
        message=f"{name or 'A visitor'} asked about {address} on your Spotlight.",
        visitor_name=name, email=email, phone=phone, lead_message=message,
    )


# One visit row per (site, visitor) at most this often - a reload or a second page within the
# same short session is the same visit, so total_views reads as visits rather than page loads.
_VIEW_DEDUPE_WINDOW = timedelta(minutes=30)


async def record_view(db: AsyncSession, site_id: str, visitor_id: str) -> bool:
    """Records a public visit to a PUBLISHED Spotlight site. No-op (returns False) for an
    unknown/unpublished site or a repeat visit inside the dedupe window. The caller has already
    excluded the site's own agent (the frontend beacon never fires for an owner-opened page)."""
    site = (await db.execute(
        select(PropertyWebsite.id).where(PropertyWebsite.id == site_id, PropertyWebsite.status == "PUBLISHED")
    )).scalar_one_or_none()
    if not site:
        return False

    cutoff = datetime.now(timezone.utc) - _VIEW_DEDUPE_WINDOW
    recent = (await db.execute(
        select(PropertyWebsiteView.id).where(
            PropertyWebsiteView.property_website_id == site_id,
            PropertyWebsiteView.visitor_id == visitor_id,
            PropertyWebsiteView.created_at >= cutoff,
        ).limit(1)
    )).scalar_one_or_none()
    if recent:
        return False

    db.add(PropertyWebsiteView(property_website_id=site_id, visitor_id=visitor_id))
    await db.commit()
    return True


async def analytics_for_owner(db: AsyncSession, owner_id: str) -> dict:
    """Visitor stats for every Spotlight site this agent owns: unique visitors (distinct
    browsers) and total visits, per site plus a de-duplicated overall total (a visitor who
    viewed two of the agent's sites counts once overall, twice across the per-site rows)."""
    per_site_rows = (await db.execute(
        select(
            PropertyWebsiteView.property_website_id,
            func.count(distinct(PropertyWebsiteView.visitor_id)),
            func.count(PropertyWebsiteView.id),
        )
        .join(PropertyWebsite, PropertyWebsite.id == PropertyWebsiteView.property_website_id)
        .where(PropertyWebsite.owner_id == owner_id)
        .group_by(PropertyWebsiteView.property_website_id)
    )).all()

    totals_row = (await db.execute(
        select(
            func.count(distinct(PropertyWebsiteView.visitor_id)),
            func.count(PropertyWebsiteView.id),
        )
        .join(PropertyWebsite, PropertyWebsite.id == PropertyWebsiteView.property_website_id)
        .where(PropertyWebsite.owner_id == owner_id)
    )).one()

    # Leads = distinct people who asked for a tour or sent a question (incl. price/open house
    # alert sign-ups) from any of this agent's Spotlight pages.
    from app.models.database import LeadEvent
    leads = (await db.execute(
        select(func.count(distinct(LeadEvent.lead_id)))
        .join(PropertyWebsite, PropertyWebsite.id == LeadEvent.source_id)
        .where(
            PropertyWebsite.owner_id == owner_id,
            LeadEvent.source_type.in_(("property_website_inquiry", "property_website_tour_request")),
        )
    )).scalar_one()

    return {
        "totals": {
            "unique_visitors": int(totals_row[0] or 0),
            "total_views": int(totals_row[1] or 0),
            "leads": int(leads or 0),
        },
        "sites": {
            site_id: {"unique_visitors": int(uniques or 0), "total_views": int(views or 0)}
            for site_id, uniques, views in per_site_rows
        },
    }


async def submit_tour_request(
    db: AsyncSession, site: PropertyWebsite, *, name: str, email: Optional[str], phone: Optional[str],
    tour_type: str, requested_date: Optional[str], preferred_time: Optional[str], notes: Optional[str],
    came_from: Optional[dict] = None,
) -> None:
    from app.services.visit_source import clean_came_from
    address = _nice_address(site)
    kind = "in-person" if tour_type == "in_person" else "virtual"
    lead = await leads_service.upsert_lead(
        db, owner_id=site.owner_id, name=name, email=email, phone=phone,
        source_type="property_website_tour_request", source_id=site.id,
        summary=f"Requested {'an' if kind[0] in 'aeiou' else 'a'} {kind} tour of {address} on your Spotlight",
        occurred_at=datetime.now(timezone.utc), property_address=address,
        raw_snapshot={
            "tour_type": tour_type, "requested_date": requested_date, "preferred_time": preferred_time,
            "notes": notes, "property_website_id": site.id,
            **({"came_from": clean_came_from(came_from)} if came_from is not None else {}),
        },
    )
    when = " at ".join(x for x in [_pretty_date(requested_date), preferred_time] if x)
    article = "an" if kind[0] in "aeiou" else "a"
    detail = f"Requested {article} {kind} tour" + (f" for {when}" if when else "") + "."
    await _notify_owner(
        db, site, lead, title=f"New tour request: {name or email}",
        message=f"{name or 'A visitor'} wants {article} {kind} tour of {address}" + (f" ({when})." if when else "."),
        visitor_name=name, email=email, phone=phone,
        lead_message=detail + (f" Notes: {notes}" if notes else ""),
    )


def _nice_address(site: PropertyWebsite, fallback: str = "this property") -> str:
    """"847 LESLEY RD" (how MLS delivers it) -> "847 Lesley Rd" for lead summaries/notifications."""
    from app.services.tasks_service import display_address
    raw = (site.property_snapshot or {}).get("address") or ""
    return (display_address(raw)[0] or raw) if raw else fallback


def _pretty_date(value: Optional[str]) -> Optional[str]:
    try:
        d = datetime.strptime(value or "", "%Y-%m-%d")
    except ValueError:
        return value or None
    return f"{d.strftime('%a, %b')} {d.day}"


async def _notify_owner(
    db: AsyncSession, site: PropertyWebsite, lead, *, title: str, message: str,
    visitor_name: Optional[str], email: Optional[str], phone: Optional[str], lead_message: Optional[str],
) -> None:
    """The in-app bell + (preference-gated) email for a Spotlight inquiry/tour request - the same
    pair every other lead source already sends (see portal_inbound_service). Never fatal: the lead
    itself is already saved by the time this runs."""
    from app.services.agent_email_service import absolute, email_agent
    address = _nice_address(site, "your listing")
    link = f"/leads/{lead.id}" if lead is not None else "/leads"
    owner = await db.get(User, site.owner_id)
    if owner is None:
        return
    try:
        db.add(Notification(
            agent_id=owner.id, type="PROPERTY_INQUIRY", reference_type="LEAD",
            reference_id=lead.id if lead is not None else site.id,
            title=title[:200], message=message[:500], property_address=address,
            visitor_name=visitor_name, link=link, is_read=False, created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
    who = visitor_name or email or "New lead"
    email_agent(owner, "PROPERTY_INQUIRY", "new_lead_captured", f"New Listing Spotlight lead: {who}", {
        "lead_name": who, "lead_source": "Listing Spotlight", "lead_email": email, "lead_phone": phone,
        "lead_message": lead_message, "property_address": address, "link": absolute(link),
    })


# ---- Public render payload (see public_property_site_routes.resolve_property_site) ----

def _str_list(value) -> list:
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _blank(value) -> bool:
    return value is None or value == "" or value == []


async def live_property_snapshot(db: AsyncSession, site: PropertyWebsite) -> dict:
    """property_snapshot is frozen when the site is created, so on its own a price cut or a move to
    Under Contract/Sold would never reach the live page. For an MLS listing, overlay the volatile
    listing facts (price, status, days on market) from the live Property row every render, and
    backfill detail fields older snapshots never captured (taxes, systems, schools). A manual
    listing has no live source - it's just normalized (see normalize_manual_snapshot)."""
    snap = dict(site.property_snapshot or {})
    if not site.listing_key:
        return normalize_manual_snapshot(snap)
    prop = await _find_property_by_key(db, site.listing_key)
    if prop is None:
        return snap
    if prop.price is not None:
        snap["price"] = prop.price
    if prop.original_list_price is not None:
        snap["original_list_price"] = prop.original_list_price
    if prop.home_status:
        snap["status"] = prop.home_status
    from app.utils.listing_dom import property_days_on_market
    snap["days_on_market"] = property_days_on_market(prop)  # today's count, not the one stored
    snap["price_changed_at"] = prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None
    snap["listed_at"] = prop.mls_list_date.isoformat() if prop.mls_list_date else None
    backfill = {
        "tax_annual_amount": prop.tax_annual_amount, "tax_year": prop.tax_year,
        "heating": _str_list(prop.heating), "cooling": _str_list(prop.cooling),
        "flooring": _str_list(prop.flooring), "basement": _str_list(prop.basement),
        "parking_features": _str_list(prop.parking_features), "fireplaces": prop.fireplaces,
        "water_source": _str_list(prop.water_source), "sewer": _str_list(prop.sewer),
        "association_fee_includes": _str_list(prop.association_fee_includes),
        "new_construction": prop.new_construction_yn,
        "elementary_school": prop.elementary_school, "middle_or_junior_school": prop.middle_or_junior_school,
        "high_school": prop.high_school, "school_district_name": prop.school_district_name,
        "subdivision_name": prop.subdivision_name, "garage_spaces": prop.garage_spaces, "stories": prop.stories,
        "association_fee": prop.association_fee, "association_fee_frequency": prop.association_fee_frequency,
        "mls_number": prop.listing_id, "list_office_name": prop.list_office_name,
        "home_type": prop.home_type, "property_type": prop.mls_property_type,
        "above_grade_finished_area": getattr(prop, "above_grade_finished_area", None),
        "below_grade_finished_area": getattr(prop, "below_grade_finished_area", None),
    }
    for key, value in backfill.items():
        if _blank(snap.get(key)) and not _blank(value):
            snap[key] = value
    if not snap.get("photos") and isinstance(prop.photos, list):
        snap["photos"] = [u for u in prop.photos if isinstance(u, str)]
    return snap


def _clock(t) -> Optional[str]:
    if t is None:
        return None
    return t.strftime("%H:%M") if hasattr(t, "strftime") else (str(t)[:5] or None)


def _event_out(d: date, start, end, source: str, event_id: Optional[str]) -> dict:
    return {"id": event_id or f"{source}-{d.isoformat()}", "date": d.isoformat(),
            "start_time": _clock(start), "end_time": _clock(end), "source": source}


async def upcoming_open_houses(db: AsyncSession, site: PropertyWebsite, content: dict) -> list[dict]:
    """Every upcoming open house for this home, computed live at render time - the Open House
    Manager's scheduled events (the owner's own, or one they're the listing agent on), the MLS
    open house date, and any extra dates the agent typed into the Spotlight editor. Draft entries
    captured when the site was first created (no `manual` flag) are ignored: those were a
    one-time copy of Open House Manager events that are re-read live here, so a cancelled event
    no longer lingers on the page."""
    today = date.today()
    snap = site.property_snapshot or {}
    found: dict[tuple, dict] = {}

    def add(item: dict) -> None:
        found.setdefault((item["date"], item.get("start_time") or ""), item)

    query = select(OpenHouseEvent).where(
        or_(OpenHouseEvent.agent_id == site.owner_id, OpenHouseEvent.listing_owner_agent_id == site.owner_id),
        OpenHouseEvent.is_deleted.is_(False),
        OpenHouseEvent.scheduled_date.is_not(None),
        OpenHouseEvent.scheduled_date >= today,
    )
    if site.listing_key:
        query = query.where(OpenHouseEvent.listing_key == site.listing_key)
    events = list((await db.execute(query.order_by(OpenHouseEvent.scheduled_date.asc()).limit(40))).scalars().all())
    if not site.listing_key:
        norm = _normalize_address(snap.get("address") or "")
        events = [e for e in events if norm and _normalize_address(e.address or "").startswith(norm)]
    for e in events:
        add(_event_out(e.scheduled_date, e.scheduled_start_time, e.scheduled_end_time, "open_house_manager", e.id))

    if site.listing_key:
        prop = await _find_property_by_key(db, site.listing_key)
        if prop is not None:
            for slot in upcoming_mls_open_houses(prop, today):
                item = _event_out(slot["date"], slot["start"], slot["end"], "mls", None)
                item["id"] = f"mls-{slot['date'].isoformat()}-{item.get('start_time') or 'x'}"
                add(item)

    for e in content.get("open_houses") or []:
        if not isinstance(e, dict) or not e.get("manual"):
            continue
        try:
            d = date.fromisoformat(str(e.get("date")))
        except ValueError:
            continue
        if d >= today:
            add({"id": e.get("id") or f"manual-{d.isoformat()}", "date": d.isoformat(),
                 "start_time": _clock(e.get("start_time")), "end_time": _clock(e.get("end_time")), "source": "manual"})

    # One that already ended today drops off now, not at midnight (Eastern - the market's zone).
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    today_iso, clock_now = now.date().isoformat(), now.strftime("%H:%M")
    live = [x for x in found.values() if not (x["date"] == today_iso and x.get("end_time") and x["end_time"] <= clock_now)]
    return sorted(live, key=lambda x: (x["date"], x.get("start_time") or ""))[:8]


def _same_person(a: dict, b: dict) -> bool:
    def norm(v):
        return re.sub(r"[^a-z0-9@.]", "", str(v or "").lower())
    for key in ("email", "phone"):
        if norm(a.get(key)) and norm(a.get(key)) == norm(b.get(key)):
            return True
    return bool(norm(a.get("name"))) and norm(a.get("name")) == norm(b.get("name"))


def merge_agents(default_agent: dict, draft_agents: list, list_agent_name: Optional[str]) -> list[dict]:
    """The presenting agent is always the account that published the page (see the comment in
    resolve_property_site); draft rows add co-agents. A draft row describing that same person -
    or the editor's old name-only prefill of the MLS list agent's name - refines the primary card
    instead of duplicating it, and never wipes the profile photo/contact it doesn't carry."""
    primary = dict(default_agent)
    co_agents: list[dict] = []
    for row in draft_agents or []:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            continue
        name_only = not any(str(row.get(k) or "").strip() for k in ("phone", "email", "brokerage", "license_number", "title"))
        if _same_person(row, primary) or (name_only and list_agent_name and _same_person(row, {"name": list_agent_name})):
            before = re.sub(r"\D", "", str(primary.get("phone") or ""))
            primary.update({k: v for k, v in row.items() if not _blank(v) and k != "name"})
            # A different phone typed for this page is the number to call AND text here.
            if not _blank(row.get("phone")) and re.sub(r"\D", "", str(row["phone"])) != before and _blank(row.get("text_phone")):
                primary["text_phone"] = row["phone"]
            continue
        co_agents.append({k: v for k, v in row.items() if isinstance(v, (str, type(None)))})
    return [primary, *co_agents[:3]]
