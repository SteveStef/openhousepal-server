"""Real lines and school locations from free federal data, shared by the town page map
(town_map_service) and the Showcase editor's search-area highlight (areas_boundary_routes).

  - U.S. Census TIGERweb (no key): ZIP areas, townships / city & town limits, school districts,
    counties. Updated by the Census every year.
  - NCES EDGE (no key): every public school's location, grades and district id (its LEAID is the
    same id as the Census school-district GEOID, so a district's schools are an exact join), and
    private school locations.

Lines for a named place (a Showcase's "Bryn Mawr, PA" / "LOWER MERION, PA" / "Montgomery, PA")
are cached in system_settings under "geo:..." for 180 days - one Census call per place, ever.

home_areas (user, 10/18): for ONE home's spot on a map - the township / borough / city it sits in, the
ones around it, and its school district - for every property map (Showcase, IDX, Guest Pass, CMA,
Spotlight). Each shape is saved once by its Census id and shared by every home and agent; the spot
-> ids lookup is saved per ~100 m.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import MarketArea, Property, SystemSettings

logger = logging.getLogger(__name__)

TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"
ZCTA, COUSUB, PLACE, UNSD, SCSD, ELSD, COUNTY = 2, 22, 28, 14, 16, 18, 82
_NCES = "https://nces.ed.gov/opengis/rest/services/K12_School_Locations"
_UA = {"User-Agent": "OpenHousePal/1.0 (town guides)"}
CACHE_DAYS = 180

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10", "DC": "11",
    "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30",
    "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
    "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56",
}


# ---------------------------------------------------------------- geometry helpers
def point_in_rings(x: float, y: float, rings: list) -> bool:
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


def bbox(rings: list) -> tuple[float, float, float, float]:
    flat = [p for ring in rings for p in ring]
    return min(p[0] for p in flat), min(p[1] for p in flat), max(p[0] for p in flat), max(p[1] for p in flat)


def contains(rings: list, x: float, y: float, box: Optional[tuple] = None) -> bool:
    x0, y0, x1, y1 = box or bbox(rings)
    return x0 <= x <= x1 and y0 <= y <= y1 and point_in_rings(x, y, rings)


# ---------------------------------------------------------------- Census
async def tiger(client: httpx.AsyncClient, layer: int, offset: float = 0.0001, with_geometry: bool = True, **params) -> list[dict]:
    r = await client.get(f"{TIGER}/{layer}/query", timeout=90, params={
        "outFields": "*", "returnGeometry": "true" if with_geometry else "false",
        "outSR": 4326, "inSR": 4326, "maxAllowableOffset": offset, "geometryPrecision": 5, "f": "json", **params,
    })
    r.raise_for_status()
    feats = r.json().get("features") or []
    return [f for f in feats if not with_geometry or (f.get("geometry") or {}).get("rings")]


def envelope(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"geometry": f"{x0},{y0},{x1},{y1}", "geometryType": "esriGeometryEnvelope", "spatialRel": "esriSpatialRelIntersects"}


def nice_place(name: str) -> str:
    # Census writes "Lower Merion township" / "Media borough" - title-case the kind.
    return " ".join(w if w[:1].isupper() else w.capitalize() for w in (name or "").split())


# ---------------------------------------------------------------- schools (NCES)
_svc_cache: dict[str, str] = {}


async def _latest_service(client: httpx.AsyncClient, prefix: str) -> Optional[str]:
    """NCES publishes one service per school year (EDGE_ADMINDATA_PUBLICSCH_2425 ...) - use the newest."""
    if prefix in _svc_cache:
        return _svc_cache[prefix]
    r = await client.get(_NCES, params={"f": "json"}, timeout=60)
    r.raise_for_status()
    names = sorted(s["name"] for s in r.json().get("services") or [] if s.get("name", "").split("/")[-1].startswith(prefix))
    if not names:
        return None
    _svc_cache[prefix] = names[-1]
    return names[-1]


_ABBR = {
    "el": "Elementary", "elem": "Elementary", "sch": "School", "ms": "Middle School", "hs": "High School",
    "shs": "Senior High School", "jhs": "Junior High School", "jshs": "Junior/Senior High School",
    "int": "Intermediate", "ctr": "Center", "acad": "Academy", "pri": "Primary", "prim": "Primary",
    "mid": "Middle", "twp": "Township", "co": "County", "mem": "Memorial", "jr": "Junior", "sr": "Senior",
    "es": "Elementary School", "ps": "Public School", "cs": "Charter School", "inter": "Intermediate",
}
_SMALL = {"of", "the", "and", "at", "for", "in", "on", "de", "la"}


_KEEP_CAPS = {"KIPP", "STEM", "STEAM", "IB", "YMCA", "YWCA", "AP", "II", "III", "IV", "PS", "IS", "JHS", "MS", "HS", "PK", "USA"}
# Abbreviations that are also real words / names ("El Carmelo", "Int'l"): only expanded right
# before "Sch"/"School" or as the last word.
_ABBR_TAIL_ONLY = {"el", "int", "mid", "pri", "co", "es", "ps", "cs"}


def _cap_word(w: str) -> str:
    if w in _KEEP_CAPS or not any(ch.isalpha() for ch in w):
        return w
    if len(w) == 2 and w.isalpha() and w.lower() not in ("st", "mt", "dr", "jr", "sr", "ft"):
        return w  # DC, NY, PA ...
    return "-".join(part[:1].upper() + part[1:].lower() for part in w.split("-"))


def nice_school(name: str, all_caps: bool = False) -> str:
    """NCES writes "Belmont Hills El Sch", "HEBRON H S", "ARDSLEY HIGH SCHOOL" - make them read like names."""
    raw = re.sub(r"\b([HM]) ?S\b(?=\s*$|\s*\()", lambda m: "High School" if m.group(1) == "H" else "Middle School", (name or "").strip())
    words = raw.split()
    out = []
    for i, w in enumerate(words):
        key = w.lower().strip(".,")
        nxt = words[i + 1].lower().strip(".,") if i + 1 < len(words) else None
        if key in _ABBR and (key not in _ABBR_TAIL_ONLY or nxt in (None, "sch", "school")) and not (key == "el" and i == 0 and nxt not in (None, "sch", "school")):
            out.append(_ABBR[key])
        elif (all_caps or (w.isupper() and len(w) > 3)) and w.isupper():
            out.append(w.lower() if (i and key in _SMALL) else _cap_word(w))
        else:
            out.append(w)
    s = " ".join(out)
    s = re.sub(r"\bSchool School\b", "School", s)
    return s


_NOT_A_SCHOOL = re.compile(r"day ?care|child ?care|childcare|pre-?school|nursery|early (learning|childhood|education)|learning center|kindercare|goddard school|creme de la creme|primrose school|tutor", re.I)


def dedupe_schools(items: list[dict]) -> list[dict]:
    """Same school twice (NCES lists some twice, once with a typo): same name, or within ~60 m. When
    the two names share a word, the fuller one stays - the survey has "Archbishop Carroll High
    School" (Malvern) and "Archbishop John Carroll High School" (Radnor) on one spot (grader, 10/27)."""
    out: list[dict] = []
    words = lambda n: set(re.findall(r"[a-z]{3,}", n.lower())) - {"the", "school", "academy", "and", "high", "elementary", "middle"}
    for s in items:
        key = re.sub(r"[^a-z]", "", s["name"].lower())
        dup = next((i for i, o in enumerate(out) if re.sub(r"[^a-z]", "", o["name"].lower()) == key
                    or (abs(o["lat"] - s["lat"]) < 0.0006 and abs(o["lng"] - s["lng"]) < 0.0006)), None)
        if dup is None:
            out.append(s)
        elif words(out[dup]["name"]) & words(s["name"]) and len(s["name"]) > len(out[dup]["name"]):
            out[dup] = s
    return out


