"""Read-only adapters that turn existing OHP data into website-ready shapes.

This is the seam described in the Website Builder Phase 2 spec: the website must not own a
second copy of an agent's profile, listings, sales, open houses, blog posts, or showcases - it
asks these systems for the current data and presents it. Every function here is read-only
(never mutates Agent Hub, Forms, Blogs, etc.) and safe to call on every public site request -
bounded/limited queries only, no full-table loads (see caller in public_site_routes.py, which
uses these to hydrate dynamic blocks with an TTL cache wrapped around the result).

Deliberately does NOT touch Bright MLS - every query here reads only the local `properties`
table (already synced by the existing Bright ingestion job), matching the spec's "no direct
Bright calls from the website" rule. This also means it does NOT reuse
`agent_hub_routes.get_my_recent_sales` as-is, since that function calls Bright live as a
fallback (`fetch_bright_agent_sales`) - only its pure local-matching helpers
(`clean_sale_status`/`is_eligible_recent_sale`/`apply_recent_sale_defaults`) are imported.
"""
import asyncio
import copy
import logging
import math
import re
import time
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agent_hub_routes import apply_recent_sale_defaults, is_eligible_recent_sale
from app.models.database import (
    AgentForm,
    AgentHub,
    AgentMarketArea,
    Blog,
    Collection,
    MarketArea,
    OpenHouseEvent,
    Property,
    StudioBrandKit,
    User,
)
from app.schemas.collection_preferences import CollectionPreferencesBase
from app.services.areas import geography, market_stats_service
from app.services.open_house_manager_service import INACTIVE_STATUSES

logger = logging.getLogger(__name__)
from app.services.mls_open_houses import open_houses_payload
from app.services.property_service import PropertyService
from app.services.website import search_query_parser
from app.services.email_service import tidy_caps  # MLS ALL-CAPS -> "583 Lakeside Rd #205"
from app.utils.listing_dom import property_days_on_market

_HERO_PROPERTY_TYPE_FLAGS = {
    "single_family": "is_single_family",
    "town_house": "is_town_house",
    "condo": "is_condo",
    "multi_family": "is_multi_family",
}


# ---------------------------------------------------------------- Bright MLS IDX display rules
# A public website may only show what the seller allowed on the internet:
#   InternetEntireListingDisplayYN = false -> the seller opted the whole listing out of internet
#     display: it must not appear anywhere on the site (lists, maps, detail page, hero photos).
#   InternetAddressDisplayYN = false -> the home can be shown, but not its street address (or a
#     map pin, which gives the address away).
# And listings that aren't on the market or sold (withdrawn, expired, canceled, off market) are
# not IDX content at all. Flags missing from a record (older rows synced before the flags were
# requested) count as allowed, the MLS default.
_IDX_NOT_SHOWN_STATUSES = (INACTIVE_STATUSES - {"CLOSED-BRIGHT"}) | {"TEMP OFF MARKET-BRIGHT"}
ADDRESS_WITHHELD = "Address available on request"
_ADDRESS_REPORT_KEY_RE = re.compile(r"address|street|latitude|longitude|directions|unit", re.I)


def internet_display_ok():
    """SQL condition: the seller did not opt this listing out of internet display."""
    return func.coalesce(Property.raw_mls_data["InternetEntireListingDisplayYN"].astext, "true") != "false"


def _withhold_address(card: dict) -> dict:
    card["address"] = ADDRESS_WITHHELD
    card["address_withheld"] = True
    for key in ("full_address", "street_address", "latitude", "longitude"):
        if key in card:
            card[key] = None
    if isinstance(card.get("report"), dict):
        card["report"] = {k: v for k, v in card["report"].items() if not _ADDRESS_REPORT_KEY_RE.search(k)}
    return card


async def apply_idx_rules(db: AsyncSession, cards: list[dict]) -> list[dict]:
    """Drops opted-out listings and withholds addresses the seller kept private - one batched
    lookup for any list of public listing cards/details (see the rules above)."""
    keys = [str(c["listing_key"]) for c in cards if c.get("listing_key")]
    if not keys:
        return cards
    rows = (await db.execute(
        select(Property.listing_key,
               Property.raw_mls_data["InternetEntireListingDisplayYN"].astext,
               Property.raw_mls_data["InternetAddressDisplayYN"].astext,
               Property.list_office_name)
        .where(Property.listing_key.in_(keys))
    )).all()
    hidden = {str(k) for k, entire, _, _ in rows if entire == "false"}
    no_address = {str(k) for k, _, addr, _ in rows if addr == "false"}
    # The listing brokerage for the card's "Courtesy of" line - the shallow full-MLS search
    # summaries don't carry it.
    office = {str(k): o for k, _, _, o in rows if o}
    out = []
    for c in cards:
        key = str(c.get("listing_key") or "")
        if key in hidden:
            continue
        if not c.get("list_office_name") and office.get(key):
            c["list_office_name"] = office[key]
        out.append(_withhold_address(c) if key in no_address else c)
    return out


def _property_photo(p) -> Optional[str]:
    if getattr(p, "img_src", None):
        return p.img_src
    photos = getattr(p, "photos", None)
    if isinstance(photos, list) and photos:
        return photos[0]
    return None


def _feature_list(*jsonb_fields, limit: int = 12) -> list[str]:
    """Shared by _listing_card and get_property_detail - real per-listing feature strings from
    the raw MLS JSONB fields (interior/exterior/appliances), bounded since a raw MLS features
    list can run long."""
    seen: list[str] = []
    for field in jsonb_fields:
        if isinstance(field, list):
            seen.extend(str(v) for v in field if v)
    return seen[:limit]


def _listing_card(p) -> dict:
    """Accepts either a full ORM `Property` row (this agent's own listings) or a "shallow"
    `PropertySummaryResponse` from PropertyService.get_properties_by_preferences (full-MLS
    filtered searches - see get_featured_page_listings's 'filtered' mode) -
    the latter deliberately omits heavier fields (photos, interior/exterior_features, appliances)
    to keep full-MLS list payloads small, so every such field is read via getattr with a safe
    default rather than assumed present."""
    photos = getattr(p, "photos", None)
    img_src = getattr(p, "img_src", None)
    return {
        "listing_key": p.listing_key,
        # Street only - full_address embeds city/state/zip inline ("637 Heather Ln, Bryn Mawr,
        # PA 19010"), which duplicated the city already shown in its own CityPill on every card.
        "address": tidy_caps(p.street_address or p.full_address),
        "city": tidy_caps(p.city),
        "state": p.state,
        "zipcode": p.zipcode,
        "price": p.price,
        "bedrooms": p.bedrooms,
        "bathrooms": p.bathrooms,
        "living_area": p.living_area,
        "lot_size_acres": getattr(p, "lot_size_acres", None),
        "status": p.home_status,
        "days_on_market": property_days_on_market(p),
        "year_built": getattr(p, "year_built", None),
        "price_per_sqft": getattr(p, "price_per_square_feet", None),
        "school_district": getattr(p, "school_district_name", None),
        # Only meaningful when explicitly known - absent (None) on the shallow full-MLS summary
        # object entirely, so the frontend fact bar simply omits the chip rather than showing a
        # false "No".
        "has_hoa": getattr(p, "has_association", None),
        # Bounded, real per-listing highlights (e.g. "Pool", "Hardwood Floors") for the Featured
        # page's hero/secondary bento cards - same source as the full property detail page's
        # Features & Amenities section, just capped much shorter for a card-sized tag row. Not
        # present on full-MLS summary results, hence the getattr defaults.
        "features": _feature_list(
            getattr(p, "interior_features", None), getattr(p, "exterior_features", None), getattr(p, "appliances", None), limit=4
        ),
        "photo": _property_photo(p),
        # Bounded to a handful - this feeds the Featured page's auto-rotating photo card
        # (PropertyShowcaseCard), not a full gallery, and some listings' photos are inline
        # base64 data (test/manual entries) rather than hosted URLs, so an unbounded list here
        # can blow well past Next.js's fetch-cache size limit on the public site.
        "photos": (photos if isinstance(photos, list) else ([img_src] if img_src else []))[:6],
        "latitude": p.latitude,
        "longitude": p.longitude,
        # Every upcoming Bright open house (soonest first) - the "Open House" chip on cards and
        # the Open Houses list on the detail page. Empty on the shallow full-MLS summary object.
        "open_houses": open_houses_payload(p),
        # Bright IDX: every listing display names the listing brokerage ("Courtesy of ...").
        "list_office_name": getattr(p, "list_office_name", None),
    }


_WEB_URL_RE = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?(/\S*)?$", re.I)


def public_web_url(value: Optional[str]) -> Optional[str]:
    """A social/website link as a working absolute URL - " instagram.com/x" -> "https://instagram.com/x".
    Anything that isn't a web address ("aw", a stray word) is dropped instead of becoming a dead
    link on the agent's public pages."""
    v = (value or "").strip()
    if not v or not _WEB_URL_RE.match(v):
        return None
    return v if v.lower().startswith(("http://", "https://")) else f"https://{v}"


async def get_agent_profile(db: AsyncSession, user: User) -> dict:
    """Merges User (account-level fields) with AgentHub.config (bio/contact/social) - read-only,
    never writes to AgentHub. Agents without an Agent Hub still get a usable profile from just
    the User fields; every field degrades gracefully to '' rather than None so block components
    don't need null-guards everywhere."""
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    config = (hub.config or {}) if hub else {}
    if hub is not None:  # the Website link filled in (their IDX site / own domain)
        from app.services.website_link_service import apply_auto_website
        config = await apply_auto_website(db, user, config, hub.slug)
    profile = config.get("profile") or {}
    contact = config.get("contact") or {}
    social_links = [
        {"id": link.get("id"), "label": link.get("label"), "url": url}
        for link in (config.get("socialLinks") or [])
        if link.get("enabled") and (url := public_web_url(link.get("url")))
    ]
    return {
        "name": profile.get("name") or f"{user.first_name} {user.last_name}".strip(),
        "bio": profile.get("about") or "",
        "title": profile.get("title") or user.professional_title or "",
        "market": profile.get("market") or user.market or "",
        "brokerage": profile.get("brokerage") or user.brokerage or "",
        "license_number": profile.get("licenseNumber") or contact.get("licenseNumber") or user.license_number or "",
        "photo_url": config.get("profilePhotoUrl") or user.photo_url,
        "phone": contact.get("call") or "",
        # The number they text from (setup's "I text from this same number" unticked) - every
        # "Text me" button uses this; blank = the same as `phone`.
        "text_phone": contact.get("text") or contact.get("call") or "",
        "email": contact.get("email") or user.email,
        "social_links": social_links,
        "hub_slug": hub.slug if hub and hub.slug else None,
    }


async def signature_hub_slug_for(db: AsyncSession, user: User) -> Optional[str]:
    """The Agent Hub slug the website footer's signature card loads from, or None when the plan
    has no Email Signature / none is saved (the footer then shows its plain agent block)."""
    from app.services import email_signature_service
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    if not hub or not hub.slug:
        return None
    return hub.slug if await email_signature_service.has_signature_card(db, user) else None


async def get_site_reviews(db: AsyncSession, user: User, limit: int = 60) -> dict:
    """The agent's reviews as their IDX website shows them (the Reviews page + any Client Reviews
    section): {show, summary, reviews, hub_slug}. show = the agent's "Show on IDX" switch AND at
    least one review left after their star filter."""
    from app.models.database import AgentReview
    from app.services.review_display import review_prefs, shown_reviews, summarize
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    prefs = review_prefs(hub.config if hub else None)
    rows = (await db.execute(
        select(AgentReview)
        .where(AgentReview.owner_id == user.id, AgentReview.status == "published")
        .order_by(AgentReview.created_at.desc())
    )).scalars().all()
    shown = shown_reviews(rows, prefs["min_rating"])
    return {
        "show": prefs["show_on_idx"] and bool(shown),
        "summary": summarize(shown),
        "reviews": [
            {
                "id": r.id, "author": r.reviewer_name, "rating": r.rating, "body": r.body,
                "source": r.source, "date": r.created_at.isoformat() if r.created_at else None,
            }
            for r in shown[:limit]
        ],
        "hub_slug": hub.slug if hub else None,
        "min_rating": prefs["min_rating"],
    }


def google_rating(site_reviews: dict) -> dict | None:
    """The star rating the IDX site may hand Google (structured data) - only when it's honest AND
    flattering: every published review counts (no 4-5-star-only filter - Google forbids a
    cherry-picked average), at least 3 reviews, averaging 4.0 or better. Otherwise nothing is
    sent and the site carries no rating at all, so a new or rough patch never shows up in search."""
    summary = site_reviews.get("summary") or {}
    if not site_reviews.get("show") or site_reviews.get("min_rating", 1) > 1:
        return None
    if (summary.get("count") or 0) < 3 or (summary.get("average") or 0) < 4.0:
        return None
    return summary


async def get_active_listings(
    db: AsyncSession, user: User, limit: int = 12, zip_codes: Optional[list[str]] = None,
    area_scope: Optional[dict] = None,
) -> list[dict]:
    """The agent's own active listings - both Bright-synced (`is_manual=False`) and agent-entered
    "My Listings" rows (`is_manual=True`, never touched by the Bright sync)."""
    if not user.mls_id:
        return []
    query = select(Property).where(
        or_(Property.list_agent_mls_id == user.mls_id, Property.co_list_agent_mls_id == user.mls_id),
        Property.home_status.notin_(INACTIVE_STATUSES),
        internet_display_ok(),
    )
    location_filter = geography.location_filter(Property, area_scope or (geography.zip_scope(zip_codes) if zip_codes else None))
    if location_filter is not None:
        query = query.where(location_filter)
    result = await db.execute(query.order_by(Property.mls_list_date.desc().nullslast(), Property.created_at.desc()).limit(limit))
    cards = await apply_idx_rules(db, [_listing_card(p) for p in result.scalars().all()])
    return await _attach_spotlight_urls(db, user, cards)


