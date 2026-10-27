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


# The same Google look-up is never paid for twice in a run of towns (owner, 10/26: "are we being
# wasteful?"): neighbouring towns name the same places (Bryn Mawr Hospital, Lankenau, Suburban
# Square - 114 of the first 881 look-ups were repeats), and research sometimes lists a place twice.
# The query names the place and its town, so the answer doesn't depend on which town asked.
_LOOKUP_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
LOOKUP_CACHE_HOURS = 24
_LOOKUP_CACHE_MAX = 5000


async def _search_once(client: httpx.AsyncClient, key: str, area: MarketArea, query: str, mask: str) -> list:
    import time
    hit = _LOOKUP_CACHE.get((query.lower(), mask))
    if hit and time.time() - hit[0] < LOOKUP_CACHE_HOURS * 3600:
        return hit[1]
    raw = await nas._search_text(client, key, area.centroid_lat, area.centroid_lng, query, LOOKUP_RADIUS_MILES,
                                 field_mask=mask, section="town_places_check")
    if len(_LOOKUP_CACHE) >= _LOOKUP_CACHE_MAX:
        _LOOKUP_CACHE.clear()
    _LOOKUP_CACHE[(query.lower(), mask)] = (time.time(), raw)
    return raw


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


_OFFICE_TYPES = {"corporate_office", "non_profit_organization", "association_or_organization", "local_government_office",
                 "government_office", "city_hall", "consultant", "real_estate_agency", "insurance_agency", "lawyer", "accounting",
                 # services, not shopping (Charlottesville's "Rio Hill Shopping Center" came back as Rio Hill Cleaners)
                 "laundry", "dry_cleaner", "car_wash", "car_repair", "hair_salon", "hair_care", "beauty_salon", "nail_salon",
                 "bank", "atm", "storage", "post_office", "gym", "dentist", "doctor"}
_OFFICE_NAME_RE = re.compile(r"\b(?:inc|llc|association|organization|foundation|chamber of commerce|council|alliance)\.?\s*$|"
                             r"\b(?:cleaners|dry cleaning|laundromat)\b", re.IGNORECASE)


def _description_is_about(description: str, name: str, town: str = "") -> bool:
    """False when the research's sentence is plainly about another place ("Rehoboth Mall is a shopping
    center..." under the Main Street office's listing). A sentence with no named subject passes."""
    m = re.match(r"\s*(?:<cite[^>]*>)?\s*((?:[A-Z][\w'&.-]*\s+){0,5}[A-Z][\w'&.-]*)\s+(?:is|was|has|offers|features|includes)\b", description or "")
    if not m:
        return True
    subject = _tokens(m.group(1), town) - _GENERIC_WORDS
    return not subject or bool(subject & _tokens(name, town))


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
    if category in ("shopping", "restaurants", "grocery") and primary == "route":
        # A street, not the place: Wayne's "Louella" boutique came back as Louella Drive.
        return "a street, not a place (Google: Route)"
    if category in ("shopping", "downtown_centers") and (primary in _OFFICE_TYPES or _OFFICE_NAME_RE.search(name)):
        # Rehoboth's "Rehoboth Mall" came back as "Rehoboth Beach Main Street Inc" - a nonprofit's office.
        return f"an office or service business, not a place to shop (Google: {nas._place_type_label(place) or primary})"
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
        raw = await _search_once(client, key, area, f"{name}, {entity.get('city') or area.name}, {area.state}", mask)
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
    if name != entity.get("name"):
        out["research_name"] = entity.get("name")  # what the research called it - its description is about that
    if out.get("description") and not _description_is_about(out["description"], name or "", str(entity.get("city") or "")):
        out.pop("description")
    if category == "parks" and "trail" in str(out.get("category") or "").lower() and not set(place.get("types") or []) & {"route", "hiking_area"}:
        # Researched as a trail, matched to the park it runs through (Gordons Pond Trail -> Cape
        # Henlopen State Park): the trail's length and route don't describe the park.
        out["category"] = nas._place_type_label(place) or "Park"
        out.pop("description", None)
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
    return _one_row_per_school(out)


def _one_row_per_school(rows: list[dict]) -> list[dict]:
    """The survey lists some schools twice - same spot, same grades, two spellings ("ARCHBISHOP
    CARROLL HIGH SCHOOL", Malvern and "ARCHBISHOP JOHN CARROLL HIGH SCHOOL", Radnor; "FUSION ACADEMY -
    ARDMORE" twice). One row per school: the fuller name, which also carries the right town."""
    out: list[dict] = []
    for s in sorted(rows, key=lambda s: -len(_match_tokens(s["name"].split(",")[0]))):
        if any(o["lowest"] == s["lowest"] and o["highest"] == s["highest"] and nas._haversine_miles(o["lat"], o["lng"], s["lat"], s["lng"]) < 0.03
               for o in out):
            continue
        out.append(s)
    return out


