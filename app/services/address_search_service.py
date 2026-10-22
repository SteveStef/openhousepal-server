"""Address search for picking ONE home (the CMA subject picker).

Why this exists (10/21): the old /api/properties/address search matched the typed text as one
substring of full_address ("1420 Locust #23H" is not a substring of "1420 LOCUST ST #23H, ..."),
then sorted for-sale homes first - so in a big condo building every active unit pushed the sold
unit the agent typed out of the 5 results. It also ran with no usable index (the trigram index had
been dropped by an old autogenerate migration), so every keystroke scanned the whole table.

How it works:
  1. parse_query() splits what was typed into a house number, a unit (any of #23H, Unit 23H,
     Apt 23H, 23H, 23-H) and words. Street words are put in one standard spelling (Street -> ST,
     Lane -> LN, North -> N ...) so "st"/"street" match either way, on both sides.
  2. One indexed query (pg_trgm GIN on full_address, migration e7b2d4f6a8c1) pulls the candidate
     rows: the house number at the start plus every distinctive word. Only light columns.
  3. score_candidate() ranks in Python: exact house number + street first, then the unit, then
     for-sale/most recent. A typo falls back to trigram word similarity.
  4. group_and_pick() keeps one row per home (house number + street + unit + zip): the active sale
     listing, else the latest sale record. A home whose only records are rentals comes back
     flagged (a CMA values a sale) instead of disappearing.
Pure functions (parse / score / group) are unit tested in tests/test_address_search_service.py.
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------------------------
# Spelling tables
# ---------------------------------------------------------------------------------------------

# canonical -> every spelling that means it (canonical included). SAINT shares ST on purpose:
# "St James Pl" and "Saint James Pl" are the same street, and the same rule runs on both sides.
_SUFFIX_FORMS: dict[str, tuple[str, ...]] = {
    "ST": ("ST", "STREET", "STR", "SAINT"),
    "AVE": ("AVE", "AVENUE", "AV", "AVN"),
    "RD": ("RD", "ROAD"),
    "DR": ("DR", "DRIVE", "DRV"),
    "LN": ("LN", "LANE"),
    "CT": ("CT", "COURT", "CRT"),
    "PL": ("PL", "PLACE"),
    "BLVD": ("BLVD", "BOULEVARD", "BLV"),
    "CIR": ("CIR", "CIRCLE", "CIRC"),
    "TER": ("TER", "TERRACE", "TERR"),
    "HWY": ("HWY", "HIGHWAY"),
    "PKWY": ("PKWY", "PARKWAY", "PKY"),
    "TRL": ("TRL", "TRAIL"),
    "SQ": ("SQ", "SQUARE"),
    "PIKE": ("PIKE", "PK"),
    "WAY": ("WAY",),
    "ALY": ("ALY", "ALLEY"),
    "CRES": ("CRES", "CRESCENT"),
    "XING": ("XING", "CROSSING"),
    "PT": ("PT", "POINT"),
    "MT": ("MT", "MOUNT"),
    "LOOP": ("LOOP",),
    "RUN": ("RUN",),
    "ROW": ("ROW",),
    "WALK": ("WALK",),
    "PATH": ("PATH",),
    "CV": ("CV", "COVE"),
    "HL": ("HL", "HILL"),
    "HTS": ("HTS", "HEIGHTS"),
    "EXT": ("EXT", "EXTENSION"),
    "RTE": ("RTE", "ROUTE", "RT"),
}
_DIRECTION_FORMS: dict[str, tuple[str, ...]] = {
    "N": ("N", "NORTH"), "S": ("S", "SOUTH"), "E": ("E", "EAST"), "W": ("W", "WEST"),
    "NE": ("NE", "NORTHEAST"), "NW": ("NW", "NORTHWEST"), "SE": ("SE", "SOUTHEAST"), "SW": ("SW", "SOUTHWEST"),
}
# Words people type for a place that the MLS spells differently.
_PLACE_FORMS: dict[str, tuple[str, ...]] = {
    "PHILADELPHIA": ("PHILADELPHIA", "PHILLY", "PHILA"),
    "PA": ("PA", "PENNSYLVANIA", "PENN"),
    "NJ": ("NJ", "JERSEY"),
    "DE": ("DE", "DELAWARE"),
    "MD": ("MD", "MARYLAND"),
    "VA": ("VA", "VIRGINIA"),
    "DC": ("DC",),
    "WV": ("WV",),
}

_CANON: dict[str, str] = {}
for _table in (_SUFFIX_FORMS, _DIRECTION_FORMS, _PLACE_FORMS):
    for _canon, _forms in _table.items():
        for _f in _forms:
            _CANON.setdefault(_f, _canon)
_SOFT_CANON = set(_SUFFIX_FORMS) | set(_DIRECTION_FORMS)  # may be missing/extra on either side
_FORMS: dict[str, tuple[str, ...]] = {**_SUFFIX_FORMS, **_DIRECTION_FORMS, **_PLACE_FORMS}
_ALL_SOFT_FORMS = sorted({f for c in _SOFT_CANON for f in _FORMS[c]})

_UNIT_WORDS = {"#", "UNIT", "APT", "APARTMENT", "STE", "SUITE", "NO", "NUM", "NUMBER", "RM", "ROOM", "BLDG", "FL", "FLOOR", "PH"}
_ROUTE_WORDS = {"RTE", "ROUTE", "RT", "HWY", "HIGHWAY", "SR", "US", "I", "CR", "COUNTY", "STATE", "INTERSTATE"}
_ORDINAL = re.compile(r"^\d+(ST|ND|RD|TH)$")
_ZIP = re.compile(r"^\d{5}$")
_HOUSE_NO = re.compile(r"^\d+[A-Z]?$|^\d+-\d+[A-Z]?$|^\d+-[A-Z]$")


def canon(token: str) -> str:
    return _CANON.get(token, token)


def forms(canonical: str) -> tuple[str, ...]:
    return _FORMS.get(canonical, (canonical,))


def norm_unit(raw: str) -> str:
    """'#APT. 2B' / 'Unit 23-H' / '23h' -> '23H'. Empty when nothing is left."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (raw or "").upper().replace("#", " "))
    words = [w for w in s.split() if w not in _UNIT_WORDS - {"PH"}]
    return "".join(words)


