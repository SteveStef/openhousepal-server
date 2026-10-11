"""Free-text place geocoding for Area centroids.

The legacy Google Geocoding API (`maps.googleapis.com/maps/api/geocode/json`) - what
`app/services/google_geocoding_service.py` uses for properties - does NOT work with this
deployment's `GOOGLE_PLACES_API_KEY` (confirmed live: it's restricted to Places API only in
Google Cloud Console, so the legacy Geocoding API returns `REQUEST_DENIED`). Rather than
requiring a second API key, this uses the same Places API (New) `searchText` endpoint
`nearby_amenities_service.py` already successfully calls for property amenities - a plain text
query with no location bias returns a place (with its own lat/lng) directly, which is exactly a
geocode. Reuses that module's URL/field-mask constants instead of redeclaring them.
"""
import os
from typing import Any, Optional

import httpx

from app.config.logging import get_logger
from app.services.areas import geography
from app.services.nearby_amenities_service import FIELD_MASK, PLACES_TEXT_URL
from app.utils.geo import haversine_distance

logger = get_logger(__name__)


async def geocode_place(name: str, state: str) -> tuple[float, float] | None:
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        logger.info("Area geocoding skipped: no GOOGLE_PLACES_API_KEY configured")
        return None

    text_query = f"{name}, {state}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                PLACES_TEXT_URL,
                json={"textQuery": text_query},
                headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Area geocoding request failed", extra={"place": text_query, "error": str(exc)})
        return None

    places = payload.get("places") or []
    if not places:
        return None
    location = places[0].get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


_NAMED_AREA_FIELD_MASK = FIELD_MASK + ",places.addressComponents,places.types,places.viewport"
_AREA_PLACE_TYPES = {"neighborhood", "sublocality", "sublocality_level_1", "locality", "political"}
_POINT_PLACE_TYPES = {"park", "point_of_interest", "establishment", "tourist_attraction"}
_POINT_LIKE_VIEWPORT_MAX_SPAN_MILES = 0.35


def _score_named_area_place(place: dict[str, Any], query_name: str) -> int:
    types = set(place.get("types") or [])
    display = ((place.get("displayName") or {}).get("text") or "").strip().lower()
    query = query_name.strip().lower()
    score = 0
    if display == query:
        score += 4
    elif query and query in display:
        score += 2
    if types & _AREA_PLACE_TYPES:
        score += 8
    if "neighborhood" in types:
        score += 4
    if types & _POINT_PLACE_TYPES:
        score -= 5
    return score


def _choose_named_area_place(places: list[dict[str, Any]], query_name: str) -> dict[str, Any]:
    return max(places, key=lambda p: _score_named_area_place(p, query_name))


def _viewport_bounds(place: dict[str, Any]) -> Optional[dict[str, float]]:
    viewport = place.get("viewport") or {}
    low = viewport.get("low") or viewport.get("southwest") or {}
    high = viewport.get("high") or viewport.get("northeast") or {}
    try:
        lat1, lng1 = float(low["latitude"]), float(low["longitude"])
        lat2, lng2 = float(high["latitude"]), float(high["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "min_lat": min(lat1, lat2),
        "max_lat": max(lat1, lat2),
        "min_lng": min(lng1, lng2),
        "max_lng": max(lng1, lng2),
    }


def _viewport_spans_miles(bounds: dict[str, float]) -> tuple[float, float]:
    mid_lat = (bounds["min_lat"] + bounds["max_lat"]) / 2
    height = haversine_distance(bounds["min_lat"], bounds["min_lng"], bounds["max_lat"], bounds["min_lng"])
    width = haversine_distance(mid_lat, bounds["min_lng"], mid_lat, bounds["max_lng"])
    return height, width


def _geography_definition(place: dict[str, Any], lat: float, lng: float) -> dict[str, Any]:
    bounds = _viewport_bounds(place)
    if bounds:
        height, width = _viewport_spans_miles(bounds)
        if max(height, width) >= _POINT_LIKE_VIEWPORT_MAX_SPAN_MILES:
            return {"type": "bounds", "bounds": bounds, "source": "google_places_viewport"}
    return geography.radius_definition(lat, lng, source="google_places_centroid_radius_fallback")


async def resolve_named_place(name: str, state: str) -> dict | None:
    """Canonical resolution for the "Add Market Area / Neighborhood" flow (Rittenhouse Square,
    Main Line, Fishtown, etc.) - unlike geocode_place (which just needs a centroid for an
    already-known city/town name), this also needs the actual containing city so the area can be
    stored and displayed as "Rittenhouse Square, Philadelphia, PA" rather than a bare name with no
    context, and so a future geographic listing filter has a real parent to reason about.

    Uses Places API (New) `addressComponents` (not regex-parsing of formattedAddress) to reliably
    pull out the containing locality - the same searchText endpoint geocode_place/nearby_amenities
    already use, just with one extra field in the mask. Returns None if the place can't be
    resolved at all; returns a result with parent_city=None if resolved but no locality component
    was present (e.g. an unincorporated area), which the caller should treat as "resolved, but
    couldn't determine a parent" rather than a hard failure.
    """
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        logger.info("Named place resolution skipped: no GOOGLE_PLACES_API_KEY configured")
        return None

    text_query = f"{name}, {state}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                PLACES_TEXT_URL,
                json={"textQuery": text_query},
                headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _NAMED_AREA_FIELD_MASK},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Named place resolution request failed", extra={"place": text_query, "error": str(exc)})
        return None

    places = payload.get("places") or []
    if not places:
        return None
    place = _choose_named_area_place(places, name)
    location = place.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    if lat is None or lng is None:
        return None

    components = place.get("addressComponents") or []

    def _component(*types: str, field: str = "longText") -> Optional[str]:
        for c in components:
            if any(t in (c.get("types") or []) for t in types):
                return c.get(field) or c.get("longText")
        return None

    parent_city = _component("locality", "postal_town")
    admin_state = _component("administrative_area_level_1", field="shortText")
    resolved_name = ((place.get("displayName") or {}).get("text") or name).strip()

    return {
        "name": resolved_name,
        "state": (admin_state or state).strip(),
        "lat": float(lat),
        "lng": float(lng),
        "parent_city": parent_city,
        "formatted_address": place.get("formattedAddress"),
        "geography": _geography_definition(place, float(lat), float(lng)),
    }
