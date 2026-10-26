"""Holds every place on an IDX town page to the Cheat Sheet's rules (user, 10/26: "make sure the
neighborhood section ... is perfectly accurate" - the same problems the Cheat Sheet had).

The town research (AI + web search, area_research_service) names the places. Before anything is
shown, each one is looked up on Google and must pass the same checks a Cheat Sheet pick does:
- still open; a business that isn't on Google at all (restaurant, grocery, care) is dropped;
- grocery = a real grocery store (no gas-station or convenience store), parks = a real park (no
  house on a court, subdivision name or hand-typed listing), hospitals = general hospitals with an
  emergency room (no VA, rehab, long-term care, children's/psych, doctors' offices);
- private schools are matched to the federal private school survey for their real grades -
  preschools and early-childhood centers drop out;
- every distance is the real driving distance from the center of town (the research guessed them -
  "0 mi" for most places), and a place too far to call nearby is dropped;
- the nearest hospitals always appear (the research left Paoli Hospital off Wayne's list).
Removed places are kept under the category's `_removed` list with the reason, for admin review.

tidy_research_text() is the render-time cleanup of the research's own words: citation tags, notes
about how it searched, and distance claims the checked list now states correctly."""
from __future__ import annotations

import logging
import math
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.database import MarketArea
from app.services import nearby_amenities_service as nas
from app.services.google_free_tier import GoogleFreeTierPaused

# Look-ups skipped this run because Google's free calls for the month were used up (see check_town_places).
_paused: ContextVar[Optional[list]] = ContextVar("town_places_paused", default=None)


def _note_paused(exc: Exception) -> None:
    hits = _paused.get()
    if hits is not None and isinstance(exc, GoogleFreeTierPaused):
        hits.append(1)

logger = logging.getLogger(__name__)

CHECK_CATEGORIES = ("downtown_centers", "restaurants", "shopping", "grocery", "parks", "medical", "transportation")
# Businesses: if Google has never heard of it, the research named something that isn't there.
MUST_BE_ON_GOOGLE = {"restaurants", "grocery", "medical", "private_schools"}
# Farther than this by car isn't "in or near" the town.
MAX_DRIVE_MILES = {
    "downtown_centers": 20, "restaurants": 10, "shopping": 25, "grocery": 10, "parks": 25,
    "medical": 30, "transportation": 30, "private_schools": 15,
}
TOWN_HOSPITALS = 3            # the nearest general hospitals by drive, always on the list
PRIVATE_SCHOOLS_SHOWN = 8
PRIVATE_SCHOOL_FILL_MILES = 4  # survey schools this close to the center fill a short list
SURVEY_MATCH_MILES = 8         # a researched school is matched to a survey school only this close
_SCHOOL_LEVEL_WORDS = {"high", "elementary", "middle", "primary", "upper", "lower", "junior", "senior", "intermediate", "secondary"}
RURAL_FILL_MILES = 10         # how far the fill reaches when fewer than 3 schools are left
LOOKUP_RADIUS_MILES = 20

_MEDICAL_LABELS = {"HOSPITAL": "Hospital", "URGENT_CARE": "Urgent care", "OTHER_MAJOR_MEDICAL": "Medical center"}
_FOOD_TYPE_WORDS = ("restaurant", "food", "cafe", "bar", "pub", "bakery", "brewery", "winery", "diner", "bistro", "deli", "pizza", "steak", "grill", "meal_")
_NAME_STOPWORDS = {"the", "and", "of", "at", "in", "a", "&", "restaurant", "bar", "grill", "cafe", "market", "store", "inc", "llc"}
_SCHOOL_PRIMARY = nas.SCHOOL_PRIMARY_TYPES | {"preschool"}


def _tokens(name: str, town: str = "") -> set[str]:
    drop = _NAME_STOPWORDS | set(re.findall(r"[a-z0-9]+", town.lower()))
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "")) if t not in drop}


def _research_name(name: str) -> str:
    """"The Buttery | Malvern" / "Brick & Brew Gastropub - Malvern" - the research's town suffix off."""
    return re.split(r"\s+[|–—-]\s+", (name or "").strip())[0].strip() or (name or "").strip()


