import contextvars
import json
import math
import os
import re
import asyncio
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import City, ExploreNearbyCache, Property
from app.models.property import ExploreNearbyPlace, ExploreNearbyPlaceDetails, ExploreNearbyResponse, NearbyAmenitiesResponse, NearbyPlace
from app.services.ai.ai_config_service import ai_config_service
from app.services.nces_school_assignment_service import resolve_school_assignment
from app.services.usage_budget_service import BudgetExceeded, reserve as reserve_budget, reserve_bucket, usage_snapshot

logger = get_logger(__name__)

# --- Temporary live-cost instrumentation (for real-world cost verification testing) ---
# Not part of the Cheat Sheet's stored/displayed data - just logs what a real generation cost.
PLACES_SEARCH_COST_USD = 0.032  # Nearby Search Pro / Text Search Pro, per call
ROUTES_MATRIX_ELEMENT_COST_USD = 0.01  # Compute Route Matrix Pro is billed per element, not per call
_cost_log_var: "contextvars.ContextVar[Optional[list[tuple[str, float]]]]" = contextvars.ContextVar("_nearby_cost_log", default=None)


def _log_billable_call(section: str, cost_usd: float) -> None:
    log = _cost_log_var.get()
    if log is not None:
        log.append((section, cost_usd))
# --- end instrumentation ---

# Per-requester daily cost ceilings on top of the platform-wide places_search/places_details/
# platform_emergency buckets above - those protect total platform spend, but say nothing about
# how fast any ONE requester can burn through the shared daily allowance (a bot doing a million
# Explore Nearby calls in a row would otherwise exhaust the whole platform's daily budget for
# every real visitor on every agent's site in well under a minute). Each identity dimension gets
# its own independent daily bucket, all reserved atomically alongside the platform buckets - a
# request must clear every applicable one. Actual dollar values live in ai_config_service.py
# (ip_daily_cost_limit_usd / device_daily_cost_limit_usd / agent_site_daily_cost_limit_usd /
# agent_total_daily_cost_limit_usd), same DB-backed-and-tunable pattern as every other limit in
# this file - see _check_and_reserve_explore_budget_locked below.

CACHE_TTL_DAYS = 180  # Schools/hospitals/parks essentially never move - safe to cache far longer than a listing's lifetime.
# Bump this whenever _fetch_live's shape or logic meaningfully changes (renamed/added fields, new
# categories, different filtering) - otherwise a cache written under the old logic (e.g. the pre-rename
# "hospitals" field, or before the assigned-schools search existed) would silently keep being served as
# "fresh" for up to CACHE_TTL_DAYS, masking the new behavior entirely instead of just being a cache miss.
AMENITIES_SCHEMA_VERSION = 15
AMENITIES_COMPATIBLE_SCHEMA_VERSIONS = {15}
CACHE_FAILURE_TTL_HOURS = 12
MAX_RESULTS_PER_CATEGORY = 5
DOWNTOWN_MAX_DISTANCE_MILES = 10
DOWNTOWN_CANDIDATE_POOL = 15
DEFAULT_SEARCH_RADIUS_MILES = 8

PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places"
ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "places.id,places.name,places.displayName,places.formattedAddress,places.location"
TYPED_FIELD_MASK = FIELD_MASK + ",places.primaryType,places.primaryTypeDisplayName,places.types"
EXPLORE_NEARBY_FIELD_MASK = (
    "places.id,places.displayName,places.primaryType,places.primaryTypeDisplayName,places.types,"
    "places.shortFormattedAddress,places.location"
)
EXPLORE_NEARBY_DETAILS_FIELD_MASK = "id,rating,userRatingCount"
ROUTES_FIELD_MASK = "originIndex,destinationIndex,duration,distanceMeters,condition"
# Google's includedTypes filter on Nearby Search (New) is a soft ranking bias, not a hard filter - a
# request scoped to includedTypes=["supermarket","grocery_store"] still comes back with gas stations,
# convenience stores, and delis mixed in (confirmed empirically against live results at a real
# address), and includedTypes=["restaurant"] comes back with coffee shops and convenience stores too.
# EXPLORE_NEARBY_MAX_RESULTS requests Google's max raw candidate pool (20, the API cap) so there's
# still enough left after EXPLORE_NEARBY_EXCLUDED_TYPES below strips the off-category noise;
# EXPLORE_NEARBY_DISPLAY_LIMIT then caps what's actually shown to a clean, non-overwhelming list.
EXPLORE_NEARBY_MAX_RESULTS = 20
EXPLORE_NEARBY_DISPLAY_LIMIT = 6
EXPLORE_NEARBY_DEFAULT_RADIUS_MILES = 2.5
EXPLORE_NEARBY_SEARCH_ESTIMATED_COST_USD = 0.032
EXPLORE_NEARBY_DETAILS_ESTIMATED_COST_USD = 0.025  # Enterprise + Atmosphere SKU (rating/review fields), not Pro
# Cache-grid precision for Stage 1 (nearby search) cache keys, in decimal degrees. 2 decimal places is
# roughly a 0.7mi x 0.5mi cell at mid-US latitudes - small relative to these categories' 2-4mi search
# radii, so two nearby properties (e.g. the same subdivision) sharing a cell get virtually the same
# underlying "what businesses are out here" answer from one paid Google call instead of two. Safe only
# because distance_miles/origin are always recomputed fresh per request from the real coordinates (see
# _rehydrate_places_for_origin) rather than served verbatim from whichever property's search paid for
# the cache entry - so a shown distance or map pin is never wrong for the property actually being
# viewed. The one accepted tradeoff: a place right at the edge of the true search radius may
# occasionally be included/excluded based on which property's exact center originally ran the search,
# rather than the current viewer's exact center - a minor effect confined to the outer ~15-20% of the
# radius, acceptable for an informational "nearby amenities" feature.
EXPLORE_NEARBY_CACHE_GEO_PRECISION = 2
# 10/24 audit (user: "grocery = major grocery stores, not tiny family shops or convenience stores;
# hospital = major hospitals, not small practices; schools public AND private; restaurants = real
# restaurants, not Wawa / fast food / gas stations"): grocery searches supermarkets only and keeps
# the big chains + true supermarkets (_explore_grocery_ok), "medical" is now Hospitals (full-service
# hospitals only, same test as the Cheat Sheet's), a Schools tab finds public and private K-12
# schools, and restaurants drop fast food / takeout counters / chains (_explore_restaurant_ok).
# Bump EXPLORE_NEARBY_CACHE_VERSION with any change here, so old cached lists aren't served.
EXPLORE_NEARBY_CACHE_VERSION = 3
EXPLORE_NEARBY_CATEGORIES: dict[str, dict[str, Any]] = {
    "restaurants": {"label": "Restaurants", "types": ["restaurant"], "radius_miles": 2.5},
    "coffee": {"label": "Coffee", "types": ["cafe", "coffee_shop"], "radius_miles": 2.0},
    "grocery": {"label": "Grocery", "types": ["supermarket", "grocery_store"], "radius_miles": 5.0},
    "schools": {"label": "School", "types": ["primary_school", "secondary_school", "school"], "radius_miles": 3.0},
    "parks": {"label": "Parks", "types": ["park"], "radius_miles": 3.0},
    "shopping": {"label": "Shopping", "types": ["shopping_mall", "department_store", "clothing_store"], "radius_miles": 3.0},
    "medical": {"label": "Hospital", "types": ["hospital", "general_hospital"], "radius_miles": 12.0},
}
# The big grocery names people mean by "the grocery store" (East Coast first, then national).
MAJOR_GROCERY_BRANDS = (
    "acme", "giant", "wegmans", "whole foods", "trader joe", "shoprite", "shop rite", "weis", "aldi",
    "lidl", "safeway", "stop & shop", "stop and shop", "food lion", "harris teeter", "redner",
    "fresh grocer", "sprouts", "the fresh market", "fresh market", "kroger", "publix", "costco",
    "sam's club", "bj's wholesale", "target", "walmart supercenter", "hannaford", "price chopper",
    "market basket", "mom's organic", "earth fare", "h mart", "h-mart", "save a lot", "save-a-lot",
    "grocery outlet", "landis", "kimberton whole foods", "genuardi",
)
# Fast food / counter-service names that aren't a sit-down "restaurant" to a home buyer.
FAST_FOOD_NAME_PATTERNS = (
    "mcdonald", "burger king", "wendy's", "wendys", "taco bell", "kfc", "kentucky fried", "popeyes",
    "chick-fil-a", "chick fil a", "arby's", "arbys", "subway", "jersey mike", "jimmy john", "sonic drive",
    "dairy queen", "hardee", "carl's jr", "jack in the box", "white castle", "checkers", "rally's",
    "five guys", "shake shack", "chipotle", "qdoba", "moe's southwest", "firehouse subs", "potbelly",
    "domino's", "dominos", "pizza hut", "papa john", "little caesars", "wingstop", "panda express",
    "boston market", "auntie anne", "dunkin", "starbucks", "tim hortons", "krispy kreme", "baskin",
    "cold stone", "rita's", "ritas italian ice", "jamba", "smoothie king", "tropical smoothie",
    "panera", "cosi", "saladworks", "chopt", "sweetgreen", "steak 'n shake", "steak n shake",
    "raising cane", "zaxby", "bojangles", "church's chicken", "long john silver", "del taco",
    "el pollo loco", "whataburger", "in-n-out", "culver's", "jet's pizza", "marco's pizza",
)
FAST_FOOD_PRIMARY_TYPES = {"fast_food_restaurant", "meal_takeaway", "food_court", "cafeteria", "sandwich_shop",
                           "hamburger_restaurant", "dessert_shop", "ice_cream_shop", "juice_shop", "snack_bar"}
