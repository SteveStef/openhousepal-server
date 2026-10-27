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


def _normalize_name(value: Optional[str], drop_square: bool = True) -> str:
    text = _NAME_RE.sub(" ", (value or "").lower()).strip()
    words = [w for w in text.split() if w not in {"neighborhood", "area"}]
    if drop_square and len(words) > 1 and words[-1] == "square":
        words.pop()
    return " ".join(words)


def _feature_names(feature: dict[str, Any], drop_square: bool = True) -> set[str]:
    props = feature.get("properties") or {}
    names = {props.get("NAME"), props.get("LISTNAME"), props.get("MAPNAME")}
    normalized = {_normalize_name(str(name).replace("_", " "), drop_square) for name in names if name}
    for raw in [str(name).replace("_", " ") for name in names if name]:
        if " - " in raw:
            normalized.add(_normalize_name(raw.split(" - ", 1)[0], drop_square))
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

# Names people use for an area the city map splits into several pieces (owner, 10/27). Germantown is
# six pieces; most of Port Richmond's homes sit in the map's "Richmond" piece (its "Port Richmond"
# piece is mostly waterfront industry); the map's "University City" is only the Penn/Drexel campus.
_PHILLY_COMBINED: dict[str, list[str]] = {
    "germantown": ["Germantown - Penn Knox", "Germantown - Morton", "Germantown - Westside",
                   "West Central Germantown", "Southwest Germantown", "East Germantown"],
    "port richmond": ["Port Richmond", "Richmond"],
    "university city": ["University City", "Powelton", "West Powelton", "Spruce Hill", "Walnut Hill",
                        "Cedar Park", "Garden Court", "Woodland Terrace"],
}


def _polygons(geometry: dict[str, Any]) -> list:
    if geometry.get("type") == "Polygon":
        return [geometry.get("coordinates") or []]
    if geometry.get("type") == "MultiPolygon":
        return list(geometry.get("coordinates") or [])
    return []


def _ring_area(ring: list) -> float:
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) / 2


def _ring_contains(ring: list, lng: float, lat: float) -> bool:
    inside = False
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i][:2], ring[i + 1][:2]
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def merge_pieces(geometries: list[dict[str, Any]]) -> dict[str, Any]:
    """One outline for neighboring map pieces: the edges two pieces share cancel out, so the map shows
    a single border instead of the inside seams. The pieces come from one dataset and share exact
    corner points; if they don't line up, the pieces are kept side by side (same area, seams shown)."""
    polys = [poly for g in geometries for poly in _polygons(g) if poly]
    fallback = {"type": "MultiPolygon", "coordinates": polys}

    def key(pt):
        return (round(float(pt[0]), 7), round(float(pt[1]), 7))

    edges: dict = {}
    for poly in polys:
        for ring in poly:
            for i in range(len(ring) - 1):
                a, b = key(ring[i]), key(ring[i + 1])
                if a == b:
                    continue
                if (b, a) in edges:
                    edges[(b, a)] -= 1
                    if not edges[(b, a)]:
                        del edges[(b, a)]
                else:
                    edges[(a, b)] = edges.get((a, b), 0) + 1
    if any(n != 1 for n in edges.values()):
        return fallback
    nxt: dict = {}
    for a, b in edges:
        nxt.setdefault(a, []).append(b)
    rings = []
    while nxt:
        start = next(iter(nxt))
        ring, cur = [start], start
        while True:
            outs = nxt.get(cur)
            if not outs:
                return fallback  # an open edge - the pieces don't share exact corners
            b = outs.pop()
            if not outs:
                del nxt[cur]
            ring.append(b)
            cur = b
            if cur == start:
                break
        # Slivers where two pieces' edges miss each other by a hair (well under a house lot) are dropped.
        if len(ring) >= 4 and abs(_ring_area(ring)) > 1e-7:
            rings.append([list(p) for p in ring])
    if not rings:
        return fallback
    outer_sign = 1 if sum(_ring_area(poly[0]) for poly in polys) > 0 else -1
    outers = [r for r in rings if _ring_area(r) * outer_sign > 0]
    holes = [r for r in rings if _ring_area(r) * outer_sign < 0]
    merged = [[r] for r in outers]
    for h in holes:
        home = next((m for m in merged if _ring_contains(m[0], h[0][0], h[0][1])), None)
        if home is None:
            return fallback
        home.append(h)
    if len(merged) == 1:
        return {"type": "Polygon", "coordinates": merged[0]}
    return {"type": "MultiPolygon", "coordinates": merged}


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

    pieces = _PHILLY_COMBINED.get(_normalize_name(name, drop_square=False))
    if pieces:
        found = [f for piece in pieces for f in _PHILLY_FEATURE_CACHE if (f.get("properties") or {}).get("MAPNAME") == piece]
        if len(found) == len(pieces):
            geometry = merge_pieces([f.get("geometry") or {} for f in found])
            bounds = _geometry_bounds(geometry)
            if bounds:
                return {
                    "type": "polygon", "geometry": geometry, "bounds": bounds,
                    "source": "opendataphilly_philadelphia_neighborhoods",
                    "source_name": "OpenDataPhilly Philadelphia Neighborhoods",
                    "source_url": PHILADELPHIA_NEIGHBORHOODS_GEOJSON_URL, "license": "CC-BY-4.0",
                    "matched_name": " ".join(w.capitalize() for w in _normalize_name(name, drop_square=False).split()),
                    "map_pieces": pieces,
                }

    # The full name first: "Logan Square" is its own map piece, not "Logan". Dropping "Square" is only
    # a fallback so "Rittenhouse Square" still finds the "Rittenhouse" piece.
    exact = _normalize_name(name, drop_square=False)
    wanted = _normalize_name(name)
    match = next((feature for feature in _PHILLY_FEATURE_CACHE if exact in _feature_names(feature, drop_square=False)), None)         or next((feature for feature in _PHILLY_FEATURE_CACHE if wanted in _feature_names(feature)), None)
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