def _same_place(entity_name: str, place: dict, town: str) -> bool:
    """Most of both names' distinctive words in common ("Brick & Brew Gastropub" = "Brick & Brew
    Malvern"; "Downtown Media (State Street corridor)" is not "State Street Pub")."""
    ours, theirs = _name_tokens(_research_name(entity_name), town), _name_tokens(nas._place_name(place), town)
    if not ours or not theirs:
        return False
    # Compared on the telling words when both names have some ("Lankenau Hospital" = "Lankenau
    # Medical Center", "Tired Hands Brewing Company" = "Tired Hands Fermentaria Tap Room").
    if ours - _GENERIC_WORDS and theirs - _GENERIC_WORDS:
        ours, theirs = ours - _GENERIC_WORDS, theirs - _GENERIC_WORDS

    def same(a: str, b: str) -> bool:  # "pescatore" = Google's "pescatorebyob"
        return a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)))
    ours_hit = {t for t in ours if any(same(t, u) for u in theirs)}
    theirs_hit = {u for u in theirs if any(same(t, u) for t in ours)}
    # One name inside the other ("Taquero" / "Taquero MX BYOB", "Dua Mediterranean Kitchen" / "Dua
    # Restaurant") - only when they share the longer name's first telling word: "State Street Pub"
    # is inside "Downtown Media (State Street corridor)" word for word, but it isn't downtown Media.
    first_ours = _first_word(entity_name, ours)
    first_theirs = _first_word(nas._place_name(place), theirs)
    if theirs_hit == theirs and first_ours in ours_hit and len(first_ours) >= 3:
        return True
    if ours_hit == ours and first_theirs in theirs_hit and len(first_theirs) >= 3:
        return True
    return len(ours_hit) / len(ours) >= 0.6 and len(theirs_hit) / len(theirs) >= 0.5


def _first_word(name: str, words: set[str]) -> str:
    for t in re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "")):
        if t in words:
            return t
    return ""


_GENERIC_WORDS = {"park", "parks", "station", "center", "centre", "shopping", "shop", "shops", "school", "trail", "garden",
                  "gardens", "hospital", "medical", "health", "care", "urgent", "plaza", "square", "street", "avenue", "road",
                  "kitchen", "tavern", "house", "farm", "farms", "village", "town", "city", "county", "regional", "memorial",
                  "community", "public", "north", "south", "east", "west", "main", "line",
                  "company", "brewing", "brewery", "brew", "cafe", "caf", "bistro", "grill", "bar", "pub", "eatery", "pizzeria",
                  "pizza", "diner", "bakery", "byob", "lounge", "steakhouse", "tap", "room", "taproom", "restaurant", "kitchen",
                  "market", "markets", "supermarket", "grocery", "foods", "food", "store", "academy", "preparatory", "prep",
                  "saint", "st", "sts", "day", "upper", "lower", "campus", "pa", "nj", "de", "md", "va"}


def _name_tokens(name: str, town: str) -> set[str]:
    """A name's distinctive words, the town's own name left out ("Brick & Brew Malvern") - unless
    that leaves only generic words ("Wayne Station" -> {"wayne", "station"})."""
    without = _tokens(name, town)
    if without and any(t not in _GENERIC_WORDS for t in without):
        return without
    return _tokens(name) or without


# The kind of Google listing each list can be matched to at all - a research name that only matches a
# different kind of place ("Wayne Station" -> "Apartments at Wayne Station") is treated as not found.
_TRANSIT_TYPES = {"train_station", "transit_station", "subway_station", "light_rail_station", "bus_station", "bus_stop", "ferry_terminal", "park_and_ride", "airport"}
_PARK_TYPES = {"park", "national_park", "state_park", "hiking_area", "garden", "botanical_garden", "nature_preserve", "wildlife_refuge",
               "playground", "dog_park", "tourist_attraction", "historical_landmark", "campground", "beach", "marina", "natural_feature",
               "sports_complex", "athletic_field", "golf_course", "zoo", "lake", "historical_place", "monument", "plaza", "museum",
               "route"}  # a rail-trail is mapped as a route ("Radnor Trail")
# A "downtown" the research named that Google knows as one store or center ("The Outlet Shoppes at
# Gettysburg" is typed store, "Gettysburg Marketplace" market) is shopping, not a downtown.
_CENTER_TYPES = {"shopping_mall", "store", "market", "department_store", "outlet_store"}
_TRANSIT_LABELS = (("airport", "Airport"), ("subway_station", "Subway station"), ("light_rail_station", "Light rail / trolley stop"),
                   ("train_station", "Train station"), ("bus_station", "Bus station"), ("ferry_terminal", "Ferry terminal"),
                   ("park_and_ride", "Park and ride"), ("transit_station", "Transit station"), ("bus_stop", "Bus stop"))
