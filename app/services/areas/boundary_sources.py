"""Automatic polygon boundary providers for shared MarketArea geography.

This deliberately starts with provider-specific, legally clear datasets rather than treating a
generic geocoder's viewport as a true boundary. Public page callers still go through
areas.geography; this module only decides whether a newly resolved named area has a better
stored definition than the Google Places approximation.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Optional

import httpx

from app.config.logging import get_logger

logger = get_logger(__name__)

PHILADELPHIA_NEIGHBORHOODS_GEOJSON_URL = (
    "https://raw.githubusercontent.com/opendataphilly/odp-data-storage/master/"
    "philadelphia-neighborhoods/philadelphia-neighborhoods.geojson"
)

_NAME_RE = re.compile(r"[^a-z0-9]+")
_PHILLY_NAMES = {"philadelphia", "city of philadelphia", "phila"}


def _normalize_name(value: Optional[str]) -> str:
    text = _NAME_RE.sub(" ", (value or "").lower()).strip()
    words = [w for w in text.split() if w not in {"neighborhood", "area"}]
    if len(words) > 1 and words[-1] == "square":
        words.pop()
    return " ".join(words)


def _feature_names(feature: dict[str, Any]) -> set[str]:
    props = feature.get("properties") or {}
    names = {props.get("NAME"), props.get("LISTNAME"), props.get("MAPNAME")}
    normalized = {_normalize_name(str(name).replace("_", " ")) for name in names if name}
    for raw in [str(name).replace("_", " ") for name in names if name]:
        if " - " in raw:
            normalized.add(_normalize_name(raw.split(" - ", 1)[0]))
    return {name for name in normalized if name}


def _iter_positions(geometry: dict[str, Any]):
    if geometry.get("type") == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        polygons = []
    for polygon in polygons:
        for ring in polygon:
            for position in ring:
                try:
                    yield float(position[0]), float(position[1])
                except (TypeError, ValueError, IndexError):
                    continue


def _geometry_bounds(geometry: dict[str, Any]) -> Optional[dict[str, float]]:
    positions = list(_iter_positions(geometry))
    if not positions:
        return None
    lngs = [p[0] for p in positions]
    lats = [p[1] for p in positions]
    return {"min_lat": min(lats), "max_lat": max(lats), "min_lng": min(lngs), "max_lng": max(lngs)}


@lru_cache(maxsize=1)
def _philly_neighborhoods_cache_key() -> str:
    return PHILADELPHIA_NEIGHBORHOODS_GEOJSON_URL


async def _fetch_philadelphia_neighborhoods() -> list[dict[str, Any]]:
    # Cached by process lifetime through the caller below after the first successful fetch. These
    # boundaries change rarely; the canonical copy is still stored on MarketArea after a match.
    async with httpx.AsyncClient(
        timeout=20.0,
        headers={"User-Agent": "OpenHousePal boundary resolver (contact: support@openhousepal.com)"},
    ) as client:
        response = await client.get(_philly_neighborhoods_cache_key())
        response.raise_for_status()
        payload = response.json()
    return payload.get("features") or []


_PHILLY_FEATURE_CACHE: Optional[list[dict[str, Any]]] = None


async def _philadelphia_neighborhood_boundary(name: str, parent_area_name: Optional[str], state: str) -> Optional[dict[str, Any]]:
    if state.upper() != "PA" or _normalize_name(parent_area_name) not in _PHILLY_NAMES:
        return None

    global _PHILLY_FEATURE_CACHE
    try:
        if _PHILLY_FEATURE_CACHE is None:
            _PHILLY_FEATURE_CACHE = await _fetch_philadelphia_neighborhoods()
    except httpx.HTTPError as exc:
        logger.warning("Philadelphia neighborhood boundary fetch failed", extra={"error": str(exc)})
        return None

    wanted = _normalize_name(name)
    match = next((feature for feature in _PHILLY_FEATURE_CACHE if wanted in _feature_names(feature)), None)
    if not match:
        return None

    geometry = match.get("geometry") or {}
    bounds = _geometry_bounds(geometry)
    if not bounds:
        return None
    props = match.get("properties") or {}
    return {
        "type": "polygon",
        "geometry": geometry,
        "bounds": bounds,
        "source": "opendataphilly_philadelphia_neighborhoods",
        "source_name": "OpenDataPhilly Philadelphia Neighborhoods",
        "source_url": PHILADELPHIA_NEIGHBORHOODS_GEOJSON_URL,
        "license": "CC-BY-4.0",
        "matched_name": props.get("MAPNAME") or props.get("LISTNAME") or props.get("NAME"),
    }


async def resolve_named_area_boundary(name: str, parent_area_name: Optional[str], state: str) -> Optional[dict[str, Any]]:
    """Return a trustworthy polygon geography definition for a named market area, if available."""
    return await _philadelphia_neighborhood_boundary(name, parent_area_name, state)
