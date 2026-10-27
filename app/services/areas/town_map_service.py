"""Free map facts for a town (one shared MarketArea row, reused by every agent who serves it):

  - boundaries (facts['boundaries']): the real lines behind a town name, from the U.S. Census
    (TIGERweb, free, no key) - the town's own outline (its ZIP area for a mailing-address town like
    Haverford, the township itself for a township like Radnor, the drawn shape for a neighborhood),
    plus the townships/boroughs and public school districts it overlaps, each with the share of
    the town's homes that sit inside it (real borders, checked against every home's coordinates -
    not the MLS's mailing-address field). Drawn on the town page map.
  - walk (facts['walk']): how much is within an easy walk of the town's homes - the share of homes
    with a grocery store / restaurants or cafes / a park / a train or trolley stop (never a
    tourist railway) within about a 15-minute walk, plus the stops themselves. OpenStreetMap (Overpass API, free, no key).
    Our own measure, not the licensed "Walk Score" (its free plan excludes subscription services).

Both are free, so the daily upkeep job refreshes them once a year without counting against its paid
cap (town_upkeep_scheduler).
"""
import asyncio
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.database import MarketArea, Property
from app.services.usage_owner import charge_google
from app.services.areas import geography
from app.services.areas import boundary_service as bs

logger = logging.getLogger(__name__)

_OVERPASS = "https://overpass-api.de/api/interpreter"
_MIN_SHARE = 3  # % of the town's homes - a sliver of a neighbor township isn't worth a line
_PLACES_SEARCH = "https://places.googleapis.com/v1/places:searchText"


async def _home_points(db: AsyncSession, area: MarketArea, limit: int = 4000) -> list[tuple[float, float]]:
    """(lng, lat) of the town's homes - for sale, pending and sold, whatever the MLS has."""
    location_filter = geography.location_filter(Property, geography.scope_for_market_area(area))
    if location_filter is None:
        return []
    rows = (await db.execute(
        select(Property.latitude, Property.longitude)
        .where(location_filter, Property.latitude.isnot(None), Property.longitude.isnot(None),
               geography.residential_for_sale_filter(Property)).limit(limit)
    )).all()
    return [(float(lng), float(lat)) for lat, lng in rows if lat and lng]


def _shares(pts: list[tuple[float, float]], shapes: list[list]) -> list[int]:
    """Each home counted once, in the FIRST shape (in list order) that contains it."""
    boxes = [bs.bbox(r) for r in shapes]
    counts = [0] * len(shapes)
    for x, y in pts:
        for i, rings in enumerate(shapes):
            if bs.contains(rings, x, y, boxes[i]):
                counts[i] += 1
                break
    return [round(100 * c / len(pts)) if pts else 0 for c in counts]


def _squash(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower().replace("the ", ""))


def _survey_private_in(x0: float, y0: float, x1: float, y1: float) -> list[dict]:
    """Private schools inside the box from the federal private school survey - the same file the
    Cheat Sheet uses, with each school's real grades. Preschools and early-childhood centers (top
    grade below 3rd, or one grade only) are left off, like on the Cheat Sheet (10/26: Malvern's map
    showed "Charlestown Playhouse" and a kindergarten)."""
    from app.services import nearby_amenities_service as nas
    out = []
    for name, city, state, lat, lng, lowest, highest, typology, students in nas._load_private_schools():
        if not (x0 <= lng <= x1 and y0 <= lat <= y1):
            continue
        if highest < nas.PRIVATE_SCHOOL_MIN_HIGHEST_GRADE or lowest == highest or bs._NOT_A_SCHOOL.search(name):
            continue
        grades = f"{nas._pss_grade_name(lowest)}-{nas._pss_grade_name(highest)}"
        # "BISHOP WALSH SCHOOL,K-12/DIOCESE" - the survey's own note after the comma isn't the name
        out.append({"id": f"pss:{_squash(name)}:{round(lat, 4)}", "name": nas._private_school_display_name(name.split(",")[0]),
                    "lat": round(lat, 6), "lng": round(lng, 6), "grades": grades,
                    **({"special_education": True} if typology == nas.PRIVATE_SCHOOL_SPECIAL_EDUCATION_TYPOLOGY else {})})
    return bs.dedupe_schools(out)