async def _spotlight_url_map(db: AsyncSession, user: User, listing_keys: list[str]) -> dict[str, str]:
    """listing_key -> the agent's own PUBLISHED Listing Spotlight for that exact property. Same
    rule the Agent Hub and email signature use (property_website_service.describe_listing_link),
    including the per-listing "always use the standard page" override from Settings - batched
    here because a page shows many cards at once."""
    keys = [k for k in listing_keys if k]
    if not keys:
        return {}
    # Never for a home the MLS lists under ANOTHER agent (user, 10/19): a Spotlight the agent made
    # for someone else's listing isn't what their share link, website or property page opens.
    # Their own listings (listing / co-listing agent) and homes with no listing agent on record
    # (manual / off-MLS) keep theirs.
    from app.utils.plan_ids import listing_owner_key
    me = listing_owner_key(user)
    others = {r.listing_key for r in (await db.execute(
        select(Property.listing_key, Property.list_agent_mls_id, Property.co_list_agent_mls_id).where(Property.listing_key.in_(keys))
    )).all() if (r.list_agent_mls_id or r.co_list_agent_mls_id) and me not in {r.list_agent_mls_id, r.co_list_agent_mls_id}}
    keys = [k for k in keys if k not in others]
    if not keys:
        return {}
    from app.models.database import PropertyWebsite
    from app.services import property_website_service
    hub_config = (await db.execute(select(AgentHub.config).where(AgentHub.owner_id == user.id))).scalar_one_or_none() or {}
    overrides = hub_config.get("listingLinkOverrides") or {}
    rows = (await db.execute(
        select(PropertyWebsite)
        .where(PropertyWebsite.owner_id == user.id, PropertyWebsite.status == "PUBLISHED", PropertyWebsite.listing_key.in_(keys))
        .order_by(PropertyWebsite.updated_at.desc().nullslast())
    )).scalars().all()
    out: dict[str, str] = {}
    for site in rows:
        if site.listing_key in out:
            continue
        if overrides.get(property_website_service.listing_override_key(site.listing_key, None)) == "property_page":
            continue
        out[site.listing_key] = await property_website_service.public_url(db, site)
    return out


async def _attach_spotlight_urls(db: AsyncSession, user: User, cards: list[dict]) -> list[dict]:
    urls = await _spotlight_url_map(db, user, [c.get("listing_key") for c in cards])
    for c in cards:
        c["spotlight_url"] = urls.get(c.get("listing_key"))
    return cards


async def attach_spotlight_urls(db: AsyncSession, user: User, cards: list[dict]) -> list[dict]:
    """Public form of the above for routes: `spotlight_url` on every card/detail dict whose
    listing the site owner has a published Spotlight for (search results, Featured page, area
    inventory, property pages) - so the Spotlight chip/banner shows wherever that home appears."""
    return await _attach_spotlight_urls(db, user, cards)


async def get_published_spotlights(db: AsyncSession, user: User, limit: int = 12) -> list[dict]:
    """The agent's published Listing Spotlights (Studio -> Spotlights) as website cards - the
    richest page an agent has for a home, so the website shows them off instead of hiding them
    behind the Agent Hub. One card per property (a listing can have more than one Spotlight), the
    live MLS status/price layered over the Spotlight's own snapshot so a sold home reads as sold."""
    from app.models.database import PropertyWebsite
    from app.services import property_website_service
    rows = (await db.execute(
        select(PropertyWebsite)
        .where(PropertyWebsite.owner_id == user.id, PropertyWebsite.status == "PUBLISHED")
        .order_by(PropertyWebsite.published_at.desc().nullslast(), PropertyWebsite.updated_at.desc().nullslast())
        .limit(max(limit * 2, 12))
    )).scalars().all()
    if not rows:
        return []
    keys = [s.listing_key for s in rows if s.listing_key]
    live: dict[str, tuple] = {}
    if keys:
        live_rows = (await db.execute(select(Property.listing_key, Property.home_status, Property.price).where(Property.listing_key.in_(keys)))).all()
        live = {k: (status, price) for k, status, price in live_rows}
    out: list[dict] = []
    seen: set[str] = set()
    for site in rows:
        dedupe_key = site.listing_key or site.slug
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        p = site.property_snapshot or {}
        photos = p.get("photos") if isinstance(p.get("photos"), list) else []
        status, price = live.get(site.listing_key, (p.get("status"), p.get("price")))
        out.append({
            "id": site.id,
            "slug": site.slug,
            "url": await property_website_service.public_url(db, site),
            "listing_key": site.listing_key,
            "address": p.get("address"),
            "city": p.get("city"),
            "state": p.get("state"),
            "price": price if price is not None else p.get("price"),
            "bedrooms": p.get("beds"),
            "bathrooms": p.get("baths"),
            "living_area": p.get("sqft"),
            "status": status,
            "photo": photos[0] if photos else None,
            "photos": photos[:6],
            "template_id": site.template_id,
        })
        if len(out) >= limit:
            break
    return out


def _sort_cards(cards: list[dict], sort: str, mixed: bool = False) -> list[dict]:
    if sort == "price_desc":
        return sorted(cards, key=lambda c: c.get("price") or 0, reverse=True)
    if sort == "price_asc":
        return sorted(cards, key=lambda c: c.get("price") or 0)
    if mixed:
        # two newest-first lists merged into one: fewest days on market first (unknown last)
        return sorted(cards, key=lambda c: (c.get("days_on_market") is None, c.get("days_on_market") or 0))
    return cards  # 'newest' - already newest-first (see get_active_listings/get_properties_by_preferences ordering)


async def _backfill_photo_arrays(db: AsyncSession, cards: list[dict], cap: int = 10) -> list[dict]:
    """The full-MLS 'filtered' branch of get_featured_page_listings builds cards from
    PropertyService's shallow PropertySummaryResponse (see _listing_card's docstring), which
    omits `photos` entirely - every such card falls back to a single cover photo. The Featured
    page's hero carousel (FeaturedHeroCarousel.tsx) shows several real photos of the same
    listing across its panels, so it needs more than that. Rather than widening the shallow
    search everywhere (which would blow up payload size for the much larger Search tab result
    grids), backfill the real `photos` JSONB with one small targeted query, scoped to just the
    handful of cards that actually end up in the hero pool."""
    needs_backfill = [c["listing_key"] for c in cards[:cap] if len(c.get("photos") or []) <= 1]
    if not needs_backfill:
        return cards
    result = await db.execute(select(Property.listing_key, Property.photos).where(Property.listing_key.in_(needs_backfill)))
    photo_map = {key: photos for key, photos in result.all() if isinstance(photos, list) and len(photos) > 1}
    for c in cards:
        real = photo_map.get(c["listing_key"])
        if real:
            c["photos"] = real[:6]
            c["photo"] = real[0]
    return cards


async def get_featured_page_listings(db: AsyncSession, user: User, settings: dict, limit: int = 48) -> list[dict]:
    """The public Featured (/properties) page's content - a full nav tab (see SiteChrome
    navLinks), not a page-builder block, so its settings live at the site level
    (Site.draft_data.featured_settings, see schemas.site.FeaturedSettings) rather than in any
    one page's blocks.

    'my_listings' mode (the default) is exactly today's behavior: this agent's own active
    listings, unfiltered. 'filtered' mode hands the whole page to an agent-defined full-MLS
    search - same full-MLS exception as get_hero_photos/get_search_results above, see that
    docstring - so an agent can build a real showcase page (e.g. only $1.5M-$4M listings across
    a few towns/school districts, highest price first) instead of just their own inventory.
    `prioritize_my_listings` (default True) pulls this agent's own matching listings to the front
    of a filtered result rather than letting them sort in wherever the full-MLS pool puts them -
    an agent showcasing the broader market still gets to lead with their own inventory if they
    want to, see schemas.site.FeaturedSettings."""
    sort = settings.get("sort", "newest")
    show_mine, show_areas, show_custom = featured_sources(settings)
    prioritize_mine = settings.get("prioritize_my_listings", True) is not False
    min_price, max_price = _as_int(settings.get("min_price")), _as_int(settings.get("max_price"))

    # Other homes for sale: (a) in the towns the agent serves (the Areas tab - optionally just
    # some of them), the right default for an agent with a thin personal inventory, whose Featured
    # page otherwise showed one or two homes; (b) a hand-picked full-MLS search (towns / school
    # districts). Both share the price range.
    searches: list[tuple[list[str], list[str]]] = []
    if show_areas:
        served = await served_area_names(db, user, 50)
        picked = {t.strip().lower() for t in (settings.get("area_towns") or []) if isinstance(t, str)}
        towns = [t for t in served if t.lower() in picked] if picked else served
        if towns:
            searches.append((towns, []))
    if show_custom:
        towns = settings.get("towns") or []
        districts = settings.get("school_districts") or []
        if towns or districts:
            searches.append((list(towns), list(districts)))

    own = await get_active_listings(db, user, limit) if (show_mine or not searches) else []
    if not searches:
        # Only their own listings (or nothing else is set up yet) - today's original page.
        return _sort_cards(own, sort)[:limit]

    others: list[dict] = []
    seen = {c["listing_key"] for c in own}
    for towns, districts in searches:
        prefs = _build_property_preferences(cities=towns or None, min_price=min_price, max_price=max_price)
        if districts:
            prefs.school_districts = districts
        results = await PropertyService.get_properties_by_preferences(db, prefs, max_properties=limit * 2)
        results = [p for p in results if geography.is_residential_for_sale(getattr(p, "home_type", None), getattr(p, "price", None))]
        for c in await apply_idx_rules(db, [_listing_card(p) for p in results]):
            if c["listing_key"] not in seen:
                seen.add(c["listing_key"])
                others.append(c)

    if prioritize_mine:
        # "My listings first, then the rest": own listings always lead regardless of sort - each
        # group is sorted on its own, so full-MLS results never jump ahead of their inventory.
        merged = _sort_cards(own, sort) + _sort_cards(others, sort)
    else:
        # "Mix them together": one list in the chosen order (newest = fewest days on market).
        merged = _sort_cards(own + others, sort, mixed=True)
    return await _attach_spotlight_urls(db, user, await _backfill_photo_arrays(db, merged[:limit]))


def featured_sources(settings: dict) -> tuple[bool, bool, bool]:
    """(own listings, homes in their towns, hand-picked search) for a Featured page. Saves from
    before 10/22 only have `mode` - read them the way they always worked: 'my_listings' = own
    only; 'served_areas' = own + their towns; 'filtered' = the search, with their own listings
    only when 'prioritize' was on."""
    mode = settings.get("mode", "my_listings")
    mine = settings.get("show_mine")
    areas = settings.get("show_area_homes")
    custom = settings.get("show_custom")
    if mine is None:
        mine = mode != "filtered" or settings.get("prioritize_my_listings", True) is not False
    if areas is None:
        areas = mode == "served_areas"
    if custom is None:
        custom = mode == "filtered"
    return bool(mine), bool(areas), bool(custom)



def _fix_mojibake(text: Optional[str]) -> Optional[str]:
    """MLS remarks sometimes arrive as UTF-8 that was read as Windows-1252 somewhere upstream
    (curly quotes become "â€œ"). Re-decode when that pattern shows up; leave anything else alone."""
    if text and "�" in text:
        # A character already lost upstream ("Caf�") - restore the common accented words.
        text = text.replace("Caf�", "Café").replace("caf�", "café").replace("Ros�", "Rosé")
    if not text or not any(mark in text for mark in ("â€", "Ã", "Â")):
        return text
    try:
        return text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def fix_mojibake_deep(value):
    """_fix_mojibake over every string in a JSON-ish value (area facts / narratives)."""
    if isinstance(value, str):
        return _fix_mojibake(value)
    if isinstance(value, list):
        return [fix_mojibake_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_mojibake_deep(v) for k, v in value.items()}
    return value


# The record keys the property page's "Every detail" panel reads (the same names the Listing
# Page's PropertyReport uses). Photos and remarks are already on the payload, so they're left out.
_REPORT_SKIP = {"photos", "PublicRemarks", "id", "createdAt", "updatedAt"}


async def _full_report(db: AsyncSession, listing_key: str) -> Optional[dict]:
    """The whole mirrored MLS record for one home (heating, cooling, parking, taxes, schools by
    level, HOA, basement, construction...) so the IDX property page can show every detail we
    have, not the short card subset. Empty values are dropped to keep the payload small."""
    try:
        full = await PropertyService.get_property_by_listing_key(db, listing_key)
    except Exception:
        logger.warning("Full property report lookup failed", extra={"listing_key": listing_key}, exc_info=True)
        return None
    if not full:
        return None
    data = full.model_dump(by_alias=True, mode="json")
    return {k: v for k, v in data.items() if k not in _REPORT_SKIP and v not in (None, "", [], {})}

async def get_property_detail(db: AsyncSession, user: User, listing_key: str) -> Optional[dict]:
    """Scoped to the requesting agent's own listings - a public site must never surface an
    arbitrary property by guessing a listing_key that belongs to a different agent."""
    if not user.mls_id:
        return None
    result = await db.execute(
        select(Property).where(
            Property.listing_key == listing_key,
            or_(Property.list_agent_mls_id == user.mls_id, Property.co_list_agent_mls_id == user.mls_id),
        )
    )
    p = result.scalar_one_or_none()
    if not p or p.home_status in _IDX_NOT_SHOWN_STATUSES:
        return None

    card = _listing_card(p)
    card.update({
        "description": _fix_mojibake(p.description),
        "photos": p.photos if isinstance(p.photos, list) else ([card["photo"]] if card["photo"] else []),
        "lot_size": p.lot_size,
        "year_built": p.year_built,
        "home_type": p.home_type,
        "county": p.county,
        "township": p.township,
        "days_on_market": property_days_on_market(p),
        "mls_number": p.listing_id,
        "expected_on_market_date": (p.raw_mls_data or {}).get("ExpectedOnMarketDate"),
        "list_office_name": p.list_office_name,
        "list_office_phone": p.list_office_phone,
        "list_agent_full_name": p.list_agent_full_name,
        "list_agent_email": p.list_agent_email,
        "list_agent_phone": p.list_agent_preferred_phone,
        "features": _feature_list(p.interior_features, p.exterior_features, p.appliances),
        # Price History section - Bright's API has no dedicated price-history resource (confirmed
        # via bright_mls_service.discover_price_history_resources/probe_price_history_for_listing),
        # so this is the full extent of what's trackable: original list price vs. current, and
        # when it last changed.
        "original_list_price": p.original_list_price,
        "price_change_timestamp": p.price_change_timestamp.isoformat() if p.price_change_timestamp else None,
        "mls_list_date": p.mls_list_date.isoformat() if p.mls_list_date else None,
    })
    card["spotlight_url"] = (await _spotlight_url_map(db, user, [listing_key])).get(listing_key)
    card["report"] = await _full_report(db, listing_key)
    from app.services.price_history_service import get_history
    card["price_history"] = await get_history(db, listing_key)
    allowed = await apply_idx_rules(db, [card])
    return allowed[0] if allowed else None