def _grade(g: Optional[str]) -> Optional[str]:
    g = (g or "").strip().upper()
    if not g or g in ("N", "M", "-1", "-2"):
        return None
    return {"PK": "Pre-K", "KG": "K", "UG": None, "AE": None}.get(g, g.lstrip("0") or "0")


async def public_schools_for_districts(client: httpx.AsyncClient, leaids: list[str]) -> list[dict]:
    """Every open, regular public school of these districts, with its grades."""
    if not leaids:
        return []
    svc = await _latest_service(client, "EDGE_ADMINDATA_PUBLICSCH_")
    if not svc:
        return []
    # The admin-data service keeps its one layer at id 1 (id 0 on some years) - try both.
    feats = []
    for layer in (1, 0):
        r = await client.get(f"{_NCES.rsplit('/', 1)[0]}/{svc}/MapServer/{layer}/query", timeout=90, params={
            "where": "LEAID IN (" + ",".join(f"'{x}'" for x in leaids[:20]) + ")",
            "outFields": "NCESSCH,SCH_NAME,LEAID,LEA_NAME,GSLO,GSHI,SCHOOL_LEVEL,SY_STATUS_TEXT,VIRTUAL,SCHOOL_TYPE_TEXT,CHARTER_TEXT,LATCOD,LONCOD",
            "returnGeometry": "false", "resultRecordCount": 2000, "f": "json",
        })
        if r.status_code == 200 and "features" in r.json():
            feats = r.json()["features"]
            break
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        if a.get("SY_STATUS_TEXT") not in (None, "Open", "New", "Reopened", "Added"):
            continue
        if (a.get("SCHOOL_TYPE_TEXT") or "Regular School") != "Regular School" or str(a.get("VIRTUAL") or "").startswith("Exclusively"):
            continue
        if a.get("LATCOD") is None or a.get("LONCOD") is None:
            continue
        lo, hi = _grade(a.get("GSLO")), _grade(a.get("GSHI"))
        out.append({
            "id": a.get("NCESSCH"), "name": nice_school(a.get("SCH_NAME") or ""), "leaid": a.get("LEAID"),
            "level": (a.get("SCHOOL_LEVEL") or "").replace("Not reported", "").replace("Not applicable", "") or None,
            "grades": f"{lo}-{hi}" if lo and hi and lo != hi else (lo or hi),
            "charter": a.get("CHARTER_TEXT") == "Yes",
            "lat": round(float(a["LATCOD"]), 6), "lng": round(float(a["LONCOD"]), 6),
        })
    return dedupe_schools(out)