def _same_school(name: str, lat: float, lng: float, have: list[dict]) -> bool:
    """"Fusion Academy Malvern" = "Fusion Malvern"; "Villa Maria Academy" = its Lower/High School campus."""
    from app.services import nearby_amenities_service as nas
    ours = nas._school_name_tokens(name)
    for s in have:
        theirs = nas._school_name_tokens(s["name"])
        if not ours or not theirs or _miles(lat, lng, s["lat"], s["lng"]) > 1.5:
            continue
        shared = len(ours & theirs)
        if shared / len(ours) >= 0.6 or shared / len(theirs) >= 0.6:
            return True
    return False


async def _researched_private(client: httpx.AsyncClient, area: MarketArea, have: list[dict]) -> list[dict]:
    """Private schools the town research named that the federal list doesn't have (it's a voluntary
    survey - The Haverford School isn't in it). The place check (town_places_check) already put each
    one on the map; an older record gets one Google lookup each, kept only when Google finds a school
    within 3 miles."""
    key = os.getenv("GOOGLE_PLACES_API_KEY")
    entities = (((area.facts or {}).get("private_schools") or {}).get("entities") or [])[:10]
    if not entities or area.centroid_lat is None:
        return []
    known = {_squash(s["name"]) for s in have}
    out = []
    for e in entities:
        n = e.get("name") or ""
        if not n or _squash(n) in known or bs._NOT_A_SCHOOL.search(n):
            continue
        if e.get("lat") is not None and e.get("lng") is not None:
            if _miles(area.centroid_lat, area.centroid_lng, e["lat"], e["lng"]) <= 3 and not _same_school(n, e["lat"], e["lng"], have + out):
                out.append({"id": f"g:{_squash(n)}", "name": n, "lat": round(e["lat"], 6), "lng": round(e["lng"], 6),
                            "grades": (e.get("category") or "").replace("Grades ", "").replace("Grade ", "") or None})
            continue
        if not key:
            continue
        try:
            r = await client.post(_PLACES_SEARCH, timeout=20,
                                  headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.displayName,places.location,places.types"},
                                  json={"textQuery": f"{n}, {area.name}, {area.state}", "maxResultCount": 1,
                                        "locationBias": {"circle": {"center": {"latitude": area.centroid_lat, "longitude": area.centroid_lng}, "radius": 8000}}})
            r.raise_for_status()
            p = (r.json().get("places") or [None])[0]
            await charge_google("places_text")
        except (httpx.HTTPError, ValueError):
            continue
        if not p or not p.get("location") or "school" not in " ".join(p.get("types") or []):
            continue
        lat, lng = p["location"]["latitude"], p["location"]["longitude"]
        if _miles(area.centroid_lat, area.centroid_lng, lat, lng) > 3 or _same_school(n, lat, lng, have + out):
            continue
        out.append({"id": f"g:{_squash(n)}", "name": n, "lat": round(lat, 6), "lng": round(lng, 6), "grades": e.get("grades_served")})
    return out


# ---------------------------------------------------------------- counties
# A mailing-address town can cross county lines (Ardmore: Montgomery + Delaware; Wayne/19087:
# Delaware, Chester and Montgomery) and a Virginia city is often no county at all - an
# "independent city" Census treats as a county-equivalent (Charlottesville is not in Albemarle
# County, 10/27). Every county with a real share of homes is kept, each marked county or
# independent city.
_COUNTY_MIN_SHARE = 3


def _county_kind(attrs: dict) -> str:
    # COUNTYCC C7 = an incorporated place independent of any county (Virginia's cities, Baltimore,
    # St. Louis, Carson City); LSADC 25 = "city" as the county-level name.
    if attrs.get("COUNTYCC") == "C7" or attrs.get("LSADC") == "25":
        return "independent_city"
    return "county"


def county_rows(features: list[dict], shares: list[int]) -> list[dict]:
    """[{name, share, kind}] for every county-level area holding 3%+ of the town's homes, biggest
    first. An independent city is named by its base name ("Charlottesville"), a county by its
    full name ("Montgomery County")."""
    rows = []
    for f, sh in zip(features, shares):
        a = f.get("attributes") or {}
        kind = _county_kind(a)
        name = (a.get("BASENAME") if kind == "independent_city" else a.get("NAME")) or a.get("NAME")
        if name and sh >= _COUNTY_MIN_SHARE:
            rows.append({"name": name, "share": sh, "kind": kind})
    return sorted(rows, key=lambda r: -r["share"])