async def get_similar_listings(db: AsyncSession, user: User, exclude_listing_key: str, city: Optional[str], limit: int = 3) -> list[dict]:
    """Same-city active listings from this agent, excluding the one being viewed - real data,
    not a guessed recommendation engine."""
    if not user.mls_id or not city:
        return []
    result = await db.execute(
        select(Property)
        .where(
            or_(Property.list_agent_mls_id == user.mls_id, Property.co_list_agent_mls_id == user.mls_id),
            Property.home_status.notin_(INACTIVE_STATUSES),
            Property.city.ilike(city),
            Property.listing_key != exclude_listing_key,
            internet_display_ok(),
        )
        .order_by(Property.mls_list_date.desc().nullslast())
        .limit(limit)
    )
    return await apply_idx_rules(db, [_listing_card(p) for p in result.scalars().all()])


def _agent_sale_identifier_filters(mls_id: str) -> list:
    """Shared by get_recent_sales and get_agent_stats - every raw-MLS-payload identifier key a
    closed sale could be matched under, buy-side or list-side (see get_recent_sales docstring)."""
    identifier_keys = ["BuyerAgentMlsId", "CoBuyerAgentMlsId", "ListAgentMlsId", "CoListAgentMlsId"]
    filters = [Property.raw_mls_data[key].astext == mls_id for key in identifier_keys]
    filters.append(Property.list_agent_mls_id == mls_id)
    filters.append(Property.co_list_agent_mls_id == mls_id)
    return filters


def _not_a_lease():
    """A rental isn't a sale - leases closed by an agent never fill their Recent Sales."""
    t = Property.raw_mls_data["PropertyType"].astext
    return or_(t.is_(None), ~t.ilike("%lease%"))


def _sold_price(p) -> Optional[float]:
    """What it sold for (ClosePrice), the list price only when Bright never sent one."""
    raw = p.raw_mls_data if isinstance(p.raw_mls_data, dict) else {}
    try:
        close = float(raw.get("ClosePrice") or 0)
    except (TypeError, ValueError):
        close = 0
    return close or p.price


def _sold_date(p) -> Optional[str]:
    raw = p.raw_mls_data if isinstance(p.raw_mls_data, dict) else {}
    return str(raw.get("CloseDate") or "")[:10] or None


async def get_recent_sales(
    db: AsyncSession, user: User, limit: int = 12, zip_codes: Optional[list[str]] = None,
    area_scope: Optional[dict] = None,
) -> list[dict]:
    """Local-DB-only equivalent of Agent Hub's recent-sales matcher (see module docstring for
    why this doesn't call Bright). Matches by MLS id on either side of the raw MLS payload since
    a closed sale may have been buy-side, not just a listing this agent held."""
    if not user.mls_id:
        return []
    mls_id = user.mls_id.strip()
    # Closed sales only, newest CLOSING first (Bright audit, 10/15): it used to take the agent's 3x
    # most recently MODIFIED rows of any status, so a busy agent's listings crowded their real
    # sales out, and it showed the list price + the last-edited date instead of what it sold for
    # and when.
    query = select(Property).where(
        or_(*_agent_sale_identifier_filters(mls_id)),
        or_(Property.home_status.ilike("%closed%"), Property.home_status.ilike("%sold%")),
        _not_a_lease(),
    )
    location_filter = geography.location_filter(Property, area_scope or (geography.zip_scope(zip_codes) if zip_codes else None))
    if location_filter is not None:
        query = query.where(location_filter)
    result = await db.execute(
        query.order_by(Property.raw_mls_data["CloseDate"].astext.desc().nullslast(),
                       Property.modification_timestamp.desc().nullslast())
        .limit(limit * 3)  # over-fetch before the price floor below, still bounded
    )
    sales = []
    for p in result.scalars().all():
        sale = {
            "id": p.id,
            "listing_key": p.listing_key,
            "address": tidy_caps(p.full_address or p.street_address),
            "city": tidy_caps(p.city),
            "state": p.state,
            "price": _sold_price(p),
            "bedrooms": p.bedrooms,
            "bathrooms": p.bathrooms,
            "living_area": p.living_area,
            "status": p.home_status,
            "photo": _property_photo(p),
            "sold_date": _sold_date(p) or p.modification_timestamp or p.mls_list_date,
        }
        if is_eligible_recent_sale(sale):
            sales.append(apply_recent_sale_defaults(sale))
    sales.sort(key=lambda s: str(s.get("sold_date") or ""), reverse=True)
    return (await apply_idx_rules(db, sales))[:limit]


async def hub_sales_rules(db: AsyncSession, user: User) -> dict:
    """The agent's sales choices from their Agent Hub settings (setup flow's Sales step / Agent
    Hub editor) that the website follows too: the minimum price a sale must clear to be shown,
    the order sales are listed in, whether sold stats show, and which form buttons show."""
    cfg = (await db.execute(select(AgentHub.config).where(AgentHub.owner_id == user.id))).scalar_one_or_none() or {}
    try:
        min_price = int(cfg.get("salesMinPrice") or 0)
    except (TypeError, ValueError):
        min_price = 0
    sort = cfg.get("salesSort") if cfg.get("salesSort") in ("newest", "price-high", "price-low") else "newest"
    buttons = cfg.get("hubFormButtons")
    return {
        "min_price": max(0, min_price), "sort": sort, "show_stats": cfg.get("showSalesStats") is not False,
        "form_buttons": [b for b in buttons if isinstance(b, str)] if isinstance(buttons, list) else None,
        # Sales / listings the agent switched off in the Agent Hub editor stay off on the website
        # too (owner, 10/24: "make sure those settings are used everywhere").
        "hidden_sales": _hidden_keys(cfg.get("recentSales")),
        "hidden_listings": _hidden_keys(cfg.get("myListings")),
    }


def sales_rules_from_config(cfg: Optional[dict]) -> dict:
    """hub_sales_rules for a hub config already in hand (no DB read): hidden picks, min price,
    order - for callers that only have the config (Showcase form examples, the AI blog writer)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        min_price = int(cfg.get("salesMinPrice") or 0)
    except (TypeError, ValueError):
        min_price = 0
    sort = cfg.get("salesSort") if cfg.get("salesSort") in ("newest", "price-high", "price-low") else "newest"
    return {"min_price": max(0, min_price), "sort": sort,
            "hidden_sales": _hidden_keys(cfg.get("recentSales")), "hidden_listings": _hidden_keys(cfg.get("myListings"))}


def _addr_key(address) -> str:
    return re.sub(r"[^a-z0-9]", "", str(address or "").split(",")[0].lower())


def _hidden_keys(items) -> set[str]:
    """ids (listing keys) + street keys of the hub items marked hidden (visible False)."""
    out: set[str] = set()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("visible") is False:
            if it.get("id") not in (None, ""):
                out.add(f"id:{it['id']}")
            if _addr_key(it.get("address")):
                out.add(f"addr:{_addr_key(it.get('address'))}")
    return out


def is_hidden_in_hub(item: dict, hidden: set[str]) -> bool:
    if not hidden:
        return False
    keys = {f"id:{item.get(k)}" for k in ("listing_key", "id") if item.get(k) not in (None, "")}
    if _addr_key(item.get("address")):
        keys.add(f"addr:{_addr_key(item.get('address'))}")
    return bool(keys & hidden)


def drop_hidden_listings(listings: list[dict], rules: dict) -> list[dict]:
    """Active listings minus the ones the agent hid in the Agent Hub editor."""
    hidden = rules.get("hidden_listings") or set()
    return [l for l in listings if not is_hidden_in_hub(l, hidden)]


def apply_sales_rules(sales: list[dict], rules: dict) -> list[dict]:
    """Drops sales the agent hid or that are under the minimum price, and orders them (newest
    is how they already come)."""
    hidden = rules.get("hidden_sales") or set()
    out = [s for s in sales if (not rules.get("min_price") or (s.get("price") or 0) >= rules["min_price"]) and not is_hidden_in_hub(s, hidden)]
    if rules.get("sort") == "price-high":
        out.sort(key=lambda s: s.get("price") or 0, reverse=True)
    elif rules.get("sort") == "price-low":
        out.sort(key=lambda s: s.get("price") or 0)
    return out


def stats_shown(stats: Optional[dict], rules: dict) -> Optional[dict]:
    """Sold count / volume blanked while the agent hides their sold stats (active listings stay)."""
    if stats is None or rules.get("show_stats", True):
        return stats
    return {**stats, "sold_count": 0, "sold_volume": 0}


async def get_agent_stats(db: AsyncSession, user: User) -> dict:
    """Real, substantiated production numbers for the About page - never a fabricated "years of
    experience" or "5-star reviews" figure OHP has no data source for. Every number here is
    derived from this agent's own actual local data: active listing count (live query) and a
    closed-sales count + total volume using the exact same identifier-matching and eligibility
    rules as get_recent_sales (so the About page and the Sold page always agree with each other).
    Deliberately uncapped by a small `limit` (unlike get_recent_sales, which is display-bound) -
    this is a count/sum, not a list to render, so it fetches every matching row up to a generous
    bound rather than silently under-counting a busy agent's real production."""
    if not user.mls_id:
        return {"active_listings": 0, "sold_count": 0, "sold_volume": 0}
    mls_id = user.mls_id.strip()

    active_count = (await db.execute(
        select(func.count(Property.id)).where(
            or_(Property.list_agent_mls_id == mls_id, Property.co_list_agent_mls_id == mls_id),
            Property.home_status.notin_(INACTIVE_STATUSES),
        )
    )).scalar_one()

    # The agent's own Recent Sales list (Agent Hub) is what their hub's Sold / Volume header counts -
    # including sales found in Bright beyond our local mirror and ones they added by hand - so the
    # website shows the same numbers on every page (it used to say 5 sold on Home, 11 on About).
    hub_config = (await db.execute(select(AgentHub.config).where(AgentHub.owner_id == user.id))).scalar_one_or_none() or {}
    hub_sales = [s for s in (hub_config.get("recentSales") or []) if isinstance(s, dict) and is_eligible_recent_sale(s)]
    if hub_sales:
        return {"active_listings": active_count, "sold_count": len(hub_sales), "sold_volume": round(sum(s.get("price") or 0 for s in hub_sales))}

    result = await db.execute(
        select(Property)
        .where(or_(*_agent_sale_identifier_filters(mls_id)),
               or_(Property.home_status.ilike("%closed%"), Property.home_status.ilike("%sold%")), _not_a_lease())
        .limit(1000)
    )
    sold_count = 0
    sold_volume = 0
    for p in result.scalars().all():
        price = _sold_price(p)
        if is_eligible_recent_sale({"status": p.home_status, "price": price}):
            sold_count += 1
            sold_volume += price or 0

    return {"active_listings": active_count, "sold_count": sold_count, "sold_volume": round(sold_volume)}


async def get_open_houses(
    db: AsyncSession, user: User, limit: int = 6, upcoming_only: bool = True,
    zip_codes: Optional[list[str]] = None, area_scope: Optional[dict] = None,
) -> list[dict]:
    query = select(OpenHouseEvent).where(
        OpenHouseEvent.agent_id == user.id,
        OpenHouseEvent.is_deleted.is_(False),
    )
    if upcoming_only:
        query = query.where(OpenHouseEvent.scheduled_date.is_not(None), OpenHouseEvent.scheduled_date >= date.today())
    location_filter = geography.location_filter(OpenHouseEvent, area_scope or (geography.zip_scope(zip_codes) if zip_codes else None))
    if location_filter is not None:
        query = query.where(location_filter)
    query = query.order_by(OpenHouseEvent.scheduled_date.asc().nullslast()).limit(limit)
    result = await db.execute(query)
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "listing_key": e.listing_key,
            "address": e.address,
            "city": e.city,
            "state": e.state,
            "price": e.price,
            "bedrooms": e.bedrooms,
            "bathrooms": e.bathrooms,
            "living_area": e.living_area,
            "status": e.home_status,
            "photo": e.cover_image_url,
            "scheduled_date": e.scheduled_date.isoformat() if e.scheduled_date else None,
            "scheduled_start_time": e.scheduled_start_time.isoformat() if e.scheduled_start_time else None,
            "scheduled_end_time": e.scheduled_end_time.isoformat() if e.scheduled_end_time else None,
        }
        for e in events
    ]


# Some covers are stored inline as base64 data: URLs (500 KB+ each). Sent as-is, one of them made
# the IDX Blog page 1.2 MB of HTML (it lands in the markup AND the page data). Public payloads get
# this small address instead - public_site_routes.get_blog_cover serves the picture, shrunk.
BLOG_COVER_ROUTE = "/public/sites/blog-cover/{id}"


def public_image_url(url: Optional[str], blog_id: str) -> Optional[str]:
    """A blog photo as public pages should reference it: inline data: images become the cover route."""
    if url and url.startswith("data:"):
        return BLOG_COVER_ROUTE.format(id=blog_id)
    return url


def _blog_photo(b: Blog) -> Optional[str]:
    """The article's cover, else the first photo inside it - cards always get a picture if one exists."""
    return public_image_url(blog_photo_raw(b), b.id)


