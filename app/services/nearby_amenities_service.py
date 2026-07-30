import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy import func, select

from app.config.logging import get_logger
from app.models.database import Property
from app.models.property import NearbyAmenitiesResponse, NearbyPlace

logger = get_logger(__name__)

CACHE_TTL_DAYS = 180  # Schools/hospitals/parks essentially never move - safe to cache far longer than a listing's lifetime.
# Bump this whenever _fetch_live's shape or logic meaningfully changes (renamed/added fields, new
# categories, different filtering) - otherwise a cache written under the old logic (e.g. the pre-rename
# "hospitals" field, or before the assigned-schools search existed) would silently keep being served as
# "fresh" for up to CACHE_TTL_DAYS, masking the new behavior entirely instead of just being a cache miss.
AMENITIES_SCHEMA_VERSION = 7
MAX_RESULTS_PER_CATEGORY = 5
DOWNTOWN_MAX_DISTANCE_MILES = 10
DOWNTOWN_CANDIDATE_POOL = 15
DOWNTOWN_MIN_NEARBY_BUSINESSES = 3  # below this, treat it as "just a named place," not a real walkable town center
DOWNTOWN_VALIDATION_RADIUS_MILES = 0.5
DEFAULT_SEARCH_RADIUS_MILES = 8

PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "places.displayName,places.formattedAddress,places.location"
TYPED_FIELD_MASK = FIELD_MASK + ",places.types"  # only needed where we classify by Places' own sub-type (medical care)
ROUTES_FIELD_MASK = "originIndex,destinationIndex,duration,distanceMeters,condition"

# Google Places has one flat "airport" type covering everything from Philadelphia International down to
# hospital-rooftop heliports, grass ultralight strips, private airfields, and FBO/charter businesses that
# happen to be tagged as operating "at" an airport (e.g. "flyADVANCED Wings" showing up as its own result
# next to the general-aviation field it operates out of) - there's no Places field for "has scheduled
# commercial passenger service." So airports are sourced from a curated, static dataset instead of a live
# Places search: OurAirports' public dataset (https://ourairports.com/data/), filtered once to US
# large/medium airports with scheduled_service=yes. That structurally rules out FBOs/charter operators
# (not airports at all, so never in the dataset) and general-aviation fields like Wings Field (no
# scheduled service), without any name-keyword guessing, and lets the true nearest commercial airport
# surface even when it's the 3rd or 4th closest thing that merely LOOKS like an airport.
AIRPORT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "major_airports.json"
AIRPORT_CANDIDATE_POOL = 8  # nearest-by-straight-line airports sent to the Routes API for real driving-distance ranking


@lru_cache(maxsize=1)
def _load_major_airports() -> list[dict]:
    with open(AIRPORT_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)

# Google has no concept of school attendance zones, and - critically - a district having multiple
# schools at a level (Lower Merion's 5 elementary schools, its 2 high schools) means there is no
# reliable way to guess which one a specific address is zoned for from geography alone: attendance
# boundaries are irregular polygons, not "nearest school wins," so even a driving-distance-ranked
# Google search would confidently show the wrong school for a meaningful share of addresses. The only
# case we treat as safe to fill in without a direct MLS answer is a district that - looking at every
# other listing we already have on file for it - only ever has ONE real school at that level, so there
# is no zoning question to get wrong in the first place.
SCHOOL_NAME_STOPWORDS = {
    "school", "district", "schools", "of", "the", "county", "township", "area", "regional", "public",
    "sd", "csd", "isd", "usd", "consolidated", "independent", "unified", "community",
    "senior", "junior", "elementary", "middle", "high", "jr", "sr", "academy",
}
# Real MLS data for a single school is wildly inconsistent across listings - abbreviations, typos,
# "Senior"/"Middle" suffix differences ("CONESTOGA", "CONESTOGA SENIOR", "CONESTOGOA" are all the same
# real high school). These thresholds need to tell "that's just noisy data entry for one real school"
# apart from "this district genuinely has a second real school" (e.g. Harriton vs. Lower Merion HS) -
# the latter would still show a majority share, just not an overwhelming one, so besides a high
# percentage bar there's also a hard cap on the absolute count of everything outside the top cluster.
SCHOOL_DOMINANCE_THRESHOLD = 0.95
SCHOOL_DOMINANCE_MAX_MINORITY_COUNT = 5
SCHOOL_NAME_FUZZY_MATCH_RATIO = 0.8


