"""Every for-sale home's high school (owner, 11/19): Bright leaves it blank on about 70% of listings, so a
"Downingtown East only" search silently missed most homes. Worked out once per home in the background, kept in
home_schools, and read by the Showcase search as one indexed lookup - nothing runs while anyone searches.

Never a guess (owner, 11/19: "even one home being wrong is bad - better unknown and a few more matches"):
1. Bright's own answer (the listing names its high school).
2. Otherwise ONLY when three separate signals all name the same school: the home's township (98%+ of its 10+
   for-sale homes that name a school), the government school map (NCES attendance boundaries) and the nearby
   listings (the 7 closest within 3/4 mile - 5+ of them, every one the same). Live hidden test on 217k named
   listings (11/19): 66k placed, 18 wrong (about 1 in 3,700); PA 1 in 3,200, NJ 1 in 3,700, MD 1 in 5,100, DE
   none. VA/DC/WV have almost no township data, so blank homes there stay unknown. 826 Williamsburg Blvd
   (Uwchlan, goes to Downingtown WEST) was the lesson - its township and the 2015 map say East, the homes around
   it say West.
Anything else stays unknown. A high-school search (always inside a picked district) still shows unknown homes,
with a "School unknown" note - nobody misses a home because the listing left the school out.

A value that only names the district ("DOWNINGTOWN" in a district with an East and a West school) says nothing
about the school: its homes are placed like blank ones.

Load: a separate small table (the 12 GB properties table is never rewritten); evidence is read through indexes
only (on-market listings + two years of sales); the big pass runs at night; by day only just-arrived listings get
a quick look; a price change never redoes a home.

Spellings: Bright writes one school many ways ("DOWNINGTOWN HIGH SCHOOL EAST CAMPUS", "DHS EAST"). Inside one
district, a spelling whose homes sit inside one map school's lines (75%+ of 5+ homes) becomes that school, so
every spelling lands on the same key ("nces:<school id>"). Spellings the map can't place keep their own key
("txt:<STATE>:<name>").

`sure` is always true for a placed home now (Bright named it, or all three agreed); source "agreed" marks the
worked-out ones.
"""
import asyncio
import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text

logger = logging.getLogger(__name__)

FOR_SALE = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT")
NEAR_MILES = 0.75
NEAR_K = 7
NEAR_MIN = 3
NEAR_AGREE = 0.8
ALIAS_MIN = 5
ALIAS_AGREE = 0.75
ALIAS_SAMPLE = 40      # homes per spelling checked against the map
EVAL_SAMPLE = 1500
_LOCK_KEY = 74_19_1119
_NIGHT_SECONDS = 420     # per 10-minute turn, at night only

_JUNK = {"", "0", "00", "N/A", "NA", "NONE", "UNKNOWN", "NOT AVAILABLE", "NOT PROVIDED", "SEE REMARKS", "CALL SCHOOL BOARD",
         "CALL DISTRICT", "BUYER TO VERIFY", "VERIFY", "TBD", "OTHER", "CALL", "NO", "SEE SCHOOL DISTRICT"}
_JUNK_TIGHT = {j.replace(" ", "") for j in _JUNK}    # "N/A" -> "N A" -> "NA"


def norm_school(name: Optional[str]) -> Optional[str]:
    """One spelling per school name: upper case, no punctuation, "HIGH SCHOOL"/"H.S."/"SR HIGH" -> "HS"."""
    s = re.sub(r"[^A-Z0-9 ]+", " ", (name or "").upper())
    s = " ".join(s.split())
    if s in _JUNK or s.replace(" ", "") in _JUNK_TIGHT or len(s) < 2:
        return None
    s = re.sub(r"\b(SENIOR HIGH SCHOOL|SR HIGH SCHOOL|SENIOR HIGH|SR HIGH|HIGH SCHOOL|H S|HIGH SCH|HIGH)\b", "HS", s)
    s = re.sub(r"\bHS( HS)+\b", "HS", s)
    s = re.sub(r"\b(SCHOOL|SCH)$", "", s).strip()
    return s or None


def norm_district(name: Optional[str]) -> str:
    return " ".join((name or "").upper().split())


def nice_name(raw: str) -> str:
    """'DOWNINGTOWN HIGH SCHOOL EAST CAMPUS' -> 'Downingtown High School East Campus'; NCES 'HS' spelled out."""
    s = " ".join((raw or "").split())
    s = re.sub(r"\bH\.?S\.?$|\bHS\b", "High School", s, flags=re.I)
    small = {"of", "and", "the", "at", "for"}
    out = []
    for i, w in enumerate(s.split(" ")):
        lw = w.lower()
        if i and lw in small:
            out.append(lw)
        elif re.fullmatch(r"[A-Za-z]{1,3}\.?", w) and w.isupper() and lw not in {"hs", "the", "east", "west"} and len(w) <= 2:
            out.append(w)    # initials: "DC", "JR"
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def _miles(lat1, lng1, lat2, lng2) -> float:
    dy = (lat1 - lat2) * 69.0
    dx = (lng1 - lng2) * 69.0 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


class _Grid:
    """Nearby-listing lookups without a query per home: points bucketed by ~0.75 mi cells."""
    CELL = 0.011

    def __init__(self):
        self.cells: Dict[Tuple[int, int], list] = defaultdict(list)

    def add(self, pid, lat, lng, key):
        self.cells[(int(lat / self.CELL), int(lng / self.CELL))].append((pid, lat, lng, key))

    def vote(self, lat, lng, skip=None) -> Optional[str]:
        k, share, n = self.detail(lat, lng, skip)
        return k if n >= NEAR_MIN and share >= NEAR_AGREE else None

    def detail(self, lat, lng, skip=None) -> Tuple[Optional[str], float, int]:
        """(the most common school among the closest named listings, its share, how many listings)."""
        cy, cx = int(lat / self.CELL), int(lng / self.CELL)
        cand = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for pid, plat, plng, key in self.cells.get((cy + dy, cx + dx), ()):
                    if pid == skip:
                        continue
                    d = _miles(lat, lng, plat, plng)
                    if d <= NEAR_MILES:
                        cand.append((d, key))
        cand.sort(key=lambda t: t[0])
        near = [k for _, k in cand[:NEAR_K]]
        if not near:
            return None, 0.0, 0
        top, n = Counter(near).most_common(1)[0]
        return top, n / len(near), len(near)


