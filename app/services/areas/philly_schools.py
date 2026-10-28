"""Philadelphia's assigned public schools from the School District's own zone maps.

Why (outside grader, 10/27, all 30 Philadelphia neighborhoods): the town research named schools
that are NEAR a neighborhood as if every home were assigned to them - Central High, Masterman, AMY
at James Martin, AMY Northwest, Saul, Bodine, Palumbo, Constitution are citywide schools a student
applies to, not neighborhood schools; it listed renamed schools twice (Andrew Jackson = Fanny
Jackson Coppin; Chester A. Arthur = Marian Anderson Neighborhood Academy) and stated one high
school for every home where the neighborhood spans several zones.

The district publishes each school year's zones (SDP_Catchment_2526, opendataphilly.org - stored in
app/data/sdp_catchments_2526.json, elementary zones; each also names its middle and high school).
Each of the neighborhood's homes is placed in its zone, so the page lists only schools a home there
is actually zoned for, with the share of homes each covers. Application-based schools never appear
as assigned. Charter schools that run a neighborhood zone (Mastery at Gratz, Universal at
Audenried...) do - homes there are assigned to them.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.services.areas import boundary_service as bs

_FILE = Path(__file__).resolve().parents[2] / "data" / "sdp_catchments_2526.json"
MIN_SHARE = 5        # % of the neighborhood's homes - a sliver of a neighboring zone isn't worth a line
MIN_HOMES = 20

# The district's approved facilities plan (board vote 4/30/2026) changes these schools in later years -
# open for 2025-26, so they are listed, with the notice (grader: "do not mark them as already closed").
FACILITIES_PLAN_URL = "https://www.philasd.org/blog/2026/04/30/acceleratingoppvote/"
_FACILITIES_PLAN = {"robert morris", "laura w waring", "laura wheeler waring", "penn treaty"}

PHILLY_CAVEAT = ("Public schools are run by the School District of Philadelphia. The neighborhood can overlap more than one "
                 "school zone, so check the assigned schools for the exact address with the district's school finder. "
                 "Citywide and criteria-based public schools (such as Central, Masterman or AMY) are separate, "
                 "application-based options - they are not assigned by address.")
SCHOOL_FINDER_URL = "https://www.phila.gov/services/education-learning/find-your-designated-neighborhood-school/"

_NAME_FIXES = {
    "Bethune, Mary Mc Leod": "Mary McLeod Bethune", "Bache / Martin": "Bache-Martin", "Kensington Campus": "Kensington High School",
    "Franklin, Benjamin ES": "Benjamin Franklin", "Franklin, Benjamin HS": "Benjamin Franklin High School",
    "Penn Treaty HS": "Penn Treaty", "South Philadelphia HS": "South Philadelphia High School", "Frankford HS": "Frankford High School",
    "Strawberry Mansion HS": "Strawberry Mansion High School", "West Philadelphia HS": "West Philadelphia High School",
    "Northeast High": "Northeast High School", "Overbrook High": "Overbrook High School", "Vare-Washington ES": "Vare-Washington",
    "Washington Jr, Grover": "Grover Washington Jr.", "Baldi, CCA": "C.C.A. Baldi", "deBurgos, Julia": "Julia de Burgos",
    "Phila Charter for Arts at HR Edmunds": "Philadelphia Charter School for Arts and Sciences at H.R. Edmunds",
    "Memphis St Charter at JP Jones": "Memphis Street Academy Charter School at J.P. Jones",
}


@lru_cache(maxsize=1)
def _zones() -> tuple[str, list[dict]]:
    try:
        data = json.loads(_FILE.read_text())
    except (OSError, ValueError):
        return "", []
    zones = []
    for z in data.get("catchments") or []:
        rings = z.get("rings") or []
        if rings:
            zones.append({**z, "_box": bs.bbox(rings)})
    return data.get("school_year") or "", zones


def display_name(raw: str, grades: str = "") -> str:
    """The district's "Furness, Horace" / "McCall, General George A" -> "Horace Furness High School" /
    "General George A. McCall School"."""
    raw = (raw or "").strip()
    name = _NAME_FIXES.get(raw)
    if not name:
        if "," in raw:
            last, first = [p.strip() for p in raw.split(",", 1)]
            first = re.sub(r"\b([A-Z])\b(?!\.)", r"\1.", first)  # "James G" -> "James G."
            name = f"{first} {last}"
        else:
            name = raw
    if re.search(r"\b(School|Academy|High|Campus)\b", name):
        return name
    lo = (grades or "").split("-")[0].strip().upper()
    if lo in ("9", "10"):
        return f"{name} High School"
    return f"{name} School"


def _plan_note(name: str) -> Optional[str]:
    key = re.sub(r"[^a-z ]", "", name.lower().replace(".", ""))
    if any(p in key for p in _FACILITIES_PLAN):
        return "Open for 2025-26; the district's approved facilities plan changes this school in a later year - check before relying on it."
    return None


def zoned_schools(points: list[tuple[float, float]]) -> Optional[dict]:
    """{"elementary_schools": [...], "middle_schools": [...], "high_schools": [...], "homes": n, "school_year": "2025-26"}
    for homes given as (lng, lat). None when too few homes fall in the district's zones."""
    year, zones = _zones()
    if not zones:
        return None
    counts: dict[str, dict] = {}
    placed = 0
    for x, y in points:
        z = next((z for z in zones if bs.contains(z["rings"], x, y, z["_box"])), None)
        if not z:
            continue
        placed += 1
        for lvl, idk, namek, gk in (("elementary_schools", "ES_ID", "ES_Name", "ES_Grade"), ("middle_schools", "MS_ID", "MS_Name", "MS_Grade"),
                                    ("high_schools", "HS_ID", "HS_Name", "HS_Grade")):
            if not z.get(idk):
                continue
            key = f"{lvl}:{z[idk]}"
            row = counts.setdefault(key, {"level": lvl, "id": z[idk], "raw": z.get(namek), "grades": z.get(gk), "n": 0})
            row["n"] += 1
    if placed < MIN_HOMES:
        return None
    out: dict = {"elementary_schools": [], "middle_schools": [], "high_schools": [], "homes": placed, "school_year": year}
    for row in sorted(counts.values(), key=lambda r: -r["n"]):
        share = round(100 * row["n"] / placed)
        if share < MIN_SHARE:
            continue
        name = display_name(row["raw"], row["grades"])
        entry = {"name": name, "district": "School District of Philadelphia", "grades": row["grades"], "share": share,
                 "sdp_id": row["id"], "assignment": "zoned", "source": f"School District of Philadelphia zones, {year}",
                 "source_url": SCHOOL_FINDER_URL}
        bits = [f"Grades {row['grades']}" if row.get("grades") else None,
                "zoned for nearly all homes" if share >= 95 else f"zoned for about {share}% of homes"]
        note = _plan_note(name)
        entry["note"] = " · ".join(b for b in bits if b)
        if note:
            entry["plan_note"] = note
        out[row["level"]].append(entry)
    return out


