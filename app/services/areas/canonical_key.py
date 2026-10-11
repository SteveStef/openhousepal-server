"""Pure, DB-independent canonicalization logic for MarketArea deduplication.

Deliberately has zero ORM/DB imports so it can be safely imported both from application code
(area_enrichment_service.get_or_create_market_area) and from the Alembic migration that backfills
canonical_key on existing rows and consolidates any pre-existing duplicates - the two callers MUST
agree on exactly the same algorithm, and importing this one module instead of hand-copying the
logic into the migration is what guarantees that instead of hoping two copies stay in sync.

No geocoding, no external API calls, no network - deterministic and local, on purpose (see
get_or_create_market_area's docstring for why a paid Google lookup must never be required just to
tell that "Villanova", "Villanova PA", and "Villanova, Pennsylvania" are the same town).
"""
import re
import unicodedata
from typing import Optional

_US_STATE_ABBRS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il", "in", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm",
    "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa",
    "wv", "wi", "wy",
}
_US_STATE_SINGLE_WORD_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma", "oregon", "pennsylvania",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "wisconsin", "wyoming",
}
_US_STATE_MULTI_WORD_NAMES = (
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "rhode island", "south carolina", "south dakota", "west virginia", "district of columbia",
)
# "Township"/"Borough" etc. are how PA/NJ especially name real municipalities, but an agent (or
# this same agent later) typing "Radnor Township" vs. just "Radnor" is still one real-world place -
# only ever stripped as a TRAILING word, never mid-name, so it can't eat a town whose actual name
# happens to contain one of these as a non-suffix word.
_MUNICIPALITY_SUFFIXES = {"township", "twp", "borough", "boro", "town", "village", "city", "cdp"}
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_PUNCT_RE = re.compile(r"[.,]")
_WS_RE = re.compile(r"\s+")


def normalize_geography_name(name: str) -> str:
    """Canonical form for "is this the same real-world place" comparison - deliberately more
    aggressive than a bare .strip().lower(), so "Villanova", "Villanova, PA", "Villanova PA",
    "Villanova, Pennsylvania", and "Villanova Township" all normalize identically, without
    over-normalizing two genuinely different towns onto the same key."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = _ZIP_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip().lower()
    for phrase in _US_STATE_MULTI_WORD_NAMES:
        text = text.replace(phrase, " ")
    words = [w for w in text.split() if w not in _US_STATE_ABBRS and w not in _US_STATE_SINGLE_WORD_NAMES]
    while words and words[-1] in _MUNICIPALITY_SUFFIXES:
        words.pop()
    return " ".join(words)


def compute_canonical_key(name: str, state: str, area_type: str, parent_area_name: Optional[str] = None) -> str:
    """The stored, DB-unique identity for a real-world geography.

    Includes state + area_type + normalized name always. For 'neighborhood' areas, also includes
    the normalized parent city name, so two identically-named neighborhoods under genuinely
    different parent cities remain two different canonical areas (e.g. two towns that each happen
    to have a "Rittenhouse Square" would NOT collapse into one MarketArea) - see
    get_or_create_market_area, which resolves parent_area_name via geocoding BEFORE calling this,
    for the neighborhood case specifically (the one case where geocoding genuinely is needed).

    area_type is part of the key (not just name+state) so a 'city' and a differently-typed
    'neighborhood' that happen to share a normalized name are treated as distinct geographies -
    more correct than collapsing them, and what the canonical-identity spec explicitly calls for."""
    state_norm = (state or "").strip().upper()
    at = area_type if area_type in ("city", "neighborhood") else "city"
    norm_name = normalize_geography_name(name)
    if at == "neighborhood":
        norm_parent = normalize_geography_name(parent_area_name) if parent_area_name else ""
        return f"{state_norm}|{at}|{norm_name}|{norm_parent}"
    return f"{state_norm}|{at}|{norm_name}"


_STATUS_RANK = {"ENRICHED": 4, "PARTIAL": 3, "RESEARCHING": 2, "PENDING": 1, "FAILED": 0}


def survivor_sort_key(row: dict) -> tuple:
    """Ranks a MarketArea row (as a plain dict with the fields below) for "which duplicate should
    survive a consolidation merge" - higher is better. Prefers the most-researched, most-current
    row; ties broken by richer facts, then by which row is older (more likely to already be the
    one other tables/agents reference). Used by the canonical-uniqueness migration's duplicate
    consolidation step - see that migration for how ties/merges are applied."""
    created_at = row.get("created_at")
    created_epoch = created_at.timestamp() if created_at is not None else 0.0
    return (
        _STATUS_RANK.get(row.get("enrichment_status"), -1),
        row.get("research_version") if row.get("research_version") is not None else -1,
        1 if row.get("narrative") else 0,
        len(row.get("facts") or {}),
        -created_epoch,
    )
