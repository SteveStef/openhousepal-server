"""AI Media from the chat (owner, 11/05: "You should be able to do everything in the chat, even dragging
and dropping a photo in"). The card is Media Lab's own new-project page as steps: the tool, the photo
(dropped in, one of the listing's, or one they uploaded before), and that tool's choices. It runs the
same POST /studio/media-lab/generate as the page, on the same AI budget, and carries on after they
leave: studio_media_service tells ai_notify when it's finished.

Photos a person drops in are Media Lab uploads (kind="upload"), so a photo can also be used anywhere
else the chat takes one: an invite cover, a blog cover, a logo, a headshot.
"""
import uuid
from typing import Optional

from sqlalchemy import select

from app.models.database import Property, StudioMediaAsset
from app.services.ai.ai_actions import ActionError, Prepared
from app.services.ai.ai_actions_catalog import _BACKGROUND, _obj, _run_route, _s, _simple  # noqa: F401
from app.services.email_service import tidy_caps

MEDIA_PLANS = {"IDX", "PRO_NO_MLS"}
TOOLS = ("twilight", "lights_on", "virtual_staging", "enhance", "video", "cinematic")
CINEMATIC_MAX = 40  # photos it may choose from (the editor picks the video's own by room and length)
ROOMS = ("living_room", "family_room", "primary_bedroom", "bedroom", "nursery", "kitchen", "dining_room", "office", "basement",
         "bonus_room", "home_gym", "sunroom", "entryway", "patio")
STYLES = ("modern", "transitional", "luxury", "traditional")
ENHANCE = ("grass", "sky", "brighten_exterior", "lighting", "polish", "yard_cleanup", "declutter", "remove_furniture")
TAGS = ("off", "bottom-middle", "bottom-right")


async def _p_media(db, user, a):
    import os
    has_mls = user.plan_tier in ("BASIC", "PREMIUM", "IDX")
    if has_mls and not user.broker_authorized and user.email != os.getenv("ADMIN_EMAIL", "admin@openhousepal.com"):
        raise ActionError("AI Media opens once your broker has approved you.")
    listing = None
    key = _s(a.get("listing_key"))
    if key and has_mls:
        p = (await db.execute(select(Property).where(Property.listing_key == key))).scalars().first()
        if p is not None:
            photos = [x for x in (p.photos or []) if isinstance(x, str)][:40] if isinstance(p.photos, list) else []
            listing = {"listing_key": key, "address": tidy_caps(p.street_address) or "", "city": tidy_caps(p.city) or "", "state": p.state, "zipcode": p.zipcode,
                       "price": p.price, "beds": p.bedrooms, "baths": p.bathrooms, "sqft": p.living_area, "photos": photos}
    ups = (await db.execute(select(StudioMediaAsset.result_url).where(StudioMediaAsset.owner_id == user.id, StudioMediaAsset.kind == "upload", StudioMediaAsset.result_url.isnot(None))
                            .order_by(StudioMediaAsset.created_at.desc()).limit(12))).scalars().all()
    try:
        usage = await _run_route(user, "GET", "/studio/media-lab/usage") or {}
    except ActionError:
        usage = {}
    tool = a.get("tool") if a.get("tool") in TOOLS else None
    photo = _s(a.get("photo_url"))
    uploads = list(dict.fromkeys(([photo] if photo else []) + list(ups)))
    payload = {"tool": tool, "photo": photo or None, "uploads": uploads, "listing": listing, "has_mls": has_mls,
               "room_type": a.get("room_type") if a.get("room_type") in ROOMS else None, "style": a.get("style") if a.get("style") in STYLES else None,
               "enhancements": [e for e in (a.get("enhancements") or []) if e in ENHANCE], "video_style": "day_to_twilight" if a.get("video_style") == "day_to_twilight" else "standard",
               "usage": {k: usage.get(k) for k in ("image_est_usd", "video_est_usd", "month_ai_budget_usd", "month_ai_percent")}}
    return Prepared("Make AI media", ["Pick the tool and the photo - it comes out in your Media Lab and your original stays as it is"], payload,
                    warning="This uses your monthly AI allowance.", confirm_label="Make it", picker="media")