def _tokens(s: str) -> list[str]:
    s = (s or "").upper().replace("#", " # ")
    s = re.sub(r"[^A-Z0-9#\- ]", " ", s)          # commas, periods, slashes, apostrophes...
    s = re.sub(r"(?<![A-Z0-9])-|-(?![A-Z0-9])", " ", s)  # a dash only survives inside a token
    return s.split()


# ---------------------------------------------------------------------------------------------
# Query side
# ---------------------------------------------------------------------------------------------

@dataclass
class ParsedQuery:
    raw: str
    house_number: Optional[str] = None
    unit: Optional[str] = None
    words: list[str] = field(default_factory=list)  # canonical words after the house number
    last_partial: bool = True  # the last thing typed may be half a word

    @property
    def hard_words(self) -> list[str]:
        """Words that must appear (street name, town, zip) - not suffixes/directions."""
        out = [w for w in self.words if w not in _SOFT_CANON]
        if len(out) >= 2 and self.last_partial and out[-1] == self.words[-1] and self.unit is None:
            # "417 dogwood la" -> LA is half of LANE: don't require it.
            last = self.words[-1]
            if len(last) <= 4 and any(f.startswith(last) and f != last for f in _ALL_SOFT_FORMS):
                out = out[:-1]
        return out


def parse_query(q: str) -> ParsedQuery:
    raw = q or ""
    toks = _tokens(raw)
    pq = ParsedQuery(raw=raw, last_partial=not raw.endswith((" ", ",")))
    if not toks:
        return pq
    i = 0
    if _HOUSE_NO.match(toks[0]):  # 5-digit house numbers are common in MD/VA - not a zip here
        pq.house_number = toks[0]
        i = 1
    seen_street_word = False
    prev = ""
    while i < len(toks):
        t = toks[i]
        if t in _UNIT_WORDS and not (t == "NO" and i + 1 >= len(toks)):
            if t == "PH" and i + 1 >= len(toks):
                pq.unit = pq.unit or "PH"
            elif i + 1 < len(toks):
                nxt = toks[i + 1]
                # "Unit 23 H" - a lone letter right after the number belongs to it
                if i + 2 < len(toks) and re.match(r"^\d+$", nxt) and re.match(r"^[A-Z]$", toks[i + 2]) and toks[i + 2] not in _DIRECTION_FORMS:
                    nxt += toks[i + 2]
                    i += 1
                pq.unit = norm_unit(("PH" if t == "PH" else "") + nxt) or pq.unit
                i += 1
            prev = t
            i += 1
            continue
        has_digit = any(c.isdigit() for c in t)
        if (seen_street_word and has_digit and pq.unit is None and not _ORDINAL.match(t)
                and not _ZIP.match(t) and prev not in _ROUTE_WORDS and len(t) <= 6):
            pq.unit = norm_unit(t)
        else:
            c = canon(t.replace("-", "")) if not has_digit else t
            pq.words.append(c)
            if not has_digit or _ORDINAL.match(t) or prev in _ROUTE_WORDS:
                if c not in _SOFT_CANON or prev in _ROUTE_WORDS:
                    seen_street_word = True
        prev = t
        i += 1
    return pq


