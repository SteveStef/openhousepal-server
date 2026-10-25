"""Shared adaptive-proximity context for OpenHousePal similarity scoring.

The similarity formula lives in the CMA analysis service and its TypeScript mirror. This module
owns the one market-dependent input: a subject-level distance half-life derived from a stable
population of suitable nearby properties. Candidate properties never compute their own scale.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property
from app.utils.geo import distance_miles_sql, get_lat_long_offsets, haversine_distance


DEFAULT_PROXIMITY_HALF_LIFE_MILES = 2.5
MIN_PROXIMITY_HALF_LIFE_MILES = 0.5
MAX_PROXIMITY_HALF_LIFE_MILES = 5.0
MIN_DENSITY_REFERENCE_COUNT = 10
DENSITY_NEIGHBOR_START = 8
DENSITY_NEIGHBOR_END = 12
MAX_DENSITY_SEARCH_MILES = 20.0
DENSITY_SQFT_RATIO_MIN = 0.75
DENSITY_SQFT_RATIO_MAX = 1.25
DENSITY_DETACHED_LOT_RATIO_MIN = 0.50
DENSITY_DETACHED_LOT_RATIO_MAX = 2.00
DENSITY_DETACHED_YEAR_DELTA = 40
DENSITY_RECENCY_WINDOWS_DAYS = (365, 548, 730, 1095, 1825)
MATCH_DISPLAY_CALIBRATION_VERSION = "match-display-v2"
# Piecewise-linear, not a power curve: a single global exponent (v1 used raw^0.70) always lifts
# low raw scores proportionally MORE than high ones, since d/dx(x^k) is decreasing in x for k<1 -
# there's no exponent that can make a mediocre real comp read as mediocre while still letting a
# genuinely exceptional one (empirically, real comps top out around raw 0.85-0.90 - two different
# properties are never identical) reach the high 80s/90s. A benchmark against ~240 real candidates
# confirmed this: v1 gave raw~0.56 a +10.6pt boost but raw~0.84 only +4.5pt - backwards from the
# intent. This curve is linear (no lift at all) through the "mediocre/decent" range and only opens
# up above the breakpoints, right where the real ceiling is.
_CALIBRATION_BREAKPOINT_1 = 0.70
_CALIBRATION_BREAKPOINT_2 = 0.90


def calibrate_match_percentage(raw_relevance: float) -> int:
    """Map strict 0-1 relevance to an intuitive badge without changing rank or valuation weight.

    raw <= 0.70: display = 100 * raw (identity - a mediocre match reads as mediocre).
    0.70 < raw <= 0.90: display = 70 + (raw - 0.70) * 125 (genuinely good -> exceptional).
    raw > 0.90: display = 95 + (raw - 0.90) * 50 (continues to 100 at raw=1.0).
    All three segments meet exactly at their breakpoints and each has a positive slope, so the
    whole function is continuous and strictly monotonic - ranking and valuation weight (which
    consume raw_relevance_score directly, never this display value) are unaffected.
    """
    raw = max(0.0, min(1.0, float(raw_relevance)))
    if raw <= _CALIBRATION_BREAKPOINT_1:
        display = 100.0 * raw
    elif raw <= _CALIBRATION_BREAKPOINT_2:
        display = 70.0 + (raw - _CALIBRATION_BREAKPOINT_1) * 125.0
    else:
        display = 95.0 + (raw - _CALIBRATION_BREAKPOINT_2) * 50.0
    return max(1, min(100, round(display)))


def _as_float(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _norm(value) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


def property_type_family(value) -> Optional[str]:
    text = _norm(value)
    if not text:
        return None
    if any(token in text for token in ("multi family", "multifamily", "multi_family", "multi unit", "duplex", "triplex", "quadruplex", "fourplex")):
        return "multi"
    if any(token in text for token in ("condo", "unit", "flat", "apartment", "penthouse")):
        return "condo"
    if any(token in text for token in ("townhouse", "townhome", "row", "twin", "attached")):
        return "attached"
    if any(token in text for token in ("single family", "singlefamily", "detached", "farm")):
        return "detached"
    return None


def _record_distance(subject: dict, record: dict) -> Optional[float]:
    supplied = _as_float(record.get("distance_miles"))
    if supplied is not None and supplied >= 0:
        return supplied
    slat = _as_float(subject.get("latitude"))
    slng = _as_float(subject.get("longitude"))
    lat = _as_float(record.get("latitude"))
    lng = _as_float(record.get("longitude"))
    if None in (slat, slng, lat, lng):
        return None
    return haversine_distance(slat, slng, lat, lng)


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _record_lot_sqft(record: dict) -> Optional[float]:
    lot = _as_float(record.get("lot_size_sqft") or record.get("lot_size"))
    if lot and lot > 0:
        return lot
    acres = _as_float(record.get("lot_size_acres") or record.get("acres"))
    return acres * 43560.0 if acres and acres > 0 else None


def _parse_market_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None


def _is_current_market_status(value) -> bool:
    status = _norm(value)
    return bool(
        status.startswith("active")
        or status.startswith("pending")
        or status.startswith("coming soon")
        or status in {"under contract", "contract"}
    )


def _record_is_recent_market_evidence(record: dict, *, max_days: int, as_of: date) -> bool:
    if _is_current_market_status(record.get("status") or record.get("home_status")):
        return True
    closed = _parse_market_date(record.get("close_date") or record.get("sale_date"))
    return bool(closed and as_of - timedelta(days=max_days) <= closed <= as_of)


def build_adaptive_proximity_context(subject: dict, references: list[dict]) -> dict:
    """Return one deterministic market-density scale shared by every candidate for a subject.

    Density is based on recently available, physically comparable housing stock. Counting every
    historical same-family listing made acre-lot suburbs look urban simply because many smaller-lot
    homes existed nearby. The recency window widens only when needed to obtain a stable sample.
    """
    subject_family = property_type_family(subject.get("property_type") or subject.get("home_type"))
    subject_sqft = _as_float(
        subject.get("above_grade_finished_area")
        or subject.get("above_grade_sqft")
        or subject.get("sqft")
        or subject.get("living_area")
    )
    subject_key = str(subject.get("listing_key") or "")
    subject_address_key = _norm("|".join(
        str(subject.get(k) or "") for k in ("street_address", "city", "state", "zipcode")
    ))
    subject_lot = _record_lot_sqft(subject)
    subject_year = _as_float(subject.get("year_built"))
    seen: set[str] = set()
    physically_suitable: list[tuple[float, dict]] = []

    for record in references or []:
        if subject_key and str(record.get("listing_key") or "") == subject_key:
            continue
        family = property_type_family(record.get("property_type") or record.get("home_type"))
        if subject_family and family != subject_family:
            continue
        record_sqft = _as_float(
            record.get("above_grade_finished_area")
            or record.get("above_grade_sqft")
            or record.get("sqft")
            or record.get("living_area")
        )
        if subject_sqft and (
            not record_sqft
            or not DENSITY_SQFT_RATIO_MIN * subject_sqft <= record_sqft <= DENSITY_SQFT_RATIO_MAX * subject_sqft
        ):
            continue
        if subject_family == "detached":
            record_lot = _record_lot_sqft(record)
            if subject_lot and (
                not record_lot
                or not DENSITY_DETACHED_LOT_RATIO_MIN * subject_lot
                <= record_lot
                <= DENSITY_DETACHED_LOT_RATIO_MAX * subject_lot
            ):
                continue
            record_year = _as_float(record.get("year_built"))
            if subject_year and (not record_year or abs(record_year - subject_year) > DENSITY_DETACHED_YEAR_DELTA):
                continue
        distance = _record_distance(subject, record)
        if distance is None or distance <= 0.01:
            continue
        address_key = _norm("|".join(str(record.get(k) or "") for k in ("street_address", "city", "state", "zipcode")))
        lat = _as_float(record.get("latitude"))
        lng = _as_float(record.get("longitude"))
        location_key = address_key or (f"{lat:.5f}|{lng:.5f}" if lat is not None and lng is not None else "")
        if not location_key or location_key == subject_address_key or location_key in seen:
            continue
        seen.add(location_key)
        physically_suitable.append((distance, record))

    lifecycle_known = any(
        _is_current_market_status(record.get("status") or record.get("home_status"))
        or _parse_market_date(record.get("close_date") or record.get("sale_date"))
        for _, record in physically_suitable
    )
    recency_window_days = None
    suitable = physically_suitable
    if lifecycle_known:
        from app.services import cma_time_machine
        as_of = cma_time_machine.today()
        for max_days in DENSITY_RECENCY_WINDOWS_DAYS:
            recent = [
                item for item in physically_suitable
                if _record_is_recent_market_evidence(item[1], max_days=max_days, as_of=as_of)
            ]
            suitable = recent
            recency_window_days = max_days
            if len(recent) >= MIN_DENSITY_REFERENCE_COUNT:
                break
        if len(suitable) < MIN_DENSITY_REFERENCE_COUNT:
            suitable = physically_suitable
            recency_window_days = None

    suitable_distances = sorted(distance for distance, _ in suitable)
    enough = len(suitable_distances) >= MIN_DENSITY_REFERENCE_COUNT
    density_distances = (
        suitable_distances[DENSITY_NEIGHBOR_START - 1:min(len(suitable_distances), DENSITY_NEIGHBOR_END)]
        if enough else []
    )
    observed = _median(density_distances)
    half_life = (
        max(MIN_PROXIMITY_HALF_LIFE_MILES, min(MAX_PROXIMITY_HALF_LIFE_MILES, observed))
        if observed is not None else DEFAULT_PROXIMITY_HALF_LIFE_MILES
    )
    band = "dense_urban" if half_life <= 0.75 else "low_density_estate" if half_life >= 4.0 else "suburban"
    return {
        "version": "adaptive-proximity-v3",
        "band": band,
        "distance_half_life_miles": round(half_life, 3),
        "local_market_half_life_miles": round(half_life, 3),
        "reference_count": len(suitable_distances),
        "physical_reference_count": len(physically_suitable),
        "recency_window_days": recency_window_days,
        "density_neighbor_ranks": [DENSITY_NEIGHBOR_START, DENSITY_NEIGHBOR_END],
        "density_distances_used_miles": [round(value, 3) for value in density_distances],
        "subject_type_family": subject_family,
        "subject_sqft": round(subject_sqft) if subject_sqft else None,
        "fallback_used": not enough,
        "fallback_half_life_miles": DEFAULT_PROXIMITY_HALF_LIFE_MILES if not enough else None,
        "basis": (
            "median distance of recent physically comparable properties ranked 8 through 12"
            if enough else "insufficient suitable local references; deterministic 2.5-mile fallback"
        ),
    }


# The same subject asks for its density several times in a row (the comp step's search, every refine,
# the current-competition lookup) and it doesn't change minute to minute - its 20-mile query took
# 0.5-4 s on the live data (10/21, slow when the rows aren't in memory). Kept per subject for 30
# minutes; never in an accuracy-test run (the answer depends on the as-of date).
_DENSITY_CACHE: dict = {}
_DENSITY_CACHE_TTL_S = 1800


async def load_local_proximity_context(db: AsyncSession, subject: dict) -> dict:
    """Load a stable, filter-independent local reference population from the MLS mirror."""
    import time as _time
    from app.services import cma_time_machine
    _key = None
    if not cma_time_machine.current():
        _key = "|".join(str((subject or {}).get(k) or "") for k in (
            "listing_key", "latitude", "longitude", "above_grade_finished_area", "sqft", "lot_size",
            "year_built", "property_type", "home_type"))
        hit = _DENSITY_CACHE.get(_key)
        if hit and _time.monotonic() - hit[0] < _DENSITY_CACHE_TTL_S:
            return dict(hit[1])
    ctx = await _load_local_proximity_context(db, subject)
    if _key:
        if len(_DENSITY_CACHE) > 500:
            _DENSITY_CACHE.clear()
        _DENSITY_CACHE[_key] = (_time.monotonic(), dict(ctx))
    return ctx


async def _load_local_proximity_context(db: AsyncSession, subject: dict) -> dict:
    subject = dict(subject or {})
    if subject.get("listing_key"):
        authoritative = (await db.execute(
            select(Property).where(Property.listing_key == subject["listing_key"]).limit(1)
        )).scalar_one_or_none()
        if authoritative:
            authoritative_values = {
                "street_address": authoritative.street_address,
                "city": authoritative.city,
                "state": authoritative.state,
                "zipcode": authoritative.zipcode,
                "latitude": authoritative.latitude,
                "longitude": authoritative.longitude,
                "sqft": authoritative.living_area,
                "above_grade_finished_area": authoritative.above_grade_finished_area,
                "lot_size": authoritative.lot_size,
                "year_built": authoritative.year_built,
                "home_type": authoritative.home_type,
                "property_type": authoritative.home_type,
            }
            for key, value in authoritative_values.items():
                if subject.get(key) in (None, "") and value not in (None, ""):
                    subject[key] = value
    lat = _as_float(subject.get("latitude"))
    lng = _as_float(subject.get("longitude"))
    if lat is None or lng is None:
        return build_adaptive_proximity_context(subject, [])

    lat_offset, lng_offset = get_lat_long_offsets(lat, MAX_DENSITY_SEARCH_MILES)
    distance_expr = distance_miles_sql(Property.latitude, Property.longitude, lat, lng)
    query = select(
        Property.listing_key,
        Property.street_address,
        Property.city,
        Property.state,
        Property.zipcode,
        Property.latitude,
        Property.longitude,
        Property.living_area,
        Property.above_grade_finished_area,
        Property.home_type,
        Property.lot_size,
        Property.year_built,
        Property.home_status,
        Property.raw_mls_data,
    ).where(
        Property.latitude.between(lat - lat_offset, lat + lat_offset),
        Property.longitude.between(lng - lng_offset, lng + lng_offset),
        Property.is_manual.is_(False),
        distance_expr <= MAX_DENSITY_SEARCH_MILES,
    )
    subject_sqft = _as_float(
        subject.get("above_grade_finished_area") or subject.get("sqft") or subject.get("living_area")
    )
    if subject_sqft:
        reference_sqft = func.coalesce(Property.above_grade_finished_area, Property.living_area)
        query = query.where(reference_sqft.between(
            subject_sqft * DENSITY_SQFT_RATIO_MIN,
            subject_sqft * DENSITY_SQFT_RATIO_MAX,
        ))
    if subject.get("listing_key"):
        query = query.where(Property.listing_key != subject["listing_key"])

    surface = await _load_price_surface(db, subject, lat, lng, subject_sqft) if PRICE_SURFACE_ENABLED else []
    rows = (await db.execute(
        query.order_by(distance_expr.asc(), Property.listing_key.asc()).limit(1000)
    )).mappings().all()
    references = [
        {
            "listing_key": row["listing_key"],
            "street_address": row["street_address"],
            "city": row["city"],
            "state": row["state"],
            "zipcode": row["zipcode"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "sqft": row["living_area"],
            "above_grade_finished_area": row["above_grade_finished_area"],
            "home_type": row["home_type"],
            "property_type": row["home_type"],
            "lot_size": row["lot_size"],
            "year_built": row["year_built"],
            "status": row["home_status"],
            "close_date": (row["raw_mls_data"] or {}).get("CloseDate"),
            "sale_date": (row["raw_mls_data"] or {}).get("CloseDate"),
        }
        for row in rows
    ]
    ctx = build_adaptive_proximity_context(subject, references)
    if surface:
        ctx["_price_surface"] = surface
    return ctx


# Neighborhood price map (10/23): recent same-type sales of similar size around the subject, as
# (listing_key, lat, lng, $/sqft). The Match % and the value engine read a "local price level" off it
# for the subject and for each comp - in a city every comp shares the subject's school district,
# township and mailing city, so those said nothing about a Grays Ferry comp ($168/sqft) for a Point
# Breeze home ($240/sqft) half a mile away. Keys starting with "_" are never stored on a report.
PRICE_SURFACE_ENABLED = os.getenv("CMA_PRICE_SURFACE", "1") == "1"
PRICE_SURFACE_MILES = 6.0
PRICE_SURFACE_DAYS = 365
PRICE_SURFACE_SQFT_RATIO = (0.6, 1.6)
PRICE_SURFACE_MAX_ROWS = 5000


async def _load_price_surface(db: AsyncSession, subject: dict, lat: float, lng: float,
                              subject_sqft: Optional[float]) -> list:
    from app.services import cma_time_machine
    family = property_type_family(subject.get("property_type") or subject.get("home_type"))
    as_of = cma_time_machine.today()
    since = (as_of - timedelta(days=PRICE_SURFACE_DAYS)).isoformat()
    close_price = func.nullif(Property.raw_mls_data["ClosePrice"].astext, "")
    close_date = Property.raw_mls_data["CloseDate"].astext
    lat_offset, lng_offset = get_lat_long_offsets(lat, PRICE_SURFACE_MILES)
    distance_expr = distance_miles_sql(Property.latitude, Property.longitude, lat, lng)
    # Each sale's condition tier rides along (5th field) so the price level can be read condition-neutral
    # (cma_ai_service CMA_AREA_PRICE_COND - a block of unrenovated rentals is not a cheap location).
    with_cond = os.getenv("CMA_AREA_PRICE_COND", "0") == "1"
    cond_cols = (Property.raw_mls_data["PropertyCondition"].label("cond"),
                 Property.raw_mls_data["PublicRemarks"].astext.label("remarks")) if with_cond else ()
    query = select(
        Property.listing_key, Property.latitude, Property.longitude, Property.home_type,
        close_price.label("close_price"),
        func.coalesce(Property.above_grade_finished_area, Property.living_area).label("sqft"),
        *cond_cols,
    ).where(
        Property.latitude.between(lat - lat_offset, lat + lat_offset),
        Property.longitude.between(lng - lng_offset, lng + lng_offset),
        Property.is_manual.is_(False),
        func.upper(Property.home_status).like("CLOSED%"),
        close_date >= since, close_date <= as_of.isoformat(),
        distance_expr <= PRICE_SURFACE_MILES,
    )
    if subject_sqft:
        reference_sqft = func.coalesce(Property.above_grade_finished_area, Property.living_area)
        query = query.where(reference_sqft.between(subject_sqft * PRICE_SURFACE_SQFT_RATIO[0],
                                                   subject_sqft * PRICE_SURFACE_SQFT_RATIO[1]))
    rows = (await db.execute(query.order_by(distance_expr.asc()).limit(PRICE_SURFACE_MAX_ROWS))).mappings().all()
    tier_of = None
    if with_cond:
        from app.services.cma_ai_service import _condition_tier as tier_of
    out = []
    for row in rows:
        if family and property_type_family(row["home_type"]) != family:
            continue
        price, sqft = _as_float(row["close_price"]), _as_float(row["sqft"])
        if not price or price < 10000 or not sqft or sqft <= 0:
            continue
        item = (str(row["listing_key"]), float(row["latitude"]), float(row["longitude"]), round(price / sqft, 2))
        if tier_of:
            item += (tier_of({"property_condition": row["cond"], "public_remarks": row["remarks"]}),)
        out.append(item)
    return out
