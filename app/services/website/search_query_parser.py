"""Deterministic (non-AI) free-text search parser for the Website Builder's public search bar.

No LLM call, no cost/rate-limit infrastructure - plain regex/keyword heuristics that turn
something like "villanova pa 4 beds 1 acre radnor schools around 1-1.5M" into the structured
filter fields PropertyService.get_properties_by_preferences already understands. Every field is
omitted (not guessed) when nothing in the text matches it.

11/09 deep pass (owner: "fully test the search bar on the hero DEEEEPLY"): prices written with
commas ("under $1,500,000" used to read as "under $1"), "between 1 and 2 million", "4+ beds",
"four bedroom", "3 bd", "townhome", "half acre", zip codes, several towns at once, and any real
MLS town (not only the agent's own - "Malvern" used to be silently ignored and the agent's towns
shown instead). Words it still can't search on ("pool") come back in `unmatched` so the Search
page can say so instead of quietly dropping them.
"""
import re
from typing import Optional

_NUM = r'(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)'
_UNIT = r'(m|mil|million|k|thousand)?'

_RANGE_RE = re.compile(rf'\$?\s*{_NUM}\s*{_UNIT}\s*(?:-|–|to)\s*\$?\s*{_NUM}\s*{_UNIT}(?![a-z])', re.IGNORECASE)
_BETWEEN_RE = re.compile(rf'between\s*\$?\s*{_NUM}\s*{_UNIT}\s*and\s*\$?\s*{_NUM}\s*{_UNIT}(?![a-z])', re.IGNORECASE)
_UNDER_RE = re.compile(rf'(?:under|below|up to|less than|max(?:imum)?|no more than)\s*\$?\s*{_NUM}\s*{_UNIT}(?![a-z])', re.IGNORECASE)
_OVER_RE = re.compile(rf'(?:over|above|at least|more than|min(?:imum)?|from)\s*\$?\s*{_NUM}\s*{_UNIT}(?![a-z])', re.IGNORECASE)
_SINGLE_PRICE_RE = re.compile(rf'(?:around|about|near|~)?\s*\$?\s*{_NUM}\s*(m|mil|million|k|thousand)(?![a-z])', re.IGNORECASE)
_PLUS_PRICE_RE = re.compile(rf'\$?\s*{_NUM}\s*(m|mil|million|k|thousand)\s*\+|\$\s*{_NUM}()\s*\+', re.IGNORECASE)
_DOLLAR_RE = re.compile(rf'(?:around|about|near|~)?\s*\$\s*{_NUM}(?![\d,a-z])', re.IGNORECASE)

_WORD_NUMS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10'}
_WORD_NUM_RE = re.compile(r'\b(' + '|'.join(_WORD_NUMS) + r')\b(?=\s*\+?\s*(?:-\s*)?(?:bed|bath|br\b|bd|ba\b|full|acre))', re.IGNORECASE)
_BEDS_RE = re.compile(r'(\d+)\s*\+?\s*(?:(?:-|to)\s*\d+\s*)?(?:or more\s*)?(?:bed(?:room)?s?|br|brs|bd|bds|bdrms?)\b', re.IGNORECASE)
_BATHS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*\+?\s*(?:(?:-|to)\s*\d+(?:\.\d+)?\s*)?(?:full\s+)?(?:bath(?:room)?s?|ba|bth|bths)\b', re.IGNORECASE)
_ACRES_RE = re.compile(r'(\d+(?:\.\d+)?|1/2|1/4|3/4|half(?: an?)?|quarter(?: of an?)?)\s*\+?\s*(?:-\s*)?acres?\b(?:\s*lot)?', re.IGNORECASE)
_ACRE_WORDS = {'1/2': 0.5, '1/4': 0.25, '3/4': 0.75, 'half': 0.5, 'half a': 0.5, 'half an': 0.5, 'quarter': 0.25, 'quarter of a': 0.25, 'quarter of an': 0.25}
_SCHOOL_RE = re.compile(r"((?:[a-z][a-z'-]*\s+){0,2}[a-z][a-z'-]*)\s+(?:school|schools|sd)\b(?:\s+district)?", re.IGNORECASE)
_SCHOOL_STOPWORDS = {'the', 'a', 'an', 'good', 'great', 'top', 'best', 'local', 'public', 'near', 'in', 'with', 'by', 'close', 'to', 'and', 'or', 'homes', 'home', 'house', 'houses', 'for', 'sale'}
_SCHOOL_AFTER_RE = re.compile(r"\s*(?:school|schools|sd)(?![a-z])", re.IGNORECASE)
_ZIP_RE = re.compile(r'(?<![\d$,.])\b(\d{5})\b(?![\d,.]|\s*(?:k|m|mil|million|thousand|sq|sf|acres?)\b)', re.IGNORECASE)