_ER_LISTING_RE =re.compile(r"[:\-–]\s*emergency (?:department|room)\s*$", re.IGNORECASE)
_STORE_SIDE_TYPES = {"pharmacy", "drugstore", "store", "medical_clinic", "doctor", "medical_lab"}
_FOOD_TYPES_EXACT = {"bar", "pub", "night_club", "wine_bar", "cafe", "coffee_shop", "bakery"}
# A district, corridor or main street is an area, not a listing - looked up, it only ever matches a business on it.
_AREA_NAME_RE = re.compile(r"\b(district|downtown|corridor|main street|town center|town centre|village center|business area|historic)\b", re.IGNORECASE)


def _kind_fits(category: str, place: dict) -> bool:
    types = set(place.get("types") or []) | {nas._place_primary_type(place)}
    if category == "transportation":
        return bool(types & _TRANSIT_TYPES)
    if category == "parks":
        return bool(types & _PARK_TYPES)
    if category == "downtown_centers":
        return nas._place_primary_type(place) in _CENTER_TYPES  # only to move a shopping center into Shopping
    if category == "shopping":
        primary = nas._place_primary_type(place)  # a shopping center lists its restaurants among its types
        return not (primary in _FOOD_TYPES_EXACT or "restaurant" in primary)
    return True


def _city_of(address: str) -> Optional[str]:
    parts = [p.strip() for p in (address or "").split(",")]
    # "123 King St, Malvern, PA 19355, USA" -> Malvern
    if len(parts) >= 3 and re.match(r"^[A-Z]{2}\b", parts[-2] if parts[-1] == "USA" else parts[-1]):
        return parts[-3] if parts[-1] == "USA" else parts[-2]
    return None


def _maps_url(place: dict) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{nas._place_name(place)} {place.get('formattedAddress') or ''}".strip())


def _rule_failure(category: str, entity: dict, place: dict) -> Optional[str]:
    """Why this Google listing can't be shown in this category (None = it can)."""
    if not nas._is_open(place):
        return "closed (Google)"
    primary = nas._place_primary_type(place)
    types = set(place.get("types") or [])
    name = nas._place_name(place)
    if category == "grocery":
        return None if nas._is_full_service_grocery(place) else f"not a grocery store (Google: {nas._place_type_label(place) or primary})"
    if category == "restaurants":
        if types & nas.EXPLORE_NEARBY_HYBRID_EXCLUDE_TYPES or any(p in name.lower() for p in nas.CONVENIENCE_GAS_HYBRID_NAME_PATTERNS):
            return "a gas station or convenience store"
        if not any(w in t for t in [primary, *types] for w in _FOOD_TYPE_WORDS):
            return f"not a place to eat (Google: {nas._place_type_label(place) or primary})"
        return None
    if category == "parks":
        # A lower-case listing with real visitors is the place's own ("Stoneleigh: a natural garden").
        hand = nas._looks_hand_entered(name) and nas._park_reviews(place) < nas.PARK_NO_ADDRESS_MIN_REVIEWS
        if types & {"route", "hiking_area"}:
            return None if not hand else "not a real park (a hand-typed listing)"  # a mapped trail has no street address or reviews (Great Allegheny Passage)
        if hand or nas._looks_like_a_house_or_subdivision(place) or nas._is_likely_subdivision_named_park(place):
            return "not a real park (a house, a subdivision name or a hand-typed listing)"
        return None
    if category == "medical":
        said_hospital = str(entity.get("category") or "").upper() == "HOSPITAL"
        if nas._is_legitimate_full_service_hospital(place):
            entity["category"] = "HOSPITAL"
            return None
        address = place.get("formattedAddress") or ""
        real_street = nas._HAS_STREET_NUMBER_RE.match(address) and not nas._SUITE_ADDRESS_RE.search(address)  # no office suite, no unbuilt site
        if said_hospital and primary in nas.HOSPITAL_PRIMARY_TYPES and real_street:
            # Two sources agree it's a hospital - the research and Google's own main label - so a new
            # one not on Medicare's list yet passes, whatever side labels Google adds (Penn Highlands
            # State College, opened 2024: also typed pharmacy/store). Every name and VA / children's /
            # rehab / long-term-care exclusion still applies.
            plain = {**place, "types": [t for t in types if t not in _STORE_SIDE_TYPES]}
            if nas._is_legitimate_full_service_hospital(plain, medicare_confirmed=True):
                return None
        if said_hospital and real_street and _ER_LISTING_RE.search(name) and not any(k in name.lower() for k in nas.HOSPITAL_NOT_GENERAL_NAME_KEYWORDS):
            # The hospital's own emergency-room listing ("Penn Highlands State College: Emergency Department").
            return None
        if said_hospital:
            return "not a general hospital with an emergency room"
        if nas._is_legitimate_urgent_care(place):
            entity["category"] = "URGENT_CARE"
            return None
        label = nas._non_hospital_medical_label(place)
        if not label:
            return "not an urgent care or doctor's office"
        # What it really is ("UVA Health Primary Care Riverside" was labeled urgent care by the research).
        entity["category"] = "URGENT_CARE" if label == "URGENT CARE" else label
        return None
    return None