# ---------------------------------------------------------------------------------------------
# Candidate side
# ---------------------------------------------------------------------------------------------

@dataclass
class Candidate:
    listing_key: str
    street_address: str
    full_address: str
    city: str
    state: str
    zipcode: str
    home_status: str
    mls_property_type: Optional[str]
    price: Optional[float]
    modified: Optional[datetime]
    extra: dict = field(default_factory=dict)  # everything else passed straight through

    # parsed
    house_number: Optional[str] = None
    street_words: list[str] = field(default_factory=list)
    unit: str = ""
    place_words: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        street = (self.street_address or "").upper()
        unit = ""
        if "#" in street:
            street, unit = street.split("#", 1)
        else:
            m = re.search(r"\b(UNIT|APT|APARTMENT|STE|SUITE)\b\.?\s*(.+)$", street)
            if m:
                unit = m.group(2)
                street = street[: m.start()]
        self.unit = norm_unit(unit)
        toks = _tokens(street)
        if toks and re.match(r"^\d", toks[0]):
            self.house_number = toks[0]
            toks = toks[1:]
        self.street_words = [canon(t) if not any(ch.isdigit() for ch in t) else t for t in toks]
        place = _tokens(f"{self.city or ''} {self.state or ''}")
        self.place_words = [canon(t) for t in place]
        z = (self.zipcode or "").strip()[:5]
        if z:
            self.place_words.append(z)

    @property
    def is_rental(self) -> bool:
        return "LEASE" in (self.mls_property_type or "").upper()

    @property
    def status_tier(self) -> int:
        return status_tier(self.home_status)

    @property
    def group_key(self) -> tuple:
        core = tuple(w for w in self.street_words if w not in _SOFT_CANON)
        return (self.house_number or "", core, self.unit, (self.zipcode or "")[:5] or (self.city or "").upper())


def status_tier(status: Optional[str]) -> int:
    s = (status or "").upper().replace("-BRIGHT", "").strip()
    if s in ("ACTIVE", "COMING SOON"):
        return 0
    if s.startswith("PENDING") or "CONTRACT" in s:
        return 1
    if s.startswith("CLOSED") or s.startswith("SOLD"):
        return 2
    return 3


# ---------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------

@dataclass
class Score:
    hard_missing: int = 0      # typed words found nowhere on this home
    house_exact: bool = True
    street_points: float = 0   # 3 per exact street word, less for a prefix / near spelling
    soft_missing: int = 0      # typed St/Ln/N... this record doesn't carry
    fuzzy: int = 0             # words matched only by near spelling
    unit_tier: int = 0         # 0 exact .. 4 another unit
    extra_street_words: int = 0

    def key(self) -> tuple:
        return (self.hard_missing, not self.house_exact, -self.street_points, self.soft_missing,
                self.unit_tier, self.fuzzy, self.extra_street_words)


