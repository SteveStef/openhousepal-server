"""Extra shared facts for a town (one MarketArea row, reused by every agent who serves it):

  - commute (facts['commute']): drive time at a weekday-morning rush and train/bus time to the
    places people from THIS town actually go - picked automatically from where the town is (the
    nearest major city's downtown, plus a nearby job center, plus the nearest major airport), so a
    Main Line town gets Center City Philadelphia and a Northern Virginia town gets Downtown D.C.
    Google Routes API, ~6 route lookups per town, once a year.
  - flood (facts['flood']): the share of the town's homes inside FEMA's high-risk flood zones (the
    "Special Flood Hazard Area", zones A/AE/AO/AH/V/VE) - FEMA's free National Flood Hazard Layer,
    checked against the real coordinates of the town's homes. Once a year.
  - verify_places(): re-checks every researched business (restaurants, shops, groceries, medical)
    against Google - permanently closed ones and ones that turn out to be far from the town come
    off the page. Every few months (see town_upkeep_scheduler).
"""
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.database import MarketArea, Property
from app.services.usage_owner import charge_google
from app.services.areas import geography

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- where people commute to
# (name, lat, lng, tier) - tier 1 = a major metro's downtown, 2 = a regional city / job center.
_DESTINATIONS = [
    ("Center City Philadelphia", 39.9526, -75.1652, 1), ("Midtown Manhattan", 40.7549, -73.9840, 1),
    ("Downtown Washington, D.C.", 38.9007, -77.0367, 1), ("Downtown Baltimore", 39.2858, -76.6131, 1),
    ("Downtown Pittsburgh", 40.4406, -79.9959, 1), ("Downtown Richmond", 37.5407, -77.4360, 1),
    ("Downtown Norfolk", 36.8508, -76.2859, 1), ("Downtown Boston", 42.3555, -71.0565, 1),
    ("Downtown Chicago", 41.8818, -87.6298, 1), ("Downtown Atlanta", 33.7537, -84.3863, 1),
    ("Downtown Charlotte", 35.2271, -80.8431, 1), ("Downtown Raleigh", 35.7796, -78.6382, 1),
    ("Downtown Miami", 25.7743, -80.1937, 1), ("Downtown Orlando", 28.5384, -81.3789, 1),
    ("Downtown Tampa", 27.9506, -82.4572, 1), ("Downtown Nashville", 36.1627, -86.7816, 1),
    ("Downtown Dallas", 32.7767, -96.7970, 1), ("Downtown Houston", 29.7604, -95.3698, 1),
    ("Downtown Austin", 30.2672, -97.7431, 1), ("Downtown Denver", 39.7392, -104.9903, 1),
    ("Downtown Phoenix", 33.4484, -112.0740, 1), ("Downtown Los Angeles", 34.0522, -118.2437, 1),
    ("Downtown San Francisco", 37.7897, -122.3972, 1), ("Downtown Seattle", 47.6062, -122.3321, 1),
    ("Downtown Detroit", 42.3314, -83.0458, 1), ("Downtown Minneapolis", 44.9778, -93.2650, 1),
    ("Downtown Cleveland", 41.4993, -81.6944, 1), ("Downtown Columbus", 39.9612, -82.9988, 1),
    ("King of Prussia", 40.0893, -75.3963, 2), ("Downtown Wilmington, DE", 39.7447, -75.5484, 2),
    ("Princeton", 40.3573, -74.6672, 2), ("Downtown Trenton", 40.2206, -74.7597, 2),
    ("Downtown Newark, NJ", 40.7357, -74.1724, 2), ("Jersey City", 40.7178, -74.0431, 2),
    ("Downtown Allentown", 40.6023, -75.4714, 2), ("Downtown Harrisburg", 40.2598, -76.8826, 2),
    ("Downtown Lancaster", 40.0379, -76.3055, 2), ("Downtown Reading", 40.3356, -75.9269, 2),
    ("Downtown York, PA", 39.9626, -76.7277, 2), ("Downtown Scranton", 41.4090, -75.6624, 2),
    ("Tysons", 38.9187, -77.2311, 2), ("Rosslyn / Arlington", 38.8964, -77.0717, 2),
    ("Bethesda", 38.9847, -77.0947, 2), ("Reston Town Center", 38.9586, -77.3570, 2),
    ("Downtown Annapolis", 38.9784, -76.4922, 2), ("Downtown Frederick", 39.4143, -77.4105, 2),
    ("Downtown Fredericksburg", 38.3032, -77.4605, 2), ("Downtown Charlottesville", 38.0293, -78.4767, 2),
    ("Downtown Roanoke", 37.2710, -79.9414, 2), ("Downtown Virginia Beach", 36.8529, -75.9780, 2),
    ("Downtown Charleston, WV", 38.3498, -81.6326, 2), ("Downtown Morgantown", 39.6295, -79.9559, 2),
    ("Downtown Atlantic City", 39.3643, -74.4229, 2), ("Downtown Dover, DE", 39.1582, -75.5244, 2),
]
# (name, lat, lng, hub) - hub = a major commercial airport most people fly from.
_AIRPORTS = [
    ("Philadelphia International (PHL)", 39.8744, -75.2424, True), ("Newark Liberty (EWR)", 40.6895, -74.1745, True),
    ("JFK", 40.6413, -73.7781, True), ("LaGuardia (LGA)", 40.7769, -73.8740, True),
    ("Baltimore/Washington (BWI)", 39.1754, -76.6684, True), ("Reagan National (DCA)", 38.8512, -77.0402, True),
    ("Washington Dulles (IAD)", 38.9531, -77.4565, True), ("Richmond International (RIC)", 37.5052, -77.3197, True),
    ("Norfolk International (ORF)", 36.8946, -76.2012, True), ("Pittsburgh International (PIT)", 40.4915, -80.2329, True),
    ("Boston Logan (BOS)", 42.3656, -71.0096, True), ("Chicago O'Hare (ORD)", 41.9742, -87.9073, True),
    ("Atlanta (ATL)", 33.6407, -84.4277, True), ("Charlotte (CLT)", 35.2144, -80.9473, True),
    ("Raleigh-Durham (RDU)", 35.8801, -78.7880, True), ("Miami (MIA)", 25.7959, -80.2870, True),
    ("Orlando (MCO)", 28.4312, -81.3081, True), ("Tampa (TPA)", 27.9755, -82.5332, True),
    ("Nashville (BNA)", 36.1263, -86.6774, True), ("Dallas/Fort Worth (DFW)", 32.8998, -97.0403, True),
    ("Houston (IAH)", 29.9902, -95.3368, True), ("Austin (AUS)", 30.1975, -97.6664, True),
    ("Denver (DEN)", 39.8561, -104.6737, True), ("Phoenix (PHX)", 33.4352, -112.0101, True),
    ("Los Angeles (LAX)", 33.9416, -118.4085, True), ("San Francisco (SFO)", 37.6213, -122.3790, True),
    ("Seattle-Tacoma (SEA)", 47.4502, -122.3088, True), ("Detroit (DTW)", 42.2162, -83.3554, True),
    ("Minneapolis (MSP)", 44.8848, -93.2223, True), ("Cleveland (CLE)", 41.4124, -81.8498, True),
    ("Columbus (CMH)", 39.9980, -82.8919, True),
    ("Lehigh Valley (ABE)", 40.6524, -75.4404, False), ("Harrisburg (MDT)", 40.1935, -76.7634, False),
    ("Atlantic City (ACY)", 39.4576, -74.5772, False), ("Charlottesville (CHO)", 38.1386, -78.4529, False),
    ("Roanoke (ROA)", 37.3255, -79.9754, False), ("Charleston, WV (CRW)", 38.3731, -81.5932, False),
    ("Wilkes-Barre/Scranton (AVP)", 41.3385, -75.7234, False), ("State College (SCE)", 40.8493, -77.8487, False),
]