def _first_meaningful_token(name: str) -> str:
    tokens = re.findall(r"[a-z]+", name.lower())
    tokens = [t for t in tokens if t not in SCHOOL_NAME_STOPWORDS] or tokens
    return tokens[0] if tokens else name.lower()


def _cluster_school_names(counts: "Counter[str]") -> list[tuple[str, int]]:
    """Groups raw MLS school-name variants (typos, abbreviations, level-suffix differences) by fuzzy
    similarity of their first meaningful word, so real-world messiness doesn't read as several different
    schools. Returns (most-common raw variant, total count) per cluster, largest cluster first."""
    clusters: list[dict] = []
    for raw, count in counts.items():
        token = _first_meaningful_token(raw)
        match = next((c for c in clusters if SequenceMatcher(None, token, c["key"]).ratio() >= SCHOOL_NAME_FUZZY_MATCH_RATIO), None)
        if match:
            match["count"] += count
            if count > match["best_count"]:
                match["best_name"], match["best_count"] = raw, count
        else:
            clusters.append({"key": token, "count": count, "best_name": raw, "best_count": count})
    clusters.sort(key=lambda c: -c["count"])
    return [(c["best_name"], c["count"]) for c in clusters]


def _dominant_school_name(counts: "Counter[str]") -> Optional[str]:
    if not counts:
        return None
    clusters = _cluster_school_names(counts)
    total = sum(count for _, count in clusters)
    top_name, top_count = clusters[0]
    minority_count = total - top_count
    if minority_count > SCHOOL_DOMINANCE_MAX_MINORITY_COUNT:
        return None  # enough disagreement that it reads as a real second school, not just noisy data entry
    return top_name if total and top_count / total >= SCHOOL_DOMINANCE_THRESHOLD else None


async def _known_district_schools(db, district_name: str) -> dict[str, "Counter[str]"]:
    """The real, ground-truth answer for 'what school is X in this district actually called' often
    already sits in our own database - other listings in the same district that DID have MLS fill in
    the assigned school. That's more reliable than matching against the district's own name, which
    fails outright for districts (common in PA) whose individual schools aren't named after the district
    at all - e.g. Tredyffrin-Easttown's high school is simply "Conestoga"."""
    result = await db.execute(
        select(Property.elementary_school, Property.middle_or_junior_school, Property.high_school)
        .where(func.upper(Property.school_district_name) == district_name.upper())
    )
    elem, mid, high = Counter(), Counter(), Counter()
    for e, m, h in result.all():
        if e:
            elem[e.strip()] += 1
        if m:
            mid[m.strip()] += 1
        if h:
            high[h.strip()] += 1
    return {"Elementary School": elem, "Middle School": mid, "High School": high}


# Places API has no first-class type for "urgent care," "primary care," or "rehab" - only the broad
# "doctor" type covers all of them, mixed in with everything else a doctor's office could be. So medical
# care classification is: trust Google's own "hospital"/"physiotherapist" types where they exist, and for
# "doctor" fall back to matching against the practice name. Best-effort, not guaranteed-accurate.
MEDICAL_EXCLUDE_KEYWORDS = [
    "cosmetic", "chiropract", "wellness", "regenerative", "med spa", "medspa", "aesthetic",
    "botox", "laser hair", "acupuncture", "massage", "hormone", "iv therapy", "iv hydration",
    "weight loss", "day spa", "medi spa",
]
MEDICAL_PRIORITY = {"Hospital": 0, "Urgent Care": 0, "Primary Care": 1, "Rehabilitation": 1, "Medical Clinic": 2}
# Nearby Search's default ranking ("POPULARITY") is a reasonable quality signal - it favors real, well-
# known places over obscure ones - but with only MAX_RESULTS_PER_CATEGORY (5) requested, a handful of
# very prominent-but-farther results (downtown Philadelphia's hospitals/parks/museums) can fill every
# slot and crowd out a genuinely closer, still-real result (e.g. Bryn Mawr Hospital at 2.1mi, ranked
# 8th by popularity behind a string of Philadelphia medical campuses 6-7mi out; Rolling Hill Park at
# 1.5mi similarly buried behind Fairmount Park and the Philadelphia Zoo). Pulling a wider raw pool before
# our own distance sort (_to_sorted_places) fixes that without needing rankPreference=DISTANCE, which
# trades this problem for a worse one on broad types like "hospital" - Google tags plenty of small
# imaging centers, wellness clinics, and specialty practices as type=hospital too, and ranking purely by
# raw distance surfaces those tiny, more-numerous clinics ahead of the genuinely closest real hospital.
NEARBY_CANDIDATE_POOL = 20  # API max per request