SCHOOL_PRIMARY_TYPES = {"primary_school", "secondary_school", "school"}
# Denylist per category for the off-category noise Google's soft includedTypes match lets through -
# see the comment on EXPLORE_NEARBY_MAX_RESULTS above. Grocery uses the existing, more thorough
# _is_full_service_grocery predicate instead (below) rather than a simple denylist, since "real grocery
# store vs. gas-station mini-mart vs. specialty market" already needed that finer-grained logic
# elsewhere in this file. Restaurants uses RESTAURANT_PRIMARY_TYPES (an allowlist) instead of a
# denylist - see its comment. Parks and medical showed no contamination in testing, so no denylist
# needed.
EXPLORE_NEARBY_EXCLUDED_TYPES: dict[str, set[str]] = {
    "coffee": {"fast_food_restaurant", "restaurant", "convenience_store", "gas_station"},
    "shopping": {"drugstore", "pharmacy", "gas_station", "convenience_store"},
}
# A denylist can never keep up with every non-restaurant identity Google might tag a place with (a
# denylist missed nothing here that a direct test caught, but the next mislabeled chain would slip
# through silently). Restaurants instead requires a genuine food-service primaryType from Google's own
# Nearby Search (New) type taxonomy - deliberately excludes cafe/coffee_shop (their own tab) and
# meal_delivery (no storefront to actually visit).
RESTAURANT_PRIMARY_TYPES = {
    "restaurant", "fast_food_restaurant", "fine_dining_restaurant", "diner", "bar_and_grill", "pub",
    "steak_house", "buffet_restaurant", "food_court", "sandwich_shop", "meal_takeaway", "cafeteria",
    "breakfast_restaurant", "brunch_restaurant", "dessert_restaurant", "seafood_restaurant",
    "pizza_restaurant", "hamburger_restaurant", "barbecue_restaurant", "american_restaurant",
    "italian_restaurant", "mexican_restaurant", "chinese_restaurant", "japanese_restaurant",
    "sushi_restaurant", "thai_restaurant", "indian_restaurant", "korean_restaurant",
    "vietnamese_restaurant", "greek_restaurant", "mediterranean_restaurant", "middle_eastern_restaurant",
    "french_restaurant", "spanish_restaurant", "turkish_restaurant", "lebanese_restaurant",
    "african_restaurant", "afghani_restaurant", "asian_restaurant", "brazilian_restaurant",
    "indonesian_restaurant", "ramen_restaurant", "vegan_restaurant", "vegetarian_restaurant", "wine_bar",
}
# Even a correct-looking primaryType isn't proof of a genuine restaurant identity - live testing showed
# Wawa tagged primaryType=fast_food_restaurant despite being a gas-station/convenience hybrid. Two extra
# signals catch that: EXPLORE_NEARBY_HYBRID_EXCLUDE_TYPES rejects anything Google also tags with a
# gas_station/convenience_store secondary type (not a normal secondary tag on a genuine restaurant/
# cafe/shop, unlike broad parent categories such as "restaurant" itself - see coffee's history above -
# so this is safe to apply across every category, not just restaurants), and
# CONVENIENCE_GAS_HYBRID_NAME_PATTERNS is a brand-name safety net for the well-known chains, in case a
# specific location's Google listing omits the secondary type entirely.
EXPLORE_NEARBY_HYBRID_EXCLUDE_TYPES = {"gas_station", "convenience_store"}
CONVENIENCE_GAS_HYBRID_NAME_PATTERNS = (
    "wawa", "sheetz", "7-eleven", "7 eleven", "circle k", "quiktrip", "quik trip", "speedway",
    "casey's", "caseys", "royal farms", "turkey hill", "cumberland farms", "kwik trip", "racetrac",
    "race trac", "pilot travel", "flying j", "love's travel", "loves travel", "getgo", "rutter's",
    "rutters", "quickchek", "kum & go", "kum and go", "maverik", "stripes", "holiday stationstore",
)


class ExploreNearbyBudgetExceeded(Exception):
    pass


class ExploreNearbyLookupUnavailable(Exception):
    pass


_explore_nearby_lock = asyncio.Lock()
_explore_nearby_inflight: dict[str, asyncio.Task[ExploreNearbyResponse]] = {}
_explore_nearby_details_inflight: dict[str, asyncio.Task[ExploreNearbyPlaceDetails]] = {}

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

GROCERY_EXCLUDED_NAME_PATTERNS = (
    "restaurant", "grill", "cafe", "coffee", "pizza", "deli", "bakery", "bagel", "kitchen",
    "convenience", "corner store", "gas", "gas station", "bp", "exxon", "shell", "sunoco",
    "wawa", "7-eleven", "7 eleven", "quick stop", "mini mart", "minimart", "liquor",
    "wine", "beer", "tobacco", "smoke", "pharmacy", "cvs", "walgreens", "dollar general",
    "dollar tree", "family dollar", "nutrition", "supplement", "catering", "candy",
    "chocolate", "ice cream",
    # "Commissary" is used exclusively by the DoD's Defense Commissary Agency on-base grocery
    # system (confirmed via research - no civilian grocery chain uses the word), so this is a
    # general restricted-access rule, not a Dover-specific one. A real Google result, "Dover
    # Commissary" (268 Galaxy St, Dover AFB), was winning a grocery slot despite requiring military
    # affiliation or an accompanied guest to shop there - not accessible to a typical home buyer.
    "commissary",
)
GROCERY_EXCLUDED_TYPES = {
    "restaurant", "cafe", "bakery", "meal_takeaway", "meal_delivery", "convenience_store",
    "gas_station", "liquor_store", "drugstore", "pharmacy",
}
GROCERY_STRONG_PRIMARY_TYPES = {"supermarket", "grocery_store"}
GROCERY_SECONDARY_TYPES = {"food_store", "market"}
GROCERY_FULL_SERVICE_NAME_HINTS = (
    "market", "supermarket", "grocery", "food market", "foods", "fresh grocer", "shoprite",
    "giant", "acme", "whole foods", "trader joe", "wegmans", "aldi", "lidl", "sprouts",
    "costco", "sam's club", "bj's",
)
# Specialty markets Google sometimes tags with a grocery-ish primary type (grocery_store/food_store)
# even though they're a single-category shop, not a full-line grocery store. These can still fill the
# second grocery slot via _is_full_service_grocery, but must never crowd out a true grocery
# store/supermarket that's reasonably as close - see _is_true_grocery_or_supermarket.
GROCERY_SPECIALTY_NAME_HINTS = (
    "farm stand", "farm market", "farmers market", "farmer's market", "farmers' market",
    "produce market", "produce stand", "butcher", "meat market", "meats", "dairy", "creamery",
    "cheese shop", "cheese market", "fish market", "fishmonger", "seafood market", "seafood",
)


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


# Medical Care intentionally shows only two high-confidence buyer-useful categories:
# one true hospital and one useful non-hospital provider. primaryType is the first signal; name
# keywords only reject obvious false positives or refine the non-hospital label.
MEDICAL_EXCLUDE_NAME_KEYWORDS = [
    "outpatient", "therapy", "therapies", "physical therapy", "physiotherapy", "rehab",
    "rehabilitation", "imaging", "radiology", "mri", "x-ray", "ultrasound", "diagnostic",
    "lab", "laboratory", "labs", "dentist", "dental", "orthodont", "pharmacy", "cvs",
    "walgreens", "rite aid", "surgery center", "surgical center", "endoscopy", "ambulatory",
    "cosmetic", "chiropract", "wellness", "regenerative", "med spa", "medspa", "aesthetic",
    "botox", "laser hair", "acupuncture", "massage", "hormone", "iv therapy", "iv hydration",
    "weight loss", "day spa", "medi spa", "behavioral health", "mental health", "counseling",
    "dermatology", "orthopedics", "cardiology", "oncology", "obgyn", "ob/gyn", "eye care",
    "dialysis", "sleep center", "wound care", "occupational health",
]
HOSPITAL_FALSE_POSITIVE_NAME_KEYWORDS = [
    *MEDICAL_EXCLUDE_NAME_KEYWORDS,
    "urgent care", "express care", "walk-in", "walk in", "family practice", "family medicine",
    "primary care", "pediatrics", "medical associates", "medical group", "specialty",
    "specialists", "specialist",
    # "Institute"-branded facilities Google types as primaryType=hospital are consistently
    # specialty/research/behavioral-health operations, never a general full-service hospital in
    # testing - e.g. a real Google Places "hospital" result named just "Argo institute" turned out
    # (confirmed via web search) to be Aquila Behavioral Health's substance-use treatment center,
    # with no lexical hint of that in the name Google actually returns. None of the genuine
    # hospitals found in testing (Bryn Mawr Hospital, Hospital of the University of Pennsylvania,
    # Thomas Jefferson University Hospital, Lankenau Medical Center, etc.) use "Institute" in their
    # Google-displayed name, so this exclusion has no observed false-negative cost.
    "institute", "medical office building",
    # A real Google Places "hospital" result named "Bayhealth Purchasing Department, Dover" showed
    # up in the same candidate pool as the actual Bayhealth Hospital, Kent Campus - an
    # administrative/supply office, not anywhere a buyer could receive care.
    "purchasing department", "administration", "administrative offices", "corporate office",
    "human resources", "billing department", "records department",
    # A real Google Places result, "Union Hospital Cecil County Pulmn" (primaryType=hospital,
    # ends in a truncated "Pulmonary"/"Pulmonology" department suffix), was 0.18mi closer than the
    # actual hospital campus ("ChristianaCare Union Hospital") and won purely on that margin -
    # it has none of the other red-flag keywords/types above, so needed its own entry. Kept
    # narrow and hospital-only (deliberately not reusing SPECIALIST_MEDICAL_NAME_KEYWORDS here -
    # that list includes "surgery"/"surgical", which appear in real, correct hospital-campus
    # listings, e.g. "Bayhealth General Surgery, Sussex Campus").
    "pulmonary", "pulmonology", "pulmn", "nephrology", "psychiatry", "pathology", "hematology",
    "audiology", "specialty associates", "interventional",
]
# A Cheat Sheet medical result must represent a named patient-care facility - not an individual
# provider and not a bare street address that Google failed to attach a real business name to.
# Both patterns matched real bad results found in production data: "Jessica A. Quirin, CRNP" (an
# individual nurse practitioner's solo listing, primaryType=medical_clinic, surfaced by the
# "urgent care" text search even though her name has nothing to do with urgent care) and
# "93 Church St Frederica, DE" (primaryType=hospital, Google literally returned an address as the
# name - the actual nearest real hospital, Bayhealth Hospital Kent Campus, was ~3mi farther away
# and lost out purely because the address-named ghost listing was closer).
_INDIVIDUAL_PROVIDER_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]*\.?){1,3},\s*"
    r"(CRNP|PA-C|PA|NP|FNP|DNP|APRN|CNM|DO|DDS|DMD|OD|DPM|PharmD|RN|LPN)\.?\s*$"
)  # "MD" deliberately excluded from the credential list - it collides with the Maryland state
# abbreviation, which shows up legitimately at the end of real facility names/addresses.
_STREET_SUFFIX_PATTERN = (
    r"(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Dr(?:ive)?|Ln|Lane|Blvd|Boulevard|Way|Pl(?:ace)?|"
    r"Ct|Court|Pike|Hwy|Highway|Cir(?:cle)?|Pkwy|Parkway)"
)
_BARE_STREET_ADDRESS_NAME_RE = re.compile(
    rf"^\d+[\d\-]*\s+[\w\s]*\b{_STREET_SUFFIX_PATTERN}\b.*,\s*[A-Z]{{2}}$", re.IGNORECASE
)
# Every real hospital found in testing says so explicitly - "Hospital", "Medical Center"/"Medical
# Centre", or "Campus" (a named site of a larger system, e.g. "Bayhealth Hospital, Kent Campus").
# Deliberately narrower than a generic "health"/"clinic"/"center" word list: a bare health-system
# brand name with none of these (e.g. a real Google result named just "Bayhealth", 665 S Bay Rd -
# a different address than the actual "Bayhealth Hospital, Kent Campus" building, 640 S State St)
# is exactly the ambiguous non-hospital-building listing this is meant to catch, alongside clearer
# ghost/administrative listings like "Myblood" (a blood-donation service) or a bare "Delaware".
HOSPITAL_FACILITY_IDENTITY_KEYWORDS = {
    "hospital", "hosp", "medical center", "medical centre", "campus",
}


def _is_individual_provider_name(name: str) -> bool:
    return bool(_INDIVIDUAL_PROVIDER_NAME_RE.match(name.strip()))


def _is_bare_street_address_name(name: str) -> bool:
    return bool(_BARE_STREET_ADDRESS_NAME_RE.match(name.strip()))