def blog_photo_raw(b: Blog) -> Optional[str]:
    """_blog_photo before the data: swap (the cover route decodes this one)."""
    if b.cover_image_url:
        return b.cover_image_url
    for blk in (b.blocks if isinstance(b.blocks, list) else []):
        data = (blk or {}).get("data") or {}
        kind = (blk or {}).get("type")
        if kind == "image" and data.get("url"):
            return data["url"]
        if kind == "gallery" and data.get("images"):
            return data["images"][0]
        if kind == "property_card" and (data.get("property") or {}).get("image_url"):
            return data["property"]["image_url"]
    for prop in (b.linked_properties or []):
        if isinstance(prop, dict) and prop.get("image_url"):
            return prop["image_url"]
    return b.social_image_url or None


def _blog_card(b: Blog) -> dict:
    blocks = b.blocks if isinstance(b.blocks, list) else []
    return {
        "id": b.id,
        "slug": b.slug,
        "title": b.title,
        "subtitle": b.subtitle,
        "cover_image_url": _blog_photo(b),
        "category": b.category,
        "published_at": b.published_at.isoformat() if b.published_at else None,
        # Real signal, not a guess - lets the Media hub show a "Watch" badge on posts that
        # actually embed a YouTube block, distinguishing video content from articles without
        # needing a separate media-library system (see content_adapters module docstring).
        "has_video": any((blk or {}).get("type") == "youtube" for blk in blocks),
    }


async def get_blog_posts(db: AsyncSession, user: User, limit: int = 6) -> list[dict]:
    result = await db.execute(
        select(Blog)
        .where(Blog.owner_id == user.id, Blog.status == "PUBLISHED", Blog.idx_visible.is_(True))
        .order_by(Blog.published_at.desc().nullslast())
        .limit(limit)
    )
    return [_blog_card(b) for b in result.scalars().all()]


async def get_blog_post_detail(db: AsyncSession, user: User, slug: str) -> Optional[Blog]:
    result = await db.execute(
        select(Blog).where(Blog.owner_id == user.id, Blog.slug == slug, Blog.status == "PUBLISHED")
    )
    return result.scalar_one_or_none()


def _hub_items(config: dict, key: str, limit: int) -> list[dict]:
    items = [i for i in (config.get(key) or []) if isinstance(i, dict)]
    # Starred favourites first, same rule as the Agent Hub itself (frontend lib/hubPins.ts).
    items = [i for i in items if i.get("pinned")] + [i for i in items if not i.get("pinned")]
    out = []
    for item in items:
        link = (item.get("link") or "").strip()
        if not link:
            continue
        out.append({
            "id": item.get("id"),
            "title": item.get("title") or "",
            "image": item.get("image"),
            "link": link,
        })
        if len(out) >= limit:
            break
    return out


async def get_agent_hub_media(db: AsyncSession, user: User, limit: int = 24) -> dict:
    """Real videos/house tours for the public Media page - a read-only mirror of this agent's own
    Agent Hub "Videos"/"House Tours" sections (AgentHub.config.videoLinks/houseTourLinks, the
    same YouTube links their Agent Hub page already shows - see app/studio/site/page.tsx's
    VideoLinksPanel/HouseToursPanel), never a second copy the agent maintains twice. Whether these
    show on the website at all is a separate toggle (Site.draft_data.media_settings, see
    schemas.site.MediaSettings) - the caller decides whether to use this at all."""
    result = await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))
    hub = result.scalar_one_or_none()
    config = (hub.config or {}) if hub else {}
    return {
        "videos": _hub_items(config, "videoLinks", limit),
        "tours": _hub_items(config, "houseTourLinks", limit),
        # The Media page hero's "Subscribe" button.
        "channel_url": (config.get("videoChannelUrl") or "").strip() or None,
    }


_HUB_SECTION_IDS = ["sales", "listings", "videos", "tours", "blogs"]


async def get_agent_hub_snapshot(db: AsyncSession, user: User) -> dict:
    """The site's About page mirrors the agent's real Agent Hub (openhousepal.com/{slug}) instead
    of a separately hand-written bio block - an agent's own site can then work as their link-tree
    too, not just openhousepal.com (see Website Builder Phase 6). Assembled entirely from data
    other content_adapters functions already expose (profile/contact/social, videos/tours, recent
    sales, active listings, blog posts, lead-capture forms) - this is a read-only re-presentation,
    never a second copy of any of it. Deliberately doesn't attempt the real Agent Hub page's
    carousels/auto-scroll/mobile-specific layout (app/[slug]/page.tsx) - just its content, restyled
    to the site's own theme instead of OpenHousePal's fixed branding.

    Section visibility/order IS carried over from the real Agent Hub's own editor (config.
    layoutSections - see app/[slug]/page.tsx's applyHubConfig), though - an agent who hid Videos
    or reordered Sales above Listings on their real Agent Hub expects that same choice reflected
    here, not every section with data unconditionally shown in a fixed order."""
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    config = (hub.config or {}) if hub else {}
    raw_sections = [s for s in (config.get("layoutSections") or []) if s.get("id") in _HUB_SECTION_IDS]
    seen_ids = {s["id"] for s in raw_sections}
    layout_sections = raw_sections + [{"id": i, "visible": True} for i in _HUB_SECTION_IDS if i not in seen_ids]
    visible = {s["id"] for s in layout_sections if s.get("visible", True)}

    profile = await get_agent_profile(db, user)
    # The hub's own sales choices (min price, order, sold stats on/off, form buttons) apply here too.
    rules = await hub_sales_rules(db, user)
    profile["stats"] = stats_shown(await get_agent_stats(db, user), rules)
    hub_media = await get_agent_hub_media(db, user, limit=12) if (visible & {"videos", "tours"}) else {"videos": [], "tours": []}
    recent_sales = apply_sales_rules(await get_recent_sales(db, user, limit=24), rules)[:8] if "sales" in visible else []
    listings = drop_hidden_listings(await get_active_listings(db, user, limit=16), rules)[:8] if "listings" in visible else []
    blog_posts = await get_blog_posts(db, user, limit=6) if "blogs" in visible else []
    forms = await get_forms_list(db, user)
    forms_by_type = {f["form_type"]: f for f in forms if rules["form_buttons"] is None or f["form_type"] in rules["form_buttons"]}
    return {
        "agent_hub_slug": hub.slug if hub else None,
        "profile": profile,
        "videos": hub_media["videos"] if "videos" in visible else [],
        "tours": hub_media["tours"] if "tours" in visible else [],
        "recent_sales": recent_sales,
        "listings": listings,
        "blog_posts": blog_posts,
        "section_order": [s["id"] for s in layout_sections],
        "buying_form": forms_by_type.get("buying"),
        "selling_form": forms_by_type.get("selling"),
        "home_value_form": forms_by_type.get("home_value"),
        "showing_form": forms_by_type.get("showing"),
    }


async def get_showcases(db: AsyncSession, user: User, limit: int = 12) -> list[dict]:
    result = await db.execute(
        select(Collection)
        .where(Collection.owner_id == user.id, Collection.is_public.is_(True), Collection.status == "ACTIVE")
        .order_by(Collection.updated_at.desc().nullslast(), Collection.created_at.desc())
        .limit(limit)
    )
    return [
        {"id": c.id, "name": c.name, "description": c.description, "share_token": c.share_token}
        for c in result.scalars().all()
        if c.share_token
    ]


async def get_showcase_by_id(db: AsyncSession, user: User, collection_id: str) -> Optional[dict]:
    result = await db.execute(
        select(Collection).where(
            Collection.id == collection_id, Collection.owner_id == user.id,
            Collection.is_public.is_(True), Collection.status == "ACTIVE",
        )
    )
    c = result.scalar_one_or_none()
    if not c or not c.share_token:
        return None
    return {"id": c.id, "name": c.name, "description": c.description, "share_token": c.share_token}


def resolve_photo_urls(photos_fact: Optional[dict]) -> Optional[dict]:
    """Rewrites stored Places `photo_name`s (see area_enrichment_service._places_photos) into
    our own proxy path (public_site_routes.get_area_photo) - never returns a raw googleapis.com
    URL with the API key embedded, since this ends up on public, crawlable agent website pages."""
    if not photos_fact or not photos_fact.get("items"):
        return photos_fact
    resolved = dict(photos_fact)
    resolved["items"] = [
        {**item, "url": f"/public/sites/photo/{item['photo_name']}" if item.get("photo_name") else item["url"]}
        for item in photos_fact["items"] if item.get("photo_name") or item.get("url")
    ]
    return resolved


async def _area_top_listing_photo(db: AsyncSession, area_scope: dict) -> Optional[str]:
    """A real photo of an actual home for sale in this area - the cover photo of the single
    highest-priced active listing matching the area's shared geography in our own regional MLS
    mirror - rather than a generic Google Places street-view photo (see resolve_photo_urls,
    still used as a fallback below when no local listings match yet, e.g. a brand-new area)."""
    location_filter = geography.location_filter(Property, area_scope)
    if location_filter is None:
        return None
    result = await db.execute(
        select(Property.photos, Property.img_src)
        .where(location_filter, Property.home_status.notin_(INACTIVE_STATUSES), geography.residential_for_sale_filter(Property), internet_display_ok())
        .order_by(Property.price.desc().nullslast())
        .limit(1)
    )
    row = result.first()
    if not row:
        return None
    photos, img_src = row
    if isinstance(photos, list) and photos:
        return photos[0]
    return img_src


def _curated_entities(facts: dict, key: str) -> list[dict]:
    value = (facts or {}).get(key) or {}
    entities = value.get("entities") if isinstance(value, dict) else None
    return entities if isinstance(entities, list) else []


def _entity_names(facts: dict, key: str, limit: int = 2) -> list[str]:
    names: list[str] = []
    for entity in _curated_entities(facts, key):
        name = str(entity.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


# ---------------------------------------------------------------- schools: checked against Bright
# A town's researched school districts can be incomplete - "Haverford" was researched as Haverford
# Township's district, while two-thirds of homes with a Haverford address are in Lower Merion. The
# listings themselves carry the district (Bright's school_district_name), so every town page checks
# its districts against them: any district serving 10%+ of the town's homes is shown (with its
# schools, borrowed from another researched town in that district), a researched district no home
# uses is dropped once there's enough data, and each district carries its share of homes.
_SCHOOLS_TTL_S = 300
_schools_cache: dict[str, tuple[float, dict]] = {}
_SCHOOL_LEVELS = ("elementary_schools", "middle_schools", "high_schools")


_DISTRICT_STOPWORDS = {"school", "schools", "district", "of", "the", "sd", "public"}


def _school_key(name) -> str:
    """'Gladwyne Elementary School' and 'Gladwyne School', 'Bala-Cynwyd' and 'Bala Cynwyd' -> same."""
    n = re.sub(r"[^a-z ]", " ", str(name or "").lower())
    return " ".join(w for w in n.split() if w not in ("school", "elementary", "the"))


def _district_key(name: str) -> str:
    n = re.sub(r"[^a-z ]", " ", str(name or "").lower())
    n = " ".join(w for w in n.split() if w not in _DISTRICT_STOPWORDS)
    return " ".join(n.split())


# Charter schools are applied to, not assigned by address, so they are never a town's "assigned"
# public schools (grader, 10/27: Rittenhouse listed Freire Middle School - a charter - as the middle
# school for its addresses). The research names them without the word "charter" ("Freire Middle
# School"; the federal file says "Freire CS"), so they're found in the federal list of every charter
# school (NCES 2024-25, app/data/charter_schools_nces.json, rebuilt from EDGE_ADMINDATA_PUBLICSCH_<year>
# where CHARTER_TEXT = 'Yes'): same state, within CHARTER_NEAR_MILES of the town, the same telling
# words, and grades that fit the level it was listed under.
CHARTER_NEAR_MILES = 10
_CHARTER_PLAIN_WORDS = {"the", "of", "and", "at", "school", "schools", "sch", "charter", "cs", "chs", "pcs", "public", "st", "saint"}
_CHARTER_LEVEL_WORDS = {"elementary": "elementary", "el": "elementary", "primary": "elementary", "middle": "middle", "ms": "middle",
                        "high": "high", "hs": "high", "junior": "middle", "senior": "high", "intermediate": "middle"}
_LEVEL_GRADES = {"elementary_schools": (1, 4), "middle_schools": (6, 8), "high_schools": (9, 12)}


def _charter_rows() -> dict[str, list[list]]:
    global _CHARTERS
    if _CHARTERS is None:
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent.parent / "data" / "charter_schools_nces.json"
        by_state: dict[str, list[list]] = {}
        try:
            with open(path, encoding="utf-8") as f:
                for row in json.load(f)["schools"]:
                    by_state.setdefault(str(row[1]).strip().upper(), []).append(row)
        except (OSError, ValueError, KeyError):
            logger.warning("charter school list missing - charter check skipped")
        _CHARTERS = by_state
    return _CHARTERS


_CHARTERS: Optional[dict[str, list[list]]] = None


def _charter_words(name: str) -> tuple[set[str], set[str]]:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower().replace("'", "").replace("&", " and "))
    levels = {_CHARTER_LEVEL_WORDS[w] for w in words if w in _CHARTER_LEVEL_WORDS}
    return {w for w in words if w not in _CHARTER_PLAIN_WORDS and w not in _CHARTER_LEVEL_WORDS}, levels


def _charter_grade(g) -> Optional[int]:
    g = str(g or "").strip().upper()
    return -1 if g == "PK" else 0 if g == "KG" else int(g) if g.isdigit() else None