def _word_hit(word: str, pool: Iterable[str], allow_prefix: bool) -> float:
    """3 exact, 2 prefix of the word being typed, 1.5 near spelling, 1 an earlier word cut short
    ("1420 loc st"), 0 none."""
    pool = list(pool)
    if word in pool:
        return 3
    prefix_of = [p for p in pool if any(f.startswith(word) for f in forms(p))]
    if allow_prefix and prefix_of:
        return 2
    if len(word) >= 4 and not word.isdigit():
        for p in pool:
            if len(p) >= 4 and not p.isdigit() and difflib.SequenceMatcher(None, word, p).ratio() >= 0.8:
                return 1.5
    if len(word) >= 3 and prefix_of:
        return 1
    return 0


def score_candidate(pq: ParsedQuery, c: Candidate) -> Score:
    sc = Score()
    if pq.house_number:
        qh, ch = pq.house_number, (c.house_number or "")
        if qh == ch:
            sc.house_exact = True
        elif not pq.words and pq.last_partial and ch.startswith(qh):
            sc.house_exact = False  # still typing the number
        else:
            sc.hard_missing += 1
            sc.house_exact = False
    used_street: set[int] = set()
    n = len(pq.words)
    for idx, w in enumerate(pq.words):
        is_last = idx == n - 1 and pq.last_partial and pq.unit is None
        soft = w in _SOFT_CANON
        free_street = [sw for j, sw in enumerate(c.street_words) if j not in used_street]
        hit = _word_hit(w, free_street, is_last)
        if hit:
            # consume the matched street word so "ST" can't count twice
            for j, sw in enumerate(c.street_words):
                if j not in used_street and _word_hit(w, [sw], is_last) == hit:
                    used_street.add(j)
                    break
            sc.street_points += hit if not soft else hit / 3
            if hit < 2:
                sc.fuzzy += 1
            continue
        place_hit = _word_hit(w, c.place_words, is_last)
        if place_hit:
            if place_hit < 2:
                sc.fuzzy += 1
            continue  # a town / state / zip they typed after the street
        if soft or (is_last and len(w) <= 4 and any(f.startswith(w) for f in _ALL_SOFT_FORMS)):
            sc.soft_missing += 1
        else:
            sc.hard_missing += 1
    sc.extra_street_words = sum(1 for j, sw in enumerate(c.street_words) if j not in used_street and sw not in _SOFT_CANON)
    if pq.unit:
        if c.unit == pq.unit:
            sc.unit_tier = 0
        elif c.unit and pq.last_partial and c.unit.startswith(pq.unit):
            sc.unit_tier = 1
        elif not c.unit:
            sc.unit_tier = 3
        else:
            sc.unit_tier = 4
    else:
        sc.unit_tier = 0 if not c.unit else 1
    return sc


# ---------------------------------------------------------------------------------------------
# One row per home
# ---------------------------------------------------------------------------------------------

def _ts(c: Candidate) -> float:
    m = c.modified
    if not m:
        return 0.0
    if m.tzinfo is None:
        m = m.replace(tzinfo=timezone.utc)
    return m.timestamp()


def pick_record(records: list[Candidate]) -> Candidate:
    """Sale listings beat rentals; then Active/Coming Soon, Pending, the latest Closed sale,
    anything else; newest first within each."""
    return min(records, key=lambda c: (c.is_rental, c.status_tier, -_ts(c)))


@dataclass
class Hit:
    record: Candidate
    score: Score
    others: int          # other records of the same home
    rental_only: bool


def group_and_pick(pq: ParsedQuery, candidates: list[Candidate]) -> list[Hit]:
    groups: dict[tuple, list[Candidate]] = {}
    for c in candidates:
        groups.setdefault(c.group_key, []).append(c)
    hits: list[Hit] = []
    for recs in groups.values():
        rec = pick_record(recs)
        hits.append(Hit(rec, score_candidate(pq, rec), len(recs) - 1, rec.is_rental))
    hits.sort(key=lambda h: (*h.score.key(), h.rental_only, h.record.status_tier, -_ts(h.record)))
    if hits and hits[0].score.hard_missing == 0:
        hits = [h for h in hits if h.score.hard_missing == 0]
    else:
        # Nothing matches every word (a wrong town, a word we can't place): keep only homes with
        # the right house number and street name - never a list of unrelated streets.
        need = 2 if pq.house_number else 3
        hits = [h for h in hits if h.score.street_points >= need and (h.score.house_exact or not pq.house_number)]
    # A home whose only records are rentals still shows (greyed, so the agent knows why it can't
    # be the subject) when it is what they typed - but at most two of them fill out a broad search.
    out: list[Hit] = []
    rentals = 0
    for h in hits:
        if h.rental_only:
            exact_unit = bool(pq.unit) and h.score.unit_tier == 0 and h.score.hard_missing == 0
            if not exact_unit and rentals >= 2:
                continue
            rentals += 1
        out.append(h)
    return out