NON_HOSPITAL_MEDICAL_EXCLUDE_NAME_KEYWORDS = [
    "outpatient", "therapy", "therapies", "physical therapy", "physiotherapy", "rehab",
    "rehabilitation", "imaging", "radiology", "mri", "x-ray", "ultrasound", "diagnostic",
    "lab", "laboratory", "labs", "dentist", "dental", "orthodont", "pharmacy", "cvs",
    "walgreens", "rite aid", "surgery center", "surgical center", "endoscopy", "ambulatory",
    "cosmetic", "chiropract", "wellness", "regenerative", "med spa", "medspa", "aesthetic",
    "botox", "laser hair", "acupuncture", "massage", "hormone", "iv therapy", "iv hydration",
    "weight loss", "day spa", "medi spa", "behavioral health", "mental health", "counseling",
    "dermatology", "orthopedics", "cardiology", "oncology", "obgyn", "ob/gyn", "eye care",
    "dialysis", "sleep center", "wound care", "occupational health",
]
SPECIALIST_MEDICAL_NAME_KEYWORDS = [
    "endocrine", "endocrinology", "cardiology", "orthopedic", "orthopedics", "dermatology",
    "pediatric", "pediatrics", "radiology", "imaging", "surgery", "surgical", "oncology",
    "cancer",  # e.g. "Ann B. Barshinger Cancer Institute" - an oncology facility that doesn't
    # literally say "oncology", found outranking closer general facilities in testing.
    "neurology", "gastroenterology", "urology", "ophthalmology", "eye", "ent", "allergy",
    "pain", "spine", "sports medicine", "women's health", "obgyn", "ob/gyn",
    # British/medical spelling variants of specialties already above - "orthopedic" is not a
    # substring of "orthopaedics", so a real result ("Rothman Orthopaedics") slipped past the
    # specialist demotion entirely under the plain-spelling keywords, found in real testing once
    # proximity-weighted ranking (below) removed the label-priority mask that had been hiding it.
    "orthopaedic", "orthopaedics", "paediatric", "paediatrics", "gynaecology", "anaesthesia",
    "occupational medicine",  # distinct phrase from "occupational health" (already fully excluded
    # above) - a real result, "CNS Occupational Medicine of Delaware," was winning a general-care
    # slot in a sparse rural market despite treating workplace injuries, not general patient care.
    # Known vision/eyewear retail brands and generic optical wording - "eye"/"ent" above are word-
    # boundary matched (see _matches_specialist_keyword) specifically so they don't false-positive
    # on unrelated words like "center"/"Meyer", but that also means they can't catch a mashed-
    # together brand name like the real result "MyEyeDr." (no space, so "\beye\b" never matches).
    "myeyedr", "eyecare", "eye care", "vision center", "visionworks", "lenscrafters",
    "pearle vision", "eyeglass world", "stanton optical", "target optical", "walmart vision",
    "optometry", "optical", "eye associates", "eye institute",
    "specialty care",  # e.g. real result "Nemours Children's Health, Deptford - Specialty Care" -
    # explicitly self-labeled as specialty, not general/primary care, found outranking a real
    # family medicine practice once proximity replaced label-priority ranking.
    "children's hospital", "childrens hospital", "hospital for children", "children's health",
    # A real result, "St. Christopher's Hospital for Children," has Google primaryType=medical_clinic
    # (not hospital) for this Place entry, so it never hits the hospital-only exclusion in
    # _non_hospital_medical_label - and "pediatric"/"pediatrics" above don't match its literal name
    # ("Children" isn't "pediatric"), so it competed in the base general-care tier at 1.3mi and won,
    # confirmed live at two independent properties. A pediatric/children's specialty hospital should
    # never fill the adult/family "General Medical" slot when a real general-care option exists.
]
URGENT_CARE_INCLUDE_KEYWORDS = [
    "urgent care", "walk-in urgent care", "walk in urgent care",
    # Bare "walk-in" wording (no literal "urgent care" phrase) is the same kind of episodic,
    # walk-without-appointment care - e.g. real results "Delaware Walk In Medical" and "Bayhealth
    # Walk-in Medical Care" were competing as ordinary general clinics instead of urgent-care-tier
    # once label-priority ranking was replaced with proximity weighting.
    "walk-in", "walk in",
]
URGENT_CARE_EXCLUDE_NAME_KEYWORDS = [
    kw for kw in MEDICAL_EXCLUDE_NAME_KEYWORDS
    if kw not in {"clinic"}  # some real urgent-care brands use "clinic"; require urgent-care wording instead.
]
MEDICAL_EXCLUDE_TYPES = {
    "pharmacy", "dentist", "physiotherapist", "drugstore",
}
URGENT_CARE_EXCLUDE_TYPES = MEDICAL_EXCLUDE_TYPES - {"doctor"}
HOSPITAL_PRIMARY_TYPES = {"general_hospital", "hospital"}
NON_HOSPITAL_MEDICAL_PRIMARY_TYPES = {"medical_clinic", "doctor", "medical_center"}
# Flattened into 2 tiers, not a strict per-label hierarchy: FAMILY MEDICINE/MEDICAL CLINIC/DOCTOR/
# MEDICAL CENTER are all equally "general patient care" and compete on distance alone within that
# tier (see _non_hospital_medical_rank's specialist-name penalty); URGENT CARE stays a strictly
# lower tier, unchanged from before. The old graded order (FAMILY MEDICINE > MEDICAL CLINIC >
# DOCTOR > MEDICAL CENTER) let a label-1 clinic beat a real, closer label-3 medical center
# regardless of how much farther away it was - a real Lancaster PA result had a 5.4mi Medical
# Clinic beating a 2.7mi Medical Center this way.
NON_HOSPITAL_MEDICAL_RANK = {
    "FAMILY MEDICINE": 0,
    "MEDICAL CLINIC": 0,
    "DOCTOR": 0,
    "MEDICAL CENTER": 0,
    "URGENT CARE": 1,
}

PRIVATE_SCHOOL_MAX_RESULTS = 2
PRIVATE_SCHOOL_EXCLUDE_KEYWORDS = [
    "public school", "public charter", "charter school", "school district", "isd", "school board",
    "preschool", "pre-school", "daycare", "day care", "child care", "childcare", "nursery",
    "early learning", "early childhood", "kindercare", "goddard school", "primrose school",
    "learning center", "learning centre", "tutoring", "tutor", "kumon", "mathnasium", "sylvan",
    "huntington learning", "driving school", "driver", "music school", "dance school", "ballet",
    "martial arts", "karate", "taekwondo", "trade school", "vocational", "technical institute",
    "career institute", "college", "university", "seminary", "adult education", "after school",
    "afterschool", "religious education", "sunday school", "hebrew school", "catechism",
]
PRIVATE_SCHOOL_SPECIALIZED_KEYWORDS = [
    "special education", "special needs", "therapeutic", "autism", "aba", "behavioral",
    "new story", "devereux", "pathway school", "center school", "learning disabilities",
]
PRIVATE_SCHOOL_POSITIVE_KEYWORDS = [
    "private school", "independent school", "academy", "school", "preparatory", "prep",
    "college preparatory", "catholic school", "christian school", "parochial", "friends school",
    "montessori", "waldorf", "episcopal", "lutheran", "st.", "saint", "independence school",
]
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

PARK_SUB_FEATURE_KEYWORDS = [
    "pavilion", "shelter", "picnic area", "picnic pavilion", "field", "fields", "ball field",
    "baseball", "softball", "soccer", "football", "lacrosse", "court", "courts", "basketball",
    "tennis", "pickleball", "playground", "tot lot", "trail", "trailhead", "boat ramp",
    "parking lot", "parking", "restroom", "bathroom", "concession", "amphitheater", "stage",
    "dog park", "disc golf",
    # A real Google result, "Ceres Park Back Lot" (primaryType=park), is an overflow parking area
    # for the real Ceres Park, not a public recreational destination itself.
    "back lot", "rear lot",
    # Private/members-only community amenities Google sometimes tags primaryType=park even though
    # a typical home buyer (unless they live in that specific community) cannot use them.
    "hoa park", "homeowners association", "homeowner's association", "community association",
    "residents only", "residents-only", "private community park", "clubhouse",
    # Switching to rankPreference=DISTANCE (see _fetch_parks) surfaces the full nearby pool instead of
    # only Google's most "popular" picks, which also surfaces sub-location entries for one underlying
    # park that popularity ranking used to hide - real results "Tacony Creek Park - Rorer St. Gateway",
    # "..., Bingham St. Gateway", "..., Whitaker Gateway" etc. are all entrances to the same park, not
    # separate destinations. Collapsed further by _dedupe_park_sub_locations below (name-prefix grouping)
    # in case a park has gateway/entrance Places with no bare parent-name entry to keep.
    "gateway", "entrance", "access point",
    # A state park/forest's own administrative buildings are not a public recreational destination -
    # a real result, "Elk Neck SF HQ Building" (Elk Neck State Forest headquarters), was winning a
    # parks slot under DISTANCE ranking.
    "headquarters", "administrative office", "administration building", "maintenance facility",
    "operations center", "ranger station", "public works",
]
_HQ_ABBREVIATION_RE = re.compile(r"\bhq\b", re.IGNORECASE)
# Residential-development/subdivision entities Google sometimes geocodes with primaryType=park even
# though they're not a recreational destination at all - real results at a single Delaware test location
# included "Mount Vernon Estates", "Stonegate Sub", "Burnwood Sub.", "Royal Grant", "Traybern" (all bare
# subdivision names, several with no street-level address at all, just a town/ZIP) sitting in the same
# DISTANCE-ranked pool as genuine small parks. Deliberately narrow and structural, not a guess from the
# name alone: only triggers when a common development-name suffix AND the place carries none of Google's
# richer park-descriptor types (a real established park nearly always has at least one of these; a bare
# geocoded subdivision entity never does) - so a legitimately named "X Acres Park" or "X Estates Nature
# Preserve" is unaffected, since it would carry a richer type or simply not end in the bare suffix word.
_SUBDIVISION_PARK_NAME_SUFFIXES = ("estates", "acres", "sub", "sub.", "manor", "farms", "gardens")
_PARK_RICH_DESCRIPTOR_TYPES = {
    "tourist_attraction", "natural_feature", "hiking_area", "campground", "national_park",
    "state_park", "nature_preserve", "wildlife_refuge", "garden", "botanical_garden",
    "historical_landmark", "plaza", "beach",
}


def _is_likely_subdivision_named_park(place: dict) -> bool:
    words = _place_name(place).lower().rstrip(".").split()
    if not words:
        return False
    if set(place.get("types") or []) & _PARK_RICH_DESCRIPTOR_TYPES:
        return False
    return words[-1] in {suffix.rstrip(".") for suffix in _SUBDIVISION_PARK_NAME_SUFFIXES}


def _private_school_level_label(name: str, google_types: list[str]) -> str:
    """Conservative school-level label. If the level is not clear from a grade-bearing Google type or
    explicit K-8/K-12/high-school wording, use PRIVATE SCHOOL instead of guessing."""
    name_lower = name.lower()
    if re.search(r"\bk\s*[-–]\s*12\b|\bkindergarten\s+through\s+12", name_lower):
        return "K-12"
    if re.search(r"\bk\s*[-–]\s*8\b|\bkindergarten\s+through\s+8", name_lower):
        return "K-8"
    if "secondary_school" in google_types or "high school" in name_lower:
        return "HIGH SCHOOL"
    if "middle school" in name_lower or "junior high" in name_lower:
        return "MIDDLE SCHOOL"
    if "primary_school" in google_types or "elementary school" in name_lower:
        return "ELEMENTARY"
    return "PRIVATE SCHOOL"


