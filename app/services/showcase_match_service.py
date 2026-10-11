"""Python twin of frontend/lib/matchScore.ts - the "X% Match" badge a guest sees on each home in
their Showcase. Used server-side to pick which home a follow-up message leads with, so the message
pitches exactly the home the guest's own Showcase ranks highest.

KEEP IN SYNC with matchScore.ts. It is a deliberate line-for-line port, including its quirks
(field-name lists, the density-dedupe key, JS rounding), and is parity-tested against the real
TypeScript on live Showcase payloads. Change one, change both.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

COMPONENT_WEIGHTS = {
    "proximity": 22, "marketArea": 16, "size": 20, "propertyType": 12,
    "lot": 9, "ageStyle": 10, "recency": 7, "dataReliability": 4,
}
DEFAULT_PROXIMITY_HALF_LIFE = 2.5
MIN_DENSITY_REFERENCES = 10
DENSITY_RECENCY_WINDOWS_DAYS = [365, 548, 730, 1095, 1825]
MATCH_DISPLAY_CALIBRATION_EXPONENT = 0.70

SQFT_KEYS = ["AboveGradeFinishedArea", "above_grade_finished_area", "above_grade_sqft", "LivingArea", "livingArea", "squareFeet", "living_area", "sqft"]
TYPE_KEYS = ["PropertyType", "property_type", "home_type", "homeType", "StructureDesignType"]


def _js_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _get_num(obj: Any, keys: list[str]) -> float:
    if not isinstance(obj, dict):
        return 0
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "":
            n = _js_number(v)
            if n is not None:
                return n
    return 0


def _get_str(obj: Any, keys: list[str]) -> str:
    if not isinstance(obj, dict):
        return ""
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "":
            return str(v)
    return ""


def _normalize(value: Optional[str]) -> str:
    return re.sub(r"[_-]", " ", (value or "").strip().lower())


def _half_life_decay(delta: float, half_life: float) -> float:
    return math.pow(0.5, abs(delta) / half_life) if half_life > 0 else 0


def _haversine(lat, lng, slat, slng) -> Optional[float]:
    if not lat or not lng or not slat or not slng:
        return None
    r = math.radians
    d_lat, d_lng = r(lat - slat), r(lng - slng)
    a = math.sin(d_lat / 2) ** 2 + math.cos(r(slat)) * math.cos(r(lat)) * math.sin(d_lng / 2) ** 2
    return 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


CONDO_HINTS = ["condo", "unit", "flat", "apartment", "penthouse"]
ATTACHED_HINTS = ["townhouse", "townhome", "row", "twin", "attached"]
DETACHED_HINTS = ["single family", "singlefamily", "detached", "farm"]


def _type_family(raw: Optional[str]) -> Optional[str]:
    v = _normalize(raw)
    if not v:
        return None
    if any(t in v for t in CONDO_HINTS):
        return "condo"
    if any(t in v for t in ATTACHED_HINTS):
        return "attached"
    if any(t in v for t in DETACHED_HINTS):
        return "detached"
    return None


_STYLE_FAMILIES = [
    ("colonial traditional", ["colonial", "traditional", "georgian", "federal"]),
    ("modern", ["modern", "contemporary", "mid century"]),
    ("tudor", ["tudor"]),
    ("ranch", ["ranch", "split level", "bi level"]),
    ("cape", ["cape cod", "cape"]),
    ("victorian", ["victorian", "queen anne"]),
    ("craftsman", ["craftsman", "arts and crafts"]),
    ("farmhouse", ["farmhouse"]),
    ("mediterranean", ["mediterranean", "spanish", "mission"]),
]


def _style_family(raw: Optional[str]) -> Optional[str]:
    v = _normalize(raw)
    if not v:
        return None
    for name, tokens in _STYLE_FAMILIES:
        if any(t in v for t in tokens):
            return name
    return v


def _lot_sqft(p: Any) -> float:
    return (_get_num(p, ["lot_size_sqft", "LotSizeSquareFeet", "lotSizeSquareFeet", "lot_size", "lotSize"])
            or _get_num(p, ["acres", "LotSizeAcres", "lotSizeAcres", "lot_size_acres"]) * 43560)


def _distance(p: Any, s: Any) -> Optional[float]:
    provided = p.get("distanceMiles", p.get("distance_miles")) if isinstance(p, dict) else None
    if provided is not None:
        n = _js_number(provided)
        if n is not None:
            return n
    return _haversine(_get_num(p, ["Latitude", "latitude"]), _get_num(p, ["Longitude", "longitude"]),
                      _get_num(s, ["Latitude", "latitude"]), _get_num(s, ["Longitude", "longitude"]))


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    o = sorted(values)
    m = len(o) // 2
    return o[m] if len(o) % 2 else (o[m - 1] + o[m]) / 2


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def calibrate_match_percentage(raw_relevance: float) -> int:
    raw = max(0.0, min(1.0, raw_relevance if math.isfinite(raw_relevance) else 0.0))
    return max(1, min(100, _js_round(100 * math.pow(raw, MATCH_DISPLAY_CALIBRATION_EXPONENT))))


def _parse_ts(raw: str) -> Optional[float]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


def _local_density(subject: Any, comparison: list, prop: Any) -> dict:
    supplied = None
    for src in (prop, subject):
        if isinstance(src, dict):
            supplied = src.get("similarity_context") or (src.get("similarity_details") or {}).get("density_context")
            if supplied:
                break
    if supplied:
        hl = _js_number(supplied.get("distance_half_life_miles", supplied.get("local_market_half_life_miles")))
        if hl is not None and hl > 0:
            return {"halfLife": max(0.5, min(5, hl))}

    subj_family = _type_family(_get_str(subject, TYPE_KEYS))
    subj_sqft = _get_num(subject, SQFT_KEYS)
    subj_lot = _lot_sqft(subject)
    subj_year = _get_num(subject, ["YearBuilt", "year_built", "yearBuilt"])
    seen: set[str] = set()
    refs = []
    for ref in comparison:
        family = _type_family(_get_str(ref, TYPE_KEYS))
        if subj_family and family != subj_family:
            continue
        sqft = _get_num(ref, SQFT_KEYS)
        if subj_sqft and (not sqft or sqft < subj_sqft * 0.75 or sqft > subj_sqft * 1.25):
            continue
        if subj_family == "detached":
            rl = _lot_sqft(ref)
            if subj_lot and (not rl or rl < subj_lot * 0.5 or rl > subj_lot * 2):
                continue
            ry = _get_num(ref, ["YearBuilt", "year_built", "yearBuilt"])
            if subj_year and (not ry or abs(ry - subj_year) > 40):
                continue
        d = _distance(ref, subject)
        if d is None or d <= 0.01:
            continue
        key = _normalize("|".join([
            _get_str(ref, ["street_address", "StreetAddress", "address"]),
            _get_str(ref, ["city", "City"]), _get_str(ref, ["state", "State"]),
            _get_str(ref, ["zipcode", "PostalCode"]),
        ])) or f"{_get_num(ref, ['latitude', 'Latitude']):.5f}|{_get_num(ref, ['longitude', 'Longitude']):.5f}"
        if not key or key in seen:
            continue
        seen.add(key)
        refs.append((d, ref))

    def is_current(r):
        st = _normalize(_get_str(r, ["status", "home_status", "StandardStatus", "standardStatus"]))
        return st.startswith("active") or st.startswith("pending") or st.startswith("coming soon") or st in ("under contract", "contract")

    def close_ts(r):
        return _parse_ts(_get_str(r, ["close_date", "sale_date", "CloseDate"]))

    lifecycle_known = any(is_current(r) or close_ts(r) is not None for _, r in refs)
    selected = refs
    if lifecycle_known:
        for max_days in DENSITY_RECENCY_WINDOWS_DAYS:
            cutoff = _now_ms() - max_days * 86400000
            recent = [(d, r) for d, r in refs if is_current(r) or (close_ts(r) is not None and close_ts(r) >= cutoff)]
            selected = recent
            if len(recent) >= MIN_DENSITY_REFERENCES:
                break
        if len(selected) < MIN_DENSITY_REFERENCES:
            selected = refs
    distances = sorted(d for d, _ in selected)
    used = distances[7:12] if len(distances) >= MIN_DENSITY_REFERENCES else []
    observed = _median(used)
    return {"halfLife": DEFAULT_PROXIMITY_HALF_LIFE if observed is None else max(0.5, min(5, observed))}


def _market_area(p: Any, s: Any) -> Optional[float]:
    fields = [
        (["SchoolDistrictName", "schoolDistrictName", "school_district_name", "schoolDistrict", "school_district"], 0.35),
        (["township", "Township", "municipality", "Municipality"], 0.25),
        (["subdivision_name", "subdivisionName", "SubdivisionName", "neighborhood", "neighborhood_name"], 0.25),
        (["City", "city"], 0.15),
    ]
    pieces = []
    for keys, w in fields:
        sv, pv = _normalize(_get_str(s, keys)), _normalize(_get_str(p, keys))
        if sv and pv:
            pieces.append((1 if sv == pv else 0, w))
    den = sum(w for _, w in pieces)
    return sum(sc * w for sc, w in pieces) / den if den else None


def _recency(p: Any) -> Optional[float]:
    st = _normalize(_get_str(p, ["status", "StandardStatus", "standardStatus"]))
    if st not in ("sold", "closed"):
        return None
    ts = _parse_ts(_get_str(p, ["close_date", "sale_date", "CloseDate"]))
    if ts is None:
        return None
    days = max(0, (_now_ms() - ts) / 86400000)
    return max(0.15, math.pow(0.5, days / 183))


def _reliability(p: Any, s: Any) -> float:
    st = _normalize(_get_str(p, ["status", "StandardStatus", "standardStatus"]))
    sold = st in ("sold", "closed")
    price = _get_num(p, ["sale_price", "close_price", "ClosePrice"]) if sold else _get_num(p, ["list_price", "ListPrice", "price"])
    verified = bool(price and (not sold or (p.get("price_verified") is not False)))
    sqft = _get_num(p, ["LivingArea", "livingArea", "squareFeet", "living_area", "sqft"])
    has_dist = _distance(p, s) is not None
    has_date = not sold or _recency(p) is not None
    has_type = bool(_type_family(_get_str(p, TYPE_KEYS)))
    return ((0.35 if verified else 0) + (0.20 if sqft else 0) + (0.15 if has_dist else 0)
            + (0.15 if has_date else 0) + (0.10 if has_type else 0) + (0.05 if _lot_sqft(p) else 0))


def _fact_blob(p: Any) -> str:
    if not isinstance(p, dict):
        return ""
    vals = []
    for k in ("public_remarks", "PublicRemarks", "amenities", "features", "interior_features",
              "exterior_features", "pool_features", "other_structures", "architectural_style"):
        v = p.get(k)
        vals.extend(v if isinstance(v, list) else [v])
    return _normalize(" ".join(str(v) for v in vals if v))


_NEG = re.compile(r"(?:\bno\b|\bwithout\b|\blacks?\b|does not have)\s+(?:an?\s+|the\s+)?$")
_FUTURE = re.compile(r"(?:potential|opportunity|option|plans?|proposed|possible|future|ready|roughed|plumbed|wired)\s+(?:in\s+place\s+)?(?:for|to\s+(?:add|create|build|convert))\s+(?:an?\s+|the\s+)?$")
_COULD = re.compile(r"(?:could|can|may)\s+be\s+(?:used|finished|converted|developed)\s+(?:as|into)\s+(?:an?\s+|the\s+)?$")
_POOL = re.compile(r"\b(pool|swimming pool|in ground|in-ground)\b", re.I)
_ACCESSORY = re.compile(r"\b(guest (house|suite|wing|apartment)|in law|in-law|adu|accessory dwelling|carriage house|apartment (?:above|over))\b", re.I)


def _has_feature(blob: str, pattern: re.Pattern) -> bool:
    for m in pattern.finditer(blob):
        prefix = blob[max(0, m.start() - 70):m.start()]
        if _NEG.search(prefix) or _FUTURE.search(prefix) or _COULD.search(prefix):
            continue
        return True
    return False


def _product_modifier(p: Any, s: Any) -> float:
    mod = 1.0
    st, pt = _type_family(_get_str(s, TYPE_KEYS)), _type_family(_get_str(p, TYPE_KEYS))
    if st and pt and st != pt:
        mod *= 0.55
    sf, pf = _fact_blob(s), _fact_blob(p)
    if sf and pf:
        if _has_feature(sf, _POOL) != _has_feature(pf, _POOL):
            mod *= 0.90
        if _has_feature(sf, _ACCESSORY) != _has_feature(pf, _ACCESSORY):
            mod *= 0.82
    return max(0.50, min(1.0, mod))


def compute_match_score(prop: dict, subject: dict, comparison: Optional[list] = None) -> int:
    """The badge percentage (1-100) for `prop` against the Showcase's comparison home."""
    comparison = comparison or []
    s_sqft, p_sqft = _get_num(subject, SQFT_KEYS), _get_num(prop, SQFT_KEYS)
    s_lot, p_lot = _lot_sqft(subject), _lot_sqft(prop)
    s_year = _get_num(subject, ["YearBuilt", "yearBuilt", "year_built"])
    p_year = _get_num(prop, ["YearBuilt", "yearBuilt", "year_built"])
    s_type, p_type = _type_family(_get_str(subject, TYPE_KEYS)), _type_family(_get_str(prop, TYPE_KEYS))
    style_keys = ["ArchitecturalStyle", "architectural_style", "structure_type"]
    s_style, p_style = _style_family(_get_str(subject, style_keys)), _style_family(_get_str(prop, style_keys))
    distance = _distance(prop, subject)
    density = _local_density(subject, comparison, prop)

    comps: list[tuple[float, float]] = []  # (weight, score)
    if distance is not None:
        comps.append((COMPONENT_WEIGHTS["proximity"], _half_life_decay(distance, density["halfLife"])))
    area = _market_area(prop, subject)
    if area is not None:
        comps.append((COMPONENT_WEIGHTS["marketArea"], area))
    if s_sqft and p_sqft:
        comps.append((COMPONENT_WEIGHTS["size"], _half_life_decay(p_sqft - s_sqft, max(s_sqft * 0.22, 400))))
    if s_type and p_type:
        comps.append((COMPONENT_WEIGHTS["propertyType"], 1 if s_type == p_type else 0))
    if s_lot and p_lot:
        comps.append((COMPONENT_WEIGHTS["lot"], _half_life_decay(p_lot - s_lot, max(s_lot * 0.4, 10890))))
    age_style = []
    if s_year and p_year:
        age_style.append((_half_life_decay(p_year - s_year, 22), 0.65))
    if s_style and p_style:
        age_style.append((1 if s_style == p_style else 0.55, 0.35))
    if age_style:
        den = sum(w for _, w in age_style)
        comps.append((COMPONENT_WEIGHTS["ageStyle"], sum(sc * w for sc, w in age_style) / den))
    rec = _recency(prop)
    if rec is not None:
        comps.append((COMPONENT_WEIGHTS["recency"], rec))
    comps.append((COMPONENT_WEIGHTS["dataReliability"], _reliability(prop, subject)))

    den = sum(w for w, _ in comps)
    if not den:
        return calibrate_match_percentage(0.5)
    base = sum(w * sc for w, sc in comps) / den
    return calibrate_match_percentage(base * _product_modifier(prop, subject))
