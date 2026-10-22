import hashlib
import json
import re
import os
import secrets
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict, Optional

from app.database import get_db
from app.models.database import AgentForm, AgentHub, EmailSignature, User
from app.services import email_signature_service, property_website_service
from app.services.website.site_service import owner_links
from app.utils.auth import EMAIL_SIGNATURE_ACCESS_TIERS, owner_has_access, require_email_signature_access

router = APIRouter(prefix="/email-signature", tags=["email-signature"])

# Same rationale/size as AGENT_HUB_CONFIG_MAX_BYTES (agent_hub_routes.py) - generous for a
# builder payload, small enough to block someone stuffing arbitrary data in.
EMAIL_SIGNATURE_CONFIG_MAX_BYTES = 150_000


class EmailSignatureConfigUpdate(BaseModel):
    config: Dict[str, Any]


async def _get_or_create_signature(db: AsyncSession, owner_id: str) -> EmailSignature:
    result = await db.execute(select(EmailSignature).where(EmailSignature.owner_id == owner_id))
    signature = result.scalar_one_or_none()
    if not signature:
        signature = EmailSignature(owner_id=owner_id, config={})
        db.add(signature)
        await db.commit()
        await db.refresh(signature)
    return signature


async def _get_agent_hub_config(db: AsyncSession, owner_id: str) -> dict:
    # Live listings + sales merged in (agent_hub_live), so the signature's featured listing / recent
    # sales stay current without the agent re-saving their Agent Hub.
    from app.services.agent_hub_live import live_hub_config
    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = result.scalar_one_or_none()
    if not hub:
        return {}
    return await live_hub_config(db, await db.get(User, owner_id), hub.config or {})


@router.get("/me")
async def get_my_email_signature(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_email_signature_access),
):
    signature = await _get_or_create_signature(db, current_user.id)
    # "My Brand Kit" (the default for a signature that was never styled): the brand look is
    # written into the config so every image endpoint renders it (brand_look_service).
    from app.services import brand_look_service
    look = await brand_look_service.get_look(db, current_user)
    if brand_look_service.signature_follows_brand(signature.config):
        nxt = brand_look_service.signature_config_with_brand(signature.config, look, current_user)
        if nxt != (signature.config or {}):
            signature.config = nxt
            await db.commit()
        return {"id": signature.id, "config": email_signature_service.resolve_signature_config(nxt), "brand_look": look}
    return {"id": signature.id, "config": email_signature_service.resolve_signature_config(signature.config), "brand_look": look}


@router.put("/me")
async def update_my_email_signature(
    payload: EmailSignatureConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_email_signature_access),
):
    serialized = json.dumps(payload.config)
    if len(serialized.encode("utf-8")) > EMAIL_SIGNATURE_CONFIG_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This signature is too large to save. Try removing a custom link or note.",
        )
    signature = await _get_or_create_signature(db, current_user.id)
    # The "last copied" fingerprints (see mark_email_signature_installed) are server-owned. The
    # editor round-trips whatever GET /me gave it at page load, which can be older than a Copy
    # made since - so always keep the stored values and never take them from the payload.
    next_config = {k: v for k, v in payload.config.items() if k not in _INSTALL_KEYS}
    for key in _INSTALL_KEYS:
        if key in (signature.config or {}):
            next_config[key] = signature.config[key]
    # Following the Brand Kit: the look comes from the Brand Kit, whatever the editor sent.
    from app.services import brand_look_service
    if next_config.get("lookSource") == "brand":
        next_config = brand_look_service.signature_config_with_brand(next_config, await brand_look_service.get_look(db, current_user), current_user)
    signature.config = next_config
    if not signature.public_token:
        signature.public_token = secrets.token_urlsafe(24)
    await db.commit()
    return {"config": email_signature_service.resolve_signature_config(signature.config)}


# Written only by POST /me/installed - see update_my_email_signature.
_INSTALL_KEYS = ("installedHtmlHash", "installedAssetsHash", "installedClient")

# Mail apps that save their own copy of the pictures when a signature is pasted (classic Outlook
# for Windows attaches them to every email by default; Apple Mail can too). Their pasted signature
# stops picking up new photos/listings/sales, so for them an image change also means "re-copy".
_EMBEDDING_CLIENTS = {"outlook_desktop", "apple"}
_INSTALL_CLIENTS = {"gmail", "outlook", "outlook_desktop", "apple"}