# ---- matching a researched school to its survey row (10/27 grader: the nearest close-enough name won -
# "Sacred Heart Academy" (Bryn Mawr, K-12) became Sacred Heart Catholic School in Havertown, "The
# Haverford School" became Friends School Haverford (PK-5), "Main Line Montessori" became Main Line
# Classical Academy). Now: the best name wins, not the nearest one.
_MATCH_STOPWORDS = {"the", "school", "schools", "schl", "sch", "of", "and", "st", "saint", "sts", "saints", "ss", "at", "for", "a", "inc"}
# The survey's short forms ("OUR LADY OF MT CARMEL SCHOOL" = "Our Lady of Mount Carmel School").
_MATCH_SAME_WORD = {"mt": "mount", "ft": "fort", "hs": "high", "ms": "middle", "el": "elementary", "elem": "elementary",
                    "acad": "academy", "prep": "preparatory", "intl": "international", "ctr": "center", "jr": "junior", "sr": "senior"}
# Words that say what kind of school it is, not which one ("Sacred Heart Academy" = "Sacred Heart
# Catholic School" by these alone) - they rank a match, they never make one.
_SOFT_SCHOOL_WORDS = {"academy", "prep", "preparatory", "catholic", "christian", "regional", "parish", "parochial",
                      "interparochial", "day", "country", "institute", "independent", "private"}
# "Friends' Central School Lower School" / "Villa Maria Academy - Upper School": one campus of a school.
_CAMPUS_RE = re.compile(r"[\s,:–—-]*\(?\b(lower|middle|upper)\s+(school|campus|division)\)?\s*$", re.IGNORECASE)
_RELIGIOUS_WORDS_RE = re.compile(r"\b(catholic|parochial|parish|diocese|archdiocese|christian|jewish|hebrew|torah|yeshiva|islamic|quaker|lutheran|episcopal|baptist|religious|faith-based|church)\b", re.IGNORECASE)
_SECULAR_WORDS_RE = re.compile(r"\b(nonsectarian|non-sectarian|secular|non-religious|nonreligious)\b", re.IGNORECASE)
_CATHOLIC_TYPOLOGY, _NONSECTARIAN_TYPOLOGY = {1, 2, 3}, {7, 8, 9}  # the survey's TYPOLOGY codes


def _match_tokens(name: str, city: str = "") -> set[str]:
    """A school name's words for matching - "Academy" counts ("Sacred Heart Academy" is not "Sacred
    Heart Catholic School"); the survey row's own town doesn't ("SACRED HEART ACADEMY BRYN MAWR")."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "").replace("’", "").replace("&", " and "))
    toks = {_MATCH_SAME_WORD.get(w, w) for w in words if w not in _MATCH_STOPWORDS}
    without = toks - set(re.findall(r"[a-z0-9]+", (city or "").lower()))
    return without or toks


def _grade_number(code: int) -> Optional[int]:
    """The survey's grade code as a grade: PK = -1, K = 0, 1st = 1 ... (ungraded = None)."""
    return None if code <= 1 else -1 if code == 2 else 0 if code in (3, 4) else code - 5


def _research_grades(text) -> Optional[tuple[int, int]]:
    """"Pre-K - 12" / "PK, K-8" / "NS-K" / "6 - 12" -> (lowest, highest), PK = -1, K = 0."""
    s = str(text or "").lower()
    if not s or "age" in s:
        return None
    s = re.sub(r"pre-?\s?k(indergarten)?|nursery|\bns\b|\bn\b|preschool", " pk ", s)
    s = re.sub(r"kindergarten|\bkg\b", " k ", s)
    found = []
    for t in re.findall(r"\bpk\b|\bk\b|\b\d{1,2}\b", s):
        found.append(-1 if t == "pk" else 0 if t == "k" else int(t))
    found = [g for g in found if -1 <= g <= 12]
    return (min(found), max(found)) if found else None


def _grades_clash(research: Optional[tuple[int, int]], s: dict) -> bool:
    """The research's grades and the survey row's share less than half of the shorter span (research
    6-12 vs Sacred Heart Catholic School's PK-8) - not the same school. One that only overstates
    (research PK-12, survey PK-8) is the same school with the research wrong."""
    lo, hi = _grade_number(s["lowest"]), _grade_number(s["highest"])
    if not research or lo is None or hi is None:
        return False
    overlap = min(research[1], hi) - max(research[0], lo) + 1
    shorter = min(research[1] - research[0], hi - lo) + 1
    return overlap / shorter < 0.5