def _private_school_candidate(place: dict) -> Optional[tuple[int, str]]:
    """Returns (rank, label) for a useful nearby private K-12 candidate, or None if unreliable.

    Rank is usefulness/confidence first, distance second. Lower rank wins.
    """
    name = _place_name(place)
    name_lower = name.lower()
    types = place.get("types") or []
    if not name:
        return None
    if any(keyword in name_lower for keyword in PRIVATE_SCHOOL_EXCLUDE_KEYWORDS):
        return None
    if "public_school" in types or "university" in types:
        return None

    is_specialized = any(keyword in name_lower for keyword in PRIVATE_SCHOOL_SPECIALIZED_KEYWORDS)
    positive_score = sum(1 for keyword in PRIVATE_SCHOOL_POSITIVE_KEYWORDS if keyword in name_lower)
    has_school_type = any(t in types for t in ["school", "primary_school", "secondary_school"])
    if positive_score == 0 and not has_school_type:
        return None

    traditional_private_hints = [
        "private", "independent", "independence school", "academy", "preparatory", "prep", "catholic", "christian",
        "parochial", "friends", "montessori", "waldorf", "episcopal", "lutheran", "st.", "saint",
    ]
    has_private_hint = any(keyword in name_lower for keyword in traditional_private_hints)
    # A plain "school" with no private/parochial/independent hint is too likely to be public or unknown.
    if not has_private_hint and not is_specialized:
        return None

    label = "SPECIALIZED SCHOOL" if is_specialized else _private_school_level_label(name, types)
    rank = 2 if is_specialized else (0 if has_private_hint else 1)
    return rank, label


def _place_name(place: dict) -> str:
    return ((place.get("displayName") or {}).get("text") or "").strip()


def _place_primary_type(place: dict) -> str:
    primary_type = str(place.get("primaryType") or "").strip()
    if primary_type:
        return primary_type
    types = place.get("types") or []
    return str(types[0]).strip() if types else ""


def _place_type_label(place: dict) -> str:
    label = ((place.get("primaryTypeDisplayName") or {}).get("text") or "").strip()
    return label


def _place_identity(place: dict) -> str:
    return str(place.get("id") or place.get("name") or "").strip()


def _place_address_key(place: dict) -> str:
    return re.sub(r"\s+", " ", str(place.get("formattedAddress") or "").lower()).strip()


def _place_dedupe_key(place: dict) -> tuple[str, str, str]:
    return (_place_identity(place), _place_address_key(place), _place_name(place).lower())


def _is_parent_park_candidate(place: dict) -> bool:
    name = _place_name(place).lower()
    primary_type = _place_primary_type(place)
    if not name or primary_type != "park":
        return False
    if any(keyword in name for keyword in PARK_SUB_FEATURE_KEYWORDS):
        return False
    if _HQ_ABBREVIATION_RE.search(name):
        return False
    return not _is_likely_subdivision_named_park(place)


def _is_legitimate_urgent_care(place: dict) -> bool:
    name_lower = _place_name(place).lower()
    primary_type = _place_primary_type(place)
    types = set(place.get("types") or [])
    if not name_lower:
        return False
    if primary_type in MEDICAL_EXCLUDE_TYPES or types & URGENT_CARE_EXCLUDE_TYPES:
        return False
    if not any(keyword in name_lower for keyword in URGENT_CARE_INCLUDE_KEYWORDS):
        return False
    return not any(keyword in name_lower for keyword in URGENT_CARE_EXCLUDE_NAME_KEYWORDS)


_MOB_NAME_RE = re.compile(r"\bmob\b")


def _is_legitimate_full_service_hospital(place: dict) -> bool:
    name_raw = _place_name(place)
    name_lower = name_raw.lower()
    primary_type = _place_primary_type(place)
    types = set(place.get("types") or [])
    if not name_lower:
        return False
    if primary_type not in HOSPITAL_PRIMARY_TYPES:
        return False
    if types & MEDICAL_EXCLUDE_TYPES:
        return False
    if any(keyword in name_lower for keyword in HOSPITAL_FALSE_POSITIVE_NAME_KEYWORDS):
        return False
    # "MOB" (Medical Office Building) is a standard healthcare real-estate abbreviation for an
    # outpatient office building on a hospital campus, distinct from the hospital itself - a real
    # Google Places result named "Numors mob Milford" (primaryType=hospital, no other red flags)
    # turned out to be "Nemours duPont Pediatrics, Milford" (confirmed via web search), an
    # outpatient pediatric practice sharing an address with the real adjacent hospital. Matched as
    # a whole word (\b), not a bare substring, so this can't false-positive on "mobile"/"automobile".
    if _MOB_NAME_RE.search(name_lower):
        return False
    if _is_individual_provider_name(name_raw) or _is_bare_street_address_name(name_raw):
        return False
    # Must actually read as a facility, not a ghost/administrative listing riding along on
    # primaryType=hospital - see HOSPITAL_FACILITY_IDENTITY_KEYWORDS for the real examples that
    # motivated this ("Myblood", a blood-donation service; a listing literally named "Delaware").
    if not any(keyword in name_lower for keyword in HOSPITAL_FACILITY_IDENTITY_KEYWORDS):
        return False
    return True


def _non_hospital_medical_label(place: dict) -> Optional[str]:
    name_raw = _place_name(place)
    name_lower = name_raw.lower()
    primary_type = _place_primary_type(place)
    types = set(place.get("types") or [])
    if not name_lower or primary_type in HOSPITAL_PRIMARY_TYPES:
        return None
    if primary_type in MEDICAL_EXCLUDE_TYPES or types & MEDICAL_EXCLUDE_TYPES:
        return None
    if any(keyword in name_lower for keyword in NON_HOSPITAL_MEDICAL_EXCLUDE_NAME_KEYWORDS):
        return None
    # A named patient-care facility only - not an individual provider's solo listing (e.g. "Jessica
    # A. Quirin, CRNP", surfaced by the urgent-care text search despite having nothing to do with
    # urgent care) and not a bare street address Google failed to attach a real name to.
    if _is_individual_provider_name(name_raw) or _is_bare_street_address_name(name_raw):
        return None
    if any(keyword in name_lower for keyword in URGENT_CARE_INCLUDE_KEYWORDS):
        return "URGENT CARE"
    if primary_type == "medical_clinic":
        if any(keyword in name_lower for keyword in ["family medicine", "family practice", "primary care", "internal medicine"]):
            return "FAMILY MEDICINE"
        return "MEDICAL CLINIC"
    if primary_type == "doctor":
        if any(keyword in name_lower for keyword in ["family medicine", "family practice", "primary care", "internal medicine"]):
            return "FAMILY MEDICINE"
        return "DOCTOR"
    if primary_type == "medical_center":
        return "MEDICAL CENTER"
    return None


# "ent" and "eye" are short enough to collide with ordinary substrings - "ent" is inside "center"
# (so literally every "Medical Center"-labeled facility was being flagged a specialist), and "eye"
# is inside "Meyer" (a common surname). Found in real testing once proximity-weighted ranking
# stopped burying MEDICAL CENTER results behind a lower rank regardless of this false penalty.
# Matched as whole words so real "ENT"/"eye" specialty names ("ENT Associates", "Eye Care Center")
# still match, but "center"/"Meyer" don't.
_WORD_BOUNDARY_SPECIALIST_KEYWORDS = {"ent", "eye"}
_SPECIALIST_KEYWORD_RES = {
    kw: re.compile(rf"\b{re.escape(kw)}\b") for kw in _WORD_BOUNDARY_SPECIALIST_KEYWORDS
}


def _matches_specialist_keyword(name_lower: str) -> bool:
    for keyword in SPECIALIST_MEDICAL_NAME_KEYWORDS:
        if keyword in _SPECIALIST_KEYWORD_RES:
            if _SPECIALIST_KEYWORD_RES[keyword].search(name_lower):
                return True
        elif keyword in name_lower:
            return True
    return False


def _non_hospital_medical_rank(label: str, place: dict) -> int:
    rank = NON_HOSPITAL_MEDICAL_RANK[label]
    name_lower = _place_name(place).lower()
    if _matches_specialist_keyword(name_lower):
        rank += 6
    return rank


def _nearest_valid_place(raw_places: list[dict], lat: float, lng: float, predicate, label: str) -> Optional[NearbyPlace]:
    candidates: list[NearbyPlace] = []
    seen_places: set[tuple[str, str, str]] = set()
    for place in raw_places:
        if not predicate(place):
            continue
        nearby = _place_to_nearby(place, lat, lng)
        if nearby is None:
            continue
        dedupe_key = _place_dedupe_key(place)
        if dedupe_key in seen_places:
            continue
        seen_places.add(dedupe_key)
        nearby.place_type = label
        candidates.append(nearby)
    candidates.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    return candidates[0] if candidates else None


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
    field_mask: str = FIELD_MASK, section: str = "unknown", rank_preference: Optional[str] = None,
) -> list[dict]:
    """Each amenity type gets its own dedicated call. A single combined multi-type call is cheaper in theory,
    but Places API's results share one 20-result cap - whichever type happens to be unusually dense nearby
    (schools in some areas, hospital-affiliated clinics in others) crowds out the rest. Proven out empirically,
    not just theoretical - so correctness wins here over trimming a few requests on a listing cached 180 days.

    rank_preference: Nearby Search defaults to POPULARITY when unset - confirmed live this systematically
    starves genuinely close options in dense metros (a Feltonville, PA parks search never returned Hunting
    Park, 1.4mi away, because the 20-result pool filled up with Center City tourist parks 4-7mi away first;
    same shape of bug found for Grocery). Pass "DISTANCE" to opt a category into real nearest-first pooling."""
    body = {
        "includedTypes": [included_type],
        "maxResultCount": max_result_count,
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": _miles_to_meters(radius_miles)}},
    }
    if rank_preference:
        body["rankPreference"] = rank_preference
    response = await client.post(
        PLACES_NEARBY_URL,
        json=body,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": field_mask},
        timeout=10.0,
    )
    response.raise_for_status()
    _log_billable_call(section, PLACES_SEARCH_COST_USD)
    return response.json().get("places", [])