class _MapSchools:
    """The government map's high-school shapes around one district, kept in memory for the whole pass."""

    def __init__(self, shapes: list):
        self.shapes = shapes    # (school_id, name, xmin, ymin, xmax, ymax, geometry)

    @classmethod
    async def load(cls, db, state: str, lats: List[float], lngs: List[float]) -> "_MapSchools":
        from app.services.nces_school_assignment_service import wgs84_to_web_mercator
        if not lats or not state:
            return cls([])
        x0, y0 = wgs84_to_web_mercator(min(lats), min(lngs))
        x1, y1 = wgs84_to_web_mercator(max(lats), max(lngs))
        rows = (await db.execute(text(
            "SELECT school_id, school_name, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, geometry, grade_low, grade_high, level "
            "FROM nces_attendance_boundaries WHERE state = :s AND bbox_xmin <= :x1 AND bbox_xmax >= :x0 "
            "AND bbox_ymin <= :y1 AND bbox_ymax >= :y0"), {"s": state.upper(), "x0": x0, "x1": x1, "y0": y0, "y1": y1})).all()
        shapes = []
        for r in rows:
            if not _serves_high(r.level, r.grade_low, r.grade_high):
                continue
            shapes.append((str(r.school_id), r.school_name, r.bbox_xmin, r.bbox_ymin, r.bbox_xmax, r.bbox_ymax, r.geometry))
        return cls(shapes)

    def schools_at(self, lat, lng) -> List[Tuple[str, str]]:
        from app.services.nces_school_assignment_service import point_in_polygon_geometry, wgs84_to_web_mercator
        x, y = wgs84_to_web_mercator(lat, lng)
        out = []
        for sid, name, a, b, c, d, geom in self.shapes:
            if a <= x <= c and b <= y <= d and point_in_polygon_geometry(x, y, geom):
                out.append((sid, name))
        return out


def _serves_high(level, lo, hi) -> bool:
    from app.services.nces_school_assignment_service import grade_to_number
    if str(level or "") == "3":
        return True
    a, b = grade_to_number(lo), grade_to_number(hi)
    return a is not None and b is not None and a <= 10 <= b


def _pick_map(at: List[Tuple[str, str]], allowed: Optional[set], area: Optional[Dict[str, float]] = None) -> Optional[str]:
    """The map school for one spot. A district-wide school (a magnet / STEM academy every home could choose)
    overlaps the real one - drop the schools no listing in the district names when we know them; otherwise the
    smallest shape is the neighborhood school."""
    ids = {sid for sid, _ in at}
    if allowed is not None:
        ids &= allowed
    if len(ids) == 1:
        return next(iter(ids))
    if len(ids) > 1 and allowed is None and area:
        return min(ids, key=lambda s: area.get(s, 0))
    return None


# ---- evidence: the listings that name their school ---------------------------------------------------------
# Read through indexes only (home_status + state, and the close-date index) and only the light columns - never the
# whole district history: listings on the market now + homes sold in the last two years.
LIVE = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "PENDING-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT")
SOLD_DAYS = 730
_cut_cache: Dict[str, Any] = {}