async def _lookup(client: httpx.AsyncClient, key: str, area: MarketArea, category: str, entity: dict) -> Optional[dict]:
    """The Google listing for this researched place: among the top results with the same name and
    the right kind of place, the one closest to town (a chain's other branch 15 miles off is not it)."""
    name = _research_name(entity.get("name") or "")
    if category in ("shopping", "downtown_centers") and _AREA_NAME_RE.search(name) and "mall" not in name.lower():
        return None
    mask = nas.PARK_FIELD_MASK if category == "parks" else nas.CHEAT_FIELD_MASK
    try:
        raw = await nas._search_text(client, key, area.centroid_lat, area.centroid_lng,
                                     f"{name}, {entity.get('city') or area.name}, {area.state}", LOOKUP_RADIUS_MILES,
                                     field_mask=mask, section="town_places_check")
    except httpx.HTTPError as exc:
        logger.info("Town place look-up failed", extra={"area": area.slug, "place": name, "error": str(exc)})
        _note_paused(exc)
        raise
    fits = [p for p in raw[:5] if _same_place(name, p, area.name) and (category == "private_schools" or _kind_fits(category, p))]

    def rank(p: dict) -> tuple:
        loc = p.get("location") or {}
        far = 9999.0 if loc.get("latitude") is None else nas._haversine_miles(area.centroid_lat, area.centroid_lng, loc["latitude"], loc["longitude"])
        # The hospital itself before a same-named doctors' building; then the nearest.
        passes = category == "private_schools" or _rule_failure(category, dict(entity), p) is None
        return (not passes, far)
    return min(fits, key=rank) if fits else None


def _placed(entity: dict, place: dict, category: str) -> dict:
    loc = place.get("location") or {}
    google = nas._place_to_nearby(place, 0, 0)  # Google's own name, SEO suffix and stray "?" off
    name = google.name if google else entity.get("name")
    name = re.sub(r"\s*[,:]\s+(?:a|an|the)\s+[a-z].*$", "", name or "") or name  # "Chanticleer, a pleasure garden"
    out = {**entity, "name": name,
           "lat": loc.get("latitude"), "lng": loc.get("longitude"), "address": place.get("formattedAddress"),
           "google_checked": True}
    city = _city_of(place.get("formattedAddress") or "")
    if city:
        out["city"] = city
    if not out.get("source_url") or "/search" in str(out.get("source_url")) or "find_loc" in str(out.get("source_url")):
        out["source_url"] = _maps_url(place)  # a Yelp search page is not this place's source
    out.pop("distance_miles", None)
    if category == "transportation":
        # What Google knows it is - the research called Doylestown's train station a "Bus route".
        types = set(place.get("types") or []) | {nas._place_primary_type(place)}
        for t, label in _TRANSIT_LABELS:
            if t in types:
                out["category"] = label
                break
    if category == "medical":
        out["category"] = out.get("category") or ("HOSPITAL" if nas._place_primary_type(place) in nas.HOSPITAL_PRIMARY_TYPES else "URGENT_CARE")
        if str(out["category"]).upper() == "HOSPITAL":
            out["name"] = _ER_LISTING_RE.sub("", out["name"]).strip()  # the hospital's name, not its ER listing's
    return out


