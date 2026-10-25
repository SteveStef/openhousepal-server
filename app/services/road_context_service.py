"""Free road/street classification for a lat/lng, from OpenStreetMap.

Answers the plain question a buyer actually cares about: "is this a quiet street I can walk
around, or does the home front / sit near a major road that carries real traffic noise?" It reads
the road *type* every road on the map is tagged with (residential / living_street vs. secondary /
primary / trunk / motorway) plus the posted speed when OSM has it, within ~170 m of the point.

- Measures how close each road actually passes the point (~32 m = "fronts on it", ~90 m = "on
  the block", up to ~180 m = "in the area"), so "fronts a major road" is only said when true.
- No API key, no billing. Uses the public Overpass API (main endpoint + one mirror fallback).
- Cached persistently in ExploreNearbyCache (key "road:v3:<lat5>:<lng5>", ~1-year TTL - a road's
  classification doesn't change) so it's a one-time lookup per location, ever.
- Best-effort: any timeout / error / no-coords returns None and the caller falls back to its
  address-suffix heuristic. Never raises into CMA generation.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ExploreNearbyCache

logger = logging.getLogger(__name__)

_CACHE_TTL_DAYS = 365.0
# Distance bands (metres). We fetch everything within _AREA_M, then bucket each road by how
# close it actually passes to the property:
#   <= _ON_STREET_M   the road the house fronts / sits directly on (~2 lot depths)
#   <= _NEAR_M        the immediate block - a road here is heard and shapes the street's feel
#   <= _AREA_M        the wider neighbourhood - context, but a busy road this far with houses
#                     between it and you doesn't make a quiet interior street "noisy"
_ON_STREET_M = 32
_NEAR_M = 90
_AREA_M = 180
_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Some Overpass front-ends (and the WAF in front of overpass-api.de) 406/403 a bare
# python-httpx User-Agent. A real identifying UA + explicit JSON Accept fixes it and is also
# the requested Overpass etiquette.
_HEADERS = {
    "User-Agent": "OpenHousePal/1.0 (real-estate CMA street classifier; +https://openhousepal.com)",
    "Accept": "application/json",
}

# "Major" = an arterial or highway - the roads that carry real traffic noise. Split by weight:
# HEAVY (highway / big arterial) is audible from ~90 m; a plain 2-lane SECONDARY connector only
# really matters if you're right on it (~45 m). tertiary / unclassified are mid-size connectors,
# busier than pure residential but not an arterial.
_MAJOR_HEAVY = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}
_MAJOR_SECONDARY = {"secondary", "secondary_link"}
_MAJOR = _MAJOR_HEAVY | _MAJOR_SECONDARY
_SECONDARY_ON_STREET_M = 45
_QUIET_ONLY = {"residential", "living_street", "service", "track", "pedestrian", "cycleway", "footway", "path"}

_SPEED_RE = re.compile(r"(\d{2,3})")


def _parse_speed_mph(raw) -> Optional[int]:
    if not raw:
        return None
    s = str(raw).lower()
    m = _SPEED_RE.search(s)
    if not m:
        return None
    n = int(m.group(1))
    if "km" in s or "kph" in s:
        n = round(n / 1.609)
    return n if 5 <= n <= 85 else None


def _round5(v: float) -> str:
    return f"{float(v):.5f}"


def _min_dist_m(lat: float, lng: float, geom: list) -> Optional[float]:
    """Shortest distance (metres) from (lat,lng) to a way's polyline. Equirectangular projection -
    plenty accurate at the <200 m scale we care about."""
    pts = [(p.get("lat"), p.get("lon")) for p in (geom or []) if p.get("lat") is not None and p.get("lon") is not None]
    if not pts:
        return None
    m_per_deg_lat = 111_320.0
    m_per_deg_lng = 111_320.0 * math.cos(math.radians(lat))
    px, py = 0.0, 0.0  # the property, at the local origin
    xs = [((lon - lng) * m_per_deg_lng, (plat - lat) * m_per_deg_lat) for plat, lon in pts]
    if len(xs) == 1:
        return math.hypot(xs[0][0] - px, xs[0][1] - py)
    best = None
    for (ax, ay), (bx, by) in zip(xs, xs[1:]):
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        best = d if best is None else min(best, d)
    return best


def _classify(lat: float, lng: float, elements: list[dict]) -> dict:
    """elements = Overpass ways with `tags` + `geometry`. Buckets each road by how close it
    actually passes to (lat,lng), then reads the verdict off the closest bands."""
    roads = []  # (dist_m, kind, tags)
    for el in elements or []:
        tags = el.get("tags") or {}
        kind = str(tags.get("highway") or "").lower()
        if not kind:
            continue
        d = _min_dist_m(lat, lng, el.get("geometry"))
        if d is None or d > _AREA_M:
            continue
        roads.append((d, kind, tags))

    if not roads:
        return {"verdict": "unknown", "road_types": [], "max_speed_mph": None,
                "nearby_road_names": [], "dead_end": False, "summary": None, "source": "openstreetmap"}

    roads.sort(key=lambda r: r[0])
    on_street = [r for r in roads if r[0] <= _ON_STREET_M] or [roads[0]]  # always know the closest
    near = [r for r in roads if r[0] <= _NEAR_M]

    def _closest(band, kinds):
        hits = [r for r in band if r[1] in kinds]
        return min(hits, key=lambda r: r[0]) if hits else None

    # Fronting ANY major road, OR a heavy arterial/highway within the block, OR a plain 2-lane
    # secondary within ~45 m -> "through". A secondary 45-90 m off is only a "neutral" note.
    major_on = _closest(on_street, _MAJOR)
    heavy_near = _closest(near, _MAJOR_HEAVY)
    secondary_close = _closest([r for r in near if r[0] <= _SECONDARY_ON_STREET_M], _MAJOR_SECONDARY)
    secondary_block = _closest(near, _MAJOR_SECONDARY)
    major_area = _closest(roads, _MAJOR)

    on_kinds = {r[1] for r in on_street}
    speeds = [s for r in near for s in [_parse_speed_mph(r[2].get("maxspeed"))] if s]
    max_speed = max(speeds) if speeds else None
    names = sorted({str(r[2].get("name")).strip() for r in near if r[2].get("name")})
    has_dead_end = any(str(r[2].get("noexit") or "").lower() in ("yes", "true") for r in on_street)
    on_street_speed = _parse_speed_mph(on_street[0][2].get("maxspeed"))
    _through_road = major_on or heavy_near or secondary_close

    if major_on or (on_street_speed and on_street_speed >= 40):
        verdict = "through"
        summary = ("The home fronts / sits directly on a major road"
                   + (f" ({on_street[0][2].get('name')})" if on_street[0][2].get("name") else "")
                   + (f", posted ~{on_street_speed} mph" if on_street_speed else "")
                   + " - expect steady traffic noise.")
    elif _through_road:
        verdict = "through"
        summary = (f"A major road ({_through_road[2].get('name') or 'arterial/highway'}"
                   + (f", ~{max_speed} mph" if max_speed and max_speed >= 40 else "")
                   + f") runs about {round(_through_road[0] / 10) * 10} m away - traffic is likely audible.")
    elif secondary_block:
        verdict = "neutral"
        summary = (f"A 2-lane connector road ({secondary_block[2].get('name') or 'unnamed'}) is about "
                   f"{round(secondary_block[0] / 10) * 10} m off - some traffic, but not an arterial or highway.")
    elif on_kinds & _QUIET_ONLY and not major_area:
        verdict = "quiet"
        summary = ("Quiet " + ("dead-end / cul-de-sac" if has_dead_end else "residential street")
                   + " - no major road in the immediate area.")
    elif on_kinds & _QUIET_ONLY and major_area:
        verdict = "quiet"
        summary = (f"Residential street; the nearest major road is ~{round(major_area[0] / 10) * 10} m off "
                   "(a couple of blocks, houses in between) - minor impact.")
    else:
        verdict = "neutral"
        summary = "On/near a mid-size connector street - busier than a pure residential street, but no arterial or highway."

    return {
        "verdict": verdict,
        "road_types": sorted({r[1] for r in on_street}),
        "nearby_road_types": sorted({r[1] for r in near}),
        "max_speed_mph": max_speed,
        "nearby_road_names": names[:6],
        "dead_end": has_dead_end,
        "on_street_distance_m": round(on_street[0][0]),
        "nearest_major_road_m": round(major_area[0]) if major_area else None,
        "summary": summary,
        "source": "openstreetmap",
    }


async def _read_cache(db: AsyncSession, key: str) -> Optional[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CACHE_TTL_DAYS)
    row = await db.execute(
        select(ExploreNearbyCache.payload).where(
            ExploreNearbyCache.cache_key == key, ExploreNearbyCache.fetched_at >= cutoff,
        )
    )
    return row.scalar_one_or_none()


async def _write_cache(db: AsyncSession, key: str, payload: dict) -> None:
    try:
        stmt = pg_insert(ExploreNearbyCache).values(
            id=str(uuid.uuid4()), cache_key=key, payload=payload,
        ).on_conflict_do_update(
            index_elements=["cache_key"], set_={"payload": payload, "fetched_at": func.now()},
        )
        await db.execute(stmt)
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("road_context cache write failed for %s", key, exc_info=True)


async def _fetch_overpass(lat: float, lng: float, race: bool = False) -> Optional[list[dict]]:
    # `out geom` gives each way's polyline so we can measure how close it really passes, instead
    # of treating "within the radius" as one bucket.
    query = (
        f"[out:json][timeout:12];"
        f"way(around:{_AREA_M},{lat},{lng})[highway];"
        f"out tags geom;"
    )
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        if race:
            # The subject: ask every mirror at once and take the first good answer. One at a time,
            # three slow mirrors cost 60 s before the fourth was tried, and 602 Media Line Rd's
            # lookup gave up - so the report never knew the home fronts Media Line Road (10/25).
            async def one(url):
                resp = await client.post(url, data={"data": query})
                if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                    return [e for e in ((resp.json() or {}).get("elements") or []) if (e.get("tags") or {}).get("highway")]
                raise ValueError(resp.status_code)

            for attempt in range(2):
                pending = {asyncio.ensure_future(one(u)) for u in _ENDPOINTS}
                try:
                    while pending:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            if not task.cancelled() and task.exception() is None:
                                return task.result()
                finally:
                    for task in pending:
                        task.cancel()
                if attempt == 0:
                    await asyncio.sleep(2.0)
            return None
        # Overpass instances 4xx/5xx under load or WAF - walk the mirror list, then one backoff
        # retry over the whole list.
        for attempt in range(2):
            for url in _ENDPOINTS:
                try:
                    resp = await client.post(url, data={"data": query})
                    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                        elements = (resp.json() or {}).get("elements") or []
                        return [e for e in elements if (e.get("tags") or {}).get("highway")]
                except Exception:  # noqa: BLE001
                    continue
            if attempt == 0:
                await asyncio.sleep(2.0)
    return None


# ---- Road-name memory (10/25) -------------------------------------------------------------------
# Every successful lookup also remembers what KIND of road each named street near that point is, by
# name and ~1 km map square. When a home's own lookup gets no answer (the public map servers are
# often overloaded), its address still names the road it fronts: 602 Media Line Rd's lookup failed
# while 538 S Media Line Rd's, 0.07 mi away, said Media Line Road is a major road - so the subject
# fronts it too. Only a MAJOR road is ever concluded this way; a residential name proves nothing
# about a busy road a few houses off.
_ROAD_SUFFIX = {
    "rd": "road", "st": "street", "ave": "avenue", "av": "avenue", "ln": "lane", "dr": "drive",
    "ct": "court", "cir": "circle", "blvd": "boulevard", "pk": "pike", "pke": "pike", "hwy": "highway",
    "pl": "place", "ter": "terrace", "pkwy": "parkway", "tpke": "turnpike", "rte": "route", "sq": "square",
}
_ROAD_DIRECTION = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}


def road_name_key(name) -> Optional[str]:
    """'602 S Media Line Rd' / 'MEDIA LINE ROAD' -> 'media line road' (house number, unit, direction
    words and suffix abbreviations normalised)."""
    s = str(name or "").lower()
    s = re.sub(r"\b(?:apt|unit|suite|ste)\b.*$|#.*$", "", s)
    s = re.sub(r"^\s*\d+[a-z]?\b", "", s)
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]
    if words and words[0] in _ROAD_DIRECTION and len(words) > 2:
        words = words[1:]
    if words and words[-1] in _ROAD_DIRECTION and len(words) > 2:
        words = words[:-1]
    if words:
        words[-1] = _ROAD_SUFFIX.get(words[-1], words[-1])
    return " ".join(words) or None


def _cell(lat: float, lng: float) -> str:
    return f"{round(lat, 2):.2f}:{round(lng, 2):.2f}"


def _neighbour_cells(lat: float, lng: float) -> list[str]:
    return [_cell(lat + dy, lng + dx) for dy in (-0.01, 0, 0.01) for dx in (-0.01, 0, 0.01)]


async def _remember_road_names(db: AsyncSession, lat: float, lng: float, elements: list[dict]) -> None:
    named: dict[str, dict] = {}
    for el in elements or []:
        tags = el.get("tags") or {}
        key = road_name_key(tags.get("name"))
        kind = str(tags.get("highway") or "").lower()
        if not key or not kind:
            continue
        d = named.setdefault(key, {"name": str(tags.get("name")).strip(), "kinds": set(), "speed": None})
        d["kinds"].add(kind)
        sp = _parse_speed_mph(tags.get("maxspeed"))
        if sp and (d["speed"] is None or sp > d["speed"]):
            d["speed"] = sp
    for key, d in named.items():
        await _write_cache(db, f"roadname:v1:{_cell(lat, lng)}:{key}",
                           {"name": d["name"], "kinds": sorted(d["kinds"]), "max_speed_mph": d["speed"]})


async def road_context_from_address(db: AsyncSession, lat, lng, address) -> Optional[dict]:
    """The fallback when a home's own lookup failed: a verdict from the remembered class of the road its
    address is on. Only 'through' (a major road) - otherwise None."""
    key = road_name_key(address)
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not key:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CACHE_TTL_DAYS)
    rows = (await db.execute(
        select(ExploreNearbyCache.payload).where(
            ExploreNearbyCache.cache_key.in_([f"roadname:v1:{c}:{key}" for c in _neighbour_cells(lat, lng)]),
            ExploreNearbyCache.fetched_at >= cutoff,
        )
    )).scalars().all()
    kinds = {k for r in rows for k in (r or {}).get("kinds") or []}
    major = sorted(kinds & _MAJOR)
    if not major:
        return None
    speed = max([(r or {}).get("max_speed_mph") or 0 for r in rows]) or None
    name = next(((r or {}).get("name") for r in rows if (r or {}).get("name")), key.title())
    return {
        "verdict": "through", "road_types": major, "nearby_road_types": sorted(kinds), "max_speed_mph": speed,
        "nearby_road_names": [name], "dead_end": False, "on_street_distance_m": None, "nearest_major_road_m": None,
        "summary": (f"The home's address is on {name}, a major road" + (f" (posted ~{speed} mph)" if speed else "")
                    + " - expect steady traffic noise. From the map data for this road near the home."),
        "source": "openstreetmap-road-name",
    }


async def get_road_context(db: AsyncSession, lat, lng, race: bool = False) -> Optional[dict]:
    """Verdict dict {'verdict': 'quiet'|'neutral'|'through'|'unknown', 'road_types', 'max_speed_mph',
    'nearby_road_names', 'summary', 'source'} or None if it couldn't be determined."""
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None

    key = f"road:v3:{_round5(lat)}:{_round5(lng)}"
    cached = await _read_cache(db, key)
    if cached is not None:
        return cached or None

    elements = await _fetch_overpass(lat, lng, race=race)
    if elements is None:
        return None  # transient - don't cache a failure
    result = _classify(lat, lng, elements)
    await _write_cache(db, key, result)
    await _remember_road_names(db, lat, lng, elements)
    return result