# ---------------------------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------------------------

_COLS = """listing_key, street_address, full_address, city, state, zipcode, home_status,
           mls_property_type, price, coalesce(modification_timestamp, updated_at, created_at) AS modified,
           latitude, longitude, google_rooftop_latitude, google_rooftop_longitude, img_src,
           bedrooms, bathrooms, living_area, home_type"""


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_candidate_sql(pq: ParsedQuery, limit: int) -> tuple[str, dict]:
    where: list[str] = ["full_address IS NOT NULL"]
    params: dict[str, Any] = {"lim": limit}
    if pq.house_number:
        if not pq.words and pq.unit is None and pq.last_partial:
            where.append("full_address ILIKE :hn")
            params["hn"] = f"{_like_escape(pq.house_number)}%"
        else:
            where.append("full_address ILIKE :hn")
            params["hn"] = f"{_like_escape(pq.house_number)} %"
    for k, w in enumerate(pq.hard_words):
        alts = forms(w)
        ors = []
        for a_i, a in enumerate(alts):
            name = f"w{k}_{a_i}"
            ors.append(f"full_address ILIKE :{name}")
            params[name] = f"%{_like_escape(a)}%"
        where.append("(" + " OR ".join(ors) + ")")
    sql = f"""SELECT {_COLS} FROM properties WHERE {' AND '.join(where)}
              ORDER BY (CASE WHEN upper(replace(home_status, '-BRIGHT', '')) IN ('ACTIVE','COMING SOON') THEN 0 ELSE 1 END),
                       modified DESC NULLS LAST
              LIMIT :lim"""
    return sql, params


def build_fuzzy_sql(pq: ParsedQuery, limit: int) -> Optional[tuple[str, dict]]:
    """Typo fallback ("239 windam"): trigram word similarity on the words, house number exact."""
    words = " ".join(w for w in pq.words if not w.isdigit())
    if len(words) < 3:
        return None
    where = ["full_address IS NOT NULL"]
    params: dict[str, Any] = {"w": words, "lim": limit}
    if pq.house_number:
        # The house number alone is selective (a few hundred rows at most) - rank them all by
        # spelling closeness instead of the 0.6 word-similarity cut-off ("windam" vs WINDHAM = 0.57).
        where.append("full_address ILIKE :hn")
        params["hn"] = f"{_like_escape(pq.house_number)} %"
    else:
        # No number: just the first distinctive word (the street name, usually) - the town words
        # would match a hundred thousand rows each.
        first = next((w for w in pq.hard_words if len(w) >= 4 and not w.isdigit()), None)
        if not first:
            return None
        params["w"] = first
        where.append(":w <% full_address")
    sql = f"""SELECT {_COLS} FROM properties WHERE {' AND '.join(where)}
              ORDER BY word_similarity(:w, full_address) DESC LIMIT :lim"""
    return sql, params


def _to_candidate(row: Any) -> Candidate:
    m = row._mapping
    return Candidate(
        listing_key=m["listing_key"], street_address=m["street_address"] or "", full_address=m["full_address"] or "",
        city=m["city"] or "", state=m["state"] or "", zipcode=m["zipcode"] or "", home_status=m["home_status"] or "",
        mls_property_type=m["mls_property_type"], price=m["price"], modified=m["modified"],
        extra={k: m[k] for k in ("latitude", "longitude", "google_rooftop_latitude", "google_rooftop_longitude",
                                 "img_src", "bedrooms", "bathrooms", "living_area", "home_type")},
    )