def _near(a: dict, b_lat: Optional[float], b_lng: Optional[float], miles: float = 0.35) -> bool:
    return a.get("lat") is not None and b_lat is not None and nas._haversine_miles(a["lat"], a["lng"], b_lat, b_lng) <= miles


async def _hospitals(client: httpx.AsyncClient, key: str, area: MarketArea) -> list[dict]:
    """The Cheat Sheet's hospital search from the center of town: real general hospitals, nearest first."""
    try:
        found = await nas._fetch_hospital_from_google(client, key, area.centroid_lat, area.centroid_lng)
    except httpx.HTTPError as exc:
        logger.info("Town hospital search failed", extra={"area": area.slug, "error": str(exc)})
        _note_paused(exc)
        return []
    out = []
    for p in found[: TOWN_HOSPITALS + 2]:
        out.append({
            "name": p.name, "category": "HOSPITAL", "city": _city_of(p.address or ""), "lat": p.latitude, "lng": p.longitude,
            "address": p.address, "relationship": "NEARBY", "source_type": "google_places", "google_checked": True,
            "source_url": "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{p.name} {p.address or ''}"),
        })
    return out


def _survey_schools_near(lat: float, lng: float, miles: float) -> list[dict]:
    pad_lat, pad_lng = miles / 69.0, miles / max(1.0, 69.0 * math.cos(math.radians(lat)))
    out = []
    for name, city, state, s_lat, s_lng, lowest, highest, typology, students in nas._load_private_schools():
        if abs(s_lat - lat) > pad_lat or abs(s_lng - lng) > pad_lng:
            continue
        d = nas._haversine_miles(lat, lng, s_lat, s_lng)
        if d <= miles:
            out.append({"name": name, "city": city, "lat": s_lat, "lng": s_lng, "lowest": lowest, "highest": highest,
                        "typology": typology, "students": students, "miles": d})
    return out


def _school_label(s: dict) -> str:
    label = nas._private_school_label(s["lowest"], s["highest"], s["typology"])
    return label.replace("SPECIAL EDUCATION", "Special education").replace("GRADES", "Grades").replace("GRADE", "Grade")


def _is_early_childhood(s: dict) -> bool:
    return s["highest"] < nas.PRIVATE_SCHOOL_MIN_HIGHEST_GRADE or s["lowest"] == s["highest"]


def _survey_entity(s: dict, base: Optional[dict] = None) -> dict:
    return {**(base or {}), "name": nas._private_school_display_name(s["name"].split(",")[0]) if not base else _research_name(base.get("name") or ""),
            "category": _school_label(s), "city": (s.get("city") or "").title() or (base or {}).get("city"),
            "lat": s["lat"], "lng": s["lng"], "survey_matched": True,
            **({} if base else {"relationship": "NEARBY", "source_type": "nces_pss",
                                "source_url": "https://nces.ed.gov/surveys/pss/privateschoolsearch/"})}