def county_label(rows: list[dict]) -> Optional[str]:
    """The page's words: "Montgomery County", "Montgomery and Delaware counties", "Independent city"
    (a town that is all one independent city), "Charlottesville (independent city) and Albemarle
    County" (a mailing area that's partly city, partly county). TownPage.tsx shows it as
    facts['county_label']."""
    if not rows:
        return None
    if all(r["kind"] == "county" for r in rows):
        rows = sorted(rows, key=lambda r: -(r.get("share") or 0))
        if len(rows) > 1 and (rows[0].get("share") or 0) >= 70:
            # Mostly one county: Princeton is a Mercer County town whose mailing area reaches a little
            # into Middlesex and Somerset - "Mercer, Middlesex and Somerset counties" read as equal parts.
            rest = [re.sub(r"\s+County$", "", r["name"]) for r in rows[1:]]
            tail = rest[0] if len(rest) == 1 else ", ".join(rest[:-1]) + " and " + rest[-1]
            return f"{rows[0]['name']} (some homes in {tail} {'County' if len(rest) == 1 else 'counties'})"
        return _join_counties([r["name"] for r in rows])
    if len(rows) == 1:
        return "Independent city"
    ordered = [r for r in rows if r["kind"] == "independent_city"] + [r for r in rows if r["kind"] == "county"]
    parts = [f"{r['name']} (independent city)" if r["kind"] == "independent_city" else r["name"] for r in ordered]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _join_counties(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    bare = [re.sub(r"\s+County$", "", n) for n in names]
    return (", ".join(bare[:-1]) + " and " + bare[-1]) + " counties"


def _apply_counties(area: MarketArea, rows: list[dict]) -> None:
    """facts['counties'] = every row; area.county = the biggest real county (an independent city
    is not a county - the column stays blank for a town that's mostly one, so no page or card
    says "Charlottesville city County")."""
    facts = dict(area.facts or {})
    facts["counties"] = rows
    facts["county_label"] = county_label(rows)  # what the town page shows (TownPage.tsx)
    area.facts = facts
    flag_modified(area, "facts")
    top = rows[0] if rows else None
    if top and top["kind"] == "county" and top["share"] >= 50:
        area.county = top["name"]
    elif top and top["kind"] == "independent_city":
        area.county = None
    elif top and not area.county:
        area.county = top["name"]


async def _refresh_counties_with(client: httpx.AsyncClient, area: MarketArea, pts: list, env: dict) -> list[dict]:
    feats = await bs.tiger(client, bs.COUNTY, with_geometry=True, **env)
    if not feats:
        return []
    rows = county_rows(feats, _shares(pts, [f["geometry"]["rings"] for f in feats]))
    if rows:
        _apply_counties(area, rows)
    return rows


async def refresh_counties(db: AsyncSession, area: MarketArea) -> Optional[list[dict]]:
    """Counties only (free Census TIGERweb, no other lookups) - for a one-off backfill."""
    pts = await _home_points(db, area)
    if len(pts) < 20:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "OpenHousePal/1.0"}) as client:
            rows = await _refresh_counties_with(client, area, pts, bs.envelope(min(xs), min(ys), max(xs), max(ys)))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("Census county lookup failed", extra={"area": area.slug, "error": str(exc)})
        return None
    if rows:
        db.add(area)
        await db.commit()
    return rows