async def get_road_contexts(db: AsyncSession, points: list[tuple], addresses: Optional[list] = None) -> list[Optional[dict]]:
    """Best-effort batch (subject first, then comps). Concurrency-limited (Overpass is strict about
    anonymous concurrency); the subject asks every mirror at once. A point with no answer falls back to
    the remembered class of the road its address is on (road_context_from_address), then to None ->
    the caller falls back to the address suffix."""
    sem = asyncio.Semaphore(2)

    async def one(pt, race=False):
        if not pt or pt[0] is None or pt[1] is None:
            return None
        async with sem:
            try:
                return await get_road_context(db, pt[0], pt[1], race=race)
            except Exception:  # noqa: BLE001
                logger.debug("road_context lookup failed for %s", pt, exc_info=True)
                return None

    if not points:
        return []
    first = await one(points[0], race=True)
    out = [first] + list(await asyncio.gather(*(one(p) for p in points[1:])))
    addresses = list(addresses or [])
    for i, (pt, ctx) in enumerate(zip(points, out)):
        addr = addresses[i] if i < len(addresses) else None
        if ctx is None and addr and pt and pt[0] is not None and pt[1] is not None:
            try:
                out[i] = await road_context_from_address(db, pt[0], pt[1], addr)
            except Exception:  # noqa: BLE001
                logger.debug("road-name fallback failed for %s", addr, exc_info=True)
    return out