async def _check_private_schools(client: httpx.AsyncClient, key: str, area: MarketArea, block: dict, removed: list, now: str) -> list[dict]:
    lat, lng = area.centroid_lat, area.centroid_lng
    survey = _survey_schools_near(lat, lng, 12)
    keep: list[dict] = []
    for e in block.get("entities") or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        ours = nas._school_name_tokens(_research_name(e["name"]))
        match = None
        for s in sorted(survey, key=lambda s: s["miles"]):
            if s["miles"] > SURVEY_MATCH_MILES:  # "St. Patrick School" two towns over is another school
                break
            theirs = nas._school_name_tokens(s["name"])
            # "Saints Peter & Paul High School" is not "Saints Peter & Paul Elementary School" (Easton).
            levels_ours, levels_theirs = ours & _SCHOOL_LEVEL_WORDS, theirs & _SCHOOL_LEVEL_WORDS
            if levels_ours and levels_theirs and not levels_ours & levels_theirs:
                continue
            if ours and theirs and len(ours & theirs) / len(ours) >= 0.6 and len(ours & theirs) / len(theirs) >= 0.5:
                match = s
                break
        if match:
            if _is_early_childhood(match):
                removed.append({**e, "removed_reason": "preschool / early-childhood center (federal private school survey)", "removed_at": now})
                continue
            if match["students"] < nas.PRIVATE_SCHOOL_ESTABLISHED_STUDENTS:
                # A small school two years ago may have closed since - the Cheat Sheet's same check.
                try:
                    place = await _lookup(client, key, area, "private_schools", e)
                except httpx.HTTPError:
                    place = None
                if place and not nas._is_open(place):
                    removed.append({**e, "removed_reason": "closed (Google)", "removed_at": now})
                    continue
            keep.append(_survey_entity(match, e))
            continue
        if any(k in e["name"].lower() for k in nas.PRIVATE_SCHOOL_EXCLUDE_KEYWORDS) or re.search(r"\b(children'?s house|kindergarten|playhouse|pre-?k)\b", e["name"].lower()):
            removed.append({**e, "removed_reason": "preschool, daycare or not a K-12 school", "removed_at": now})
            continue
        # Not in the survey (it's voluntary - The Haverford School isn't in it): Google must know it as a school.
        try:
            place = await _lookup(client, key, area, "private_schools", e)
        except httpx.HTTPError:
            keep.append({k: v for k, v in e.items() if k != "distance_miles"})
            continue
        if not place:
            removed.append({**e, "removed_reason": "not found (federal survey or Google)", "removed_at": now})
            continue
        types = set(place.get("types") or [])
        if not nas._is_open(place):
            removed.append({**e, "removed_reason": "closed (Google)", "removed_at": now})
            continue
        # The Baldwin School is typed preschool too (it has a lower school) - only a school with no
        # grade school or high school listed is a preschool.
        if nas._place_primary_type(place) in ("preschool", "child_care_agency") or not (types & {"primary_school", "secondary_school", "school"}) \
                or ("preschool" in types and not types & {"primary_school", "secondary_school"}):
            removed.append({**e, "removed_reason": "a preschool or not a school (Google)", "removed_at": now})
            continue
        keep.append(_placed(e, place, "private_schools"))
    # A short list is filled from the survey: real schools (3rd grade and up, 50+ students) close to the center.
    # A rural town (Rehoboth Beach: the research's two schools were a typo and a preschool) gets its
    # nearest three within RURAL_FILL_MILES instead of an empty list.
    if len(keep) < PRIVATE_SCHOOLS_SHOWN:
        for s in sorted(survey, key=lambda s: s["miles"]):
            if len(keep) >= PRIVATE_SCHOOLS_SHOWN or s["miles"] > PRIVATE_SCHOOL_FILL_MILES and (len(keep) >= 3 or s["miles"] > RURAL_FILL_MILES):
                break
            if _is_early_childhood(s) or s["students"] < nas.PRIVATE_SCHOOL_MIN_STUDENTS or s["typology"] == nas.PRIVATE_SCHOOL_SPECIAL_EDUCATION_TYPOLOGY:
                continue
            if any(_near(k, s["lat"], s["lng"], 0.2) for k in keep):
                continue
            keep.append(_survey_entity(s))
    return keep


async def _drive_miles(client: httpx.AsyncClient, key: str, area: MarketArea, ents: list[dict]) -> None:
    pts = [e for e in ents if e.get("lat") is not None and e.get("lng") is not None]
    for i in range(0, len(pts), 25):
        chunk = pts[i:i + 25]
        routes = await nas._driving_routes(client, key, area.centroid_lat, area.centroid_lng, [(e["lat"], e["lng"]) for e in chunk], "town_places_check")
        for e, route in zip(chunk, routes):
            e["distance_miles"] = route[1] if route else round(nas._haversine_miles(area.centroid_lat, area.centroid_lng, e["lat"], e["lng"]), 1)


def _sort_key(category: str):
    def key(e: dict):
        d = e.get("distance_miles")
        rank = 0 if category != "medical" else {"HOSPITAL": 0, "URGENT_CARE": 1}.get(str(e.get("category") or "").upper(), 2)
        return (rank, d if isinstance(d, (int, float)) else 999)
    return key


async def check_town_places(db: AsyncSession, area: MarketArea) -> dict:
    """See the module docstring. Writes the checked lists back onto the shared town record."""
    key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not key or area.centroid_lat is None or area.centroid_lng is None:
        return {"checked": 0}
    facts = dict(area.facts or {})
    if not any((facts.get(c) or {}).get("entities") for c in (*CHECK_CATEGORIES, "private_schools")):
        return {"checked": 0}  # not researched yet - nothing to check (and no half-built lists)
    now = datetime.now(timezone.utc).isoformat()
    lists: dict[str, list[dict]] = {}
    removed: dict[str, list[dict]] = {}
    paused: list = []
    token = _paused.set(paused)
    try:
        return await _check(db, area, key, facts, now, lists, removed, paused)
    finally:
        _paused.reset(token)