# Any renderer change (a deploy) invalidates every ETag below on its own.
_RENDERER_VERSION = hashlib.sha256(Path(email_signature_service.__file__).read_bytes()).hexdigest()[:8]


def _fingerprint(*parts: Any) -> str:
    raw = json.dumps([_RENDERER_VERSION, *parts], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _assets_fingerprint(resolved: dict, hub_config: dict, owner: User) -> str:
    """What the live images SHOW (photo, banner content, stats) - not today's rotation pick, so a
    daily listing rotation alone never counts as a change."""
    banner = resolved["banner"]
    return _fingerprint(
        "assets",
        hub_config.get("profilePhotoUrl") or owner.photo_url or "",
        hub_config.get("profilePhotoPosition"),
        _agent_name(owner, hub_config),
        email_signature_service.featured_pool(hub_config, banner.get("showFeaturedListing", True), banner.get("showRecentSales", True)),
        email_signature_service.compute_sales_stats(hub_config) if email_signature_service.wants_stats(resolved) else None,
    )


def _is_public_base(url: str) -> bool:
    host = (url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]).lower()
    private = host in ("localhost", "0.0.0.0") or host.endswith(".local") or re.match(r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host)
    return url.startswith("https://") and not private


def _https_outside_dev(url: str) -> str:
    # Behind the production proxy the request can arrive as plain http://; public links must be https.
    if os.getenv("DEV", "false") != "true" and url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _api_base(request: Request) -> str:
    # Prefer the configured public API URL; fall back to the request's own host (keeps dev
    # working even when the backend port has moved) - same idiom as the .ics calendar feed URL.
    return (os.getenv("API_URL") or _https_outside_dev(str(request.base_url))).rstrip("/")


@router.get("/me/html")
async def get_my_email_signature_html(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_email_signature_access),
):
    """The actual install HTML for Gmail/Outlook/Apple Mail's own signature settings - same
    renderer the public asset endpoints and (indirectly, via the same source data) the Studio
    live preview are built from."""
    signature = await _get_or_create_signature(db, current_user.id)
    if not signature.public_token:
        signature.public_token = secrets.token_urlsafe(24)
        await db.commit()

    hub_config = await _get_agent_hub_config(db, current_user.id)

    asset_base_url = f"{_api_base(request)}/email-signature/public/{signature.public_token}"
    # The Agent Hub link and any form-linked buttons route through this stable, owner-scoped
    # redirect instead of a resolved domain - see stable_agent_hub_redirect/stable_form_redirect
    # below. That's what lets an install-once signature keep working through a slug rename, a
    # domain change, or a tier upgrade/downgrade without the agent ever touching Gmail/Outlook
    # again: the href baked into their signature never changes, only where it points does, and
    # that's resolved fresh on every click, not frozen at copy time.
    redirect_base_url = f"{_api_base(request)}/email-signature/go/{current_user.id}"
    # Every link in the HTML is a stable redirect now (hub, forms, vCard, socials, and the strip's
    # /featured link), so the HTML itself only changes when something that genuinely can't update
    # on its own changes - text, colors, which icons/buttons/blocks show. That makes a fingerprint
    # of it an exact "does the agent need to re-copy?" signal (see mark_email_signature_installed).
    html = email_signature_service.build_signature_html(signature.config, hub_config, current_user, redirect_base_url, asset_base_url)
    html_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()[:20]
    stored = signature.config or {}
    installed_hash = stored.get("installedHtmlHash")
    assets_hash = _assets_fingerprint(email_signature_service.resolve_signature_config(signature.config), hub_config, current_user)
    installed_client = stored.get("installedClient")
    # "edits": text/colors/layout changed - true for every mail app.
    # "images": only for apps that keep their own copy of the pictures (see _EMBEDDING_CLIENTS);
    # Gmail/Outlook.com/new Outlook re-fetch them on every open, so they never need this one.
    recopy_reason = None
    if installed_hash and installed_hash != html_hash:
        recopy_reason = "edits"
    elif installed_client in _EMBEDDING_CLIENTS and stored.get("installedAssetsHash") not in (None, assets_hash):
        recopy_reason = "images"
    return {
        "html": html,
        "asset_base_url": asset_base_url,
        "html_hash": html_hash,
        "assets_hash": assets_hash,
        "installed": bool(installed_hash),
        "installed_client": installed_client,
        "needs_recopy": recopy_reason is not None,
        "recopy_reason": recopy_reason,
        # False when the links/images point at a dev machine (localhost / plain http) - Gmail's image
        # proxy and every recipient's phone can't reach those, so a sent copy shows broken images.
        "public_ready": _is_public_base(asset_base_url),
    }