_warned_missing_key = False


def _classify_medical_place(name: str, google_type: str) -> Optional[str]:
    name_lower = name.lower()
    if any(kw in name_lower for kw in MEDICAL_EXCLUDE_KEYWORDS):
        return None
    if google_type == "hospital":
        return "Hospital"
    if google_type == "physiotherapist":
        return "Rehabilitation"
    if "urgent care" in name_lower:
        return "Urgent Care"
    if any(kw in name_lower for kw in ["rehab", "physical therapy", "physiotherapy"]):
        return "Rehabilitation"
    if any(kw in name_lower for kw in ["family medicine", "family practice", "family health", "primary care", "internal medicine", "pediatric"]):
        return "Primary Care"
    return "Medical Clinic"


def _miles_to_meters(miles: float) -> float:
    return miles * 1609.34


def _meters_to_miles(meters: float) -> float:
    return meters / 1609.34


def _parse_duration_seconds(duration: Optional[str]) -> float:
    if not duration:
        return float("inf")
    try:
        return float(duration.rstrip("s"))
    except ValueError:
        return float("inf")


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _place_to_nearby(place: dict, origin_lat: float, origin_lng: float) -> Optional[NearbyPlace]:
    name = (place.get("displayName") or {}).get("text")
    if not name:
        return None
    location = place.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    distance = _haversine_miles(origin_lat, origin_lng, lat, lng) if lat is not None and lng is not None else None
    return NearbyPlace(name=name, address=place.get("formattedAddress"), distance_miles=round(distance, 1) if distance is not None else None)