def _survey_name_score(research_name: str, s: dict) -> Optional[float]:
    """How well a researched name fits a survey row (None = not the same school)."""
    city = s.get("city") or ""
    ours, theirs = _match_tokens(_CAMPUS_RE.sub("", research_name) or research_name, city), _match_tokens(s["name"].split(",")[0], city)
    ours |= _match_tokens(research_name, city) & _SCHOOL_LEVEL_WORDS  # a campus word still has to agree
    if not ours or not theirs:
        return None
    # "Saints Peter & Paul High School" is not "Saints Peter & Paul Elementary School" (Easton).
    lo, lt = ours & _SCHOOL_LEVEL_WORDS, theirs & _SCHOOL_LEVEL_WORDS
    if lo and lt and not lo & lt:
        return None
    core_o = (ours - _SOFT_SCHOOL_WORDS - _SCHOOL_LEVEL_WORDS) or (ours - _SCHOOL_LEVEL_WORDS) or ours
    core_t = (theirs - _SOFT_SCHOOL_WORDS - _SCHOOL_LEVEL_WORDS) or (theirs - _SCHOOL_LEVEL_WORDS) or theirs
    shared = core_o & core_t
    if len(shared) / len(core_o) < 0.6 or len(shared) / len(core_t) < 0.5:
        return None
    # One name has a telling word the other lacks, and the other has any word of its own ("Main Line
    # Montessori" / "Main Line Classical"; "Princeton Academy of the Sacred Heart" / "Stuart Country
    # Day School of the Sacred Heart") - two schools sharing words. A survey name that only adds to the
    # research's ("Caskey Torah Academy ..." for "Torah Academy ...") is the same school.
    if core_t - core_o and ours - theirs or core_o - core_t and theirs - ours:
        return None
    return len(ours & theirs) / len(ours | theirs)


def _best_survey_match(e: dict, survey: list[dict]) -> Optional[dict]:
    name = _research_name(e.get("name") or "")
    research = _research_grades(e.get("grades_served"))
    best, best_key = None, None
    for s in survey:
        if s["miles"] > SURVEY_MATCH_MILES:  # "St. Patrick School" two towns over is another school
            continue
        score = _survey_name_score(name, s)
        if score is None or _grades_clash(research, s):
            continue
        key = (score, -s["miles"])
        if best_key is None or key > best_key:
            best, best_key = s, key
    return best


def _school_label(s: dict) -> str:
    label = nas._private_school_label(s["lowest"], s["highest"], s["typology"])
    return label.replace("SPECIAL EDUCATION", "Special education").replace("GRADES", "Grades").replace("GRADE", "Grade")


def _is_early_childhood(s: dict) -> bool:
    return s["highest"] < nas.PRIVATE_SCHOOL_MIN_HIGHEST_GRADE or s["lowest"] == s["highest"]


def _shown_school_name(research: str, s: dict) -> str:
    """The name a matched school is shown under.
    - The research named one campus ("Friends' Central School Lower School") and the survey row is the
      whole school (no Lower/Middle/Upper in its name): the row's grades and spot are the whole
      school's (PK-12, main campus), so the name is too - "Friends' Central School". Showing the
      campus name with the whole school's grades is what the grader caught (Lower School "PK-12").
    - The survey's name holds the research's and says more ("Torah Academy of Greater Philadelphia" ->
      "Caskey Torah Academy of Greater Philadelphia", "Archbishop Carroll" -> "Archbishop John
      Carroll"): the fuller, official name. A town ("... Bryn Mawr") or a kind word ("Parish") isn't more."""
    city = s.get("city") or ""
    survey_name = _survey_display_name(s)
    theirs = _match_tokens(s["name"].split(",")[0], city)
    name = research
    if _CAMPUS_RE.search(research) and not theirs & {"lower", "middle", "upper"}:
        name = _CAMPUS_RE.sub("", research).strip(" -–—,") or survey_name
    ours = _match_tokens(name, city)
    if ours < theirs and (theirs - ours) - _SOFT_SCHOOL_WORDS:
        return survey_name
    return name


def _survey_display_name(s: dict) -> str:
    """"MAIN LINE CLASSICAL ACADEMY INC" -> "Main Line Classical Academy" (the company suffix isn't the name)."""
    name = nas._private_school_display_name(s["name"].split(",")[0])
    return re.sub(r",?\s+(?:Inc|Incorporated|LLC)\.?$", "", name, flags=re.IGNORECASE) or name