def is_charter_school(name: str, state: Optional[str], lat: Optional[float], lng: Optional[float], level_key: Optional[str] = None,
                      local_words: frozenset = frozenset(), district_schools: tuple = ()) -> bool:
    """`local_words`: the town's and its districts' own names - "Princeton Middle School" is the
    district's school, not Princeton Charter School. `district_schools`: the district's own schools
    from the federal file (the town map) - a name found there is the district's."""
    if re.search(r"\bcharter\b", str(name or ""), re.IGNORECASE):
        return True
    if not state or lat is None or lng is None:
        return False
    # "Delta High School (democratic program of choice)" - what's in brackets is a note, not the name.
    words, levels = _charter_words(re.sub(r"\(.*?\)", "", str(name or "")))
    if not words or words <= local_words or any(_charter_words(n)[0] == words for n in district_schools):
        return False
    span = _LEVEL_GRADES.get(level_key or "")
    for row in _charter_rows().get(state.strip().upper(), []):
        c_name, _, c_lat, c_lng, lo, hi = row[:6]
        if abs(c_lat - lat) > 0.3 or abs(c_lng - lng) > 0.4:
            continue
        c_words, c_levels = _charter_words(c_name)
        if c_words != words or (levels and c_levels and not levels & c_levels):
            continue
        if math.hypot((c_lat - lat) * 69.0, (c_lng - lng) * 69.0 * math.cos(math.radians(lat))) > CHARTER_NEAR_MILES:
            continue
        g_lo, g_hi = _charter_grade(lo), _charter_grade(hi)
        if span and g_lo is not None and g_hi is not None and (g_hi < span[0] or g_lo > span[1]):
            continue  # a charter high school doesn't make the district's middle school of the same name one
        return True
    return False


def _without_charters(ps: dict, area: MarketArea) -> dict:
    """The public school lists with charter schools taken off (see is_charter_school), and the
    research's own sentences naming them dropped from the note."""
    from app.services.areas.town_places_check import drop_removed_mentions
    facts = area.facts or {}
    local = " ".join([str(area.name or ""), str(area.county or "")]
                     + [str((d or {}).get("name") or "") for d in (ps.get("districts") or []) if isinstance(d, dict)]
                     + [str((d or {}).get("name") or "") for d in ((facts.get("boundaries") or {}).get("school_districts") or []) if isinstance(d, dict)])
    local_words = frozenset(_charter_words(local)[0])
    district_schools = tuple(str(s.get("name") or "") for s in (((facts.get("boundaries") or {}).get("schools") or {}).get("public") or [])
                             if isinstance(s, dict) and not s.get("charter"))
    gone: list[str] = []
    for lvl in _SCHOOL_LEVELS:
        items = ps.get(lvl)
        if not isinstance(items, list):
            continue
        keep = []
        for x in items:
            if isinstance(x, dict) and is_charter_school(x.get("name"), area.state, area.centroid_lat, area.centroid_lng, lvl, local_words, district_schools):
                gone.append(str(x.get("name")))
                continue
            keep.append(x)
        ps[lvl] = keep
    if gone and isinstance(ps.get("boundary_caveat"), str):
        ps["boundary_caveat"] = drop_removed_mentions(ps["boundary_caveat"], gone)
    return ps


async def reconcile_public_schools(db: AsyncSession, area: MarketArea, area_scope: dict) -> dict:
    """facts.public_schools with its districts checked against Bright (see above). Cached 5 min."""
    facts = area.facts or {}
    # Always from the original research (kept once by check_and_store_public_schools), so running
    # the check again never builds on an earlier checked copy.
    ps = dict(facts.get("public_schools_research") or facts.get("public_schools") or {})
    for k in ("checked_against_listings_at",):
        ps.pop(k, None)
    ps = _without_charters(ps, area)
    hit = _schools_cache.get(area.id)
    now = time.monotonic()
    if hit and now - hit[0] < _SCHOOLS_TTL_S:
        return copy.deepcopy(hit[1])
    try:
        location_filter = geography.location_filter(Property, area_scope)
        rows = []
        if location_filter is not None:
            rows = (await db.execute(
                select(Property.school_district_name, func.count())
                .where(location_filter, Property.school_district_name.isnot(None))
                .group_by(Property.school_district_name)
            )).all()
        total = sum(c for _, c in rows)
        if total < 20:
            _schools_cache[area.id] = (now, ps)
            return copy.deepcopy(ps)
        shares = {}
        for name, count in rows:
            key = _district_key(name)
            if key and key not in ("other pa", "other", "delaware"):
                shares[key] = (shares.get(key, (name, 0))[0], shares.get(key, (name, 0))[1] + count)
        districts = [dict(d) for d in (ps.get("districts") or []) if isinstance(d, dict) and d.get("name")]
        kept = []
        for d in districts:
            k = _district_key(d["name"])
            # Bright spells some districts more than one way ("PHILADELPHIA CITY" and "THE SCHOOL
            # DISTRICT OF PHILADELPHIA") - every spelling that matches counts toward this district.
            matches = [sk for sk in shares if sk == k or sk in k or k in sk]
            share = round(100 * sum(shares[m][1] for m in matches) / total) if matches else 0
            for m in matches:
                shares.pop(m)
            if share == 0 and total >= 100:
                continue
            d["share"] = share
            kept.append(d)
        dropped = {_district_key(d["name"]) for d in districts} - {_district_key(d["name"]) for d in kept}
        levels = {lvl: [dict(x) for x in (ps.get(lvl) or []) if isinstance(x, dict) and _district_key(x.get("district", "")) not in dropped] for lvl in _SCHOOL_LEVELS}
        missing = [(sk, raw, cnt) for sk, (raw, cnt) in shares.items() if cnt / total >= 0.10]
        if missing:
            others = (await db.execute(select(MarketArea).where(MarketArea.id != area.id, MarketArea.state == area.state))).scalars().all()
            for sk, raw, cnt in missing:
                # Every researched town in that district donates its schools for it (not just the
                # first one found), so the list is the district's schools - not one town's zone.
                entry, donors = None, []
                for other in others:
                    ops = (other.facts or {}).get("public_schools") or {}
                    for od in ops.get("districts") or []:
                        ok = _district_key((od or {}).get("name", ""))
                        if ok and (ok == sk or sk in ok or ok in sk):
                            entry = entry or dict(od)
                            donors.append((ops, ok))
                            break
                entry = entry or {"name": f"{raw.title()} School District"}
                entry["share"] = round(100 * cnt / total)
                entry["added_from_listings"] = True
                kept.append(entry)
                for donor_ps, dk in donors:
                    for lvl in _SCHOOL_LEVELS:
                        have = {_school_key(x.get("name")) for x in levels[lvl]}
                        for x in donor_ps.get(lvl) or []:
                            if isinstance(x, dict) and _district_key(x.get("district", "")) == dk and _school_key(x.get("name")) not in have:
                                have.add(_school_key(x.get("name")))
                                levels[lvl].append(dict(x))
        kept.sort(key=lambda d: d.get("share", 0), reverse=True)
        ps["districts"] = kept
        ps.update(levels)
        ps = _without_charters(ps, area)  # schools borrowed from another town's research too
        if len(kept) > 1:
            ps["crosses_district_boundary"] = True
            split = " and ".join(f"{d['name']} ({d.get('share', 0)}% of homes)" for d in kept[:3])
            ps["boundary_caveat"] = f"{area.name} addresses are split between {split} - the exact address decides which schools a home is assigned to."
    except Exception:
        logger.exception("school district check failed for %s", area.slug)
    _schools_cache[area.id] = (now, ps)
    return copy.deepcopy(ps)


async def check_and_store_public_schools(db: AsyncSession, area: MarketArea) -> bool:
    """Saves the Bright-checked school districts (reconcile_public_schools) into the SHARED town
    record, so every feature that reads a town - its page, the compare page, the town cards, the AI
    blog writer, the town write-up - gets the same corrected list, for every agent who serves it.
    The original research is kept once under public_schools_research. Returns True if it changed."""
    from sqlalchemy.orm.attributes import flag_modified
    facts = dict(area.facts or {})
    if not facts.get("public_schools"):
        return False
    _schools_cache.pop(area.id, None)
    checked = await reconcile_public_schools(db, area, geography.scope_for_market_area(area))
    names = lambda ps: [(_district_key(d.get("name", "")), d.get("share")) for d in (ps or {}).get("districts") or []]
    if names(checked) == names(facts.get("public_schools")) and all(len(checked.get(l) or []) == len(facts["public_schools"].get(l) or []) for l in _SCHOOL_LEVELS):
        return False
    facts.setdefault("public_schools_research", facts["public_schools"])
    checked["checked_against_listings_at"] = datetime.now(timezone.utc).isoformat()
    facts["public_schools"] = checked
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return True


def _public_school_districts(facts: dict, fallback: list[str]) -> list[str]:
    public_schools = (facts or {}).get("public_schools") or {}
    districts = public_schools.get("districts") if isinstance(public_schools, dict) else None
    names = [
        str(d.get("name") or "").strip()
        for d in (districts or [])
        if isinstance(d, dict) and str(d.get("name") or "").strip()
    ]
    return names[:3] or fallback[:3]


def _area_hover_facts(facts: dict, stats: dict) -> dict:
    market_bits: list[str] = []
    if stats.get("active_count") is not None:
        market_bits.append(f"{stats['active_count']} homes for sale")
    if stats.get("median_price") is not None:
        market_bits.append(f"${int(stats['median_price']):,} median")
    if stats.get("median_sold_dom") is not None:
        market_bits.append(f"{stats['median_sold_dom']} median days on market")
    elif stats.get("median_dom") is not None:
        market_bits.append(f"listed a median of {stats['median_dom']} days so far")

    downtown = _entity_names(facts, "downtown_centers", 1)
    parks = _entity_names(facts, "parks", 2)
    restaurants = _entity_names(facts, "restaurants", 2)
    shopping = _entity_names(facts, "shopping", 1)
    grocery = _entity_names(facts, "grocery", 1)
    transportation = _entity_names(facts, "transportation", 1)
    market_context = (facts or {}).get("market_context") or {}
    standout = []
    for key in ("summary", "notes"):
        value = str(market_context.get(key) or "").strip() if isinstance(market_context, dict) else ""
        if value:
            standout.append(value)
            break
    for key in ("parks", "restaurants", "downtown_centers", "transportation"):
        notes = str(((facts or {}).get(key) or {}).get("notes") or "").strip()
        if notes:
            standout.append(notes)
        if len(standout) >= 2:
            break

    return {
        "market": market_bits[:3],
        "downtown": downtown,
        "parks": parks,
        "restaurants_shopping": (restaurants + shopping)[:3],
        "grocery": grocery,
        "transportation": transportation,
        "standout": standout[:2],
    }


# The served-areas cards (cover photo + live stats per town) cost a query per town against the
# whole MLS mirror - seconds for an agent with several towns - and every public page asks for
# them (footer, Neighborhoods, home). Kept in memory for 5 minutes, the same rhythm as the Bright
# sync, so the numbers are never more than one sync behind.
_SERVED_AREAS_TTL_S = 300
_served_areas_cache: dict[tuple, tuple[float, list[dict]]] = {}


_market_cache: dict[str, tuple[float, object]] = {}


_MARKET_STALE_OK_S = 6 * 3600
_market_refreshing: set[str] = set()


def _market_store(key: str, value) -> None:
    _market_cache[key] = (time.monotonic(), value)
    from app.services import app_cache
    app_cache.save_bg(f"mkt:{key}", value)  # survives a restart (preload_persistent_caches)
    if len(_market_cache) > 2000:
        for k in sorted(_market_cache, key=lambda k: _market_cache[k][0])[:500]:
            _market_cache.pop(k, None)


async def cached_market(key: str, make, db: Optional[AsyncSession] = None):
    """Live market numbers (town stats, trends, related towns) for town pages, refreshed every 5
    minutes - the Bright sync's rhythm. `make(db)` computes them with the session it's given.

    Stale-while-revalidate (user, 10/17: neighborhood pages "taking forever"; one took 27 s on the
    live site): after 5 minutes the last numbers are still served instantly and recomputed in the
    background on their own session, so no visitor waits on the math. Only a town nobody has
    opened for 6 hours (or since a restart - see warm_area_caches) is computed while waiting."""
    hit = _market_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _SERVED_AREAS_TTL_S:
        return copy.deepcopy(hit[1])
    if hit and now - hit[0] < _MARKET_STALE_OK_S:
        if key not in _market_refreshing:
            _market_refreshing.add(key)

            async def refresh():
                from app.database import AsyncSessionLocal
                try:
                    async with AsyncSessionLocal() as fresh:
                        _market_store(key, await make(fresh))
                except Exception:
                    logger.warning("market cache refresh failed", extra={"key": key}, exc_info=True)
                finally:
                    _market_refreshing.discard(key)
            task = asyncio.create_task(refresh())
            _market_tasks.add(task)
            task.add_done_callback(_market_tasks.discard)
        return copy.deepcopy(hit[1])
    value = await make(db)
    _market_store(key, value)
    return copy.deepcopy(value)


_market_tasks: set = set()


async def served_area_names(db: AsyncSession, user: User, limit: int = 50) -> list[str]:
    """Just the agent's served town names (search scoping) - no photos or stats."""
    result = await db.execute(
        select(MarketArea.name)
        .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == user.id)
        .order_by(MarketArea.name)
        .limit(limit)
    )
    return [n for n in result.scalars().all() if n]


async def get_served_areas_summary(db: AsyncSession, user: User, limit: int = 15, with_stats: bool = False) -> list[dict]:
    key = (str(user.id), limit, with_stats)
    hit = _served_areas_cache.get(key)
    now = time.monotonic()
    if hit:
        # Past 5 minutes: hand back the last copy instantly and rebuild it in the background,
        # so a visitor never waits on the rebuild.
        if now - hit[0] >= _SERVED_AREAS_TTL_S and key not in _served_areas_refreshing:
            _served_areas_refreshing.add(key)
            asyncio.create_task(_refresh_served_areas(key, str(user.id), limit, with_stats))
        return copy.deepcopy(hit[1])
    out = await _build_served_areas_summary(db, user, limit, with_stats)
    _served_store(key, out)
    return copy.deepcopy(out)


def _served_store(key: tuple, value: list[dict]) -> None:
    _served_areas_cache[key] = (time.monotonic(), value)
    from app.services import app_cache
    app_cache.save_bg(f"served:{key[0]}:{key[1]}:{int(bool(key[2]))}", value)


