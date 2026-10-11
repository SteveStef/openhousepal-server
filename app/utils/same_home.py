"""One house listed twice in the MLS (e.g. "2801 RIVERTON ROAD, MOORESTOWN" and "2801 RIVERTON RD,
CINNAMINSON" - two listing numbers, two towns, same home) should only show up once in a Showcase.

same_home_key() = house number + street (suffixes/directions normalized) + unit + beds + baths +
square feet. Town is left out on purpose: the duplicate is often filed under the neighbouring one.
Two listings with the same key only count as the same home if they're also within a few miles.
"""
import math
import re
from typing import Any, Iterable, List, Optional, Tuple

_SUFFIX = {
    "ROAD": "RD", "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "BOULEVARD": "BLVD", "PLACE": "PL", "CIRCLE": "CIR", "TERRACE": "TER", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "TRAIL": "TRL", "SQUARE": "SQ", "PIKE": "PIKE", "WAY": "WAY", "RUN": "RUN", "HILL": "HL", "LOOP": "LOOP",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_UNIT = re.compile(r"\s*(?:#|\bAPT\b|\bUNIT\b|\bSTE\b|\bSUITE\b)\s*([A-Z0-9-]+)\s*$")
# Same house filed under two towns sits a few miles apart at most (often the same coordinates).
MAX_APART_KM = 5.0


def _street(street: Optional[str]) -> Optional[Tuple[str, str, str]]:
    s = re.sub(r"[.,]", " ", (street or "").upper()).strip()
    unit = ""
    m = _UNIT.search(s)
    if m:
        unit, s = m.group(1), s[:m.start()].strip()
    parts = s.split()
    if len(parts) < 2 or not re.match(r"^\d+[A-Z]?$", parts[0]):
        return None
    name = " ".join(_SUFFIX.get(w, w) for w in parts[1:])
    return parts[0], name, unit


def _get(p: Any, *names: str):
    for n in names:
        v = p.get(n) if isinstance(p, dict) else getattr(p, n, None)
        if v is not None:
            return v
    return None


def same_home_key(p: Any) -> Optional[tuple]:
    """None when there isn't enough to be sure (no house number / no size) - never merged."""
    st = _street(_get(p, "street_address", "FullStreetAddress"))
    sqft = _get(p, "living_area", "LivingArea")
    if st is None or not sqft:
        return None
    beds = _get(p, "bedrooms", "BedroomsTotal")
    baths = _get(p, "bathrooms", "BathroomsTotal")
    return (*st, float(beds or 0), float(baths or 0), round(float(sqft)))


def _coords(p: Any) -> Optional[Tuple[float, float]]:
    lat, lng = _get(p, "latitude", "Latitude"), _get(p, "longitude", "Longitude")
    return (float(lat), float(lng)) if lat is not None and lng is not None else None


def close_enough(a: Any, b: Any) -> bool:
    ca, cb = _coords(a), _coords(b)
    if ca is None or cb is None:
        return True
    dlat, dlng = math.radians(cb[0] - ca[0]), math.radians(cb[1] - ca[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(ca[0])) * math.cos(math.radians(cb[0])) * math.sin(dlng / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h)) <= MAX_APART_KM


def is_same_home(a: Any, b: Any) -> bool:
    ka = same_home_key(a)
    return ka is not None and ka == same_home_key(b) and close_enough(a, b)


def dedupe_same_homes(props: Iterable[Any]) -> List[Any]:
    """Keeps the FIRST of each same-home group (callers order newest listing first)."""
    kept: List[Any] = []
    by_key: dict = {}
    for p in props:
        k = same_home_key(p)
        if k is not None and any(close_enough(p, q) for q in by_key.get(k, [])):
            continue
        if k is not None:
            by_key.setdefault(k, []).append(p)
        kept.append(p)
    return kept