def _survey_entity(s: dict, base: Optional[dict] = None) -> dict:
    if not base:
        return {"name": _survey_display_name(s), "category": _school_label(s),
                "city": (s.get("city") or "").title() or None, "lat": s["lat"], "lng": s["lng"], "survey_matched": True,
                "relationship": "NEARBY", "source_type": "nces_pss", "source_url": "https://nces.ed.gov/surveys/pss/privateschoolsearch/"}
    research = _research_name(base.get("research_name") or base.get("name") or "")
    research = re.sub(r",?\s+(?:Inc|Incorporated|LLC)\.?$", "", research, flags=re.IGNORECASE) or research
    out = {k: v for k, v in base.items() if k not in ("address", "google_checked", "research_name")}
    if base.get("lat") is None or base.get("lng") is None or nas._haversine_miles(base["lat"], base["lng"], s["lat"], s["lng"]) > 0.05:
        out.pop("distance_miles", None)  # a different spot than last time - driven again
    out.update({"name": _shown_school_name(research, s), "category": _school_label(s),
                "city": (s.get("city") or "").title() or base.get("city"), "lat": s["lat"], "lng": s["lng"], "survey_matched": True})
    if out["name"] != research:
        out["research_name"] = research
    # The survey says what kind of school it is: a research line calling nonsectarian St. Peter's School
    # "Catholic" (Rittenhouse, 10/27) - or a Catholic school "secular" - is dropped, not shown.
    desc = str(out.get("description") or "")
    if desc and (s["typology"] in _NONSECTARIAN_TYPOLOGY and _RELIGIOUS_WORDS_RE.search(desc)
                 or s["typology"] in _CATHOLIC_TYPOLOGY and _SECULAR_WORDS_RE.search(desc)):
        out.pop("description")
    return out


def _as_researched(e: dict) -> tuple[dict, bool]:
    """An entry of a list that was checked before, back to what the research said: a survey row's
    grades are that earlier match's answer, and outlived it when the match was wrong (Easton's high
    school kept the elementary school's "Grades PK-8" after the level rule landed). Also says whether
    its spot is a survey row's (no Google listing) - dropped if the survey no longer matches."""
    e = dict(e)
    survey_spot = False
    if e.pop("survey_matched", None):
        e.pop("category", None)
        if e.get("research_name"):
            e["name"] = e.pop("research_name")
        survey_spot = not e.get("google_checked")
    return e, survey_spot


async def _check_private_schools(client: httpx.AsyncClient, key: str, area: MarketArea, block: dict, removed: list, now: str) -> list[dict]:
    lat, lng = area.centroid_lat, area.centroid_lng
    survey = _survey_schools_near(lat, lng, 12)
    keep: list[dict] = []
    used: list[dict] = []
    for e in block.get("entities") or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        e, survey_spot = _as_researched(e)
        match = _best_survey_match({**e, "name": e.get("research_name") or e["name"]}, survey)
        if match and any(u is match for u in used):
            # The research named the same school twice ("Friends' Central" and its Lower School).
            removed.append({**e, "removed_reason": "listed twice (same school in the federal survey)", "removed_at": now})
            continue
        if not match and survey_spot:  # the survey row it was matched to before isn't this school
            e = {k: v for k, v in e.items() if k not in ("lat", "lng", "distance_miles")}
        if match:
            used.append(match)
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
        # Its own listed grades stop before 3rd grade ("Main Line Montessori School", "NS-K"): an
        # early-childhood program, whatever it's called - no look-up needed.
        grades = _research_grades(e.get("grades_served"))
        if grades and grades[1] < 3:
            removed.append({**e, "removed_reason": "preschool / early-childhood program (its own grades)", "removed_at": now})
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
    # A place that already has its driving distance (checked before a paused run stopped) isn't routed again.
    pts = [e for e in ents if e.get("lat") is not None and e.get("lng") is not None
           and not isinstance(e.get("distance_miles"), (int, float))]
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


def _admin_clash(e: dict, held: list[dict], gone: list[dict]) -> bool:
    """The same place as one the admin added/edited (held) or took off the list (gone) - by name or spot."""
    return any(_tokens(_research_name(e.get("name", ""))) == _tokens(_research_name(h.get("name", ""))) or _near(e, h.get("lat"), h.get("lng"), 0.05)
               for h in (*held, *gone))


