"""The town page's schools as shown - a render-time pass (public_site_routes.area_page_payload) over
what the town record already holds. No look-ups, nothing written back.

Why (outside grader, 10/27):
- The map's private-school dots were built by an older version in 8 towns and disagreed with the
  checked Private schools list next to it: a kindergarten, a closed school (St. Denis), the research's
  grades instead of the survey's (Torah Academy "PK-12", it's PK-8). The dots now follow the checked
  list: a school the check took off (closed, a preschool, not found) has no dot, a listed school's
  dot carries the list's name and grades, and a preschool/kindergarten never gets a dot.
- Public-school grade spans come from the newest federal file (NCES 2024-25), which lags a district
  reorganisation by a year or more: Central Bucks went K-5 / 6-8 / 9-12 in fall 2025 but the file
  still says K-6 / 7-9 / 10-12; Charlottesville's middle school is 6-8 now, the file says 7-8. A
  school's level (elementary / middle / high) almost never changes, its span does - so the map shows
  the level and no grade span for public schools.
- Renamed schools: the research writes "Sunrise Elementary (formerly Burnley-Moran Elementary)";
  the federal file still has the old name - the dot takes the new one.
- Per-town fixes a person checked (facts["_school_map_fixes"]: {"rename": {old: new}, "drop": [names]})
  survive a map rebuild - e.g. a school the district turned into an early-learning center.
"""
from __future__ import annotations

import re
from typing import Optional

_NOT_K12_RE = re.compile(r"kindergarten|pre-?school|pre-?k\b|nursery|day ?care|child ?care|childcare|children'?s house|playhouse|"
                         r"early (learning|childhood|education)|for young children|learning center|toddler|infant", re.IGNORECASE)
# The check's reasons that mean "there is no school here to show" (not "too far for this list").
_GONE_RE = re.compile(r"closed|preschool|early-childhood|daycare|not a (k-12 )?school|not found", re.IGNORECASE)
_STOP = {"the", "school", "schools", "of", "and", "st", "saint", "sts", "saints", "ss", "at", "for", "a", "inc",
         "academy", "elementary", "middle", "high", "el", "sch", "upper", "lower"}
_SAME_WORD = {"mt": "mount", "ft": "fort", "hs": "high", "ms": "middle", "elem": "elementary"}


def _words(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "").replace("’", "").replace("&", " and "))
    return {_SAME_WORD.get(w, w) for w in words if _SAME_WORD.get(w, w) not in _STOP}