async def _check(db, area, key, facts, now, lists, removed, paused) -> dict:
    checked = removed_count = 0
    async with httpx.AsyncClient() as client:
        for cat in CHECK_CATEGORIES:
            block = facts.get(cat) or {}
            ents = block.get("entities")
            if not isinstance(ents, list) or not ents:
                continue
            keep, gone = [], removed.setdefault(cat, [])
            for e in ents:
                if not isinstance(e, dict) or not e.get("name"):
                    continue
                if cat == "downtown_centers":
                    # What the research itself says it is: an office park isn't a downtown ("Radnor Corporate
                    # Center"), and a shopping center belongs under Shopping ("King of Prussia Town Center").
                    kind = f"{e.get('category') or ''} {e.get('name')}".lower()
                    if re.search(r"\b(office|corporate|business park|industrial)\b", kind):
                        gone.append({**e, "removed_reason": "an office park, not a downtown", "removed_at": now})
                        continue
                    # ...but Charlottesville's "Historic Downtown Mall" is its downtown (a pedestrian street).
                    if re.search(r"\b(shopping cent|retail cent|mall|outlet|plaza|marketplace|lifestyle center)", kind) \
                            and not re.search(r"\b(downtown|historic|main street)\b", e["name"].lower()):
                        lists.setdefault("shopping", []).append({k: v for k, v in e.items() if k != "distance_miles"})
                        gone.append({**e, "removed_reason": "a shopping center, not a downtown (moved to Shopping)", "removed_at": now})
                        continue
                try:
                    place = await _lookup(client, key, area, cat, e)
                except httpx.HTTPError:
                    keep.append({k: v for k, v in e.items() if k != "distance_miles"})
                    continue
                checked += 1
                if not place:
                    if cat in MUST_BE_ON_GOOGLE:
                        gone.append({**e, "removed_reason": "not found on Google", "removed_at": now})
                    else:  # a trail, a bus route, a shopping district - kept, but no guessed distance
                        keep.append({k: v for k, v in e.items() if k != "distance_miles"})
                    continue
                why = _rule_failure(cat, e, place)
                if why:
                    gone.append({**e, "removed_reason": why, "removed_at": now})
                    continue
                if cat == "downtown_centers":  # it only matches a store or center (_kind_fits)
                    lists.setdefault("shopping", []).append(_placed({**e, "category": "Shopping center"}, place, "shopping"))
                    gone.append({**e, "removed_reason": "a shopping center, not a downtown (moved to Shopping)", "removed_at": now})
                    continue
                keep.append(_placed(e, place, cat))
            lists.setdefault(cat, []).extend(keep)

        # The nearest general hospitals are always there, whatever the research found.
        med = lists.setdefault("medical", [])
        for h in await _hospitals(client, key, area):
            if not any(_near(m, h["lat"], h["lng"]) or _tokens(m.get("name", "")) == _tokens(h["name"]) for m in med):
                med.append(h)

        # Always: even when the research found none, the federal survey knows the real schools nearby.
        lists["private_schools"] = await _check_private_schools(client, key, area, facts.get("private_schools") or {}, removed.setdefault("private_schools", []), now)

        everything = [e for items in lists.values() for e in items]
        await _drive_miles(client, key, area, everything)

    for cat, items in lists.items():
        cap = MAX_DRIVE_MILES.get(cat, 25)
        kept = []
        for e in items:
            d = e.get("distance_miles")
            if isinstance(d, (int, float)) and d > cap:
                removed.setdefault(cat, []).append({**e, "removed_reason": f"too far to call nearby ({d} mi by car)", "removed_at": now})
                continue
            kept.append(e)
        # One entry per place (the research sometimes names a place twice).
        seen: list[dict] = []
        for e in sorted(kept, key=_sort_key(cat)):
            if any((_near(s, e.get("lat"), e.get("lng"), 0.05) and _tokens(s.get("name", "")) & _tokens(e.get("name", "")))
                   or _tokens(_research_name(s.get("name", ""))) == _tokens(_research_name(e.get("name", ""))) for s in seen):
                continue
            seen.append(e)
        if cat == "medical":
            hospitals = [e for e in seen if str(e.get("category") or "").upper() == "HOSPITAL"]
            others = [e for e in seen if str(e.get("category") or "").upper() != "HOSPITAL"]
            for h in hospitals[TOWN_HOSPITALS:]:
                removed.setdefault(cat, []).append({**h, "removed_reason": f"{TOWN_HOSPITALS} closer hospitals already listed", "removed_at": now})
            seen = hospitals[:TOWN_HOSPITALS] + others
        block = dict(facts.get(cat) or {})
        block["entities"] = seen
        gone = removed.get(cat) or []
        removed_count += len(gone)
        if gone:
            block["_removed"] = (list(block.get("_removed") or []) + gone)[-50:]
        block["_checked_at"] = now
        facts[cat] = block

    facts["_places_verified_at"] = now
    facts["_places_check_version"] = PLACES_CHECK_VERSION
    # Google's free calls ran out part-way: the places it couldn't check stay (with no guessed
    # distance) and town upkeep finishes the check once the free calls are back.
    if paused:
        facts["_places_check_waiting"] = True
    else:
        facts.pop("_places_check_waiting", None)
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return {"checked": checked, "removed": removed_count, "removed_names": {c: [r.get("name") for r in v] for c, v in removed.items() if v}}