def _miles(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    return 2 * r * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def pick_destinations(lat: float, lng: float) -> list[dict]:
    """Up to 3: the nearest major downtown (within 90 mi, else simply the nearest), the nearest
    regional job center within 25 mi that isn't the same place, and the nearest hub airport
    (within 100 mi, else the nearest airport of any size)."""
    out = []
    ranked = sorted(_DESTINATIONS, key=lambda d: _miles(lat, lng, d[1], d[2]))
    major = next((d for d in ranked if d[3] == 1 and _miles(lat, lng, d[1], d[2]) <= 90), None) or next(d for d in ranked if d[3] == 1)
    out.append({"name": major[0], "lat": major[1], "lng": major[2], "kind": "city"})
    local = next((d for d in ranked if d[3] == 2 and _miles(lat, lng, d[1], d[2]) <= 25 and _miles(major[1], major[2], d[1], d[2]) > 5), None)
    if local:
        out.append({"name": local[0], "lat": local[1], "lng": local[2], "kind": "job_center"})
    ap = sorted(_AIRPORTS, key=lambda a: _miles(lat, lng, a[1], a[2]))
    hub = next((a for a in ap if a[3] and _miles(lat, lng, a[1], a[2]) <= 100), None) or ap[0]
    out.append({"name": hub[0], "lat": hub[1], "lng": hub[2], "kind": "airport"})
    return out


def _tz_for(lng: float) -> ZoneInfo:
    return ZoneInfo("America/New_York" if lng > -87.5 else "America/Chicago" if lng > -102 else "America/Denver" if lng > -114.5 else "America/Los_Angeles")


def _next_weekday_8am(lng: float) -> str:
    tz = _tz_for(lng)
    d = datetime.now(tz).date() + timedelta(days=1)
    while d.weekday() != 1:  # a Tuesday - a normal workday, not a Monday/Friday
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 8, 0, tzinfo=tz).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _route_matrix(client: httpx.AsyncClient, key: str, lat: float, lng: float, dests: list[dict], mode: str) -> dict[int, dict]:
    body = {
        "origins": [{"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}],
        "destinations": [{"waypoint": {"location": {"latLng": {"latitude": d["lat"], "longitude": d["lng"]}}}} for d in dests],
        "travelMode": mode,
        "departureTime": _next_weekday_8am(lng),
    }
    if mode == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE"
    r = await client.post("https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", json=body, timeout=30,
                          headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition"})
    r.raise_for_status()
    await charge_google("routes_matrix_element_traffic" if mode in ("DRIVE", "TRANSIT") else "routes_matrix_element", len(dests))
    out = {}
    for row in r.json():
        if row.get("condition") == "ROUTE_EXISTS" and row.get("duration"):
            out[row["destinationIndex"]] = {"minutes": round(int(str(row["duration"]).rstrip("s")) / 60), "miles": round((row.get("distanceMeters") or 0) / 1609.34, 1)}
    return out


async def refresh_commute(db: AsyncSession, area: MarketArea) -> Optional[dict]:
    key = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
    if not key or area.centroid_lat is None or area.centroid_lng is None:
        return None
    dests = pick_destinations(area.centroid_lat, area.centroid_lng)
    try:
        async with httpx.AsyncClient() as client:
            drive = await _route_matrix(client, key, area.centroid_lat, area.centroid_lng, dests, "DRIVE")
            try:
                transit = await _route_matrix(client, key, area.centroid_lat, area.centroid_lng, dests, "TRANSIT")
            except httpx.HTTPError:
                transit = {}
    except httpx.HTTPError as exc:
        logger.info("commute lookup failed", extra={"area": area.slug, "error": str(exc)})
        return None
    rows = []
    for i, d in enumerate(dests):
        dr, tr = drive.get(i), transit.get(i)
        if not dr:
            continue
        row = {"name": d["name"], "kind": d["kind"], "drive_minutes": dr["minutes"], "miles": dr["miles"]}
        # Transit only when it's a real option (not a 3-hour bus odyssey).
        if tr and tr["minutes"] <= max(90, dr["minutes"] * 2.5):
            row["transit_minutes"] = tr["minutes"]
        rows.append(row)
    if not rows:
        return None
    commute = {"destinations": rows, "departure": "weekday 8 AM", "computed_at": datetime.now(timezone.utc).isoformat(), "source": "Google Maps"}
    facts = dict(area.facts or {})
    facts["commute"] = commute
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return commute


# ---------------------------------------------------------------- FEMA flood zones
_NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"


def _point_in_rings(x: float, y: float, rings: list) -> bool:
    inside = False
    for ring in rings:  # even-odd across outer rings and holes
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
            j = i
    return inside


async def refresh_flood(db: AsyncSession, area: MarketArea) -> Optional[dict]:
    location_filter = geography.location_filter(Property, geography.scope_for_market_area(area))
    if location_filter is None:
        return None
    lat_col = Property.latitude
    lng_col = Property.longitude
    pts = (await db.execute(
        select(lat_col, lng_col).where(location_filter, lat_col.isnot(None), lng_col.isnot(None),
                                       geography.residential_for_sale_filter(Property)).limit(4000)
    )).all()
    pts = [(float(lng), float(lat)) for lat, lng in pts if lat and lng]
    if len(pts) < 20:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    env = f"{min(xs) - 0.002},{min(ys) - 0.002},{max(xs) + 0.002},{max(ys) + 0.002}"
    polys: list[tuple[str, list]] = []
    try:
        async with httpx.AsyncClient() as client:
            offset = 0
            while offset < 5000:
                r = await client.get(_NFHL, timeout=150, params={
                    "geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects", "where": "SFHA_TF='T'", "outFields": "FLD_ZONE",
                    "returnGeometry": "true", "geometryPrecision": 6, "f": "json", "resultOffset": offset, "resultRecordCount": 500,
                })
                r.raise_for_status()
                body = r.json()
                feats = body.get("features") or []
                polys += [(f["attributes"].get("FLD_ZONE") or "A", f["geometry"]["rings"]) for f in feats if f.get("geometry", {}).get("rings")]
                if not body.get("exceededTransferLimit") or not feats:
                    break
                offset += len(feats)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("FEMA flood lookup failed", extra={"area": area.slug, "error": str(exc)})
        return None

    def bbox(rings):
        flat = [pt for ring in rings for pt in ring]
        return min(p[0] for p in flat), min(p[1] for p in flat), max(p[0] for p in flat), max(p[1] for p in flat)
    boxes = [(zone, rings, bbox(rings)) for zone, rings in polys]
    zones: dict[str, int] = {}
    hits = 0
    for x, y in pts:
        for zone, rings, (x0, y0, x1, y1) in boxes:
            if x0 <= x <= x1 and y0 <= y <= y1 and _point_in_rings(x, y, rings):
                hits += 1
                zones[zone] = zones.get(zone, 0) + 1
                break
    flood = {
        "high_risk_pct": round(100 * hits / len(pts), 1),
        "homes_checked": len(pts),
        "zones": zones,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "FEMA National Flood Hazard Layer",
    }
    facts = dict(area.facts or {})
    facts["flood"] = flood
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return flood


# ---------------------------------------------------------------- keep researched places current
_BUSINESS_CATEGORIES = ("restaurants", "shopping", "grocery", "medical")
_PLACES_SEARCH = "https://places.googleapis.com/v1/places:searchText"


def _tokens(s: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if w not in {"the", "and", "of", "at", "inc", "llc", "co"}}


async def verify_places(db: AsyncSession, area: MarketArea) -> dict:
    """Each researched business is looked up on Google. Permanently closed -> removed. Found but
    more than 25 miles from the town -> removed (the research named the wrong place). Not found at
    all -> kept (plenty of real places, like a trail, aren't Google businesses). Removed entries
    are kept under the category's '_removed' list with the reason, for admin review."""
    key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not key or area.centroid_lat is None:
        return {"checked": 0}
    facts = dict(area.facts or {})
    checked = removed = 0
    now = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient() as client:
        for cat in _BUSINESS_CATEGORIES:
            block = dict(facts.get(cat) or {})
            ents = block.get("entities")
            if not isinstance(ents, list) or not ents:
                continue
            keep, gone = [], list(block.get("_removed") or [])
            for e in ents:
                if not isinstance(e, dict) or not e.get("name"):
                    keep.append(e)
                    continue
                try:
                    r = await client.post(_PLACES_SEARCH, timeout=15, headers={
                        "X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.displayName,places.businessStatus,places.location"},
                        json={"textQuery": f"{e['name']}, {e.get('city') or area.name}, {area.state}",
                              "locationBias": {"circle": {"center": {"latitude": area.centroid_lat, "longitude": area.centroid_lng}, "radius": 30000}},
                              "maxResultCount": 1})
                    r.raise_for_status()
                    places = r.json().get("places") or []
                    await charge_google("places_text")
                except httpx.HTTPError:
                    keep.append(e)
                    continue
                checked += 1
                p = places[0] if places else None
                same = p and len(_tokens(e["name"]) & _tokens((p.get("displayName") or {}).get("text", ""))) >= max(1, len(_tokens(e["name"])) // 2)
                if same and p.get("businessStatus") == "CLOSED_PERMANENTLY":
                    gone.append({**e, "removed_reason": "permanently closed (Google)", "removed_at": now})
                    removed += 1
                    continue
                if same and p.get("location") and _miles(area.centroid_lat, area.centroid_lng, p["location"]["latitude"], p["location"]["longitude"]) > 25:
                    gone.append({**e, "removed_reason": "not near this town (Google)", "removed_at": now})
                    removed += 1
                    continue
                keep.append({**e, "verified_at": now} if same else e)
            block["entities"] = keep
            if gone:
                block["_removed"] = gone[-50:]
            facts[cat] = block
    facts["_places_verified_at"] = now
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return {"checked": checked, "removed": removed}
