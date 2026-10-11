"""US Census ACS 5-Year demographics for Area records - the ONLY file in this codebase that
talks to the Census API. Nothing outside `area_enrichment_service.py` should import this module,
and nothing outside this file should construct a Census request - keeps the website templates
consuming only normalized, already-verified Area.facts, never a live Census call (per the
Website Builder Phase 3 spec).

Matching is deliberately conservative: a statistic is only ever stored when it can be tied to a
real Census geography with reasonable confidence. There are two paths, tried in order, and if
neither is confident, demographics are OMITTED for that area rather than guessed:

  1. Place-level: the town has its own Census "Place" (city/CDP/borough/etc.) whose normalized
     name matches the area's name exactly.
  2. ZCTA fallback: average the ACS numbers for the area's own ZIP codes (Zip Code Tabulation
     Areas) - no name-matching risk since ZIP codes are already real, known facts about the area.

No API key is required for light ACS usage; CENSUS_API_KEY (optional) raises the rate limit if
set - see https://api.census.gov/data/key_signup.html.
"""
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
_warned_missing_key = False

CENSUS_API_KEY = os.getenv("CENSUS_API_KEY")
ACS_YEAR = int(os.getenv("CENSUS_ACS_YEAR", "2023"))
_BASE_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"

# variable -> (facts key, human label)
_VARIABLES = {
    "B01003_001E": "population",
    "B19013_001E": "median_household_income",
    "B01002_001E": "median_age",
    "B11001_001E": "household_count",
    "B25003_001E": "_occupied_housing_units",   # intermediate, used to derive homeownership_rate
    "B25003_002E": "_owner_occupied_units",     # intermediate
}

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
    "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}

# Process-lifetime cache of each state's Place list - Census place boundaries don't change
# often enough to need a TTL, and this avoids re-fetching ~hundreds of rows per state for every
# new area in that state.
_places_cache: dict[str, list[dict]] = {}

_PLACE_SUFFIX_RE = re.compile(
    r"\s+(cdp|borough|township|town|city|village)$", re.IGNORECASE
)


def _normalize_place_name(name: str) -> str:
    name = name.strip().lower()
    name = _PLACE_SUFFIX_RE.sub("", name)
    return name.strip()


@dataclass
class DemographicValue:
    value: float
    geography: str
    year: int = ACS_YEAR

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "source": "census_acs5",
            "geography": self.geography,
            "year": self.year,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


def _params(get_vars: list[str], **for_in) -> dict:
    params = {"get": ",".join(["NAME", *get_vars])}
    params.update(for_in)
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    return params


async def _fetch_places(client: httpx.AsyncClient, state: str) -> list[dict]:
    fips = STATE_FIPS.get(state.upper())
    if not fips:
        return []
    if fips in _places_cache:
        return _places_cache[fips]
    rows = await _get_json(client, {"for": "place:*", "in": f"state:{fips}"})
    if rows is None:
        _places_cache[fips] = []
        return []
    header, *data_rows = rows
    places = [dict(zip(header, row)) for row in data_rows]
    _places_cache[fips] = places
    return places


async def _fetch_zcta(client: httpx.AsyncClient, zip_code: str) -> Optional[dict]:
    rows = await _get_json(client, {"for": f"zip code tabulation area:{zip_code}"})
    if not rows or len(rows) < 2:
        return None
    header, row = rows[0], rows[1]
    return dict(zip(header, row))


async def _get_json(client: httpx.AsyncClient, for_in: dict) -> Optional[list]:
    """Census redirects unkeyed/invalid requests to an HTML "missing key" page (still HTTP 200,
    only visible after following the redirect) rather than a clean 4xx - detected explicitly so
    a missing CENSUS_API_KEY shows up as one clear log line instead of silently looking like
    "no confident match for this town" (a real omission) every single call."""
    global _warned_missing_key
    try:
        resp = await client.get(_BASE_URL, params=_params(list(_VARIABLES.keys()), **for_in), timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        if "missing_key" in str(resp.url):
            if not _warned_missing_key:
                logger.warning("Census API request redirected to missing_key.html - set CENSUS_API_KEY in backend/.env (free signup: https://api.census.gov/data/key_signup.html)")
                _warned_missing_key = True
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Census API request failed", extra={"error": str(exc)})
        return None


def _to_facts(raw: dict, geography: str) -> dict:
    """Converts raw Census variable codes into our facts.demographics shape, deriving
    homeownership_rate from the two intermediate housing-tenure variables. Any variable Census
    returned as null/negative (its 'not available' sentinel) is skipped, not zeroed."""
    def num(code: str) -> Optional[float]:
        raw_value = raw.get(code)
        if raw_value is None:
            return None
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None  # Census uses negative sentinels for unavailable

    out: dict = {}
    for code, key in _VARIABLES.items():
        if key.startswith("_"):
            continue
        value = num(code)
        if value is not None:
            out[key] = DemographicValue(value=value, geography=geography).to_dict()

    occupied = num("B25003_001E")
    owner_occupied = num("B25003_002E")
    if occupied and owner_occupied is not None and occupied > 0:
        out["homeownership_rate"] = DemographicValue(value=round(owner_occupied / occupied, 4), geography=geography).to_dict()

    return out


async def get_demographics(state: str, zip_codes: list[str], place_name: str) -> dict:
    """Returns a facts.demographics dict, or {} if nothing could be confidently matched."""
    async with httpx.AsyncClient() as client:
        # 1. Place-level match
        places = await _fetch_places(client, state)
        target = _normalize_place_name(place_name)
        match = next(
            (p for p in places if _normalize_place_name((p.get("NAME") or "").split(",")[0]) == target),
            None,
        )
        if match:
            fips = STATE_FIPS.get(state.upper(), "")
            geography = f"place:{match.get('place')} (state:{fips}) - {match.get('NAME')}"
            facts = _to_facts(match, geography)
            if facts:
                return facts

        # 2. ZCTA fallback - average across the area's known ZIP codes
        zcta_rows = []
        for zip_code in zip_codes[:5]:  # bounded - an area realistically has a handful of ZIPs
            row = await _fetch_zcta(client, zip_code)
            if row:
                zcta_rows.append(row)
        if not zcta_rows:
            return {}

        merged: dict = {}
        for code, key in _VARIABLES.items():
            values = [float(r[code]) for r in zcta_rows if r.get(code) not in (None, "") and float(r[code]) >= 0]
            if values:
                merged[code] = sum(values) / len(values)

        geography = f"ZCTA average ({', '.join(zip_codes[:5])})"
        return _to_facts({k: str(v) for k, v in merged.items()}, geography)