def _adjust_media(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    tool = picks.get("tool") if picks.get("tool") in TOOLS else payload.get("tool")
    if not tool:
        raise ActionError("Pick what to make first.")
    listing = payload.get("listing") or {}
    listed = set(listing.get("photos") or [])
    allowed = listed | set(payload.get("uploads") or []) | {u for u in (picks.get("added") or []) if isinstance(u, str)}
    if tool == "cinematic":
        # Cinematic Video: a listing hands over all its photos; uploads are picked on the card. The
        # length, the rooms, the look and making it happen in its editor (right in the chat).
        listing_photos = [u for u in (listing.get("photos") or []) if isinstance(u, str)]
        if len(listing_photos) >= 2:
            photos = listing_photos[:CINEMATIC_MAX]
        else:
            photos = [u for u in (picks.get("photos") or []) if u in allowed][:CINEMATIC_MAX]
        if len(photos) < 2:
            raise ActionError("Pick at least 2 photos first.")
        out.update(tool=tool, photos=photos)
        return out
    if tool == "video":
        photos = [u for u in (picks.get("photos") or []) if u in allowed][:7]
        if not photos:
            raise ActionError("Pick at least one photo first.")
        style = "day_to_twilight" if picks.get("video_style") == "day_to_twilight" else "standard"
        if style == "day_to_twilight" and len(photos) != 1:
            raise ActionError("Day to twilight works from exactly one photo.")
        out.update(tool=tool, photos=photos, video_style=style)
        return out
    effects = [e for e in (picks.get("effects") or [tool]) if e in ("twilight", "lights_on", "virtual_staging", "enhance")]
    if not effects:
        raise ActionError("Pick what to do to the photo first.")
    photo = picks.get("photo")
    if photo not in allowed:
        raise ActionError("Pick a photo first.")
    options: dict = {}
    if "virtual_staging" in effects:
        if picks.get("room_type") not in ROOMS or picks.get("style") not in STYLES:
            raise ActionError("Pick the room and the style first.")
        options.update(room_type=picks["room_type"], style=picks["style"])
        if picks.get("staging_tag") in TAGS:
            options["staging_tag"] = picks["staging_tag"]
    if "enhance" in effects:
        chosen = [e for e in (picks.get("enhancements") or []) if e in ENHANCE]
        if not chosen:
            raise ActionError("Pick at least one fix first.")
        options["enhancements"] = chosen
    out.update(tool=tool, photo=photo, effects=effects, options=options, source_kind="listing" if photo in listed else "upload")
    return out


def _linked(p: dict) -> Optional[dict]:
    home = p.get("listing")
    if not home:
        return None
    return {k: home[k] for k in ("listing_key", "address", "city", "state", "zipcode", "price", "beds", "baths", "sqft") if home.get(k) is not None}


async def _r_cinematic(user, p: dict, linked: Optional[dict]) -> dict:
    """Cinematic Video from the chat: makes the project (a draft, same as the page's Continue) with the
    picked photos, starts the AI looking at them, and hands over to its editor - checking each room's
    label, the look, transitions and making the video happen there (too much for a card)."""
    listed = set((p.get("listing") or {}).get("photos") or [])
    photos = p.get("photos") or []
    from_listing = bool(linked) and bool(photos) and photos[0] in listed
    agent = {"name": f"{user.first_name or ''} {user.last_name or ''}".strip(), "brokerage": user.brokerage or "",
             "photo": user.photo_url if (user.photo_url or "").startswith(("http://", "https://")) else None}
    body = {"photos": photos, "source_kind": "listing" if from_listing else "upload", "agent": agent,
            "client_request_id": str(uuid.uuid4())}
    if from_listing:
        body["linked_property"] = linked
    out = await _run_route(user, "POST", "/studio/media-lab/motion", body)
    aid = (out or {}).get("id")
    if not aid:
        return {"message": "Your Cinematic Video is waiting in Media Lab.", "link": "/studio/media"}
    try:
        await _run_route(user, "POST", f"/studio/media-lab/motion/{aid}/analyze")
    except ActionError:
        pass  # the editor starts it itself when it opens
    return {"message": "Your Cinematic Video is started - I'm sorting the photos into rooms now. Pick the length and shape "
                       "below, tap the rooms to show, check each photo and make it - right here. It's saved as you go.",
            "link": f"/studio/media/motion/{aid}"}


async def _r_media(db, user, p):
    from app.services.ai import ai_notify
    from app.services.ai.ai_actions import CARD
    video = p.get("tool") == "video"
    linked = _linked(p)
    if p.get("tool") == "cinematic":
        return await _r_cinematic(user, p, linked)
    if video:
        first = (p.get("photos") or [None])[0]
        listed = set((p.get("listing") or {}).get("photos") or [])
        body = {"action": "animate", "source_photo_url": first, "source_kind": "listing" if first in listed else "upload", "photos": p["photos"],
                "options": {"video_style": p.get("video_style") or "standard"}}
    else:
        body = {"action": "stack", "effects": p["effects"], "source_photo_url": p["photo"], "source_kind": p["source_kind"], "options": p.get("options") or {}}
    if linked:
        body["linked_property"] = linked
    body["client_request_id"] = str(uuid.uuid4())
    out = await _run_route(user, "POST", "/studio/media-lab/generate", body)
    aid = (out or {}).get("id")
    if not aid:
        return {"message": "Started - it'll be in your Media Lab.", "link": "/studio/media"}
    label = "your video" if video else "your photo"
    key = f"media:{aid}"
    await ai_notify.watch(db, user, key, CARD.get() or {}, label)
    # A quick edit could already be done by the time the watch is on.
    status = (await db.execute(select(StudioMediaAsset.status, StudioMediaAsset.result_url).where(StudioMediaAsset.id == aid).execution_options(populate_existing=True))).first()
    if status and status.status != "processing":
        await ai_notify.finish_media(user.id, aid)
    return {"working": True, "message": ("Making your video now - it usually takes a few minutes." if video else "Working on your photo now - usually under a minute.")
            + " I'll tell you right here when it's ready, even if you close the site."}


_simple("make_ai_media", "Make AI media from a photo, the Media Lab's tools: twilight (day to dusk), lights_on, virtual_staging (needs a room and a style), enhance (grass, sky, brighten_exterior, lighting, polish, yard_cleanup, "
        "declutter, remove_furniture), an AI video (1 to 7 landscape photos, standard or day_to_twilight) or a Cinematic Video (tool=cinematic: uses every photo of the named listing, or 2+ uploads picked on the card; then its editor opens right in the chat to pick the length and shape, tap the rooms to show, check each photo, the look and make it - no AI video model, well under 1% of their AI). Set it up right away: the card asks the tool, the photo and that tool's choices with a preview. "
        "Pass photo_url when they attached a photo (from the [Photo attached: url] note), listing_key when they named one of their homes, and room_type or style if you can see or were told them. Runs in the background; never ask them to wait. IDX and Pro plans.",
        _obj({"tool": {"type": "string", "enum": list(TOOLS)}, "photo_url": {"type": "string"}, "listing_key": {"type": "string"},
              "room_type": {"type": "string", "enum": list(ROOMS)}, "style": {"type": "string", "enum": list(STYLES)},
              "enhancements": {"type": "array", "items": {"type": "string", "enum": list(ENHANCE)}}, "video_style": {"type": "string", "enum": ["standard", "day_to_twilight"]}}),
        _p_media, _r_media, MEDIA_PLANS, adjust=_adjust_media)