RENTAL_NOTE = "Rental listing - a CMA values a sale"


async def search_homes(db: AsyncSession, query: str, *, limit: int = 8, allow_rentals: bool = False,
                       candidate_limit: int = 400) -> dict:
    t0 = time.perf_counter()
    pq = parse_query(query)
    # "1" or "12 st" alone would read a third of the table - wait for a street name.
    too_short = not pq.hard_words and (not pq.house_number or len(pq.house_number) < 3)
    if too_short:
        return {"results": [], "tookMs": 0, "parsed": _parsed_dict(pq)}
    sql, params = build_candidate_sql(pq, candidate_limit)
    rows = (await db.execute(text(sql), params)).fetchall()
    cands = [_to_candidate(r) for r in rows]
    hits = group_and_pick(pq, cands) if cands else []
    fuzzy_used = False
    if not hits or hits[0].score.hard_missing > 0:
        fz = build_fuzzy_sql(pq, 60)
        if fz:
            frows = (await db.execute(text(fz[0]), fz[1])).fetchall()
            if frows:
                fuzzy_used = True
                seen = {c.listing_key for c in cands}
                cands += [c for c in (_to_candidate(r) for r in frows) if c.listing_key not in seen]
                hits = group_and_pick(pq, cands)
    hits = hits[:limit]

    # Sale price / date and the MLS photo only for the rows we return (raw_mls_data is heavy).
    keys = [h.record.listing_key for h in hits]
    extra: dict[str, dict] = {}
    if keys:
        erows = (await db.execute(text(
            """SELECT listing_key, raw_mls_data->>'ClosePrice' AS close_price, raw_mls_data->>'CloseDate' AS close_date,
                      raw_mls_data->>'ListPictureURL' AS pic, raw_mls_data->>'PropertySubType' AS sub_type,
                      CASE WHEN jsonb_typeof(photos) = 'array' THEN photos->>0 END AS photo0
               FROM properties WHERE listing_key = ANY(:keys)"""), {"keys": keys})).fetchall()
        extra = {r.listing_key: dict(r._mapping) for r in erows}

    results = [_result_dict(h, extra.get(h.record.listing_key, {}), allow_rentals, fuzzy_used) for h in hits]
    return {"results": results, "tookMs": round((time.perf_counter() - t0) * 1000), "parsed": _parsed_dict(pq)}


def _parsed_dict(pq: ParsedQuery) -> dict:
    return {"houseNumber": pq.house_number, "unit": pq.unit, "words": pq.words}


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _result_dict(h: Hit, ex: dict, allow_rentals: bool, fuzzy_used: bool) -> dict:
    c = h.record
    e = c.extra
    tier = c.status_tier
    close_price = _float(ex.get("close_price"))
    image = e.get("img_src") or ex.get("photo0") or ex.get("pic")
    exact = h.score.hard_missing == 0 and h.score.fuzzy == 0 and h.score.house_exact
    unit_ok = (h.score.unit_tier == 0)
    return {
        "listingKey": c.listing_key,
        "address": c.full_address,
        "street": c.street_address,
        "unit": c.unit or None,
        "city": c.city, "state": c.state, "zipcode": c.zipcode,
        "status": c.home_status,
        "statusTier": ["active", "pending", "sold", "off_market"][tier],
        "price": close_price if tier == 2 and close_price else c.price,
        "listPrice": c.price,
        "closePrice": close_price,
        "closeDate": ex.get("close_date"),
        "image": image,
        "lat": e.get("google_rooftop_latitude") or e.get("latitude"),
        "lng": e.get("google_rooftop_longitude") or e.get("longitude"),
        "beds": e.get("bedrooms"), "baths": e.get("bathrooms"), "sqft": e.get("living_area"),
        "propertyType": c.mls_property_type, "propertySubType": ex.get("sub_type") or e.get("home_type"),
        "isRental": h.rental_only,
        "selectable": (not h.rental_only) or allow_rentals,
        "note": RENTAL_NOTE if h.rental_only and not allow_rentals else None,
        "otherRecords": h.others,
        "match": "exact" if exact and unit_ok else ("close" if not fuzzy_used or exact else "fuzzy"),
    }