async def refresh_boundaries(db: AsyncSession, area: MarketArea) -> Optional[dict]:
    pts = await _home_points(db, area)
    if len(pts) < 20:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    env = bs.envelope(min(xs), min(ys), max(xs), max(ys))
    is_township = (area.municipality_type or "").lower() == "township"
    geo = dict(area.geography or {})
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "OpenHousePal/1.0"}) as client:
            # Real local governments only. Census "county subdivisions" are townships in PA/NJ/NY
            # and the Midwest (FUNCSTAT 'A'), but voting/statistical districts elsewhere (Virginia's
            # "Braddock district", California's "CCDs") - those are dropped. Incorporated cities,
            # boroughs, towns and villages come from the places layer (Narberth borough, Vienna
            # town VA, Scarsdale village NY).
            cousubs = [f for f in await bs.tiger(client, bs.COUSUB, **env) if f["attributes"].get("FUNCSTAT") == "A"]
            names = {(f["attributes"].get("NAME") or "").lower() for f in cousubs}
            places = [f for f in await bs.tiger(client, bs.PLACE, **env)
                      if f["attributes"].get("FUNCSTAT") == "A" and (f["attributes"].get("NAME") or "").lower() not in names]
            unified = [(f, "unified") for f in await bs.tiger(client, bs.UNSD, **env)]
            # States that split elementary and high school districts (much of NJ, IL, CA, AZ): a home
            # sits in one of each, so both kinds are kept and labelled.
            split = [(f, "elementary") for f in await bs.tiger(client, bs.ELSD, **env)] + \
                    [(f, "secondary") for f in await bs.tiger(client, bs.SCSD, **env)]
            # Every county the town's homes are in (facts['counties'], 3%+ each) and the main one
            # (area.county) - see _apply_counties.
            await _refresh_counties_with(client, area, pts, env)
            zips = [str(z) for z in (area.zip_codes or []) if str(z).isdigit()][:12]
            zcta = await bs.tiger(client, bs.ZCTA, where="ZCTA5 IN (" + ",".join(f"'{z}'" for z in zips) + ")") if zips and not is_township else []
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("Census boundary lookup failed", extra={"area": area.slug, "error": str(exc)})
        return None

    # Local government - a home in a village/borough/city counts there (the most local government),
    # otherwise in its township; no home is counted twice.
    munis = places + cousubs
    shares = _shares(pts, [f["geometry"]["rings"] for f in munis])
    muni_rows = sorted(
        [{"name": bs.nice_place(f["attributes"].get("NAME") or ""), "share": sh, "rings": f["geometry"]["rings"],
          "kind": "place" if i < len(places) else "township"}
         for i, (f, sh) in enumerate(zip(munis, shares)) if sh >= _MIN_SHARE],
        key=lambda m: -m["share"])
    # Homes outside any city/town limits are under the county itself (most of Virginia, Maryland).
    covered = sum(m["share"] for m in muni_rows)
    if covered <= 90 and area.county:
        county = str(area.county)
        county = county if county.lower().endswith("county") else f"{county} County"
        muni_rows.append({"name": f"Unincorporated {county}", "share": max(0, 100 - covered), "rings": [], "kind": "county"})
    if len(muni_rows) == 1 and muni_rows[0]["kind"] == "county":
        muni_rows = []  # the whole town is county-run - nothing worth a switch

    district_rows = []
    groups = [unified] if unified else []
    if split:
        groups += [[d for d in split if d[1] == "elementary"], [d for d in split if d[1] == "secondary"]]
    for group in groups:
        if not group:
            continue
        sh = _shares(pts, [f["geometry"]["rings"] for f, _ in group])
        district_rows += [{"name": f["attributes"].get("NAME") or "", "geoid": f["attributes"].get("GEOID"), "kind": kind,
                           "share": s, "rings": f["geometry"]["rings"]} for (f, kind), s in zip(group, sh) if s >= _MIN_SHARE]
    district_rows.sort(key=lambda d: ({"unified": 0, "elementary": 1, "secondary": 2}[d["kind"]], -d["share"]))

    # The town's own outline: the drawn shape for a neighborhood, the township for a township town,
    # otherwise the ZIP area(s) its mailing addresses use.
    outline = None
    if geo.get("type") == "polygon" and (geo.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon"):
        g = geo["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        outline = {"label": area.name, "kind": "neighborhood", "rings": [ring for poly in polys for ring in poly]}
    elif is_township:
        own = next((m for m in muni_rows if (area.name or "").lower() in m["name"].lower()), None)
        if own:
            outline = {"label": own["name"], "kind": "township", "rings": own["rings"]}
    elif zcta:
        outline = {"label": "ZIP " + ", ".join(f["attributes"].get("BASENAME") for f in zcta), "kind": "zip",
                   "rings": [ring for f in zcta for ring in f["geometry"]["rings"]]}
    if not outline and not muni_rows and not district_rows:
        return None

    # Schools: every public school of the town's districts (NCES - an exact district join), and the
    # private schools in and around the town. A big district (a whole county) keeps only the
    # schools near the town.
    schools = {"public": [], "private": []}
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "OpenHousePal/1.0"}) as client:
            pub = await bs.public_schools_for_districts(client, [d["geoid"] for d in district_rows if d.get("geoid")])
            frame = bs.bbox(outline["rings"]) if outline else (min(xs), min(ys), max(xs), max(ys))
            fx, fy = (frame[2] - frame[0]) * 0.12 + 0.005, (frame[3] - frame[1]) * 0.12 + 0.005
            near = (frame[0] - fx, frame[1] - fy, frame[2] + fx, frame[3] + fy)
            by_district: dict[str, list] = {}
            for s in pub:
                by_district.setdefault(s["leaid"], []).append(s)
            share_of = {d["geoid"]: d["share"] for d in district_rows}
            for leaid, lst in by_district.items():
                # A main district (a quarter of the town or more) shows all its schools when it's a
                # town-sized one; a big county/city system or a sliver neighbor shows only the ones nearby.
                whole = share_of.get(leaid, 0) >= 25 and len(lst) <= 16
                schools["public"] += lst if whole else [s for s in lst if near[0] <= s["lng"] <= near[2] and near[1] <= s["lat"] <= near[3]]
            priv = _survey_private_in(frame[0] - 0.004, frame[1] - 0.004, frame[2] + 0.004, frame[3] + 0.004)
            if outline:
                ob = bs.bbox(outline["rings"])
                priv = [s for s in priv if bs.contains(outline["rings"], s["lng"], s["lat"], ob)] or priv
            priv += await _researched_private(client, area, priv)
            schools["private"] = priv[:40]
            for s in schools["public"] + schools["private"]:
                s["in_town"] = bool(outline and bs.contains(outline["rings"], s["lng"], s["lat"]))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("NCES school lookup failed", extra={"area": area.slug, "error": str(exc)})

    boundaries = {
        "outline": outline,
        "municipalities": muni_rows,
        "school_districts": district_rows,
        "schools": schools,
        "homes_checked": len(pts),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "U.S. Census Bureau TIGER boundaries; schools: NCES",
    }
    facts = dict(area.facts or {})
    facts["boundaries"] = boundaries
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return boundaries