async def _sold_cut(db) -> str:
    """Two years before the newest sale we hold ('YYYY-MM-DD') - the live site's 'now', and still right on an old copy."""
    if _cut_cache.get("at") and time.monotonic() - _cut_cache["at"] < 6 * 3600:
        return _cut_cache["cut"]
    top = (await db.execute(text("SELECT max(mls_close_date) FROM properties"))).scalar()
    try:
        cut = (datetime.strptime(str(top)[:10], "%Y-%m-%d") - timedelta(days=SOLD_DAYS)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        cut = (datetime.now(timezone.utc) - timedelta(days=SOLD_DAYS)).strftime("%Y-%m-%d")
    _cut_cache.update(at=time.monotonic(), cut=cut)
    return cut


async def _evidence(db, state: str, raw_names: Optional[List[str]] = None) -> Dict[str, list]:
    """{normalized district: [rows that name a school]} for one state (or just these raw district names)."""
    from sqlalchemy import bindparam
    cut = await _sold_cut(db)
    cols = "id, latitude, longitude, high_school, school_district_name, township"
    cond = ("state = :s AND latitude IS NOT NULL AND longitude IS NOT NULL AND coalesce(btrim(high_school), '') <> '' "
            "AND is_manual IS NOT TRUE")
    if raw_names:
        cond += " AND school_district_name IN :names"
    q = text(f"SELECT {cols} FROM properties WHERE home_status IN :live AND {cond} "
             f"UNION ALL SELECT {cols} FROM properties WHERE mls_close_date >= :cut AND home_status LIKE 'CLOSED%' AND {cond}")
    q = q.bindparams(bindparam("live", expanding=True), *( [bindparam("names", expanding=True)] if raw_names else []))
    params = {"s": state, "live": LIVE, "cut": cut, **({"names": list(raw_names)} if raw_names else {})}
    out: Dict[str, list] = defaultdict(list)
    seen = set()
    for r in (await db.execute(q, params)).all():
        if r.id not in seen:
            seen.add(r.id)
            out[norm_district(r.school_district_name)].append(r)
    return out


def _needs_work_sql() -> str:
    """A for-sale home needs its school worked out: never done, Bright's school changed, the map just arrived
    (checked_at NULL), it was a quick look on arrival, or it's unknown and a week old. A price change never."""
    return ("(h.property_id IS NULL OR h.checked_at IS NULL OR h.quick "
            " OR coalesce(h.bright_hs, '') IS DISTINCT FROM coalesce(p.high_school, '') "
            " OR (h.hs_key IS NULL AND h.checked_at < now() - interval '7 days'))")


async def fill_unit(db, state: str, district: str, *, only_ids: Optional[Iterable[str]] = None,
                    write: bool = True, evaluate: bool = False, evidence: Optional[list] = None) -> Dict[str, Any]:
    """Work out the high school for the for-sale homes of one (state, district). `district` is the normalized Bright
    district name. Used by the nightly pass (evidence handed in) and on demand (chat, tests)."""
    from sqlalchemy import bindparam
    raw = [r[0] for r in (await db.execute(text(
        "SELECT DISTINCT school_district_name FROM properties WHERE home_status IN :fs AND state = :s "
        "AND upper(btrim(coalesce(school_district_name, ''))) = :d").bindparams(bindparam("fs", expanding=True)),
        {"fs": FOR_SALE, "s": state, "d": district})).all()]
    if evidence is None:
        evidence = (await _evidence(db, state, raw or None)).get(district, []) if raw else []
    tq = ("SELECT p.id, p.latitude, p.longitude, p.high_school, p.township FROM properties p WHERE p.state = :s AND p.home_status IN :fs "
          "AND p.is_manual IS NOT TRUE AND p.school_district_name IN :names")
    binds = [bindparam("fs", expanding=True), bindparam("names", expanding=True)]
    tparams: Dict[str, Any] = {"s": state, "fs": FOR_SALE, "names": raw or ["\x00"]}
    if only_ids is not None:
        tq += " AND p.id IN :ids"
        binds.append(bindparam("ids", expanding=True))
        tparams["ids"] = list(only_ids) or ["\x00"]
    targets = (await db.execute(text(tq).bindparams(*binds), tparams)).all()
    return await _fill(db, state, district, evidence, targets, write=write, evaluate=evaluate)


async def _fill(db, state, district, evidence, targets, *, write=True, evaluate=False) -> Dict[str, Any]:
    if not targets and not evaluate:
        return {"homes": 0}
    pts = [(r.latitude, r.longitude) for r in list(evidence) + [t for t in targets if t.latitude is not None]]
    maps = await _MapSchools.load(db, state, [p[0] for p in pts], [p[1] for p in pts])
    # The number-crunching (map lines, nearby votes) runs off the event loop - the site keeps answering meanwhile.
    stats, rows = await asyncio.to_thread(_compute, state, district, list(evidence), list(targets), maps, evaluate)
    if write and rows:
        await db.execute(text(
            "INSERT INTO home_schools (property_id, state, district, hs_key, hs_name, source, sure, bright_hs, bright_norm, checked_at, quick) "
            "VALUES (:p, :s, :d, :k, :n, :src, :sure, :b, :bn, :at, false) "
            "ON CONFLICT (property_id) DO UPDATE SET state = EXCLUDED.state, district = EXCLUDED.district, hs_key = EXCLUDED.hs_key, "
            "hs_name = EXCLUDED.hs_name, source = EXCLUDED.source, sure = EXCLUDED.sure, bright_hs = EXCLUDED.bright_hs, "
            "bright_norm = EXCLUDED.bright_norm, checked_at = EXCLUDED.checked_at, quick = false"), rows)
        await db.commit()
    return stats


def _fullest(spellings: Counter) -> str:
    """The name to show: the fullest of the common spellings ("Downingtown High School East Campus", not "DHS East")."""
    total = sum(spellings.values()) or 1
    common = [s for s, k in spellings.items() if k / total >= 0.15] or [spellings.most_common(1)[0][0]]
    return max(common, key=lambda s: (len(s.split()), len(s), spellings[s]))


_NAME_FILLER = {"HS", "SENIOR", "SR", "AREA", "HIGH", "SCHOOL", "CAMPUS"}
_SIDES = {"EAST", "WEST", "NORTH", "SOUTH", "CENTRAL", "NORTHEAST", "NORTHWEST", "SOUTHEAST", "SOUTHWEST"}
RARE = 8      # fewer named homes than this, unplaced by the map: a stray spelling
TWP_MIN, TWP_AGREE = 10, 0.98    # a township that agrees on its school
NEAR_ALL = 5                      # nearby listings that must ALL name the same school


def _twp(r) -> Optional[str]:
    t = " ".join(str(getattr(r, "township", None) or "").upper().split())
    return t or None

_DISTRICT_WORDS_SKIP = {"AREA", "SCHOOL", "SCHOOLS", "DISTRICT", "SD", "PUBLIC", "COUNTY", "CITY", "THE", "OF", "TOWNSHIP", "TWP",
                        "HS", "UNIFIED", "REGIONAL", "CONSOLIDATED", "CO"}


def _compute(state: str, district: str, ev: list, targets: list, maps: "_MapSchools", evaluate: bool) -> Tuple[Dict[str, Any], list]:
    map_names = {sid: name for sid, name, *_ in maps.shapes}
    # Bright spellings -> one school: where a sample of each spelling's homes sit on the map.
    by_spell: Dict[str, Counter] = defaultdict(Counter)
    n_spell: Counter = Counter()
    spell_raw: Dict[str, Counter] = defaultdict(Counter)
    home_spell: Dict[str, str] = {}
    for r in ev:
        sp = norm_school(r.high_school)
        if not sp:
            continue
        home_spell[r.id] = sp
        spell_raw[sp][" ".join(r.high_school.split())] += 1
        if maps.shapes and n_spell[sp] < ALIAS_SAMPLE:
            for sid, _n in maps.schools_at(r.latitude, r.longitude):
                by_spell[sp][sid] += 1
            n_spell[sp] += 1
    # spellings seen only on the homes being placed are judged too (a new "DOWNINGTOWN" is still just the district)
    for t in targets:
        tsp = norm_school(t.high_school)
        if tsp and tsp not in spell_raw:
            spell_raw[tsp][" ".join(str(t.high_school).split())] += 0
    # A district-wide school (a STEM / magnet academy any home may choose) holds every home too - of the schools
    # that hold most of a spelling's homes, the smallest area is the neighborhood school.
    area = {sid: (c - a) * (d - b) for sid, _n, a, b, c, d, _g in maps.shapes}
    # Shapes that hold a spelling's homes together with a smaller shape are district-wide (STEM / magnet) - never a
    # neighborhood school, so a spelling whose homes they hold says nothing by that alone.
    wide: set = set()
    for sp, c in by_spell.items():
        n = n_spell[sp]
        good = [sid for sid, k in c.items() if n and k / n >= ALIAS_AGREE]
        if len(good) >= 2:
            small = min(good, key=lambda s: area.get(s, 0))
            wide |= {s for s in good if s != small}
    alias: Dict[str, str] = {}
    spread: set = set()
    dwords = set(re.findall(r"[A-Z0-9]+", district)) - _DISTRICT_WORDS_SKIP
    zones = {sid for sid, *_ in maps.shapes if sid not in wide}
    for sp, c in by_spell.items():
        n = n_spell[sp]
        c = Counter({sid: k for sid, k in c.items() if sid not in wide})
        if n >= ALIAS_MIN and c:
            good = [sid for sid, k in c.items() if k / n >= ALIAS_AGREE]
            only_district = not (set(sp.split()) - _NAME_FILLER - dwords)
            if only_district and len(zones) >= 2 and max(c.values()) / n < 0.95:
                # "DOWNINGTOWN" with 78% of its homes on the West side is still the district, not West (11/19 live data)
                spread.add(sp)
            elif good:
                alias[sp] = f"nces:{min(good, key=lambda s: area.get(s, 0))}"
            else:
                shares = sorted((k / n for k in c.values()), reverse=True)
                if len(shares) >= 2 and shares[1] >= 0.2:
                    spread.add(sp)

    # Which spellings name a school, which only name the district, which are another spelling of a school (live data
    # 11/19: "DOWNINGTOWN" is the district - homes on both sides; "LOWER MERION" 66/34 across two schools; but
    # "READING" / "NORRISTOWN" are their district's one school, spelled short; "RIVER HILL" is a real school even
    # where the 2015 map's lines are out of date).
    words = {sp: set(sp.split()) - _NAME_FILLER for sp in spell_raw}
    count = {sp: sum(v.values()) for sp, v in spell_raw.items()}
    vague: set = set()
    merged: Dict[str, str] = {}
    # the same words but for "High School" / "Senior" / "Area" ("READING" = "READING SENIOR"): one school
    for sp in spell_raw:
        if sp in alias:
            continue
        twin = max((o for o in spell_raw if o != sp and words[o] == words[sp] and words[sp]),
                   key=lambda o: (count[o], o), default=None)
        if twin and (count[twin], twin) > (count[sp], sp):
            merged[sp] = twin
    for sp in spell_raw:
        if sp in alias or not words[sp] or sp in merged:
            continue
        generic = not (words[sp] - dwords)          # nothing but the district's own name
        supers = [o for o in spell_raw if o != sp and words[sp] < words[o]]
        tops = [o for o in supers if not any(words[o] < words[x] for x in supers)]
        if len(tops) >= 2 and len({alias.get(o) or o for o in tops}) >= 2:
            vague.add(sp)                           # "DOWNINGTOWN" inside "... EAST CAMPUS" and "... WEST CAMPUS"
        elif len(tops) == 1 and count[sp] <= count[tops[0]]:
            merged[sp] = tops[0]                    # "READING" = "READING SENIOR" - the one school, spelled short
        elif generic and sp in spread:
            vague.add(sp)                           # "LOWER MERION": the district's name, homes in two schools
    # typos of a more common spelling ("BATRAM" / "BARTRAM", "PENNSBURYE", "NORTHEAST" / "NORTH EAST")
    from difflib import SequenceMatcher
    common = sorted(spell_raw, key=lambda x: -count[x])
    for sp in spell_raw:
        if sp in alias or sp in vague or sp in merged:
            continue
        flat = sp.replace(" ", "")
        for o in common:
            if o == sp or count[o] < count[sp] or o in vague:
                continue
            if (_SIDES & words[sp]) == (_SIDES & words[o]) and len(flat) >= 5 and \
                    SequenceMatcher(None, flat, o.replace(" ", "")).ratio() >= 0.88:
                merged[sp] = o
                break
    # a rare stray spelling the map couldn't place, in a district with other schools, says little - placed like a blank
    others = {alias.get(x) or x for x in spell_raw if x not in vague and x not in merged}
    for sp in spell_raw:
        if sp not in alias and sp not in merged and sp not in vague and count[sp] < RARE and len(others - {sp}) >= 2 and maps.shapes:
            vague.add(sp)

    def _root(sp: str) -> str:
        seen = set()
        while sp in merged and sp not in seen:
            seen.add(sp)
            sp = merged[sp]
        return sp

    for rid in [rid for rid, sp in home_spell.items() if sp in vague]:
        del home_spell[rid]

    def key_of_spell(sp: str) -> str:
        sp = _root(sp)
        return alias.get(sp) or f"txt:{state}:{sp}"

    names: Dict[str, Counter] = defaultdict(Counter)
    for sp, raws in spell_raw.items():
        if sp in vague:
            continue
        for raw, k in raws.items():
            names[key_of_spell(sp)][raw] += k
    allowed = {k[5:] for k in alias.values()} if alias else None

    grid = _Grid()
    twp_votes: Dict[str, Counter] = defaultdict(Counter)
    for r in ev:
        sp = home_spell.get(r.id)
        if sp:
            grid.add(r.id, r.latitude, r.longitude, key_of_spell(sp))
            t = _twp(r)
            if t:
                twp_votes[t][key_of_spell(sp)] += 1
    # A township whose named listings all but agree (90%+ of 10+) - school lines mostly follow township lines, so a
    # home there goes to that school even when it sits at the edge and the listings across the line say otherwise.
    twp_school: Dict[str, str] = {}
    for t, c in twp_votes.items():
        k, n = c.most_common(1)[0]
        if sum(c.values()) >= TWP_MIN and n / sum(c.values()) >= TWP_AGREE:
            twp_school[t] = k

    def map_key(lat, lng) -> Optional[str]:
        if not maps.shapes:
            return None
        sid = _pick_map(maps.schools_at(lat, lng), allowed, area)
        return f"nces:{sid}" if sid else None

    def blank_answer(lat, lng, skip=None, twp=None) -> Tuple[Optional[str], Optional[str], bool]:
        """Only when three independent signals name the same school - otherwise unknown (shown, marked)."""
        t = twp_school.get(twp) if twp else None
        if not t:
            return None, None, False
        m = map_key(lat, lng)
        nk, share, n = grid.detail(lat, lng, skip=skip)
        if m and nk and t == m == nk and share >= 1.0 and n >= NEAR_ALL:
            return t, "agreed", True
        return None, None, False

    stats: Dict[str, Any] = {"homes": len(targets), "bright": 0, "agreed": 0, "unknown": 0, "sure": 0,
                             "vague": sorted(vague), "merged": dict(merged)}
    if evaluate:
        # Hide a sample of named homes' own answers and see what the other sources would have said.
        ok = Counter()
        triples = []
        for r in [r for r in ev if r.id in home_spell][::max(1, len(home_spell) // EVAL_SAMPLE)]:
            truth = key_of_spell(home_spell[r.id])
            m, near = map_key(r.latitude, r.longitude), grid.vote(r.latitude, r.longitude, skip=r.id)
            k, src, sure = blank_answer(r.latitude, r.longitude, skip=r.id, twp=_twp(r))
            ok[(src or "none", "right" if k == truth else ("wrong" if k else "none"), sure)] += 1
            if m and near and m != near:
                ok[("disagree", "map right" if m == truth else ("near right" if near == truth else "neither"), False)] += 1
            if m:
                ok[("map alone", "right" if m == truth else "wrong", False)] += 1
            if near:
                ok[("near alone", "right" if near == truth else "wrong", False)] += 1
            # the township's answer without this home's own vote
            t_key, t_share, t_n = None, 0.0, 0
            tw = _twp(r)
            if tw and tw in twp_votes:
                c = Counter(twp_votes[tw])
                c[truth] -= 1
                c = +c
                if c:
                    t_key, top = c.most_common(1)[0]
                    t_n = sum(c.values())
                    t_share = top / t_n
            nk, nsh, nn = grid.detail(r.latitude, r.longitude, skip=r.id)
            triples.append((t_key, round(t_share, 3), t_n, m, near, truth, nk, round(nsh, 3), nn))
        stats["evaluate"] = ok
        stats["triples"] = triples

    now = datetime.now(timezone.utc)
    rows = []
    for t in targets:
        sp = norm_school(t.high_school)
        if sp and sp not in vague:
            k, src, sure = key_of_spell(sp), "bright", True
        elif t.latitude is not None and t.longitude is not None:
            k, src, sure = blank_answer(t.latitude, t.longitude, twp=_twp(t))
        else:
            k, src, sure = None, None, False
        stats[src or "unknown"] += 1
        stats["sure"] += 1 if (k and sure) else 0
        name = None
        if k:
            if names.get(k):
                name = nice_name(_fullest(names[k]))
            elif k.startswith("nces:") and map_names.get(k[5:]):
                name = nice_name(map_names[k[5:]])
        rows.append({"p": t.id, "s": state, "d": district, "k": k, "n": name, "src": src, "sure": bool(k and sure),
                     "b": t.high_school, "bn": sp, "at": now})
    # one name, two keys (a school the map lists twice, or a spelling it didn't place): one school - the bigger one
    by_name: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["k"] and r["n"]:
            by_name[r["n"].lower()][r["k"]] += 1
    same = {k: c.most_common(1)[0][0] for c in by_name.values() if len(c) > 1 for k in c}
    for r in rows:
        if r["k"] in same:
            r["k"] = same[r["k"]]
    return stats, rows


def _quiet_hours() -> bool:
    from app.services import mls_mirror
    return mls_mirror._quiet_hours()


def _night() -> bool:
    """05:00-10:30 UTC (midnight-5:30 AM Eastern in winter, 1-6:30 AM in summer): the big pass runs only then,
    after the nightly backup and before agents start their day."""
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    return 5 * 60 <= m < 10 * 60 + 30


async def fill_night(run_seconds: Optional[float] = None) -> Dict[str, Any]:
    """The nightly pass: state by state, each state's evidence read once (two indexed reads), then district by
    district until the time is up. One worker at a time (advisory lock)."""
    from sqlalchemy import bindparam
    from app.database import AsyncSessionLocal, engine
    done = {"districts": 0, "homes": 0}
    async with engine.connect() as raw_lock:
        lockdb = await raw_lock.execution_options(isolation_level="AUTOCOMMIT")
        if not (await lockdb.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar():
            return {**done, "skipped": "another worker"}
        try:
            if not (await lockdb.execute(text("SELECT to_regclass('home_schools')"))).scalar():
                return {**done, "skipped": "migration not applied"}
            deadline = time.monotonic() + (run_seconds if run_seconds is not None else _NIGHT_SECONDS)
            async with AsyncSessionLocal() as db:
                todo = (await db.execute(text(
                    "SELECT p.id, p.latitude, p.longitude, p.high_school, p.township, p.state, upper(btrim(coalesce(p.school_district_name, ''))) AS d "
                    "FROM properties p LEFT JOIN home_schools h ON h.property_id = p.id "
                    f"WHERE p.home_status IN :fs AND p.is_manual IS NOT TRUE AND p.state IS NOT NULL AND {_needs_work_sql()}"
                ).bindparams(bindparam("fs", expanding=True)), {"fs": FOR_SALE})).all()
            by_state: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
            for t in todo:
                by_state[t.state][t.d].append(t)
            for state, districts in sorted(by_state.items(), key=lambda kv: -sum(len(v) for v in kv[1].values())):
                if time.monotonic() > deadline:
                    break
                async with AsyncSessionLocal() as db:
                    ev = await _evidence(db, state)
                for d, targets in sorted(districts.items(), key=lambda kv: -len(kv[1])):
                    if time.monotonic() > deadline:
                        break
                    async with AsyncSessionLocal() as db:
                        st = await _fill(db, state, d, ev.get(d, []) if d else [], targets)
                    done["districts"] += 1
                    done["homes"] += st.get("homes", 0)
        finally:
            await lockdb.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    return done


async def fill_new(limit: int = 150) -> Dict[str, Any]:
    """Daytime: only listings that just arrived (or whose listing just started naming its school) - found through
    the modification-time index, a quick look each. Everything else waits for the night."""
    from sqlalchemy import bindparam
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        if not (await db.execute(text("SELECT to_regclass('home_schools')"))).scalar():
            return {"homes": 0}
        ids = [r[0] for r in (await db.execute(text(
            "SELECT p.id FROM properties p LEFT JOIN home_schools h ON h.property_id = p.id "
            "WHERE p.modification_timestamp > now() - interval '6 hours' AND p.home_status IN :fs AND p.is_manual IS NOT TRUE "
            "AND (h.property_id IS NULL OR coalesce(h.bright_hs, '') IS DISTINCT FROM coalesce(p.high_school, '')) LIMIT :n"
        ).bindparams(bindparam("fs", expanding=True)), {"fs": FOR_SALE, "n": limit})).all()]
        for pid in ids:
            await _quick_key(db, pid)
            await db.commit()
    return {"homes": len(ids)}


SABS_URL = "https://nces.ed.gov/programs/edge/data/SABS_1516.zip"   # the official NCES file, public, free (584 MB)
SABS_BYTES = 583873446
_IMPORT_LOCK = 74_19_1120
_STATE_KEY = "nces_map_import"


async def _import_state(db) -> Dict[str, Any]:
    v = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": _STATE_KEY})).scalar()
    return v if isinstance(v, dict) else {}


async def _set_import_state(db, st: Dict[str, Any]) -> None:
    import json
    await db.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, cast(:v as jsonb)) "
                          "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"), {"k": _STATE_KEY, "v": json.dumps(st)})
    await db.commit()


async def map_settled(db) -> bool:
    """The school map is loaded, or loading it was turned off / failed today - the fill doesn't wait for it then."""
    if os.getenv("OHP_NCES_IMPORT", "on") == "off":
        return True
    if (await db.execute(text("SELECT 1 FROM nces_attendance_boundaries LIMIT 1"))).first():
        return True
    st = await _import_state(db)
    failed = st.get("failed_at")
    return bool(failed and datetime.fromisoformat(failed) > datetime.now(timezone.utc) - timedelta(days=1))


async def scheduled_map_import():
    """Once: the government school map (NCES attendance boundaries) for the states we have listings in. It was only
    ever loaded on dev machines - without it, homes whose listing leaves the school out lean on nearby listings
    alone. Downloads the official file to a temp folder, loads it in a separate process (the site keeps answering),
    deletes the file. A failure waits a day before trying again."""
    import shutil
    import subprocess
    import sys
    import tempfile
    from app.database import AsyncSessionLocal, engine
    if os.getenv("OHP_NCES_IMPORT", "on") == "off" or not _night():
        return
    async with engine.connect() as raw_lock:
        lockdb = await raw_lock.execution_options(isolation_level="AUTOCOMMIT")
        if not (await lockdb.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _IMPORT_LOCK})).scalar():
            return
        zpath = None
        try:
            async with AsyncSessionLocal() as db:
                if await map_settled(db):
                    return
                states = sorted(r[0] for r in (await db.execute(text(
                    "SELECT DISTINCT state FROM properties WHERE home_status IN ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT') "
                    "AND state IS NOT NULL AND length(state) = 2"))).all())
            tmp = tempfile.gettempdir()
            if not states or shutil.disk_usage(tmp).free < 3 * 1024 ** 3:
                raise RuntimeError(f"no states ({states}) or under 3 GB free in {tmp}")
            zpath = os.path.join(tmp, "SABS_1516.zip")
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=300), follow_redirects=True,
                                         headers={"User-Agent": "OpenHousePal/1.0 (school map)"}) as client:
                async with client.stream("GET", SABS_URL) as r:
                    r.raise_for_status()
                    with open(zpath + ".part", "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            f.write(chunk)
            got = os.path.getsize(zpath + ".part")
            if got != SABS_BYTES:
                raise RuntimeError(f"school map file is {got} bytes, expected {SABS_BYTES}")
            os.replace(zpath + ".part", zpath)
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            proc = await asyncio.create_subprocess_exec(
                sys.executable, os.path.join(root, "scripts", "import_nces_boundaries.py"), zpath, "--states", ",".join(states),
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _out, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"import failed: {(err or b'')[-500:].decode(errors='replace')}")
            async with AsyncSessionLocal() as db:
                n = (await db.execute(text("SELECT count(*) FROM nces_attendance_boundaries"))).scalar()
                await _set_import_state(db, {"done_at": datetime.now(timezone.utc).isoformat(), "shapes": n, "states": states})
                # homes worked out before the map was there: redo them with it
                await db.execute(text("UPDATE home_schools SET checked_at = NULL WHERE source IS DISTINCT FROM 'bright'"))
                await db.commit()
            logger.info("school map loaded", extra={"shapes": n, "states": states})
        except Exception as e:
            logger.warning("school map import failed", exc_info=True)
            try:
                async with AsyncSessionLocal() as db:
                    await _set_import_state(db, {"failed_at": datetime.now(timezone.utc).isoformat(), "error": str(e)[:500]})
            except Exception:
                pass
        finally:
            for pth in (zpath, (zpath + ".part") if zpath else None):
                try:
                    if pth and os.path.exists(pth):
                        os.remove(pth)
                except OSError:
                    pass
            await lockdb.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _IMPORT_LOCK})