async def preload_persistent_caches() -> int:
    """At startup, before the first request: the last saved market numbers and served-areas cards
    go back into memory, marked due for a refresh - so the first visitor after a deploy gets them
    instantly (refreshed in the background) instead of waiting while they're rebuilt."""
    from app.services import app_cache
    saved = await app_cache.load_all(("mkt:", "served:"))
    now = time.monotonic()
    for k, v in saved.items():
        if k.startswith("mkt:"):
            _market_cache.setdefault(k[4:], (now - _SERVED_AREAS_TTL_S - 1, v))
        else:
            _, uid, limit, stats = k.split(":", 3)
            _served_areas_cache.setdefault((uid, int(limit), stats == "1"), (now - _SERVED_AREAS_TTL_S - 1, v))
    return len(saved)


_served_areas_refreshing: set[tuple] = set()


async def _refresh_served_areas(key: tuple, user_id: str, limit: int, with_stats: bool) -> None:
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                _served_store(key, await _build_served_areas_summary(db, user, limit, with_stats))
    except Exception:
        logger.exception("served areas refresh failed")
    finally:
        _served_areas_refreshing.discard(key)


async def _build_served_areas_summary(db: AsyncSession, user: User, limit: int = 15, with_stats: bool = False) -> list[dict]:
    """For the Communities block / Communities index page - name/slug/photo/one-line teaser by
    default, not the full facts payload (that's fetched per-area on the community page itself).
    `with_stats=True` (Communities index only, where the agent's served-area count is small - a
    handful of towns, never a full-MLS-sized set) adds a live median-price/active-count card
    stat per area via the same request-time market_stats_service query the community page uses,
    so the index never shows numbers that drift from what the community page itself says."""
    result = await db.execute(
        select(MarketArea)
        .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == user.id)
        .order_by(*SERVED_AREA_ORDER)
        .limit(limit)
    )
    areas = result.scalars().all()
    return [await area_summary_card(db, a, with_stats) for a in areas]


# The agent's own town order (Areas list arrows), then by name for towns never ordered - the
# Areas list and every town list on the site use this, so they always match.
SERVED_AREA_ORDER = (AgentMarketArea.sort_order.asc().nullslast(), MarketArea.name)


def forget_served_areas(user_id: str) -> None:
    """Drop this agent's cached town cards (this worker) so a reorder shows on the next visit."""
    for key in [k for k in _served_areas_cache if k[0] == str(user_id)]:
        _served_areas_cache.pop(key, None)


async def area_summary_card(db: AsyncSession, a: MarketArea, with_stats: bool = False) -> dict:
    """One town's card in the served-areas list. Also used on its own by the admin's town preview
    for a town no agent serves yet (admin_area_routes.preview_town_page)."""
    area_scope = geography.scope_for_market_area(a)
    # Photo + stats go through the town pages' shared market cache (same keys warm_area_caches
    # fills): built cold they took ~20 s for 8 towns - the first home-page visit after a restart.
    photo = await cached_market(f"photo:{a.id}", lambda d, sc=area_scope: _area_top_listing_photo(d, sc), db)
    if not photo:
        resolved_photos = resolve_photo_urls((a.facts or {}).get("photos"))
        items = (resolved_photos or {}).get("items") or []
        photo = items[0]["url"] if items else None
    card = {
        "slug": a.slug,
        "name": a.name,
        "state": a.state,
        "county": a.county,
        "photo": photo,
    }
    # The card says what the town page says (fair_housing_service.town_display).
    from app.services.areas.fair_housing_service import town_display
    shown_facts, shown_narrative = town_display(a)
    card["summary"] = (shown_narrative or {}).get("summary")
    if with_stats:
        stats = await cached_market(f"stats:{a.id}", lambda d, sc=area_scope, ms=(a.facts or {}).get("market_stats"): get_area_live_market_stats(d, sc, ms), db)
        card["median_price"] = stats.get("median_price")
        card["active_count"] = stats.get("active_count")
        card["min_price"] = stats.get("min_price")
        card["max_price"] = stats.get("max_price")
        card["price_range_basis"] = stats.get("price_range_basis")
        checked = await reconcile_public_schools(db, a, area_scope) if (a.facts or {}).get("public_schools") else None
        card["school_districts"] = _public_school_districts({"public_schools": checked} if checked else a.facts, stats.get("school_districts") or [])
        card["hover_facts"] = _area_hover_facts(shown_facts, stats)
    return card


async def get_forms_list(db: AsyncSession, user: User) -> list[dict]:
    """For the website editor's form picker - agents choose an existing form, they don't build
    a new one here. Mirrors what /forms/me already returns, trimmed to what the picker needs."""
    result = await db.execute(
        select(AgentForm).where(AgentForm.owner_id == user.id, AgentForm.is_enabled.is_(True)).order_by(AgentForm.order_index)
    )
    return [{"id": f.id, "slug": f.slug, "title": f.title, "form_type": f.form_type} for f in result.scalars().all()]


def _as_int(value) -> Optional[int]:
    # Studio editor / search box send these as free-text - never trust it's already an int.
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _build_property_preferences(
    cities: Optional[list[str]] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_beds: Optional[int] = None,
    min_baths: Optional[float] = None,
    school_district: Optional[str] = None,
    property_type_flag: Optional[str] = None,
) -> CollectionPreferencesBase:
    """Shared builder behind every full-MLS search in this file (get_hero_photos,
    get_search_results) - a thin wrapper around CollectionPreferencesBase so both callers stay
    in sync with PropertyService.get_properties_by_preferences's actual filter vocabulary."""
    prefs = CollectionPreferencesBase()
    if cities:
        prefs.cities = cities
    if min_price is not None:
        prefs.min_price = min_price
    if max_price is not None:
        prefs.max_price = max_price
    if min_beds is not None:
        prefs.min_beds = min_beds
    if min_baths is not None:
        prefs.min_baths = min_baths
    if school_district:
        prefs.school_districts = [school_district]
    if property_type_flag:
        setattr(prefs, property_type_flag, True)
    return prefs


async def get_hero_photos(db: AsyncSession, cities: list[str], criteria: dict) -> list[str]:
    """For the Hero block's 'auto' photo mode (Phase 5) - deliberately one of the few content
    adapters in this file that does NOT scope to the requesting agent's own listings. Every other
    function here reads only what a given user.mls_id owns; hero background photos are real
    IDX behavior (see the Phase 5 plan) and search the full regional MLS mirror the same way
    /api/discovery already does, via the existing PropertyService.get_properties_by_preferences.
    Never copy this un-scoped pattern into a function that returns anything beyond a public
    listing photo.

    `cities` comes from the agent's already-configured served areas (AgentMarketArea, same data
    the Communities block/AreasPanel already use) - an agent never re-types a location here, only
    optional price/type narrowing in `criteria`. If they haven't set up any served areas yet,
    `cities` is empty and the search runs unfiltered by location across the full mirror."""
    flag = _HERO_PROPERTY_TYPE_FLAGS.get(criteria.get("property_type"))
    prefs = _build_property_preferences(
        cities=cities,
        min_price=_as_int(criteria.get("price_min")),
        max_price=_as_int(criteria.get("price_max")),
        property_type_flag=flag,
    )
    results = await PropertyService.get_properties_by_preferences(db, prefs, max_properties=10)
    cards = [{"listing_key": p.listing_key, "photo": p.img_src} for p in results
             if p.img_src and geography.is_residential_for_sale(getattr(p, "home_type", None), getattr(p, "price", None))]
    return [c["photo"] for c in await apply_idx_rules(db, cards)][:5]


async def get_header_listings(db: AsyncSession, user: User, served_cities: list[str], data: dict, limit: int = 6) -> list[dict]:
    """The homes in the home page header (the Hero block): the NEWEST homes for sale in the towns
    the agent picks (default: every town they serve, the Areas tab), inside their price range and
    home type - refreshed on every visit, so the header is always the latest listings. The
    agent's own matching listings lead when `header_mine_first` (default on). Same full-MLS
    exception as get_hero_photos. Block fields: header_towns, photo_price_min/max,
    photo_property_type, header_mine_first."""
    picked = [t for t in (data.get("header_towns") or []) if isinstance(t, str) and t.strip()]
    towns = picked or served_cities
    flag = _HERO_PROPERTY_TYPE_FLAGS.get(data.get("photo_property_type"))
    min_price, max_price = _as_int(data.get("photo_price_min")), _as_int(data.get("photo_price_max"))
    prefs = _build_property_preferences(cities=towns or None, min_price=min_price, max_price=max_price, property_type_flag=flag)
    results = await PropertyService.get_properties_by_preferences(db, prefs, max_properties=40)
    results = [p for p in results if getattr(p, "img_src", None) and geography.is_residential_for_sale(getattr(p, "home_type", None), getattr(p, "price", None))]
    cards = await apply_idx_rules(db, [_listing_card(p) for p in results])
    # Newest first - fewest days on the market (unknown days sort after known ones).
    cards.sort(key=lambda c: (c.get("days_on_market") is None, c.get("days_on_market") or 0))
    if data.get("header_mine_first", True):
        own = [c for c in await get_active_listings(db, user, 12) if c.get("photo")]
        own = [c for c in own if (not towns or (c.get("city") or "").lower() in {t.lower() for t in towns})
               and (min_price is None or (c.get("price") or 0) >= min_price) and (max_price is None or (c.get("price") or 0) <= max_price)]
        seen = {c["listing_key"] for c in own}
        cards = own + [c for c in cards if c["listing_key"] not in seen]
    cards = cards[:limit]
    return await _attach_spotlight_urls(db, user, await _backfill_photo_arrays(db, cards))


_CITY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,40}$")


async def _sold_cities(db: AsyncSession, user: User, limit: int) -> list[str]:
    """Distinct cities from this agent's own real closed Bright MLS sales (same match rule as
    get_recent_sales - see its docstring), most-frequent first. Used only to ground the hero
    search box's example suggestions in inventory this agent has actually sold, never a guess.
    Deliberately Bright-only (`is_manual=False`) - manually-entered "My Listings" rows are often
    placeholder/test data with unreliable city spelling, and the search box's own city matching
    (search_query_parser) only ever recognizes real Bright-synced/served-area city names anyway."""
    if not user.mls_id:
        return []
    mls_id = user.mls_id.strip()
    identifier_keys = ["BuyerAgentMlsId", "CoBuyerAgentMlsId", "ListAgentMlsId", "CoListAgentMlsId"]
    filters = [Property.raw_mls_data[key].astext == mls_id for key in identifier_keys]
    filters.append(Property.list_agent_mls_id == mls_id)
    filters.append(Property.co_list_agent_mls_id == mls_id)
    result = await db.execute(
        select(Property.city, func.count(Property.id).label("n"))
        .where(or_(*filters), Property.city.isnot(None), Property.is_manual.is_(False))
        .group_by(Property.city)
        .order_by(func.count(Property.id).desc())
        .limit(limit * 3)  # over-fetch, garbled MLS city values get filtered below
    )
    return [row[0] for row in result.all() if row[0] and _CITY_NAME_RE.match(row[0].strip())][:limit]


def _format_price_band(price: Optional[float]) -> str:
    # Rounds a real listing price up to a clean, round example ceiling (e.g. $3.395M -> "$3.5M"),
    # 15% headroom so the example query itself would actually match that real listing. Clamped to
    # a plausible residential band regardless of source data - a garbled/mis-keyed MLS price
    # (data entry errors happen upstream) must never surface a nonsense example on a public site.
    if not price:
        return "$1M"
    ceiling = min(max(math.ceil((price * 1.15) / 250000) * 250000, 250_000), 5_000_000)
    if ceiling >= 1_000_000:
        return f"${ceiling / 1_000_000:g}M"
    return f"${int(ceiling / 1000)}K"


# Per agent, 30 min: the hero's example searches are placeholder text, and building them runs one
# un-indexable city ILIKE scan of the whole MLS table per city (~0.5 s of the home page's 0.55 s
# server time, 10/19 sweep).
_SUGGESTIONS_CACHE: dict[tuple, tuple[float, list[str]]] = {}
_SUGGESTIONS_TTL = 30 * 60


async def get_search_suggestions(db: AsyncSession, user: User, limit: int = 3) -> list[str]:
    """Rotating example queries shown under the hero search box - grounded in cities this agent
    has actually sold in (falls back to served areas, then a generic default) and a real nearby
    listing's price/bed/lot size, so every example describes real local inventory an agent could
    plausibly be asked about, never a fabricated city or number."""
    key = (user.id, limit)
    hit = _SUGGESTIONS_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _SUGGESTIONS_TTL:
        return list(hit[1])
    result = await _build_search_suggestions(db, user, limit)
    if len(_SUGGESTIONS_CACHE) > 500:
        _SUGGESTIONS_CACHE.clear()
    _SUGGESTIONS_CACHE[key] = (time.monotonic(), result)
    return list(result)


async def _build_search_suggestions(db: AsyncSession, user: User, limit: int) -> list[str]:
    cities = await _sold_cities(db, user, limit * 2)
    if not cities:
        cities = await served_area_names(db, user, limit * 2)
    if not cities:
        return ["4 bed homes under $1M", "homes with 1+ acre", "new listings this week"]

    suggestions: list[str] = []
    for city in cities[:limit]:
        result = await db.execute(
            select(Property)
            .where(Property.city.ilike(city), Property.price.isnot(None))
            .order_by(Property.mls_list_date.desc().nullslast())
            .limit(1)
        )
        sample = result.scalars().first()
        beds = min(max(int(sample.bedrooms), 2), 6) if sample and sample.bedrooms else 4
        price_label = _format_price_band(sample.price if sample else None)
        has_acre = bool(sample and sample.lot_size_acres and sample.lot_size_acres >= 0.5)
        text = f"{beds} bed home in {city.title()} under {price_label}"
        if has_acre:
            text += " with 1 acre"
        suggestions.append(text)
    return suggestions