PLACES_CHECK_VERSION = 1


# ---------------------------------------------------------------- the research's own words

_CITE_RE = re.compile(r"\(?<?/?cite[^>]*>|\(cite index=\"[^\"]*\">", re.IGNORECASE)
_PROCESS_RE = re.compile(
    r"\b(searched for|search(?:es)? (?:for|conducted|turned up|found|did not|didn't)|my search|web search|"
    r"comprehensive (?:web )?search|entities|listed above|all entities|research (?:confirms|shows|found|indicates)|"
    r"i (?:found|searched|could not|couldn't|was unable)|sources? (?:show|indicate|provide)|according to (?:my|the) search|"
    r"also identified|were identified|identified \d+|not able to (?:find|verify|confirm)|searched|searching|"
    r"sources? (?:from \d{4} )?confirm|multiple sources|within walking distance)\b", re.IGNORECASE)
_LABEL_RE = re.compile(r"\b(?:IN_AREA|ADJACENT|NEARBY|OTHER_MAJOR_MEDICAL|URGENT_CARE)\b")  # the research's own labels, all caps
_DISTANCE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:mi|miles?|minutes?|min)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")  # "1.4 miles" and "St. Davids" stay whole


def tidy_research_text(text: Optional[str], drop_distances: bool = True) -> str:
    """The research's notes and descriptions as a visitor should read them: no citation tags, no
    sentences about how it searched, and (by default) no distance claims - the checked list shows the
    real driving miles. Safe on anything, any number of times."""
    if not text or not isinstance(text, str):
        return text or ""
    out = _CITE_RE.sub("", text)
    out = re.sub(r"</?cite[^>]*>", "", out)
    # A markdown title the model opened with, stored flattened: "# Gettysburg, Pennsylvania Gettysburg offers..."
    out = re.sub(r"(?m)^\s*#+[^\n]*\n+", "", out)
    out = re.sub(r"^\s*#+\s*[^,\n]{1,60},\s*(?:(?:New|North|South|West|Rhode) [A-Z][a-z]+|[A-Z][a-z]+|[A-Z]{2})\s+", "", out).replace("**", "")
    # "comprehensive sports and comprehensive sports and recreational amenities" - a phrase said twice in a row
    out = re.sub(r"\b((?:\w+\s+){1,4}\w+)\s+(?:and\s+)?\1\b", r"\1", out)
    out = re.sub(r"\b(the|a|an|and|of|to|in)\s+\1\b", r"\1", out, flags=re.IGNORECASE)
    keep = []
    for s in _SENTENCE_SPLIT_RE.split(out):
        if not s.strip():
            continue
        if _PROCESS_RE.search(s) or _LABEL_RE.search(s) or (drop_distances and _DISTANCE_RE.search(s)):
            continue
        keep.append(s.strip())
    out = " ".join(keep)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def medical_label(category: Optional[str]) -> Optional[str]:
    if not category:
        return category
    key = str(category).upper()
    if key in _MEDICAL_LABELS:
        return _MEDICAL_LABELS[key]
    return key.capitalize() if key == str(category) else category  # "FAMILY MEDICINE" -> "Family medicine"
