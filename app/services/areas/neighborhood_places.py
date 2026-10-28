"""City-neighborhood rules for a town page's checked places - no look-ups, safe to run any number of times.

Why (outside grader, 10/27, the 30 Philadelphia neighborhoods): the place check was built for
suburban towns, where "nearby" means 10-30 miles by car. In a city that let Manayunk list a Trader
Joe's in Center City (7.5 mi), Germantown fill its restaurants and shops from Chestnut Hill, Logan
Square list the "Logan" subway station (North Philadelphia), and Somerton list Carpenter's Woods
(13.9 mi). Here, for a neighborhood with a real border (area.geography polygon):
- each place is IN the neighborhood (inside its border) or NEARBY IN another named one - labelled so
  on the card ("Nearby in Fairmount"), never as if it were in this one;
- a place more than a short walk/ride past the border is taken off (to `_removed`, with the reason) -
  a list is never filled with far-away places to reach a count;
- a large park or trail is one map point for miles of land, so its distance is hidden ("access
  varies by entrance") instead of measuring to an arbitrary spot;
- transit: a station is a real station with its real kind (Broad Street Line / Market-Frankford Line
  subway, PATCO rapid transit, Regional Rail, trolley, bus stop); a line, route or trip with no
  station of its own ("PATCO", "Airport-to-Suburban") is not a station and is taken off;
- medical: a student clinic or crisis center isn't a hospital; a specialty hospital says what it is.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from app.services.areas import boundary_sources

# Miles past the neighborhood's border (straight line) a place may sit and still be "nearby".
PAST_BORDER_MILES = {
    "restaurants": 0.75, "downtown_centers": 0.75, "shopping": 1.0, "grocery": 1.0, "parks": 1.0,
    "transportation": 1.0, "private_schools": 2.0, "medical": 2.0,
}
BIG_PARK_PAST_BORDER_MILES = 3.0
# One map point for miles of land: the distance to it says nothing about the nearest entrance.
_BIG_PARK_RE = re.compile(r"\b(trail|towpath|greenway|valley park|creek park|fairmount park|pennypack park|state park|"
                          r"park system|regional park|parkway)\b", re.IGNORECASE)
_BIG_PARK_CATEGORY_RE = re.compile(r"\b(trail|regional|park system|large|state park|natural area)\b", re.IGNORECASE)

# SEPTA's subway stations (fixed infrastructure). A name on both lines is told apart by the street:
# the Broad Street Line runs under Broad Street (west of -75.155), the Market-Frankford Line east of it north of Market.
_BSL = {"fern rock", "olney", "logan", "wyoming", "hunting park", "erie", "allegheny", "north philadelphia",
        "susquehanna dauphin", "cecil b moore", "girard", "fairmount", "spring garden", "race vine", "city hall",
        "walnut locust", "lombard south", "ellsworth federal", "tasker morris", "snyder", "oregon", "nrg",
        "pattison", "chinatown", "8th market"}
_MFL = {"69th street", "millbourne", "63rd street", "60th street", "56th street", "52nd street", "46th street",
        "40th street", "34th street", "30th street", "15th street", "13th street", "11th street", "8th street",
        "5th street", "2nd street", "spring garden", "girard", "berks", "york dauphin", "huntingdon", "somerset",
        "allegheny", "tioga", "erie torresdale", "church", "margaret orthodox", "arrott", "arrott transit center",
        "frankford transit center", "front girard", "5th street independence hall", "2nd street"}
# SEPTA Regional Rail stations inside the city (a "Transit station" label from Google tells nothing).
_REGIONAL_RAIL = {"fox chase", "ryers", "cheltenham", "lawndale", "wayne junction", "germantown", "chelten avenue",
                  "tulpehocken", "upsal", "carpenter", "allen lane", "st martins", "highland", "chestnut hill west",
                  "chestnut hill east", "gravers", "wyndmoor", "mount airy", "sedgwick", "stenton", "washington lane",
                  "east falls", "wissahickon", "manayunk", "ivy ridge", "north broad", "north philadelphia", "bridesburg",
                  "tacony", "holmesburg junction", "torresdale", "somerton", "forest hills", "fern rock transportation center",
                  "olney", "eastwick", "university city", "penn medicine", "temple university", "jefferson", "suburban"}
_PATCO = {"8th market", "9th 10th locust", "12th 13th locust", "15th 16th locust", "franklin square"}
_LINE_ONLY_RE = re.compile(r"\b(line|lines|route|routes|service|metro|to)\b|^patco$|^septa\b", re.IGNORECASE)

# Facilities whose kind isn't what their name or Google's label says (each checked on the provider's own site).
_FACILITY_KIND = {
    "kensington hospital": "Specialty hospital - withdrawal management and substance-use treatment",
    "jefferson hospital for neuroscience": "Specialty hospital (neuroscience) at Jefferson's Center City campus",
    "children's hospital of philadelphia": "Children's hospital",
}
_NOT_A_HOSPITAL_RE = re.compile(r"student health|crisis response|crisis center|\bpenn medicine washington square\b", re.IGNORECASE)


_STREET_CORNER_RE = re.compile(r"&.*\b(st|av|ave|blvd|rd)\b|\b(st|av|ave|blvd|rd)\b.*&|\bstop\b", re.IGNORECASE)


def _station_key(name: str) -> str:
    """"Ellsworth-Federal Station" -> "ellsworth federal"; "2nd Street Station" -> "2nd street"."""
    s = re.sub(r"\(.*?\)", " ", (name or "").lower())
    s = re.sub(r"[&–—\-./]", " ", s)
    s = re.sub(r"\b(station|septa|subway|stn)\b", " ", s)
    s = re.sub(r"\bst\b", "street", s)
    return re.sub(r"\s+", " ", s).strip()


def _on(key: str, names: set) -> bool:
    return key in names or (key.endswith(" street") and key[:-7] in names) or f"{key} street" in names


def transit_kind(e: dict) -> Optional[str]:
    """What a Philadelphia-area station really is, from its name and spot (None = leave the label)."""
    raw = e.get("name") or ""
    key = _station_key(raw)
    if "patco" in key or "patco" in str(e.get("address") or "").lower() or (key in _PATCO and "locust" in key):
        return "PATCO rapid-transit station"
    cat = str(e.get("category") or "").lower()
    if "airport" in cat or "airport" in key:
        return None
    if _STREET_CORNER_RE.search(raw) or cat in ("bus station", "bus stop"):
        return "Bus stop"  # a street corner or a stop is not a station or a transit hub
    in_bsl, in_mfl = _on(key, _BSL), _on(key, _MFL)
    if in_bsl and in_mfl:
        lng = e.get("lng")
        if lng is None:
            return "SEPTA subway station"
        in_bsl, in_mfl = lng < -75.155, lng >= -75.155
    if in_bsl:
        return "SEPTA Broad Street Line subway station"
    if in_mfl:
        return "SEPTA Market-Frankford Line subway station"
    if "30th street" in key:
        return "Amtrak and SEPTA Regional Rail station"
    if "trolley" in cat or "light rail" in cat or "trolley" in key:
        return "SEPTA trolley stop"
    if cat == "train station" or key in _REGIONAL_RAIL or cat == "regional rail station":
        return "SEPTA Regional Rail station"
    return None


def _segment_miles(lat: float, lng: float, a: list, b: list) -> float:
    kx = 69.0 * math.cos(math.radians(lat))
    ax, ay = (a[0] - lng) * kx, (a[1] - lat) * 69.0
    bx, by = (b[0] - lng) * kx, (b[1] - lat) * 69.0
    dx, dy = bx - ax, by - ay
    t = 0.0 if not (dx or dy) else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def miles_past_border(geometry: dict, lat: float, lng: float) -> float:
    """0 inside the border, else the straight-line miles to the nearest point of it."""
    polys = boundary_sources._polygons(geometry)
    if any(boundary_sources._ring_contains(p[0], lng, lat) and not any(boundary_sources._ring_contains(h, lng, lat) for h in p[1:])
           for p in polys if p):
        return 0.0
    return min((_segment_miles(lat, lng, r[i], r[i + 1]) for p in polys for r in p for i in range(len(r) - 1)), default=999.0)


def is_big_park(e: dict) -> bool:
    return bool(_BIG_PARK_RE.search(e.get("name") or "") or _BIG_PARK_CATEGORY_RE.search(str(e.get("category") or "")))


JUST_OUTSIDE_MILES = 0.3  # a few blocks: the city map's border runs down the middle of a shopping street


def _where(area_name: str, e: dict, past: float) -> str:
    if past == 0:
        return f"In {area_name}"
    if past <= JUST_OUTSIDE_MILES:
        return f"Just outside {area_name}"
    other = boundary_sources.philly_neighborhood_at(e["lat"], e["lng"]) if e.get("lat") is not None else None
    if other and other.lower() in str(e.get("name") or "").lower():
        return "Nearby"  # "Pennypack Park" is its own map piece - not "Nearby in Pennypack Park"
    if other and other.lower() != area_name.lower():
        return f"Nearby in {other}"
    city = e.get("city")
    return f"Nearby in {city}" if city and str(city).lower() != "philadelphia" else "Nearby"


_CITY_WIDE_RE = re.compile(r"\b(center city|downtown philadelphia|philadelphia)\b", re.IGNORECASE)


def _names_elsewhere(name: str, area_name: str) -> Optional[str]:
    """The other neighborhood (or the whole city) a district's name points to, else None."""
    if _CITY_WIDE_RE.search(name):
        return _CITY_WIDE_RE.search(name).group(1)
    own = area_name.lower()
    for other in boundary_sources.philly_neighborhood_names():
        o = other.lower()
        # "Germantown Avenue" is a street through many neighborhoods, not Germantown.
        if len(o) >= 5 and o not in own and own not in o and re.search(
                rf"\b{re.escape(o)}\b(?!\s+(?:avenue|ave|street|st|road|rd|pike|boulevard|blvd|lane|drive)\b)", name.lower()):
            return other
    return None