class EmailSignatureInstalled(BaseModel):
    html_hash: str
    assets_hash: Optional[str] = None
    client: Optional[str] = None


@router.post("/me/installed")
async def mark_email_signature_installed(
    payload: EmailSignatureInstalled,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_email_signature_access),
):
    """Records the fingerprint of the HTML the agent just copied. Photo, sales, stats, listing and
    every link update themselves in an installed signature - but text, colors and which pieces
    show are baked into the pasted HTML, so when the current HTML's fingerprint drifts from this
    one the Studio can say, truthfully, "re-copy to apply these edits"."""
    signature = await _get_or_create_signature(db, current_user.id)
    installed = {"installedHtmlHash": payload.html_hash[:64]}
    if payload.assets_hash:
        installed["installedAssetsHash"] = payload.assets_hash[:64]
    if payload.client in _INSTALL_CLIENTS:
        installed["installedClient"] = payload.client
    signature.config = {**(signature.config or {}), **installed}
    await db.commit()
    return {"ok": True}


async def _resolve_featured_listing_link(db: AsyncSession, owner: User, signature_config: dict, hub_config: dict) -> Optional[str]:
    """Whenever the banner is currently showing a featured LISTING (not a recent sale), work out
    its "smart QR" destination up front - the agent's own Listing Spotlight for that exact
    property if they've published one, else the generic one-page property view. Re-runs the same
    deterministic pick_featured_items() the renderer itself uses, so this always agrees with
    what's actually on screen. A no-op (returns None immediately) whenever a sale is showing."""
    resolved_cfg = email_signature_service.resolve_signature_config(signature_config)
    if not resolved_cfg["blockVisibility"].get("banner"):
        return None
    banner_cfg = resolved_cfg["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if not featured or featured[0] != "listing":
        return None
    item = featured[1][0]
    listing_key = item.get("id") if item.get("source") != "manual" else None
    fallback_address = item.get("address") if item.get("source") == "manual" else None
    overrides = hub_config.get("listingLinkOverrides") or {}
    override_key = property_website_service.listing_override_key(listing_key, fallback_address)
    skip_spotlight = overrides.get(override_key) == "property_page"
    return await property_website_service.resolve_listing_link(db, owner, listing_key, fallback_address, skip_spotlight=skip_spotlight)


@router.get("/go/{owner_id}/hub")
async def stable_agent_hub_redirect(owner_id: str, db: AsyncSession = Depends(get_db)):
    """The permanent link an installed signature's "My Agent Hub" button points to - looked up
    fresh on every click (current domain, current slug), not baked once at install time."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return RedirectResponse((await owner_links(db, owner, hub.slug)).hub())


@router.get("/go/{owner_id}/sales")
async def stable_sales_redirect(owner_id: str, db: AsyncSession = Depends(get_db)):
    """The permanent link the signature's recent-sale banner is wrapped in - the agent's public
    Recent Sales page, same stable-redirect guarantee as the hub/form links (survives a slug
    rename or domain change without the installed signature ever needing to be re-pasted)."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return RedirectResponse((await owner_links(db, owner, hub.slug)).sales())


@router.get("/go/{owner_id}/listings")
async def stable_listings_redirect(owner_id: str, db: AsyncSession = Depends(get_db)):
    """Kept for signatures installed while the hub still had a separate Listings page - current
    listings now lead the Sales page (/{slug}/sales), so this lands there too."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return RedirectResponse((await owner_links(db, owner, hub.slug)).sales())


@router.get("/go/{owner_id}/featured")
async def stable_featured_redirect(owner_id: str, db: AsyncSession = Depends(get_db)):
    """The one permanent link on the signature's Recent Sales / Featured Listing strip. Decides at
    click time what the strip is showing right now - a featured listing goes to that listing's
    Spotlight (or plain property page, per the agent's link settings), recent sales go to the Sales
    page - so the link always matches the live image even after a listing sells."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    links = await owner_links(db, owner, hub.slug)
    sig_result = await db.execute(select(EmailSignature).where(EmailSignature.owner_id == owner_id))
    signature = sig_result.scalar_one_or_none()
    from app.services.agent_hub_live import live_hub_config
    hub_config = await live_hub_config(db, owner, hub.config or {})
    resolved_cfg = email_signature_service.resolve_signature_config(signature.config if signature else {})
    banner_cfg = resolved_cfg["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if featured and featured[0] == "listing":
        link = await _resolve_featured_listing_link(db, owner, signature.config if signature else {}, hub_config)
        # Tagged at click time (never baked into the pasted signature), so a message sent from that
        # page says it came from the email signature.
        from app.services.visit_source import with_src
        return RedirectResponse(with_src(link, "email") if link else links.sales())
    if featured:
        return RedirectResponse(links.sales())
    return RedirectResponse(links.hub())


@router.get("/go/{owner_id}/social/{network}")
async def stable_social_redirect(owner_id: str, network: str, db: AsyncSession = Depends(get_db)):
    """A signature social/website icon's permanent link - the agent's CURRENT URL for that network,
    looked up on every click, so editing it in Settings fixes an installed signature with no
    re-paste. Falls back to the Agent Hub if that network's link has since been removed."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    from app.services.website_link_service import apply_auto_website
    config = await apply_auto_website(db, owner, hub.config or {}, hub.slug)  # Website: IDX site / own domain / hub
    url = email_signature_service._social_url(config.get("socialLinks") or [], network)
    if not url:
        url = (await owner_links(db, owner, hub.slug)).hub()
    return RedirectResponse(url)


@router.get("/go/{owner_id}/vcard")
async def stable_vcard_redirect(owner_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The permanent link an installed signature's "Save Contact" badge points to - redirects to
    the same live-generated .vcf the Agent Hub's own Save Contact button uses (agent_hub_routes.py
    get_public_agent_hub_vcard), so it always reflects whatever the agent's Contact Card settings
    say right now. Same stable-redirect guarantee as the hub/form links above: the agent never has
    to re-paste their signature just because they updated a phone number or photo in Settings."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return RedirectResponse(f"{_api_base(request)}/agent-hub/public/{hub.slug}/vcard")


@router.get("/go/{owner_id}/form/{form_type}")
async def stable_form_redirect(owner_id: str, form_type: str, db: AsyncSession = Depends(get_db)):
    """Same permanence guarantee as stable_agent_hub_redirect, for a signature button linked to
    one of the agent's premade forms (how_can_i_help/home_value/showing/buying/selling) - the
    form's own `slug` is editable in Studio, so this re-resolves it live instead of baking
    whatever slug happened to be current the day the signature was installed."""
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == owner_id))
    hub = hub_result.scalar_one_or_none()
    owner = await db.get(User, owner_id) if hub else None
    if not hub or not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    form_result = await db.execute(select(AgentForm).where(AgentForm.owner_id == owner_id, AgentForm.form_type == form_type))
    form = form_result.scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return RedirectResponse((await owner_links(db, owner, hub.slug)).form(form.slug))


async def _load_public_signature(db: AsyncSession, token: str) -> tuple[EmailSignature, User, dict]:
    result = await db.execute(select(EmailSignature).where(EmailSignature.public_token == token))
    signature = result.scalar_one_or_none()
    if not signature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    owner = await db.get(User, signature.owner_id)
    if not owner or not owner_has_access(owner, EMAIL_SIGNATURE_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    hub_config = await _get_agent_hub_config(db, owner.id)
    return signature, owner, hub_config


def _agent_name(owner: User, hub_config: dict) -> str:
    profile = hub_config.get("profile") or {}
    return (profile.get("name") or "").strip() or f"{owner.first_name} {owner.last_name}".strip()


# Regenerated on every request (not cached to disk) so rotating/animated content genuinely
# changes for an already-installed static signature - see email_signature_service docstring. A
# short Cache-Control still lets mail clients avoid re-fetching on every single open.
_CACHE_HEADERS = {"Cache-Control": "public, max-age=3600"}

# The per-agent images an installed signature actually loads (photo, banner, header, stats) are
# served "store but always re-check": Gmail's image proxy honours no-cache, so a new photo or sale
# shows on the next open instead of up to an hour later. The ETag is a fingerprint of everything
# the image is drawn from, so a re-check with nothing changed is a 304 with no rendering at all,
# and a full re-fetch with nothing changed comes from this small in-memory cache.
_LIVE_HEADERS = {"Cache-Control": "no-cache, max-age=0, must-revalidate"}
_RENDERED: "OrderedDict[str, bytes]" = OrderedDict()
_RENDERED_MAX = 256


async def _live_image(request: Request, etag: str, media_type: str, render) -> Response:
    quoted = f'"{etag}"'
    headers = {**_LIVE_HEADERS, "ETag": quoted}
    if quoted in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    body = _RENDERED.get(etag)
    if body is None:
        body = await render()
        _RENDERED[etag] = body
        while len(_RENDERED) > _RENDERED_MAX:
            _RENDERED.popitem(last=False)
    else:
        _RENDERED.move_to_end(etag)
    return Response(content=body, media_type=media_type, headers=headers)


@router.get("/public/{token}/listing.jpg")
async def get_signature_listing_jpeg(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    banner_cfg = resolved["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if not featured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No featured listing yet.")
    mode, items = featured
    dark = bool(resolved.get("darkMode"))
    accent = resolved["accentColor"]
    luxe = bool(resolved.get("luxe"))

    # 2 sales = the featured strip flipping between them: a GIF on this same URL (the installed
    # HTML stays identical for a listing, one sale or two - see render_sale_feature).
    media_type = "image/gif" if (mode == "sale_feature" and len(items) > 1) else "image/jpeg"

    async def render():
        if mode == "listing":
            return await email_signature_service.render_listing_bar_jpeg(items[0], accent, dark=dark, luxe=luxe)
        if mode == "sale_feature":
            return (await email_signature_service.render_sale_feature(items, accent, dark=dark, luxe=luxe))[0]
        return await email_signature_service.render_recent_sales_carousel_jpeg(items, accent, dark=dark)
    return await _live_image(request, _fingerprint("listing.jpg", mode, items, accent, dark, luxe, "v4"), media_type, render)


@router.get("/public/{token}/listing.webp")
async def get_signature_listing_webp(token: str, db: AsyncSession = Depends(get_db)):
    """WebP sibling of listing.jpg - see the <picture> wrapper in _listing_bar_html. Featured-
    listing mode gets a static (lossy-quality, smaller-than-JPEG) WebP; recent-sales mode gets
    the real ANIMATED WebP (render_recent_sales_carousel_webp) - the one place this format
    actually carries motion, not just a smaller still image."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    banner_cfg = resolved["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if not featured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No featured listing yet.")
    mode, items = featured
    dark = bool(resolved.get("darkMode"))
    if mode in ("listing", "sale_feature"):
        webp_bytes = await email_signature_service.render_listing_bar_webp(items[0], resolved["accentColor"], dark=dark, luxe=bool(resolved.get("luxe")))
    else:
        webp_bytes = await email_signature_service.render_recent_sales_carousel_webp(items, resolved["accentColor"], dark=dark)
    return Response(content=webp_bytes, media_type="image/webp", headers=_CACHE_HEADERS)


@router.get("/public/{token}/stats.png")
async def get_signature_stats_png(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Live-fetched counterpart to the Stats column's numbers/pills - see render_stats_png for why
    this used to be the one part of the card that never updated (numbers baked in at copy time, not
    just colors) even though it's the most likely thing to go stale. PNG (not JPEG) so the pills can
    sit transparently on the column's own gradient wash - see render_stats_png's docstring."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    stats = email_signature_service.compute_sales_stats(hub_config) if email_signature_service.wants_stats(resolved) else None
    if not stats:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No stats yet.")
    # Same target height _build_gallery's icon rail spaces itself against (see
    # side_column_content_height) - computed independently here since this route never sees
    # _build_gallery's own locals, but from the exact same inputs, so the two always agree.
    target_h = email_signature_service.side_column_content_height(resolved, hub_config, owner)
    accent, dark = resolved["accentColor"], bool(resolved.get("darkMode"))

    async def render():
        return await email_signature_service.render_stats_png(stats, accent, dark_theme=dark, target_h=target_h)
    return await _live_image(request, _fingerprint("stats.png", stats, accent, dark, target_h), "image/png", render)


@router.get("/public/{token}/stats-strip.png")
async def get_signature_stats_strip_png(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The stats as a slim strip - shown instead of the stats column when the listings & sales
    strip is off (see email_signature_service.stats_as_strip). Live, like every other image here."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    stats = email_signature_service.compute_sales_stats(hub_config) if email_signature_service.wants_stats(resolved) else None
    if not stats:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No stats yet.")
    accent, dark = resolved["accentColor"], bool(resolved.get("darkMode"))

    async def render():
        return await email_signature_service.render_stats_strip_png(stats, accent, dark_theme=dark)
    return await _live_image(request, _fingerprint("stats-strip.png", stats, accent, dark), "image/png", render)


@router.get("/public/{token}/stats.webp")
async def get_signature_stats_webp(token: str, db: AsyncSession = Depends(get_db)):
    """Lossless WebP sibling of stats.png - see the <picture> wrapper in _stats_img_html."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    stats = email_signature_service.compute_sales_stats(hub_config) if email_signature_service.wants_stats(resolved) else None
    if not stats:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No stats yet.")
    target_h = email_signature_service.side_column_content_height(resolved, hub_config, owner)
    webp_bytes = await email_signature_service.render_stats_webp(stats, resolved["accentColor"], dark_theme=bool(resolved.get("darkMode")), target_h=target_h)
    return Response(content=webp_bytes, media_type="image/webp", headers=_CACHE_HEADERS)


@router.get("/public/{token}/sales-header.png")
async def get_signature_sales_header_png(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The "RECENT SALES" label as its own small lossless PNG - see render_recent_sales_header_png
    for why this had to split out of the shared listing.gif/jpg image. Only meaningful in recent-
    sales mode (the featured-listing strip has no such label at all)."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    banner_cfg = resolved["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if not featured or featured[0] != "sale":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    dark, accent = bool(resolved.get("darkMode")), resolved["accentColor"]

    from app.services.sales_wording import sales_are_recent
    recent = sales_are_recent((email_signature_service.compute_sales_stats(hub_config) or {}).get("totalVolume"))

    async def render():
        return email_signature_service.render_recent_sales_header_png(accent, dark=dark, recent=recent)
    return await _live_image(request, _fingerprint("sales-header.png", accent, dark, recent), "image/png", render)


@router.get("/public/{token}/sales-header.webp")
async def get_signature_sales_header_webp(token: str, db: AsyncSession = Depends(get_db)):
    """Lossless WebP sibling of sales-header.png - see the <picture> wrapper in _listing_bar_html."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    banner_cfg = resolved["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if not featured or featured[0] != "sale":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    dark = bool(resolved.get("darkMode"))
    from app.services.sales_wording import sales_are_recent
    recent = sales_are_recent((email_signature_service.compute_sales_stats(hub_config) or {}).get("totalVolume"))
    webp_bytes = email_signature_service.render_recent_sales_header_webp(resolved["accentColor"], dark=dark, recent=recent)
    return Response(content=webp_bytes, media_type="image/webp", headers=_CACHE_HEADERS)


@router.get("/public/{token}/listing.gif")
async def get_signature_listing_gif(token: str, db: AsyncSession = Depends(get_db)):
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    banner_cfg = resolved["banner"]
    featured = email_signature_service.pick_featured_items(hub_config, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))
    if not featured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No featured listing yet.")
    mode, items = featured
    dark = bool(resolved.get("darkMode"))
    if mode == "sale_feature" and len(items) > 1:
        gif_bytes = (await email_signature_service.render_sale_feature(items, resolved["accentColor"], dark=dark, luxe=bool(resolved.get("luxe"))))[0]
    elif mode in ("listing", "sale_feature"):
        gif_bytes = await email_signature_service.render_listing_bar_gif(items[0], resolved["accentColor"], dark=dark, luxe=bool(resolved.get("luxe")))
    else:
        gif_bytes = await email_signature_service.render_recent_sales_carousel_gif(items, resolved["accentColor"], dark=dark)
    return Response(content=gif_bytes, media_type="image/gif", headers=_CACHE_HEADERS)


@router.get("/public/{token}/photo.gif")
async def get_signature_photo_gif(token: str, db: AsyncSession = Depends(get_db)):
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    headshot = hub_config.get("profilePhotoUrl") or owner.photo_url or ""
    card_bg_hex = email_signature_service.theme_card_bg(bool(resolved.get("darkMode")))
    # Same focal point Agent Hub's own photo cropper saved (Settings) - without this the signature
    # always center-crops the source photo, which can cut off the top of the agent's head whenever
    # they've deliberately positioned the photo off-center there (e.g. a wider/half-body shot).
    photo_position = hub_config.get("profilePhotoPosition")
    gif_bytes = await email_signature_service.render_photo_gif(headshot or None, _agent_name(owner, hub_config), resolved["accentColor"], card_bg_hex, photo_position=photo_position)
    return Response(content=gif_bytes, media_type="image/gif", headers=_CACHE_HEADERS)


@router.get("/public/{token}/social/{network}.gif")
async def get_signature_social_badge_gif(token: str, network: str, bg: str = "FFFFFF", db: AsyncSession = Depends(get_db)):
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    if network not in ("instagram", "facebook", "youtube", "tiktok", "website"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    card_bg_hex = f"#{bg}" if email_signature_service.is_hex_color(bg) else "#FFFFFF"
    gif_bytes = email_signature_service.render_badge_gif(network, resolved["accentColor"], card_bg_hex)
    return Response(content=gif_bytes, media_type="image/gif", headers=_CACHE_HEADERS)


@router.get("/public/{token}/photo.jpg")
async def get_signature_photo_jpeg(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The static, retina-resolution counterpart to photo.gif - see render_photo_jpeg. This is what
    the actual install HTML links to now."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    headshot = hub_config.get("profilePhotoUrl") or owner.photo_url or ""
    card_bg_hex = email_signature_service.theme_card_bg(bool(resolved.get("darkMode")))
    photo_position = hub_config.get("profilePhotoPosition")
    name, accent = _agent_name(owner, hub_config), resolved["accentColor"]

    async def render():
        return await email_signature_service.render_photo_jpeg(headshot or None, name, accent, card_bg_hex, photo_position=photo_position)
    return await _live_image(request, _fingerprint("photo.jpg", headshot, name, accent, card_bg_hex, photo_position), "image/jpeg", render)


@router.get("/public/{token}/photo.webp")
async def get_signature_photo_webp(token: str, db: AsyncSession = Depends(get_db)):
    """WebP sibling of photo.jpg - see the <picture> wrapper in _build_gallery."""
    signature, owner, hub_config = await _load_public_signature(db, token)
    resolved = email_signature_service.resolve_signature_config(signature.config)
    headshot = hub_config.get("profilePhotoUrl") or owner.photo_url or ""
    card_bg_hex = email_signature_service.theme_card_bg(bool(resolved.get("darkMode")))
    photo_position = hub_config.get("profilePhotoPosition")
    webp_bytes = await email_signature_service.render_photo_webp(headshot or None, _agent_name(owner, hub_config), resolved["accentColor"], card_bg_hex, photo_position=photo_position)
    return Response(content=webp_bytes, media_type="image/webp", headers=_CACHE_HEADERS)


@router.get("/public/{token}/social/{network}.png")
async def get_signature_social_badge_png(network: str, size: int = email_signature_service._BADGE_SIZE):
    """The static, real-alpha counterpart to social/{network}.gif - see render_badge_png. This is
    what the actual install HTML links to now. No token/DB lookup needed - unlike the GIF version,
    this doesn't depend on the agent's accent color or a `?bg=` background to flatten against.

    `size` (explicit ask: bigger icons when the agent has fewer than 5 networks filled in, see
    _rail_icons_html) - clamped to the same _BADGE_SIZE.._BADGE_SIZE_MAX range that function ever
    actually requests, regardless of what a client sends, so this stays the same small, bounded
    render_badge_png cache key space no matter what's in the URL."""
    if network not in ("instagram", "facebook", "youtube", "tiktok", "website"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    size = max(email_signature_service._BADGE_SIZE, min(size, email_signature_service._BADGE_SIZE_MAX))
    png_bytes = email_signature_service.render_badge_png(network, size)
    return Response(content=png_bytes, media_type="image/png", headers=_CACHE_HEADERS)
