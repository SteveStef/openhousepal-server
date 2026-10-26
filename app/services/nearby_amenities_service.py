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
from app.models.database import ExploreNearbyCache, Property
from app.models.property import ExploreNearbyPlace, ExploreNearbyPlaceDetails, ExploreNearbyResponse, NearbyAmenitiesResponse, NearbyPlace
from app.services.ai.ai_config_service import ai_config_service
from app.services.areas.boundary_service import nice_school
from app.services.nces_school_assignment_service import resolve_school_assignment
from app.services.usage_budget_service import BudgetExceeded, reserve as reserve_budget, reserve_bucket, usage_snapshot

logger = get_logger(__name__)

# --- Temporary live-cost instrumentation (for real-world cost verification testing) ---
# Not part of the Cheat Sheet's stored/displayed data - just logs what a real generation cost.
PLACES_SEARCH_COST_USD = 0.032  # Nearby Search Pro / Text Search Pro, per call
ROUTES_MATRIX_ELEMENT_COST_USD = 0.005  # Compute Route Matrix Essentials (no traffic-aware routing requested), billed per element
_cost_log_var: "contextvars.ContextVar[Optional[list[tuple[str, float]]]]" = contextvars.ContextVar("_nearby_cost_log", default=None)


# Next-best options per Cheat Sheet list, already through every rule - what the AI proofread
# (cheat_sheet_proofread_service) refills a slot from when it removes a wrong item. Set per _fetch_live.
_alternates_var: "contextvars.ContextVar[Optional[dict[str, list]]]" = contextvars.ContextVar("_nearby_alternates", default=None)
ALTERNATES_PER_LIST = 2
PROOFREAD_RENAME_MAX_MILES = 0.3    # within this, the rename keeps the item's spot
PROOFREAD_RENAME_SEARCH_MILES = 3.0  # how far from the old pin the renamed place may be found
HOSPITAL_BACKUPS = 4                 # more backups for the hospital - the shortest drive picks among them


def _stash_alternates(category: str, places: list, limit: int = ALTERNATES_PER_LIST) -> None:
    store = _alternates_var.get()
    if store is not None:
        store[category] = [p for p in places if p is not None][:limit]


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
AMENITIES_SCHEMA_VERSION = 16
AMENITIES_COMPATIBLE_SCHEMA_VERSIONS = {16}
CACHE_FAILURE_TTL_HOURS = 12
MAX_RESULTS_PER_CATEGORY = 5
DOWNTOWN_MAX_DISTANCE_MILES = 10
DOWNTOWN_CANDIDATE_POOL = 15
DOWNTOWN_CENSUS_EXTRA = 8          # nearest incorporated towns Google didn't list, added as candidates
DOWNTOWN_CORE_HALF_MILES = 0.5     # the square around a town's center point the restaurants and shops are counted in
# Restaurants + shops (max 40) a candidate needs, by what its name is - calibrated 10/25 on 18 real
# homes. Order matters: when two candidates are one place, the earlier kind's name wins.
DOWNTOWN_MIN_SCORE_BY_KIND = {"incorporated": 15, "cdp": 30, "township": 30, "other": 38}
DOWNTOWN_MIN_SEPARATION_MILES = 0.75
DOWNTOWN_MAX_DRIVE_MILES = 15
DOWNTOWN_TIEBREAK_HALF_MILES = 0.2
DOWNTOWN_AI_MIN_SCORE = 10         # restaurants + shops an AI-named downtown still has to show at its point
DOWNTOWN_AI_LOCATE_LOOKUPS = 2     # Google look-ups for AI names that aren't among our candidates
DOWNTOWN_KIND_WORDS = {"incorporated": "official town", "cdp": "census community", "township": "township name", "other": "unofficial name"}
CENSUS_PLACES_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "census_places.json"  # 2024 Census Gazetteer places + townships by state
DEFAULT_SEARCH_RADIUS_MILES = 8

PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places"
ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "places.id,places.name,places.displayName,places.formattedAddress,places.location"
TYPED_FIELD_MASK = FIELD_MASK + ",places.primaryType,places.primaryTypeDisplayName,places.types"
# Cheat Sheet searches also ask whether the place is still open (10/25 grader: closed hospitals and
# stale listings were reaching the sheet) - businessStatus is a Pro field, same price as the rest.
CHEAT_FIELD_MASK = TYPED_FIELD_MASK + ",places.businessStatus"
# Parks add the review count, the only signal that separates a park people actually visit from a
# mapped tree, a youth-league field or an old name that lingers next to the renamed park. It's an
# Enterprise field: $0.035 a call instead of $0.032.
PARK_FIELD_MASK = CHEAT_FIELD_MASK + ",places.userRatingCount"
PLACES_ENTERPRISE_SEARCH_COST_USD = 0.035
EXPLORE_NEARBY_FIELD_MASK = (
    "places.id,places.displayName,places.primaryType,places.primaryTypeDisplayName,places.types,"
    "places.shortFormattedAddress,places.location,places.businessStatus"  # Pro field like the rest - same price
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
    # Giant's Martin's Food Markets (Virginia, West Virginia, central PA) - Winchester's main grocer.
    "martin's", "martins food",
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
# OurAirports' "medium_airport + scheduled service" still includes fields with a handful of commuter
# flights (Lancaster, Wilmington, Trenton) and military fields with a civil terminal (Dover AFB) - the
# 10/25 grader caught one of those listed as a "major airport". So each airport is now classed by the
# FAA's passenger-boarding hub categories: large + medium hubs are a MAJOR AIRPORT; small hubs (real
# daily airline service, fewer routes) are a REGIONAL AIRPORT; everything else is left off.
MAJOR_AIRPORT_IATA = {
    # FAA large hubs
    "ATL", "DFW", "DEN", "ORD", "LAX", "JFK", "LAS", "MCO", "MIA", "CLT", "SEA", "PHX", "EWR", "SFO", "IAH",
    "BOS", "FLL", "MSP", "LGA", "DTW", "PHL", "SLC", "BWI", "DCA", "SAN", "IAD", "TPA", "BNA", "AUS", "MDW",
    "HNL",
    # FAA medium hubs
    "DAL", "PDX", "STL", "HOU", "SMF", "MSY", "RDU", "SJC", "SNA", "MCI", "OAK", "SAT", "RSW", "CLE", "IND",
    "PIT", "CVG", "CMH", "PBI", "JAX", "BDL", "SJU", "OGG", "ONT", "ABQ", "BUR", "MKE", "CHS", "OMA", "RNO",
    "OKC", "MEM", "BOI", "ANC", "BUF", "SDF", "RIC", "TUS",
}
REGIONAL_AIRPORT_IATA = {  # FAA small hubs
    "ABE", "ACY", "ALB", "AMA", "ATW", "AVL", "BGR", "BHM", "BIL", "BTV", "BZN", "CAE", "CAK", "CHA", "CID",
    "COS", "CRP", "DAY", "DSM", "ECP", "ELP", "EUG", "EYW", "FAR", "FAT", "FSD", "FWA", "GEG", "GPT", "GRR",
    "GSO", "GSP", "HPN", "HSV", "ICT", "ILM", "ISP", "JAC", "JAN", "KOA", "LBB", "LEX", "LGB", "LIH", "LIT",
    "MAF", "MDT", "MHT", "MLI", "MSN", "MSO", "MYR", "ORF", "PIA", "PIE", "PNS", "PSP", "PVD", "PWM", "RDM",
    "ROC", "SAV", "SBA", "SBN", "SFB", "SGF", "SRQ", "STT", "STX", "SYR", "TUL", "TYS", "VPS", "XNA",
}
AIRPORT_MAJOR_CANDIDATES = 3      # nearest majors (straight line) sent to the Routes API for real driving ranking
AIRPORT_REGIONAL_CANDIDATES = 2
AIRPORT_MAJOR_MAX_RESULTS = 2
AIRPORT_MAJOR_MAX_DRIVE_MILES = 150  # past this a second major isn't "nearby"; the nearest one always shows
AIRPORT_FALLBACK_MAX_MILES = 40   # how far the local airline airport may be when the hubs are far
AIRPORT_LOCAL_WHEN_HUBS_BEYOND_MILES = 60
# Below small-hub size but with daily airline flights - the local airport where the hubs are far.
# Curated: OurAirports' "scheduled service" flag still lets through fields with none (Ocean City
# Municipal turned up for a Rehoboth Beach home in the 10-town test).
LOCAL_AIRLINE_AIRPORT_IATA = {
    "SCE", "CHO", "HGR", "SBY", "AVP", "IPT", "SHD", "LWB", "MGW", "CKB", "ERI", "LBE", "JST", "DUJ", "BGM",
    "ITH", "ELM", "PHF", "LYH", "ROA", "TTN", "OAJ", "EWN", "PGV", "ACK", "MVY", "HYA", "LEB", "PSM", "BFD",
}
_MILITARY_AIRPORT_RE = re.compile(r"air force base|\bafb\b|army airfield|naval air|joint base", re.IGNORECASE)


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
    # Dollar General's grocery-forward stores - a dollar store, not a supermarket (real 10/25 sheet).
    "dg market", "dollar",
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
    # "Valley Hospitalists, PC" - a doctors' group based at Winchester Medical Center, typed hospital
    # (10-town test, 10/25); "hospital" is inside "hospitalists".
    "hospitalist",
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
# The hospital slot answers "where's the nearest hospital I could go to in an emergency" - a general
# hospital anyone can walk into. These are real hospitals, just not that (10/25 grader, from real
# sheets: "Coatesville VA Medical Center" shown as the hospital, "Kindred Hospital Philadelphia -
# Havertown" beating Bryn Mawr Hospital): VA and military hospitals (veterans / service members),
# long-term acute care and specialty hospitals (no public emergency room), children's, psychiatric,
# cancer, eye and rehab hospitals, and animal hospitals Google sometimes types as "hospital".
HOSPITAL_NOT_GENERAL_NAME_KEYWORDS = [
    "veteran", "va medical", "va hospital", "naval", "air force", "army", "military", "walter reed",
    "kindred", "select specialty", "vibra hospital", "lifecare", "encompass health", "post acute",
    "post-acute", "long term acute", "long-term acute", "ltach", "specialty hospital", "transitional care",
    "children's", "childrens", "shriners", "psychiatric", "state hospital", "behavioral", "hospice",
    "eye hospital", "wills eye", "orthopaedic hospital", "orthopedic hospital", "surgical hospital",
    "heart hospital", "cancer", "skilled nursing", "nursing home", "animal", "pet hospital", "veterinary",
    "vet hospital", "equine", "nemours", "pediatric",
]
# "\bchop\b": the Children's Hospital of Philadelphia's own name for its campuses ("CHOP King of
# Prussia Hospital, Middleman Family Pavilion" - town page check 10/26, no "children's" in it).
_VA_NAME_RE = re.compile(r"\bva\b|\bv\.a\.|\bvamc\b|\bchop\b")
# Medicare's own lists of hospitals that aren't a general hospital - long-term care, inpatient rehab,
# psychiatric, children's, VA and military - keyed by street number|ZIP, minus any address that is also
# a general hospital (rehab and long-term-care units often sit inside one). Catches the ones no name
# list knows: a 10/25 random test had "BridgePoint Hospital Capitol Hill" (a long-term-care hospital)
# as a DC home's hospital. Rebuilt from CMS provider data (xubh-q36u, azum-44iv, 7t8x-u3ir).
NON_GENERAL_HOSPITALS_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "non_general_hospitals.json"
_ADDRESS_NUMBER_RE = re.compile(r"^\s*(\d+)")
_ADDRESS_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?,\s*USA$")


@lru_cache(maxsize=1)
def _load_non_general_hospitals() -> dict[str, str]:
    with open(NON_GENERAL_HOSPITALS_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)["addresses"]


def _address_key(place: dict) -> Optional[str]:
    address = place.get("formattedAddress") or ""
    number, zipcode = _ADDRESS_NUMBER_RE.match(address), _ADDRESS_ZIP_RE.search(address)
    return f"{number.group(1)}|{zipcode.group(1)}" if number and zipcode else None


_NAME_SMALL_WORDS = {"of", "the", "and", "at", "for", "in", "on", "by", "de", "la", "a"}


def _looks_hand_entered(name: str) -> bool:
    """A listing someone typed into Google by hand rather than the place's own: a question mark, or
    mostly lower-case words ("Garrett memorial hospital?" sat on the map next to the real "Garrett
    Regional Medical Center"; "Red mill pond", "ccea" among parks)."""
    if "?" in name:
        return True
    # The name itself, not a tagline after a comma or colon ("Chanticleer, a pleasure garden" - 1,491
    # reviews; "Stoneleigh: a natural garden").
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z']*", re.split(r"[,:]", name)[0]) if w.lower() not in _NAME_SMALL_WORDS]
    if not words:
        return False
    lower = sum(1 for w in words if w[0].islower())
    return lower == len(words) or (len(words) >= 2 and lower * 2 > len(words))
# Medicare's emergency hospitals (CMS Hospital General Information: acute care, critical access and
# rural emergency hospitals reporting an emergency department), each with its map point (Census
# geocoder; Google for the ~15% it couldn't place) and street number|ZIP. Only ever ADDS: a listing at
# one of these addresses counts as a hospital however Google labels it, and one Google missed gets
# looked up by name (_medicare_hospitals_missing) - it never removes Google's own picks, because
# Medicare files a system's satellite campuses under one license (Virtua Marlton, Jefferson
# Washington Township and Union Hospital aren't on it).
ER_HOSPITAL_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "er_hospitals.json"
MEDICARE_CONFIRMED_PRIMARY_TYPES = {"medical_center", "medical_clinic", "health"}
MEDICARE_HOSPITAL_SEARCH_MILES = 25
MEDICARE_HOSPITAL_LOOKUPS = 2
_PRACTICE_SUFFIX_RE = re.compile(r",?\s+(p\.?\s?c\.?|pllc|l\.?l\.?c\.?|inc\.?)\s*$", re.IGNORECASE)
_NAME_WITH_PERSON_RE = re.compile(r":\s*[A-Z][a-z]+\s+[A-Z][a-z]+")


@lru_cache(maxsize=1)
def _load_er_hospitals() -> list[list]:
    with open(ER_HOSPITAL_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)["hospitals"]


def _near_medicare_hospital(place: dict, miles: float = 0.2) -> bool:
    """The listing sits on a Medicare emergency hospital's map point - for the few Medicare records
    without a street number to match ("One Plainsboro Road", Penn Medicine Princeton Medical Center)."""
    loc = place.get("location") or {}
    p_lat, p_lng = loc.get("latitude"), loc.get("longitude")
    if p_lat is None or p_lng is None:
        return False
    return any(abs(h[3] - p_lat) < 0.01 and abs(h[4] - p_lng) < 0.013 and _haversine_miles(p_lat, p_lng, h[3], h[4]) <= miles
               for h in _load_er_hospitals())


@lru_cache(maxsize=1)
def _er_hospital_keys() -> frozenset:
    return frozenset(h[6] for h in _load_er_hospitals() if h[6])


# "VA HOSPITAL 25" / "Building 3" - a numbered building of a campus that's already on the list by name.
_NUMBERED_BUILDING_NAME_RE = re.compile(r"\s\d+$")
_HAS_STREET_NUMBER_RE = re.compile(r"^\s*(?:\d|one\b|two\b|three\b)", re.IGNORECASE)  # "1 Plainsboro Rd", "One Hospital Dr"
# A suite or unit number in the street address: an office in a building, not a hospital (town page check 10/26).
_SUITE_ADDRESS_RE = re.compile(r"^[^,]*(?:#\s*\w+|\b(?:ste|suite|unit|fl|floor)\.?\s+\w+)", re.IGNORECASE)


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
    "neurology", "gastroenterology", "urology", "ophthalm", "eye", "ent", "allergy",
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
MEDICAL_EVERYDAY_MAX_MILES = 15
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
# Private schools come from the federal private-school list (NCES Private School Universe Survey
# 2023-24, every US private school with its grade span, type and enrollment) instead of a Google text
# search for "private schools". The 10/25 grader found preschools shown as private schools with no
# grades ("Little Lights Christian Academy", "Joyful Noise Christian School" - PK-K): Google has no
# grade data, so it can't tell a preschool calling itself an academy from a K-12 school. The federal
# list can, and gives the real grades for the label. Rebuild app/data/private_schools_pss.json from a
# newer survey year when NCES publishes one (columns: PINST, PCITY, PSTABB, LATITUDE24, LONGITUDE24,
# LOGR2024, HIGR2024, TYPOLOGY, NUMSTUDS; keep rows whose highest grade is 1st or above).
PRIVATE_SCHOOL_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "private_schools_pss.json"
PRIVATE_SCHOOL_RADIUS_MILES = 8
PRIVATE_SCHOOL_MIN_HIGHEST_GRADE = 8   # 3rd grade (survey code) - a school that stops at 1st/2nd is an early-childhood center
PRIVATE_SCHOOL_MIN_STUDENTS = 50       # smaller ones (micro-schools, home-school co-ops) only fill in behind real schools
PRIVATE_SCHOOL_SPECIAL_EDUCATION_TYPOLOGY = 9
PRIVATE_SCHOOL_VERIFY_LOOKUPS = 4      # Google look-ups to confirm the picks are still open (and get the proper-cased name)
PRIVATE_SCHOOL_MAX_DRIVE_MILES = 15     # farther by car isn't "nearby" (schools across a river can be 8 mi straight, 25 by car)
PRIVATE_SCHOOL_ESTABLISHED_STUDENTS = 150  # a school this size two years ago is still there - no look-up (saves ~3 cents a pick)
_PSS_GRADE_NAMES = {1: "UNGRADED", 2: "PK", 3: "K", 4: "K", 5: "1"}
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
    # "Gettysburg Auto Tour Stop 8" - a stop inside a park, not the park (10-town test).
    "tour stop", "auto tour",
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
    # Clubs and leagues Google types as park - a sports organization or a members' club, not a park
    # (a real sheet showed "Camden-Wyoming Little League" as a park).
    "little league", "athletic association", "youth association", "sports association", "swim club",
    "country club", "golf club", "anglers", "kennel",
]
# Preserves and state parks count - often the best outdoor places near a home (Okehocking Preserve,
# Tall Pines State Preserve came back typed this way, not "park").
PARK_PRIMARY_TYPES = {"park", "state_park", "national_park", "nature_preserve", "wildlife_refuge", "botanical_garden"}
PARK_POPULAR_RADIUS_MILES = 5
PARK_MIN_REVIEWS = 10
PARK_MAX_RESULTS = 3
PARK_HOUSE_ADDRESS_MIN_REVIEWS = 25   # a "park" at a house number on a residential court needs this many to count
PARK_NO_ADDRESS_MIN_REVIEWS = 50      # one with no street address and no park word in its name needs this many
PARK_SCORE_MILES_PER_POINT = 1.5   # in the "worth the drive" ranking, 10x the reviews is worth 1.5 extra miles
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


@lru_cache(maxsize=1)
def _load_private_schools() -> list[list]:
    with open(PRIVATE_SCHOOL_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)["schools"]


def _pss_grade_name(code: int) -> str:
    return _PSS_GRADE_NAMES.get(code, str(code - 5))


def _private_school_label(lowest: int, highest: int, typology: int) -> str:
    """The sheet's tag line for a private school: its real grade span from the federal survey
    ("GRADES PK-8", "GRADES 9-12"), flagged when it's a special-education school."""
    low, high = _pss_grade_name(lowest), _pss_grade_name(highest)
    grades = f"GRADES {low}-{high}" if low != high else f"GRADE {high}"
    if typology == PRIVATE_SCHOOL_SPECIAL_EDUCATION_TYPOLOGY:
        return f"SPECIAL EDUCATION · {grades}"
    return grades