async def private_schools_in(client: httpx.AsyncClient, x0: float, y0: float, x1: float, y1: float) -> list[dict]:
    svc = await _latest_service(client, "EDGE_GEOCODE_PRIVATESCH_")
    if not svc:
        return []
    r = await client.get(f"{_NCES.rsplit('/', 1)[0]}/{svc}/MapServer/0/query", timeout=90, params={
        **envelope(x0, y0, x1, y1), "inSR": 4326, "outFields": "PPIN,NAME,LAT,LON",
        "returnGeometry": "false", "resultRecordCount": 1000, "f": "json",
    })
    r.raise_for_status()
    out = []
    for f in r.json().get("features") or []:
        a = f.get("attributes") or {}
        if a.get("LAT") is None or a.get("LON") is None or not a.get("NAME"):
            continue
        if _NOT_A_SCHOOL.search(a["NAME"]):
            continue  # daycares and preschools are in the federal private-school file too
        out.append({"id": a.get("PPIN"), "name": nice_school(a["NAME"], all_caps=a["NAME"].isupper()),
                    "lat": round(float(a["LAT"]), 6), "lng": round(float(a["LON"]), 6)})
    return dedupe_schools(out)


# ---------------------------------------------------------------- named-place lines (Showcase editor)
# Words that don't tell two districts apart. "Township" / "Borough" / "City" / "County" DO
# (Berlin Borough vs Berlin Township, NJ), so they stay in the key.
_DISTRICT_WORDS = {"school", "district", "schools", "public", "area", "the", "of", "sd", "unified", "consolidated",
                   "regional", "community", "independent", "isd", "usd", "cusd", "csd", "board", "education",
                   "joint", "union", "free"}
_PLACE_KINDS = {"township", "borough", "city", "town", "village", "county", "parish"}


def district_key(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("-", " ").replace("'", "")).split()
    words = ["township" if w == "twp" else w for w in words]
    if len(words) > 2 and words[0] in _PLACE_KINDS and words[1] == "of":  # "City of Vineland" = "Vineland City"
        words = words[2:] + [words[0]]
    core = [w for w in words if w not in _DISTRICT_WORDS]
    return " ".join(core) or " ".join(words)


def _split_label(value: str) -> tuple[str, Optional[str]]:
    # Showcase values are stored as "NAME, ST".
    parts = [p.strip() for p in (value or "").rsplit(",", 1)]
    if len(parts) == 2 and len(parts[1]) == 2:
        return parts[0], parts[1].upper()
    return (value or "").strip(), None


async def _cache_get(db: AsyncSession, key: str) -> Optional[dict]:
    row = await db.get(SystemSettings, key)
    if not row or not isinstance(row.value, dict):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row.value.get("at"))).days
    except (TypeError, ValueError):
        return None
    return row.value if age <= CACHE_DAYS else None


async def _cache_put(db: AsyncSession, key: str, value: dict) -> None:
    value = {**value, "at": datetime.now(timezone.utc).isoformat()}
    row = await db.get(SystemSettings, key)
    if row:
        row.value = value
    else:
        db.add(SystemSettings(key=key, value=value))
    await db.commit()