async def _check(db, area, key, facts, now, lists, removed, paused) -> dict:
    checked = removed_count = 0
    # Finishing a check Google's free-call line stopped part-way: only the places it never reached
    # are looked up - the ones it did check keep their result and driving distance.
    finishing = bool(facts.get("_places_check_waiting"))
    # Admin changes (Neighborhoods tab, admin_area_routes) are never re-checked: an added or edited
    # place keeps its spot and every field, and a place the admin took off is never added back.
    held: dict[str, list[tuple[int, dict]]] = {}
    admin_gone = {c: [r for r in ((facts.get(c) or {}).get("_removed") or []) if isinstance(r, dict) and r.get("admin_removed")]
                  for c in (*CHECK_CATEGORIES, "private_schools")}
    async with httpx.AsyncClient() as client:
        for cat in (*CHECK_CATEGORIES, "private_schools"):
            for i, e in enumerate((facts.get(cat) or {}).get("entities") or []):
                if isinstance(e, dict) and e.get("admin_edited"):
                    held.setdefault(cat, []).append((i, e))
        for cat in CHECK_CATEGORIES:
            block = facts.get(cat) or {}
            ents = block.get("entities")
            if not isinstance(ents, list) or not ents:
                continue
            keep, gone = [], removed.setdefault(cat, [])
            for e in ents:
                if not isinstance(e, dict) or not e.get("name") or e.get("admin_edited"):
                    continue
                if finishing and e.get("google_checked") and isinstance(e.get("distance_miles"), (int, float)):
                    keep.append(e)
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
            if not any(_near(m, h["lat"], h["lng"]) or _tokens(m.get("name", "")) == _tokens(h["name"]) for m in med) \
                    and not _admin_clash(h, [e for _, e in held.get("medical", [])], admin_gone["medical"]):
                med.append(h)

        # Always: even when the research found none, the federal survey knows the real schools nearby.
        ps_block = dict(facts.get("private_schools") or {})
        ps_block["entities"] = [e for e in ps_block.get("entities") or [] if not (isinstance(e, dict) and e.get("admin_edited"))]
        lists["private_schools"] = [e for e in await _check_private_schools(client, key, area, ps_block, removed.setdefault("private_schools", []), now)
                                    if not _admin_clash(e, [h for _, h in held.get("private_schools", [])], admin_gone["private_schools"])]

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
        mine = held.get(cat, [])
        if mine:  # the admin's places back in their own spots, untouched; a checked copy of one drops out
            seen = [e for e in seen if not _admin_clash(e, [h for _, h in mine], [])]
            for i, e in mine:
                seen.insert(min(i, len(seen)), e)
        order = (facts.get(cat) or {}).get("_admin_order")
        if isinstance(order, list) and order:  # the admin's own order of the list
            pos = {n: i for i, n in enumerate(order)}
            seen = sorted(seen, key=lambda e: pos.get(e.get("name"), len(order)))
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
    r"sources? (?:from \d{4} )?confirm|multiple sources|within walking distance|research confirmed|confirmed finding|"
    r"platform data|existing data|platform's note|the platform|data aligns|well[- ]documented|consistently listed|according to (?:yelp|google|tripadvisor|the research)|"
    r"documented as|(?:only|main)\b[^.]{0,40}\bidentified)\b", re.IGNORECASE)
_LABEL_RE = re.compile(r"\b(?:IN_AREA|ADJACENT|NEARBY|OTHER_MAJOR_MEDICAL|URGENT_CARE)\b")  # the research's own labels, all caps
_DISTANCE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:mi|miles?|minutes?|min)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")  # "1.4 miles" and "St. Davids" stay whole
_ABBREV_END_RE = re.compile(r"(?:^|[\s(])(?:U\.S|St|Ste|Mt|Ft|Dr|Rd|Ave|Blvd|Ln|Pkwy|No|Jr|Sr|Inc|Co|vs|approx|e\.g|i\.e|a\.m|p\.m|[A-Z])\.$")