async def _search_nearby_types(
    client: httpx.AsyncClient, api_key: str, lat: float, lng: float, included_types: list[str], max_result_count: int,
    radius_miles: float = DEFAULT_SEARCH_RADIUS_MILES, field_mask: str = TYPED_FIELD_MASK, section: str = "unknown",
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
    _log_billable_call(section, PLACES_SEARCH_COST_USD)
    return response.json().get("places", [])


async def _search_text(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, text_query: str, radius_miles: float, field_mask: str = FIELD_MASK, section: str = "unknown") -> list[dict]:
    response = await client.post(
        PLACES_TEXT_URL,
        json={
            "textQuery": text_query,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": _miles_to_meters(radius_miles)}},
        },
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": field_mask},
        timeout=10.0,
    )
    response.raise_for_status()
    _log_billable_call(section, PLACES_SEARCH_COST_USD)
    return response.json().get("places", [])


def _to_sorted_places(raw_places: list[dict], lat: float, lng: float) -> list[NearbyPlace]:
    places = [p for p in (_place_to_nearby(raw, lat, lng) for raw in raw_places) if p is not None]
    places.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    return places[:MAX_RESULTS_PER_CATEGORY]


async def _check_minute_burst_limit(db: AsyncSession, ip: Optional[str], device_id: Optional[str], config: dict) -> None:
    """A fast first-line throttle the day/month $ ceilings don't provide on their own: a visitor
    (or a script) firing dozens of requests in a few seconds stays well under its $3/day cap the
    whole time, just very quickly - the daily cap bounds total damage, but does nothing to slow
    down how fast it gets there. Same atomic single-row UPDATE pattern as every other budget
    bucket (usage_budget_service.reserve_bucket), just keyed to the current UTC minute instead of
    day/month, so concurrent requests from the same visitor genuinely can't both slip through once
    the limit's hit - no separate read-then-write gap for a race to land in.

    Checked before the day/month reservation below (cheapest, fastest-rejecting gate first) and
    raises the same ExploreNearbyBudgetExceeded a caller already knows how to handle."""
    limit = config["places_explore_minute_request_limit"]
    minute_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    try:
        for kind, ident in (("ip", ip), ("device", device_id)):
            if not ident:
                continue
            bucket = f"{kind}_minute:{ident}"
            ok = await reserve_bucket(db, bucket, "minute", minute_key, count_delta=1, cost_delta_micros=0, count_limit=limit, cost_limit_micros=None)
            if not ok:
                raise ExploreNearbyBudgetExceeded("Too many requests - please slow down and try again in a moment.")
        await db.commit()
    except ExploreNearbyBudgetExceeded:
        await db.rollback()
        raise


async def _check_and_reserve_explore_budget_locked(
    db: AsyncSession, feature: str, config: dict,
    ip: Optional[str] = None, device_id: Optional[str] = None, owner_id: Optional[str] = None,
) -> None:
    """DB-backed, atomic across workers, survives a restart (usage_budget_service.py) - replaces
    an earlier in-process Counter, which reset on every restart and gave each worker process in a
    multi-worker deployment its own independent budget (silently multiplying the real ceiling).

    Reserves against this feature's own bucket (places_search/places_details), the platform-wide
    emergency ceiling, AND (when the caller can supply them) per-IP, per-device, and per-agent
    daily ceilings - so a Places spike alone can't quietly eat the whole platform budget, AND one
    abusive requester can't burn through everyone else's shared daily allowance either. All
    buckets are reserved atomically in one call - the request must clear every one of them."""
    await _check_minute_burst_limit(db, ip, device_id, config)

    if feature == "explore_nearby_search":
        bucket, cost_usd = "places_search", config["places_explore_search_estimated_cost_usd"]
        daily_limit, monthly_limit = config["places_explore_search_daily_limit"], config["places_explore_search_monthly_limit"]
    else:
        bucket, cost_usd = "places_details", config["places_explore_details_estimated_cost_usd"]
        daily_limit, monthly_limit = config["places_explore_details_daily_limit"], config["places_explore_details_monthly_limit"]

    buckets = {
        bucket: (daily_limit, monthly_limit, None, None),
        "platform_emergency": (
            None, None,
            config["platform_emergency_daily_cost_ceiling_usd"], config["platform_emergency_monthly_cost_ceiling_usd"],
        ),
    }
    if ip:
        buckets[f"ip_daily:{ip}"] = (None, None, config["ip_daily_cost_limit_usd"], None)
    if device_id:
        buckets[f"device_daily:{device_id}"] = (None, None, config["device_daily_cost_limit_usd"], None)
    if owner_id:
        buckets[f"agent_site_daily:{owner_id}"] = (None, None, config["agent_site_daily_cost_limit_usd"], None)
        buckets[f"agent_total_daily:{owner_id}"] = (None, None, config["agent_total_daily_cost_limit_usd"], None)

    try:
        await reserve_budget(db, buckets, cost_usd, count_delta=1)
    except BudgetExceeded as exc:
        if exc.bucket.startswith("ip_daily:") or exc.bucket.startswith("device_daily:"):
            raise ExploreNearbyBudgetExceeded("Too many nearby-places requests from this visitor today.")
        if exc.bucket.startswith("agent_"):
            raise ExploreNearbyBudgetExceeded("This site has reached its daily nearby-places limit.")
        raise ExploreNearbyBudgetExceeded(f"Daily or monthly Google Places {feature.replace('_', ' ')} limit reached.")


async def is_explore_nearby_available(db: AsyncSession, config: dict) -> bool:
    """A read-only check (no reservation, no Google call) so a page can proactively hide the whole
    Explore Nearby section before a visitor ever clicks a category, rather than only discovering the
    monthly search budget is exhausted after a click fails. Search-only (not Details) - Details
    exhaustion already degrades silently per-place (see get_explore_place_details) without needing to
    hide anything. Naturally reopens on its own once the monthly period rolls over, since
    usage_snapshot reads the current month's counter, which resets to a fresh row automatically."""
    snap = await usage_snapshot(db, "places_search")
    if snap["day_count"] >= config["places_explore_search_daily_limit"]:
        return False
    if snap["month_count"] >= config["places_explore_search_monthly_limit"]:
        return False
    return True


async def _read_explore_cache(db: AsyncSession, cache_key: str, ttl_days: float) -> Optional[dict]:
    """A hit here means zero Google calls and zero budget reservation - the real cost lever for
    this feature (see ExploreNearbyCache's docstring), not just a rate limit. A stale row (past
    ttl_days) reads as a miss and gets overwritten on the next successful live fetch, rather than
    being deleted separately."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    result = await db.execute(
        select(ExploreNearbyCache.payload).where(
            ExploreNearbyCache.cache_key == cache_key,
            ExploreNearbyCache.fetched_at >= cutoff,
        )
    )
    return result.scalar_one_or_none()


async def _write_explore_cache(db: AsyncSession, cache_key: str, payload: dict) -> None:
    stmt = pg_insert(ExploreNearbyCache).values(
        id=str(uuid.uuid4()), cache_key=cache_key, payload=payload,
    ).on_conflict_do_update(
        index_elements=["cache_key"], set_={"payload": payload, "fetched_at": func.now()},
    )
    await db.execute(stmt)
    await db.commit()


def _explore_place_to_response(place: dict, category: str, origin_lat: float, origin_lng: float) -> Optional[ExploreNearbyPlace]:
    name = _place_name(place)
    location = place.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    place_id = _place_identity(place)
    if not name or lat is None or lng is None or not place_id:
        return None
    distance = _haversine_miles(origin_lat, origin_lng, float(lat), float(lng))
    return ExploreNearbyPlace(
        place_id=place_id,
        name=name,
        category=((place.get("primaryTypeDisplayName") or {}).get("text") or category).strip() or category,
        address=place.get("shortFormattedAddress"),
        distance_miles=round(distance, 1),
        latitude=float(lat),
        longitude=float(lng),
    )


def _explore_place_is_relevant(place: dict, category_key: str) -> bool:
    """Google's includedTypes match for Nearby Search (New) is a soft ranking bias, not a hard filter
    - see the comment on EXPLORE_NEARBY_MAX_RESULTS. Strips the off-category noise that gets through
    anyway (a gas station under Grocery, a coffee shop under Restaurants, a drugstore under Shopping).
    Grocery reuses the existing, more thorough _is_full_service_grocery predicate rather than a simple
    denylist, since that finer-grained "real grocery store vs. mini-mart vs. specialty market" logic
    already existed elsewhere in this file. Quality over count throughout: this only ever removes
    candidates, never backfills to hit a target count - a category with 4 genuine matches shows 4."""
    if category_key == "grocery":
        return _explore_grocery_ok(place)
    if category_key == "medical":
        # "VA HOSPITAL 25" - a numbered building of a campus that's already on the list by name.
        return _is_legitimate_full_service_hospital(place) and not re.search(r"\s\d+$", _place_name(place))
    if category_key == "coffee":
        # A real coffee shop / cafe - not a bookstore or breakfast diner that happens to pour coffee.
        if _place_primary_type(place) not in {"cafe", "coffee_shop", "coffee_roastery", "coffee_stand", "tea_house"}:
            return False
    if category_key == "parks" and re.match(r"^(playground|sports fields?|ball ?fields?|tennis courts?|basketball courts?|dog park)\b", _place_name(place).lower()):
        return False   # a nameless feature, not a park someone would look up
    if category_key == "schools":
        return _explore_school_label(place) is not None

    # Applied to every remaining category, not just restaurants - gas_station/convenience_store are
    # not normal secondary tags on a genuine restaurant/cafe/shop, unlike broad parent categories (see
    # the "restaurant"-as-cafe's-secondary-type false positive below), so this is safe everywhere.
    if set(place.get("types") or []) & EXPLORE_NEARBY_HYBRID_EXCLUDE_TYPES:
        return False

    if category_key == "restaurants":
        # An allowlist, not a denylist - a denylist can only catch mislabeling already seen once, and
        # even a correct-looking primaryType isn't proof of genuine identity (Wawa tested as
        # primaryType=fast_food_restaurant), so a name-based safety net backs up the type check too.
        name_lower = _place_name(place).lower()
        if any(pattern in name_lower for pattern in CONVENIENCE_GAS_HYBRID_NAME_PATTERNS):
            return False
        return _explore_restaurant_ok(place)

    excluded = EXPLORE_NEARBY_EXCLUDED_TYPES.get(category_key)
    if not excluded:
        return True
    # primaryType only, deliberately - Google's secondary `types` list commonly includes broad parent
    # categories (a real cafe's types often include "restaurant" alongside "cafe"), so checking the
    # excluded set against secondary types produced false positives (e.g. wiped out real coffee shops
    # because "restaurant" is excluded there but also a common secondary tag on cafes).
    return _place_primary_type(place) not in excluded


def _explore_grocery_ok(place: dict) -> bool:
    """A real grocery store (user, 10/24): one of the big names, or a genuine supermarket - never a
    corner shop, deli, convenience store or specialty market."""
    if not _is_full_service_grocery(place):
        return False
    name = _place_name(place).lower()
    if any(p in name for p in CONVENIENCE_GAS_HYBRID_NAME_PATTERNS):
        return False
    if any(b in name for b in MAJOR_GROCERY_BRANDS):
        return True
    if any(h in name for h in GROCERY_SPECIALTY_NAME_HINTS):
        return False
    return _place_primary_type(place) == "supermarket"


def _explore_restaurant_ok(place: dict) -> bool:
    """A sit-down restaurant (user, 10/24): not fast food, a takeout counter or a chain quick-serve."""
    primary = _place_primary_type(place)
    if primary not in RESTAURANT_PRIMARY_TYPES or primary in FAST_FOOD_PRIMARY_TYPES:
        return False
    if "fast_food_restaurant" in set(place.get("types") or []):
        return False
    name = _place_name(place).lower()
    return not any(p in name for p in FAST_FOOD_NAME_PATTERNS)


def _explore_school_label(place: dict) -> Optional[str]:
    """K-12 schools, public and private (user, 10/24) - with a plain label ("Private · High school",
    "Elementary school"). None for daycare, tutoring, colleges, district offices and the like."""
    name = _place_name(place)
    low = name.lower()
    types = place.get("types") or []
    if not name or _place_primary_type(place) not in SCHOOL_PRIMARY_TYPES:
        return None
    skip = [k for k in PRIVATE_SCHOOL_EXCLUDE_KEYWORDS if k not in {"public school", "public charter", "charter school"}]
    if any(k in low for k in skip) or "university" in types or "preschool" in types:
        return None
    if any(k in low for k in ("administration", "admin building", "district office", "bus garage", "transportation")):
        return None
    if re.search(r"\bk\s*[-–]\s*12\b", low):
        level = "K-12 school"
    elif re.search(r"\bk\s*[-–]\s*8\b", low):
        level = "K-8 school"
    elif re.search(r"middle school|junior high|\bintermediate\b|\bmiddle\b", low):
        level = "Middle school"
    elif re.search(r"elementary|\bprimary\b|kindergarten", low):
        level = "Elementary school"
    elif re.search(r"high school|\bhigh\b|secondary|\bhs\b", low):
        level = "High school"
    elif "secondary_school" in types:
        level = "High school"
    elif "primary_school" in types:
        level = "Elementary school"
    else:
        level = "School"
    private_hints = ("private", "independent", "academy", "preparatory", " prep", "catholic", "christian",
                     "parochial", "friends", "montessori", "waldorf", "episcopal", "lutheran", "st.", "sts.", "saint",
                     "day school", "country day", "baptist", "hebrew", "jewish", "islamic", "yeshiva")
    if any(h in low for h in private_hints) and "charter" not in low:
        return f"Private · {level[0].lower() + level[1:]}" if level != "School" else "Private school"
    if "charter" in low:
        return "Charter school"
    return level


def _rehydrate_places_for_origin(payload: dict, lat: float, lng: float) -> dict:
    """A served result (cache hit or in-flight reuse) may have been fetched for a nearby property
    sharing the same coarse cache-grid cell (see get_explore_nearby's EXPLORE_NEARBY_CACHE_GEO_PRECISION)
    rather than this exact request's coordinates. Reusing the underlying place list costs nothing extra,
    but each place's distance and the response origin must always be recomputed for the real viewer -
    pure local haversine math, no Google call - so a distance or map pin is never shown relative to a
    different property than the one actually being viewed."""
    places = []
    for raw in payload.get("places", []):
        place = dict(raw)
        place_lat, place_lng = place.get("latitude"), place.get("longitude")
        if place_lat is not None and place_lng is not None:
            place["distance_miles"] = round(_haversine_miles(lat, lng, float(place_lat), float(place_lng)), 1)
        places.append(place)
    places.sort(key=lambda p: p.get("distance_miles") if p.get("distance_miles") is not None else float("inf"))
    return {**payload, "origin": {"lat": lat, "lng": lng}, "places": places}


async def _fetch_explore_nearby_live(
    api_key: str,
    lat: float,
    lng: float,
    category_key: str,
    radius_miles: float,
) -> ExploreNearbyResponse:
    category = EXPLORE_NEARBY_CATEGORIES[category_key]
    started = time.perf_counter()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            PLACES_NEARBY_URL,
            json={
                "includedTypes": category["types"],
                "maxResultCount": EXPLORE_NEARBY_MAX_RESULTS,
                "rankPreference": "POPULARITY",
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": _miles_to_meters(radius_miles),
                    }
                },
            },
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": EXPLORE_NEARBY_FIELD_MASK,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    raw_places = [raw for raw in response.json().get("places", []) if _explore_place_is_relevant(raw, category_key)]
    places = []
    for raw in raw_places:
        p = _explore_place_to_response(raw, category["label"], lat, lng)
        if p is None:
            continue
        # Plain labels a buyer reads at a glance (not Google's own "Secondary school").
        if category_key == "schools":
            p.category = _explore_school_label(raw) or p.category
        elif category_key == "medical":
            p.category = "Hospital"
        # The same name twice a few doors apart (two listings for one Starbucks) - once is enough.
        if any(q.name.strip().lower() == p.name.strip().lower()
               and _haversine_miles(q.latitude, q.longitude, p.latitude, p.longitude) < 0.25 for q in places):
            continue
        places.append(p)
    places.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    places = places[:EXPLORE_NEARBY_DISPLAY_LIMIT]
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "Google Places Explore Nearby request",
        extra={
            "feature": "explore_nearby_search",
            "category": category_key,
            "radius_miles": radius_miles,
            "result_count": len(places),
            "cache_hit": False,
            "estimated_sku": "places_api_nearby_search_pro",
            "estimated_cost_usd": EXPLORE_NEARBY_SEARCH_ESTIMATED_COST_USD,
            "elapsed_ms": elapsed_ms,
        },
    )
    return ExploreNearbyResponse(
        category=category_key,
        origin={"lat": lat, "lng": lng},
        radius_miles=radius_miles,
        places=places,
        cache_hit=False,
        sku="nearby_search_pro",
    )


async def get_explore_nearby(
    db: AsyncSession,
    lat: float,
    lng: float,
    category_key: str,
    ip: Optional[str] = None,
    device_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> ExploreNearbyResponse:
    """Interactive public-site Places lookup.

    Backed by a persistent cache (ExploreNearbyCache, TTL config-driven - see
    places_explore_cache_ttl_days) keyed at a coarse lat/lng grid (EXPLORE_NEARBY_CACHE_GEO_PRECISION)
    - the real lever on total spend, since the same property/area+category gets asked about by many
    visitors over the following days, AND nearby properties sharing a grid cell reuse each other's
    search. A cache hit costs nothing and never touches the budget reservation below at all - distances
    and the origin are always recomputed for the real requester (_rehydrate_places_for_origin), so
    sharing a grid cell never shows a wrong distance or pin. Only a genuine cache miss reaches Google,
    and identical concurrent misses still share one in-flight task so a burst of simultaneous
    first-time requests doesn't fan out into duplicate outbound calls.
    """
    normalized_category = category_key.strip().lower()
    if normalized_category not in EXPLORE_NEARBY_CATEGORIES:
        raise ValueError("Unsupported nearby category.")

    # Radius is always the category's own fixed default - never caller-supplied. It used to accept
    # a client radius_miles query param, which let a requester force a fresh (billed) cache miss on
    # every single request by jittering the radius by 0.01 across its [0.25, 5.0] range (~476
    # distinct cache buckets per lat/lng/category) instead of ever hitting the real cache - nothing
    # legitimate ever set a custom radius (the frontend never sent one), so this closes that off
    # entirely rather than just capping the damage.
    safe_radius = EXPLORE_NEARBY_CATEGORIES[normalized_category].get("radius_miles") or EXPLORE_NEARBY_DEFAULT_RADIUS_MILES
    grid_key = (
        f"{round(float(lat), EXPLORE_NEARBY_CACHE_GEO_PRECISION)}:{round(float(lng), EXPLORE_NEARBY_CACHE_GEO_PRECISION)}:"
        f"{normalized_category}:{round(safe_radius, 2)}"
    )
    cache_key = f"search:v{EXPLORE_NEARBY_CACHE_VERSION}:{grid_key}"

    config = await ai_config_service.get_config(db)

    cached = await _read_explore_cache(db, cache_key, config["places_explore_cache_ttl_days"])
    if cached is not None:
        logger.info(
            "Google Places Explore Nearby request served from cache",
            extra={"feature": "explore_nearby_search", "category": normalized_category, "cache_hit": True, "estimated_cost_usd": 0.0},
        )
        return ExploreNearbyResponse(**{**_rehydrate_places_for_origin(cached, float(lat), float(lng)), "cache_hit": True})

    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise ExploreNearbyLookupUnavailable("GOOGLE_PLACES_API_KEY is not configured.")

    owner = False
    async with _explore_nearby_lock:
        task = _explore_nearby_inflight.get(grid_key)
        if task is None:
            await _check_and_reserve_explore_budget_locked(db, "explore_nearby_search", config, ip=ip, device_id=device_id, owner_id=owner_id)
            task = asyncio.create_task(_fetch_explore_nearby_live(api_key, float(lat), float(lng), normalized_category, safe_radius))
            _explore_nearby_inflight[grid_key] = task
            owner = True

    try:
        result = await task
        if owner:
            await _write_explore_cache(db, cache_key, result.model_dump(mode="json"))
            return result
        logger.info(
            "Google Places Explore Nearby request reused in-flight result",
            extra={
                "feature": "explore_nearby_search",
                "category": normalized_category,
                "cache_hit": True,
                "estimated_sku": "places_api_nearby_search_pro",
                "estimated_cost_usd": 0.0,
            },
        )
        rehydrated = _rehydrate_places_for_origin(result.model_dump(mode="json"), float(lat), float(lng))
        return ExploreNearbyResponse(**{**rehydrated, "cache_hit": True})
    finally:
        if owner:
            async with _explore_nearby_lock:
                if _explore_nearby_inflight.get(grid_key) is task:
                    _explore_nearby_inflight.pop(grid_key, None)


def _normalize_place_id(place_id: str) -> str:
    value = str(place_id or "").strip()
    if value.startswith("places/"):
        value = value.split("/", 1)[1]
    if not re.fullmatch(r"[A-Za-z0-9_\-:.]{8,256}", value):
        raise ValueError("Invalid place ID.")
    return value


async def _fetch_explore_place_details_live(api_key: str, place_id: str) -> ExploreNearbyPlaceDetails:
    started = time.perf_counter()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{PLACES_DETAILS_URL}/{place_id}",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": EXPLORE_NEARBY_DETAILS_FIELD_MASK,
            },
            timeout=10.0,
        )
    response.raise_for_status()
    data = response.json()
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "Google Places Explore Nearby detail request",
        extra={
            "feature": "explore_nearby_details",
            "place_id": place_id,
            "cache_hit": False,
            "estimated_sku": "places_api_place_details_enterprise",
            "estimated_cost_usd": EXPLORE_NEARBY_DETAILS_ESTIMATED_COST_USD,
            "elapsed_ms": elapsed_ms,
        },
    )
    return ExploreNearbyPlaceDetails(
        place_id=data.get("id") or place_id,
        rating=data.get("rating"),
        review_count=data.get("userRatingCount"),
        cache_hit=False,
    )


async def get_explore_place_details(
    db: AsyncSession, place_id: str,
    ip: Optional[str] = None, device_id: Optional[str] = None, owner_id: Optional[str] = None,
) -> ExploreNearbyPlaceDetails:
    """Backed by the same persistent cache as get_explore_nearby (ExploreNearbyCache, keyed by
    place_id) - a place's rating/review count doesn't change fast enough to justify re-paying for
    it every time a different visitor selects it. A cache hit costs nothing and never touches the
    budget reservation below."""
    safe_place_id = _normalize_place_id(place_id)
    cache_key = f"details:{safe_place_id}"

    config = await ai_config_service.get_config(db)

    cached = await _read_explore_cache(db, cache_key, config["places_explore_cache_ttl_days"])
    if cached is not None:
        logger.info(
            "Google Places Explore Nearby detail request served from cache",
            extra={"feature": "explore_nearby_details", "place_id": safe_place_id, "cache_hit": True, "estimated_cost_usd": 0.0},
        )
        return ExploreNearbyPlaceDetails(**{**cached, "cache_hit": True})

    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise ExploreNearbyLookupUnavailable("GOOGLE_PLACES_API_KEY is not configured.")

    owner = False
    async with _explore_nearby_lock:
        task = _explore_nearby_details_inflight.get(safe_place_id)
        if task is None:
            await _check_and_reserve_explore_budget_locked(db, "explore_nearby_details", config, ip=ip, device_id=device_id, owner_id=owner_id)
            task = asyncio.create_task(_fetch_explore_place_details_live(api_key, safe_place_id))
            _explore_nearby_details_inflight[safe_place_id] = task
            owner = True

    try:
        result = await task
        if owner:
            await _write_explore_cache(db, cache_key, result.model_dump(mode="json"))
            return result
        logger.info(
            "Google Places Explore Nearby detail request reused in-flight result",
            extra={
                "feature": "explore_nearby_details",
                "place_id": safe_place_id,
                "cache_hit": True,
                "estimated_sku": "places_api_place_details_enterprise",
                "estimated_cost_usd": 0.0,
            },
        )
        return result.model_copy(update={"cache_hit": True})
    finally:
        if owner:
            async with _explore_nearby_lock:
                if _explore_nearby_details_inflight.get(safe_place_id) is task:
                    _explore_nearby_details_inflight.pop(safe_place_id, None)


def _is_full_service_grocery(place: dict) -> bool:
    name = ((place.get("displayName") or {}).get("text") or "").lower()
    primary_type = _place_primary_type(place)
    types = set(place.get("types") or [])
    if not name:
        return False
    if primary_type in GROCERY_EXCLUDED_TYPES or primary_type in {"discount_store", "department_store", "gift_shop"}:
        return False
    if any(pattern in name for pattern in GROCERY_EXCLUDED_NAME_PATTERNS):
        return False
    if primary_type in GROCERY_STRONG_PRIMARY_TYPES:
        return True
    if types & GROCERY_EXCLUDED_TYPES:
        return False
    if primary_type in GROCERY_SECONDARY_TYPES and any(hint in name for hint in GROCERY_FULL_SERVICE_NAME_HINTS):
        return True
    return bool(types & GROCERY_STRONG_PRIMARY_TYPES) and any(hint in name for hint in GROCERY_FULL_SERVICE_NAME_HINTS)


def _is_true_grocery_or_supermarket(place: dict) -> bool:
    """Stricter than _is_full_service_grocery: only a genuine grocery store/supermarket counts, not
    a specialty market (farm stand, butcher, dairy, produce stand, etc.) that Google may still tag
    under a grocery-ish primary type. Used to guarantee at least one of the two grocery results is a
    real grocery store whenever one is reasonably nearby - see _select_grocery_pair."""
    if not _is_full_service_grocery(place):
        return False
    name = ((place.get("displayName") or {}).get("text") or "").lower()
    if any(hint in name for hint in GROCERY_SPECIALTY_NAME_HINTS):
        return False
    return _place_primary_type(place) in GROCERY_STRONG_PRIMARY_TYPES


def _sorted_valid_grocery_candidates(raw_places: list[dict], lat: float, lng: float) -> list[tuple[dict, NearbyPlace]]:
    pairs = []
    for raw in raw_places:
        if not _is_full_service_grocery(raw):
            continue
        nearby = _place_to_nearby(raw, lat, lng)
        if nearby is None:
            continue
        pairs.append((raw, nearby))
    pairs.sort(key=lambda pair: pair[1].distance_miles if pair[1].distance_miles is not None else float("inf"))
    return pairs


def _select_grocery_pair(pairs: list[tuple[dict, NearbyPlace]]) -> list[NearbyPlace]:
    """Picks up to 2 results from nearest-first valid grocery candidates, guaranteeing a true
    grocery store/supermarket fills one slot whenever one exists in the pool - so two specialty
    markets (farm stand, butcher, dairy, produce stand) never crowd out a legitimate grocery store
    that's just as reasonably close."""
    if not pairs:
        return []
    true_grocery_pairs = [pair for pair in pairs if _is_true_grocery_or_supermarket(pair[0])]
    if not true_grocery_pairs:
        return [nearby for _, nearby in pairs[:2]]
    nearest_true = true_grocery_pairs[0]
    others = [pair for pair in pairs if pair is not nearest_true]
    selected = [nearest_true, *others[:1]]
    selected.sort(key=lambda pair: pair[1].distance_miles if pair[1].distance_miles is not None else float("inf"))
    return [nearby for _, nearby in selected]


async def _fetch_grocery_stores(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """supermarket and grocery_store are searched together in one call per radius tier (tested
    against 3 real locations - identical final picks to searching them separately, since both
    types share the same 20-result cap either way and the selection logic below is unaffected by
    which single call the raw pool came from)."""
    collected: list[dict] = []
    seen_ids: set[tuple[str, str]] = set()
    for radius in [DEFAULT_SEARCH_RADIUS_MILES, 12, 16, 24]:
        raw_places = await _search_nearby_types(
            client,
            api_key,
            lat,
            lng,
            ["supermarket", "grocery_store"],
            max_result_count=NEARBY_CANDIDATE_POOL,
            radius_miles=radius,
            field_mask=TYPED_FIELD_MASK,
            section="grocery",
        )
        for place in raw_places:
            key = (_place_name(place).lower(), place.get("formattedAddress") or "")
            if key not in seen_ids:
                seen_ids.add(key)
                collected.append(place)
        pairs = _sorted_valid_grocery_candidates(collected, lat, lng)
        if len(pairs) >= 2:
            return _select_grocery_pair(pairs)
    return _select_grocery_pair(_sorted_valid_grocery_candidates(collected, lat, lng))


async def _fetch_medical_care(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Return nearest true hospital plus one useful non-hospital medical provider."""
    hospital_place: Optional[NearbyPlace] = None
    non_hospital_candidates: list[tuple[int, NearbyPlace]] = []
    hospital_name_key: Optional[str] = None
    seen_non_hospital: set[tuple[str, str, str]] = set()

    for radius in [DEFAULT_SEARCH_RADIUS_MILES, 12, 16, 24]:
        # general_hospital + hospital searched together in one call - kept as its OWN call,
        # separate from the non-hospital types below, so hospital results never have to compete
        # for the shared 20-result cap against the far more numerous doctor/clinic listings
        # (tested: combining hospital types into the SAME call as clinic/doctor/medical_center
        # let dense doctor-office listings crowd out real hospitals in a low-density market).
        hospital_raw = await _search_nearby_types(
            client,
            api_key,
            lat,
            lng,
            ["general_hospital", "hospital"],
            max_result_count=NEARBY_CANDIDATE_POOL,
            radius_miles=radius,
            field_mask=TYPED_FIELD_MASK,
            section="medical_hospital",
        )
        if not hospital_place:
            hospital_place = _nearest_valid_place(hospital_raw, lat, lng, _is_legitimate_full_service_hospital, "HOSPITAL")
            hospital_name_key = hospital_place.name.strip().lower() if hospital_place else None

        medical_raw = await _search_nearby_types(
            client,
            api_key,
            lat,
            lng,
            ["medical_clinic", "doctor", "medical_center"],
            max_result_count=NEARBY_CANDIDATE_POOL,
            radius_miles=radius,
            field_mask=TYPED_FIELD_MASK,
            section="medical_non_hospital",
        )
        urgent_raw = await _search_text(client, api_key, lat, lng, "urgent care", radius, field_mask=TYPED_FIELD_MASK, section="medical_urgent_care")
        for place in [*medical_raw, *urgent_raw]:
            label = _non_hospital_medical_label(place)
            if not label:
                continue
            nearby = _place_to_nearby(place, lat, lng)
            if not nearby:
                continue
            name_key = nearby.name.strip().lower()
            dedupe_key = _place_dedupe_key(place)
            if name_key == hospital_name_key or dedupe_key in seen_non_hospital:
                continue
            seen_non_hospital.add(dedupe_key)
            nearby.place_type = label
            non_hospital_candidates.append((_non_hospital_medical_rank(label, place), nearby))

        if hospital_place and non_hospital_candidates:
            break

    non_hospital_candidates.sort(key=lambda pair: (pair[0], pair[1].distance_miles if pair[1].distance_miles is not None else float("inf")))
    results: list[NearbyPlace] = []
    if hospital_place:
        results.append(hospital_place)
    if non_hospital_candidates:
        results.append(non_hospital_candidates[0][1])
    return results


async def _fetch_private_schools(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Returns up to two nearby private K-12 schools useful to a typical homebuyer.

    Google text search returns many "school-like" businesses (public schools, daycares, tutoring,
    music/driving schools, colleges, and specialized therapeutic programs). We rank by private-school
    confidence/usefulness first and distance second, rather than blindly taking the closest results.
    """
    raw = await _search_text(client, api_key, lat, lng, "private schools", DEFAULT_SEARCH_RADIUS_MILES, field_mask=TYPED_FIELD_MASK, section="private_schools")

    candidates: list[tuple[int, NearbyPlace]] = []
    seen_names: set[str] = set()
    for place in raw:
        candidate = _private_school_candidate(place)
        if candidate is None:
            continue
        nearby = _place_to_nearby(place, lat, lng)
        if nearby is None:
            continue
        name_key = nearby.name.strip().lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        rank, label = candidate
        nearby.place_type = label
        candidates.append((rank, nearby))

    candidates.sort(key=lambda pair: (pair[0], pair[1].distance_miles if pair[1].distance_miles is not None else float("inf")))
    return [nearby for _, nearby in candidates[:PRIVATE_SCHOOL_MAX_RESULTS]]


_PARK_SUB_LOCATION_SEPARATOR_KEYWORDS = ("gateway", "entrance", "trailhead", "trail head", "access point")


def _park_parent_name_key(place: dict) -> str:
    """Groups a park candidate under its underlying park's name, stripping a ", <sub-location>" or
    " - <sub-location>" qualifier when that qualifier reads as an entrance/gateway/trailhead - so
    duplicate entries for the same linear/greenway park (e.g. several differently-worded "Tacony Creek
    Park, X Gateway" entries not already caught by the PARK_SUB_FEATURE_KEYWORDS name check) still
    collapse to one representative instead of consuming multiple of the 5 available result slots."""
    name = _place_name(place).lower()
    for sep in (" - ", " – ", ","):
        if sep in name:
            head, tail = name.split(sep, 1)
            if any(kw in tail for kw in _PARK_SUB_LOCATION_SEPARATOR_KEYWORDS):
                return head.strip()
    return name.strip()


def _dedupe_park_sub_locations(candidates: list[dict], lat: float, lng: float) -> list[dict]:
    """Collapses candidates sharing a parent-park name key to one representative each, preferring the
    canonical bare-name entry (name equals the group's key exactly) when one exists in the group, else
    the nearest sub-location - see _park_parent_name_key."""
    groups: dict[str, tuple[dict, float, bool]] = {}
    for place in candidates:
        key = _park_parent_name_key(place)
        nearby = _place_to_nearby(place, lat, lng)
        distance = nearby.distance_miles if nearby and nearby.distance_miles is not None else float("inf")
        is_canonical = _place_name(place).lower().strip() == key
        existing = groups.get(key)
        if existing is None:
            groups[key] = (place, distance, is_canonical)
            continue
        _, existing_distance, existing_canonical = existing
        if is_canonical and not existing_canonical:
            groups[key] = (place, distance, is_canonical)
        elif is_canonical == existing_canonical and distance < existing_distance:
            groups[key] = (place, distance, is_canonical)
    return [place for place, _, _ in groups.values()]


async def _fetch_parks(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    # rankPreference=DISTANCE: unset (Google's POPULARITY default) was confirmed live to systematically
    # hide genuinely close neighborhood parks behind a metro's famous/high-traffic parks - see
    # _search_nearby's docstring. Surfacing the true nearest pool is what makes the sub-location dedupe
    # below necessary in the first place (a linear/greenway park's many gateway entries only become
    # visible once ranking stops filtering the pool down to "popular" destinations first).
    raw = await _search_nearby(
        client,
        api_key,
        lat,
        lng,
        "park",
        max_result_count=NEARBY_CANDIDATE_POOL,
        field_mask=TYPED_FIELD_MASK,
        section="parks",
        rank_preference="DISTANCE",
    )
    deduped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for place in raw:
        if not _is_parent_park_candidate(place):
            continue
        key = _place_dedupe_key(place)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(place)
    deduped = _dedupe_park_sub_locations(deduped, lat, lng)
    return _to_sorted_places(deduped, lat, lng)


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
        _log_billable_call("airports", len(candidates) * ROUTES_MATRIX_ELEMENT_COST_USD)
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


async def _fetch_assigned_schools(property: Property, db) -> list[NearbyPlace]:
    """Assigned schools for the Cheat Sheet.

    Priority is per level:
    1. Use Bright MLS when it gives the exact assigned school.
    2. Fill only missing levels from imported NCES attendance boundaries by true point-in-polygon.
    3. If neither source resolves a level reliably, leave it absent.
    """
    assignment = await resolve_school_assignment(db, property)
    return assignment.to_nearby_places(NearbyPlace)


async def _known_locality_names(db, names: list[str]) -> set[str]:
    """Zero-cost "is this actually a real town" check against OHP's own local `cities` reference
    table (2,600+ names, already covers every state seen in testing - PA/DE/NJ) - not scoped by
    state, since a downtown candidate can legitimately sit just across a state line (e.g. Camden,
    NJ showing up for a Philadelphia property). Returns the lowercased subset of `names` that
    matched a known real city/town name; anything not in this table is treated the same way the
    old restaurant-validation call treated a "just a named place" result - excluded."""
    if not names:
        return set()
    rows = (await db.execute(select(City.name).where(func.lower(City.name).in_([n.lower() for n in names])))).scalars().all()
    return {n.strip().lower() for n in rows}


async def _fetch_downtown_areas(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, db) -> list[NearbyPlace]:
    """Text-searching the word "downtown" matches literal business names containing it (a short-term
    rental listing titled "Beautiful Convenient Studio Downtown Devon" was outranking the actual town of
    Devon) rather than real places. "locality" is a proper Places type for named towns/villages/boroughs -
    verified live that it's the type Google actually uses for small unincorporated Main Line villages
    (Wayne, Berwyn, Devon, Strafford all came back as "locality") - but it's also what Google uses for
    small named residential/historic places that have no real downtown at all (Ithan, Allgates). Note:
    "sublocality"/"neighborhood" are NOT valid includedTypes for Nearby Search (confirmed live - Google
    400s on them, "Unsupported types"), so don't add them here. Google also returns duplicate entries
    for the same locality name (verified live) - dedupe by name. Names are shown as Google returns them
    - no "Downtown " prefix is invented.

    Ranking is now two zero-cost local signals instead of a per-candidate paid restaurant-validation
    call (tested at 3 real locations, including a rural one - identical top-2 result to the old
    validated approach every time): (1) keep only candidates whose name matches a real town in OHP's
    own `cities` table - this alone separates genuine towns from bare named places (verified live: it
    correctly dropped things like "Redden"/"Wood Branch"/"Flea Hill" while keeping "Georgetown"/
    "Milton"/"Harbeson"), then (2) within the survivors, keep Google's own natural return order - this
    search has no rankPreference override, so it already defaults to POPULARITY, meaning the
    busyness-based ranking the old validation loop paid to reconstruct is already sitting in the
    response order, unused until now."""
    raw = await _search_nearby(client, api_key, lat, lng, "locality", radius_miles=DOWNTOWN_MAX_DISTANCE_MILES, max_result_count=DOWNTOWN_CANDIDATE_POOL, field_mask=FIELD_MASK, section="downtown")

    seen: set[str] = set()
    deduped = []
    for place in raw:
        name = (place.get("displayName") or {}).get("text")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        deduped.append(place)

    known_names = await _known_locality_names(db, [(place.get("displayName") or {}).get("text") for place in deduped])

    results: list[NearbyPlace] = []
    for place in deduped:  # already in Google's natural (POPULARITY) return order
        name = (place.get("displayName") or {}).get("text")
        if not name or name.lower() not in known_names:
            continue
        nearby = _place_to_nearby(place, lat, lng)
        if nearby:
            results.append(nearby)
        if len(results) >= MAX_RESULTS_PER_CATEGORY:
            break
    return results


async def _fetch_live(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, property: Property, db, owner_id: Optional[str] = None) -> NearbyAmenitiesResponse:
    cost_token = _cost_log_var.set([])
    try:
        schools = await _fetch_assigned_schools(property, db)
        grocery_stores = await _fetch_grocery_stores(client, api_key, lat, lng)
        medical_care = await _fetch_medical_care(client, api_key, lat, lng)
        parks = await _fetch_parks(client, api_key, lat, lng)
        airports = await _fetch_airports(client, api_key, lat, lng)
        downtown_areas = await _fetch_downtown_areas(client, api_key, lat, lng, db)

        # Best-effort text search + conservative classification; questionable school-like businesses are
        # skipped rather than shown as private schools.
        private_schools = await _fetch_private_schools(client, api_key, lat, lng)
    finally:
        call_log = _cost_log_var.get() or []
        _cost_log_var.reset(cost_token)

    total_cost = sum(cost for _, cost in call_log)
    by_section: dict[str, float] = {}
    for section, cost in call_log:
        by_section[section] = by_section.get(section, 0.0) + cost
    breakdown = ", ".join(f"{section}=${cost:.3f}" for section, cost in by_section.items())
    logger.info(
        f"[CHEAT SHEET COST] listing_key={property.listing_key} CACHE MISS - live Google cost=${total_cost:.3f} "
        f"({len(call_log)} calls) breakdown: {breakdown}"
    )

    # Attribute this cheat-sheet's live Google spend to the agent who triggered it, so it rolls
    # into their monthly AI % meter / $20 budget / 90% soft cap alongside the LLM tools. No-op
    # when owner_id is None (a sync/warm-cache call with no user - stays on the platform brake).
    if total_cost > 0 and owner_id:
        try:
            from app.services.ai.ai_rate_limit_service import record_external_api_cost
            await record_external_api_cost(db, owner_id, kind="cheat_sheet_places", cost_usd=total_cost)
        except Exception as exc:  # noqa: BLE001 - never fail a cheat sheet over cost logging
            logger.warning(f"cheat-sheet Google cost attribution failed: {exc}")

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
    if property.nearby_amenities.get("_schema_version") not in AMENITIES_COMPATIBLE_SCHEMA_VERSIONS:
        return None  # written under old field names/logic - treat as a miss rather than serve stale shape
    cached_at = property.nearby_amenities_cached_at
    failed_at_raw = property.nearby_amenities.get("_google_lookup_failed_at")
    if failed_at_raw and cached_at:
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - cached_at > timedelta(hours=CACHE_FAILURE_TTL_HOURS):
            return None
    try:
        return NearbyAmenitiesResponse(**property.nearby_amenities, cached_at=property.nearby_amenities_cached_at, available=True)
    except Exception:
        return None


async def _persist_amenities_cache(db, property: Property, result: NearbyAmenitiesResponse, google_failed: bool = False) -> None:
    try:
        data = result.model_dump(mode="json", exclude={"available", "cached_at"})
        data["_schema_version"] = AMENITIES_SCHEMA_VERSION
        # Keep the saved web school lookup (ai_school_lookup_service) - it's paid for once per home.
        prev = property.nearby_amenities if isinstance(property.nearby_amenities, dict) else {}
        if prev.get("ai_schools"):
            data["ai_schools"] = prev["ai_schools"]
        if google_failed:
            data["_google_lookup_failed_at"] = datetime.now(timezone.utc).isoformat()
        property.nearby_amenities = data
        property.nearby_amenities_cached_at = result.cached_at or datetime.now(timezone.utc)
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist nearby amenities cache for listing {property.listing_key}: {e}")
        await db.rollback()


async def get_nearby_amenities(db, property: Property, force_refresh: bool = False, owner_id: Optional[str] = None) -> NearbyAmenitiesResponse:
    """The Cheat Sheet's nearby data. Schools still missing after Bright MLS + NCES get a last-resort
    web lookup (ai_school_lookup_service - saved on the listing, so once per home)."""
    result = await _get_nearby_amenities_base(db, property, force_refresh=force_refresh, owner_id=owner_id)
    try:
        from app.services.ai.ai_school_lookup_service import fill_missing_schools
        result.schools = await fill_missing_schools(db, property, list(result.schools or []), owner_id=owner_id)
        if result.schools:
            result.available = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"school fallback skipped for listing {property.listing_key}: {e}")
    return result


async def _get_nearby_amenities_base(db, property: Property, force_refresh: bool = False, owner_id: Optional[str] = None) -> NearbyAmenitiesResponse:
    """Never raises - on any failure (missing key, network error, bad response), falls back to a
    stale cache if one exists, else returns available=False so the PDF just omits the section."""
    global _warned_missing_key

    assigned_schools = await _fetch_assigned_schools(property, db)

    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        if not _warned_missing_key:
            logger.warning("GOOGLE_PLACES_API_KEY not set - nearby amenities will be unavailable on all Cheat Sheets.")
            _warned_missing_key = True
        cached = _cached_response(property)
        if cached:
            cached.schools = assigned_schools
            return cached
        result = NearbyAmenitiesResponse(schools=assigned_schools, available=bool(assigned_schools), cached_at=datetime.now(timezone.utc))
        if assigned_schools:
            await _persist_amenities_cache(db, property, result, google_failed=True)
        return result

    lookup_lat = property.google_rooftop_latitude if property.google_rooftop_latitude is not None else property.latitude
    lookup_lng = property.google_rooftop_longitude if property.google_rooftop_longitude is not None else property.longitude

    if lookup_lat is None or lookup_lng is None:
        cached = _cached_response(property)
        if cached:
            cached.schools = assigned_schools
            return cached
        result = NearbyAmenitiesResponse(schools=assigned_schools, available=bool(assigned_schools), cached_at=datetime.now(timezone.utc))
        if assigned_schools:
            await _persist_amenities_cache(db, property, result, google_failed=True)
        return result

    if not force_refresh:
        cached = _cached_response(property)
        if cached and property.nearby_amenities_cached_at:
            cached_at = property.nearby_amenities_cached_at
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - cached_at < timedelta(days=CACHE_TTL_DAYS):
                logger.info(f"[CHEAT SHEET COST] listing_key={property.listing_key} CACHE HIT - live Google cost=$0.000 (served from cache)")
                return cached

    # Only a home's FIRST lookup counts toward the agent's AI usage. A re-lookup because we changed
    # the cheat sheet's data format (AMENITIES_SCHEMA_VERSION - every release that bumps it would
    # otherwise re-charge every agent ~$0.30 per home), the 180-day refresh, or a forced refresh is
    # our doing, not theirs - it stays on the platform budget like any sync call.
    charge_to = owner_id if not property.nearby_amenities and not force_refresh else None
    try:
        async with httpx.AsyncClient() as client:
            result = await _fetch_live(client, api_key, lookup_lat, lookup_lng, property, db, owner_id=charge_to)
    except Exception as e:
        logger.warning(f"Nearby amenities lookup failed for listing {property.listing_key}: {e}")
        cached = _cached_response(property)
        if cached:
            cached.schools = assigned_schools
            return cached
        result = NearbyAmenitiesResponse(schools=assigned_schools, available=bool(assigned_schools), cached_at=datetime.now(timezone.utc))
        if assigned_schools:
            await _persist_amenities_cache(db, property, result, google_failed=True)
        return result

    await _persist_amenities_cache(db, property, result)

    return result
