import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select

from app.models.database import NcesAttendanceBoundary, Property

EARTH_RADIUS_M = 6378137.0

SCHOOL_LEVELS = {
    "elementary": {
        "label": "Elementary School",
        "mls_attr": "elementary_school",
        "nces_level": "1",
        "grades": set(range(0, 6)),
        "representative_grade": 3,
    },
    "middle": {
        "label": "Middle School",
        "mls_attr": "middle_or_junior_school",
        "nces_level": "2",
        "grades": set(range(6, 9)),
        "representative_grade": 7,
    },
    "high": {
        "label": "High School",
        "mls_attr": "high_school",
        "nces_level": "3",
        "grades": set(range(9, 13)),
        "representative_grade": 10,
    },
}

INVALID_SCHOOL_VALUES = {
    "",
    "0",
    "00",
    "n/a",
    "na",
    "none",
    "unknown",
    "not available",
    "not provided",
    "see remarks",
    "call school board",
    "call district",
    "buyer to verify",
    "verify",
    "tbd",
}


@dataclass
class SchoolAssignment:
    elementary: Optional[str]
    middle: Optional[str]
    high: Optional[str]
    sources: dict[str, Optional[str]]
    bright: dict[str, Optional[str]]
    nces_matches: dict[str, list[dict[str, Any]]]
    final: dict[str, Optional[str]]
    coordinates: dict[str, Optional[float]]

    def to_nearby_places(self, nearby_place_cls) -> list[Any]:
        places = []
        for key, config in SCHOOL_LEVELS.items():
            name = self.final.get(key)
            if name:
                places.append(nearby_place_cls(name=name, place_type=config["label"]))
        return places

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "coordinates": self.coordinates,
            "bright_mls": self.bright,
            "nces_boundary_matches": self.nces_matches,
            "final": self.final,
            "sources": self.sources,
        }


def _clean_school_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    if cleaned.lower() in INVALID_SCHOOL_VALUES:
        return None
    return cleaned or None


