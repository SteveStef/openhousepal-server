"""Deterministic (non-AI) free-text search parser for the Website Builder's public search bar.

No LLM call, no cost/rate-limit infrastructure - plain regex/keyword heuristics that turn
something like "villanova pa 4 beds 1 acre radnor schools around 1-1.5M" into the structured
filter fields PropertyService.get_properties_by_preferences already understands. Every field is
omitted (not guessed) when nothing in the text matches it - an unmatched search runs unfiltered
across the full MLS mirror rather than silently returning nothing.
"""
import re
from typing import Optional

_MILLION = re.compile(r'^(\d+(?:\.\d+)?)\s*m(?:illion)?$', re.IGNORECASE)
_THOUSAND = re.compile(r'^(\d+(?:\.\d+)?)\s*k$', re.IGNORECASE)

_RANGE_RE = re.compile(
    r'\$?\s*(\d+(?:\.\d+)?)\s*(m|million|k)?\s*(?:-|to)\s*\$?\s*(\d+(?:\.\d+)?)\s*(m|million|k)?',
    re.IGNORECASE,
)
_UNDER_RE = re.compile(r'(?:under|below|up to)\s*\$?\s*(\d+(?:\.\d+)?)\s*(m|million|k)?', re.IGNORECASE)
_OVER_RE = re.compile(r'(?:over|above|at least|min(?:imum)?)\s*\$?\s*(\d+(?:\.\d+)?)\s*(m|million|k)?', re.IGNORECASE)
_SINGLE_PRICE_RE = re.compile(r'\$?\s*(\d+(?:\.\d+)?)\s*(m|million|k)\b', re.IGNORECASE)

_BEDS_RE = re.compile(r'(\d+)\s*(?:bed|beds|bedroom|bedrooms|br)\b', re.IGNORECASE)
_BATHS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:bath|baths|bathroom|bathrooms|ba)\b', re.IGNORECASE)
_ACRES_RE = re.compile(r'(\d+(?:\.\d+)?)\s*acres?\b', re.IGNORECASE)
_SCHOOL_RE = re.compile(r'([a-z][a-z\'-]*)\s+school', re.IGNORECASE)
_SCHOOL_STOPWORDS = {'the', 'a', 'an', 'good', 'great', 'top', 'best', 'local', 'public'}

_PROPERTY_TYPE_KEYWORDS = [
    ('single family', 'is_single_family'),
    ('single-family', 'is_single_family'),
    ('townhouse', 'is_town_house'),
    ('town house', 'is_town_house'),
    ('condo', 'is_condo'),
    ('condominium', 'is_condo'),
    ('multi family', 'is_multi_family'),
    ('multi-family', 'is_multi_family'),
]


def _price_to_number(amount: str, unit: Optional[str]) -> Optional[float]:
    try:
        value = float(amount.replace(',', '').replace('$', ''))
    except ValueError:
        return None
    if unit and unit.lower().startswith('m'):
        return value * 1_000_000
    if unit and unit.lower() == 'k':
        return value * 1_000
    return value


def _parse_price(lowered: str, result: dict) -> None:
    range_match = _RANGE_RE.search(lowered)
    if range_match and (range_match.group(2) or range_match.group(4)):
        unit_low = range_match.group(2) or range_match.group(4)
        unit_high = range_match.group(4) or range_match.group(2)
        low = _price_to_number(range_match.group(1), unit_low)
        high = _price_to_number(range_match.group(3), unit_high)
        if low is not None:
            result['min_price'] = low
        if high is not None:
            result['max_price'] = high
        return

    under_match = _UNDER_RE.search(lowered)
    if under_match:
        price = _price_to_number(under_match.group(1), under_match.group(2))
        if price is not None:
            result['max_price'] = price

    over_match = _OVER_RE.search(lowered)
    if over_match:
        price = _price_to_number(over_match.group(1), over_match.group(2))
        if price is not None:
            result['min_price'] = price

    if 'min_price' not in result and 'max_price' not in result:
        single_match = _SINGLE_PRICE_RE.search(lowered)
        if single_match:
            price = _price_to_number(single_match.group(1), single_match.group(2))
            if price is not None:
                # A single bare price ("around $1.2M") is a target, not a hard cutoff - use a
                # comfortable band around it rather than guessing which side the visitor meant.
                result['min_price'] = round(price * 0.85)
                result['max_price'] = round(price * 1.15)


def parse_search_query(text: str, known_cities: list[str]) -> dict:
    """Returns a dict with any of: city, min_price, max_price, min_beds, min_baths,
    min_lot_size_acres, school_district, property_type_flag."""
    lowered = text.lower()
    result: dict = {}

    beds_match = _BEDS_RE.search(lowered)
    if beds_match:
        result['min_beds'] = int(beds_match.group(1))

    baths_match = _BATHS_RE.search(lowered)
    if baths_match:
        result['min_baths'] = float(baths_match.group(1))

    acres_match = _ACRES_RE.search(lowered)
    if acres_match:
        result['min_lot_size_acres'] = float(acres_match.group(1))

    _parse_price(lowered, result)

    school_match = _SCHOOL_RE.search(lowered)
    if school_match:
        words = school_match.group(1).strip().split()
        candidate = words[-1] if words else None
        if candidate and candidate not in _SCHOOL_STOPWORDS:
            result['school_district'] = candidate

    for phrase, flag in _PROPERTY_TYPE_KEYWORDS:
        if phrase in lowered:
            result['property_type_flag'] = flag
            break

    # Only matched against the agent's own already-configured served areas (known vocabulary) -
    # never guessed from arbitrary capitalized words in the query.
    for city in known_cities:
        if city and city.lower() in lowered:
            result['city'] = city
            break

    return result
