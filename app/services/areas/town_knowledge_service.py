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


def sales_snapshot_items(area: MarketArea, snap: dict) -> list[dict]:
    """The dated Bright MLS snapshot as evidence items."""
    if not snap or not snap.get("current"):
        return []
    town = _town(area)
    cur, prior = snap["current"], snap.get("prior_year") or {}
    through = snap.get("data_through")
    win = f"the {snap.get('window_days', 90)} days ending {through}"
    out = []

    def add(claim):
        out.append({"topic": "market", "claim": claim, "source_name": "Bright MLS (OpenHousePal data)", "source_url": None,
                    "as_of": through, "confidence": 0.97, "town": town, "origin": "mls_live", "id": _iid(town, claim)})

    if cur.get("closed_count") is not None:
        add(f"{area.name}: {cur['closed_count']} closed sales in {win}" + (f" (vs {prior['closed_count']} in the same window a year earlier)." if prior.get("closed_count") is not None else "."))
    if cur.get("median_close_price"):
        add(f"{area.name}: median closed price {_fmt_money(cur['median_close_price'])} in {win}" + (f" (vs {_fmt_money(prior['median_close_price'])} a year earlier)." if prior.get("median_close_price") else "."))
    if cur.get("median_sale_to_list_pct"):
        add(f"{area.name}: median sale-to-list price ratio {cur['median_sale_to_list_pct']}% in {win}" + (f" (vs {prior['median_sale_to_list_pct']}% a year earlier)." if prior.get("median_sale_to_list_pct") else "."))
    if cur.get("median_days_on_market") is not None:
        add(f"{area.name}: median {cur['median_days_on_market']} days on market for homes that closed in {win}" + (f" (vs {prior['median_days_on_market']} a year earlier)." if prior.get("median_days_on_market") is not None else "."))
    if snap.get("active_count") is not None:
        add(f"{area.name}: {snap['active_count']} homes actively for sale as of {datetime.now(timezone.utc).date().isoformat()}"
            + (f", median list price {_fmt_money(snap['active_median_list_price'])}." if snap.get("active_median_list_price") else "."))
    return out


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


async def town_dossier(db: AsyncSession, area: MarketArea, *, include_bank: bool = True) -> dict:
    """Everything we already know about one town, as evidence items + a coverage report:
    {"town", "slug", "items": [...], "covered": {topic: count}, "gaps": [topic], "stale": [topic]}."""
    from app.services.areas import geography
    from app.services.areas.market_stats_service import get_area_sales_snapshot

    items = research_items(area)
    try:
        snap = await get_area_sales_snapshot(db, geography.scope_for_market_area(area))
    except Exception:
        logger.warning("town dossier: sales snapshot failed for %s", area.slug, exc_info=True)
        snap = {}
    items += sales_snapshot_items(area, snap)
    if include_bank:
        items += bank_items(area)

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
            "sales_snapshot": snap, "researched_at": _date(area.last_enriched_at)}
