"""Shared geographic scoping for MarketArea-backed website inventory.

The public website should query the current local Bright mirror at request time, but the
definition of "this area" belongs to the shared MarketArea row. This module turns that stored
definition into SQL filters that can be reused by inventory, stats, agent-owned listings, and
open houses without each caller inventing its own location logic.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import and_, case, func, literal, not_, or_

from app.utils.geo import EARTH_RADIUS_MILES, get_lat_long_offsets

DEFAULT_NEIGHBORHOOD_RADIUS_MILES = 0.75
_GEOGRAPHY_PRECISION = {"radius": 1, "bounds": 2, "polygon": 3}

# Homes a buyer on a real estate website is actually shopping for: residential property types at a
# believable sale price. The Bright sync lands commercial space, land and $20/sq-ft lease rows in
# the same `properties` table - without this they leaked into every "homes for sale" count and
# dragged a town's "from $20" price range onto public pages.
RESIDENTIAL_HOME_TYPES = ("SINGLE_FAMILY", "TOWNHOUSE", "CONDO", "MULTI_FAMILY", "FARM", "Single Family")
MIN_PLAUSIBLE_SALE_PRICE = 50_000


def polygon_from_param(text: Optional[str], max_points: int = 120) -> Optional[dict[str, Any]]:
    """A visitor-drawn search area as it travels in a URL / form answer - "lat,lng;lat,lng;..."
    (IDX map search "Draw", saved into a Showcase) - turned into the GeoJSON Polygon the search
    builder already understands (CollectionPreferences.search_polygon). None for anything that
    isn't a real shape (under 3 points, out-of-range numbers, junk)."""
    if not text or not isinstance(text, str):
        return None
    ring: list[list[float]] = []
    for pair in text.strip().split(";")[: max_points + 1]:
        try:
            lat_s, lng_s = pair.split(",")
            lat, lng = float(lat_s), float(lng_s)
        except (ValueError, TypeError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        ring.append([round(lng, 6), round(lat, 6)])
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return {"type": "Polygon", "coordinates": [ring]}


def residential_for_sale_filter(model):
    return and_(
        or_(model.home_type.in_(RESIDENTIAL_HOME_TYPES), model.home_type.is_(None)),
        or_(model.price.is_(None), model.price >= MIN_PLAUSIBLE_SALE_PRICE),
    )


def is_residential_for_sale(home_type, price) -> bool:
    """Python twin of residential_for_sale_filter for rows that already came back from a shared
    query builder (PropertyService results)."""
    if home_type is not None and home_type not in RESIDENTIAL_HOME_TYPES:
        return False
    return price is None or price >= MIN_PLAUSIBLE_SALE_PRICE


def zip_scope(zip_codes: Optional[list[str]], city: Optional[str] = None, state: Optional[str] = None) -> dict[str, Any]:
    return {
        "kind": "zip_city",
        "zip_codes": [z for z in (zip_codes or []) if z],
        "city": city,
        "state": state,
    }


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_bounds(raw: dict[str, Any]) -> Optional[dict[str, float]]:
    values = {
        "min_lat": _as_float(raw.get("min_lat")),
        "max_lat": _as_float(raw.get("max_lat")),
        "min_lng": _as_float(raw.get("min_lng")),
        "max_lng": _as_float(raw.get("max_lng")),
    }
    if any(v is None for v in values.values()):
        return None
    return {
        "min_lat": min(values["min_lat"], values["max_lat"]),
        "max_lat": max(values["min_lat"], values["max_lat"]),
        "min_lng": min(values["min_lng"], values["max_lng"]),
        "max_lng": max(values["min_lng"], values["max_lng"]),
    }


def radius_definition(lat: float, lng: float, radius_miles: float = DEFAULT_NEIGHBORHOOD_RADIUS_MILES, source: str = "centroid_radius") -> dict[str, Any]:
    return {
        "type": "radius",
        "center": {"lat": float(lat), "lng": float(lng)},
        "radius_miles": float(radius_miles),
        "source": source,
    }


def precision_rank(definition: Optional[dict[str, Any]]) -> int:
    return _GEOGRAPHY_PRECISION.get((definition or {}).get("type"), 0)


def is_more_precise(candidate: Optional[dict[str, Any]], current: Optional[dict[str, Any]]) -> bool:
    return precision_rank(candidate) > precision_rank(current)


def scope_for_market_area(area) -> dict[str, Any]:
    area_type = getattr(area, "area_type", "city") or "city"
    state = getattr(area, "state", None)
    if area_type != "neighborhood":
        scope = zip_scope(getattr(area, "zip_codes", None), city=getattr(area, "name", None), state=state)
        # A town that is formally a township (Radnor, Lower Merion) also owns the homes whose MLS
        # "township" is that name even when their postal city differs (Wayne, Villanova). Only
        # when the area record says so - matching townships blindly pulled all of Haverford
        # Township (Havertown) onto the Haverford village page.
        scope["township"] = (getattr(area, "municipality_type", None) or "").lower() == "township"
        return scope

    geography = dict(getattr(area, "geography", None) or {})
    scope: dict[str, Any] = {
        "kind": geography.get("type"),
        "state": state,
        "parent_city": getattr(area, "parent_area_name", None),
        "area_type": area_type,
    }

    if geography.get("type") == "polygon":
        geometry = geography.get("geometry") or {}
        bounds = _normalized_bounds(geography.get("bounds") or {})
        if geometry.get("type") in ("Polygon", "MultiPolygon") and bounds:
            return {
                **scope,
                "kind": "polygon",
                "geometry": geometry,
                "bounds": bounds,
                "source": geography.get("source"),
            }

    if geography.get("type") == "bounds":
        bounds = _normalized_bounds(geography.get("bounds") or {})
        if bounds:
            return {**scope, "kind": "bounds", "bounds": bounds, "source": geography.get("source")}

    if geography.get("type") == "radius":
        center = geography.get("center") or {}
        lat, lng = _as_float(center.get("lat")), _as_float(center.get("lng"))
        radius = _as_float(geography.get("radius_miles"))
        if lat is not None and lng is not None and radius is not None and radius > 0:
            return {**scope, "kind": "radius", "center": {"lat": lat, "lng": lng}, "radius_miles": radius, "source": geography.get("source")}

    # Existing neighborhood rows created before the geography field can still be scoped safely
    # from their stored centroid, without falling back to all ZIP/city inventory.
    lat, lng = _as_float(getattr(area, "centroid_lat", None)), _as_float(getattr(area, "centroid_lng", None))
    if lat is not None and lng is not None:
        definition = radius_definition(lat, lng, source="market_area_centroid_radius_fallback")
        return {
            **scope,
            "kind": "radius",
            "center": definition["center"],
            "radius_miles": definition["radius_miles"],
            "source": definition["source"],
        }

    return {"kind": "none", "state": state, "area_type": area_type}


def _distance_miles_expr(lat_column, lng_column, center_lat: float, center_lng: float):
    dlat = (func.radians(lat_column) - func.radians(center_lat)) / 2
    dlng = (func.radians(lng_column) - func.radians(center_lng)) / 2
    haversine = (
        func.pow(func.sin(dlat), 2)
        + func.cos(func.radians(center_lat)) * func.cos(func.radians(lat_column)) * func.pow(func.sin(dlng), 2)
    )
    return 2 * EARTH_RADIUS_MILES * func.asin(func.sqrt(haversine))


def _ring_filter(model, ring: list[list[float]]):
    # GeoJSON positions are [longitude, latitude]. Horizontal edges are skipped to avoid a
    # divide-by-zero in the ray-casting expression; they do not affect the crossing count.
    if len(ring) < 4:
        return literal(False)
    crossings = literal(0)
    lat_col = model.latitude
    lng_col = model.longitude
    for a, b in zip(ring, ring[1:]):
        try:
            lng1, lat1 = float(a[0]), float(a[1])
            lng2, lat2 = float(b[0]), float(b[1])
        except (TypeError, ValueError, IndexError):
            continue
        if lat1 == lat2:
            continue
        crosses_lat = (literal(lat1) > lat_col) != (literal(lat2) > lat_col)
        intersect_lng = ((literal(lng2) - literal(lng1)) * (lat_col - literal(lat1)) / (literal(lat2) - literal(lat1))) + literal(lng1)
        crossings = crossings + case((and_(crosses_lat, lng_col < intersect_lng), 1), else_=0)
    return (crossings % 2) == 1


def _polygon_filter(model, geometry: dict[str, Any]):
    if geometry.get("type") == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        return literal(False)

    polygon_filters = []
    for polygon in polygons:
        if not polygon:
            continue
        outer = _ring_filter(model, polygon[0])
        holes = [not_(_ring_filter(model, ring)) for ring in polygon[1:]]
        polygon_filters.append(and_(outer, *holes))
    return or_(*polygon_filters) if polygon_filters else literal(False)


def location_filter(model, scope: Optional[dict[str, Any]]):
    if not scope:
        return None

    kind = scope.get("kind")
    if kind == "zip_city":
        city = scope.get("city")
        state = scope.get("state")
        if city:
            # A town's MLS city name (and its township, where the table carries one) is the
            # tightest definition we have. ZIP codes are shared between neighbouring towns - Wayne
            # and Radnor are both 19087 - so scoping a town page by ZIP alone made two different
            # neighborhood pages show the same homes and the same numbers.
            name_match = [model.city.ilike(city)]
            if scope.get("township") and hasattr(model, "township"):
                name_match.append(model.township.ilike(city))
            clause = or_(*name_match)
            if state and hasattr(model, "state"):
                clause = and_(clause, model.state == state)
            return clause
        zip_codes = scope.get("zip_codes") or []
        if zip_codes:
            return model.zipcode.in_(zip_codes)
        return None

    filters = []
    state = scope.get("state")
    if state:
        filters.append(model.state == state)
    parent_city = scope.get("parent_city")
    if parent_city:
        filters.append(model.city.ilike(parent_city))

    if kind == "bounds":
        bounds = scope.get("bounds") or {}
        return and_(
            *filters,
            model.latitude.isnot(None),
            model.longitude.isnot(None),
            model.latitude >= bounds["min_lat"],
            model.latitude <= bounds["max_lat"],
            model.longitude >= bounds["min_lng"],
            model.longitude <= bounds["max_lng"],
        )

    if kind == "polygon":
        bounds = scope.get("bounds") or {}
        geometry = scope.get("geometry") or {}
        return and_(
            *filters,
            model.latitude.isnot(None),
            model.longitude.isnot(None),
            model.latitude >= bounds["min_lat"],
            model.latitude <= bounds["max_lat"],
            model.longitude >= bounds["min_lng"],
            model.longitude <= bounds["max_lng"],
            _polygon_filter(model, geometry),
        )

    if kind == "radius":
        center = scope.get("center") or {}
        lat, lng = center.get("lat"), center.get("lng")
        radius_miles = scope.get("radius_miles")
        if lat is None or lng is None or not radius_miles:
            return None
        lat_offset, lng_offset = get_lat_long_offsets(lat, radius_miles)
        distance = _distance_miles_expr(model.latitude, model.longitude, float(lat), float(lng))
        return and_(
            *filters,
            model.latitude.isnot(None),
            model.longitude.isnot(None),
            model.latitude >= float(lat) - lat_offset,
            model.latitude <= float(lat) + lat_offset,
            model.longitude >= float(lng) - lng_offset,
            model.longitude <= float(lng) + lng_offset,
            distance <= float(radius_miles),
        )

    return None