def split_sentences(text: str) -> list[str]:
    """Sentences, with abbreviations kept whole - splitting at "U.S. News" left Lankenau's description
    reading "News & World Report." and Radnor's "Vybe urgent care is located in St."."""
    parts: list[str] = []
    for p in _SENTENCE_SPLIT_RE.split(text or ""):
        if parts and _ABBREV_END_RE.search(parts[-1].rstrip()):
            parts[-1] += " " + p
        else:
            parts.append(p)
    return [p for p in parts if p.strip()]


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
    # Sizes the check can't confirm (Willows Park "5 acres" - it's 47.5; Radnor Trail "four miles" - 2.4).
    out = _SIZE_MODIFIER_RE.sub("", out)
    out = _SIZE_PAREN_RE.sub("", out)
    out = _SIZE_OF_RE.sub("", out)
    out = _SIZE_WITH_RE.sub("", out)
    out = _HOURS_RE.sub("", out)  # before the 24-hour sentence rule, so "open 24 hours" loses only those words
    out = _LEGACY_LINE_RE.sub("", out)  # SEPTA's retired "R5" names
    # "residents 'go into town.' The downtown..." - a quote closing after the period hid the sentence
    # end, and the page's sentence splitter then showed "' The downtown...".
    out = re.sub("([.!?])(['\"’”])(?=\\s|$)", r"\2\1", out)
    keep, skipped = [], False
    for s in split_sentences(out):
        # A sentence leaning on a dropped one ("All are privately operated...") goes with it.
        skipped = bool(_PROCESS_RE.search(s) or _LABEL_RE.search(s) or (drop_distances and _DISTANCE_RE.search(s))
                       or _is_bus_route_sentence(s) or _STALE_CLAIM_RE.search(s) or _SIZE_LEFT_RE.search(s)
                       or (skipped and _LEANS_BACK_RE.match(s.strip())))
        if skipped:
            continue
        keep.append(s.strip())
    out = " ".join(keep)
    out = _COUNT_COMPOUND_RE.sub("", out)
    out = _COUNT_RE.sub("", out)
    # "Located at 17616 Coastal Highway, hub for..." opening a sentence -> "Hub for..."
    out = _ADDRESS_OPENER_RE.sub(lambda m: m.group(1) + m.group(2).upper(), out)
    out = _ADDRESS_RE.sub(" ", out)
    out = re.sub(r"\s+([,.;])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()
    out = re.sub(r"[,;]+\.", ".", out)  # "Orange Theory Fitness;." once a trailing count is gone
    out = re.sub(r"\s{2,}", " ", re.sub("(^|\\s)['\"‘’“”](?=\\s|$)", " ", out)).strip()  # a lone quote mark left by the research ("borough. ' The downtown")
    out = re.sub("^['\"‘’“”]+(?=[A-Za-z])", "", out)  # one opening the text with no partner
    out = re.sub(r"\s*\(\s*\)", "", out)  # "Natural Lands preserve ()" once a size inside it is gone
    out = re.sub(r"\b(?:an?|the)\s+(an?|the)\b", r"\1", out, flags=re.IGNORECASE)  # "is an a welcoming community"
    out = re.sub(r"\s+([,.;])", r"\1", out)
    out = re.sub(r"^[,;\s]+", "", out)
    return out[:1].upper() + out[1:] if out else out


# SEPTA's retired Regional Rail numbers ("R5"), in or out of brackets - riders now see line names.
_LEGACY_LINE_RE = re.compile(r"\s*\((?:designated\s+)?R[1-8](?:\s+[A-Z][\w/]*)?\)|\bR[1-8]\b\s*(?=[A-Z(]|$)?")


# How many stores / restaurants / rides a place has goes stale and the research's sources disagree
# (Rehoboth: "223 total restaurants", Tanger "140+" stores - 115 today, Funland "20 rides" - 18).
_COUNTED = (r"restaurants?|eateries|eatery|stores?|shops?|outlets?|boutiques?|brands?|rides?|attractions?|"
            r"vendors?|galleries|gallery|dining options?|businesses|merchants?|tenants?|retailers?|"
            # the research miscounts its own lists (Malvern "five major supermarkets" - four listed;
            # State College "five surrounding townships" - six named)
            r"supermarkets?|grocery stores?|hospitals?|urgent care (?:centers?|facilities|clinics?)|parks?|preserves?|"
            r"townships?|boroughs?|stations?|schools?")
_NUMBER = (r"(?:\d[\d,]*(?:\s*(?:-|–|to)\s*\d[\d,]*)?\+?|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
           r"Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)")  # "40-50", "20+", "1,200", "five"
_COUNT_COMPOUND_RE = re.compile(rf"\b{_NUMBER}[- ](?:restaurant|store|shop|outlet|boutique|brand|ride|attraction|vendor|gallery|merchant|tenant|retailer|seat)"
                                r"\s+(?=[a-z])")  # "a 140+ store outlet mall"
_COUNT_RE = re.compile(rf"(?<![-–/])(?<!grades )(?<!grade )(?<!Grades )\b(?:(?:over|more than|nearly|almost|about|approximately|around|some|roughly)\s+)?{_NUMBER}\s+"  # not "K-12"
                       rf"(?:total\s+|different\s+|unique\s+)?"
                       # up to three describing words between ("five major full-service supermarkets"),
                       # never a distance or time ("15 minutes from parks")
                       rf"(?=(?:(?!(?:from|to|of|in|on|at|within|by|for|away|minutes?|miles?|hours?|blocks?|walk|drive|min|mi|"
                       rf"students?|homes?|residents?|people|percent|years?|attending|serving|with)\b)"
                       rf"[a-z][\w'-]*\s+){{0,3}}(?:{_COUNTED})\b)", re.IGNORECASE)
# A street address inside a sentence - the checked address is shown on its own line, and the
# research's one was sometimes another branch (Beebe Walk-In Care "at 18941 John J Williams Hwy").
_ADDRESS_CORE = (r"(?:at|on|and)\s+\d{1,6}\s+(?:[A-Z][\w.']*\s+){1,5}"
                 r"(?:Rd|Road|Hwy|Highway|Ave|Avenue|St|Street|Way|Wy|Blvd|Boulevard|Dr|Drive|Ln|Lane|Pike|Pkwy|Parkway|Ct|Court|"
                 r"Plaza|Pl|Place|Route|Rte|Circle|Cir|Terrace|Trail|Trl)\b(?:\s+(?:N|S|E|W|NE|NW|SE|SW)\b)?(?:,?\s*(?:#|Unit|Suite|Ste\.?)\s*\w+)?")
_ADDRESS_RE = re.compile(r",?\s*\b(?:[Ll]ocated\s+)?" + _ADDRESS_CORE + ",?")
_ADDRESS_OPENER_RE = re.compile(r"(^|[.!?]\s+)Located\s+" + _ADDRESS_CORE + r",?\s*([a-z])")
# Bus route numbers and colored bus "lines" change every few months (SEPTA ended Route 106 on
# 8/24/2026; rabbittransit's Gold Line is seasonal) - only stations, which the check confirms, stay.
_BUS_WORD_RE = re.compile(r"\b(?:bus(?:es)?|transit|SEPTA|DART|ChescoBus|SCCOOT|rabbittransit|Krapf)\b", re.IGNORECASE)
_BUS_ROUTE_RE = re.compile(r"\broutes?\s+\d|\b(?:Red|Blue|Gold|Green|Gray|Grey|Orange|Purple|Yellow|Silver|Brown) Line\b|"
                           r"\bfixed routes?\b|\b(?:Red|Blue|Gold|Green|Gray|Grey|Orange|Purple|Yellow|Silver|Brown)\b[^.]{0,40}\blines\b",
                           re.IGNORECASE)  # rabbittransit "Four fixed routes (Lincoln, Blue, Gray, and Gold lines)... daily"


def _is_bus_route_sentence(s: str) -> bool:
    # "the D1 (formerly Route 101 Media line)" names a trolley line's history, not a bus route.
    if re.search(r"\bfixed routes?\b", s, re.IGNORECASE):
        return True
    return bool(_BUS_WORD_RE.search(s) and _BUS_ROUTE_RE.search(re.sub(r"formerly\s+Route\s+\d+", "", s, flags=re.IGNORECASE)))


# Claims that go stale or can't be checked: rankings (Lankenau "top five" - No. 10 in 2026-27),
# "closest" (AFC Havertown for Ardmore - Wynnewood is nearer), resident counts that disagree with
# the Census row (Media "5,350 residents" vs 5,868), and how the research found things.
_STALE_CLAIM_RE = re.compile(
    r"\branked\b|\bU\.?\s?S\.? News\b|\btop[- ](?:\d+|five|ten|three)\b|\bNo\. \d+\b|\b(?:closest|nearest)\b|"
    r"\b\d[\d,]{2,}\s+(?:residents|people|inhabitants)\b|\bpopulation of\b|\bsearch results\b|\bmarkets itself\b|"
    r"\b24[- ]hours?\b(?!\s+(?:emergency|ER\b))", re.IGNORECASE)  # "Wegmans is 24-hour" - it's 6 a.m. to midnight
_SPELLED = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty"
_SIZE_MODIFIER_RE = re.compile(rf"\b(?:(?:nearly|about|approximately|over|more than|roughly|some|almost)\s+)?(?:\d[\d,]*(?:\.\d+)?|{_SPELLED})\+?-(?:mile|acre|bed)\s+",
                               re.IGNORECASE)  # "3.2-mile trail", "a 264-bed hospital"
_SIZE_PAREN_RE = re.compile(r"\s*\(\s*(?:about\s+|approximately\s+|over\s+)?\d[\d,.]*\+?\s*acres?\s*\)", re.IGNORECASE)  # "(71 acres)"
_SIZE_OF_RE = re.compile(rf"\b(?:(?:nearly|about|approximately|over|more than|roughly|some|almost)\s+)?(?:\d[\d,.]*\+?|{_SPELLED})\s+(?:miles|acres)\s+of\s+",
                         re.IGNORECASE)  # "12+ miles of hiking trails" -> "hiking trails"
_SIZE_WITH_RE = re.compile(r"\s+with\s+(?:about\s+|over\s+)?\d[\d,.]*\+?\s+acres\b", re.IGNORECASE)  # "Haverford Reserve with 124 acres"
_SIZE_LEFT_RE = re.compile(r"\b\d[\d,.]*\+?\s+acres?\b", re.IGNORECASE)
# Opening hours (Wegmans Malvern "open 24 hours" - it's 6 a.m. to midnight).
_HOURS_RE = re.compile(
    r",?\s*\bopen\s+(?:24\s+hours(?:\s+a\s+day)?|24/7|around the clock|(?:daily\s+)?(?:from\s+)?\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?)\s*(?:to|-|–)\s*"
    r"(?:\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?)|midnight|noon)(?:\s+daily)?|(?:7|seven) days a week)"
    r"|,?\s*\b(?:and\s+)?closed\s+(?:on\s+)?(?:Sun|Mon|Tues|Tue|Wednes|Wed|Thurs|Thu|Fri|Satur|Sat)\w*"
    r"(?:\s*(?:,|and|&)\s*(?:Sun|Mon|Tues|Tue|Wednes|Wed|Thurs|Thu|Fri|Satur|Sat)\w*)*", re.IGNORECASE)  # "closed Sundays and Mondays"
# A sentence leaning on the one before it ("These are the primary grocery options...") goes with it.
_LEANS_BACK_RE = re.compile(r"^(?:These|This|They|Those|All|Both|Each|It|Together|Additionally|Additional|Also|Of these|"
                            r"The (?:hospital|school|station|store|market|park|center|mall|restaurant))\b")


def drop_removed_mentions(text: Optional[str], removed_names: list[str]) -> str:
    """Sentences that name a place the check took off the page (Rehoboth's grocery text still listed
    "Stop & Shop", which has no Delaware stores; its school text named a surf school)."""
    if not text or not removed_names:
        return text or ""
    pats = [re.compile(r"\b" + re.escape(n) + r"\b", re.IGNORECASE) for n in removed_names if len(n) >= 5]
    sentences = split_sentences(text)
    keep, dropped, any_dropped = [], False, False
    for s in sentences:
        dropped = any(p.search(s) for p in pats) or (dropped and bool(_LEANS_BACK_RE.match(s.strip())))
        any_dropped = any_dropped or dropped
        keep.append(None if dropped else s)
    if not any_dropped:
        return text
    # The page shows the first two sentences: what's left of those, never a third pulled up in
    # their place (the later ones were weaker - "Research confirmed finding...").
    return " ".join(s for s in keep[:2] if s).strip()


_DISTRICT_MENTION_RE = re.compile(r"\b((?:[A-Z][\w.'&]*(?:-[A-Z][\w.'&]*)*\s+){1,6})School District\b")


def fix_district_names(text: Optional[str], districts: list[str]) -> str:
    """Puts the town's real school district names back into AI-written text that shortened them -
    Malvern's summary said "Valley School District" for Great Valley School District. A mention
    that is part of exactly one real name ("Valley", "Tredyffrin") becomes that full name; a real
    name, or a part that fits more than one district, is left alone. Safe to run any number of times."""
    if not text or not isinstance(text, str) or not districts:
        return text or ""

    def words(core: str) -> list[str]:
        return [w for w in re.split(r"[\s-]+", core.strip()) if w]

    cores = {}
    for d in districts:
        m = re.match(r"(.+?)\s+School District\b", str(d or "").strip())
        if m:
            cores[m.group(1).strip()] = words(m.group(1))

    def holders(part: list[str]) -> list[str]:
        n = len(part)
        return [c for c, ws in cores.items() if any(ws[i:i + n] == part for i in range(len(ws) - n + 1))]

    def fix(m: re.Match) -> str:
        mention = words(m.group(1))
        # "In Great Valley School District" - the real name is a tail of the capitalised run.
        for i in range(len(mention)):
            if any(ws == mention[i:] for ws in cores.values()):
                return m.group(0)
        for i in range(len(mention)):
            found = holders(mention[i:])
            if len(found) == 1:
                lead = m.group(1).split()[:i] if i and len(m.group(1).split()) == len(mention) else []
                return " ".join(lead + [found[0], "School District"])
        return m.group(0)

    return _DISTRICT_MENTION_RE.sub(fix, text)


def medical_label(category: Optional[str]) -> Optional[str]:
    if not category:
        return category
    key = str(category).upper()
    if key in _MEDICAL_LABELS:
        return _MEDICAL_LABELS[key]
    return key.capitalize() if key == str(category) else category  # "FAMILY MEDICINE" -> "Family medicine"