def _private_school_candidates(lat: float, lng: float) -> list[dict]:
    """Federal-survey private schools within PRIVATE_SCHOOL_RADIUS_MILES that teach past 2nd grade,
    most useful first: regular schools of real size by distance, then small ones, then special-
    education schools (a genuine school, but not the general pick a typical buyer is asking about)."""
    lat_pad = PRIVATE_SCHOOL_RADIUS_MILES / 69.0
    lng_pad = PRIVATE_SCHOOL_RADIUS_MILES / max(1.0, 69.0 * math.cos(math.radians(lat)))
    out: list[tuple[int, float, dict]] = []
    for name, city, state, s_lat, s_lng, lowest, highest, typology, students in _load_private_schools():
        if abs(s_lat - lat) > lat_pad or abs(s_lng - lng) > lng_pad:
            continue
        if highest < PRIVATE_SCHOOL_MIN_HIGHEST_GRADE or lowest == highest:
            continue
        distance = _haversine_miles(lat, lng, s_lat, s_lng)
        if distance > PRIVATE_SCHOOL_RADIUS_MILES:
            continue
        if typology == PRIVATE_SCHOOL_SPECIAL_EDUCATION_TYPOLOGY:
            tier = 2
        else:
            tier = 0 if students >= PRIVATE_SCHOOL_MIN_STUDENTS else 1
        out.append((tier, distance, {
            "name": name, "city": city, "state": state, "lat": s_lat, "lng": s_lng,
            "label": _private_school_label(lowest, highest, typology), "students": students,
        }))
    out.sort(key=lambda item: (item[0], item[1]))
    return [school for _, _, school in out]