_PROPERTY_TYPE_KEYWORDS = [
    ('single family', 'is_single_family'),
    ('single-family', 'is_single_family'),
    ('detached', 'is_single_family'),
    ('townhouse', 'is_town_house'),
    ('town house', 'is_town_house'),
    ('townhome', 'is_town_house'),
    ('town home', 'is_town_house'),
    ('rowhome', 'is_town_house'),
    ('row home', 'is_town_house'),
    ('rowhouse', 'is_town_house'),
    ('condominium', 'is_condo'),
    ('condo', 'is_condo'),
    ('apartment', 'is_condo'),
    ('multi family', 'is_multi_family'),
    ('multi-family', 'is_multi_family'),
    ('duplex', 'is_multi_family'),
]

# Words that carry no filter on their own - never reported back as "not searched".
_FILLER = {
    'a', 'an', 'the', 'in', 'on', 'at', 'of', 'for', 'with', 'and', 'or', 'to', 'near', 'by', 'from', 'around', 'about',
    'home', 'homes', 'house', 'houses', 'property', 'properties', 'listing', 'listings', 'sale', 'buy', 'find', 'me',
    'show', 'looking', 'want', 'need', 'i', 'im', "i'm", 'my', 'we', 'our', 'that', 'has', 'have', 'is', 'are', 'any',
    'all', 'new', 'available', 'pa', 'nj', 'de', 'md', 'va', 'dc', 'wv', 'area', 'town', 'township', 'twp', 'borough',
    'city', 'price', 'priced', 'budget', 'max', 'min', 'up', 'less', 'more', 'than', 'least', 'between', 'under',
    'over', 'below', 'above', 'plus', 'lot', 'lots', 'land', 'yard', 'district', 'dollars', 'k', 'm', 'million',
    'sq', 'ft', 'sqft', 'school', 'schools', 'nice', 'good', 'great', 'big', 'large', 'small', 'family', 'families',
    'or more', 'just', 'listed', 'please', 'zip', 'code', 'some', 'like', 'within', 'close', 'walk', 'walking',
}
# Real town names that are also everyday words - only matched with a state ("Media, PA") or when
# they're one of the agent's own towns, so "media room" never turns into the town of Media.
_COMMON_WORD_TOWNS = {
    'media', 'gap', 'paradise', 'pool', 'center', 'centre', 'garage', 'view', 'park', 'lake', 'hill', 'hills', 'bay',
    'mountain', 'valley', 'forest', 'grove', 'spring', 'springs', 'beach', 'harbor', 'hope', 'unity', 'liberty',
    'friendship', 'industry', 'enterprise', 'commerce', 'fairplay', 'basement', 'deck', 'patio', 'acme', 'bird',
    'marshall', 'washington', 'union', 'oxford', 'chester', 'lincoln', 'franklin', 'jefferson', 'madison', 'clinton',
    'new', 'home', 'modern', 'colonial', 'ranch', 'cape', 'split', 'level', 'main', 'line', 'water', 'woods',
}


def _price_to_number(amount: str, unit: Optional[str]) -> Optional[float]:
    try:
        value = float(amount.replace(',', '').replace('$', ''))
    except ValueError:
        return None
    u = (unit or '').lower()
    if u.startswith('m'):
        return value * 1_000_000
    if u in ('k', 'thousand'):
        return value * 1_000
    return value


def _blank(text: str, m: re.Match) -> str:
    return text[:m.start()] + ' ' * (m.end() - m.start()) + text[m.end():]


def _pair(m: re.Match) -> tuple[Optional[float], Optional[float]]:
    """A price range's two ends, sharing a unit ("2-3M" is 2M-3M). A pair with no unit counts only
    when both are real dollar amounts ($500,000 - $750,000), so "3-4 beds" is never a price."""
    a, ua, b, ub = m.group(1), m.group(2), m.group(3), m.group(4)
    if not (ua or ub):
        lo, hi = _price_to_number(a, None), _price_to_number(b, None)
        return (lo, hi) if lo and hi and lo >= 10_000 and hi >= 10_000 else (None, None)
    return _price_to_number(a, ua or ub), _price_to_number(b, ub or ua)


def _parse_price(text: str, result: dict) -> str:
    for rx in (_BETWEEN_RE, _RANGE_RE):
        m = rx.search(text)
        if m:
            low, high = _pair(m)
            if low is not None and high is not None:
                if low > high:
                    low, high = high, low
                result['min_price'], result['max_price'] = low, high
                return _blank(text, m)

    m = _PLUS_PRICE_RE.search(text)
    if m:
        # "$2m+" / "$750,000+" = that much and up
        price = _price_to_number(m.group(1), m.group(2)) if m.group(1) else _price_to_number(m.group(3), None)
        if price is not None and price >= 10_000:
            result['min_price'] = price
            text = _blank(text, m)

    for rx, key in ((_UNDER_RE, 'max_price'), (_OVER_RE, 'min_price')):
        m = rx.search(text)
        if m:
            price = _price_to_number(m.group(1), m.group(2))
            # a bare "under 5" is not a price; "under 900" means $900k to nobody
            if price is not None and (m.group(2) or price >= 10_000):
                result[key] = price
                text = _blank(text, m)

    if 'min_price' not in result and 'max_price' not in result:
        m = _SINGLE_PRICE_RE.search(text) or _DOLLAR_RE.search(text)
        if m:
            price = _price_to_number(m.group(1), m.group(2) if m.re is _SINGLE_PRICE_RE else None)
            if price is not None and price >= 10_000:
                # A single bare price ("around $1.2M") is a target, not a hard cutoff - use a
                # comfortable band around it rather than guessing which side the visitor meant.
                result['min_price'] = round(price * 0.85)
                result['max_price'] = round(price * 1.15)
                text = _blank(text, m)
    return text