async def district_shape(db: AsyncSession, client: httpx.AsyncClient, value: str) -> Optional[dict]:
    name, state = _split_label(value)
    fips = STATE_FIPS.get(state or "")
    key = district_key(name)
    if not fips or not key:
        return None
    ck = f"geo:sd:{state}:{key}"
    hit = await _cache_get(db, ck)
    if hit:
        return hit.get("shape")
    longest = max(key.split(), key=len).replace("'", "''")
    best = None
    for layer in (UNSD, ELSD, SCSD):
        feats = await tiger(client, layer, offset=0.0002, where=f"STATE='{fips}' AND UPPER(NAME) LIKE '%{longest.upper()}%'")
        for f in feats:
            fk = district_key(f["attributes"].get("NAME") or "")
            # exact, or one is the other plus only a place-kind word ("paulsboro" / "paulsboro
            # borough") - looser matches drew the wrong district ("cape may" -> Cape May Point).
            longer, shorter = (fk, key) if len(fk) > len(key) else (key, fk)
            extra = longer[len(shorter):].split() if longer.startswith(shorter + " ") else None
            score = 3 if fk == key else 2 if extra and all(w in _PLACE_KINDS for w in extra) else 0
            if score and (not best or score > best[0]):
                best = (score, f, layer)
        if best and best[0] == 3:
            break
    shape = None
    if best:
        f = best[1]
        shape = {"kind": "school_district", "label": f["attributes"]["NAME"], "geoid": f["attributes"].get("GEOID"),
                 "query": value, "rings": f["geometry"]["rings"]}
    await _cache_put(db, ck, {"shape": shape})
    return shape


async def county_shape(db: AsyncSession, client: httpx.AsyncClient, value: str) -> Optional[dict]:
    name, state = _split_label(value)
    # Bright stores counties as "DELAWARE-PA" / "MONTGOMERY-MD" - the suffix is the state.
    m = re.match(r"^(.*?)[-\s]+([A-Za-z]{2})$", name.strip())
    if m and m.group(2).upper() in STATE_FIPS:
        name, state = m.group(1), m.group(2).upper()
    fips = STATE_FIPS.get(state or "")
    if state == "DC":
        name = "District of Columbia"
    wants_city = bool(re.search(r"\bcity$", name.strip(), re.I))  # "Baltimore City" is not Baltimore County
    base = re.sub(r"\s+(county|parish|city)$", "", name.strip(), flags=re.I)
    if not fips or not base:
        return None
    ck = f"geo:county:{state}:{base.lower()}{':city' if wants_city else ''}"
    hit = await _cache_get(db, ck)
    if hit:
        return hit.get("shape")
    squash = lambda v: re.sub(r"[^a-z]", "", (v or "").lower())
    first = base.split()[0].replace("'", "''").upper()
    feats = await tiger(client, COUNTY, offset=0.0006, where=f"STATE='{fips}' AND UPPER(BASENAME) LIKE '{first}%'")
    # "Prince Georges" = "Prince George's": compare letters only.
    feats = [f for f in feats if squash(f["attributes"].get("BASENAME")) == squash(base)]
    # "Baltimore" is both a county and an independent city - the county unless the city was asked for.
    feats.sort(key=lambda f: ("city" in (f["attributes"].get("NAME") or "").lower()) != wants_city)
    shape = None
    if feats:
        f = feats[0]
        shape = {"kind": "county", "label": f["attributes"]["NAME"], "geoid": f["attributes"].get("GEOID"),
                 "query": value, "rings": f["geometry"]["rings"]}
    await _cache_put(db, ck, {"shape": shape})
    return shape


async def town_shape(db: AsyncSession, client: httpx.AsyncClient, value: str) -> Optional[dict]:
    """A Showcase town is the MLS's postal city - its homes' ZIP areas are the honest outline. A
    town we've built a guide for reuses that outline (township towns use the township itself)."""
    name, state = _split_label(value)
    if not name:
        return None
    area = (await db.execute(
        select(MarketArea).where(func.lower(MarketArea.name) == name.lower(), *( [func.upper(MarketArea.state) == state] if state else []))
        .order_by(MarketArea.area_type != "city").limit(1)
    )).scalars().first()
    outline = ((area.facts or {}).get("boundaries") or {}).get("outline") if area else None
    if outline and outline.get("rings"):
        return {"kind": "town", "label": f"{name.title()}", "query": value, "rings": outline["rings"], "zips": None}

    ck = f"geo:town:{state}:{name.lower()}"
    hit = await _cache_get(db, ck)
    if hit:
        return hit.get("shape")
    q = select(Property.zipcode, func.count()).where(func.lower(Property.city) == name.lower())
    if state:
        q = q.where(func.upper(Property.state) == state)
    rows = (await db.execute(q.group_by(Property.zipcode))).all()
    counts: dict[str, int] = {}
    for z, c in rows:
        z5 = str(z or "")[:5]
        if z5.isdigit() and len(z5) == 5:
            counts[z5] = counts.get(z5, 0) + c
    total = sum(counts.values())
    zips = [z for z, c in sorted(counts.items(), key=lambda kv: -kv[1]) if total and c / total >= 0.04][:10]
    shape = None
    if zips:
        feats = await tiger(client, ZCTA, offset=0.0002, where="ZCTA5 IN (" + ",".join(f"'{z}'" for z in zips) + ")")
        if feats:
            shape = {"kind": "town", "label": name.title(), "query": value, "zips": zips,
                     "rings": [ring for f in feats for ring in f["geometry"]["rings"]]}
    await _cache_put(db, ck, {"shape": shape})
    return shape