# ---------------------------------------------------------------- what's within a walk
_WALK_MILES = 0.6  # straight line - about a 15-minute walk once streets bend
_WALK_GROUPS = {
    "grocery": ('nwr["shop"~"^(supermarket|grocery|greengrocer)$"]', "A grocery store"),
    "food": ('nwr["amenity"~"^(restaurant|cafe)$"]', "Restaurants or cafes"),
    "park": ('nwr["leisure"~"^(park|playground|nature_reserve)$"]', "A park or playground"),
    "transit": ('nwr["railway"~"^(station|halt)$"]', "A train station"),
}


# Not a way to get to work: tourist / heritage railways (10/27: Gettysburg's "train station" was its
# historic depot, used only by excursion trains) and disused stations.
_TOURIST_WORDS = re.compile(r"touris|excursion|heritage|scenic|museum|dinner train|historic|steam train", re.I)


def station_kind(tags: dict) -> Optional[str]:
    """'train' (commuter / intercity rail, subway), 'trolley' (light rail, tram, trolley - Ardmore's
    SEPTA Metro M stops), or None for a station that shouldn't count (tourist / heritage /
    disused)."""
    t = tags or {}
    if t.get("railway") not in ("station", "halt"):
        return None
    if any(k in t for k in ("tourism", "heritage", "railway:historic", "disused", "abandoned")) \
            or t.get("usage") in ("tourism", "heritage") or t.get("railway:preserved") == "yes" \
            or any(_TOURIST_WORDS.search(str(t.get(k) or "")) for k in ("operator", "network")):
        return None
    # Softer signs (a historic-building tag, "tourist rail" in the description) count only against
    # a station no real rail network serves - plenty of working SEPTA/Amtrak stations are historic
    # buildings.
    served = bool(t.get("network") or t.get("railway:ref"))
    if not served and ("historic" in t or any(_TOURIST_WORDS.search(str(t.get(k) or "")) for k in ("description", "name", "note"))):
        return None
    # Freight only - no passengers (Kennett Square's "East Penn Railroad" yard counted as a station).
    if t.get("usage") == "freight" or t.get("passenger") == "no" or "freight" in str(t.get("railway:traffic_mode") or "") \
            or (not served and re.search(r"\b(?:rail\s?road|railway|freight|yard)\b", str(t.get("name") or ""), re.IGNORECASE)
                and not re.search(r"\bstation\b", str(t.get("name") or ""), re.IGNORECASE)):
        return None
    if t.get("station") in ("light_rail", "tram") or t.get("light_rail") == "yes" or t.get("tram") == "yes":
        return "trolley"
    return "train"


def walk_pct_words(pct: Optional[int]) -> Optional[str]:
    """95%+ reads "nearly all" - our 0.6-mile straight line is an estimate, not a promise for every
    home (owner/grader 10/27: "100% of homes near a train station")."""
    if pct is None:
        return None
    return "Nearly all" if pct >= 95 else f"{pct}%"