def _search_result_card(p) -> dict:
    return {
        "listing_key": p.listing_key,
        # Street only - see matching comment in _listing_card.
        "address": tidy_caps(p.street_address or p.full_address),
        "city": tidy_caps(p.city),
        "state": p.state,
        "zipcode": p.zipcode,
        "price": p.price,
        "bedrooms": p.bedrooms,
        "bathrooms": p.bathrooms,
        "living_area": p.living_area,
        "lot_size_acres": p.lot_size_acres,
        "status": p.home_status,
        "photo": p.img_src,
        "latitude": p.latitude,
        "longitude": p.longitude,
        "open_houses": open_houses_payload(p),
        "list_office_name": getattr(p, "list_office_name", None),
    }


_ADDRESS_LIKE_RE = re.compile(r'^\s*\d+\s+\S')


async def _address_search(db: AsyncSession, query_text: str, limit: int) -> list:
    """A literal street-address search (e.g. "422 Bryn Mawr Ave") - the free-text box's
    deterministic parser (search_query_parser) only ever extracts price/beds/city/school-district
    style facts out of natural language, it was never meant to locate one specific address.
    Real full-MLS substring match on the same `properties` table every other search function here
    reads, bounded the same way. Only triggered when the query looks address-shaped (starts with
    a house number) so an ordinary "4 bed homes in Wayne" query never accidentally matches this
    branch instead of the normal filtered search below."""
    if not _ADDRESS_LIKE_RE.match(query_text or ''):
        return []
    result = await db.execute(
        select(Property)
        .where(
            Property.full_address.ilike(f"%{query_text.strip()}%"),
            Property.home_status.notin_(INACTIVE_STATUSES),
            internet_display_ok(),
            # a seller who hid the address must not be findable by typing it
            func.coalesce(Property.raw_mls_data["InternetAddressDisplayYN"].astext, "true") != "false",
        )
        .order_by(Property.mls_list_date.desc().nullslast())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_search_results(
    db: AsyncSession, user: User, query_text: str, limit: int = 24,
    min_price: Optional[int] = None, max_price: Optional[int] = None,
    min_beds: Optional[int] = None, min_baths: Optional[float] = None,
    property_type: Optional[str] = None, min_sqft: Optional[int] = None,
    school_district: Optional[str] = None, sort: str = "newest",
    include_rentals: bool = False,
    search_polygon: Optional[dict] = None,
    max_sqft: Optional[int] = None, min_acres: Optional[float] = None,
    min_year_built: Optional[int] = None,
) -> dict:
    """The public Search tab's results - same full-MLS exception as get_hero_photos above, see
    that docstring. `query_text` is parsed deterministically, no AI/LLM call (see
    search_query_parser.py), for the hero's free-text search box - a query that looks like a
    street address (starts with a house number) instead runs a direct address match (see
    _address_search) rather than the natural-language filter parser, which was never meant to
    locate one specific property. The explicit keyword params below back the Search tab's own
    Zillow-style filter bar and always take precedence over whatever was parsed from query_text
    when both are present, so a filter a visitor explicitly picked is never silently overridden by
    a looser natural-language guess.

    Returns `{"results": [...], "applied_filters": {...}}` rather than a bare list -
    `applied_filters` is the *effective* filter values (explicit param, else whatever
    query_text's free-text parser inferred), so the Search tab's filter bar can visibly reflect
    what a "2 beds over 3M radnor schools"-style query actually matched against, not just leave
    every field blank because nothing was explicitly clicked."""
    # A drawn area (map "Draw") is the "where" - it replaces the served-towns default and any town
    # in the typed query; every other filter still applies. More homes, since it's a map.
    if search_polygon:
        limit = max(limit, 60)
    address_matches = [] if search_polygon else await _address_search(db, query_text, limit)
    if address_matches:
        cards = [_search_result_card(p) for p in address_matches]
        if sort == "price_desc":
            cards.sort(key=lambda c: c.get("price") or 0, reverse=True)
        elif sort == "price_asc":
            cards.sort(key=lambda c: c.get("price") or 0)
        return {"results": cards[:limit], "applied_filters": {}}

    known_cities = await served_area_names(db, user, 50)
    parsed = search_query_parser.parse_search_query(query_text, known_cities)

    # No city recognized in the query (including a blank query, e.g. landing on /search directly
    # instead of typing something on the home page first) - default to the agent's own served
    # areas rather than an unscoped search across the entire regional MLS mirror, same "known
    # vocabulary only" principle as the rest of this function.
    cities = None if search_polygon else ([parsed["city"]] if parsed.get("city") else (known_cities or None))
    # Home type can be several at once ("single_family,town_house") - same as a Showcase.
    type_flags = [f for f in (_HERO_PROPERTY_TYPE_FLAGS.get(t.strip()) for t in (property_type or "").split(",")) if f]
    flag = (type_flags[0] if type_flags else None) or parsed.get("property_type_flag")

    # Effective (explicit param wins, else whatever query_text's parser inferred) - computed once
    # so both the query builder below and applied_filters (returned to the frontend so the filter
    # bar can visibly reflect a "2 beds over 3M radnor schools"-style query) stay in sync.
    eff_min_price = min_price if min_price is not None else _as_int(parsed.get("min_price"))
    eff_max_price = max_price if max_price is not None else _as_int(parsed.get("max_price"))
    eff_min_beds = min_beds if min_beds is not None else parsed.get("min_beds")
    eff_min_baths = min_baths if min_baths is not None else parsed.get("min_baths")
    # The dedicated school-district filter (a real district name chosen from the same
    # autocomplete FeaturedSettingsPanel/Showcase use) always wins over the free-text parser's
    # crude "word before 'school'" guess - both are substring-matched against
    # school_district_name (see PropertyService.get_properties_by_preferences), so either a
    # short colloquial name ("radnor") or the full legal name matches.
    eff_school_district = school_district or parsed.get("school_district")

    prefs = _build_property_preferences(
        cities=cities,
        min_price=eff_min_price,
        max_price=eff_max_price,
        min_beds=eff_min_beds,
        min_baths=eff_min_baths,
        school_district=eff_school_district,
        property_type_flag=flag,
    )
    if search_polygon:
        prefs.search_polygon = search_polygon
    for extra_flag in type_flags[1:]:
        setattr(prefs, extra_flag, True)
    # The same size / lot / age filters a Showcase has, applied in the database query.
    if min_sqft:
        prefs.min_sqft = min_sqft
    if max_sqft:
        prefs.max_sqft = max_sqft
    if min_year_built:
        prefs.min_year_built = min_year_built

    # Lot-size (acres, from free-text only), sqft (filter bar), and the for-sale/rental split
    # aren't wired into the shared query builder (see its own docstring caveat), so fetch a wider
    # batch and filter those dimensions here rather than modify a function several other features
    # already depend on. Rentals (home_type RESIDENTIAL_LEASE) are excluded by default - a
    # visitor searching a real estate site expects for-sale homes unless they explicitly flip the
    # "Rentals" toggle, but get_properties_by_preferences includes every type when no is_XXX flag
    # is set, since that's also its correct behavior for callers that DO want every type mixed.
    min_acres = min_acres if min_acres else parsed.get("min_lot_size_acres")
    needs_post_filter = min_acres is not None or not include_rentals
    fetch_cap = max(limit * 3, 60) if needs_post_filter else limit
    results = await PropertyService.get_properties_by_preferences(db, prefs, max_properties=fetch_cap)
    if min_acres is not None:
        results = [r for r in results if (r.lot_size_acres or 0) >= min_acres]
    if include_rentals:
        # Rentals in, but never commercial space / land / data-entry junk.
        results = [r for r in results if r.home_type == "RESIDENTIAL_LEASE" or geography.is_residential_for_sale(r.home_type, r.price)]
    else:
        results = [r for r in results if r.home_type != "RESIDENTIAL_LEASE" and geography.is_residential_for_sale(r.home_type, r.price)]

    cards = await apply_idx_rules(db, [_search_result_card(p) for p in results])
    if _ADDRESS_LIKE_RE.match(query_text or ''):
        # an address typed in fell through to the general search - a home whose seller withheld
        # its address must not answer "is it this house?"
        cards = [c for c in cards if not c.get("address_withheld")]
    if sort == "price_desc":
        cards.sort(key=lambda c: c.get("price") or 0, reverse=True)
    elif sort == "price_asc":
        cards.sort(key=lambda c: c.get("price") or 0)
    elif sort == "sqft_desc":
        cards.sort(key=lambda c: c.get("living_area") or 0, reverse=True)

    property_type_value = ",".join(t.strip() for t in (property_type or "").split(",") if t.strip() in _HERO_PROPERTY_TYPE_FLAGS) or next((k for k, v in _HERO_PROPERTY_TYPE_FLAGS.items() if v == flag), None)
    applied_filters = {
        "min_price": eff_min_price,
        "max_price": eff_max_price,
        "min_beds": eff_min_beds,
        "min_baths": eff_min_baths,
        "school_district": eff_school_district,
        "property_type": property_type_value,
        "min_sqft": min_sqft,
        "max_sqft": max_sqft,
        "min_acres": min_acres,
        "min_year_built": min_year_built,
        "area": bool(search_polygon),
        # The towns this search actually covered (typed town, else the agent's served towns) -
        # "Save this search" hands them to the Showcase so its alerts cover the same ground.
        "cities": [] if search_polygon else (cities or []),
    }
    return {"results": await _attach_spotlight_urls(db, user, cards[:limit]), "applied_filters": applied_filters}


_AREA_SOLD_STATUSES = {"CLOSED-BRIGHT", "CLOSED"}
_AREA_SORTS = {
    "newest": Property.mls_list_date.desc().nullslast(),
    "price_desc": Property.price.desc().nullslast(),
    "price_asc": Property.price.asc().nullslast(),
}


async def get_area_inventory(
    db: AsyncSession, area_scope: dict | list[str], limit: int = 24, offset: int = 0,
    min_price: Optional[int] = None, max_price: Optional[int] = None, sort: str = "newest",
) -> tuple[list[dict], int]:
    """The broader "Homes for Sale in {Community}" inventory - deliberately NOT scoped to any one
    agent (same full-MLS exception as get_hero_photos/get_search_results, see that docstring).
    Geographic scoping uses the shared MarketArea definition: city/town areas use their stored
    ZIPs (with city fallback only if ZIPs are unavailable), while neighborhoods use a stored
    coordinate definition from Places resolution or the MarketArea centroid. That keeps
    Rittenhouse Square from showing all of Philadelphia/19103 while preserving the town behavior
    existing pages already relied on.

    This reads the `properties` table directly at request time - not a cached/baked snapshot -
    so a new listing, a price change, or a status flip shows up on the community page immediately
    the next time it's requested, with no site republish needed. Pagination is real SQL
    LIMIT/OFFSET (not an in-memory slice), and a COUNT query runs alongside so the caller can
    render "142 homes" / page through the full set without ever loading it all into memory."""
    if isinstance(area_scope, list):
        area_scope = geography.zip_scope(area_scope)
    location_filter = geography.location_filter(Property, area_scope)
    if location_filter is None:
        return [], 0

    filters = [
        location_filter,
        Property.home_status.notin_(INACTIVE_STATUSES),
        geography.residential_for_sale_filter(Property),
        internet_display_ok(),
    ]
    if min_price is not None:
        filters.append(Property.price >= min_price)
    if max_price is not None:
        filters.append(Property.price <= max_price)

    total = (await db.execute(select(func.count(Property.id)).where(*filters))).scalar_one()

    order = _AREA_SORTS.get(sort, _AREA_SORTS["newest"])
    result = await db.execute(select(Property).where(*filters).order_by(order).offset(offset).limit(limit))
    return await apply_idx_rules(db, [_listing_card(p) for p in result.scalars().all()]), total


async def get_area_recent_market_sales(db: AsyncSession, area_scope: dict | list[str], limit: int = 6) -> list[dict]:
    """The latest closed sales in an area across the whole MLS (not just this agent's) - what a
    buyer or seller actually wants to see on a neighborhood page. Residential only, newest close
    date first, with the real close price."""
    if isinstance(area_scope, list):
        area_scope = geography.zip_scope(area_scope)
    location_filter = geography.location_filter(Property, area_scope)
    if location_filter is None:
        return []
    close_date = Property.raw_mls_data["CloseDate"].astext
    result = await db.execute(
        select(Property)
        .where(location_filter, Property.home_status.in_(_AREA_SOLD_STATUSES), geography.residential_for_sale_filter(Property), close_date.isnot(None), internet_display_ok())
        .order_by(close_date.desc())
        .limit(limit)
    )
    out = []
    for p in result.scalars().all():
        card = _listing_card(p)
        raw = p.raw_mls_data or {}
        try:
            close_price = float(raw.get("ClosePrice")) if raw.get("ClosePrice") else None
        except (TypeError, ValueError):
            close_price = None
        card["sold_price"] = close_price or p.price
        card["sold_date"] = raw.get("CloseDate")
        out.append(card)
    return await apply_idx_rules(db, out)


async def get_area_live_market_stats(db: AsyncSession, area_scope: dict | list[str], fallback: Optional[dict] = None) -> dict:
    """Market stats computed fresh from the current `properties` table on every request, not the
    one-time snapshot area_enrichment_service bakes into MarketArea.facts.market_stats at
    enrichment time - the whole point of a community page is that it stays live as the Bright
    sync updates prices/statuses, so stats shown here must never go stale between re-enrichments.
    Falls back to the baked snapshot only if the live query finds nothing (e.g. a brand-new area
    whose stored geography doesn't yet match any local properties)."""
    if isinstance(area_scope, list):
        area_scope = geography.zip_scope(area_scope)
    live = await market_stats_service.get_area_market_stats(db, area_scope)
    return live or fallback or {}


async def get_area_related(db: AsyncSession, user: User, exclude_area_id: str, limit: int = 3) -> list[dict]:
    """Other communities this same agent serves, for the community page's "Explore Nearby
    Areas" section - real served areas only, never a guessed/nearby-by-distance list."""
    result = await db.execute(
        select(MarketArea)
        .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == user.id, MarketArea.id != exclude_area_id)
        .order_by(MarketArea.name)
        .limit(limit)
    )
    out = []
    for a in result.scalars().all():
        area_scope = geography.scope_for_market_area(a)
        photo = await _area_top_listing_photo(db, area_scope)
        if not photo:
            resolved_photos = resolve_photo_urls(a.facts.get("photos"))
            items = (resolved_photos or {}).get("items") or []
            photo = items[0]["url"] if items else None
        out.append({
            "slug": a.slug, "name": a.name, "state": a.state,
            "photo": photo,
            "median_price": (a.facts.get("market_stats") or {}).get("median_price"),
        })
    return out