async def scheduled_fill():
    """Every 10 minutes (off during the nightly backup). Night: the big pass, several minutes a turn. Day: only the
    listings that just arrived, a quick look each - the 5-minute Bright sync itself never waits on this."""
    if os.getenv("OHP_HOME_SCHOOLS", "on") == "off" or _quiet_hours():
        return
    try:
        if _night():
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                if not await map_settled(db):
                    return   # the map loads first tonight - homes aren't all worked out twice
            st = await fill_night()
        else:
            st = await fill_new()
        if st.get("homes"):
            logger.info("home schools filled", extra={"stats": st})
    except Exception:
        logger.warning("home schools fill failed", exc_info=True)


async def anyone_searching_schools(db) -> bool:
    """Is any live Showcase limited to high schools? (Then a new listing's school is worked out on arrival.)"""
    try:
        return bool((await db.execute(text(
            "SELECT 1 FROM collection_preferences cp JOIN collections c ON c.id = cp.collection_id WHERE c.status = 'ACTIVE' "
            "AND jsonb_typeof(cp.high_schools) = 'array' AND jsonb_array_length(cp.high_schools) > 0 LIMIT 1"))).first())
    except Exception:
        await db.rollback()
        return False


async def quick_key(property_id: str) -> Optional[str]:
    """A listing's high school for the new-listing match. Already worked out (and Bright still says the same) =
    that answer, nothing redone - a price change never repeats it. Otherwise a quick look (`_quick_key`). Its own
    session, so a failure here never touches the sync's work."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        have = (await db.execute(text(
            "SELECT h.hs_key FROM home_schools h JOIN properties p ON p.id = h.property_id WHERE h.property_id = :p "
            "AND coalesce(h.bright_hs, '') = coalesce(p.high_school, '') AND h.checked_at IS NOT NULL"), {"p": property_id})).first()
        if have is not None:
            return have.hs_key
        k = await _quick_key(db, property_id)
        await db.commit()
        return k


async def _quick_key(db, property_id: str) -> Optional[str]:
    """One home, a few small indexed reads: what its district's spellings mean (learned by the nightly pass), the
    map at its spot, and the listings within 3/4 mile that name a school (map-point index). Marked quick - the next
    night redoes it with the whole district in view."""
    from sqlalchemy import bindparam
    try:
        p = (await db.execute(text(
            "SELECT id, state, upper(btrim(coalesce(school_district_name, ''))) AS d, latitude AS lat, longitude AS lng, high_school, township "
            "FROM properties WHERE id = :p"), {"p": property_id})).first()
        if not p or not p.state:
            return None
        sp = norm_school(p.high_school)
        near_rows = []
        if p.lat is not None:
            box = NEAR_MILES / 69.0
            near_rows = (await db.execute(text(
                "SELECT x.id, x.latitude, x.longitude, x.high_school FROM properties x "
                "WHERE point(x.longitude, x.latitude) <@ box(point(:c, :a), point(:e, :b)) "
                "AND x.latitude BETWEEN :a AND :b AND x.longitude BETWEEN :c AND :e AND x.state = :s "
                "AND upper(btrim(coalesce(x.school_district_name, ''))) = :d AND coalesce(btrim(x.high_school), '') <> '' LIMIT 400"),
                {"s": p.state, "d": p.d, "a": p.lat - box, "b": p.lat + box, "c": p.lng - box * 1.4, "e": p.lng + box * 1.4})).all()
        spells = {s for s in [sp, *(norm_school(r.high_school) for r in near_rows)] if s}
        # what the nightly pass decided each spelling means: a school (source bright) or nothing (vague -> map / nearby)
        learned: Dict[str, Optional[str]] = {}
        names: Dict[str, str] = {}
        if spells:
            for r in (await db.execute(text(
                    "SELECT bright_norm, source, hs_key, hs_name FROM home_schools WHERE state = :s AND district = :d AND bright_norm IN :sp "
                    "AND NOT quick").bindparams(bindparam("sp", expanding=True)), {"s": p.state, "d": p.d, "sp": list(spells)})).all():
                if r.source == "bright" and r.hs_key:
                    learned[r.bright_norm] = r.hs_key
                    names.setdefault(r.hs_key, r.hs_name)
                else:
                    learned.setdefault(r.bright_norm, None)
        allowed = {k[5:] for k in (await db.execute(text(
            "SELECT DISTINCT hs_key FROM home_schools WHERE state = :s AND district = :d AND source = 'bright' AND hs_key LIKE 'nces:%' "
            "AND NOT quick"), {"s": p.state, "d": p.d})).scalars().all()} or None

        def key_of(s: str) -> Optional[str]:
            if s in learned:
                return learned[s]          # None = a vague spelling (the district's name)
            dwords = set(re.findall(r"[A-Z0-9]+", p.d)) - _DISTRICT_WORDS_SKIP
            if allowed and len(allowed) >= 2 and set(s.split()) - {"HS"} <= dwords:
                return None
            return f"txt:{p.state}:{s}"

        maps = await _MapSchools.load(db, p.state, [p.lat], [p.lng]) if p.lat is not None else _MapSchools([])
        area = {sid: (c - a) * (d - b) for sid, _n, a, b, c, d, _g in maps.shapes}
        map_sid = _pick_map(maps.schools_at(p.lat, p.lng), allowed, area) if maps.shapes else None
        map_key = f"nces:{map_sid}" if map_sid else None
        own = key_of(sp) if sp else None
        # the home's own township, as the nightly pass counted it (for-sale homes there that name their school)
        twp_key = None
        if not own and _twp(p):
            votes = (await db.execute(text(
                "SELECT h.hs_key, count(*) n FROM home_schools h JOIN properties x ON x.id = h.property_id WHERE x.township = :t "
                "AND h.state = :s AND h.district = :d AND h.source = 'bright' AND h.hs_key IS NOT NULL AND NOT h.quick GROUP BY 1 ORDER BY 2 DESC"),
                {"t": p.township, "s": p.state, "d": p.d})).all()
            total = sum(v.n for v in votes)
            if votes and total >= TWP_MIN and votes[0].n / total >= TWP_AGREE:
                twp_key = votes[0].hs_key
        if own:
            k, src, sure = own, "bright", True
        elif twp_key and p.lat is not None:
            g = _Grid()
            for r in near_rows:
                nkey = key_of(norm_school(r.high_school) or "")
                if nkey:
                    g.add(r.id, r.latitude, r.longitude, nkey)
            nk, share, n = g.detail(p.lat, p.lng, skip=p.id)
            if map_key and nk and twp_key == map_key == nk and share >= 1.0 and n >= NEAR_ALL:
                k, src, sure = twp_key, "agreed", True
            else:
                k, src, sure = None, None, False
        else:
            k, src, sure = None, None, False
        name = names.get(k) if k else None
        if k and not name:
            name = (await db.execute(text("SELECT hs_name FROM home_schools WHERE hs_key = :k AND hs_name IS NOT NULL LIMIT 1"),
                                     {"k": k})).scalar()
        if k and not name and k.startswith("nces:"):
            name = next((nice_name(n) for sid, n, *_ in maps.shapes if f"nces:{sid}" == k), None)
        if k and not name and own:
            name = nice_name(p.high_school)
        await db.execute(text(
            "INSERT INTO home_schools (property_id, state, district, hs_key, hs_name, source, sure, bright_hs, bright_norm, checked_at, quick) "
            "VALUES (:p, :s, :d, :k, :n, :src, :sure, :b, :bn, now(), true) ON CONFLICT (property_id) DO UPDATE SET "
            "hs_key = EXCLUDED.hs_key, hs_name = EXCLUDED.hs_name, source = EXCLUDED.source, sure = EXCLUDED.sure, "
            "bright_hs = EXCLUDED.bright_hs, bright_norm = EXCLUDED.bright_norm, checked_at = EXCLUDED.checked_at, quick = true"),
            {"p": p.id, "s": p.state, "d": p.d, "k": k, "n": name, "src": src, "sure": bool(k and sure), "b": p.high_school, "bn": sp})
        return k
    except Exception:
        logger.warning("home schools: new listing lookup failed", exc_info=True)
        await db.rollback()
        return None


# ---- reading -----------------------------------------------------------------------------------------------

def _picked(preferences) -> List[str]:
    """The high schools a search keeps - only together with a school district (owner, 11/19): a school alone would
    let every 'school unknown' home in, from any town."""
    keys = [k for k in (getattr(preferences, "high_schools", None) or []) if k]
    return keys if keys and (getattr(preferences, "school_districts", None) or []) else []


def search_filter(preferences):
    """Showcase search: homes going to one of these high schools, plus the homes in the picked district(s) whose
    school we can't tell (owner, 11/19: "I don't want people to miss out on homes because we don't have the school")
    - the card says "School unknown". Two indexed lookups on home_schools."""
    from sqlalchemy import exists, or_, select
    from app.models.database import HomeSchool, Property
    keys = _picked(preferences)
    if not keys:
        return None
    known = exists().where(HomeSchool.property_id == Property.id, HomeSchool.hs_key.isnot(None))
    return or_(Property.id.in_(select(HomeSchool.property_id).where(HomeSchool.hs_key.in_(keys))), ~known)


async def unknown_school_ids(db, preferences, property_ids) -> set:
    """Of these homes, the ones in a high-school search only because their school is unknown (the card's note)."""
    from sqlalchemy import bindparam
    ids = [i for i in (property_ids or []) if i]
    if not ids or not _picked(preferences):
        return set()
    known = {r[0] for r in (await db.execute(text(
        "SELECT property_id FROM home_schools WHERE property_id IN :ids AND hs_key IS NOT NULL").bindparams(
        bindparam("ids", expanding=True)), {"ids": ids})).all()}
    return set(ids) - known


async def school_marks(db, preferences, property_ids) -> Tuple[set, Dict[str, str]]:
    """A high-school search's card marks (owner, 11/20): the homes whose school is unknown (amber note, district pill)
    and {home: picked school's name} for the homes going to a picked school (the photo's pill shows it in gold)."""
    from sqlalchemy import bindparam
    ids = [i for i in (property_ids or []) if i]
    picked = _picked(preferences)
    if not ids or not picked:
        return set(), {}
    keyed = {r[0]: r[1] for r in (await db.execute(text(
        "SELECT property_id, hs_key FROM home_schools WHERE property_id IN :ids AND hs_key IS NOT NULL").bindparams(
        bindparam("ids", expanding=True)), {"ids": ids})).all()}
    names = await names_for(db, picked)
    return set(ids) - set(keyed), {p: names[k] for p, k in keyed.items() if k in picked and names.get(k)}


async def schools_in(db, districts: List[str]) -> List[Dict[str, Any]]:
    """The high schools for sale homes in these districts ("Downingtown Area, PA" style names, matched like the
    search does): key, name, homes for sale, and how many of those the listing itself names. "unknown" = homes
    there whose school can't be told (they stay in a school search, with a note)."""
    out: Dict[str, Dict[str, Any]] = {}
    for d in districts or []:
        parts = [s.strip() for s in str(d).split(",")]
        name, st = parts[0], (parts[1].upper() if len(parts) > 1 else None)
        q = ("SELECT h.hs_key, h.hs_name, count(*) AS n, count(*) FILTER (WHERE h.source = 'bright') AS named, "
             "count(*) FILTER (WHERE h.sure) AS sure FROM properties p LEFT JOIN home_schools h ON h.property_id = p.id "
             "WHERE p.home_status IN ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT') AND p.is_manual IS NOT TRUE "
             "AND p.school_district_name ILIKE :d" + (" AND p.state = :s" if st else "") + " GROUP BY 1, 2")
        for r in (await db.execute(text(q), {"d": f"%{name}%", "s": st})).all():
            key = r.hs_key or "unknown"
            o = out.setdefault(key, {"key": key, "name": r.hs_name or "School unknown", "homes": 0, "named": 0, "sure": 0, "_names": Counter()})
            o["homes"] += r.n
            o["named"] += r.named
            o["sure"] += r.sure
            if r.hs_name:
                o["_names"][r.hs_name] += r.n
    res = []
    for o in out.values():
        if o["_names"]:
            o["name"] = o["_names"].most_common(1)[0][0]
        o.pop("_names", None)
        res.append(o)
    unknown = [o for o in res if o["key"] == "unknown"]
    return sorted([o for o in res if o["key"] != "unknown"], key=lambda o: -o["homes"]) + unknown


_PICK_FILLER = re.compile(r"\b(high|school|schools|hs|senior|sr|campus|area|sd|district|the|in|only|go|goes|going|to|for|of)\b", re.I)


def match_school_name(opts: List[Dict[str, Any]], raw: str) -> Optional[Dict[str, Any]]:
    """Words someone typed ("Downingtown East schools") -> one of the district's high schools (schools_in rows).
    None unless exactly one school fits - a side word (East/West) must be in the school's name."""
    def words(s: str) -> set:
        return {w for w in re.findall(r"[a-z0-9]+", _PICK_FILLER.sub(" ", (s or "").lower())) if len(w) > 1}
    want = words(raw)
    side = {w for w in want if w.upper() in _SIDES}
    scored = []
    for o in opts:
        if o.get("key") in (None, "unknown"):
            continue
        have = words(o.get("name") or "")
        if side and not side <= have:
            continue
        n = len(want & have)
        if n:
            scored.append((n, o))
    if not scored:
        return None
    best = max(n for n, _ in scored)
    top = [o for n, o in scored if n == best]
    return top[0] if len(top) == 1 else None


async def names_for(db, keys: List[str]) -> Dict[str, str]:
    keys = [k for k in (keys or []) if k]
    if not keys:
        return {}
    from sqlalchemy import bindparam
    rows = (await db.execute(text(
        "SELECT hs_key, hs_name, count(*) FROM home_schools WHERE hs_key IN :k AND hs_name IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC"
    ).bindparams(bindparam("k", expanding=True)), {"k": keys})).all()
    out: Dict[str, str] = {}
    for r in rows:
        out.setdefault(r.hs_key, r.hs_name)
    return out
