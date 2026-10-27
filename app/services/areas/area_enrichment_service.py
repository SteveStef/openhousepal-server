"""Orchestrates real, sourced enrichment of a MarketArea - the actual implementation of "research
this town once, reuse forever" (see get_or_create_market_area). Every fact category is wrapped so
one source failing (missing API key, network error, no confident match) never blocks the others -
same isolation pattern as domain_verification_service's per-check try/except. Nothing here is
ever called from a public page request (see public_site_routes.py) - only from the areas API's
BackgroundTasks trigger, so a slow/failed external call never affects a visitor.
"""
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import MarketArea, Property, User
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_orchestrator_service import _get_client
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_global_and_emergency,
)
from app.services.areas import area_research_service, census_service, geocoding, geography, market_stats_service
from app.services.areas.canonical_key import compute_canonical_key
from app.services.nces_school_assignment_service import (
    SCHOOL_LEVELS,
    _choose_unique_best,
    _nces_matches_for_point,
)
from app.services.nearby_amenities_service import FIELD_MASK, PLACES_TEXT_URL, _search_nearby, _search_text

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_area(name: str, state: str) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = _SLUG_RE.sub("-", base.lower()).strip("-")
    return f"{base}-{state.lower()}"


async def _unique_slug(db: AsyncSession, name: str, state: str) -> str:
    base = slugify_area(name, state)
    slug = base
    suffix = 2
    while (await db.execute(select(MarketArea.id).where(MarketArea.slug == slug))).scalar_one_or_none():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


async def detect_municipality_type(db: AsyncSession, name: str, state: str, zip_codes: Optional[list[str]] = None) -> Optional[str]:
    """'township' when the MLS mirror knows this name mainly as a *township*, not a postal city.
    Radnor: ~200 listings say City=Radnor but ~1,400 say Township=Radnor (their postal cities are
    Wayne, Villanova, Bryn Mawr) - scoping its page by city alone showed almost nothing. A village
    that merely shares a township's name stays city-scoped (Haverford, 19041, vs Haverford Township
    = Havertown): the township footprint has to be at least 3x the city footprint, at least 40
    listings, and - when the area's ZIPs are known - a quarter of it inside those ZIPs.
    Deterministic, from the mirror; never an external call. Read by geography.scope_for_market_area."""
    if not hasattr(Property, "township"):
        return None
    from sqlalchemy import func
    base = [Property.state == state]
    city_n = (await db.execute(select(func.count(Property.id)).where(*base, Property.city.ilike(name)))).scalar_one()
    twp_n = (await db.execute(select(func.count(Property.id)).where(*base, Property.township.ilike(name)))).scalar_one()
    if twp_n < 40 or twp_n < city_n * 3:
        return None
    if zip_codes:
        in_zip = (await db.execute(select(func.count(Property.id)).where(*base, Property.township.ilike(name), Property.zipcode.in_(zip_codes)))).scalar_one()
        if in_zip < twp_n * 0.25:
            return None
    return "township"