def apply(area_name: str, geometry: Optional[dict], facts: dict, now: str, categories: tuple) -> dict:
    """The rules above over every checked list; returns the new facts (removed places under `_removed`)."""
    if not geometry:
        return facts
    facts = dict(facts)
    for cat in categories:
        block = facts.get(cat)
        if not isinstance(block, dict) or not isinstance(block.get("entities"), list):
            continue
        keep, gone = [], []
        for e in block["entities"]:
            if not isinstance(e, dict) or e.get("admin_edited"):
                keep.append(e)
                continue
            e = dict(e)
            name = e.get("name") or ""
            if cat == "transportation":
                if e.get("lat") is None:
                    # No confirmed spot: a line, a route, a trip - or a station nobody located ("SEPTA
                    # Regional Rail Station (2nd & South Streets)" doesn't exist; "Logan station" is miles off).
                    why = "a line or service, not a station" if _LINE_ONLY_RE.search(name) else "station not located (no confirmed spot)"
                    gone.append({**e, "removed_reason": why, "removed_at": now})
                    continue
                kind = transit_kind(e)
                if kind == "Bus stop":
                    # Bus stops and their routes change every few months - the page lists stations.
                    gone.append({**e, "removed_reason": "a bus stop, not a station", "removed_at": now})
                    continue
                if kind:
                    e["category"] = kind
                e["name"] = re.sub(r"\s*\((?:SEPTA|PATCO)[^)]*\)|\s+-\s+(?:MFL|BSL)\b", "", name).strip() or name
            if cat == "medical":
                if _NOT_A_HOSPITAL_RE.search(name.replace("’", "'")):
                    gone.append({**e, "removed_reason": "a clinic or crisis center, not a hospital", "removed_at": now})
                    continue
                kind = _FACILITY_KIND.get(re.sub(r"\s+", " ", name.lower().replace("’", "'")).strip())
                if kind:
                    e["category"] = kind
            if e.get("lat") is None or e.get("lng") is None:
                # No spot - a district or corridor. One named for somewhere else isn't this neighborhood's
                # ("Center City Philadelphia" for East Kensington, "Chestnut Hill Business District" for Germantown).
                other = _names_elsewhere(name, area_name) if cat in ("downtown_centers", "shopping", "parks") else None
                if other:
                    gone.append({**e, "removed_reason": f"names another area ({other}), not {area_name}", "removed_at": now})
                    continue
                if cat == "downtown_centers" and re.search(r"\b(station|farmers market)\b", name, re.IGNORECASE):
                    gone.append({**e, "removed_reason": "a station or market, not a neighborhood center", "removed_at": now})
                    continue
                city = str(e.get("city") or "")
                if city and city.lower() not in ("philadelphia", area_name.lower()):
                    gone.append({**e, "removed_reason": f"in {city}, outside {area_name} (not located)", "removed_at": now})
                    continue
                if cat in ("restaurants", "grocery", "shopping") and not _DISTRICT_NAME_RE.search(name):
                    # One store nobody could place on the map ("Le Souk", "Willow Grove Park Mall") - unchecked.
                    gone.append({**e, "removed_reason": "not located (no confirmed spot)", "removed_at": now})
                    continue
                keep.append(e)  # a district or corridor: nothing to measure (and no distance is shown for it)
                continue
            past = miles_past_border(geometry, e["lat"], e["lng"])
            big = cat == "parks" and is_big_park(e)
            airport = cat == "transportation" and "airport" in str(e.get("category") or "").lower()
            hospital = cat == "medical" and str(e.get("category") or "").upper() == "HOSPITAL"
            cap = BIG_PARK_PAST_BORDER_MILES if big else PAST_BORDER_MILES.get(cat, 1.0)
            if past > cap and not airport and not hospital:
                gone.append({**e, "removed_reason": f"outside {area_name} ({past:.1f} mi past its border)", "removed_at": now})
                continue
            e["where"] = _where(area_name, e, past) if not airport else "Regional"
            e["miles_past_border"] = round(past, 2)
            if big:
                e.pop("distance_miles", None)  # measured to one arbitrary spot of a big park
                e["access_note"] = "Access varies by entrance"
            keep.append(e)
        if cat == "medical":  # the nearest hospitals only - a moved center can leave a farther one behind
            hospitals = sorted((e for e in keep if str(e.get("category") or "").upper() == "HOSPITAL" and not e.get("admin_edited")),
                               key=lambda e: e.get("distance_miles") if isinstance(e.get("distance_miles"), (int, float)) else 999)
            for h in hospitals[HOSPITALS_SHOWN:]:
                keep.remove(h)
                gone.append({**h, "removed_reason": f"{HOSPITALS_SHOWN} closer hospitals already listed", "removed_at": now})
        block = {**block, "entities": keep}
        if gone:
            block["_removed"] = (list(block.get("_removed") or []) + gone)[-50:]
        facts[cat] = block
    # A store on both lists (Trader Joe's under Shopping and Grocery) is shown once, as grocery.
    grocery = [e for e in (facts.get("grocery") or {}).get("entities") or [] if isinstance(e, dict)]
    for other_cat in ("shopping", "restaurants"):  # Germantown listed its grocery co-op as a restaurant too
        shop = facts.get(other_cat)
        if not (grocery and isinstance(shop, dict) and isinstance(shop.get("entities"), list) and other_cat in categories):
            continue
        dup = [e for e in shop["entities"] if isinstance(e, dict) and not e.get("admin_edited") and any(_same_store(e, g) for g in grocery)]
        if dup:
            facts[other_cat] = {**shop, "entities": [e for e in shop["entities"] if e not in dup],
                                "_removed": (list(shop.get("_removed") or []) + [{**e, "removed_reason": "listed under Grocery", "removed_at": now} for e in dup])[-50:]}
    return facts


def _same_store(a: dict, b: dict) -> bool:
    """The same name, or the same spot (a few yards) with a word in common."""
    return _name_key(a.get("name")) == _name_key(b.get("name")) or (
        a.get("lat") is not None and b.get("lat") is not None and abs(a["lat"] - b["lat"]) < 0.0002 and abs(a["lng"] - b["lng"]) < 0.00025
        and bool(set(_name_key(a.get("name")).split()) & set(_name_key(b.get("name")).split())))


HOSPITALS_SHOWN = 3
_DISTRICT_NAME_RE = re.compile(r"\b(district|corridor|strip|row|avenue|street|cluster|village|area|commercial|mall|main street)\b", re.IGNORECASE)


def _name_key(name) -> str:
    # The whole name: "Weavers Way Co-op - Mercantile" (a gift shop) isn't "Weavers Way Co-op - Mt. Airy".
    return re.sub(r"[^a-z0-9 ]", "", str(name or "").lower()).strip()