def _miles(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    dlat = (b_lat - a_lat) * 69.0
    dlng = (b_lng - a_lng) * 69.0 * math.cos(math.radians((a_lat + b_lat) / 2))
    return math.hypot(dlat, dlng)


async def refresh_walkability(db: AsyncSession, area: MarketArea) -> Optional[dict]:
    pts = await _home_points(db, area, limit=6000)
    if len(pts) < 20:
        return None
    step = max(1, len(pts) // 400)
    homes = pts[::step]
    pad = 0.012  # ~0.8 mi, so places just over the town line still count
    xs, ys = [p[0] for p in homes], [p[1] for p in homes]
    bbox = f"{min(ys) - pad},{min(xs) - pad},{max(ys) + pad},{max(xs) + pad}"
    query = "[out:json][timeout:90];(" + "".join(f"{sel}({bbox});" for sel, _ in _WALK_GROUPS.values()) + ");out center tags;"
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "OpenHousePal/1.0 (town guides)"}) as client:
            for attempt in range(4):  # the free server asks busy callers to wait (429/504)
                r = await client.post(_OVERPASS, data={"data": query}, timeout=120)
                if r.status_code not in (429, 502, 503, 504) or attempt == 3:
                    break
                await asyncio.sleep(15 * (attempt + 1))
            r.raise_for_status()
            elements = r.json().get("elements") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("OpenStreetMap walk lookup failed", extra={"area": area.slug, "error": str(exc)})
        return None

    places: dict[str, list[tuple[float, float, str]]] = {k: [] for k in _WALK_GROUPS}
    trolley_names: set[str] = set()
    train_names: set[str] = set()
    for e in elements:
        t = e.get("tags") or {}
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        if t.get("shop") in ("supermarket", "grocery", "greengrocer"):
            k = "grocery"
        elif t.get("amenity") in ("restaurant", "cafe"):
            k = "food"
        elif t.get("leisure") in ("park", "playground", "nature_reserve"):
            k = "park"
        elif t.get("railway") in ("station", "halt"):
            kind = station_kind(t)
            if not kind:
                continue
            k = "transit"
            if t.get("name"):
                (trolley_names if kind == "trolley" else train_names).add(t["name"])
        else:
            continue
        places[k].append((float(lat), float(lng), t.get("name") or ""))

    def near(k: str, need: int = 1) -> int:
        n = 0
        for x, y in homes:
            close = 0
            for lat, lng, _ in places[k]:
                if abs(lat - y) < 0.01 and _miles(y, x, lat, lng) <= _WALK_MILES:
                    close += 1
                    if close >= need:
                        n += 1
                        break
        return round(100 * n / len(homes))

    # Station names inside (or right at the edge of) the town, nearest-to-most-homes first.
    stations: dict[str, int] = {}
    for lat, lng, name in places["transit"]:
        if name:
            served = sum(1 for x, y in homes if _miles(y, x, lat, lng) <= _WALK_MILES)
            if served:
                stations[name] = max(stations.get(name, 0), served)
    # Trolley / light-rail stops count, and then the row says so (Ardmore: three of its five
    # "train stations" were SEPTA Metro M stops).
    has_trolley = any(n in trolley_names for n in stations)
    # "Bryn Mawr" / "Haverford" / "Villanova" are both a Regional Rail station and a Metro M stop -
    # only a name that is a trolley stop and nothing else gets the "(trolley)" tag.
    trolley_only = trolley_names - train_names
    rows = [
        {"key": "grocery", "label": _WALK_GROUPS["grocery"][1], "pct": near("grocery")},
        {"key": "food", "label": "Three or more restaurants or cafes", "pct": near("food", 3)},
        {"key": "park", "label": _WALK_GROUPS["park"][1], "pct": near("park")},
        {"key": "transit", "label": "A train or trolley stop" if has_trolley else _WALK_GROUPS["transit"][1], "pct": near("transit")},
    ]
    for r in rows:
        r["pct_words"] = walk_pct_words(r["pct"])
    # (No bus-stop row - OpenStreetMap's U.S. bus stops are too patchy to state a share honestly.)
    everyday = (rows[0]["pct"] + rows[1]["pct"]) / 2
    level = "Very walkable" if everyday >= 70 else "Walkable in parts" if everyday >= 35 else "Mostly by car"
    walk = {
        "level": level,
        "rows": rows,
        "stations": [f"{n} (trolley)" if n in trolley_only else n for n, _ in sorted(stations.items(), key=lambda s: -s[1])][:5],
        "has_trolley": has_trolley,
        "homes_checked": len(homes),
        "walk_minutes": 15,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "OpenStreetMap",
    }
    facts = dict(area.facts or {})
    facts["walk"] = walk
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return walk