async def get_or_create_market_area(
    db: AsyncSession, name: str, state: str, area_type: str = "city",
    parent_area_name: Optional[str] = None, centroid: Optional[tuple[float, float]] = None,
    geography_definition: Optional[dict] = None,
) -> MarketArea:
    """The actual "don't research the same town twice" mechanism, and the actual "never create a
    second row for the same real-world place" mechanism. Returns an already-ENRICHED area
    instantly with zero new API calls if one exists; otherwise creates a PENDING row for the
    caller to enqueue enrichment on (see areas routes - this function itself never makes an
    external call itself, so it's always fast/cheap to call from a request handler - any geocoding
    for a 'neighborhood' area must happen in the CALLER before this, see
    site_routes.add_my_served_area, so `name`/`centroid` arrive here already resolved/canonical -
    geocoding is never required here for the ordinary 'city' path, deliberately, so this stays
    correct even if Google is unavailable).

    Identity is `canonical_key` (see app.services.areas.canonical_key.compute_canonical_key) -
    state + area_type + a deterministic local text normalization of the name (+ normalized parent
    name for neighborhoods) - not the raw `name` string, so "Villanova"/"Villanova PA"/"Villanova,
    Pennsylvania" all resolve to the same row without ever calling an external API.

    Race-safety: creation uses `INSERT ... ON CONFLICT (canonical_key) DO NOTHING` against the
    DB-level UNIQUE constraint on canonical_key (see the market_area_canonical_key_dedup
    migration) - this is what actually prevents two concurrent requests for the same (or
    textually-different-but-equivalent) geography from creating two rows, not the SELECT above it
    (which is only a fast-path optimization for the common case). Two concurrent creates for the
    SAME new geography also independently compute the SAME deterministic slug (see _unique_slug -
    neither can see the other's not-yet-committed row), so the loser's INSERT can just as easily
    conflict on the separate `slug` unique constraint as on canonical_key - Postgres only
    suppresses a conflict on the specific index named in ON CONFLICT, so a same-slug collision
    still raises a real IntegrityError even though ON CONFLICT was specified. That's caught below
    and treated the same as an ON-CONFLICT no-op: re-SELECT by canonical_key and return the
    winner's row. Either way, no IntegrityError ever escapes this function, no 500, no duplicate.

    area_type/parent_area_name/centroid/geography are used when creating a new row, and also to
    fill blank fields on an already-shared row that was created before those columns existed."""
    name = name.strip()
    state = state.strip().upper()
    area_type = area_type if area_type in ("city", "neighborhood") else "city"
    canonical_key = compute_canonical_key(name, state, area_type, parent_area_name)

    match = (await db.execute(select(MarketArea).where(MarketArea.canonical_key == canonical_key))).scalar_one_or_none()
    if match:
        changed = False
        if parent_area_name and not match.parent_area_name:
            match.parent_area_name = parent_area_name
            changed = True
        if centroid and (match.centroid_lat is None or match.centroid_lng is None):
            match.centroid_lat, match.centroid_lng = centroid
            changed = True
        if geography_definition and (not (match.geography or {}) or geography.is_more_precise(geography_definition, match.geography)):
            match.geography = geography_definition
            changed = True
        if area_type == "city" and not match.municipality_type:
            detected = await detect_municipality_type(db, name, state, match.zip_codes or None)
            if detected:
                match.municipality_type = detected
                changed = True
        if changed:
            await db.commit()
            await db.refresh(match)
        return match

    municipality_type = await detect_municipality_type(db, name, state) if area_type == "city" else None
    for _attempt in range(3):
        new_id = str(uuid.uuid4())
        insert_stmt = (
            pg_insert(MarketArea)
            .values(
                id=new_id, name=name, state=state, slug=await _unique_slug(db, name, state),
                municipality_type=municipality_type,
                area_type=area_type, parent_area_name=parent_area_name, geography=geography_definition or {},
                canonical_key=canonical_key, enrichment_status="PENDING",
                centroid_lat=centroid[0] if centroid else None, centroid_lng=centroid[1] if centroid else None,
            )
            .on_conflict_do_nothing(index_elements=["canonical_key"])
            .returning(MarketArea.id)
        )
        try:
            inserted_id = (await db.execute(insert_stmt)).scalar_one_or_none()
            await db.commit()
        except IntegrityError:
            # Almost always the slug collision described above, itself caused by a genuine
            # canonical_key race - the winner's row is guaranteed visible now (their transaction
            # must have committed for ours to have failed against it). In the astronomically
            # unlikely case this was a real, unrelated slug collision, the SELECT below finds
            # nothing and the loop retries with a freshly regenerated slug.
            await db.rollback()
            winner = (await db.execute(select(MarketArea).where(MarketArea.canonical_key == canonical_key))).scalar_one_or_none()
            if winner:
                return winner
            continue

        if inserted_id is None:
            # ON CONFLICT (canonical_key) no-op - another concurrent request's INSERT committed
            # first and now owns this canonical_key. Their row is guaranteed visible here:
            # Postgres blocks our INSERT on the conflicting unique index until their transaction
            # commits or rolls back before ever returning control to us.
            return (await db.execute(select(MarketArea).where(MarketArea.canonical_key == canonical_key))).scalar_one()

        return (await db.execute(select(MarketArea).where(MarketArea.id == inserted_id))).scalar_one()

    raise RuntimeError(f"Could not create MarketArea for {name!r}, {state!r} after repeated slug conflicts")


def needs_research(area: MarketArea) -> bool:
    """True if this area has never completed research, previously failed, or was researched
    under an older pipeline version than area_research_service.CURRENT_RESEARCH_VERSION and
    should be treated as stale - e.g. every area enriched under the old one-call-per-category
    design (or anything from before research_version existed at all, which reads as None) needs
    a fresh run under the current combined-request pipeline. Used both by the live add-area route
    (site_routes.add_my_served_area) and by the one-time backfill across existing MarketArea rows -
    a single source of truth for "is this row current" so the two paths can't disagree."""
    if area.enrichment_status in ("PENDING", "FAILED"):
        return True
    return area.research_version != area_research_service.CURRENT_RESEARCH_VERSION


def unresolved_research_categories(facts: dict) -> list[str]:
    return [
        category
        for category in area_research_service.RESEARCH_CATEGORIES
        if not area_research_service.category_is_resolved(category, (facts or {}).get(category) or {})
    ]