def _key(name: str) -> str:
    """"Buford Middle" / "Buford Middle School" -> "bufordmiddle"."""
    s = re.sub(r"\bschool\b", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _miles(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    import math
    dlat = (b_lat - a_lat) * 69.0
    dlng = (b_lng - a_lng) * 69.0 * math.cos(math.radians((a_lat + b_lat) / 2))
    return math.hypot(dlat, dlng)


def _same_school(dot: dict, e: dict) -> bool:
    """A map dot and a list entry are one school: most of the shorter name's telling words in common
    ("Torah Academy of Greater Philadelphia" = "Caskey Torah Academy of Greater Philadelphia"), and -
    when both have a spot - within a mile and a half."""
    names = [n for n in (e.get("name"), e.get("research_name")) if n]
    ours = _words(dot.get("name") or "")
    if not ours:
        return False
    close = None
    if dot.get("lat") is not None and e.get("lat") is not None:
        miles = _miles(dot["lat"], dot["lng"], e["lat"], e["lng"])
        if miles > 1.5:
            return False
        close = miles <= 0.15
    for n in names:
        theirs = _words(n)
        if theirs and len(ours & theirs) / min(len(ours), len(theirs)) >= 0.6 and (ours & theirs):
            # One telling word against several ("Friends School Haverford" / "The Haverford School") is
            # only the same school on the same spot ("Fusion Academy - Ardmore" / "Fusion Academy").
            if min(len(ours), len(theirs)) > 1 or ours == theirs or close:
                return True
    return False


def _grade_num(g: str) -> Optional[int]:
    g = g.strip().lower().replace(".", "")
    if g in ("pk", "pre-k", "prek", "ns", "n", "nursery", "preschool"):
        return -1
    if g in ("k", "kg", "tk"):
        return 0
    return int(g) if g.isdigit() else None


def _top_grade(grades: Optional[str]) -> Optional[tuple[int, int]]:
    parts = [p for p in re.split(r"\s*(?:-|–|to|,)\s*", str(grades or "")) if p.strip()]
    nums = [n for n in (_grade_num(p) for p in parts) if n is not None]
    return (min(nums), max(nums)) if nums else None


def _grades_from_label(label: Optional[str]) -> Optional[str]:
    """The checked list's "Grades PK-8" / "Special education · Grades 6-12" -> "PK-8"."""
    m = re.search(r"\bGrades?\s+([A-Za-z0-9]+(?:\s*-\s*[A-Za-z0-9]+)?)", str(label or ""))
    return m.group(1).replace(" ", "") if m else None


def _not_k12(dot: dict) -> bool:
    if _NOT_K12_RE.search(dot.get("name") or ""):
        return True
    span = _top_grade(dot.get("grades"))
    return bool(span and (span[1] < 3 or span[0] == span[1]))


LISTED_DOT_MILES = 3  # a listed school this close to the town center gets a dot if the map has none


def private_dots(dots: list[dict], block: dict, center: Optional[tuple] = None, outline: Optional[dict] = None) -> list[dict]:
    ents = [e for e in (block or {}).get("entities") or [] if isinstance(e, dict) and e.get("name")]
    out = _private_dots_from_map(dots, ents, block)
    # A listed school the map has no dot for (the map build took "The Haverford School" for
    # "Friends School Haverford" by its one shared word) - its dot, from the list.
    if center and center[0] is not None:
        from app.services.areas import boundary_service as bs
        for e in ents:
            if e.get("lat") is None or e.get("lng") is None or _miles(center[0], center[1], e["lat"], e["lng"]) > LISTED_DOT_MILES:
                continue
            if any(_same_school(d, e) for d in out):
                continue
            d = {"id": f"list:{_key(e['name'])}", "name": e["name"], "lat": round(e["lat"], 6), "lng": round(e["lng"], 6),
                 "grades": _grades_from_label(e.get("category")),
                 "in_town": bool(outline and outline.get("rings") and bs.contains(outline["rings"], e["lng"], e["lat"]))}
            if not _not_k12(d):
                out.append(d)
    return out


def _private_dots_from_map(dots: list[dict], ents: list[dict], block: dict) -> list[dict]:
    gone = [r for r in (block or {}).get("_removed") or []
            if isinstance(r, dict) and r.get("name") and _GONE_RE.search(str(r.get("removed_reason") or ""))]
    out: list[dict] = []
    for d in dots:
        if not isinstance(d, dict) or not d.get("name") or _not_k12(d):
            continue
        listed = next((e for e in ents if _same_school(d, e)), None)
        if not listed and any(_same_school(d, {**r, "lat": None}) for r in gone):
            continue  # the check took it off the list: closed, a preschool, not found
        if listed:
            grades = _grades_from_label(listed.get("category"))
            # A dot from the research (not the survey) has only the research's grades - unchecked.
            d = {**d, "name": listed["name"], "grades": grades or (d.get("grades") if str(d.get("id") or "").startswith("pss:") else None)}
            if _not_k12(d):
                continue
        if any(_key(o["name"]) == _key(d["name"]) for o in out):
            continue
        out.append(d)
    return out


def _renames(facts: dict) -> dict[str, str]:
    """Old name -> new name: the research's "New Name (formerly Old Name)" and a person's own fixes."""
    out: dict[str, str] = {}
    ps = facts.get("public_schools") or {}
    for lvl in ("elementary_schools", "middle_schools", "high_schools"):
        for s in ps.get(lvl) or []:
            m = re.match(r"\s*(.+?)\s*\((?:formerly|previously|was)\s+(.+?)\)\s*$", str((s or {}).get("name") or ""), re.IGNORECASE)
            if m:
                out[_key(m.group(2))] = m.group(1)
    for old, new in ((facts.get("_school_map_fixes") or {}).get("rename") or {}).items():
        out[_key(old)] = new
    return out


def public_dots(dots: list[dict], facts: dict) -> list[dict]:
    renames = _renames(facts)
    drop = {_key(n) for n in (facts.get("_school_map_fixes") or {}).get("drop") or []}
    out = []
    for d in dots:
        if not isinstance(d, dict) or _key(d.get("name") or "") in drop:
            continue
        d = {k: v for k, v in d.items() if k != "grades"}
        new = renames.get(_key(d.get("name") or ""))
        if new:
            d["name"] = new
        out.append(d)
    return out


def tidy_school_facts(facts: dict, center: Optional[tuple] = None) -> dict:
    """See the module docstring. `center` = the town center (lat, lng). Returns a new facts dict;
    the stored record is untouched."""
    facts = dict(facts or {})
    b = facts.get("boundaries")
    if isinstance(b, dict) and isinstance(b.get("schools"), dict):
        schools = dict(b["schools"])
        schools["private"] = private_dots(schools.get("private") or [], facts.get("private_schools") or {}, center, b.get("outline"))
        schools["public"] = public_dots(schools.get("public") or [], facts)
        facts["boundaries"] = {**b, "schools": schools}
    return facts