# ---------------------------------------------------------------- one home's surroundings
# States whose townships are real local governments (taxes, zoning, trash) - elsewhere the Census
# "county subdivisions" are just statistical, and an incorporated city / town is the government.
_MCD_STATES = {"09", "23", "25", "26", "27", "33", "34", "36", "42", "44", "50", "55"}  # CT ME MA MI MN NH NJ NY PA RI VT WI


def _point(lat: float, lng: float) -> dict:
    return {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects"}


async def _shape_by_geoid(db: AsyncSession, client: httpx.AsyncClient, layer: int, geoid: str, offset: float) -> Optional[dict]:
    ck = f"geo:shape:{layer}:{geoid}"
    hit = await _cache_get(db, ck)
    if hit:
        return hit.get("shape")
    safe = re.sub(r"[^0-9A-Za-z]", "", geoid)
    feats = await tiger(client, layer, offset=offset, where=f"GEOID='{safe}'")
    shape = None
    if feats:
        a = feats[0]["attributes"]
        shape = {"name": nice_place(a.get("NAME") or ""), "geoid": safe, "rings": feats[0]["geometry"]["rings"]}
    await _cache_put(db, ck, {"shape": shape})
    return shape


async def home_areas(db: AsyncSession, client: httpx.AsyncClient, lat: float, lng: float) -> dict:
    """{'municipality': shape|None, 'neighbors': [shape], 'school_districts': [shape]} for a home's
    spot. A shape is {name, geoid, rings ([lng, lat] rings)}."""
    ck = f"geo:pt2:{lat:.3f},{lng:.3f}"
    ids = (await _cache_get(db, ck) or {}).get("ids")
    if ids is None:
        pt = _point(lat, lng)
        muni = None
        cs = await tiger(client, COUSUB, with_geometry=False, **pt)
        state = cs[0]["attributes"].get("STATE") if cs else None
        if cs and state in _MCD_STATES and cs[0]["attributes"].get("FUNCSTAT") == "A":
            muni = [COUSUB, cs[0]["attributes"]["GEOID"]]
        else:
            places = [f for f in await tiger(client, PLACE, with_geometry=False, **pt) if f["attributes"].get("LSADC") != "57"]  # 57 = CDP
            if places:
                muni = [PLACE, places[0]["attributes"]["GEOID"]]
        if not muni:
            # Unincorporated (Bethesda MD, Arlington VA, Hockessin DE): the county is the local government.
            county = await tiger(client, COUNTY, with_geometry=False, **pt)
            if county:
                muni = [COUNTY, county[0]["attributes"]["GEOID"]]
        # (Neighbouring towns are no longer drawn - user 10/18: the grey lines made maps messy.)
        neighbors: list = []
        districts: list = []
        unified = await tiger(client, UNSD, with_geometry=False, **pt)
        if unified:
            districts = [[UNSD, unified[0]["attributes"]["GEOID"]]]
        else:
            for layer in (ELSD, SCSD):
                hit = await tiger(client, layer, with_geometry=False, **pt)
                if hit:
                    districts.append([layer, hit[0]["attributes"]["GEOID"]])
        ids = {"muni": muni, "neighbors": neighbors, "districts": districts}
        await _cache_put(db, ck, {"ids": ids})

    m = ids.get("muni")
    municipality = await _shape_by_geoid(db, client, m[0], m[1], 0.0006 if m[0] == COUNTY else 0.0002) if m else None
    near = [await _shape_by_geoid(db, client, layer, g, 0.0004) for layer, g in ids.get("neighbors") or []]
    sds = [await _shape_by_geoid(db, client, layer, g, 0.0003) for layer, g in ids.get("districts") or []]
    return {"municipality": municipality, "neighbors": [n for n in near if n and n.get("rings")],
            "school_districts": [d for d in sds if d and d.get("rings")]}
