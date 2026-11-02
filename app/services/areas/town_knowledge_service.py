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
from datetime import date, datetime, timedelta, timezone
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

_CITE = re.compile(r"</?cite|cite index=", re.I)
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
            # 11/01 Rehoboth Beach: descriptions that were raw quoted web text with the research's
            # citation markup ('(cite index="13-1">The Shops at ... is a charming collection...').
            if _CITE.search(desc):
                desc = ""
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
                            "claim": (f"About {r['pct']}%" if r["pct"] else "Almost no") + f" {'of ' if r['pct'] else ''}{area.name} homes are within a {mins}-minute walk of {r['label'].lower()} (an OpenStreetMap estimate from a sample of {walk['homes_checked']} homes).",
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
        out.append({"topic": "other", "claim": f"About {flood['high_risk_pct']}% of {area.name} homes sit in a FEMA high-risk flood zone (FEMA National Flood Hazard Layer); flood risk is address-specific.",
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


_DISTRICT_GENERIC = {"regional", "schools", "school", "public", "district", "unified", "consolidated", "valley", "union", "joint", "city", "of", "the"}


def _district_core(norm: str) -> frozenset:
    """'princeton regional schools' and 'princeton public schools' -> {'princeton'}."""
    return frozenset(w for w in norm.split() if w not in _DISTRICT_GENERIC)


def _county_label(raw: Optional[str]) -> Optional[str]:
    n = norm_place(raw)
    # An independent city (Virginia's Alexandria, Baltimore City) is its own county-level place -
    # "Alexandria City", never "Alexandria City County".
    if n.endswith(" city"):
        return n.title()
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

    # Every local government the homes sit in, MEASURED by map location - small incorporated towns
    # and unincorporated county land included (11/01 grader: Wilmington's "unincorporated 58%" was
    # 100% minus the city and hid Bellefonte). Free Census data, cached; blog engine only.
    measured = None
    try:
        from app.services.areas.town_map_service import all_municipalities
        measured = await all_municipalities(db, area)
    except Exception:
        logger.warning("geography profile: municipality measurement failed for %s", area.slug, exc_info=True)

    towns: dict[str, dict] = {}
    small: list[dict] = []
    if measured and measured.get("rows"):
        def pct(v: float):
            return round(v) if v >= 1 else "under 1"
        main = max((m for m in measured["rows"] if m["kind"] == "county"), key=lambda m: m["homes"], default=None)
        # The counties that really hold the town's homes (1%+ of them, towns and county land together).
        weight: dict[str, float] = {}
        for m in measured["rows"]:
            if m.get("county"):
                weight[m["county"]] = weight.get(m["county"], 0) + m["share"]
        for m in measured["rows"]:
            # Stray homes are bad map locations, not places to name (11/01: a Cherry Hill home "in
            # Montgomery County", a Wilmington one in a PA township, Frederick homes "in Rockville", a
            # Baltimore one "in Annapolis City"). Every home is placed inside an official Census
            # boundary; a slice is a bad map point when it's tiny AND far from all the town's other
            # homes ("isolated", town_map_service). A small slice next to the others is a real place
            # and is named however rare (Annapolis' Highland Beach; round-13 grader: Baltimore
            # addresses in Anne Arundel County are real). A lone home across a state line never is.
            if m["homes"] < 2 and not m.get("same_state", True):
                continue
            if "isolated" in m:
                if m["isolated"]:
                    continue
            # Measurements saved before the distance test: the older rules.
            elif (m["homes"] < 2 and m["kind"] == "county" and m is not main) or \
                    (m["share"] < 1 and m.get("county") and weight.get(m["county"], 0) < 1):
                continue
            if m["share"] >= 1:
                towns[m["name"].strip().lower()] = {"name": m["name"], "share": pct(m["share"]), "sort": m["share"],
                                                "county": None, "source": "census", "kind": m["kind"]}
            else:
                small.append(m)
        if small:
            names = [m["name"] for m in small]
            what = "other incorporated towns" if all(m["kind"] == "place" for m in small) else "other municipalities"
            total = sum(m["share"] for m in small)
            towns["_small"] = {"name": f"{what} (" + ", ".join(names[:6]) + (" and others" if len(names) > 6 else "") + ")",
                               "share": pct(total), "sort": total, "county": None, "source": "census", "kind": "small"}
    # Not measured (no Census answer): the stored TIGER shares - whose "Unincorporated X County" row is
    # only the remainder (100% minus the towns on the map), so it never gets a share of its own below.
    for m in [] if towns else (bounds.get("municipalities") or []):
        if m.get("name"):
            towns[str(m["name"]).strip().lower()] = {"name": m["name"], "share": m.get("share"), "county": None, "source": "census",
                                            # the town map's leftover row (also when its kind wasn't kept)
                                            "remainder": m.get("kind") == "county" or str(m["name"]).lower().startswith("unincorporated")}
    for t in [] if measured and measured.get("rows") else (mix.get("townships") or []):
        k = norm_place(t["name"])
        # 11/01 Rehoboth Beach: Bright's township field holds Delaware hundreds ("LEWES REHOBOTH
        # HUNDRED" - an old land division, not a government) and came out "Lewes Rehoboth Hundred
        # Township (99%)" next to the census "Unincorporated Sussex County (86%)".
        # Only PA and NJ have townships: in MD the field is an election district ("Frederick
        # Township (46%)" next to Frederick City 59%), in DE a hundred, in VA/DC a county.
        if (area.state or "").upper() not in ("PA", "NJ") or re.search(r"\b(hundred|county)\b", str(t["name"]), re.I):
            continue
        if k and k not in {norm_place(x["name"]) for x in towns.values()} and t["share"] >= 3 and t["homes"] >= 3:
            towns[k] = {"name": _township_label(t["name"]), "share": t["share"], "county": None, "source": "mls"}
    for k, t in towns.items():
        t["county"] = county_of.get(norm_place(t["name"])) if k != "_small" else None
    districts: dict[str, dict] = {}
    for d in bounds.get("school_districts") or []:
        if d.get("name"):
            districts[norm_place(d["name"])] = {"name": d["name"], "share": d.get("share")}
    for d in mix.get("school_districts") or []:
        k = norm_place(d["name"])
        # 11/01 Princeton: Bright's "PRINCETON REGIONAL SCHOOLS" is the census "Princeton Public
        # Schools" - one district, listed twice as 50% and 48%.
        if any(_district_core(k) and _district_core(k) == _district_core(c) for c in districts):
            continue
        if k and k not in districts and d["share"] >= 3 and d["homes"] >= 3:
            # 11/01 grader: "Cherry Hill Township Public Schools School District" - no suffix twice.
            title = d["name"].title()
            districts[k] = {"name": title if re.search(r"\b(schools?|district)\b", title, re.I) else f"{title} School District", "share": d["share"]}
    counties = [c for c in (mix.get("counties") or []) if c["share"] >= 3]
    mailing = [m for m in (mix.get("mailing_towns") or []) if m["share"] >= 1]

    kind = (area.municipality_type or "").lower() or ("neighborhood" if area.area_type == "neighborhood" else "postal town")
    source = "U.S. Census TIGER boundaries + Bright MLS records" if bounds.get("municipalities") else "Bright MLS records"
    when = _date(bounds.get("computed_at")) or datetime.now(timezone.utc).date().isoformat()
    items: list[dict] = []

    def add(claim: str):
        items.append({"topic": "geography", "claim": claim, "source_name": source, "source_url": None, "as_of": when,
                      "confidence": 0.97, "town": _town(area), "origin": "geo_verified", "id": _iid(_town(area), claim)})

    tw = sorted(towns.values(), key=lambda t: (bool(t.get("remainder")), -float(t.get("sort", t.get("share") or 0))))

    def part(t: dict) -> str:
        if t.get("remainder"):
            # A leftover, not a measurement: no share, and say what else it can hold.
            county = re.sub(r"^unincorporated\s+", "", t["name"], flags=re.I)
            return f"the rest of {county} - unincorporated areas or other incorporated towns (not measured separately)"
        return (f"{t['name']}" + (f", {t['county']}" if t.get("county") else "")
                + (f" ({t['share']}% of {area.name}-addressed homes)" if t.get("share") is not None else ""))

    if tw:
        parts = [part(t) for t in tw]
        if kind == "township" and len(tw) >= 1 and norm_place(tw[0]["name"]) == norm_place(area.name):
            others = [m for m in mailing if norm_place(m["name"]) != norm_place(area.name)][:7]
            add(f"{area.name} here means {tw[0]['name']}" + (f" ({tw[0]['county']})" if tw[0].get("county") else "")
                + " - the township, not one mailing address."
                + (f" Homes in it carry these mailing addresses: {', '.join(m['name'].title() for m in others)} as well as {area.name}." if others else "")
                + (f" A few {area.name}-addressed homes outside the township (in {', '.join(t['name'] for t in tw[1:])}) are included too." if len(tw) > 1 else ""))
        else:
            # Each share says whose share it is (11/01 review: "Radnor Township includes homes with Wayne
            # (46%)..." read as if 46% of Radnor were Wayne) - and no sample counts, which the writer
            # carried into the article as "homes checked".
            # 11/01 grader: "No X home on record is in any other township" became "Every Cherry Hill
            # mailing address is in Cherry Hill Township" - a claim about records, not every address.
            placed = f" (placed on the map by each home's location: {measured['placed']:,} of {measured['homes']:,})" if measured and measured.get("rows") else ""
            lead = (f"{area.name}-addressed homes in our Bright MLS records{placed} sit in " if len(parts) == 1
                    else f"{_a(area.name).capitalize()} {area.name} mailing address is not one municipality: {area.name}-addressed homes in our Bright MLS records{placed} sit in ")
            add(lead + "; ".join(parts) + "."
                + " A mailing address doesn't decide the municipality or school district - confirm both for a specific home.")
    if districts:
        ds = sorted(districts.values(), key=lambda d: -(d.get("share") or 0))
        if len(ds) == 1 and (area.state or "").upper() in _COUNTY_WIDE_SCHOOLS and len(counties) <= 1:
            # Maryland, DC, West Virginia and Virginia run one school system per county (11/01 grader: "a few
            # Annapolis addresses may use other districts" was unsupported - AACPS runs them all).
            add(f"Public schools for {area.name}-addressed homes are run by {ds[0]['name']}. Which school a home is assigned to "
                "depends on its exact address - check it with the district.")
        else:
            # Never the complete list (11/01 grader: Wilmington's three districts left out Colonial).
            add(f"Public school districts serving {area.name}-addressed homes in our records include " + "; ".join(d["name"] + (f" ({d['share']}% of {area.name}-addressed homes)" if d.get("share") is not None else "") for d in ds)
                + ". Others may serve some addresses - the district depends on the specific address; verify with the district.")
    if counties and len(counties) > 1 and not tw:
        add(f"{area.name} homes span " + ", ".join(f"{_county_label(c['name'])} ({c['share']}%)" for c in counties) + ".")
    return {
        "town": area.name, "slug": area.slug, "kind": kind, "townships": tw, "school_districts": list(districts.values()),
        "counties": [_county_label(c["name"]) for c in counties], "mailing_towns": [m["name"].title() for m in mailing],
        # The measured outlines (every town + county land) - the tax split places listings with them.
        "muni_shapes": [{"name": m["name"], "rings": m["rings"]} for m in (measured or {}).get("rows") or [] if m.get("rings")],
        "norm": {"townships": {norm_place(t["name"]) for k, t in towns.items() if k != "_small"} | {norm_place(m["name"]) for m in small} | ({norm_place(area.name)} if kind == "township" else set()),
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
    # "Middlesex County", and a list that ends in "counties" ("Mercer, Middlesex and Somerset
    # counties" - 11/01 Princeton: Middlesex was read as the NJ borough of that name).
    county_rx = [(c, re.compile(r"\b" + r"[\s\-]+".join(re.escape(w) for w in c.split())
                                + r"(?:\s+County\b|(?=(?:\s*,\s*|\s+and\s+|\s+or\s+)[A-Z][\w\s,\-]{0,60}?\bcounties\b))", re.I)) for c in counties if c]
    # A district named after one township needs "School District" after it to count as a district;
    # a joint one (Tredyffrin-Easttown) is a district wherever it appears, suffix or not.
    district_rx = [(d, re.compile(r"\b" + r"[\s\-]+".join(re.escape(w) for w in d.split())
                                  + (r"(\s+(area|township))?\s+(school district|sd)\b" if d in munis else r"\b(\s+(area\s+)?(school district|sd)\b)?"), re.I),
                    # never "Lower Merion Township" - a township named next to its district stays a township claim
                    re.compile(r"\b" + r"[\s\-]+".join(re.escape(w) for w in d.split()) + r"\b(?!\s+(?:township|borough|twp)\b)(\s+(area\s+)?(school district|sd)\b)?", re.I))
                   for d in districts_all if d]
    findings, seen = [], set()
    for sentence in _SENT.split(text):
        # In a sentence about school districts, a verified district's short name IS that district
        # (11/01 round 13 Wilmington: "districts ... include Red Clay Consolidated, Brandywine and
        # Christina" was cut as putting Wilmington in a place called Brandywine).
        school_ctx = bool(re.search(r"\b(?:school\s+)?districts?\b|\bschools?\b", sentence, re.I))
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
            for d, strict, loose in district_rx:
                take(loose if school_ctx else strict, "school district", d)
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
                    # 11/01 Frederick: "Unincorporated Frederick County" - the MLS's generic
                    # "UNINCORPORATED" matched on its own once "Frederick County" was taken as the county.
                    if any(a.startswith(place + " ") for a in allowed):
                        continue
                    truth = "; ".join(t["name"] for t in p["townships"]) or "unknown"
                    key = (sentence.strip(), p["town"], place)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({"sentence": sentence.strip(), "town": p["town"], "place": place.title(), "kind": kind,
                                     "truth": f"{p['town']} homes sit in {truth}" + (f"; districts: {', '.join(d['name'] for d in p['school_districts'])}" if p["school_districts"] else "")})
    return findings


# One public school system per county (or the whole District; in Virginia per county or independent city -
# 11/01 round-11 grader: Ashburn is all Loudoun County Public Schools) - a town inside one county has one.
_COUNTY_WIDE_SCHOOLS = {"MD", "DC", "WV", "VA"}

# Maryland's courts and land records share one statewide site with a page per county
# (courts.state.md.us/clerks/frederick/...) - the county is only in the address.
_MD_COUNTIES = {"allegany", "anne arundel", "baltimore", "calvert", "caroline", "carroll", "cecil", "charles", "dorchester",
                "frederick", "garrett", "harford", "howard", "kent", "montgomery", "prince georges", "queen annes", "somerset",
                "st marys", "talbot", "washington", "wicomico", "worcester"}


def _counties_named(it: dict) -> set[str]:
    """Counties a fact or source names - in its text ("Frederick County"), or only in its web
    address ("frederickcountymd.gov", ".../clerks/frederick/...")."""
    text = f"{it.get('claim') or ''} {it.get('source_name') or it.get('name') or ''}"
    named = {norm_place(m) for m in re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z']+)?)\s+County\b", text)}
    url = str(it.get("source_url") or it.get("url") or "").lower()
    if url:
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        # Skips initials ("aacounty.org" is Anne Arundel's) - they can't be matched to a name.
        named |= {norm_place(m) for m in re.findall(r"([a-z]{4,})county", host)}
        m = re.search(r"/clerks?/([a-z\-]+)/", url)
        if m and norm_place(m.group(1)) in _MD_COUNTIES:
            named.add(norm_place(m.group(1)))
    return {n for n in named if n}


def _munis_named(it: dict) -> set[str]:
    """Townships and boroughs a fact or source names - in its text ("Ephrata Township") or its web
    address ("ephratatownship.org")."""
    text = f"{it.get('claim') or ''} {it.get('source_name') or it.get('name') or ''}"
    named = {norm_place(m) for m in re.findall(r"\b((?:[A-Z][a-z]+\s){0,2}[A-Z][a-z]+)\s+(?:Township|Borough)\b", text)}
    # "City of Alexandria" / "Town of Leesburg" (11/01 round 11: an Alexandria recordation-tax page in
    # an Ashburn article - a Virginia city is its own jurisdiction, not part of Loudoun County).
    named |= {norm_place(m) for m in re.findall(r"\b(?:City|Town|Village|Borough|Township) of ((?:[A-Z][a-z]+\s)?[A-Z][a-z]+)\b", text)}
    url = str(it.get("source_url") or it.get("url") or "").lower()
    if url:
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        named |= {norm_place(m) for m in re.findall(r"([a-z]{4,})(?:township|borough|twp|boro)\b", host)}
        # A city's own site with its state code: "alexandriava.gov" (never federal "consumerfinance.gov").
        m = re.fullmatch(r"([a-z]{4,}?)(?:va|md|pa|nj|de|dc)\.gov", host)
        if m and not m.group(1).endswith(("county", "state")):
            named.add(norm_place(m.group(1)))
    return {n for n in named if n}


def place_protections(profiles: list[dict]) -> list[dict]:
    """Every local government holding 1%+ of a town's homes, as a must-name item (checked like the
    reader protections - writer list, fact-check, repair, built-in grader). 11/01 round-13 grader:
    a Baltimore article framed it as "Baltimore City or Baltimore County" while 1% of Baltimore
    addresses are in Anne Arundel County. Only for towns with two or more such places; the
    geography item already says smaller verified places exist ("other incorporated towns ...")."""
    out = []
    for p in profiles:
        big = [t for t in p.get("townships") or [] if t.get("kind") not in (None, "small") and not t.get("remainder")
               and isinstance(t.get("sort", t.get("share")), (int, float)) and t.get("sort", t.get("share")) >= 1]
        if len(big) < 2:
            continue
        town, town_n = p["town"], norm_place(p["town"])
        for t in big:
            name = t["name"]
            core = re.sub(r"^unincorporated\s+|\s+(county|city|town|village|borough|township)$", "", name, flags=re.I).strip()
            words = r"[\s\-]+".join(re.escape(w) for w in core.split())
            kind = re.search(r"(county|city|town|village|borough|township)$", name, re.I)
            if norm_place(core) == town_n and kind:
                # The mailing town's own name is everywhere - the article must name the government itself.
                k = kind.group(1)
                check = rf"\b{words}\s+{k}\b|\b{k}\s+of\s+{words}\b"
            else:
                check = rf"\b{words}\b"
            label = f"unincorporated {core} County" if name.lower().startswith("unincorporated") else name
            share = t.get("share")
            out.append({"state": "", "readers": set(), "key": f"place_{norm_place(name).replace(' ', '_')}", "check": check,
                        "name": label, "source_name": "U.S. Census Bureau boundaries (OpenHousePal map check of each home)", "source_url": None,
                        "says": f"A {town} mailing address can be in {label} - about {share}% of {town}-addressed homes in our Bright MLS records; "
                                "confirm the jurisdiction, school district and tax bill for any specific home."})
    return out


def drop_other_county_items(items: list[dict], profiles: list[dict]) -> list[dict]:
    """Facts and sources about a county none of the article's towns are in - dropped. 11/01: a
    Frederick County recording-fee page reached a Baltimore article through research, then an
    Annapolis one through the saved Maryland facts - so this runs on every item and every source,
    and reads the web address too. An item naming one of the article's counties, or none, stays.
    The same for townships and boroughs (11/01 round-10 grader: an Ephrata Township tax page in a
    Lancaster article's sources - same county, but no Lancaster-addressed homes there)."""
    ours = {norm_place(c) for p in profiles for c in p.get("counties") or []} | {norm_place(p.get("town")) for p in profiles}
    ours = {o.replace(" ", "") for o in ours if o}
    if not ours:
        return items
    kept = [it for it in items if not (named := {n.replace(" ", "") for n in _counties_named(it)}) or named & ours]
    # Allowed: the towns' own municipalities, the towns, and their counties (a county's own site).
    munis = {m.replace(" ", "") for p in profiles for m in (p.get("norm") or {}).get("townships") or set()} | ours
    return [it for it in kept if not (named := {n.replace(" ", "") for n in _munis_named(it)}) or named & munis]


_YEAR_SPAN = re.compile(r"(?<!\d)(20\d\d)\s*[-–_/]\s*(20\d\d|\d\d)(?!\d)")


def stale_edition(it: dict, today: Optional[date] = None) -> bool:
    """A yearly document (school or fiscal year, "2025-2026" / "2025-26" in its title or web
    address) whose year has already ended - last year's edition. 11/01 round-10 grader: a Lancaster
    article linked the 2025-2026 school millage table after the 2026-2027 one was out."""
    today = today or datetime.now(timezone.utc).date()
    current_end = today.year + 1 if today.month >= 7 else today.year  # school / fiscal years start July 1
    for m in _YEAR_SPAN.finditer(f"{it.get('source_name') or it.get('name') or ''} {it.get('source_url') or it.get('url') or ''}"):
        start, end = int(m.group(1)), m.group(2)
        end = int(end) if len(end) == 4 else (start // 100) * 100 + int(end)
        if end == start + 1 and end < current_end:
            return True
    return False


def drop_stale_editions(items: list[dict], today: Optional[date] = None) -> list[dict]:
    """Facts and sources from last year's edition of a yearly document - dropped, so an article
    never cites an outdated table (research is told to find the newest edition)."""
    return [it for it in items if it.get("origin") in ("mls_live", "geo_verified", "calc", "agent") or not stale_edition(it, today)]


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
    return [{**i, "origin": "state_bank", "town": "general"} for i in _fresh(row.items if row else [], datetime.now(timezone.utc))
            if not _counties_named(i)]


async def save_state_items(db: AsyncSession, state: str, items: list[dict]) -> int:
    """Add sourced state-wide facts to the shared store (deduped, capped, expired pruned)."""
    from app.models.database import SharedKnowledge
    items = [i for i in items if i.get("source_url") and i.get("claim") and i.get("topic") != "rates" and "freddie mac" not in i["claim"].lower()]
    # A county's own fact (its fees, its office) isn't state-wide - banked here it reached every
    # later article in the state (11/01: Frederick County's recording fees in an Annapolis one).
    items = [i for i in items if not _counties_named(i)]
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
    # A remainder row (not measured) is "other parts of X County" - never "Unincorporated X County".
    label = lambda t: f"other parts of {re.sub(r'^unincorporated\s+', '', t['name'], flags=re.I)}" if t.get("remainder") else t["name"]
    where = ", ".join(label(t) for t in tw) if len(tw) > 1 else ""
    return f"{area.name} numbers cover homes with {_a(area.name)} {area.name} mailing address" + (f", which spans {where}" if where else "") + f"; {_HOME_TYPES_NOTE}."


def sales_snapshot_items(area: MarketArea, snap: dict, geo: Optional[dict] = None) -> list[dict]:
    """The dated Bright MLS snapshot as evidence items - each says what it covers, "for sale" is
    active listings only (under contract counted on its own), year-ago figures only when our MLS
    history for that window is complete, and small samples say so. Breakdowns must fit inside their
    total (parts_fit) or they're left out."""
    from app.services.areas.market_stats_service import parts_fit
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
    if cur.get("by_type") and parts_fit(cur.get("closed_count"), [v["count"] for v in cur["by_type"].values()]):
        names = {"condo": "condos", "townhouse": "townhomes", "house": "single-family houses"}
        add(f"{area.name}: closed sales by type in {win}: " + "; ".join(
            f"{names[k]} {_fmt_money(v['median_close_price'])} median ({v['count']} sales)" for k, v in cur["by_type"].items()) + ".")
    if cur.get("median_sale_to_list_pct"):
        va = cur.get("vs_asking") or {}
        split = (f"; of the {va['of']} sales with a list price, {va['above']} sold above asking, {va['at']} at exactly asking and {va['below']} below"
                 if va.get("of", 0) >= 10 and parts_fit(va["of"], [va["above"], va["at"], va["below"]]) else "")
        add(f"{area.name}: median sale-to-list price ratio {cur['median_sale_to_list_pct']}% in {win} (the midpoint of those sales, as a share of asking price){split}"
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


def cost_items(area: MarketArea, cost: dict, geo: Optional[dict] = None) -> list[dict]:
    """Owning costs read off the listings (taxes per township, HOA fees) and how recent buyers
    paid - local, dated, sourced numbers a first-time-buyer article can lean on. Every breakdown
    must fit inside its total (parts_fit) or it's left out."""
    from app.services.areas.market_stats_service import parts_fit
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
            + ". A buyer's bill depends on the home's assessment, not its price.")
        # No tax-as-a-percent-of-price figures, overall or per municipality: readers take them for
        # official tax rates (11/01 grader, Princeton "1.52% in Hopewell Township"). The comparison
        # is said in words; the per-municipality split stays internal.
        if len(tax.get("by_township") or []) > 1:
            add(f"{area.name}: property taxes vary across the municipalities here - tax rates are set locally (municipality, county and school district), "
                "so two similarly priced homes can have substantially different tax bills. Check the actual tax bill and assessment for any home before making an offer.")
    tf = cost.get("townhome_fees") or {}
    for kind, label in (("condo", "condos"), ("townhouse", "townhomes")):
        h = (cost.get("hoa") or {}).get(kind)
        if not h or (h.get("of") and not parts_fit(h["of"], [h["homes"]])):
            continue
        covers = (" Most of those fees include: " + ", ".join(f"{_FEE_WORDS.get(c.lower(), c.lower())} ({pct}%)" for c, pct in h["covers"]) + "."
                  if h.get("covers") else "")
        reported = (tf.get("condo_fee") or 0) + (tf.get("hoa_only") or 0)
        if kind == "townhouse" and tf and reported and parts_fit(tf["of"], [tf["condo_fee"], tf["hoa_only"], tf["no_fee"]]) and parts_fit(reported, [h["homes"]]):
            # Round 13 grader (Baltimore): "159 HOA + 44 condo fee" is 203, but "201 show a fee" - the 201
            # are the ones with a usable dollar amount. Every subset says which set it's from.
            add(f"{area.name}: of the {tf['of']} townhomes for sale, {reported} report a condo or association fee, and {h['homes']} of those {reported} "
                f"give a usable dollar amount; those {h['homes']} show a median of ${h['median_monthly']:,}/month, all fees combined." + covers, "market")
            continue
        add(f"{area.name}: {label} for sale that list a monthly condo or association fee show a median of ${h['median_monthly']:,}/month, all fees combined "
            + (f"({h['homes']} of the {h['of']} {label} for sale show a fee)." if h.get("of") else f"({h['homes']} listings).") + covers, "market")
    if tf and parts_fit(tf["of"], [tf["condo_fee"], tf["hoa_only"], tf["no_fee"]]):
        # 11/01 grader: "with a condo or townhome, the inspection only covers part of what you're
        # buying" - true of a condo unit, not of a townhome owned with its land.
        add(f"{area.name}: \"townhome\" is a building style, not a kind of ownership. Of the {tf['of']} townhomes for sale, {tf['condo_fee']} list a "
            f"condominium fee (often a sign the townhome is owned as a condo unit), {tf['hoa_only']} list only an HOA fee and {tf['no_fee']} list no fee. "
            # Round 7 grader: ownership type matters, but the association's governing documents decide
            # who maintains what (Maryland's condo law lets declarations and bylaws set it).
            "Whether a townhome is owned as a condo unit or fee simple (with its land) matters, but the association's governing documents "
            "decide which maintenance belongs to the owner and which to the association - the listing, the association documents and the title show which.", "market")
    mix = cost.get("mix") or {}
    if mix and parts_fit(mix["homes"], [mix["attached"]["count"], mix["house"]["count"]]):
        def group(g, label):
            return f"{g['count']} {label}" + (f" (median list price {_fmt_money(g['median'])})" if g.get("median") else "")
        add(f"{area.name}: of its {mix['homes']} homes for sale on {on}, " + group(mix["attached"], "are condos or townhomes")
            + " and " + group(mix["house"], "are single-family houses") + ".", "market")
        low = mix["low_quarter"]
        if low["count"] and parts_fit(low["count"], [low["attached"]]):
            add(f"{area.name}: in the lowest-priced quarter of its homes for sale on {on}, {low['attached']} of {low['count']} are condos or townhomes"
                + (" - so that quarter price mostly reflects condos and townhomes, not houses." if low["attached"] * 2 > low["count"] else "."), "market")
        def some(g, what):
            n = g["count"]
            head = f"none of its {mix['homes']} homes for sale on {on} is" if not n else f"{n} of its {mix['homes']} homes for sale on {on} {'is' if n == 1 else 'are'}"
            return f"{head} {what}" + (f" (median list price {_fmt_money(g['median'])})" if g.get("median") else "")
        add(f"{area.name}: {some(mix['one_level'], 'one-level (one-floor living: a one-level house or condo unit)')}, as entered on the listings.", "market")
        if mix.get("senior"):
            add(f"{area.name}: {some(mix['senior'], 'in a 55+ (age-restricted) community')}, as entered on the listings.", "market")
        if (mix.get("manufactured") or {}).get("count"):
            add(f"{area.name}: {some(mix['manufactured'], 'a manufactured home')} - counted apart from the one-level homes. A manufactured home may sit "
                "on land the buyer owns or on leased land with a monthly lot rent, and financing differs; each listing says which.", "market")
    fin = cost.get("financing") or {}
    if fin.get("mix") and parts_fit(fin["closed_sales"], [fin["recorded"]]):
        mix = ", ".join(f"{m['type']} {m['pct']}%" for m in fin["mix"][:5])
        # The window our records fully cover (11/01: "since Nov 1, 2025" was really two months).
        span = f"in the {fin['days']} days ending {fin['through']}" if fin.get("days") and fin.get("through") else f"since {fin['since']}"
        add(f"{area.name}: how buyers paid, from the {fin['recorded']} of {fin['closed_sales']} closed sales {span} that recorded it in Bright MLS: {mix}.", "market")
    return out


def overlap_items(pairs: list[tuple[MarketArea, MarketArea, int, int]]) -> list[dict]:
    """(a, b, shared homes for sale, a's homes for sale) -> 'don't add these together' notes."""
    out = []
    for a, b, shared, total in pairs:
        if shared <= 0:
            continue
        claim = (f"Overlap: {shared} of {a.name}'s {total} homes for sale are also inside {b.name}'s numbers ({_a(a.name)} {a.name} mailing address within "
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
    # Round 7 grader: "the lender's appraisal needs to support the price" was too absolute - a financed
    # purchase can still close after a low appraisal.
    ("regulation", "A lender may require an appraisal (an independent opinion of the home's value) to help decide how much it will lend. "
     "If the appraisal comes in below the purchase price, the buyer may be able to negotiate the price with the seller, bring additional cash, "
     "or review options with the lender; depending on the contract, the buyer may be able to cancel - an attorney can explain the options. "
     "The lender must give the buyer a copy of the appraisal.",
     "CFPB: My appraisal is less than the sale price. What does that mean for me?",
     "https://www.consumerfinance.gov/ask-cfpb/my-appraisal-came-in-lower-than-i-expected-en-2007/", "2026-11", None),
]


def conforming_limit(today=None) -> Optional[int]:
    """The one-unit baseline conforming loan limit while the item above is current."""
    return 832_750 if (today or datetime.now(timezone.utc).date()).isoformat() <= "2026-12-31" else None


_CLL_TABLES: dict[int, Optional[dict]] = {}


def _cll_table(year: int) -> Optional[dict]:
    """FHFA's county one-unit limits for `year` (app/data/fhfa_conforming_limits_<year>.json), indexed
    by state + normalized county name -> the set of limits under that name (Virginia's Fairfax County
    and Fairfax City both read "fairfax")."""
    if year not in _CLL_TABLES:
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent.parent / "data" / f"fhfa_conforming_limits_{year}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            idx: dict[tuple[str, str], set[int]] = {}
            for key, limit in raw["one_unit"].items():
                st, name = key.split("|", 1)
                idx.setdefault((st, norm_place(name)), set()).add(int(limit))
            _CLL_TABLES[year] = {"index": idx, "source_name": raw["source_name"], "source_url": raw["source_url"], "published": raw["published"]}
        except (OSError, ValueError, KeyError):
            _CLL_TABLES[year] = None
    return _CLL_TABLES[year]


def county_conforming_limit(state: Optional[str], counties: list[str], today=None) -> Optional[dict]:
    """The one-unit conforming loan limit that applies to every home in these counties, from FHFA's
    county list for the current year: {"limit", "counties", "source_name", "source_url", "published"}.
    None when this year's list isn't on file, a county isn't found, or the counties' limits differ -
    then whether a loan is jumbo depends on the property's county (11/01 round-11 grader: Ashburn's
    $1.34M example was called jumbo even at 20% down, against the national $832,750, while Loudoun
    County's 2026 limit is $1,249,125)."""
    table = _cll_table((today or datetime.now(timezone.utc).date()).year)
    names = [c for c in counties if c]
    if not table or not state or not names:
        return None
    limits = set()
    for c in names:
        # A Virginia/Maryland independent city may come labeled without "City" (Bright's county field).
        n = norm_place(c)
        found = table["index"].get((state.upper(), n)) or table["index"].get((state.upper(), f"{n} city"))
        if not found or len(found) > 1:
            return None
        limits |= found
    if len(limits) != 1:
        return None
    return {"limit": limits.pop(), "counties": names, "source_name": table["source_name"], "source_url": table["source_url"], "published": table["published"]}


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
        # Too little MLS coverage (Charlottesville - CAAR's listings left Bright 7/15/2026): the article
        # gets no MLS numbers, the same rule the town page uses (market_stats_service.coverage_verdict).
        from app.services.areas.market_stats_service import get_area_market_coverage, town_households
        try:
            mls = not (await get_area_market_coverage(db, scope, town_households(area))).get("thin")
        except Exception:
            logger.warning("town dossier: coverage check failed for %s", area.slug, exc_info=True)
    if mls:
        try:
            snap = await get_area_sales_snapshot(db, scope)
        except Exception:
            logger.warning("town dossier: sales snapshot failed for %s", area.slug, exc_info=True)
            snap = {}
        items += sales_snapshot_items(area, snap, geo)
        try:
            # The town's census outlines (town map, TIGER) place each listing by its map location.
            munis = (geo or {}).get("muni_shapes") or [m for m in (((area.facts or {}).get("boundaries") or {}).get("municipalities") or []) if m.get("rings")]
            cost = await get_area_cost_profile(db, scope, munis=munis)
        except Exception:
            logger.warning("town dossier: cost profile failed for %s", area.slug, exc_info=True)
            cost = {}
        items += cost_items(area, cost, geo)
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


def _a(word: str) -> str:
    """'an Arlington', 'a Wayne'."""
    return "an" if (word or "").strip()[:1].lower() in "aeiou" else "a"


# Bright's AssociationFeeIncludes shorthand, in a reader's words.
_FEE_WORDS = {"ext bldg maint": "exterior building maintenance", "common area maintenance": "upkeep of shared areas",
              "lawn maintenance": "lawn care", "hvac maint": "heating and cooling upkeep", "custodial services": "cleaning of shared areas",
              "reserve funds": "savings for future repairs (reserve funds)", "insurance": "the building's master insurance",
              "pool(s)": "pool", "health club": "fitness room"}