def grade_to_number(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in {"", "UG", "AE", "N"}:
        return None
    if normalized in {"PK", "PK3", "PK4", "P3", "P4"}:
        return -1
    if normalized in {"KG", "K", "KN"}:
        return 0
    try:
        return int(normalized)
    except ValueError:
        return None


def grade_range_targets(grade_low: Optional[str], grade_high: Optional[str]) -> set[str]:
    low = grade_to_number(grade_low)
    high = grade_to_number(grade_high)
    if low is None or high is None:
        return set()
    if low > high:
        low, high = high, low
    covered = set(range(max(0, low), min(12, high) + 1))
    return {key for key, config in SCHOOL_LEVELS.items() if covered & config["grades"]}


def wgs84_to_web_mercator(latitude: float, longitude: float) -> tuple[float, float]:
    lat = max(min(float(latitude), 89.5), -89.5)
    lng = float(longitude)
    x = EARTH_RADIUS_M * math.radians(lng)
    y = EARTH_RADIUS_M * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def _point_on_segment(x: float, y: float, ax: float, ay: float, bx: float, by: float, epsilon: float = 1e-7) -> bool:
    cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
    if abs(cross) > epsilon:
        return False
    dot = (x - ax) * (bx - ax) + (y - ay) * (by - ay)
    if dot < -epsilon:
        return False
    squared_len = (bx - ax) ** 2 + (by - ay) ** 2
    if squared_len <= epsilon:
        return (x - ax) ** 2 + (y - ay) ** 2 <= epsilon
    return dot <= squared_len + epsilon


def point_in_ring(x: float, y: float, ring: list[list[float]]) -> bool:
    if len(ring) < 3:
        return False

    inside = False
    prev_x, prev_y = ring[-1]
    for cur_x, cur_y in ring:
        if _point_on_segment(x, y, prev_x, prev_y, cur_x, cur_y):
            return True
        if (cur_y > y) != (prev_y > y):
            intersection_x = (prev_x - cur_x) * (y - cur_y) / (prev_y - cur_y) + cur_x
            if x <= intersection_x:
                inside = not inside
        prev_x, prev_y = cur_x, cur_y
    return inside


def point_in_polygon_geometry(x: float, y: float, geometry: list[list[list[float]]]) -> bool:
    # Even-odd containment works for multipart polygons and holes without relying on ring orientation.
    contains_count = 0
    for ring in geometry or []:
        if point_in_ring(x, y, ring):
            contains_count += 1
    return contains_count % 2 == 1


def _candidate_targets(boundary: NcesAttendanceBoundary) -> set[str]:
    targets = grade_range_targets(boundary.grade_low, boundary.grade_high)
    for key, config in SCHOOL_LEVELS.items():
        if boundary.level == config["nces_level"]:
            targets.add(key)
    return targets


def _grade_width(boundary: NcesAttendanceBoundary) -> int:
    low = grade_to_number(boundary.grade_low)
    high = grade_to_number(boundary.grade_high)
    if low is None or high is None:
        return 99
    return abs(high - low) + 1


def _match_score(boundary: NcesAttendanceBoundary, target: str) -> float:
    config = SCHOOL_LEVELS[target]
    score = 0.0
    if boundary.level == config["nces_level"]:
        score += 100.0
    low = grade_to_number(boundary.grade_low)
    high = grade_to_number(boundary.grade_high)
    if low is not None and high is not None:
        if low > high:
            low, high = high, low
        covered = set(range(max(0, low), min(12, high) + 1))
        score += len(covered & config["grades"]) * 5
        if config["grades"].issubset(covered):
            score += 20
        if low <= config["representative_grade"] <= high:
            score += 10
        score -= _grade_width(boundary) * 0.1
    if boundary.open_enroll == "1":
        score -= 5
    return score


def _summarize_boundary(boundary: NcesAttendanceBoundary, target: Optional[str] = None) -> dict[str, Any]:
    return {
        "school_id": boundary.school_id,
        "school_name": boundary.school_name,
        "lea_id": boundary.lea_id,
        "state": boundary.state,
        "grade_low": boundary.grade_low,
        "grade_high": boundary.grade_high,
        "level": boundary.level,
        "source_name": boundary.source_name,
        "open_enroll": boundary.open_enroll,
        "multi_boundary": boundary.multi_boundary,
        "target": target,
    }


def _choose_unique_best(matches: list[NcesAttendanceBoundary], target: str) -> tuple[Optional[NcesAttendanceBoundary], bool]:
    if not matches:
        return None, False
    scored = sorted(matches, key=lambda boundary: (-_match_score(boundary, target), boundary.school_name, boundary.school_id))
    best = scored[0]
    best_score = _match_score(best, target)
    tied = [candidate for candidate in scored if abs(_match_score(candidate, target) - best_score) < 0.0001]
    names = {candidate.school_name.strip().lower() for candidate in tied}
    if len(names) > 1:
        return None, True
    return best, False


async def _nces_matches_for_point(db, lat: Optional[float], lng: Optional[float], state: Optional[str]) -> dict[str, list[NcesAttendanceBoundary]]:
    """The actual point-in-polygon NCES lookup - only ever needed lat/lng/state, never anything
    else Property-specific, so this is reusable as-is for area-centroid lookups (see
    app/services/areas/area_enrichment_service.py). `_nces_matches_for_property` below is now
    just a thin adapter for the property-specific caller."""
    if lat is None or lng is None or not state:
        return {key: [] for key in SCHOOL_LEVELS}

    x, y = wgs84_to_web_mercator(lat, lng)
    result = await db.execute(
        select(NcesAttendanceBoundary)
        .where(
            NcesAttendanceBoundary.state == state.upper(),
            NcesAttendanceBoundary.bbox_xmin <= x,
            NcesAttendanceBoundary.bbox_xmax >= x,
            NcesAttendanceBoundary.bbox_ymin <= y,
            NcesAttendanceBoundary.bbox_ymax >= y,
        )
    )

    matches_by_level: dict[str, list[NcesAttendanceBoundary]] = defaultdict(list)
    for boundary in result.scalars().all():
        if point_in_polygon_geometry(x, y, boundary.geometry):
            for target in _candidate_targets(boundary):
                matches_by_level[target].append(boundary)

    return {key: matches_by_level.get(key, []) for key in SCHOOL_LEVELS}


async def _nces_matches_for_property(db, property: Property) -> dict[str, list[NcesAttendanceBoundary]]:
    return await _nces_matches_for_point(db, property.latitude, property.longitude, property.state)


async def resolve_school_assignment(db, property: Property) -> SchoolAssignment:
    bright = {
        key: _clean_school_name(getattr(property, config["mls_attr"], None))
        for key, config in SCHOOL_LEVELS.items()
    }
    final = dict(bright)
    sources = {key: "bright_mls" if value else None for key, value in bright.items()}
    nces_matches_by_level = await _nces_matches_for_property(db, property)
    debug_matches: dict[str, list[dict[str, Any]]] = {}

    for key in SCHOOL_LEVELS:
        debug_matches[key] = [_summarize_boundary(boundary, key) for boundary in nces_matches_by_level.get(key, [])]
        if final.get(key):
            continue

        best, ambiguous = _choose_unique_best(nces_matches_by_level.get(key, []), key)
        if best and _clean_school_name(best.school_name):
            final[key] = best.school_name
            sources[key] = "nces"
        elif ambiguous:
            sources[key] = "ambiguous_nces"

    return SchoolAssignment(
        elementary=final.get("elementary"),
        middle=final.get("middle"),
        high=final.get("high"),
        sources=sources,
        bright=bright,
        nces_matches=debug_matches,
        final=final,
        coordinates={"latitude": property.latitude, "longitude": property.longitude},
    )
