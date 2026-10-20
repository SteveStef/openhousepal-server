"""The CMA builder's "enter the property manually" path - a home that isn't on the MLS yet (most
seller CMAs). Everything here is a helper for the subject step; nothing changes how a CMA values.

  * area_suggestions: the school district, township, county, water and sewer most of the MLS homes
    right around the pin share. Pre-filling them in the MLS's own spelling is what lets the
    engine's market-area match (_relevance_market_area_score) score a manual subject like an MLS
    one - a hand-typed "Lower Merion School District" never matches a comp's "LOWER MERION".
  * geocode_address: the fallback when Google's address suggestions don't load in the browser
    (the comp search needs a latitude/longitude).
  * google_photo: a Street View photo of the front of the house, or an aerial view, fetched with
    the backend's Google key (the live browser key has no Static Maps access) and stored in S3.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Property, User
from app.services import s3_service
from app.utils.geo import distance_miles_sql, get_lat_long_offsets
from app.utils.image_validation import photo_to_jpeg

logger = get_logger(__name__)

SUBJECT_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_NEARBY_RADIUS_MILES = 1.0
_NEARBY_LIMIT = 80
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_STREET_VIEW_URL = "https://maps.googleapis.com/maps/api/streetview"
_STREET_VIEW_META_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
_STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
# Placeholder values Bright uses for "no subdivision" - never suggested.
_JUNK_NAME_RE = re.compile(
    r"^\s*(none|n/?a|no|not applicable|not in (a )?(development|subdivision)|unknown|other)\b", re.IGNORECASE)


def _google_key() -> Optional[str]:
    return os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")


def _flat(value) -> list[str]:
    if value in (None, "", [], {}):
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(v).strip() for v in items if str(v or "").strip()]


def _ranked(counter: Counter, limit: int = 8) -> list[str]:
    return [name for name, _ in counter.most_common(limit)]


async def area_suggestions(db: AsyncSession, lat: float, lng: float) -> dict:
    """Most common location facts among the nearest MLS homes (any status) - the nearest home's
    value breaks a tie, and the subdivision only counts homes within a few hundred feet."""
    lat_off, lng_off = get_lat_long_offsets(lat, _NEARBY_RADIUS_MILES)
    dist = distance_miles_sql(Property.latitude, Property.longitude, lat, lng)
    rows = (await db.execute(
        select(Property.school_district_name, Property.township, Property.county, Property.city,
               Property.zipcode, Property.subdivision_name, Property.water_source, Property.sewer,
               dist.label("d"))
        .where(Property.latitude.between(lat - lat_off, lat + lat_off),
               Property.longitude.between(lng - lng_off, lng + lng_off),
               Property.is_manual.is_(False),
               dist <= _NEARBY_RADIUS_MILES)
        .order_by(dist.asc())
        .limit(_NEARBY_LIMIT)
    )).all()

    counters = {k: Counter() for k in ("school_district", "township", "county", "city", "zipcode",
                                         "subdivision_name", "water_source", "sewer")}
    for i, r in enumerate(rows):
        # Nearest homes count a little more, so a boundary street leans to its own side.
        w = 1.0 + max(0.0, 1.0 - i / 10)
        for key, value in (("school_district", r.school_district_name), ("township", r.township),
                           ("county", r.county), ("city", r.city), ("zipcode", r.zipcode)):
            if value:
                counters[key][str(value).strip()] += w
        if r.subdivision_name and r.d is not None and r.d <= 0.12 and not _JUNK_NAME_RE.match(str(r.subdivision_name)):
            counters["subdivision_name"][str(r.subdivision_name).strip()] += w
        for key, value in (("water_source", r.water_source), ("sewer", r.sewer)):
            for v in _flat(value):
                counters[key][v] += w

    def top(key: str) -> Optional[str]:
        best = counters[key].most_common(1)
        return best[0][0] if best else None

    return {
        "nearby_count": len(rows),
        "school_district": top("school_district"),
        "township": top("township"),
        "county": top("county"),
        "city": top("city"),
        "zipcode": top("zipcode"),
        "subdivision_name": top("subdivision_name"),
        "water_source": top("water_source"),
        "sewer": top("sewer"),
        "school_district_options": _ranked(counters["school_district"]),
        "township_options": _ranked(counters["township"]),
        "subdivision_options": _ranked(counters["subdivision_name"]),
    }


def _component(components: list, kind: str, short: bool = False) -> Optional[str]:
    for c in components or []:
        if kind in (c.get("types") or []):
            return c.get("short_name" if short else "long_name")
    return None


def _formatted_city(formatted: Optional[str]) -> Optional[str]:
    parts = [p.strip() for p in str(formatted or "").split(",")]
    return parts[1] if len(parts) >= 4 and parts[1] else None


async def geocode_address(address: str) -> Optional[dict]:
    """Address text -> location + parts, via Google Geocoding with the backend key."""
    key = _google_key()
    if not key or not address.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(_GEOCODE_URL, params={"address": address, "components": "country:US", "key": key})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning("CMA manual subject geocode failed", extra={"error": str(exc)})
        return None
    results = payload.get("results") or []
    if not results:
        return None
    best = results[0]
    comps = best.get("address_components") or []
    loc = (best.get("geometry") or {}).get("location") or {}
    if loc.get("lat") is None or loc.get("lng") is None:
        return None
    # Short route name ("Dogwood Ln", not "Dogwood Lane") - the MLS spells streets that way, and the
    # comp search drops the subject's own past sales by matching the street text exactly.
    number, route = _component(comps, "street_number"), _component(comps, "route", short=True)
    return {
        "street_address": " ".join(p for p in (number, route) if p) or None,
        # Google leaves `locality` out for some mailing towns (417 Dogwood Ln, Wallingford PA) - the
        # formatted address still names it ("417 Dogwood Ln, Wallingford, PA 19086, USA").
        "city": (_component(comps, "locality") or _component(comps, "postal_town")
                 or _component(comps, "neighborhood") or _formatted_city(best.get("formatted_address"))),
        "state": _component(comps, "administrative_area_level_1", short=True),
        "zipcode": _component(comps, "postal_code"),
        "county": _component(comps, "administrative_area_level_2"),
        "township": _component(comps, "administrative_area_level_3"),
        "latitude": float(loc["lat"]),
        "longitude": float(loc["lng"]),
        "precise": (best.get("geometry") or {}).get("location_type") in ("ROOFTOP", "RANGE_INTERPOLATED"),
        "formatted_address": best.get("formatted_address"),
    }


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compass bearing (0-360) from point 1 to point 2."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


async def google_photo(lat: float, lng: float, kind: str) -> bytes:
    """JPEG bytes of the house from Google: 'street_view' (camera on the nearest street, turned
    toward the pin) or 'aerial' (satellite view centred on it). Raises ValueError with a message
    the agent can read when there's nothing to show."""
    key = _google_key()
    if not key:
        raise ValueError("Google photos aren't set up on this server yet - upload a photo instead.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        if kind == "street_view":
            # The metadata call is free and says whether there is any imagery here at all - the
            # image call would otherwise return (and bill) a grey "no imagery" placeholder.
            meta = (await client.get(_STREET_VIEW_META_URL, params={
                "location": f"{lat},{lng}", "source": "outdoor", "radius": 50, "key": key})).json()
            if meta.get("status") != "OK":
                raise ValueError("Google has no street photo of this address - try the aerial view or upload a photo.")
            # Aim from where the photo was taken toward the house - left to itself the camera
            # looked down the road (417 Dogwood Ln test, 10/20).
            params = {"size": "640x480", "fov": 70, "pitch": 5, "return_error_code": "true", "key": key}
            pano_loc = meta.get("location") or {}
            if meta.get("pano_id") and pano_loc.get("lat") is not None:
                params.update(pano=meta["pano_id"], heading=round(_bearing(pano_loc["lat"], pano_loc["lng"], lat, lng)))
            else:
                params.update(location=f"{lat},{lng}", source="outdoor", radius=50)
            resp = await client.get(_STREET_VIEW_URL, params=params)
        else:
            resp = await client.get(_STATIC_MAP_URL, params={
                "center": f"{lat},{lng}", "zoom": 19, "size": "640x480", "scale": 2,
                "maptype": "satellite", "key": key})
    if resp.status_code != 200 or not (resp.headers.get("content-type") or "").startswith("image/"):
        logger.warning("CMA manual subject Google photo failed", extra={"kind": kind, "status": resp.status_code})
        raise ValueError("Couldn't get a photo from Google right now - upload a photo instead.")
    return photo_to_jpeg(resp.content)


async def save_subject_photo(db: AsyncSession, user: User, jpeg: bytes) -> str:
    """Stores a subject photo in S3 against the agent's storage cap and returns its public URL."""
    from app.services import storage_ledger_service
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    await storage_ledger_service.enforce_cap(db, user.id, config, getattr(user, "plan_tier", None), len(jpeg))
    url = s3_service.upload_bytes(jpeg, "cma-subject-photos", ".jpg", "image/jpeg")
    await storage_ledger_service.record(db, user.id, "listing_photo", url, len(jpeg))
    return url