def enrichment_status_for_facts(facts: dict) -> str:
    """Status reflects required public-page research completeness, not optional source retries."""
    if not facts:
        return "FAILED"
    return "PARTIAL" if unresolved_research_categories(facts) else "ENRICHED"


async def resolve_school_districts_for_area(db: AsyncSession, lat: float, lng: float, state: str) -> list[dict]:
    """NCES-only (no Bright/MLS blending, unlike the per-property resolver - see
    nces_school_assignment_service.resolve_school_assignment) - real government attendance-
    boundary data is the whole point for a shared, agent-independent area record."""
    matches_by_level = await _nces_matches_for_point(db, lat, lng, state)
    schools: list[dict] = []
    for level in SCHOOL_LEVELS:
        best, _ambiguous = _choose_unique_best(matches_by_level.get(level, []), level)
        if best and best.school_name:
            schools.append({
                "level": level,
                "name": best.school_name,
                "lea_id": best.lea_id,
                "source": "nces",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
    return schools


async def _places_facts(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> dict:
    facts: dict = {}
    now = datetime.now(timezone.utc).isoformat()

    async def category(key: str, included_type: str, radius_miles: float, limit: int):
        try:
            raw = await _search_nearby(client, api_key, lat, lng, included_type, radius_miles=radius_miles, max_result_count=limit)
        except httpx.HTTPError as exc:
            logger.info("Area Places lookup failed", extra={"category": key, "error": str(exc)})
            return
        items = [
            {"name": (p.get("displayName") or {}).get("text"), "address": p.get("formattedAddress"), "place_id": p.get("id")}
            for p in raw if (p.get("displayName") or {}).get("text")
        ]
        if items:
            facts[key] = {"items": items, "source": "google_places", "fetched_at": now}

    await category("parks", "park", radius_miles=3.0, limit=6)
    await category("downtown_areas", "locality", radius_miles=5.0, limit=3)
    await category("transportation", "train_station", radius_miles=5.0, limit=4)

    try:
        raw = await _search_text(client, api_key, lat, lng, "shops and restaurants", radius_miles=2.0)
    except httpx.HTTPError as exc:
        logger.info("Area amenities text search failed", extra={"error": str(exc)})
        raw = []
    items = [
        {"name": (p.get("displayName") or {}).get("text"), "address": p.get("formattedAddress"), "place_id": p.get("id")}
        for p in raw if (p.get("displayName") or {}).get("text")
    ][:8]
    if items:
        facts["amenities"] = {"items": items, "source": "google_places", "fetched_at": now}

    return facts


async def _places_photos(client: httpx.AsyncClient, api_key: str, name: str, state: str) -> dict:
    """Reuses the same searchText call shape as geocoding.geocode_place, just requesting a
    `places.photos` field so this doesn't cost a second API call for areas that also need
    geocoding - see area's caller, which fetches this alongside the centroid lookup."""
    try:
        response = await client.post(
            PLACES_TEXT_URL,
            json={"textQuery": f"{name}, {state}"},
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK + ",places.photos"},
            timeout=10.0,
        )
        response.raise_for_status()
        places = response.json().get("places") or []
    except httpx.HTTPError as exc:
        logger.info("Area photo lookup failed", extra={"error": str(exc)})
        return {}
    if not places:
        return {}
    photos = (places[0].get("photos") or [])[:6]
    items = []
    for photo in photos:
        photo_name = photo.get("name")
        if not photo_name:
            continue
        items.append({
            # Stores the Places photo RESOURCE NAME, never a pre-signed URL with the API key
            # baked in - this data ends up embedded in publicly served HTML, and a raw
            # "...&key=..." URL would leak our Google API key to anyone who views page source.
            # public_site_routes.get_area_photo resolves this server-side at request time,
            # keeping the key off the public internet entirely.
            "photo_name": photo_name,
            "attribution": ", ".join(a.get("displayName", "") for a in (photo.get("authorAttributions") or []) if a.get("displayName")) or "Google",
            "source": "google_places",
        })
    if not items:
        return {}
    return {"items": items, "source": "google_places", "fetched_at": datetime.now(timezone.utc).isoformat()}


_NARRATIVE_SYSTEM_PROMPT = (
    "You write one short, warm paragraph (3-4 sentences) describing a real-estate community for "
    "an agent's website. You will be given a JSON object of ALREADY-VERIFIED facts about the "
    "town. Only describe what is present in the JSON - never state a specific number, name, "
    "statistic, or claim that is not literally present in the input. If the facts are sparse, "
    "write a shorter, more general paragraph rather than inventing detail. Write every school "
    "district, school and place name in full exactly as the input spells it - never shorten one. "
    "Output only the paragraph text, no preamble."
)


async def _generate_narrative(db: AsyncSession, area: MarketArea, triggering_user: User) -> Optional[dict]:
    config = await ai_config_service.get_config(db)

    # Reserved GLOBALLY (all agents combined, via the "area_narrative_global" bucket) rather
    # than against triggering_user's own budget - a shared MarketArea's narrative is generated
    # once, ever, on behalf of every agent who'll eventually reuse it, so no single agent's
    # personal spend should be charged for researching a town on everyone's behalf.
    # AIUsageEvent.owner_id below still records who triggered it, for admin reporting - only
    # the ceiling check itself is unscoped. Race-free across workers (usage_budget_service.py),
    # not a read-then-compare.
    try:
        reservation = await reserve_global_and_emergency(
            db, "area_narrative_global", config["area_narrative_global_reservation_estimate_usd"],
            None, config["area_narrative_monthly_cost_ceiling_usd"],
            config,
        )
    except AIRateLimitExceeded:
        logger.info("Skipping area narrative generation - global monthly cost ceiling reached")
        return None

    # The summary is written from what the page itself may show (clean_area_payload): no outside
    # price figures, no income/age rows (Malvern's summary leaned on "median household income"),
    # no places the check removed, and the checked school districts by their full names.
    from app.services.areas.fair_housing_service import clean_area_payload, town_district_names
    from app.services.areas.town_places_check import fix_district_names
    grounding, _ = clean_area_payload(dict(area.facts or {}), None)
    grounding = {k: v for k, v in grounding.items() if not k.startswith("_") and k not in ("photos", "boundaries", "schools")}
    try:
        client = _get_client()
        response = await client.messages.create(
            model=config["model"],
            max_tokens=250,
            system=_NARRATIVE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Town: {area.name}, {area.state}\n\nVerified facts:\n{grounding}"}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        # Plain prose only - the model sometimes opens with a markdown title ("# Gettysburg, Pennsylvania").
        text = re.sub(r"(?m)^\s*#+[^\n]*\n+", "", text).replace("**", "").strip()
        text = fix_district_names(text, town_district_names(dict(area.facts or {})))
        if not text:
            raise ValueError("empty narrative")
    except Exception as exc:
        logger.warning("Area narrative generation failed", extra={"area_id": area.id, "error": str(exc)})
        await reconcile_budget(db, reservation, 0.0)
        return None

    cost = ai_config_service.estimate_cost_usd(config, config["model"], response.usage.input_tokens, response.usage.output_tokens)
    await record_usage_event(
        db, triggering_user.id, None, config["model"],
        response.usage.input_tokens, response.usage.output_tokens, cost, 0, False, None,
        event_type="area_narrative",
    )
    await reconcile_budget(db, reservation, cost)
    return {
        "summary": text,
        "source": "ai_generated",
        "model": config["model"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grounded_fact_keys": list(area.facts.keys()),
    }


async def _derive_zip_codes(db: AsyncSession, name: str, state: str) -> list[str]:
    """Real ZIP codes for this town, derived from our own already-synced Property data (city
    match) rather than any external source - the same "reuse OHP data" principle as everywhere
    else in Website Builder. An area with no matching local listings yet simply starts with no
    ZIP codes (Census/market-stats gracefully have less to work with, not guessed data)."""
    from sqlalchemy import case, or_
    rows = (await db.execute(
        select(Property.zipcode, func.count()).where(Property.city.ilike(name), Property.state == state, Property.zipcode.is_not(None))
        .group_by(Property.zipcode)
    )).all()
    counts = {str(z)[:5]: 0 for z, _ in rows if z}
    for z, n in rows:
        if z:
            counts[str(z)[:5]] += n
    total = sum(counts.values()) or 1
    # Real ZIPs only ("00000" came through on Princeton), and not one a couple of mistyped listings
    # dragged in (Malvern picked up Exton's 19341 and Phoenixville's 19460).
    candidates = [z for z, n in counts.items()
                  if re.fullmatch(r"\d{5}", z) and z != "00000" and (n >= 5 or n / total >= 0.05)]
    if not candidates:
        return []
    # ...and only a ZIP that is really this town's postal area: most of THAT ZIP's own listings use
    # this town's name (10/27: Charlottesville held Barboursville's 22923 - 98% of its listings say
    # Barboursville; Princeton held Princeton Junction's 08550). A township town also owns the ZIP
    # when most of its listings sit in the township.
    owners = select(Property.zipcode, func.count(), func.sum(case((Property.city.ilike(name), 1), else_=0)),
                    func.sum(case((Property.township.ilike(name), 1), (Property.township.ilike(f"{name} township"), 1), else_=0))) \
        .where(Property.state == state, or_(*[Property.zipcode.like(f"{z}%") for z in candidates])).group_by(Property.zipcode)
    own: dict[str, list[int]] = {}
    for z, n, by_city, by_township in (await db.execute(owners)).all():
        acc = own.setdefault(str(z)[:5], [0, 0, 0])
        acc[0] += n or 0
        acc[1] += by_city or 0
        acc[2] += by_township or 0
    return sorted(z for z in candidates if zip_belongs_to_town(*own.get(z, [0, 0, 0])))


ZIP_OWNER_SHARE = 0.5


def zip_belongs_to_town(zip_listings: int, named_this_town: int, in_this_township: int = 0) -> bool:
    """A ZIP joins a town when at least half of the ZIP's own listings carry the town's name (or,
    for a township town, sit in the township)."""
    if zip_listings <= 0:
        return False
    return max(named_this_town, in_this_township) / zip_listings >= ZIP_OWNER_SHARE


async def enrich_area(db: AsyncSession, area: MarketArea, triggering_user: User) -> MarketArea:
    facts: dict = dict(area.facts or {})
    any_failure = False

    if not area.zip_codes:
        derived = await _derive_zip_codes(db, area.name, area.state)
        if derived:
            area.zip_codes = derived

    if area.centroid_lat is None or area.centroid_lng is None:
        centroid = await geocoding.geocode_place(area.name, area.state)
        if centroid:
            area.centroid_lat, area.centroid_lng = centroid
        else:
            any_failure = True

    if area.area_type == "neighborhood" and not (area.geography or {}) and area.centroid_lat is not None and area.centroid_lng is not None:
        area.geography = geography.radius_definition(area.centroid_lat, area.centroid_lng, source="market_area_centroid_radius_fallback")

    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if area.centroid_lat is not None and area.centroid_lng is not None:
        if api_key:
            async with httpx.AsyncClient() as client:
                # (The old Places parks / train / "shops and restaurants" lookups now run only as a
                # backup after AI research - see below - instead of on every town.)
                photos = await _places_photos(client, api_key, area.name, area.state)
                if photos:
                    facts["photos"] = photos
        else:
            any_failure = True

        try:
            schools = await resolve_school_districts_for_area(db, area.centroid_lat, area.centroid_lng, area.state)
            if schools:
                facts["schools"] = schools
        except Exception as exc:
            logger.warning("Area school lookup failed", extra={"area_id": area.id, "error": str(exc)})
            any_failure = True

    try:
        demographics = await census_service.get_demographics(area.state, area.zip_codes or [], area.name)
        if demographics:
            facts["demographics"] = demographics
        else:
            any_failure = True  # legitimately omitted or Census unavailable - see census_service logs
    except Exception as exc:
        logger.warning("Area demographics lookup failed", extra={"area_id": area.id, "error": str(exc)})
        any_failure = True

    try:
        stats = await market_stats_service.get_area_market_stats(db, geography.scope_for_market_area(area))
        if stats:
            facts["market_stats"] = stats
    except Exception as exc:
        logger.warning("Area market stats lookup failed", extra={"area_id": area.id, "error": str(exc)})
        any_failure = True

    # AI-grounded research (schools/private schools/grocery/parks/restaurants/shopping/downtown
    # centers/medical/transportation/historical market context) - see area_research_service.py.
    # Runs after every deterministic source above so it can hand its own results in as grounding
    # evidence (esp. the real MLS school_district_name values just computed into `stats` and the
    # NCES boundary lookup already in `facts["schools"]`) rather than researching cold. Wrapped in
    # the same try/except isolation as every other source above (a real crash inside AI research
    # once propagated out of enrich_area entirely during testing, losing the zip codes/geocode/
    # Places/NCES/Census/market_stats already computed above since `area.facts = facts` never got
    # to run) - one source failing, AI research included, should never blow up the whole job.
    try:
        ai_failure = await _run_ai_research(db, area, facts, triggering_user)
    except Exception as exc:
        logger.warning("AI area research raised unexpectedly", extra={"area_id": area.id, "error": str(exc)})
        ai_failure = True
    any_failure = any_failure or ai_failure

    # Backup only: if AI research came back empty for parks or transportation, fill them from a
    # plain Google Places lookup (never overwriting researched data). Saves the Places calls on
    # every town the research covers - which is nearly all of them.
    if api_key and area.centroid_lat is not None and area.centroid_lng is not None and not all(
        area_research_service.result_has_content(c, facts.get(c) or {}) for c in ("parks", "transportation")
    ):
        try:
            async with httpx.AsyncClient() as client:
                fallback = await _places_facts(client, api_key, area.centroid_lat, area.centroid_lng)
            for key, value in fallback.items():
                if not facts.get(key) or not area_research_service.result_has_content(key, facts.get(key) or {}):
                    facts.setdefault(key, value)
        except Exception as exc:
            logger.info("Places backup lookup failed", extra={"area_id": area.id, "error": str(exc)})

    area.facts = facts
    db.add(area)
    await db.commit()
    await db.refresh(area)

    # School districts checked against the town's real listings BEFORE the write-up is generated,
    # so a new town is right from day one (see content_adapters.check_and_store_public_schools).
    try:
        from app.services.website.content_adapters import check_and_store_public_schools
        if await check_and_store_public_schools(db, area):
            await db.refresh(area)
            facts = area.facts
    except Exception:
        logger.exception("school district check after research failed for %s", area.slug)

    # Every researched place held to the Cheat Sheet's rules (open, real, right kind, real driving
    # distance, nearest hospitals) BEFORE the write-up, so the write-up never names a dropped place.
    try:
        from app.services.areas import town_places_check
        await town_places_check.check_town_places(db, area)
        await db.refresh(area)
        facts = area.facts
    except Exception:
        logger.exception("town place check failed for %s", area.slug)
        await db.rollback()
        await db.refresh(area)

    if facts:
        narrative = await _generate_narrative(db, area, triggering_user)
        if narrative:
            area.narrative = narrative

    area.enrichment_status = enrichment_status_for_facts(facts)
    area.last_enriched_at = datetime.now(timezone.utc)
    db.add(area)
    await db.commit()
    await db.refresh(area)

    # Commute times, FEMA flood share, the town map (Census borders) and what's within a walk
    # (OpenStreetMap), and the Fair Housing wording pass (AI + fixed rules) - each isolated, none can
    # undo the research above.
    from app.services.areas import fair_housing_service, town_extras_service, town_map_service
    for step in (town_extras_service.refresh_commute, town_extras_service.refresh_flood,
                 town_map_service.refresh_boundaries, town_map_service.refresh_walkability):
        try:
            await step(db, area)
        except Exception:
            logger.exception("town extra step %s failed for %s", step.__name__, area.slug)
            await db.rollback()
    try:
        await fair_housing_service.fix_area_text(db, area, triggering_user.id, use_ai=True)
    except Exception:
        logger.exception("fair housing pass failed for %s", area.slug)
        await db.rollback()
    await db.refresh(area)
    return area


def _write_category_facts(facts: dict, category: str, cleaned: dict, dropped: dict, usage: Optional[dict], escalated: bool) -> None:
    facts[category] = cleaned
    facts[category]["_meta"] = {
        "source": "ai_research",
        "model": (usage or {}).get("model"),
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "web_search_requests": (usage or {}).get("web_search_requests"),
        "dropped": dropped,
        "escalated": escalated,
    }


_COMPLETION_MAX_SEARCHES = 3
_SONNET_FALLBACK_MAX_SEARCHES = 3


async def _run_ai_research(db: AsyncSession, area: MarketArea, facts: dict, triggering_user: User) -> bool:
    """v6 (see area_research_service module docstring for the full v3->v6 history): v5's 5-group
    primary Haiku pass, then a targeted, bounded top-up for anything not genuinely RESOLVED (see
    area_research_service.category_is_resolved - empty is only "done" if the model credibly
    explained a real search found nothing; "the schema allowed it" is not enough):
      1. Primary pass: research_community_grouped (unchanged from v5).
      2. Completion pass: any category still unresolved gets ONE focused, single-category Haiku
         call (run_solo_category_research) - never a second pass at the same multi-category group.
      3. Narrow Sonnet fallback: public_schools ONLY, and only if still unresolved after its
         completion pass AND this platform's own real MLS school_district_name evidence names a
         district the model still isn't finding (a genuine, checkable conflict) - the one place
         Sonnet is used anywhere in this pipeline.
    A hard per-community budget (area_research_max_cost_per_community_usd) gates steps 2 and 3 -
    once a community's own spend this run hits that budget, remaining unresolved categories are
    left as-is (logged for review) rather than pursued further; this is separate from and checked
    in addition to the existing monthly ceiling below. Mutates `facts` in place. Returns True if
    anything is still unresolved when this returns, a real error occurred, or the whole phase was
    skipped for cost.

    This platform's OWN existing evidence (real MLS school_district_name values, any older
    Places/NCES facts already on this row) is deliberately NOT sent as grounding to research
    FROM - see the module's architecture note. It's handed to the model only as a labeled
    secondary cross-check, after independent web research, with an explicit "may be wrong or
    stale" caveat."""
    config = await ai_config_service.get_config(db)
    per_community_budget = config.get("area_research_max_cost_per_community_usd", 1.0)

    # Reserved GLOBALLY (all agents combined, via the "area_research_global" bucket) at the size
    # of the worst case this one job could ever spend (per_community_budget - the hard per-
    # instance cap enforced throughout the rest of this function) - trued up below with the real
    # spend once the job finishes. Deliberately NOT scoped to triggering_user (a shared
    # MarketArea's research benefits every agent who'll ever add that town, not just whoever was
    # first) and deliberately a NEW, separate ceiling from area_research_max_cost_per_community_usd
    # - see ai_config_service.py for why this isn't just a renamed version of an older, smaller
    # per-agent-attributed value. Race-free across workers (usage_budget_service.py), not a
    # read-then-compare - so concurrent first-time research jobs for different new communities
    # can't collectively blow past the ceiling by all reading a stale "not yet reached" total.
    try:
        reservation = await reserve_global_and_emergency(
            db, "area_research_global", per_community_budget,
            config["area_research_global_daily_cost_ceiling_usd"], config["area_research_global_monthly_cost_ceiling_usd"],
            config,
        )
    except AIRateLimitExceeded:
        logger.info("Skipping AI area research - platform-wide research budget already reached", extra={"area_id": area.id})
        return True

    school_districts_evidence: list[str] = (facts.get("market_stats") or {}).get("school_districts") or []
    nces_evidence = facts.get("schools") or []
    nces_summary = ", ".join(f"{s.get('level')}: {s.get('name')}" for s in nces_evidence) or "(none available)"
    districts_summary = ", ".join(school_districts_evidence) or "(none available)"
    secondary_evidence_text = (
        "This platform's own EXISTING evidence for this community, for use ONLY as a secondary cross-check AFTER "
        "your own independent web research - this may be wrong, incomplete, or stale, so do not treat it as a "
        "starting point or let it override what your current research finds:\n"
        f"- School district name(s) recorded on past MLS listings in this community's ZIP codes: {districts_summary}\n"
        f"- An older attendance-boundary lookup for this community's centroid point: {nces_summary}"
    )

    try:
        raw_result, usage = await area_research_service.research_community_grouped(db, area, triggering_user, secondary_evidence_text)
    except Exception as exc:
        logger.warning("Grouped AI area research failed", extra={"area_id": area.id, "error": str(exc)})
        raw_result, usage = None, None

    if raw_result is None:
        logger.warning("Grouped AI area research produced no structured result", extra={"area_id": area.id})
        await record_usage_event(
            db, triggering_user.id, None, (usage or {}).get("model"), (usage or {}).get("input_tokens"), (usage or {}).get("output_tokens"),
            (usage or {}).get("estimated_cost_usd"), 0, True, "no_submission",
            event_type=area_research_service.AREA_RESEARCH_EVENT_TYPE,
        )
        await reconcile_budget(db, reservation, (usage or {}).get("estimated_cost_usd") or 0.0)
        return True  # research_version stays as it was, so the nightly upkeep tries this town again

    # Only a run that came back with research marks the town current (10/15 on live: six towns got
    # nothing, were stamped current anyway and never researched again).
    area.research_version = area_research_service.CURRENT_RESEARCH_VERSION
    running_cost = (usage or {}).get("estimated_cost_usd") or 0.0
    all_calls: list[dict] = list((usage or {}).get("calls") or [])
    total_input_tokens = (usage or {}).get("input_tokens") or 0
    total_output_tokens = (usage or {}).get("output_tokens") or 0
    category_usage: dict[str, dict] = dict((usage or {}).get("category_usage") or {})

    cleaned_by_category: dict[str, dict] = {}
    dropped_by_category: dict[str, dict] = {}
    for category in area_research_service.RESEARCH_CATEGORIES:
        cleaned, dropped = area_research_service.validate_category_result(category, raw_result.get(category) or {})
        if any(dropped.values()):
            logger.info("AI area research dropped entries during validation", extra={"area_id": area.id, "category": category, "dropped": dropped})
        cleaned_by_category[category] = cleaned
        dropped_by_category[category] = dropped

    # Completion pass: focused, single-category Haiku follow-up for anything the primary pass
    # left unresolved - never a blind retry of the whole job or a whole group, and budget-gated
    # against per_community_budget so a genuinely stubborn town can't run away on cost.
    budget_exceeded = False
    for category in area_research_service.RESEARCH_CATEGORIES:
        if area_research_service.category_is_resolved(category, cleaned_by_category[category]):
            continue
        if running_cost >= per_community_budget:
            budget_exceeded = True
            logger.warning(
                "AI area research per-community budget reached before every category was resolved",
                extra={"area_id": area.id, "spent_usd": running_cost, "budget_usd": per_community_budget, "category": category},
            )
            break
        try:
            solo_result, solo_usage = await area_research_service.run_solo_category_research(
                db, area, triggering_user, category, secondary_evidence_text, max_searches=_COMPLETION_MAX_SEARCHES,
            )
        except Exception as exc:
            logger.warning("Completion-pass call failed", extra={"area_id": area.id, "category": category, "error": str(exc)})
            solo_result, solo_usage = None, {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0, "estimated_cost_usd": 0.0, "model": "claude-haiku-4-5"}

        running_cost += solo_usage.get("estimated_cost_usd") or 0.0
        total_input_tokens += solo_usage.get("input_tokens") or 0
        total_output_tokens += solo_usage.get("output_tokens") or 0
        all_calls.append({"group": f"completion_{category}", "result": "ok" if solo_result is not None else "no_submission", **{k: solo_usage.get(k, 0) for k in ("input_tokens", "output_tokens", "web_search_requests")}})

        raw_value = (solo_result or {}).get(category)
        if isinstance(raw_value, dict):
            new_cleaned, new_dropped = area_research_service.validate_category_result(category, raw_value)
            cleaned_by_category[category] = new_cleaned
            dropped_by_category[category] = new_dropped
            category_usage[category] = {"model": solo_usage.get("model", "claude-haiku-4-5"), "web_search_requests": solo_usage.get("web_search_requests", 0), "escalated": False}

    # Narrow Sonnet fallback - public_schools only, and only if a genuine, checkable evidence
    # conflict remains (our own real MLS school_district_name evidence names a district the model
    # still isn't reporting) - never triggered by persistent emptiness alone. The one place Sonnet
    # is used anywhere in this pipeline, deliberately rare.
    if (
        not budget_exceeded
        and running_cost < per_community_budget
        and not area_research_service.category_is_resolved("public_schools", cleaned_by_category["public_schools"])
        and school_districts_evidence
    ):
        escalation_model = config.get("area_research_escalation_model", "claude-sonnet-5")
        try:
            solo_result, solo_usage = await area_research_service.run_solo_category_research(
                db, area, triggering_user, "public_schools", secondary_evidence_text,
                model_override=escalation_model, max_searches=_SONNET_FALLBACK_MAX_SEARCHES,
            )
        except Exception as exc:
            logger.warning("Sonnet fallback call failed", extra={"area_id": area.id, "category": "public_schools", "error": str(exc)})
            solo_result, solo_usage = None, {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0, "estimated_cost_usd": 0.0, "model": escalation_model}

        running_cost += solo_usage.get("estimated_cost_usd") or 0.0
        total_input_tokens += solo_usage.get("input_tokens") or 0
        total_output_tokens += solo_usage.get("output_tokens") or 0
        all_calls.append({"group": "sonnet_fallback_public_schools", "result": "ok" if solo_result is not None else "no_submission", **{k: solo_usage.get(k, 0) for k in ("input_tokens", "output_tokens", "web_search_requests")}})

        raw_value = (solo_result or {}).get("public_schools")
        if isinstance(raw_value, dict):
            new_cleaned, new_dropped = area_research_service.validate_category_result("public_schools", raw_value)
            cleaned_by_category["public_schools"] = new_cleaned
            dropped_by_category["public_schools"] = new_dropped
            category_usage["public_schools"] = {"model": escalation_model, "web_search_requests": solo_usage.get("web_search_requests", 0), "escalated": True}

    if running_cost > per_community_budget:
        logger.warning("AI area research exceeded its per-community budget", extra={"area_id": area.id, "spent_usd": running_cost, "budget_usd": per_community_budget})

    logger.info("Grouped AI area research call breakdown", extra={"area_id": area.id, "calls": all_calls, "total_cost_usd": running_cost})

    await record_usage_event(
        db, triggering_user.id, None, (usage or {}).get("model"), total_input_tokens, total_output_tokens,
        running_cost, 0, False, None,
        event_type=area_research_service.AREA_RESEARCH_EVENT_TYPE,
    )
    await reconcile_budget(db, reservation, running_cost)

    any_failure = budget_exceeded
    for category in area_research_service.RESEARCH_CATEGORIES:
        cleaned = cleaned_by_category[category]
        dropped = dropped_by_category[category]
        cat_usage = category_usage.get(category) or {}
        if not area_research_service.category_is_resolved(category, cleaned):
            any_failure = True
        _write_category_facts(facts, category, cleaned, dropped, cat_usage, escalated=bool(cat_usage.get("escalated")))

    return any_failure