async def get_public_mls_property_detail(db: AsyncSession, listing_key: str) -> Optional[dict]:
    """Full detail for a search-result listing - unlike get_property_detail, this is NOT scoped
    to any one agent (same full-MLS exception as get_hero_photos/get_search_results above). Only
    ever reached from a search-result click-through (public_site_routes' /search/properties/{key}
    route), never guessable/linked from an agent's own site nav."""
    p = await PropertyService.get_property_by_listing_key(db, listing_key)
    if not p or p.home_status in _IDX_NOT_SHOWN_STATUSES:
        return None

    card = _search_result_card(p)
    # The detail model above doesn't carry the open house columns - read them off the row.
    oh_row = (await db.execute(
        select(Property.mls_open_houses, Property.mls_open_house_date, Property.mls_open_house_start_time, Property.mls_open_house_end_time)
        .where(Property.listing_key == str(listing_key))
    )).first()
    card["open_houses"] = open_houses_payload(oh_row) if oh_row else []
    card["expected_on_market_date"] = (await db.execute(
        select(Property.raw_mls_data["ExpectedOnMarketDate"].astext).where(Property.listing_key == str(listing_key))
    )).scalar()
    card.update({
        "description": _fix_mojibake(p.description),
        "photos": p.photos if isinstance(p.photos, list) else ([card["photo"]] if card["photo"] else []),
        "lot_size": p.lot_size,
        "year_built": p.year_built,
        "price_per_sqft": p.price_per_square_feet,
        "school_district": p.school_district_name,
        "has_hoa": p.has_association,
        "days_on_market": property_days_on_market(p),
        "mls_number": p.listing_id,
        "list_office_name": p.list_office_name,
        "list_office_phone": p.list_office_phone,
        "list_agent_full_name": p.list_agent_full_name,
        "list_agent_email": p.list_agent_email,
        "list_agent_phone": p.list_agent_preferred_phone,
        "features": _feature_list(p.interior_features, p.exterior_features, p.appliances),
        # See get_property_detail's identical fields - Bright has no dedicated price-history
        # resource, so original-vs-current is the full extent of what's trackable.
        "original_list_price": p.original_list_price,
        "price_change_timestamp": p.price_change_timestamp.isoformat() if p.price_change_timestamp else None,
        "mls_list_date": p.mls_list_date.isoformat() if p.mls_list_date else None,
    })
    card["report"] = await _full_report(db, listing_key)
    from app.services.price_history_service import get_history
    card["price_history"] = await get_history(db, listing_key)
    allowed = await apply_idx_rules(db, [card])
    return allowed[0] if allowed else None


async def get_brand_kit_theme(db: AsyncSession, user: User) -> Optional[dict]:
    result = await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id))
    bk = result.scalar_one_or_none()
    if not bk:
        return None
    return {
        "primary_color": bk.primary_color,
        "secondary_color": bk.secondary_color,
        "accent_color": bk.accent_color,
        "heading_font": bk.heading_font,
        "body_font": bk.body_font,
        "logo_url": bk.personal_logo_url or bk.team_logo_url or bk.brokerage_logo_url,
    }


_DEFAULT_LIMITS = {
    "featured_listings": 6,
    "recent_sales": 6,
    "open_houses": 4,
    "blog_posts": 3,
    "communities": 6,
    "newly_listed": 8,
    "spotlights": 6,
}
# Cap for 'selected' mode - an agent hand-picking specific items is still bounded, never an
# unbounded table scan, while comfortably covering any realistic hand-picked selection size.
_SELECTED_MODE_FETCH_CAP = 60


async def hydrate_page_blocks(db: AsyncSession, user: User, page: dict, featured_settings: Optional[dict] = None) -> dict:
    """Resolves every OHP-data-backed block on a single page into a `resolved` payload the
    public block components can render directly. Scoped to ONE page on purpose - the caller
    (public_site_routes.resolve_site) only hydrates the page actually being viewed, not every
    page in the site, so a request never pays for content the visitor isn't looking at."""
    blocks = page.get("blocks") or []
    types_present = {b.get("type") for b in blocks if not b.get("hidden")}
    fetched: dict[str, list[dict]] = {}
    # featured_listings is deliberately NOT batch-fetched here like the others - it shares the
    # Featured (/properties) tab's own settings (site-level Site.draft_data.featured_settings,
    # see get_featured_page_listings) rather than having its own separate pool, so the homepage
    # widget and the full Featured page always agree on what "featured" means for this agent -
    # the fetch has to happen per-block, inside resolve_block below, once we know the block is
    # actually present.
    if "recent_sales" in types_present:
        # the Agent Hub's minimum price + order (setup flow's Sales step) apply on the website too
        fetched["recent_sales"] = apply_sales_rules(await get_recent_sales(db, user, _SELECTED_MODE_FETCH_CAP), await hub_sales_rules(db, user))
    if "open_houses" in types_present:
        fetched["open_houses"] = await get_open_houses(db, user, _SELECTED_MODE_FETCH_CAP, upcoming_only=True)
    if "blog_posts" in types_present:
        fetched["blog_posts"] = await get_blog_posts(db, user, _SELECTED_MODE_FETCH_CAP)
    if "communities" in types_present:
        # with_stats=True - the homepage card now reveals real active-count/price-range/school-
        # district stats on hover (see CommunitiesBlock), same live numbers the Communities index
        # itself shows, not a second, potentially-stale copy.
        fetched["communities"] = await get_served_areas_summary(db, user, _SELECTED_MODE_FETCH_CAP, with_stats=True)
    if "newly_listed" in types_present:
        # Same source as Featured Listings' own-inventory pool (get_active_listings, already
        # newest-first) - a distinct presentational block (rotating card-stack carousel, see
        # NewlyListedBlock), not a second listing source.
        fetched["newly_listed"] = drop_hidden_listings(await get_active_listings(db, user, _SELECTED_MODE_FETCH_CAP), await hub_sales_rules(db, user))
    if "spotlights" in types_present:
        fetched["spotlights"] = await get_published_spotlights(db, user, _SELECTED_MODE_FETCH_CAP)
    profile = await get_agent_profile(db, user) if ("about_agent" in types_present or "hero" in types_present or "contact_cta" in types_present) else None
    # "Recent" sales wording only under $50M in volume (services/sales_wording): the Sold page's
    # heading and a Recent Sales block's headline follow it.
    sales_wording_needed = "recent_sales" in types_present or any(
        b.get("type") == "heading_text" and (b.get("data") or {}).get("text") in ("Recently Sold", "Recent Sales") for b in blocks)
    agent_stats = await get_agent_stats(db, user) if ("about_agent" in types_present or sales_wording_needed) else None
    from app.services.sales_wording import sales_are_recent, sales_words
    sales_recent = sales_are_recent((agent_stats or {}).get("sold_volume"))
    showcases_by_id: dict[str, dict] = {}
    if "showcase" in types_present:
        wanted_ids = {b.get("data", {}).get("collection_id") for b in blocks if b.get("type") == "showcase"}
        for collection_id in wanted_ids:
            if not collection_id:
                continue
            showcase = await get_showcase_by_id(db, user, collection_id)
            if showcase:
                showcases_by_id[collection_id] = showcase

    # Hero's served-areas eyebrow, real photo search (by those same areas - an agent never
    # re-enters a location here), and premade Buying/Selling/Home-Value quick actions (Phase 5) -
    # fetched once and shared across every hero block on the page, same as the batch fetches
    # above. Only optional price/type narrowing is per-block - see resolve_block's "auto" branch.
    hero_areas: Optional[list[dict]] = None
    hero_forms: Optional[list[dict]] = None
    hero_suggestions: Optional[list[str]] = None
    if "hero" in types_present:
        hero_areas = await get_served_areas_summary(db, user, _SELECTED_MODE_FETCH_CAP)
        all_forms = await get_forms_list(db, user)
        hero_forms = [f for f in all_forms if f["form_type"] in ("buying", "selling", "home_value")]
        hero_suggestions = await get_search_suggestions(db, user)

    id_key = {"featured_listings": "listing_key", "recent_sales": "id", "open_houses": "id", "blog_posts": "id", "communities": "slug", "newly_listed": "listing_key", "spotlights": "slug"}

    async def resolve_block(block: dict) -> dict:
        block_type = block.get("type")
        data = dict(block.get("data") or {})
        if block_type in _DEFAULT_LIMITS:
            source_mode = data.get("source_mode", "automatic")
            # "Number to show" is a free-text editor field (like every other numeric field in
            # this schema - see blockFieldSchema.ts), so it arrives as a string - _as_int guards
            # against passing that straight into a list slice, which raises a TypeError.
            limit = _as_int(data.get("limit")) or _DEFAULT_LIMITS[block_type]
            if block_type == "featured_listings":
                all_items = await get_featured_page_listings(db, user, featured_settings or {}, _SELECTED_MODE_FETCH_CAP)
            else:
                all_items = fetched.get(block_type, [])
            if source_mode == "hidden":
                items: list[dict] = []
            elif source_mode == "selected":
                key = id_key[block_type]
                wanted = data.get("selected_ids") or []
                by_id = {str(item[key]): item for item in all_items}
                items = [by_id[i] for i in wanted if i in by_id]
            else:
                items = all_items[:limit]
            data["resolved"] = items
            if block_type == "recent_sales":
                data["headline"] = sales_words(data.get("headline") or "Recently Sold", sales_recent)
                # "Show my sold stats" off (setup Sales step) hides the "N Homes Sold · $X in
                # Volume" line here too - the sold homes themselves still show.
                data["show_stats"] = (await hub_sales_rules(db, user)).get("show_stats", True)
                # The line counts ALL the agent's sales (their Agent Hub list, get_agent_stats) -
                # it used to add up only the few cards shown, so it said 6 / $17M right under an
                # About strip saying 10 / $21M (user, 10/27).
                data["sold_count"] = (agent_stats or {}).get("sold_count") or 0
                data["sold_volume"] = (agent_stats or {}).get("sold_volume") or 0
        elif block_type == "heading_text" and data.get("text"):
            data["text"] = sales_words(data["text"], sales_recent)
        elif block_type == "about_agent" and profile:
            # Source + override: real profile data first, then the agent's website-only field
            # overrides (see SiteBlockType docstring) layered on top, never mutating AgentHub.
            overrides = {k: v for k, v in (data.get("overrides") or {}).items() if v}
            data["resolved"] = {**profile, **overrides, "stats": stats_shown(agent_stats, await hub_sales_rules(db, user))}
        elif block_type == "contact_cta" and profile:
            # A blank phone/email falls back to the agent's real profile, so a Contact Band added
            # from the editor's catalogue works the moment it lands (typed values still win).
            data["resolved_phone"] = profile.get("phone") or ""
            data["resolved_text_phone"] = profile.get("text_phone") or profile.get("phone") or ""
            data["resolved_email"] = profile.get("email") or ""
        elif block_type == "showcase":
            data["resolved"] = showcases_by_id.get(data.get("collection_id"))
        elif block_type == "testimonials":
            # Follows the agent's Reviews switches: nothing while "Show on IDX" is off, and only
            # 4-5 stars when they chose that (an empty list hides the section).
            site_reviews = await get_site_reviews(db, user, limit=12)
            data["resolved"] = site_reviews["reviews"] if site_reviews["show"] else []
            data["review_path"] = "testimonials" if site_reviews["show"] else None
        elif block_type == "hero":
            if hero_areas:
                data["resolved_areas"] = hero_areas
            if hero_forms:
                data["resolved_forms"] = hero_forms
            if hero_suggestions:
                data["resolved_suggestions"] = hero_suggestions
            if profile and profile.get("market"):
                data["resolved_market"] = profile["market"]
            if data.get("photo_source_mode") == "auto":
                criteria = {
                    "price_min": data.get("photo_price_min"),
                    "price_max": data.get("photo_price_max"),
                    "property_type": data.get("photo_property_type"),
                }
                area_cities = [a["name"] for a in (hero_areas or [])]
                homes = await get_header_listings(db, user, area_cities, data)
                data["resolved_homes"] = homes
                data["resolved_photos"] = [h["photo"] for h in homes if h.get("photo")] or await get_hero_photos(db, area_cities, criteria)
        block = dict(block)
        block["data"] = data
        return block

    hydrated = dict(page)
    hydrated["blocks"] = [await resolve_block(b) for b in blocks]
    return hydrated


async def resolve_theme(db: AsyncSession, user: User, theme: dict) -> dict:
    """Source + override for branding (see SiteTheme.follow_brand_kit). While the site is
    following the Brand Kit, a Brand Kit edit shows up on the public site on the next request -
    no republish needed, since this runs at render time, not at publish time."""
    if not theme.get("follow_brand_kit", True):
        return theme
    brand = await get_brand_kit_theme(db, user)
    if not brand:
        return theme
    merged = dict(theme)
    merged["primary_color"] = brand["primary_color"]
    merged["secondary_color"] = brand["secondary_color"]
    merged["heading_font"] = brand["heading_font"]
    merged["body_font"] = brand["body_font"]
    if brand.get("logo_url"):
        merged["logo_url"] = brand["logo_url"]
    # The Brand Kit's LOOK (Classic / Luxe, metal, Ivory / Midnight) - only once the agent has
    # saved one, so every site that was already following the kit keeps its template until then.
    from app.services import brand_look_service
    look = brand_look_service.look_of(user, None)
    if look["saved"]:
        merged["template"] = "luxe" if look["theme"] == "luxe" else "default"
        merged["mode"] = "dark" if look["dark"] else "light"
        merged["luxe_metal"] = look["metal"]
        if look["theme"] != "luxe":
            merged["secondary_color"] = brand_look_service.classic_accent(brand["secondary_color"])
    return merged