async def _search_nearby(
    client: httpx.AsyncClient, api_key: str, lat: float, lng: float, included_type: str,
    radius_miles: float = DEFAULT_SEARCH_RADIUS_MILES, max_result_count: int = MAX_RESULTS_PER_CATEGORY,
    field_mask: str = FIELD_MASK,
) -> list[dict]:
    """Each amenity type gets its own dedicated call. A single combined multi-type call is cheaper in theory,
    but Places API's results share one 20-result cap - whichever type happens to be unusually dense nearby
    (schools in some areas, hospital-affiliated clinics in others) crowds out the rest. Proven out empirically,
    not just theoretical - so correctness wins here over trimming a few requests on a listing cached 180 days."""
    response = await client.post(
        PLACES_NEARBY_URL,
        json={
            "includedTypes": [included_type],
            "maxResultCount": max_result_count,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": _miles_to_meters(radius_miles)}},
        },
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": field_mask},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json().get("places", [])


async def _search_nearby_types(
    client: httpx.AsyncClient, api_key: str, lat: float, lng: float, included_types: list[str], max_result_count: int,
    radius_miles: float = DEFAULT_SEARCH_RADIUS_MILES, field_mask: str = TYPED_FIELD_MASK,
) -> list[dict]:
    """Like _search_nearby, but for cases where combining multiple types into one call is actually
    correct because we want them merged into a single list anyway, not kept as separate categories -
    e.g. doctor + physiotherapist into one Medical Care list, or locality/sublocality/neighborhood into
    one "nearby named places" list. No starvation risk to worry about, since there's only ever one list."""
    response = await client.post(
        PLACES_NEARBY_URL,
        json={
            "includedTypes": included_types,
            "maxResultCount": max_result_count,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": _miles_to_meters(radius_miles)}},
        },
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": field_mask},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json().get("places", [])


async def _search_text(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, text_query: str, radius_miles: float) -> list[dict]:
    response = await client.post(
        PLACES_TEXT_URL,
        json={
            "textQuery": text_query,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": _miles_to_meters(radius_miles)}},
        },
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json().get("places", [])


def _to_sorted_places(raw_places: list[dict], lat: float, lng: float) -> list[NearbyPlace]:
    places = [p for p in (_place_to_nearby(raw, lat, lng) for raw in raw_places) if p is not None]
    places.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    return places[:MAX_RESULTS_PER_CATEGORY]


async def _fetch_medical_care(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Hospital gets its own dedicated call (real hospitals are rare - a shared pool with the much more
    common "doctor" type would risk losing them entirely). Doctor + physiotherapist are then merged
    into the same list since we want one combined, classified, prioritized "Medical Care" section,
    not separate categories - so combining those two types carries no starvation risk."""
    hospital_raw = await _search_nearby(client, api_key, lat, lng, "hospital", max_result_count=NEARBY_CANDIDATE_POOL)
    doctor_raw = await _search_nearby_types(client, api_key, lat, lng, ["doctor", "physiotherapist"], NEARBY_CANDIDATE_POOL)

    candidates: list[NearbyPlace] = []
    for place, google_type in [(p, "hospital") for p in hospital_raw] + [(p, "physiotherapist" if "physiotherapist" in p.get("types", []) else "doctor") for p in doctor_raw]:
        name = (place.get("displayName") or {}).get("text")
        if not name:
            continue
        place_type = _classify_medical_place(name, google_type)
        if place_type is None:
            continue
        nearby = _place_to_nearby(place, lat, lng)
        if nearby is None:
            continue
        nearby.place_type = place_type
        candidates.append(nearby)

    candidates.sort(key=lambda p: (MEDICAL_PRIORITY.get(p.place_type, 3), p.distance_miles if p.distance_miles is not None else float("inf")))
    return candidates[:MAX_RESULTS_PER_CATEGORY]


async def _rank_airports_by_driving_distance(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, candidates: list[dict]) -> list[dict]:
    """Straight-line distance regularly picks the wrong airport - a small field can be closer as the
    crow flies while the real major airport sits a similar or shorter drive away right off a highway.
    Ranks the curated candidate list by actual driving time via the Routes API instead. Best-effort: if
    the call fails (e.g. Routes API not yet enabled on this key), falls back to the straight-line order
    `candidates` already arrives in rather than dropping airports from the sheet."""
    if len(candidates) <= 1:
        return candidates
    try:
        response = await client.post(
            ROUTES_MATRIX_URL,
            json={
                "origins": [{"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}],
                "destinations": [
                    {"waypoint": {"location": {"latLng": {"latitude": c["lat"], "longitude": c["lon"]}}}}
                    for c in candidates
                ],
                "travelMode": "DRIVE",
            },
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": ROUTES_FIELD_MASK},
            timeout=10.0,
        )
        response.raise_for_status()
        rows = response.json()

        duration_by_index: dict[int, float] = {}
        distance_by_index: dict[int, float] = {}
        for row in rows:
            if row.get("condition") != "ROUTE_EXISTS":
                continue
            idx = row.get("destinationIndex", 0)
            duration_by_index[idx] = _parse_duration_seconds(row.get("duration"))
            if row.get("distanceMeters") is not None:
                distance_by_index[idx] = row["distanceMeters"]

        if not duration_by_index:
            return candidates

        ranked_indices = sorted(range(len(candidates)), key=lambda i: duration_by_index.get(i, float("inf")))
        ranked = []
        for i in ranked_indices:
            airport = dict(candidates[i])
            if i in distance_by_index:
                airport["_driving_miles"] = round(_meters_to_miles(distance_by_index[i]), 1)
            ranked.append(airport)
        return ranked
    except Exception as e:
        logger.warning(f"Driving-distance ranking for airports failed, falling back to straight-line: {e}")
        return candidates


async def _fetch_airports(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Sourced entirely from the curated OurAirports dataset (see AIRPORT_DATA_PATH) rather than a live
    Places search - see that constant's comment for why. Takes the nearest candidates by straight-line
    distance, re-ranks them by real driving time, and shows the nearest valid one even if it's farther
    than the closest thing that merely looks like an airport."""
    airports = _load_major_airports()
    nearest = sorted(airports, key=lambda a: _haversine_miles(lat, lng, a["lat"], a["lon"]))[:AIRPORT_CANDIDATE_POOL]
    ranked = await _rank_airports_by_driving_distance(client, api_key, lat, lng, nearest)

    results: list[NearbyPlace] = []
    for airport in ranked[:MAX_RESULTS_PER_CATEGORY]:
        distance_miles = airport.get("_driving_miles")
        if distance_miles is None:
            distance_miles = round(_haversine_miles(lat, lng, airport["lat"], airport["lon"]), 1)
        address = ", ".join(part for part in [airport.get("municipality"), airport.get("region")] if part) or None
        results.append(NearbyPlace(name=airport["name"], address=address, distance_miles=distance_miles))
    return results


async def _fetch_assigned_schools(district_name: Optional[str], property: Property, db) -> list[NearbyPlace]:
    """Deliberately makes no Google call and no geography-based guess of any kind. A district having
    more than one real school at a level (Lower Merion's 5 elementary schools, its 2 high schools) means
    there is no reliable way to tell which one a specific address is zoned for without real attendance-
    boundary data, which we don't have - "nearest school" is frequently the wrong answer, which is exactly
    the mistake this feature exists to avoid. The only thing ever filled in here is a level where every
    other listing we have on file for this exact district agrees on one real school - i.e. there was never
    a zoning question to get wrong for that level in the first place."""
    if not district_name:
        return []

    levels = [
        ("Elementary School", property.elementary_school),
        ("Middle School", property.middle_or_junior_school),
        ("High School", property.high_school),
    ]
    known_by_level = await _known_district_schools(db, district_name)

    results: list[NearbyPlace] = []
    for label, mls_value in levels:
        if mls_value:
            continue  # MLS already names this one directly
        dominant = _dominant_school_name(known_by_level.get(label, Counter()))
        if dominant:
            results.append(NearbyPlace(name=dominant, place_type=label))
    return results


async def _has_commercial_core(client: httpx.AsyncClient, api_key: str, name: str, lat: float, lng: float) -> bool:
    """"locality" covers everything from a real downtown (Wayne, Berwyn) to a small hamlet, historic
    estate, or purely residential named place (Ithan, Allgates) with no actual town center - Places has
    no type or flag distinguishing them. First proxy: does this locality's own point have a real cluster
    of restaurants immediately around it? A real walkable town center always does; a residential enclave
    or estate essentially never does. That alone isn't sufficient though - a small named subdivision that
    happens to sit right next to a neighboring town's real downtown (e.g. "Larchmont Meadows" bordering
    Newtown Square's actual commercial strip) will also show a dense restaurant cluster nearby, without
    having any commercial core of its own. So also require that at least one of those restaurants is
    actually addressed in this place, not merely physically close to a differently-named neighbor's
    downtown. Fails closed (excludes the candidate) on any API error or address mismatch, since showing a
    wrong "downtown" is worse than showing one fewer."""
    try:
        raw = await _search_nearby(
            client, api_key, lat, lng, "restaurant",
            radius_miles=DOWNTOWN_VALIDATION_RADIUS_MILES, max_result_count=DOWNTOWN_MIN_NEARBY_BUSINESSES + 2,
        )
    except Exception as e:
        logger.warning(f"Downtown commercial-core check failed, excluding candidate to be safe: {e}")
        return False
    if len(raw) < DOWNTOWN_MIN_NEARBY_BUSINESSES:
        return False
    name_lower = name.lower()
    return any(name_lower in (place.get("formattedAddress") or "").lower() for place in raw)


async def _fetch_downtown_areas(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Text-searching the word "downtown" matches literal business names containing it (a short-term
    rental listing titled "Beautiful Convenient Studio Downtown Devon" was outranking the actual town of
    Devon) rather than real places. "locality" is a proper Places type for named towns/villages/boroughs -
    verified live that it's the type Google actually uses for small unincorporated Main Line villages
    (Wayne, Berwyn, Devon, Strafford all came back as "locality") - but it's also what Google uses for
    small named residential/historic places that have no real downtown at all (Ithan, Allgates), so each
    candidate is additionally checked for a real commercial core (see _has_commercial_core) before being
    shown - filling every result slot is not the goal, accuracy is. Note: "sublocality"/"neighborhood" are
    NOT valid includedTypes for Nearby Search (confirmed live - Google 400s on them, "Unsupported types"),
    so don't add them here. Google also returns duplicate entries for the same locality name (verified
    live) - dedupe by name. Names are shown as Google returns them - no "Downtown " prefix is invented,
    since that mislabels non-downtown localities that still pass the commercial-core check as loosely
    "walkable town center" adjacent (e.g. a real but small named village)."""
    raw = await _search_nearby(client, api_key, lat, lng, "locality", radius_miles=DOWNTOWN_MAX_DISTANCE_MILES, max_result_count=DOWNTOWN_CANDIDATE_POOL, field_mask=FIELD_MASK)

    seen: set[str] = set()
    deduped = []
    for place in raw:
        name = (place.get("displayName") or {}).get("text")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        deduped.append(place)

    def _distance(place: dict) -> float:
        location = place.get("location") or {}
        place_lat, place_lng = location.get("latitude"), location.get("longitude")
        if place_lat is None or place_lng is None:
            return float("inf")
        return _haversine_miles(lat, lng, place_lat, place_lng)

    deduped.sort(key=_distance)

    results: list[NearbyPlace] = []
    for place in deduped:
        if len(results) >= MAX_RESULTS_PER_CATEGORY:
            break
        location = place.get("location") or {}
        place_lat, place_lng = location.get("latitude"), location.get("longitude")
        if place_lat is None or place_lng is None:
            continue
        name = (place.get("displayName") or {}).get("text")
        if not name or not await _has_commercial_core(client, api_key, name, place_lat, place_lng):
            continue
        nearby = _place_to_nearby(place, lat, lng)
        if nearby:
            results.append(nearby)
    return results


async def _fetch_live(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, property: Property, db) -> NearbyAmenitiesResponse:
    schools = await _fetch_assigned_schools(property.school_district_name, property, db)
    grocery_stores = _to_sorted_places(await _search_nearby(client, api_key, lat, lng, "supermarket", max_result_count=NEARBY_CANDIDATE_POOL), lat, lng)
    medical_care = await _fetch_medical_care(client, api_key, lat, lng)
    parks = _to_sorted_places(await _search_nearby(client, api_key, lat, lng, "park", max_result_count=NEARBY_CANDIDATE_POOL), lat, lng)
    airports = await _fetch_airports(client, api_key, lat, lng)
    downtown_areas = await _fetch_downtown_areas(client, api_key, lat, lng)

    # No dedicated Places type distinguishes private schools from public ones - this is a best-effort
    # text search, not guaranteed-accurate categorization.
    private_schools = _to_sorted_places(await _search_text(client, api_key, lat, lng, "private schools", DEFAULT_SEARCH_RADIUS_MILES), lat, lng)

    return NearbyAmenitiesResponse(
        schools=schools,
        private_schools=private_schools,
        grocery_stores=grocery_stores,
        medical_care=medical_care,
        parks=parks,
        downtown_areas=downtown_areas,
        airports=airports,
        available=True,
        cached_at=datetime.now(timezone.utc),
    )


def _cached_response(property: Property) -> Optional[NearbyAmenitiesResponse]:
    if not property.nearby_amenities:
        return None
    if property.nearby_amenities.get("_schema_version") != AMENITIES_SCHEMA_VERSION:
        return None  # written under old field names/logic - treat as a miss rather than serve stale shape
    try:
        return NearbyAmenitiesResponse(**property.nearby_amenities, cached_at=property.nearby_amenities_cached_at, available=True)
    except Exception:
        return None


async def get_nearby_amenities(db, property: Property, force_refresh: bool = False) -> NearbyAmenitiesResponse:
    """Never raises - on any failure (missing key, network error, bad response), falls back to a
    stale cache if one exists, else returns available=False so the PDF just omits the section."""
    global _warned_missing_key

    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        if not _warned_missing_key:
            logger.warning("GOOGLE_PLACES_API_KEY not set - nearby amenities will be unavailable on all Cheat Sheets.")
            _warned_missing_key = True
        return _cached_response(property) or NearbyAmenitiesResponse(available=False)

    if property.latitude is None or property.longitude is None:
        return _cached_response(property) or NearbyAmenitiesResponse(available=False)

    if not force_refresh:
        cached = _cached_response(property)
        if cached and property.nearby_amenities_cached_at:
            cached_at = property.nearby_amenities_cached_at
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - cached_at < timedelta(days=CACHE_TTL_DAYS):
                return cached

    try:
        async with httpx.AsyncClient() as client:
            result = await _fetch_live(client, api_key, property.latitude, property.longitude, property, db)
    except Exception as e:
        logger.warning(f"Nearby amenities lookup failed for listing {property.listing_key}: {e}")
        return _cached_response(property) or NearbyAmenitiesResponse(available=False)

    try:
        data = result.model_dump(mode="json", exclude={"available", "cached_at"})
        data["_schema_version"] = AMENITIES_SCHEMA_VERSION
        property.nearby_amenities = data
        property.nearby_amenities_cached_at = result.cached_at
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist nearby amenities cache for listing {property.listing_key}: {e}")
        await db.rollback()

    return result