def _town_pattern(name: str) -> re.Pattern:
    words = [re.escape(w) for w in re.split(r'[\s-]+', name.strip()) if w]
    return re.compile(r'(?<![a-z])' + r'[\s-]+'.join(words) + r'(?:\s*,?\s*(?:pa|nj|de|md|va|dc|wv)\b)?(?![a-z])', re.IGNORECASE)


def parse_search_query(text: str, known_cities: list[str], other_cities: Optional[list[str]] = None) -> dict:
    """Returns a dict with any of: city (first town), cities (every town named), zip, min_price,
    max_price, min_beds, min_baths, min_lot_size_acres, school_district, school_words,
    property_type_flag, unmatched. `known_cities` are the agent's own towns; `other_cities` every
    other real town in the MLS around them ("Name" or "Name, ST") - both matched as whole words."""
    lowered = ' ' + (text or '').lower().replace('’', "'") + ' '
    lowered = _WORD_NUM_RE.sub(lambda m: _WORD_NUMS[m.group(1).lower()], lowered)
    result: dict = {}
    rest = lowered

    # Acres before prices: "2 acres" is never "$2"
    m = _ACRES_RE.search(rest)
    if m:
        raw = m.group(1).lower()
        acres = _ACRE_WORDS.get(raw) if raw in _ACRE_WORDS else float(raw) if re.fullmatch(r'\d+(?:\.\d+)?', raw) else None
        if acres:
            result['min_lot_size_acres'] = acres
        rest = _blank(rest, m)

    m = _BEDS_RE.search(rest)
    if m:
        result['min_beds'] = int(m.group(1))
        rest = _blank(rest, m)
    m = _BATHS_RE.search(rest)
    if m:
        result['min_baths'] = float(m.group(1))
        rest = _blank(rest, m)

    rest = _parse_price(rest, result)

    m = re.search(r'(?<![a-z])(?:new(?:ly)?[\s-]+(?:construction|build|built)|brand[\s-]+new|new[\s-]+homes?)(?![a-z])', rest)
    if m:
        result['new_construction'] = True
        rest = _blank(rest, m)

    for phrase, flag in _PROPERTY_TYPE_KEYWORDS:
        m = re.search(r'(?<![a-z])' + re.escape(phrase) + r's?(?![a-z])', rest)
        if m:
            result.setdefault('property_type_flag', flag)
            rest = _blank(rest, m)

    # Home features ("pool", "finished basement", "no HOA") - before towns, so "pool" is never a town.
    from app.services.website import search_features
    feats, rest = search_features.extract(rest)
    if feats:
        result['features'] = feats

    # Towns: the agent's own first, then any other real MLS town; longest names first so
    # "Bryn Mawr" wins over a shorter name inside it. Several towns ("Wayne or Berwyn") all count.
    towns: list = []
    candidates = [(c, True) for c in known_cities if c] + [(c, False) for c in (other_cities or []) if c]
    candidates.sort(key=lambda c: -len(c[0].split(',')[0]))
    seen: set[str] = set()
    for city, own in candidates:
        name = city.split(',')[0].strip()
        key = name.lower()
        if key in seen:
            continue
        # "radnor schools" is the school district, not the town of Radnor
        m = next((x for x in _town_pattern(name).finditer(rest) if not _SCHOOL_AFTER_RE.match(rest, x.end())), None)
        if not m:
            continue
        if not own and key in _COMMON_WORD_TOWNS and not re.search(r',?\s*(?:pa|nj|de|md|va|dc|wv)\b', m.group(0)):
            continue
        seen.add(key)
        towns.append((m.start(), city))
        rest = _blank(rest, m)
    towns = [c for _, c in sorted(towns)]
    if towns:
        result['city'] = towns[0]
        result['cities'] = towns

    m = _SCHOOL_RE.search(rest)
    if m:
        # only the words right before "school" that aren't filler ("homes with radnor schools")
        words = m.group(1).strip().split()
        tail: list[str] = []
        for w in reversed(words):
            if w in _SCHOOL_STOPWORDS:
                break
            tail.insert(0, w)
        if tail:
            result['school_district'] = tail[-1]
            result['school_words'] = tail
        rest = _blank(rest, m)

    m = _ZIP_RE.search(rest)
    if m:
        result['zip'] = m.group(1)
        rest = _blank(rest, m)

    leftover = [w.strip(".,!?;:()'\"") for w in rest.split()]
    leftover = [w for w in leftover if w and w not in _FILLER and not re.fullmatch(r'[\d$.,+%-]+', w)]
    if leftover:
        result['unmatched'] = leftover[:8]
    return result