def is_philadelphia(ps: dict, state: Optional[str]) -> bool:
    if str(state or "").upper() != "PA":
        return False
    return any("philadelphia" in str((d or {}).get("name") or "").lower() for d in (ps or {}).get("districts") or [])


def apply_zones(ps: dict, zoned: dict) -> dict:
    """The researched public-school block with its school lists replaced by the zoned ones."""
    ps = dict(ps)
    for lvl in ("elementary_schools", "middle_schools", "high_schools"):
        ps[lvl] = zoned[lvl]
    philly = next((d for d in ps.get("districts") or [] if "philadelphia" in str((d or {}).get("name", "")).lower()), None)
    if philly:  # one district, under its real name (the research wrote "Philadelphia City School District")
        ps["districts"] = [{"name": "School District of Philadelphia", "source_url": "https://www.philasd.org/",
                            **({"share": philly["share"]} if philly.get("share") is not None else {})}]
    ps["assignment_depends_on_exact_address"] = True
    ps["boundary_caveat"] = PHILLY_CAVEAT
    ps["zones"] = {"school_year": zoned["school_year"], "homes": zoned["homes"], "source": "School District of Philadelphia catchment maps",
                   "source_url": "https://opendataphilly.org/datasets/sdp-school-catchment-areas/", "plan_url": FACILITIES_PLAN_URL}
    ps.pop("crosses_district_boundary", None)
    return ps
