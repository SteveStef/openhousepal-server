"""Shared town knowledge - one evidence ledger per real-world town, reused by every product.

The expensive part of writing anything local (an article, a Spotlight neighborhood section, an IDX
community page) is research. MarketArea already holds a deep, sourced research pass per town -
researched ONCE platform-wide by area_enrichment_service when any agent adds the town to their IDX
site (schools/districts, transit, parks, dining, downtown, medical, market context, Census, photos).
This module turns that into a flat list of citable evidence items:

    {"id", "town", "topic", "claim", "source_name", "source_url", "as_of", "confidence", "origin"}

plus two live layers that are never stored stale:
  - a dated Bright MLS sales snapshot (market_stats_service.get_area_sales_snapshot), and
  - the town's EVIDENCE BANK (MarketArea.facts["evidence_bank"]): extra facts later research
    found (e.g. the blog pipeline's gap research - property-tax millage, a new station, a
    district boundary note). Each banked item expires by topic (market facts fast, civic facts
    slowly), so a later article - or Spotlight, or the IDX page - reuses it for free instead of
    paying to research the same thing again.

Rules carried through from the research layer: no subjective school language (only names /
districts / "verify with the district"), no demographics that describe WHO lives somewhere (only
population, households, homeownership rate), and every item keeps its source + date.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.database import MarketArea

logger = logging.getLogger(__name__)

# How long a banked fact stays usable, by topic. Market facts move monthly; civic facts rarely.
_BANK_TTL_DAYS = {"market": 45, "rates": 10, "taxes": 365, "schools": 365, "transit": 365, "development": 120}
_BANK_TTL_DEFAULT = 270
_BANK_MAX_ITEMS = 120

_SUBJECTIVE = re.compile(r"\b(best|top|top-rated|top-ranked|ranked|highly|regarded|acclaimed|excellent|prestigious|desirable|sought-after|award-winning|renowned|outstanding|premier|elite|famil(y|ies)|kids|children|safe|safest|crime)\b", re.I)

# facts[category] -> evidence topic
_PLACE_TOPICS = {
    "transportation": "transit", "parks": "parks", "restaurants": "dining", "shopping": "shopping",
    "downtown_centers": "downtown", "grocery": "everyday", "medical": "medical", "private_schools": "schools",
}

# Everything an article about a town can lean on - what the gap check looks for.
COVERAGE_TOPICS = ("market", "schools", "transit", "downtown", "parks", "dining", "taxes")


def _iid(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:10]


def _date(value) -> Optional[str]:
    if not value:
        return None
    s = str(value)
    return s[:10] if len(s) >= 10 and s[4] == "-" else s


def _domain(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").replace("www.", "")
    except Exception:
        return ""


def _town(area: MarketArea) -> str:
    return f"{area.name}, {area.state}" if area.state else area.name


def research_items(area: MarketArea) -> list[dict]:
    """The stored IDX research for this town as evidence items (free - already paid for)."""
    facts = area.facts or {}
    town = _town(area)
    out: list[dict] = []

    ps = facts.get("public_schools") or {}
    ps_date = _date((ps.get("_meta") or {}).get("researched_at"))
    for d in ps.get("districts") or []:
        if d.get("name"):
            out.append({"topic": "schools", "claim": f"Parts of {area.name} are served by {d['name']} (attendance depends on the specific address).",
                        "source_url": d.get("source_url"), "as_of": ps_date, "confidence": d.get("confidence")})
    for level in ("high_schools", "middle_schools"):
        for s in (ps.get(level) or [])[:6]:
            if s.get("name"):
                out.append({"topic": "schools", "claim": f"{s['name']} is a public {level.split('_')[0]} school in {s.get('district') or 'the area'}.",
                            "source_url": s.get("source_url"), "as_of": ps_date, "confidence": s.get("confidence")})

    for cat, topic in _PLACE_TOPICS.items():
        block = facts.get(cat) or {}
        when = _date((block.get("_meta") or {}).get("researched_at"))
        for e in (block.get("entities") or [])[:8]:
            if not e.get("name"):
                continue
            where = f"in {e.get('city') or area.name}" if e.get("relationship") == "IN_AREA" else (
                f"about {e['distance_miles']} mi away in {e.get('city')}" if e.get("distance_miles") and e.get("city") else "nearby")
            desc = (e.get("description") or "").strip().rstrip(".")
            # Schools get name + place only (no descriptions - they carry rankings / "great for
            # families" language); any other description with subjective or family-status wording is dropped.
            if topic == "schools" or _SUBJECTIVE.search(desc):
                desc = ""
            label = "private school" if cat == "private_schools" else (e.get("category") or topic)
            claim = f"{e['name']} ({label}) is {where}" + (f" - {desc}." if desc else ".")
            out.append({"topic": topic, "claim": claim, "source_url": e.get("source_url"), "as_of": when, "confidence": e.get("confidence")})

    mc = facts.get("market_context") or {}
    for st in (mc.get("stats") or [])[:10]:
        if st.get("label") and st.get("value"):
            out.append({"topic": "market", "claim": f"{st['label']} ({st.get('geography') or town}): {st['value']} - third-party estimate from {st.get('source_type') or 'a market data site'}.",
                        "source_url": st.get("source_url"), "as_of": st.get("time_period") or _date((mc.get("_meta") or {}).get("researched_at")),
                        "confidence": st.get("confidence")})

    # The current enrichment shape (10/2026): Google Places `items` lists, OpenStreetMap walk
    # reach, Google Maps commute times and FEMA flood zones. research_items only read the older
    # AI-research shape (`entities`), so towns enriched this way reached articles with nothing but
    # their MLS numbers - 10/17 the writer had no school, rail or park facts for Villanova, Radnor
    # or Wayne at all.
    out += _places_items(area, facts)

    demo = facts.get("demographics") or {}
    for key, label, fmt in (("population", "Population", lambda v: f"{int(v):,}"),
                            ("household_count", "Households", lambda v: f"{int(v):,}"),
                            ("homeownership_rate", "Owner-occupied share of homes", lambda v: f"{round(v * 100)}%")):
        d = demo.get(key) or {}
        if d.get("value") is not None:
            out.append({"topic": "demographics", "claim": f"{label} of {d.get('geography', '').split(' - ')[-1] or town}: {fmt(d['value'])} (U.S. Census ACS 5-year, {d.get('year')}).",
                        "source_url": "https://data.census.gov", "source_name": "U.S. Census Bureau", "as_of": str(d.get("year") or ""), "confidence": 0.95})

    for item in out:
        item.setdefault("source_name", _domain(item.get("source_url")) or "OpenHousePal Areas research")
        item.update({"town": town, "origin": "idx_research"})
        item["id"] = _iid(town, item["claim"])
    return out


_PLACES_TOPICS = (("transportation", "transit", "rail station"), ("parks", "parks", "park"), ("downtown_areas", "downtown", "town center"),
                  ("amenities", "dining", "shopping or dining spot"))


def _places_items(area: MarketArea, facts: dict) -> list[dict]:
    """Evidence from the place lookups every enriched town has (names + addresses only - an
    address says exactly where a place is, so "nearby" never turns into "in town")."""
    out: list[dict] = []
    for key, topic, label in _PLACES_TOPICS:
        block = facts.get(key) or {}
        when = _date(block.get("fetched_at"))
        seen: set[str] = set()
        for it in (block.get("items") or [])[:8]:
            name, addr = (it.get("name") or "").strip(), (it.get("address") or "").strip()
            pid = it.get("place_id") or name
            if not name or not addr or pid in seen or name.lower() == area.name.lower():
                continue
            seen.add(pid)
            out.append({"topic": topic, "claim": f"{name} ({label}) - {addr}.", "source_name": "Google Maps (Places)",
                        "source_url": None, "as_of": when, "confidence": 0.9})
    walk = facts.get("walk") or {}
    if walk.get("rows") and walk.get("homes_checked"):
        mins = walk.get("walk_minutes") or 15
        for r in walk["rows"]:
            if r.get("label") and r.get("pct") is not None:
                out.append({"topic": "transit" if r.get("key") == "transit" else "downtown",
                            "claim": f"{r['pct']}% of the {walk['homes_checked']} {area.name} homes we checked are within a {mins}-minute walk of {r['label'].lower()} (OpenStreetMap).",
                            "source_name": "OpenStreetMap (OpenHousePal walk check)", "source_url": "https://www.openstreetmap.org", "as_of": _date(walk.get("computed_at")), "confidence": 0.9})
        if walk.get("stations"):
            out.append({"topic": "transit", "claim": f"Rail and trolley stops within a {mins}-minute walk of {area.name} homes: {', '.join(walk['stations'][:6])} (OpenStreetMap).",
                        "source_name": "OpenStreetMap (OpenHousePal walk check)", "source_url": "https://www.openstreetmap.org", "as_of": _date(walk.get("computed_at")), "confidence": 0.9})
    commute = facts.get("commute") or {}
    for d in (commute.get("destinations") or [])[:4]:
        if d.get("name") and d.get("drive_minutes"):
            transit = f" and about {d['transit_minutes']} minutes by public transit" if d.get("transit_minutes") else ""
            out.append({"topic": "transit", "claim": f"From {area.name}, {d['name']} is about {d.get('miles')} miles away: about {d['drive_minutes']} minutes by car{transit}, leaving at {commute.get('departure') or 'a weekday morning'} (Google Maps estimate).",
                        "source_name": "Google Maps", "source_url": None, "as_of": _date(commute.get("computed_at")), "confidence": 0.85})
    flood = facts.get("flood") or {}
    if flood.get("homes_checked") and flood.get("high_risk_pct") is not None:
        out.append({"topic": "other", "claim": f"{flood['high_risk_pct']}% of the {flood['homes_checked']} {area.name} homes we checked sit in a FEMA high-risk flood zone (FEMA National Flood Hazard Layer); flood risk is address-specific.",
                    "source_name": "FEMA National Flood Hazard Layer", "source_url": "https://msc.fema.gov/portal/home", "as_of": _date(flood.get("computed_at")), "confidence": 0.9})
    return out


# ---------------------------------------------------------------------------
# Geography truth - which townships, counties and school districts a town's homes actually sit
# in (Census TIGER boundary shares stored on the town + the MLS records' own township / county /
# district fields). Every geography sentence an article makes is checked against this, and any
# researched or banked fact that contradicts it loses that sentence before a writer sees it.
# ---------------------------------------------------------------------------

_GEO_NOISE = re.compile(r"\b(township|twp|borough|boro|city of|school district|area|sd|county|co)\b\.?", re.I)


def norm_place(name: Optional[str]) -> str:
    """'Tredyffrin-Easttown School District' / 'TREDYFFRIN-EASTTOWN' -> 'tredyffrin easttown';
    'Lower Merion Township' / 'LOWER MERION' -> 'lower merion'; 'DELAWARE-PA' -> 'delaware'."""
    s = re.sub(r"-(pa|nj|de|md|va|dc|wv)$", "", (name or "").strip(), flags=re.I)
    s = _GEO_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def _county_label(raw: Optional[str]) -> Optional[str]:
    n = norm_place(raw)
    return f"{n.title()} County" if n else None


def _township_label(raw: str) -> str:
    n = norm_place(raw)
    return f"{n.title()} Township" if n else raw


async def geography_profile(db: AsyncSession, area: MarketArea) -> dict:
    """{"town", "kind", "townships": [{name, share, county}], "school_districts": [{name, share}],
    "counties": [...], "mailing_towns": [...], "norm": {...sets for the audit}, "items": [...]}."""
    from app.services.areas import geography
    from app.services.areas.market_stats_service import area_geography_mix
    facts = area.facts or {}
    bounds = facts.get("boundaries") or {}
    try:
        mix = await area_geography_mix(db, geography.scope_for_market_area(area))
    except Exception:
        logger.warning("geography profile: MLS mix failed for %s", area.slug, exc_info=True)
        mix = {}
    county_of = {norm_place(t["name"]): _county_label(t.get("county")) for t in mix.get("townships") or []}

    # Townships: the stored TIGER shares first; MLS-only names join when they hold 3%+ of homes.
    towns: dict[str, dict] = {}
    for m in bounds.get("municipalities") or []:
        if m.get("name"):
            towns[norm_place(m["name"])] = {"name": m["name"], "share": m.get("share"), "county": None, "source": "census"}
    for t in mix.get("townships") or []:
        k = norm_place(t["name"])
        if k and k not in towns and t["share"] >= 3 and t["homes"] >= 3:
            towns[k] = {"name": _township_label(t["name"]), "share": t["share"], "county": None, "source": "mls"}
    for k, t in towns.items():
        t["county"] = county_of.get(k)
    districts: dict[str, dict] = {}
    for d in bounds.get("school_districts") or []:
        if d.get("name"):
            districts[norm_place(d["name"])] = {"name": d["name"], "share": d.get("share")}
    for d in mix.get("school_districts") or []:
        k = norm_place(d["name"])
        if k and k not in districts and d["share"] >= 3 and d["homes"] >= 3:
            districts[k] = {"name": f"{d['name'].title()} School District", "share": d["share"]}
    counties = [c for c in (mix.get("counties") or []) if c["share"] >= 3]
    mailing = [m for m in (mix.get("mailing_towns") or []) if m["share"] >= 1]

    kind = (area.municipality_type or "").lower() or ("neighborhood" if area.area_type == "neighborhood" else "postal town")
    source = "U.S. Census TIGER boundaries + Bright MLS records" if bounds.get("municipalities") else "Bright MLS records"
    homes = bounds.get("homes_checked") or mix.get("homes")
    when = _date(bounds.get("computed_at")) or datetime.now(timezone.utc).date().isoformat()
    items: list[dict] = []

    def add(claim: str):
        items.append({"topic": "geography", "claim": claim, "source_name": source, "source_url": None, "as_of": when,
                      "confidence": 0.97, "town": _town(area), "origin": "geo_verified", "id": _iid(_town(area), claim)})

    tw = sorted(towns.values(), key=lambda t: -(t.get("share") or 0))
    if tw:
        parts = [f"{t['name']}" + (f", {t['county']}" if t.get("county") else "") + (f" ({t['share']}%)" if t.get("share") is not None else "") for t in tw]
        if kind == "township" and len(tw) >= 1 and norm_place(tw[0]["name"]) == norm_place(area.name):
            others = [m for m in mailing if norm_place(m["name"]) != norm_place(area.name)][:7]
            add(f"{area.name} here means {tw[0]['name']}" + (f" ({tw[0]['county']})" if tw[0].get("county") else "")
                + " - the township, not one mailing address."
                + (f" Homes in it carry these mailing addresses: {', '.join(m['name'].title() for m in others)} as well as {area.name}." if others else "")
                + (f" A few {area.name}-addressed homes outside the township (in {', '.join(t['name'] for t in tw[1:])}) are included too." if len(tw) > 1 else ""))
        else:
            add(f"A {area.name} mailing address is not one municipality: {area.name} homes sit in " + "; ".join(parts)
                + (f" (shares of {homes} homes checked)" if homes else "") + f". No {area.name} home on record is in any other township or borough.")
    if districts:
        ds = sorted(districts.values(), key=lambda d: -(d.get("share") or 0))
        add(f"Public school districts serving {area.name} homes: " + "; ".join(d["name"] + (f" ({d['share']}%)" if d.get("share") is not None else "") for d in ds)
            + ". Attendance depends on the specific address - verify with the district.")
    if counties and len(counties) > 1 and not tw:
        add(f"{area.name} homes span " + ", ".join(f"{_county_label(c['name'])} ({c['share']}%)" for c in counties) + ".")
    return {
        "town": area.name, "slug": area.slug, "kind": kind, "townships": tw, "school_districts": list(districts.values()),
        "counties": [_county_label(c["name"]) for c in counties], "mailing_towns": [m["name"].title() for m in mailing],
        "norm": {"townships": set(towns) | ({norm_place(area.name)} if kind == "township" else set()),
                 "districts": set(districts), "counties": {norm_place(c["name"]) for c in counties},
                 "mailing": {norm_place(m["name"]) for m in mailing}},
        "items": items,
    }


async def known_places(db: AsyncSession, counties: list[str], state: Optional[str]) -> dict:
    """Every township / county name on record in these counties (and the counties themselves) -
    what the geography audit recognizes as a place name in a sentence."""
    from sqlalchemy import func
    from app.models.database import Property
    raw = [c for c in counties if c]
    if not raw:
        return {"townships": set(), "counties": set()}
    norm_c = {norm_place(c) for c in raw}
    rows = (await db.execute(select(Property.county, Property.township).where(Property.state == state, Property.county.isnot(None))
                             .group_by(Property.county, Property.township).having(func.count(Property.id) >= 3))).all() if state else []
    townships = {norm_place(t) for c, t in rows if t and norm_place(c) in norm_c}
    all_counties = {norm_place(c) for c, _ in rows if c}
    return {"townships": {t for t in townships if len(t) >= 4}, "counties": all_counties}


_CUE = re.compile(r"\b(in|within|part of|parts of|partly|partially|lie|lies|lying|span|spans|spanning|straddle|straddles|split|located|sit|sits|fall|falls|cover|covers|include|includes|served by|serve|serves|belong|belongs)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r";|\bwhile\b|\bwhereas\b|\bbut\b", re.I)


def _rx(norm: str):
    return re.compile(r"\b" + r"[\s\-]+".join(re.escape(w) for w in norm.split()) + r"\b", re.I)


def geography_audit(text: str, profiles: list[dict], known: Optional[dict] = None) -> list[dict]:
    """Sentences that put one of the article's towns in a township, county or school district its
    homes are NOT in (per geography_profile). Deterministic - no model. Each finding:
    {"sentence", "town", "place", "kind", "truth"}."""
    if not text or not profiles:
        return []
    known = known or {}
    munis = set().union(*(p["norm"]["townships"] for p in profiles), known.get("townships") or set())
    counties = set().union(*(p["norm"]["counties"] for p in profiles), known.get("counties") or set())
    districts_all = set().union(*(p["norm"]["districts"] for p in profiles))
    town_rx = [(p, _rx(norm_place(p["town"]))) for p in profiles]
    muni_rx = [(m, _rx(m)) for m in sorted(munis, key=len, reverse=True) if m]
    county_rx = [(c, re.compile(r"\b" + r"[\s\-]+".join(re.escape(w) for w in c.split()) + r"\s+County\b", re.I)) for c in counties if c]
    # A district named after one township needs "School District" after it to count as a district;
    # a joint one (Tredyffrin-Easttown) is a district wherever it appears, suffix or not.
    district_rx = [(d, re.compile(r"\b" + r"[\s\-]+".join(re.escape(w) for w in d.split())
                                  + (r"(\s+(area|township))?\s+(school district|sd)\b" if d in munis else r"\b(\s+(area\s+)?(school district|sd)\b)?"), re.I))
                   for d in districts_all if d]
    findings, seen = [], set()
    for sentence in _SENT.split(text):
        for clause in _CLAUSE.split(sentence):
            if not clause or not _CUE.search(clause):
                continue
            # District names are checked as districts (Tredyffrin-Easttown is a district, not a
            # claim about Easttown Township), then masked (same length, so positions hold) before
            # county / township matching.
            body = clause
            places: list[tuple[int, int, str, str]] = []  # (start, end, kind, name)

            def take(rx, kind, name):
                nonlocal body
                for m in list(rx.finditer(body)):
                    places.append((m.start(), m.end(), kind, name))
                body = rx.sub(lambda m: "#" * len(m.group(0)), body)
            for d, rx in district_rx:
                take(rx, "school district", d)
            for c, rx in county_rx:
                take(rx, "county", c)
            towns = sorted(((m.start(), m.end(), p) for p, rx in town_rx for m in rx.finditer(body)), key=lambda t: t[0])
            if not towns:
                continue
            for m, rx in muni_rx:
                for hit in rx.finditer(body):
                    places.append((hit.start(), hit.end(), "township", m))
            # "Villanova lies partly in Radnor": Radnor (a township-type town in the article) is
            # the PLACE there, not a second subject.
            muni_spans = {(s, e) for s, e, k, _ in places if k == "township"}
            if len(towns) > 1:
                towns = [t for t in towns if not (t[2]["kind"] == "township" and (t[0], t[1]) in muni_spans)] or towns
            # Towns joined as one subject ("Villanova and Wayne lie in ...") share every place;
            # otherwise each place belongs to the town mentioned nearest to it ("a Bryn Mawr home
            # in Lower Merion and a Wayne home in Tredyffrin" is two separate, true claims).
            groups: list[list[tuple]] = []
            for t in towns:
                if groups and re.fullmatch(r"\s*(,|and|or|&|,\s*and|,\s*or)\s*", body[groups[-1][-1][1]:t[0]], re.I):
                    groups[-1].append(t)
                else:
                    groups.append([t])
            for s, e, kind, place in places:
                def dist(g):
                    return min(abs(s - t[1]) if s >= t[1] else abs(t[0] - e) for t in g)
                group = min(groups, key=dist)
                for _, _, p in group:
                    allowed = p["norm"]["townships" if kind == "township" else "counties" if kind == "county" else "districts"]
                    if (kind == "township" and place == norm_place(p["town"])) or not allowed or place in allowed:
                        continue
                    truth = "; ".join(t["name"] for t in p["townships"]) or "unknown"
                    key = (sentence.strip(), p["town"], place)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({"sentence": sentence.strip(), "town": p["town"], "place": place.title(), "kind": kind,
                                     "truth": f"{p['town']} homes sit in {truth}" + (f"; districts: {', '.join(d['name'] for d in p['school_districts'])}" if p["school_districts"] else "")})
    return findings


def scrub_geography(items: list[dict], profiles: list[dict], known: Optional[dict] = None) -> tuple[list[dict], int]:
    """Drop the sentences of stored/researched facts that contradict the geography profiles
    (a tax fact once carried "Villanova and Wayne lie partly in Lower Merion and Radnor" - a
    researcher's own aside, banked and reused). Returns (clean items, sentences removed)."""
    removed = 0
    out = []
    for it in items:
        if it.get("origin") in ("geo_verified", "mls_live"):
            out.append(it)
            continue
        bad = {f["sentence"] for f in geography_audit(it.get("claim") or "", profiles, known)}
        if not bad:
            out.append(it)
            continue
        kept = [s for s in _SENT.split(it["claim"]) if s.strip() and s.strip() not in bad]
        removed += len(bad)
        if kept:
            out.append({**it, "claim": " ".join(kept)})
    return out, removed


def bank_items(area: MarketArea, *, now: Optional[datetime] = None) -> list[dict]:
    """Facts later research added to this town's shared bank, minus anything past its shelf life."""
    now = now or datetime.now(timezone.utc)
    keep = []
    for item in ((area.facts or {}).get("evidence_bank") or []):
        try:
            saved = datetime.fromisoformat(item.get("saved_at"))
        except Exception:
            continue
        if now - saved <= timedelta(days=_BANK_TTL_DAYS.get(item.get("topic"), _BANK_TTL_DEFAULT)):
            keep.append({**item, "origin": "evidence_bank"})
    return keep


async def save_to_bank(db: AsyncSession, area: MarketArea, items: list[dict]) -> int:
    """Add newly researched, sourced facts about this town to its shared bank (deduped, capped,
    expired items pruned). Only items with a real source URL are kept. Returns how many were added."""
    if not items:
        return 0
    now = datetime.now(timezone.utc)
    current = bank_items(area, now=now)
    seen = {i.get("id") for i in current}
    added = 0
    for it in items:
        if not it.get("source_url") or not it.get("claim"):
            continue
        iid = _iid(_town(area), it["claim"])
        if iid in seen:
            continue
        seen.add(iid)
        current.append({
            "id": iid, "town": _town(area), "topic": it.get("topic") or "other", "claim": it["claim"],
            "source_name": it.get("source_name") or _domain(it.get("source_url")), "source_url": it["source_url"],
            "as_of": it.get("as_of"), "confidence": it.get("confidence"), "saved_at": now.isoformat(),
        })
        added += 1
    if added:
        facts = dict(area.facts or {})
        facts["evidence_bank"] = [{k: v for k, v in i.items() if k != "origin"} for i in current[-_BANK_MAX_ITEMS:]]
        area.facts = facts
        flag_modified(area, "facts")
        await db.commit()
    return added


def _fresh(items: list[dict], now: datetime) -> list[dict]:
    keep = []
    for item in items or []:
        try:
            saved = datetime.fromisoformat(item.get("saved_at"))
        except Exception:
            continue
        if now - saved <= timedelta(days=_BANK_TTL_DAYS.get(item.get("topic"), _BANK_TTL_DEFAULT)):
            keep.append(item)
    return keep


async def state_items(db: AsyncSession, state: str) -> list[dict]:
    """State-wide facts already researched (shared_knowledge "state:XX"), minus expired ones."""
    from app.models.database import SharedKnowledge
    row = (await db.execute(select(SharedKnowledge).where(SharedKnowledge.scope == f"state:{(state or '').upper()}"))).scalar_one_or_none()
    return [{**i, "origin": "state_bank", "town": "general"} for i in _fresh(row.items if row else [], datetime.now(timezone.utc))]


async def save_state_items(db: AsyncSession, state: str, items: list[dict]) -> int:
    """Add sourced state-wide facts to the shared store (deduped, capped, expired pruned)."""
    from app.models.database import SharedKnowledge
    items = [i for i in items if i.get("source_url") and i.get("claim") and i.get("topic") != "rates" and "freddie mac" not in i["claim"].lower()]
    if not items or not state:
        return 0
    scope = f"state:{state.upper()}"
    row = (await db.execute(select(SharedKnowledge).where(SharedKnowledge.scope == scope))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    current = _fresh(row.items if row else [], now)
    # A newer reading of a fast-moving fact (this week's mortgage rate) replaces the old one.
    if any(i.get("topic") == "rates" for i in items):
        current = [c for c in current if c.get("topic") != "rates"]
    seen = {c.get("id") for c in current}
    added = 0
    for it in items:
        iid = _iid(scope, it["claim"])
        if iid in seen:
            continue
        seen.add(iid)
        current.append({"id": iid, "topic": it.get("topic") or "other", "claim": it["claim"], "source_name": it.get("source_name") or _domain(it.get("source_url")),
                        "source_url": it["source_url"], "as_of": it.get("as_of"), "confidence": it.get("confidence"), "saved_at": now.isoformat()})
        added += 1
    if added:
        if row is None:
            row = SharedKnowledge(scope=scope, items=[])
            db.add(row)
        row.items = current[-_BANK_MAX_ITEMS:]
        flag_modified(row, "items")
        await db.commit()
    return added


# ---------------------------------------------------------------------------
# Mortgage rates straight from Freddie Mac's published PMMS history (free, no key, updated every
# Thursday) - exact and dated, so no AI research is spent on rates. Cached in-process for 6h.
# ---------------------------------------------------------------------------

_PMMS_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
_pmms_cache: dict = {"at": None, "rows": None}


async def _pmms_rows() -> list:
    import csv
    import io
    import httpx
    now = datetime.now(timezone.utc)
    if not _pmms_cache["rows"] or not _pmms_cache["at"] or now - _pmms_cache["at"] > timedelta(hours=6):
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0 (OpenHousePal)"}) as client:
            resp = await client.get(_PMMS_URL, follow_redirects=True)
            resp.raise_for_status()
        rows = []
        for r in csv.DictReader(io.StringIO(resp.text)):
            try:
                d = datetime.strptime(r["date"].strip(), "%m/%d/%Y").date()
                rows.append((d, float(r["pmms30"]) if r.get("pmms30", "").strip() else None, float(r["pmms15"]) if r.get("pmms15", "").strip() else None))
            except (ValueError, KeyError):
                continue
        _pmms_cache.update(at=now, rows=rows)
    return _pmms_cache["rows"] or []


async def freddie_mac_latest() -> Optional[dict]:
    """{"rate_30", "rate_15", "as_of"} from the newest survey week."""
    rows = await _pmms_rows()
    if not rows:
        return None
    d, r30, r15 = rows[-1]
    return {"rate_30": r30, "rate_15": r15, "as_of": d.isoformat()}


async def freddie_mac_rate_items() -> list[dict]:
    rows = await _pmms_rows()
    if len(rows) < 2:
        return []
    latest, prev = rows[-1], rows[-2]
    year_ago = min(rows, key=lambda r: abs((r[0] - (latest[0] - timedelta(days=365))).days))
    day = latest[0].strftime("%B %-d, %Y") if os.name != "nt" else latest[0].strftime("%B %#d, %Y")
    items = []

    def add(claim):
        items.append({"topic": "rates", "claim": claim, "source_name": "Freddie Mac Primary Mortgage Market Survey", "source_url": "https://www.freddiemac.com/pmms",
                      "as_of": latest[0].isoformat(), "confidence": 0.99, "town": "general", "origin": "freddie_mac", "id": _iid("pmms", claim)})

    if latest[1] is not None:
        add(f"Freddie Mac's Primary Mortgage Market Survey for the week of {day}: the U.S. average 30-year fixed rate was {latest[1]:.2f}%"
            + (f", vs {prev[1]:.2f}% the week before" if prev[1] is not None else "")
            + (f" and {year_ago[1]:.2f}% a year earlier." if year_ago[1] is not None else "."))
    if latest[2] is not None:
        add(f"Freddie Mac's survey for the week of {day}: the U.S. average 15-year fixed rate was {latest[2]:.2f}%"
            + (f" (vs {year_ago[2]:.2f}% a year earlier)." if year_ago[2] is not None else "."))
    return items


def _fmt_money(v) -> str:
    v = float(v)
    return f"${v / 1_000_000:.2f}M".replace(".00M", "M") if v >= 1_000_000 else f"${round(v / 1000):,}K"


_HOME_TYPES_NOTE = "houses, townhomes, condos and multi-family homes - no rentals, land or commercial listings"


def market_definition(area: MarketArea, geo: Optional[dict] = None) -> str:
    """What an area's market numbers cover, in one plain sentence (also shown under the article's
    Sources). Without it, "Radnor" (the whole township, including many Wayne and Villanova
    addresses) and "Wayne" (a mailing address across three townships) read like separate places."""
    geo = geo or {}
    tw = geo.get("townships") or []
    if (area.municipality_type or "").lower() == "township":
        others = [m for m in geo.get("mailing_towns") or [] if norm_place(m) != norm_place(area.name)][:5]
        return (f"{area.name} numbers cover every home in {area.name} Township"
                + (f" (including homes with {', '.join(others)} mailing addresses)" if others else "")
                + f" plus {area.name}-addressed homes; {_HOME_TYPES_NOTE}.")
    if area.area_type == "neighborhood":
        return f"{area.name} numbers cover homes inside the {area.name} boundary on our map; {_HOME_TYPES_NOTE}."
    where = ", ".join(t["name"] for t in tw) if len(tw) > 1 else ""
    return f"{area.name} numbers cover homes with a {area.name} mailing address" + (f", which spans {where}" if where else "") + f"; {_HOME_TYPES_NOTE}."


def sales_snapshot_items(area: MarketArea, snap: dict, geo: Optional[dict] = None) -> list[dict]:
    """The dated Bright MLS snapshot as evidence items - each says what it covers, "for sale" is
    active listings only (under contract counted on its own), year-ago figures only when our MLS
    history for that window is complete, and small samples say so."""
    if not snap or not snap.get("current"):
        return []
    town = _town(area)
    cur, prior = snap["current"], (snap.get("prior_year") or {}) if snap.get("prior_year_reliable", True) else {}
    through = snap.get("data_through")
    win = f"the {snap.get('window_days', 90)} days ending {through}"
    small = (cur.get("closed_count") or 0) < 10
    out = []

    def add(claim, as_of=through):
        out.append({"topic": "market", "claim": claim, "source_name": "Bright MLS (OpenHousePal data)", "source_url": None,
                    "as_of": as_of, "confidence": 0.97, "town": town, "origin": "mls_live", "id": _iid(town, claim)})

    add(f"Definition - {market_definition(area, geo)}")
    if cur.get("closed_count") is not None:
        add(f"{area.name}: {cur['closed_count']} closed sales in {win}"
            + (f" (vs {prior['closed_count']} in the same window a year earlier)" if prior.get("closed_count") else "")
            + (" - a small sample, so one or two sales can move its medians a lot." if small else "."))
    if cur.get("median_close_price"):
        add(f"{area.name}: median closed price {_fmt_money(cur['median_close_price'])} in {win}" + (f" (vs {_fmt_money(prior['median_close_price'])} a year earlier)." if prior.get("median_close_price") and (prior.get("closed_count") or 0) >= 10 else "."))
    if cur.get("median_sale_to_list_pct"):
        add(f"{area.name}: median sale-to-list price ratio {cur['median_sale_to_list_pct']}% in {win} (half of those homes sold above that ratio, half below)"
            + (f"; {prior['median_sale_to_list_pct']}% a year earlier." if prior.get("median_sale_to_list_pct") and (prior.get("closed_count") or 0) >= 10 else "."))
    if cur.get("median_days_on_market") is not None:
        add(f"{area.name}: median {cur['median_days_on_market']} days on market for homes that closed in {win}" + (f" (vs {prior['median_days_on_market']} a year earlier)." if prior.get("median_days_on_market") is not None and (prior.get("closed_count") or 0) >= 10 else "."))
    if snap.get("active_count") is not None:
        on = snap.get("inventory_as_of") or datetime.now(timezone.utc).date().isoformat()
        add(f"{area.name}: {snap['active_count']} homes for sale (active listings) as of {on}"
            + (f", median list price {_fmt_money(snap['active_median_list_price'])}" if snap.get("active_median_list_price") else "")
            + (f"; a quarter of them are listed at or below {_fmt_money(snap['active_p25_list_price'])}" if snap.get("active_p25_list_price") and snap["active_count"] >= 8 else "")
            + (f". Another {snap['under_contract_count']} are already under contract or pending (not counted as for sale)." if snap.get("under_contract_count") else "."), as_of=on)
    if not snap.get("prior_year_reliable", True):
        add(f"{area.name}: year-ago comparisons are left out - our Bright MLS history for the same window a year earlier is incomplete, so those numbers would mislead.")
    return out


def cost_items(area: MarketArea, cost: dict) -> list[dict]:
    """Owning costs read off the listings (taxes per township, HOA fees) and how recent buyers
    paid - local, dated, sourced numbers a first-time-buyer article can lean on."""
    if not cost:
        return []
    town = _town(area)
    on = cost.get("as_of")
    out = []

    def add(claim, topic="taxes"):
        out.append({"topic": topic, "claim": claim, "source_name": "Bright MLS listing data (OpenHousePal)", "source_url": None,
                    "as_of": on, "confidence": 0.9, "town": town, "origin": "mls_live", "id": _iid(town, claim)})

    tax = cost.get("tax") or {}
    if tax:
        add(f"{area.name}: the {tax['homes']} homes for sale that list an annual property tax show a median bill of ${tax['median_annual']:,}"
            + (f" (tax year {tax['tax_year']}, as entered on the listings)" if tax.get("tax_year") else " (as entered on the listings)")
            + f" - a median of {tax['median_pct_of_list']}% of list price per year. A buyer's bill depends on the home's assessment, not its price.")
        for t in tax.get("by_township") or []:
            if len(tax["by_township"]) > 1:
                add(f"{area.name} homes for sale in {t['township']} Township: median annual tax about {t['median_pct_of_list']}% of list price ({t['homes']} listings) - millage is set by the township, county and school district, so it differs by township.")
    hoa = cost.get("hoa") or {}
    if hoa:
        add(f"{area.name}: condos and townhomes for sale that list a monthly association fee show a median of ${hoa['median_monthly']:,}/month ({hoa['homes']} listings).", "market")
    fin = cost.get("financing") or {}
    if fin.get("mix"):
        mix = ", ".join(f"{m['type']} {m['pct']}%" for m in fin["mix"][:5])
        add(f"{area.name}: how buyers paid on the {fin['recorded']} closed sales since {fin['since']} that recorded financing in Bright MLS: {mix}"
            + (f" ({fin['closed_sales'] - fin['recorded']} more sales left financing blank)." if fin["closed_sales"] > fin["recorded"] else "."), "market")
    return out


def overlap_items(pairs: list[tuple[MarketArea, MarketArea, int, int]]) -> list[dict]:
    """(a, b, shared homes for sale, a's homes for sale) -> 'don't add these together' notes."""
    out = []
    for a, b, shared, total in pairs:
        if shared <= 0:
            continue
        claim = (f"Overlap: {shared} of {a.name}'s {total} homes for sale are also inside {b.name}'s numbers (a {a.name} mailing address within "
                 f"{b.name} Township), so the areas' counts must never be added together.") if (b.municipality_type or "").lower() == "township" else (
                 f"Overlap: {shared} of {a.name}'s {total} homes for sale are also counted in {b.name}'s numbers, so the areas' counts must never be added together.")
        out.append({"topic": "market", "claim": claim, "source_name": "Bright MLS (OpenHousePal data)", "source_url": None,
                    "as_of": datetime.now(timezone.utc).date().isoformat(), "confidence": 0.97, "town": _town(a), "origin": "mls_live", "id": _iid(_town(a), claim)})
    return out


# ---------------------------------------------------------------------------
# Federal home-loan basics - the rules that differ by LOAN TYPE, from the agencies themselves
# (hand-verified 10/24/2026). An article about buying must never state one loan type's rule as
# everyone's ("under 20% down means mortgage insurance" is conventional-only; VA has none).
# `valid_through`: items whose numbers change yearly drop out after that date.
# ---------------------------------------------------------------------------

_FEDERAL_FACTS = [
    ("regulation", "Conventional loans: with a down payment under 20% of the purchase price, the lender may require private mortgage insurance (PMI).",
     "CFPB: What is private mortgage insurance?", "https://www.consumerfinance.gov/ask-cfpb/what-is-private-mortgage-insurance-en-122/", "2026-10", None),
    ("regulation", "Conventional loans: a borrower can ask the servicer to cancel PMI once the balance is scheduled to reach 80% of the home's original value, and the servicer generally must end it at 78% (if payments are current).",
     "CFPB: When can I remove PMI?", "https://www.consumerfinance.gov/ask-cfpb/when-can-i-remove-private-mortgage-insurance-pmi-from-my-loan-en-202/", "2026-10", None),
    ("regulation", "FHA loans allow down payments as low as 3.5%, and mortgage insurance is required on all FHA loans, whatever the down payment.",
     "CFPB: FHA loans", "https://www.consumerfinance.gov/owning-a-home/fha-loans/", "2026-10", None),
    ("regulation", "FHA: under HUD's current policy a minimum credit score of 580 qualifies for maximum financing (3.5% down); scores of 500-579 are limited to 90% loan-to-value (10% down).",
     "HUD: FHA minimum credit score", "https://answers.hud.gov/FHA/s/article/Does-FHA-require-a-minimum-credit-score-and-how-is-it-determined", "2026-10", None),
    ("regulation", "VA-backed purchase loans (for eligible service members, veterans and some surviving spouses): no down payment as long as the price isn't above the appraised value, and no PMI or mortgage insurance premium; a one-time VA funding fee may apply.",
     "VA: Purchase loan", "https://www.va.gov/housing-assistance/home-loans/loan-types/purchase-loan/", "2026-10", None),
    ("regulation", "Fannie Mae's HomeReady conventional loan allows down payments as low as 3% for borrowers with income at or below 80% of their area's median; it is not limited to first-time buyers.",
     "Fannie Mae: HomeReady", "https://yourhome.fanniemae.com/buy/homeready", "2026-10", None),
    ("regulation", "FHFA set the 2026 baseline conforming loan limit for one-unit homes at $832,750 (up from $806,500 in 2025); loans above a county's limit are 'jumbo' loans with their own rules and rates.",
     "FHFA: 2026 conforming loan limits", "https://www.fhfa.gov/news/news-release/fhfa-announces-conforming-loan-limit-values-for-2026", "2025-11-25", "2026-12-31"),
    ("taxes", "Closing costs, not counting the down payment, typically run 2-5% of the home's purchase price.",
     "CFPB: Determine your down payment (closing costs)", "https://www.consumerfinance.gov/owning-a-home/prepare/determine-your-down-payment/", "2026-10", None),
    ("regulation", "Lenders must give the borrower the Closing Disclosure three business days before the scheduled closing.",
     "CFPB: Closing Disclosure", "https://www.consumerfinance.gov/owning-a-home/closing-disclosure/", "2026-10", None),
]


def conforming_limit(today=None) -> Optional[int]:
    """The one-unit baseline conforming loan limit while the item above is current."""
    return 832_750 if (today or datetime.now(timezone.utc).date()).isoformat() <= "2026-12-31" else None


def federal_items(today=None) -> list[dict]:
    today = (today or datetime.now(timezone.utc).date()).isoformat()
    return [{"topic": t, "claim": c, "source_name": n, "source_url": u, "as_of": a, "confidence": 0.98, "town": "general",
             "origin": "federal", "id": _iid("federal", c)}
            for t, c, n, u, a, through in _FEDERAL_FACTS if not through or today <= through]


async def find_areas(db: AsyncSession, *, slugs: list[str] = (), names: list[str] = (), state: Optional[str] = None) -> list[MarketArea]:
    """MarketAreas by slug, or by town name ("Ardmore", "Ardmore, PA")."""
    found: dict[str, MarketArea] = {}
    if slugs:
        for a in (await db.execute(select(MarketArea).where(MarketArea.slug.in_(list(slugs))))).scalars().all():
            found[a.id] = a
    for raw in names or []:
        name, _, st = str(raw).partition(",")
        q = select(MarketArea).where(MarketArea.name.ilike(name.strip()))
        st = (st.strip() or state or "").upper()
        if st:
            q = q.where(MarketArea.state == st)
        a = (await db.execute(q.limit(1))).scalars().first()
        if a:
            found[a.id] = a
    return list(found.values())


async def town_dossier(db: AsyncSession, area: MarketArea, *, include_bank: bool = True, mls: bool = True) -> dict:
    """Everything we already know about one town, as evidence items + a coverage report:
    {"town", "slug", "items": [...], "covered": {topic: count}, "gaps": [topic], "stale": [topic],
    "sales_snapshot", "geo" (geography_profile), "cost" (listing cost profile)}."""
    from app.services.areas import geography
    from app.services.areas.market_stats_service import get_area_cost_profile, get_area_sales_snapshot

    scope = geography.scope_for_market_area(area)
    try:
        geo = await geography_profile(db, area)
    except Exception:
        logger.warning("town dossier: geography profile failed for %s", area.slug, exc_info=True)
        geo = {"town": area.name, "kind": "", "townships": [], "school_districts": [], "counties": [], "mailing_towns": [],
               "norm": {"townships": set(), "districts": set(), "counties": set(), "mailing": set()}, "items": []}
    items = list(geo["items"]) + research_items(area)
    # mls=False (No-MLS plans): no Bright MLS sales snapshot or listing cost profile - those are
    # built from the Bright mirror, which those agents may not use.
    snap: dict = {}
    cost: dict = {}
    if mls:
        try:
            snap = await get_area_sales_snapshot(db, scope)
        except Exception:
            logger.warning("town dossier: sales snapshot failed for %s", area.slug, exc_info=True)
            snap = {}
        items += sales_snapshot_items(area, snap, geo)
        try:
            cost = await get_area_cost_profile(db, scope)
        except Exception:
            logger.warning("town dossier: cost profile failed for %s", area.slug, exc_info=True)
            cost = {}
        items += cost_items(area, cost)
    if include_bank:
        items += bank_items(area)
    # Stored research can carry geography that contradicts the records (see scrub_geography).
    items, _ = scrub_geography(items, [geo])

    covered: dict[str, int] = {}
    for it in items:
        covered[it["topic"]] = covered.get(it["topic"], 0) + 1
    gaps = [t for t in COVERAGE_TOPICS if not covered.get(t)]
    # Third-party market context older than ~4 months doesn't count as current market coverage
    # when we don't also have the live MLS snapshot.
    stale = []
    if not any(i["origin"] == "mls_live" for i in items) and covered.get("market"):
        stale.append("market")
    return {"town": _town(area), "slug": area.slug, "items": items, "covered": covered, "gaps": gaps, "stale": stale,
            "sales_snapshot": snap, "geo": geo, "cost": cost, "definition": market_definition(area, geo),
            "researched_at": _date(area.last_enriched_at)}