def _private_school_display_name(survey_name: str) -> str:
    """The survey's all-caps name as people write it: "SS COLMAN-JOHN NEUMANN SCHOOL" -> "Saints
    Colman-John Neumann School", "ST MARY MAGDALEN PARISH" -> "St. Mary Magdalen Parish School"."""
    name = nice_school(survey_name, all_caps=True)
    name = re.sub(r"^(?:ss|sts)\.?(?=\s)", "Saints", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(st|mt)\b(?!\.)", lambda m: m.group(1).capitalize() + ".", name, flags=re.IGNORECASE)
    if not _SCHOOL_WORD_RE.search(name):
        name += " School"
    return name


_SCHOOL_WORD_RE = re.compile(r"school|academy|prep|institute|montessori|college|seminary|learning|education|yeshiva", re.IGNORECASE)


_SCHOOL_MATCH_STOPWORDS = {"the", "school", "academy", "of", "and", "st", "saint", "sts", "saints", "ss"}


def _school_name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ")) if t not in _SCHOOL_MATCH_STOPWORDS}


def _google_place_matches_school(place: dict, school: dict) -> bool:
    """A Google result is the same school as a federal-survey row when it sits within half a mile
    and shares most of the name's distinctive words ("SS SIMON AND JUDE SCHOOL" = "Saints Simon and
    Jude School")."""
    location = place.get("location") or {}
    if location.get("latitude") is None or location.get("longitude") is None:
        return False
    if _haversine_miles(school["lat"], school["lng"], location["latitude"], location["longitude"]) > 0.5:
        return False
    ours, theirs = _school_name_tokens(school["name"]), _school_name_tokens(_place_name(place))
    if not ours or not theirs:
        return False
    return len(ours & theirs) / len(ours) >= 0.6


def _place_name(place: dict) -> str:
    return ((place.get("displayName") or {}).get("text") or "").strip()


def _is_open(place: dict) -> bool:
    """False for a place Google marks closed (permanently or for now). A result fetched without
    businessStatus in its field mask counts as open."""
    return (place.get("businessStatus") or "OPERATIONAL") == "OPERATIONAL"


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


_HOUSE_STREET_SUFFIX_RE = re.compile(r"^\d+\s+[\w\s.'-]*\b(ct|court|cir|circle|ter|terrace|pl|place|cv|cove)\b", re.IGNORECASE)
_PARK_NAME_WORD_RE = re.compile(
    r"\b(park|parks|preserve|reserve|playground|field|fields|green|garden|gardens|arboretum|trail|forest|"
    r"recreation|rec|commons|common|square|plaza|beach|lake|pond|woods|wildlife|nature|conservation|"
    r"refuge|sanctuary|grove|meadow|meadows|landing|pier|wharf|marsh|creek|river|island|falls|mall|"
    r"memorial|monument|grounds|reservation|battlefield|overlook)\b", re.IGNORECASE)


def _looks_like_a_house_or_subdivision(place: dict) -> bool:
    """10/25 random test: "Mammal Park" was a house (6107 Koala Ct, 11 reviews) and "Academy Hill" a
    subdivision (no street address, no park word in its name, 20 reviews). A park on a residential
    court with few reviews, or with no street address, no park word and few reviews, is one of these."""
    address = (place.get("formattedAddress") or "").strip()
    reviews = _park_reviews(place)
    if _HOUSE_STREET_SUFFIX_RE.match(address) and reviews < PARK_HOUSE_ADDRESS_MIN_REVIEWS:
        return True
    no_street = not re.match(r"^\d", address)
    plain = not set(place.get("types") or []) & _PARK_RICH_DESCRIPTOR_TYPES
    return no_street and plain and not _PARK_NAME_WORD_RE.search(_place_name(place)) and reviews < PARK_NO_ADDRESS_MIN_REVIEWS


def _is_parent_park_candidate(place: dict) -> bool:
    name = _place_name(place).lower()
    primary_type = _place_primary_type(place)
    if not name or primary_type not in PARK_PRIMARY_TYPES:
        return False
    if _looks_hand_entered(_place_name(place)) or _looks_like_a_house_or_subdivision(place):
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


def _is_legitimate_full_service_hospital(place: dict, medicare_confirmed: bool = False) -> bool:
    """A general hospital a buyer could go to in an emergency. `medicare_confirmed` (or the listing
    sitting at a Medicare emergency hospital's street address, ER_HOSPITAL_DATA_PATH) lets through the
    real hospitals Google labels oddly - "Winchester Medical Center" and "Meritus Medical Center" typed
    medical_center, "Beebe Healthcare" with no hospital word in its name - which the name test below
    turned away (10-town test, 10/25: Rehoboth, Hagerstown and Winchester all lost their hospital)."""
    name_raw = _place_name(place)
    name_lower = name_raw.lower()
    primary_type = _place_primary_type(place)
    types = set(place.get("types") or [])
    if not name_lower:
        return False
    medicare = medicare_confirmed or _address_key(place) in _er_hospital_keys()
    if primary_type not in HOSPITAL_PRIMARY_TYPES and not (medicare and primary_type in MEDICARE_CONFIRMED_PRIMARY_TYPES):
        return False
    if _PRACTICE_SUFFIX_RE.search(name_raw) or _NAME_WITH_PERSON_RE.search(name_raw):
        return False  # "Valley Hospitalists, PC", "Beebe Medical Center: Mangrelli Meghan A" - a practice or a doctor at the hospital
    if types & MEDICAL_EXCLUDE_TYPES:
        return False
    if any(keyword in name_lower for keyword in HOSPITAL_FALSE_POSITIVE_NAME_KEYWORDS):
        return False
    if any(keyword in name_lower for keyword in HOSPITAL_NOT_GENERAL_NAME_KEYWORDS) or _VA_NAME_RE.search(name_lower):
        return False
    if _NUMBERED_BUILDING_NAME_RE.search(name_raw) or not _is_open(place):
        return False
    if not medicare and _SUITE_ADDRESS_RE.search(place.get("formattedAddress") or ""):
        return False  # "Medical Center of Princeton, 88 Princeton Hightstown Rd #202" - an office suite, typed general_hospital
    if not medicare and not _HAS_STREET_NUMBER_RE.match(place.get("formattedAddress") or "") and not _near_medicare_hospital(place):
        # "University of Maryland Shore Regional Medical Center, Easton, MD 21601" - a hospital still being
        # built (opens 2028) that Google already lists as open: no street address yet (town page check 10/26).
        return False
    if _looks_hand_entered(name_raw) or _address_key(place) in _load_non_general_hospitals():
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
    if not medicare and not any(keyword in name_lower for keyword in HOSPITAL_FACILITY_IDENTITY_KEYWORDS):
        return False
    return True


def _non_hospital_medical_label(place: dict) -> Optional[str]:
    name_raw = _place_name(place)
    name_lower = name_raw.lower()
    primary_type = _place_primary_type(place)
    types = set(place.get("types") or [])
    if not name_lower or primary_type in HOSPITAL_PRIMARY_TYPES or not _is_open(place):
        return None
    if primary_type in MEDICAL_EXCLUDE_TYPES or types & MEDICAL_EXCLUDE_TYPES:
        return None
    if any(keyword in name_lower for keyword in NON_HOSPITAL_MEDICAL_EXCLUDE_NAME_KEYWORDS):
        return None
    if "veteran" in name_lower or _VA_NAME_RE.search(name_lower):
        return None  # a VA clinic only sees veterans
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


def _valid_places_sorted(raw_places: list[dict], lat: float, lng: float, predicate, label: str) -> list[NearbyPlace]:
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
    return candidates


def _nearest_valid_place(raw_places: list[dict], lat: float, lng: float, predicate, label: str) -> Optional[NearbyPlace]:
    found = _valid_places_sorted(raw_places, lat, lng, predicate, label)
    return found[0] if found else None


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


def _search_cost(field_mask: str) -> float:
    return PLACES_ENTERPRISE_SEARCH_COST_USD if "userRatingCount" in field_mask or "rating" in field_mask else PLACES_SEARCH_COST_USD


def _place_to_nearby(place: dict, origin_lat: float, origin_lng: float) -> Optional[NearbyPlace]:
    name = (place.get("displayName") or {}).get("text")
    if not name:
        return None
    name = re.split(r"\s+\|\s+", name)[0]  # "Roxborough Memorial Hospital | Philadelphia" - an SEO suffix, not the name
    name = name.strip(" ?") or name  # "Garrett memorial hospital?" - a stray question mark in Google's listing
    if name == name.lower():
        name = name.title()  # "vybe urgent care", "ccea" - typed in all lower case on Google
    location = place.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    distance = _haversine_miles(origin_lat, origin_lng, lat, lng) if lat is not None and lng is not None else None
    return NearbyPlace(name=name, address=place.get("formattedAddress"), distance_miles=round(distance, 1) if distance is not None else None,
                       latitude=lat, longitude=lng)


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
    _log_billable_call(section, _search_cost(field_mask))
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
    _log_billable_call(section, _search_cost(field_mask))
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
    _log_billable_call(section, _search_cost(field_mask))
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
    if not _is_open(place):
        return False  # closed for good or for now (the Cheat Sheet's same rule, 10/26 town page check)
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
    if category_key == "parks" and (_looks_hand_entered(_place_name(place)) or _is_likely_subdivision_named_park(place)):
        return False   # "Red mill pond", "Academy Hill" - the Cheat Sheet's park rules
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


def _major_grocery_brand(name: str) -> Optional[str]:
    """The big-name grocer in a store's name. "Grocery Outlet" counts only as the chain's own name -
    "Sharp Shopper Grocery Outlet" is a surplus store (10-town test, Winchester)."""
    name = name.lower()
    for brand in MAJOR_GROCERY_BRANDS:
        if brand in name and (brand != "grocery outlet" or name.startswith(brand)):
            return brand
    return None


def _explore_grocery_ok(place: dict) -> bool:
    """A real grocery store (user, 10/24): one of the big names, or a genuine supermarket - never a
    corner shop, deli, convenience store or specialty market."""
    if not _is_full_service_grocery(place):
        return False
    name = _place_name(place).lower()
    if any(p in name for p in CONVENIENCE_GAS_HYBRID_NAME_PATTERNS):
        return False
    if _major_grocery_brand(name):
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
    if types & {"gas_station", "convenience_store"}:
        # "Himalayan Market" was typed supermarket AND gas_station - a gas-station store (10/25 test).
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
        if not _is_full_service_grocery(raw) or not _is_open(raw):
            continue
        nearby = _place_to_nearby(raw, lat, lng)
        if nearby is None:
            continue
        pairs.append((raw, nearby))
    pairs.sort(key=lambda pair: pair[1].distance_miles if pair[1].distance_miles is not None else float("inf"))
    return pairs


def _grocery_brand_key(place: dict) -> str:
    """"ShopRite of Sewell" and "ShopRite of Glassboro" are one brand - the sheet shows two different
    stores to choose from, not the same chain twice (10/25: "Trader Joe's | Trader Joe's")."""
    name = _place_name(place).lower()
    brand = _major_grocery_brand(name)
    if brand:
        return brand.replace(" ", "").replace("-", "")
    return re.split(r"\s+(?:of|at|-|–)\s+", name)[0].strip()


def _select_grocery_pair(pairs: list[tuple[dict, NearbyPlace]]) -> list[NearbyPlace]:
    """Up to 2 nearest-first stores from different brands, real supermarkets first (a big-name
    grocer or a store Google types supermarket - see _explore_grocery_ok); a farm market, specialty
    or ethnic market only fills a slot when no second real supermarket is in the pool."""
    if not pairs:
        return []
    ranked = sorted(pairs, key=lambda pair: (0 if _explore_grocery_ok(pair[0]) else 1, pair[1].distance_miles if pair[1].distance_miles is not None else float("inf")))
    selected: list[tuple[dict, NearbyPlace]] = []
    spare: list[NearbyPlace] = []
    brands: set[str] = set()
    for pair in ranked:
        brand = _grocery_brand_key(pair[0])
        if brand in brands:
            continue
        brands.add(brand)
        if len(selected) < 2:
            selected.append(pair)
        elif _explore_grocery_ok(pair[0]):
            spare.append(pair[1])
    _stash_alternates("grocery_stores", spare)
    selected.sort(key=lambda pair: pair[1].distance_miles if pair[1].distance_miles is not None else float("inf"))
    return [nearby for _, nearby in selected]


def _real_supermarket_brands(pairs: list[tuple[dict, NearbyPlace]]) -> int:
    return len({_grocery_brand_key(raw) for raw, _ in pairs if _explore_grocery_ok(raw)})


async def _fetch_grocery_stores(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """supermarket and grocery_store are searched together in one call per radius tier (tested
    against 3 real locations - identical final picks to searching them separately, since both
    types share the same 20-result cap either way and the selection logic below is unaffected by
    which single call the raw pool came from). Widens the radius until two different real
    supermarkets are in the pool."""
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
            field_mask=CHEAT_FIELD_MASK,
            section="grocery",
        )
        for place in raw_places:
            key = (_place_name(place).lower(), place.get("formattedAddress") or "")
            if key not in seen_ids:
                seen_ids.add(key)
                collected.append(place)
        pairs = _sorted_valid_grocery_candidates(collected, lat, lng)
        if _real_supermarket_brands(pairs) >= 2:
            return _select_grocery_pair(pairs)
    return _select_grocery_pair(_sorted_valid_grocery_candidates(collected, lat, lng))


async def _fetch_medical_care(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """The nearest general hospital (see _is_legitimate_full_service_hospital), plus the nearest
    urgent care - one text search; a doctor's office / clinic search runs only when there's no urgent
    care around. (10/25: a federal Medicare hospital list was tried for the hospital and dropped - it
    files a health system's satellite campuses under one license, so Virtua Marlton, Jefferson
    Washington Township and Union Hospital weren't on it and much farther hospitals were picked.)"""
    results: list[NearbyPlace] = []
    hospitals = await _fetch_hospital_from_google(client, api_key, lat, lng)
    hospital, hospital_spare = (hospitals[0], hospitals[1:]) if hospitals else (None, [])
    if hospital is not None:
        results.append(hospital)
    hospital_key = hospital.name.strip().lower() if hospital else None

    def best_everyday_care(raw: list[dict]) -> Optional[NearbyPlace]:
        candidates: list[tuple[int, NearbyPlace]] = []
        seen: set[tuple[str, str, str]] = set()
        for place in raw:
            label = _non_hospital_medical_label(place)
            nearby = _place_to_nearby(place, lat, lng) if label else None
            if not nearby or nearby.name.strip().lower() == hospital_key or _place_dedupe_key(place) in seen:
                continue
            if nearby.distance_miles is not None and nearby.distance_miles > MEDICAL_EVERYDAY_MAX_MILES:
                continue
            seen.add(_place_dedupe_key(place))
            nearby.place_type = label
            candidates.append((0 if label == "URGENT CARE" else 1 + _non_hospital_medical_rank(label, place), nearby))
        candidates.sort(key=lambda pair: (pair[0], pair[1].distance_miles if pair[1].distance_miles is not None else float("inf")))
        everyday_spare[:] = [n for _, n in candidates[1:]]
        return candidates[0][1] if candidates else None

    everyday_spare: list[NearbyPlace] = []
    everyday = best_everyday_care(await _search_text(client, api_key, lat, lng, "urgent care", DEFAULT_SEARCH_RADIUS_MILES, field_mask=CHEAT_FIELD_MASK, section="medical_urgent_care"))
    if everyday is None:
        everyday = best_everyday_care(await _search_nearby_types(
            client, api_key, lat, lng, ["medical_clinic", "doctor", "medical_center"], max_result_count=NEARBY_CANDIDATE_POOL,
            radius_miles=DEFAULT_SEARCH_RADIUS_MILES, field_mask=CHEAT_FIELD_MASK, section="medical_non_hospital",
        ))
    if everyday is not None:
        results.append(everyday)
    _stash_alternates("medical_care:hospital", hospital_spare, HOSPITAL_BACKUPS)
    _stash_alternates("medical_care:everyday", everyday_spare)
    return results


async def _fetch_hospital_from_google(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Real general hospitals, nearest first (the first is the sheet's; the rest are its backups).
    Google's hospital search, widening the radius until at least two turn up, hospital types only, in a
    call of its own so the far more numerous doctor/clinic listings never crowd real hospitals out of
    the 20-result cap - then any Medicare emergency hospital closer than those that Google's 20 left
    out is looked up by name (UM Shore Medical Center at Easton never made the list for an Easton
    home: twenty clinics typed "hospital" filled it). One entry per hospital address."""
    found: list[NearbyPlace] = []
    for radius in [DEFAULT_SEARCH_RADIUS_MILES, 12, 16, 24]:
        raw = await _search_nearby_types(
            client, api_key, lat, lng, ["general_hospital", "hospital"], max_result_count=NEARBY_CANDIDATE_POOL,
            radius_miles=radius, field_mask=CHEAT_FIELD_MASK, section="medical_hospital",
        )
        found = _one_per_hospital_address(raw, lat, lng)
        # Two to compare by drive: a lone nearest hospital can be across a river (Bryans Road MD: Inova
        # Mount Vernon, 24 miles by car, while UM Charles Regional sat just past the first ring).
        if len(found) >= 2:
            break
    found += await _medicare_hospitals_missing(client, api_key, lat, lng, found)
    found.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    return found


def _one_per_hospital_address(raw: list[dict], lat: float, lng: float, medicare_confirmed: bool = False) -> list[NearbyPlace]:
    """Valid hospitals, nearest first, one per street address - "Meritus Medical Center" and "Meritus
    Health" are two listings for one hospital; the name that says hospital / medical center wins."""
    best: dict[str, tuple[int, dict]] = {}
    for place in raw:
        if not _is_legitimate_full_service_hospital(place, medicare_confirmed):
            continue
        key = _address_key(place) or _place_identity(place) or _place_name(place).lower()
        rank = 0 if any(k in _place_name(place).lower() for k in HOSPITAL_FACILITY_IDENTITY_KEYWORDS) else 1
        if key not in best or rank < best[key][0]:
            best[key] = (rank, place)
    out = []
    for _, place in best.values():
        nearby = _place_to_nearby(place, lat, lng)
        if nearby:
            nearby.place_type = "HOSPITAL"
            out.append(nearby)
    out.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    return out


async def _medicare_hospitals_missing(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, found: list[NearbyPlace]) -> list[NearbyPlace]:
    nearest_found = found[0].distance_miles if found and found[0].distance_miles is not None else float("inf")
    pad_lat = MEDICARE_HOSPITAL_SEARCH_MILES / 69.0
    pad_lng = MEDICARE_HOSPITAL_SEARCH_MILES / max(1.0, 69.0 * math.cos(math.radians(lat)))
    nearby = sorted(
        ((_haversine_miles(lat, lng, h[3], h[4]), h) for h in _load_er_hospitals() if abs(h[3] - lat) <= pad_lat and abs(h[4] - lng) <= pad_lng),
        key=lambda item: item[0],
    )
    added: list[NearbyPlace] = []
    lookups = 0
    for miles, (name, city, state, h_lat, h_lng, _kind, _key) in nearby:
        if miles > min(MEDICARE_HOSPITAL_SEARCH_MILES, nearest_found) or lookups >= MEDICARE_HOSPITAL_LOOKUPS:
            break
        if any(p.latitude is not None and _haversine_miles(h_lat, h_lng, p.latitude, p.longitude) <= 0.35 for p in [*found, *added]):
            continue  # Google already has it
        lookups += 1
        try:
            raw = await _search_text(client, api_key, h_lat, h_lng, f"{nice_school(name, all_caps=True)}, {city}, {state}", 1.0,
                                     field_mask=CHEAT_FIELD_MASK, section="medical_hospital_medicare")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Medicare hospital look-up failed for {name}: {e}")
            continue
        close = [p for p in raw[:5] if (p.get("location") or {}).get("latitude") is not None
                 and _haversine_miles(h_lat, h_lng, p["location"]["latitude"], p["location"]["longitude"]) <= 0.35]
        added += _one_per_hospital_address(close, lat, lng, medicare_confirmed=True)[:1]
    return added


async def _fetch_private_schools(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """Up to two nearby private schools a typical buyer would weigh, each with its real grades.

    Picks come from the federal private-school list (see PRIVATE_SCHOOL_DATA_PATH), most useful
    first. Smaller picks (under PRIVATE_SCHOOL_ESTABLISHED_STUDENTS) are looked up on Google: a school
    Google marks closed is skipped for the next one, and a match gives the name as the school writes it
    (the survey is all capitals). A school Google can't find stays, under the survey's name."""
    results: list[NearbyPlace] = []
    spare: list[NearbyPlace] = []
    lookups = 0
    for school in _private_school_candidates(lat, lng):
        if len(results) >= PRIVATE_SCHOOL_MAX_RESULTS:
            if len(spare) < ALTERNATES_PER_LIST and school["label"] and not school["label"].startswith("SPECIAL"):
                spare.append(NearbyPlace(name=_private_school_display_name(school["name"]), address=f"{school['city']}, {school['state']}",
                                         place_type=school["label"], latitude=school["lat"], longitude=school["lng"],
                                         distance_miles=round(_haversine_miles(lat, lng, school["lat"], school["lng"]), 1)))
                continue
            break
        name = _private_school_display_name(school["name"])
        match: Optional[dict] = None
        if lookups < PRIVATE_SCHOOL_VERIFY_LOOKUPS and school["students"] < PRIVATE_SCHOOL_ESTABLISHED_STUDENTS:
            lookups += 1
            try:
                raw = await _search_text(client, api_key, school["lat"], school["lng"], f"{name}, {school['city']}, {school['state']}", 1.0, field_mask=CHEAT_FIELD_MASK, section="private_schools")
                match = next((p for p in raw[:5] if _google_place_matches_school(p, school)), None)
            except Exception as e:  # noqa: BLE001 - the survey row is good enough on its own
                logger.warning(f"private school check failed for {name}: {e}")
        if match is not None and not _is_open(match):
            continue
        google_name = _place_name(match) if match is not None else ""
        # Google's spelling, unless the match is the parish/church listing next door ("St Mary
        # Magdalen Parish") rather than the school's own, or typed in all lower case.
        if google_name and _SCHOOL_WORD_RE.search(google_name) and google_name != google_name.lower():
            name = google_name
        if any(r.name.lower() == name.lower() for r in results):
            continue
        address = (match or {}).get("formattedAddress") or f"{school['city']}, {school['state']}"
        results.append(NearbyPlace(
            name=name, address=address, place_type=school["label"],
            distance_miles=round(_haversine_miles(lat, lng, school["lat"], school["lng"]), 1),
            latitude=school["lat"], longitude=school["lng"],
        ))
    _stash_alternates("private_schools", spare)
    return results


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
    """The nearest park people actually use, plus the best-known parks and preserves close by.

    Two pools: the 20 nearest parks (rankPreference=DISTANCE - Google's POPULARITY default hides close
    neighborhood parks behind a metro's famous ones, see _search_nearby) and the 20 most popular within
    PARK_POPULAR_RADIUS_MILES (the DISTANCE pool alone is all pocket parks in a dense suburb and never
    reaches a Ridley Creek State Park or a Rose Tree Park 2 miles away). Review count is the quality
    bar (10/25 grader: a mapped tree "Chinese chestnut 2", 0-review pocket parks and a stale park name
    were on real sheets) - see _park_review_floor."""
    distance_pool = await _search_nearby(
        client, api_key, lat, lng, "park", max_result_count=NEARBY_CANDIDATE_POOL,
        field_mask=PARK_FIELD_MASK, section="parks", rank_preference="DISTANCE",
    )
    popular_pool = await _search_nearby(
        client, api_key, lat, lng, "park", radius_miles=PARK_POPULAR_RADIUS_MILES,
        max_result_count=NEARBY_CANDIDATE_POOL, field_mask=PARK_FIELD_MASK, section="parks_popular",
    )
    deduped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for place in [*distance_pool, *popular_pool]:
        if not _is_parent_park_candidate(place) or not _is_open(place):
            continue
        key = _place_dedupe_key(place)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(place)
    deduped = _dedupe_park_sub_locations(deduped, lat, lng)
    deduped = _drop_park_sub_areas(deduped)

    floor = _park_review_floor(deduped)
    pairs = [(p, n) for p in deduped if _park_reviews(p) >= floor and (n := _place_to_nearby(p, lat, lng)) is not None]
    if not pairs:
        return []
    pairs.sort(key=lambda pair: pair[1].distance_miles if pair[1].distance_miles is not None else float("inf"))
    nearest, rest = pairs[0], pairs[1:]
    # Then the parks worth the drive: popularity (log of reviews) against distance.
    rest.sort(key=lambda pair: -(math.log10(_park_reviews(pair[0]) + 1) - (pair[1].distance_miles or 0) / PARK_SCORE_MILES_PER_POINT))
    picked = [nearest, *rest[:PARK_MAX_RESULTS - 1]]
    _stash_alternates("parks", [n for _, n in rest[PARK_MAX_RESULTS - 1:]])
    picked.sort(key=lambda pair: pair[1].distance_miles if pair[1].distance_miles is not None else float("inf"))
    return [nearby for _, nearby in picked]


def _park_reviews(place: dict) -> int:
    return int(place.get("userRatingCount") or 0)


def _park_review_floor(candidates: list[dict]) -> int:
    """PARK_MIN_REVIEWS where enough parks clear it; in rural areas, where even a real park has a
    handful of reviews, the bar drops so the section isn't empty - but never to zero-review entries."""
    for floor in (PARK_MIN_REVIEWS, 3, 1):
        if sum(1 for p in candidates if _park_reviews(p) >= floor) >= PARK_MAX_RESULTS:
            return floor
    return 1


def _drop_park_sub_areas(candidates: list[dict]) -> list[dict]:
    """"Cedar Lake Park at Washington Lake Park" or "Ridley Creek State Park - Area 3" when the park
    itself is in the list - one entry per park. Also two entries within ~500 ft of each other (the
    same park listed twice): the better-known one stays."""
    names = {_place_name(p).lower() for p in candidates}

    def is_sub_area(name: str) -> bool:
        return any(other != name and (name.endswith(f" at {other}") or any(name.startswith(other + sep) for sep in (" - ", " – ", ", ", " area ")))
                   for other in names)

    kept = [p for p in candidates if not is_sub_area(_place_name(p).lower())]
    kept.sort(key=lambda p: -_park_reviews(p))
    out: list[dict] = []
    for place in kept:
        loc = place.get("location") or {}
        if loc.get("latitude") is not None and any(
            _haversine_miles(loc["latitude"], loc["longitude"], (o.get("location") or {}).get("latitude", 0), (o.get("location") or {}).get("longitude", 0)) < 0.1
            for o in out
        ):
            continue
        out.append(place)
    return out


async def _driving_routes(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, points: list[tuple[float, float]], section: str) -> list[Optional[tuple[float, float]]]:
    """(drive seconds, drive miles) from the home to each point, one Routes API call for all of them;
    None for a point with no route. Best effort: any failure returns all None and callers keep
    straight-line distances."""
    if not points:
        return []
    try:
        response = await client.post(
            ROUTES_MATRIX_URL,
            json={
                "origins": [{"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}],
                "destinations": [{"waypoint": {"location": {"latLng": {"latitude": p_lat, "longitude": p_lng}}}} for p_lat, p_lng in points],
                "travelMode": "DRIVE",
            },
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": ROUTES_FIELD_MASK},
            timeout=10.0,
        )
        response.raise_for_status()
        _log_billable_call(section, len(points) * ROUTES_MATRIX_ELEMENT_COST_USD)
        routes: list[Optional[tuple[float, float]]] = [None] * len(points)
        for row in response.json():
            idx = row.get("destinationIndex", 0)
            if row.get("condition") != "ROUTE_EXISTS" or row.get("distanceMeters") is None or not 0 <= idx < len(points):
                continue
            routes[idx] = (_parse_duration_seconds(row.get("duration")), round(_meters_to_miles(row["distanceMeters"]), 1))
        return routes
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Driving distances ({section}) failed, keeping straight-line: {e}")
        return [None] * len(points)


async def _fetch_airports(client: httpx.AsyncClient, api_key: str, lat: float, lng: float) -> list[NearbyPlace]:
    """The nearest MAJOR AIRPORTs (FAA large/medium hubs, up to two) plus a REGIONAL AIRPORT (FAA small
    hub) only when it's a shorter drive than the nearest major - see MAJOR_AIRPORT_IATA. Commuter and
    military fields are never shown. Sourced from the curated OurAirports dataset (AIRPORT_DATA_PATH),
    ranked by driving miles - not drive time, which swings with where the airport's pin sits on the
    airfield (Reagan National ranked ahead of Dulles, 15 miles closer, for a Sterling VA home)."""
    airports = _load_major_airports()
    by_distance = sorted(airports, key=lambda a: _haversine_miles(lat, lng, a["lat"], a["lon"]))
    majors = [a for a in by_distance if a.get("iata") in MAJOR_AIRPORT_IATA][:AIRPORT_MAJOR_CANDIDATES]
    regionals = [a for a in by_distance if a.get("iata") in REGIONAL_AIRPORT_IATA][:AIRPORT_REGIONAL_CANDIDATES]
    candidates = [*majors, *regionals]
    if not candidates:
        return []
    routes = await _driving_routes(client, api_key, lat, lng, [(a["lat"], a["lon"]) for a in candidates], "airports")
    ranked = []
    for airport, route in zip(candidates, routes):
        straight = round(_haversine_miles(lat, lng, airport["lat"], airport["lon"]), 1)
        # No route: straight-line miles stand in.
        seconds, miles = route if route else (straight / 45 * 3600, straight)
        ranked.append((seconds, miles, airport))
    ranked.sort(key=lambda item: item[1])
    major_rows = [r for r in ranked if r[2].get("iata") in MAJOR_AIRPORT_IATA]
    regional_rows = [r for r in ranked if r[2].get("iata") in REGIONAL_AIRPORT_IATA]

    picked: list[tuple[float, float, dict, str]] = []
    for i, (seconds, miles, airport) in enumerate(major_rows[:AIRPORT_MAJOR_MAX_RESULTS]):
        if i == 0 or miles <= AIRPORT_MAJOR_MAX_DRIVE_MILES:
            picked.append((seconds, miles, airport, "MAJOR AIRPORT"))
    nearest_major_miles = major_rows[0][1] if major_rows else float("inf")
    if regional_rows and regional_rows[0][1] < nearest_major_miles:
        picked.append((*regional_rows[0], "REGIONAL AIRPORT"))
    if not picked or min(item[1] for item in picked) > AIRPORT_LOCAL_WHEN_HUBS_BEYOND_MILES:
        # The nearest hub is a long drive: add the local airport with airline service (State College,
        # Charlottesville, Hagerstown were left with only airports 70-150 miles away in the 10-town
        # test). Never a military field with a civil terminal (Dover AFB).
        local = next((a for a in by_distance if a.get("iata") in LOCAL_AIRLINE_AIRPORT_IATA
                      and not _MILITARY_AIRPORT_RE.search(a.get("name") or "")), None)
        straight = round(_haversine_miles(lat, lng, local["lat"], local["lon"]), 1) if local else None
        if local and straight <= AIRPORT_FALLBACK_MAX_MILES:
            route = (await _driving_routes(client, api_key, lat, lng, [(local["lat"], local["lon"])], "airports"))[0]
            miles = route[1] if route else straight
            picked.append((miles / 45 * 3600, miles, local, "REGIONAL AIRPORT"))
    picked.sort(key=lambda item: item[1])

    results: list[NearbyPlace] = []
    for _, miles, airport, label in picked:
        address = ", ".join(part for part in [airport.get("municipality"), airport.get("region")] if part) or None
        results.append(NearbyPlace(name=airport["name"], address=address, distance_miles=miles, place_type=label,
                                   latitude=airport["lat"], longitude=airport["lon"]))
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


async def _fetch_downtown_areas(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, db, home_city: Optional[str] = None,
                                home_state: Optional[str] = None, home_zip: Optional[str] = None) -> list[NearbyPlace]:
    """Nearby towns with a real town center - somewhere a guest could go for dinner or shops.

    10/25 audit: real sheets showed "Edgmont", "Evesham", "Deptford", "Cherry Hill Township", "Gulph
    Mills", "Chester Springs" and subdivision names as downtowns, and missed West Chester itself. So:
    - Candidates: every town Google lists around the home ("locality"; "sublocality"/"neighborhood"
      aren't valid includedTypes here - Google 400s on them), plus the nearest incorporated towns
      (boroughs, cities, towns, villages) from the Census list that Google left out
      (CENSUS_PLACES_DATA_PATH). "... Township" names are dropped outright (the whole township,
      pinned at its middle).
    - Each must show a real cluster of restaurants and shops around its center point
      (_town_center_score, free to count). How big depends on what the name is - see
      DOWNTOWN_MIN_SCORE_BY_KIND: an incorporated town needs the least; a Census-designated
      community (Wayne, Ardmore - but also "Haverford College"), a township municipality's name
      (Moorestown, but also Edgmont, pinned on a shopping strip) need more; any other name (Havertown
      is real, "Larchmont Meadows" is a subdivision) needs a full main street's worth.
    - Two that are really one place (Rosemont and Bryn Mawr on one stretch of Lancaster Ave, "St.
      Davids" and "Saint Davids") within DOWNTOWN_MIN_SEPARATION_MILES collapse to one name: the
      home's own town if it's one of them, else the more official kind, then the bigger downtown.
    - Then the AI (ai_downtown_service) gets the home's town and all these measurements and names the
      downtowns locals would ("Marlton", not the township "Evesham"; no malls when there's a main
      street). Each name it gives is checked against the data again (_verify_ai_downtowns) and only
      those that hold up print; with no usable AI answer the data-only list above prints.
    Shown nearest first; no "Downtown" prefix invented."""
    raw = await _search_nearby(client, api_key, lat, lng, "locality", radius_miles=DOWNTOWN_MAX_DISTANCE_MILES, max_result_count=DOWNTOWN_CANDIDATE_POOL, field_mask=FIELD_MASK, section="downtown")

    candidates: dict[str, tuple[NearbyPlace, str]] = {}
    for place in raw:
        name = _place_name(place)
        key = _town_key(name)
        if not name or key in candidates or re.search(r"\b(township|twp\.?)$", name, re.IGNORECASE):
            continue
        nearby = _place_to_nearby(place, lat, lng)
        if nearby and nearby.latitude is not None:
            candidates[key] = (nearby, _town_kind(place))
    extra = 0
    for name, state, t_lat, t_lng, is_cdp in sorted(_census_places_near(lat, lng, DOWNTOWN_MAX_DISTANCE_MILES), key=lambda t: _haversine_miles(lat, lng, t[2], t[3])):
        if is_cdp or _town_key(name) in candidates:
            continue
        candidates[_town_key(name)] = (NearbyPlace(name=name, address=f"{name}, {state}", latitude=t_lat, longitude=t_lng,
                                                   distance_miles=round(_haversine_miles(lat, lng, t_lat, t_lng), 1)), "incorporated")
        extra += 1
        if extra >= DOWNTOWN_CENSUS_EXTRA:
            break
    ordered = list(candidates.values())

    gate = asyncio.Semaphore(8)

    async def score(n: NearbyPlace) -> Optional[tuple[int, int]]:
        async with gate:
            return await _town_center_score(client, api_key, n.latitude, n.longitude)

    scores = await asyncio.gather(*(score(n) for n, _ in ordered))
    if all(s is None for s in scores):
        # The free count failed outright - fall back to the incorporated towns rather than nothing.
        passing = [(n, kind, 0) for n, kind in ordered if kind == "incorporated"]
    else:
        passing = [(n, kind, s[1]) for (n, kind), s in zip(ordered, scores) if s is not None and s[0] >= DOWNTOWN_MIN_SCORE_BY_KIND[kind]]

    # One place, two names: the official kind wins, then the bigger downtown.
    home_key = _town_key(home_city) if home_city else None
    rank = list(DOWNTOWN_MIN_SCORE_BY_KIND)
    passing.sort(key=lambda item: (rank.index(item[1]), -item[2], item[0].distance_miles or 0))
    kept: list[tuple[NearbyPlace, str, int]] = []
    for item in passing:
        n, kind, size = item
        clash = next((i for i, (k, _, _) in enumerate(kept) if _haversine_miles(n.latitude, n.longitude, k.latitude, k.longitude) < DOWNTOWN_MIN_SEPARATION_MILES), None)
        if clash is None:
            kept.append(item)
            continue
        k, k_kind, k_size = kept[clash]
        if home_key and _town_key(n.name) == home_key:
            kept[clash] = item  # a Bryn Mawr home's sheet says "Bryn Mawr", not its neighbor's name
        elif home_key and _town_key(k.name) == home_key:
            continue
        elif kind == k_kind and size == k_size:
            # Dead heat (both max out the count, e.g. Rosemont and Bryn Mawr on one stretch of
            # Lancaster Ave): the name whose point sits on the denser core wins.
            mine, theirs = await asyncio.gather(_town_center_score(client, api_key, n.latitude, n.longitude, DOWNTOWN_TIEBREAK_HALF_MILES),
                                                _town_center_score(client, api_key, k.latitude, k.longitude, DOWNTOWN_TIEBREAK_HALF_MILES))
            if mine and theirs and mine[1] > theirs[1]:
                kept[clash] = item
    kept = [n for n, _, _ in kept]
    kept.sort(key=lambda n: n.distance_miles if n.distance_miles is not None else float("inf"))
    data_list = kept[:MAX_RESULTS_PER_CATEGORY]

    # The AI step (ai_downtown_service): it gets the home's town and these same measurements, names
    # the downtowns locals would, and every name is checked against the data again below.
    from app.services.ai.ai_downtown_service import downtown_picks
    table = [{"name": n.name, "kind": DOWNTOWN_KIND_WORDS[kind], "miles": n.distance_miles, "score": s[0] if s else None,
              "passed": bool(s) and s[0] >= DOWNTOWN_MIN_SCORE_BY_KIND[kind]}
             for (n, kind), s in zip(ordered, scores)]
    picks = await downtown_picks(db, home_city, home_state, home_zip, table)
    if picks:
        verified = await _verify_ai_downtowns(client, api_key, lat, lng, picks, ordered, scores, home_state)
        if verified:
            return verified
    return data_list


async def _verify_ai_downtowns(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, picks: list[dict],
                               ordered: list[tuple[NearbyPlace, str]], scores: list, home_state: Optional[str]) -> list[NearbyPlace]:
    """The AI's downtowns that hold up against our data: located at a known candidate's point (or found
    with one Google look-up, at most DOWNTOWN_AI_LOCATE_LOOKUPS), with at least DOWNTOWN_AI_MIN_SCORE
    restaurants + shops around it and inside the search area; one per place. A main street the AI
    gives sharpens the look-up. A shopping-center pick (only when there's no real downtown nearby)
    is tagged so on the sheet."""
    known = {_town_key(n.name): (n, s) for (n, _), s in zip(ordered, scores)}
    out: list[NearbyPlace] = []
    lookups = 0
    for pick in picks:
        name, kind = pick["name"], pick.get("kind")
        hit = known.get(_town_key(name))
        if hit:
            point, score = (hit[0].latitude, hit[0].longitude), hit[1]
        else:
            if lookups >= DOWNTOWN_AI_LOCATE_LOOKUPS:
                continue
            lookups += 1
            state = pick.get("state") or home_state or ""
            query = ", ".join(x for x in [pick.get("main_street"), name, state] if x)
            try:
                raw = await _search_text(client, api_key, lat, lng, query, DOWNTOWN_MAX_DISTANCE_MILES, field_mask=CHEAT_FIELD_MASK, section="downtown_ai_locate")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"downtown look-up failed for {query}: {e}")
                continue
            loc = next(((p["location"]["latitude"], p["location"]["longitude"]) for p in raw[:3] if (p.get("location") or {}).get("latitude") is not None), None)
            if loc is None:
                continue
            point, score = loc, await _town_center_score(client, api_key, loc[0], loc[1])
        miles = _haversine_miles(lat, lng, point[0], point[1])
        if score is None or score[0] < DOWNTOWN_AI_MIN_SCORE or miles > DOWNTOWN_MAX_DISTANCE_MILES + 2:
            continue
        if any(_haversine_miles(point[0], point[1], o.latitude, o.longitude) < DOWNTOWN_MIN_SEPARATION_MILES for o in out):
            continue
        out.append(NearbyPlace(name=name, address=f"{name}, {pick.get('state') or home_state or ''}".strip(", "), latitude=point[0], longitude=point[1],
                               distance_miles=round(miles, 1), place_type="SHOPPING AREA" if kind == "shopping_center" else None))
    out.sort(key=lambda n: n.distance_miles if n.distance_miles is not None else float("inf"))
    return out[:MAX_RESULTS_PER_CATEGORY]


def _town_key(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"^saint\b", "st", name.lower().strip())).strip()


@lru_cache(maxsize=1)
def _load_census_places() -> dict[str, dict]:
    with open(CENSUS_PLACES_DATA_PATH, encoding="utf-8") as f:
        states = json.load(f)["states"]
    return {st: {"places": v["places"], "kinds": {p[0].lower(): ("cdp" if p[3] else "incorporated") for p in v["places"]},
                 "townships": set(v["townships"])}
            for st, v in states.items()}


def _census_places_near(lat: float, lng: float, miles: float) -> list[tuple[str, str, float, float, bool]]:
    pad_lat, pad_lng = miles / 69.0, miles / max(1.0, 69.0 * math.cos(math.radians(lat)))
    return [(name, st, p_lat, p_lng, bool(is_cdp))
            for st, data in _load_census_places().items()
            for name, p_lat, p_lng, is_cdp in data["places"]
            if abs(p_lat - lat) <= pad_lat and abs(p_lng - lng) <= pad_lng and _haversine_miles(lat, lng, p_lat, p_lng) <= miles]


_LOCALITY_STATE_RE = re.compile(r",\s*([A-Z]{2})(?:\s+\d{5})?,\s*USA$")


def _town_kind(place: dict) -> str:
    """What a Google locality's name is in its state, per the Census: "incorporated" (city, borough,
    town, village - also when the state can't be read off the address), "cdp" (a Census-designated
    community), "township" (a township/town municipality's name) or "other"."""
    match = _LOCALITY_STATE_RE.search(place.get("formattedAddress") or "")
    data = _load_census_places().get(match.group(1)) if match else None
    if data is None:
        return "incorporated"
    name = _place_name(place).lower().strip()
    if name in data["kinds"]:
        return data["kinds"][name]
    return "township" if name in data["townships"] else "other"


async def _town_center_score(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, half_miles: float = DOWNTOWN_CORE_HALF_MILES) -> Optional[tuple[int, int]]:
    """(score, size) for the restaurants and shops within DOWNTOWN_CORE_HALF_MILES (a square) of a
    town's center point: score counts up to 20 of each (what the DOWNTOWN_MIN_SCORE_BY_KIND bars are
    set against), size up to 40 of each, to tell a bigger downtown from a smaller one when both max
    out the score. Google Text Searches asking for place IDs only, which Google doesn't charge for
    (Text Search Essentials IDs Only: unlimited, $0). None when both counts fail."""
    pad_lat = half_miles / 69.0
    pad_lng = half_miles / max(1.0, 69.0 * math.cos(math.radians(lat)))
    box = {"rectangle": {"low": {"latitude": lat - pad_lat, "longitude": lng - pad_lng},
                         "high": {"latitude": lat + pad_lat, "longitude": lng + pad_lng}}}

    async def count(kind: str) -> Optional[int]:
        total, token = 0, None
        try:
            for _ in range(2):
                body = {"textQuery": kind, "includedType": kind, "strictTypeFiltering": True, "pageSize": 20, "locationRestriction": box}
                if token:
                    body["pageToken"] = token
                response = await client.post(
                    PLACES_TEXT_URL, json=body,
                    headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": "places.id,nextPageToken"},
                    timeout=10.0,
                )
                response.raise_for_status()
                _log_billable_call("downtown_check", 0.0)
                data = response.json()
                total += len(data.get("places") or [])
                token = data.get("nextPageToken")
                if not token:
                    break
            return total
        except Exception as e:  # noqa: BLE001
            logger.warning(f"town center {kind} count failed: {e}")
            return total or None

    restaurants, stores = await asyncio.gather(count("restaurant"), count("store"))
    if restaurants is None and stores is None:
        return None
    restaurants, stores = restaurants or 0, stores or 0
    return min(restaurants, 20) + min(stores, 20), restaurants + stores


async def _apply_driving_distances(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, lists: dict[str, list[NearbyPlace]]) -> None:
    """Swaps each shown place's straight-line miles for real driving miles (10/25 grader: verify the
    distances - "1.9 mi" to a hospital across a creek can be a 4-mile drive), one Routes call for the
    whole sheet, then re-sorts each list nearest-drive first (Medical keeps hospital-then-clinic).
    Best effort: on failure the straight-line miles stay."""
    places = [p for items in lists.values() for p in items if p.latitude is not None and p.longitude is not None]
    routes = await _driving_routes(client, api_key, lat, lng, [(p.latitude, p.longitude) for p in places], "driving_distances")
    for place, route in zip(places, routes):
        if route:
            place.distance_miles = route[1]
    for key, items in lists.items():
        if key != "medical_care":
            items.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))


def _prefer_shortest_drive(medical_care: list[NearbyPlace], hospital_backups: list[NearbyPlace],
                           private_schools: list[NearbyPlace], school_backups: list[NearbyPlace]) -> None:
    """After driving miles are in: a backup hospital that's a clearly shorter drive takes the hospital
    slot (the old one becomes a backup), and a private school more than PRIVATE_SCHOOL_MAX_DRIVE_MILES
    away by car gives way to a backup within it - or just drops off."""
    def miles(p: NearbyPlace) -> float:
        return p.distance_miles if p.distance_miles is not None else float("inf")

    if medical_care and medical_care[0].place_type == "HOSPITAL" and hospital_backups:
        best = min(hospital_backups, key=miles)
        if miles(best) + 1.0 < miles(medical_care[0]):
            hospital_backups[hospital_backups.index(best)] = medical_care[0]
            medical_care[0] = best
    kept = [s for s in private_schools if miles(s) <= PRIVATE_SCHOOL_MAX_DRIVE_MILES]
    for spare in sorted(school_backups, key=miles):
        if len(kept) >= len(private_schools):
            break
        if miles(spare) <= PRIVATE_SCHOOL_MAX_DRIVE_MILES and spare not in kept:
            kept.append(spare)
    # Special-education schools last - a genuine school, but not the general pick a buyer is asking about.
    private_schools[:] = sorted(kept, key=lambda p: ("SPECIAL" in (p.place_type or ""), miles(p)))


async def _proofread_sheet(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, property: Property, db, owner_id: Optional[str],
                           lists: dict[str, list[NearbyPlace]], backups: dict[str, list[NearbyPlace]]) -> Optional[dict]:
    """Runs the AI proofread and applies it to `lists` in place. Returns what was found and done
    (saved with the sheet for checking later), or None when there was no review."""
    from app.services.ai.cheat_sheet_proofread_service import review
    home = ", ".join(x for x in [getattr(property, "unparsed_address", None) or getattr(property, "street_address", None), property.city, getattr(property, "state", None)] if x)
    found = await review(db, home, lists, backups, owner_id=owner_id)
    if not found:
        return None
    try:
        actions = await _apply_proofread(client, api_key, lat, lng, lists, backups, found["issues"])
    except Exception as e:  # noqa: BLE001 - never fail a cheat sheet over the proofread
        logger.warning(f"applying the cheat sheet proofread failed for {home}: {e}")
        actions = ["not applied - error"]
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "issues": found["issues"], "actions": actions, "cost_usd": found["cost_usd"]}


async def _apply_proofread(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, lists: dict[str, list[NearbyPlace]],
                           backups: dict[str, list[NearbyPlace]], issues: list[dict]) -> list[str]:
    """What the proofread is allowed to do with each reported item:
    - renamed: take the current name only when Google shows an open place by that name within
      PROOFREAD_RENAME_MAX_MILES of the item (else the item stays as it is);
    - closed / wrong_category / not_a_public_place: remove it and refill the slot from that list's
      backups (already through every rule; any backup the AI also reported is skipped). Medical's
      hospital and everyday-care slots each refill from their own backups. Lists without backups
      (airports, downtowns) just lose the item.
    Nothing the AI names itself is ever added."""
    by_id: dict[str, tuple[str, int]] = {f"{key}#{i}": (key, i) for key, items in lists.items() for i in range(len(items))}
    flagged_backups = {i["id"] for i in issues if i["id"].startswith("backup:")}
    actions: list[str] = []
    removed: list[tuple[str, int]] = []
    refilled: list[NearbyPlace] = []
    for issue in issues:
        hit = by_id.get(issue["id"])
        if not hit:
            continue
        key, idx = hit
        place = lists[key][idx]
        if issue["problem"] == "renamed":
            new_name = issue.get("current_name")
            found = await _confirm_rename(client, api_key, place, new_name, key) if new_name else None
            if found is None:
                actions.append(f"kept {place.name} ({key}) - rename to {new_name!r} not confirmed")
                continue
            old_name, loc = place.name, found["location"]
            place.name = _place_name(found)
            place.address = found.get("formattedAddress") or place.address
            if _haversine_miles(place.latitude, place.longitude, loc["latitude"], loc["longitude"]) > PROOFREAD_RENAME_MAX_MILES:
                place.latitude, place.longitude = loc["latitude"], loc["longitude"]
                place.distance_miles = round(_haversine_miles(lat, lng, loc["latitude"], loc["longitude"]), 1)
                refilled.append(place)  # moved - gets a fresh driving distance below
            actions.append(f"renamed {old_name} -> {place.name} ({key})")
            continue
        removed.append((key, idx))

    for key, idx in sorted(removed, key=lambda r: -r[1]):
        old = lists[key][idx]
        slot = key if key != "medical_care" else ("medical_care:hospital" if old.place_type == "HOSPITAL" else "medical_care:everyday")
        shown = {p.name.lower() for p in lists[key]}
        spare = next((b for i, b in enumerate(backups.get(slot) or [])
                      if f"backup:{slot}#{i}" not in flagged_backups and b.name.lower() not in shown), None)
        if spare is not None:
            lists[key][idx] = spare
            refilled.append(spare)
            actions.append(f"replaced {old.name} with {spare.name} ({key})")
        else:
            del lists[key][idx]
            actions.append(f"removed {old.name} ({key}) - no checked backup")
    if refilled:
        await _apply_driving_distances(client, api_key, lat, lng, {"refilled": refilled})
        for key in ("private_schools", "grocery_stores", "parks"):
            lists.get(key, []).sort(key=lambda p: p.distance_miles if p.distance_miles is not None else float("inf"))
    return actions


# A rename from the proofread must pass the same rule as the list it's on before it prints.
_RENAME_RULES = {
    "grocery_stores": _is_full_service_grocery,
    "parks": _is_parent_park_candidate,
}


async def _confirm_rename(client: httpx.AsyncClient, api_key: str, place: NearbyPlace, new_name: str, key: str) -> Optional[dict]:
    """The Google place the proofread's corrected name points to, or None. It has to be open, carry
    that name, sit within PROOFREAD_RENAME_SEARCH_MILES of the old item (Google's pin for a misnamed
    listing can be off - "Garrett memorial hospital?" sat away from Garrett Regional Medical Center)
    and pass the list's own rule (a hospital must be a real general hospital, a park a park)."""
    if place.latitude is None or place.longitude is None:
        return None
    try:
        raw = await _search_text(client, api_key, place.latitude, place.longitude, new_name, PROOFREAD_RENAME_SEARCH_MILES,
                                 field_mask=CHEAT_FIELD_MASK, section="proofread_rename")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"proofread rename check failed for {new_name}: {e}")
        return None
    if key == "medical_care":
        rule = (lambda g: _is_legitimate_full_service_hospital(g, _near_medicare_hospital(g))) if place.place_type == "HOSPITAL" \
            else (lambda g: _non_hospital_medical_label(g) is not None)
    else:
        rule = _RENAME_RULES.get(key, lambda g: True)
    wanted = _school_name_tokens(new_name)
    for g in raw[:5]:
        loc = g.get("location") or {}
        if loc.get("latitude") is None or not _is_open(g):
            continue
        if _haversine_miles(place.latitude, place.longitude, loc["latitude"], loc["longitude"]) > PROOFREAD_RENAME_SEARCH_MILES:
            continue
        got = _school_name_tokens(_place_name(g))
        if wanted and got and len(wanted & got) / len(wanted) >= 0.6 and rule(g):
            return g
    return None


async def _fetch_live(client: httpx.AsyncClient, api_key: str, lat: float, lng: float, property: Property, db, owner_id: Optional[str] = None) -> NearbyAmenitiesResponse:
    listing_key = property.listing_key  # read once, before anything else touches the session
    cost_token = _cost_log_var.set([])
    alternates_token = _alternates_var.set({})
    proofread: Optional[dict] = None
    try:
        schools = await _fetch_assigned_schools(property, db)
        # The lists look up side by side (the downtown AI alone can take ~15 s). Only the downtown step
        # uses the database session, so nothing shares it at once.
        grocery_stores, medical_care, parks, airports, private_schools, downtown_areas = await asyncio.gather(
            _fetch_grocery_stores(client, api_key, lat, lng),
            _fetch_medical_care(client, api_key, lat, lng),
            _fetch_parks(client, api_key, lat, lng),
            _fetch_airports(client, api_key, lat, lng),
            _fetch_private_schools(client, api_key, lat, lng),
            _fetch_downtown_areas(client, api_key, lat, lng, db, home_city=property.city,
                                  home_state=getattr(property, "state", None), home_zip=getattr(property, "zipcode", None)),
        )
        # Parks keep straight-line miles: their Google pin sits mid-park, and a route to it measured
        # 0.9 mi as a 5-mile drive (Ithan Valley Park) on a real sheet. Downtowns do get driving miles -
        # a town 6 miles across the Potomac with no bridge (Poolesville, from Sterling VA) isn't nearby.
        # The hospital and school backups get driving miles too: across a river the straight-line
        # nearest can be the longest drive (Bryans Road MD: the "nearest" hospital and private schools
        # were 24-27 miles by car, across the Potomac).
        alternates = _alternates_var.get() or {}
        hospital_backups = alternates.get("medical_care:hospital", [])
        school_backups = alternates.get("private_schools", [])
        await _apply_driving_distances(client, api_key, lat, lng, {
            "private_schools": private_schools, "grocery_stores": grocery_stores, "medical_care": medical_care,
            "downtown_areas": downtown_areas, "hospital_backups": hospital_backups, "school_backups": school_backups,
        })
        downtown_areas[:] = [d for d in downtown_areas if d.distance_miles is None or d.distance_miles <= DOWNTOWN_MAX_DRIVE_MILES]
        _prefer_shortest_drive(medical_care, hospital_backups, private_schools, school_backups)

        # Last: the AI proofread (cheat_sheet_proofread_service) - can only take a confirmed-wrong item
        # out, refill its slot from that list's checked backup, or apply a rename Google confirms.
        lists = {"private_schools": private_schools, "grocery_stores": grocery_stores, "medical_care": medical_care,
                 "parks": parks, "airports": airports, "downtown_areas": downtown_areas}
        proofread = await _proofread_sheet(client, api_key, lat, lng, property, db, owner_id, lists, _alternates_var.get() or {})
    finally:
        call_log = _cost_log_var.get() or []
        _cost_log_var.reset(cost_token)
        _alternates_var.reset(alternates_token)

    total_cost = sum(cost for _, cost in call_log)
    by_section: dict[str, float] = {}
    for section, cost in call_log:
        by_section[section] = by_section.get(section, 0.0) + cost
    breakdown = ", ".join(f"{section}=${cost:.3f}" for section, cost in by_section.items())
    logger.info(
        f"[CHEAT SHEET COST] listing_key={listing_key} CACHE MISS - live Google cost=${total_cost:.3f} "
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
        schema_version=AMENITIES_SCHEMA_VERSION,
        proofread=proofread,
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
        return NearbyAmenitiesResponse(**property.nearby_amenities, cached_at=property.nearby_amenities_cached_at, available=True,
                                       schema_version=None if failed_at_raw else property.nearby_amenities.get("_schema_version"))
    except Exception:
        return None


async def _persist_amenities_cache(db, property: Property, result: NearbyAmenitiesResponse, google_failed: bool = False) -> None:
    try:
        data = result.model_dump(mode="json", exclude={"available", "cached_at", "schema_version"})
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


SAME_BUILDING_MAX_MILES = 0.06  # ~300 ft: another unit / the same address listed again


async def _same_building_amenities(db, property: Property, lat: float, lng: float) -> Optional[NearbyAmenitiesResponse]:
    """A home in the same building or at the same spot as one already looked up under the current
    rules (another condo unit, the same house relisted under a new listing key - "191 Presidential
    Blvd" is on file three times) has the same places around it, so it reuses that lookup instead of
    paying Google again. Assigned schools are still worked out for this listing on its own."""
    pad = 0.002
    lat_col = func.coalesce(Property.google_rooftop_latitude, Property.latitude)
    lng_col = func.coalesce(Property.google_rooftop_longitude, Property.longitude)
    rows = (await db.execute(
        select(Property.nearby_amenities, Property.nearby_amenities_cached_at, lat_col, lng_col)
        .where(Property.listing_key != property.listing_key, Property.nearby_amenities.isnot(None),
               lat_col.between(lat - pad, lat + pad), lng_col.between(lng - pad, lng + pad))
        .limit(20)
    )).all()
    now = datetime.now(timezone.utc)
    for data, cached_at, o_lat, o_lng in rows:
        if not isinstance(data, dict) or data.get("_schema_version") != AMENITIES_SCHEMA_VERSION or data.get("_google_lookup_failed_at"):
            continue
        if cached_at is None or _haversine_miles(lat, lng, o_lat, o_lng) > SAME_BUILDING_MAX_MILES:
            continue
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        if now - cached_at > timedelta(days=CACHE_TTL_DAYS):
            continue
        try:
            return NearbyAmenitiesResponse(**{k: v for k, v in data.items() if k != "ai_schools"}, cached_at=cached_at, available=True,
                                           schema_version=AMENITIES_SCHEMA_VERSION)
        except Exception:  # noqa: BLE001
            continue
    return None


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
        shared = await _same_building_amenities(db, property, lookup_lat, lookup_lng)
        if shared is not None:
            shared.schools = assigned_schools
            logger.info(f"[CHEAT SHEET COST] listing_key={property.listing_key} SAME-BUILDING HIT - live Google cost=$0.000")
            await _persist_amenities_cache(db, property, shared)
            return shared

    # Only a home's FIRST lookup counts toward the agent's AI usage. A re-lookup because we changed
    # the cheat sheet's data format (AMENITIES_SCHEMA_VERSION - every release that bumps it would
    # otherwise re-charge every agent ~$0.30 per home), the 180-day refresh, or a forced refresh is
    # our doing, not theirs - it stays on the platform budget like any sync call.
    charge_to = owner_id if not property.nearby_amenities and not force_refresh else None
    from app.services.usage_owner import bill_to
    try:
        # bill_to: the downtown AI follows the same rule - charged on the first lookup only.
        with bill_to(charge_to):
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
