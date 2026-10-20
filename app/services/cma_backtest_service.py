"""Admin CMA accuracy test ("backtest").

Takes homes that really sold, sets the clock back to the day before each went under contract, and
runs the SAME flow an agent uses - address lookup -> the wizard's comp search with its own
auto-widen + auto-pick rules -> the full AI generation with photos -> QA - then scores the value
against the real sale price. The only differences from an agent run are the time machine (see
cma_time_machine.py) and that reports are flagged is_backtest (never listed, never shareable).

Queue: one batch at a time, one home at a time, in this process. A cost cap (default $20) is
checked before a batch starts (estimate) and before every home (actual spend so far). DRY RUN mode
swaps both AI providers for zero-cost stubs (cma_backtest_stubs) to prove the plumbing + no-leak
guarantee without spending anything.
"""
from __future__ import annotations

import asyncio

import statistics
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Float, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.database import AIUsageEvent, CmaBacktestBatch, CmaReport, Property, PropertyPriceEvent, User
from app.schemas.cma import CmaCompSearchRequest, CmaComparable, CmaSubjectProperty
from app.services import cma_ai_service, cma_service, cma_time_machine
from app.services.cma_backtest_stubs import StubAnthropic, stub_reasoning
from app.services.cma_report_audit import audit_report, client_text_sections
from app.services.property_service import PropertyService

logger = get_logger(__name__)

PROPERTY_TYPES = {
    "single_family": ("Single family", ["SINGLE_FAMILY", "FARM"]),
    "townhouse": ("Townhouse / twin", ["TOWNHOUSE"]),
    "condo": ("Condo", ["CONDO"]),
}
PRICE_BANDS = ((0, 600_000, "Under $600K"), (600_000, 1_000_000, "$600K-$1M"), (1_000_000, 2_000_000, "$1M-$2M"),
               (2_000_000, 10**10, "$2M+"))
DEFAULT_COST_CAP_USD = 20.0
MAX_HOMES_PER_BATCH = 50
FALLBACK_COST_PER_HOME_USD = 0.65
SALES_WINDOW_DAYS = 365

# Studio CMA wizard (frontend app/studio/cma/[id]/page.tsx CompsStep) - kept identical.
_SOLD_DAYS_STEPS = [100, 200, 300, 400, 500, 600, 700, 750]
_RADIUS_STEPS = [1, 2, 3, 5, 8, 12, 18, 25]
_MIN_STRONG_MATCHES = 5
_MAX_COMPS = 5
_AUTO_PICK_EXTRA_MIN_SCORE = 60


def price_band(price: Optional[float]) -> str:
    for lo, hi, label in PRICE_BANDS:
        if price is not None and lo <= price < hi:
            return label
    return "Unknown"


def _d(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


# ---- sampling -----------------------------------------------------------------------------------

async def _median_contract_to_close_days(db: AsyncSession) -> int:
    r = await db.execute(text(
        "select percentile_cont(0.5) within group (order by ((raw_mls_data->>'CloseDate')::date - "
        "(raw_mls_data->>'PurchaseContractDate')::date)) from properties where home_status ilike 'closed%' "
        "and raw_mls_data->>'PurchaseContractDate' is not null and raw_mls_data->>'CloseDate' >= :since"),
        {"since": (date.today() - timedelta(days=SALES_WINDOW_DAYS)).isoformat()})
    v = r.scalar()
    return int(round(v)) if v else 35


async def price_events(db: AsyncSession, keys: list[str]) -> dict:
    if not keys:
        return {}
    rows = (await db.execute(select(PropertyPriceEvent).where(PropertyPriceEvent.listing_key.in_(keys))
                             .order_by(PropertyPriceEvent.observed_at))).scalars().all()
    out: dict = {}
    for e in rows:
        out.setdefault(e.listing_key, []).append(e)
    return out


def as_of_facts(prop: Property, median_lag: int, events: Optional[list] = None) -> Optional[dict]:
    """The day the test pretends it is (the day before the home went under contract) and what an
    agent would have seen that day. Returns None when the record can't support a fair test.
    `events` = this listing's price/status history (property_price_events), oldest first."""
    raw = prop.raw_mls_data or {}
    close_price = cma_ai_service._as_float(raw.get("ClosePrice"))
    close_date = _d(raw.get("CloseDate"))
    list_price = cma_ai_service._as_float(raw.get("ListPrice")) or cma_ai_service._as_float(prop.price)
    if not (close_price and close_date and list_price) or close_date > date.today():
        return None   # incomplete, or an impossible future closing date
    contract = _d(raw.get("PurchaseContractDate"))
    pending_event = next((e for e in (events or []) if e.kind == "status" and e.status and any(
        w in str(e.status).upper() for w in ("PENDING", "CONTRACT"))), None)
    if contract and contract <= close_date:
        as_of, source = contract - timedelta(days=1), "the day before its MLS contract date (PurchaseContractDate)"
    elif pending_event and pending_event.observed_at.date() <= close_date:
        as_of = pending_event.observed_at.date() - timedelta(days=1)
        source = "the day before the price history first shows it pending / under contract"
    else:
        as_of = close_date - timedelta(days=median_lag + 1)
        source = f"closing date minus {median_lag} days (local median contract-to-close) minus 1 day - no contract date on record"
    list_date = _d(raw.get("ListingContractDate")) or _d(raw.get("OnMarketDate")) or _d(raw.get("MLSListDate")) \
        or (prop.mls_list_date.date() if getattr(prop, "mls_list_date", None) else None)
    if list_date and list_date >= as_of:
        as_of = list_date  # it went under contract on its first day - test it as a brand-new listing
        source += " (moved to its list date - it went under contract right away)"
    # Days on market that morning. Bright's DaysOnMarket on a closed listing stopped counting when it
    # went under contract, so count back from the contract date; otherwise list date -> as-of.
    raw_dom = cma_ai_service._as_float(raw.get("DaysOnMarket"))
    if contract and raw_dom is not None and as_of <= contract:
        dom = max(0, int(raw_dom) - (contract - as_of).days)
    elif list_date:
        dom = (as_of - list_date).days
    else:
        dom = raw_dom
    # The asking price ON the as-of date, from the price history when it has one (a later cut or
    # raise must not show); otherwise the final list price, which is what it was listed at when it
    # went under contract.
    asking = [e for e in (events or []) if e.kind in ("listed", "price") and e.price and e.observed_at.date() <= as_of]
    if asking:
        list_price = float(asking[-1].price)
        source += "; asking price from the price history on that date"
    return {
        "as_of": as_of.isoformat(), "as_of_source": source, "list_price": list_price, "dom_as_of": int(dom) if dom is not None else None,
        "list_date": list_date.isoformat() if list_date else None,
        "close_price": close_price, "close_date": close_date.isoformat(), "contract_date": contract.isoformat() if contract else None,
    }


async def sample_homes(db: AsyncSession, *, towns: list[str], random_spread: bool, price_min: Optional[float],
                       price_max: Optional[float], property_type: str, count: int) -> list[dict]:
    types = PROPERTY_TYPES.get(property_type, PROPERTY_TYPES["single_family"])[1]
    since = (date.today() - timedelta(days=SALES_WINDOW_DAYS)).isoformat()
    close_price = func.nullif(Property.raw_mls_data["ClosePrice"].astext, "").cast(Float)
    q = select(Property).where(
        Property.is_manual.is_(False),
        func.upper(Property.home_status).like("CLOSED%"),
        Property.home_type.in_(types),
        Property.raw_mls_data["CloseDate"].astext >= since,
        Property.raw_mls_data["CloseDate"].astext <= date.today().isoformat(),
        close_price > 0,
        Property.living_area > 0,
        Property.latitude.isnot(None), Property.longitude.isnot(None),
        Property.price > 0,
    )
    if price_min:
        q = q.where(close_price >= price_min)
    if price_max:
        q = q.where(close_price <= price_max)
    if towns and not random_spread:
        # "Town, ST" matches that state only; a bare "Town" still matches it in any state.
        conds = []
        for t in (t.strip() for t in towns if t and t.strip()):
            town, _, st = t.rpartition(",") if "," in t else (t, "", "")
            c = func.upper(Property.city) == town.strip().upper()
            conds.append(and_(c, func.upper(Property.state) == st.strip().upper()) if st.strip() else c)
        q = q.where(or_(*conds))
    rows = (await db.execute(q.order_by(func.random()).limit(max(60, count * 12)))).scalars().all()
    lag = await _median_contract_to_close_days(db)
    events = await price_events(db, [p.listing_key for p in rows])
    picked, seen_addr = [], set()
    if random_spread:
        by_town: dict[str, list] = {}
        for p in rows:
            by_town.setdefault((p.city or "").upper(), []).append(p)
        order = []
        while any(by_town.values()) and len(order) < len(rows):
            for t in list(by_town):
                if by_town[t]:
                    order.append(by_town[t].pop())
        rows = order
    for p in rows:
        if len(picked) >= count:
            break
        key = ((p.street_address or "").upper(), (p.zipcode or "")[:5])
        if key in seen_addr:
            continue
        facts = as_of_facts(p, lag, events.get(p.listing_key))
        if not facts:
            continue
        seen_addr.add(key)
        picked.append({
            "listing_key": p.listing_key, "address": p.street_address, "city": p.city, "state": p.state,
            "zipcode": p.zipcode, "property_type": p.home_type, "sqft": p.living_area, "year_built": p.year_built,
            "segment": {"town": (p.city or "").title(), "price_band": price_band(facts["close_price"]),
                        "type": next((lbl for k, (lbl, ts) in PROPERTY_TYPES.items() if p.home_type in ts), p.home_type)},
            **facts, "status": "queued",
        })
    return picked


async def town_options(db: AsyncSession) -> list[dict]:
    since = (date.today() - timedelta(days=SALES_WINDOW_DAYS)).isoformat()
    r = await db.execute(text(
        "select initcap(lower(city)) c, upper(state) st, count(*) n from properties where home_status ilike 'closed%' "
        "and raw_mls_data->>'CloseDate' >= :since and nullif(raw_mls_data->>'ClosePrice','')::float > 0 "
        "and living_area > 0 and latitude is not null and is_manual is false "
        "group by 1, 2 having count(*) >= 5 order by 3 desc limit 400"), {"since": since})
    # "Springfield, PA" - Bright covers several states and town names repeat (accuracy test 10/19
    # picked Springfield, VA for a Delaware County test).
    return [{"town": f"{c}, {st}" if st else c, "closed_sales": n} for c, st, n in r.all() if c]


async def cost_per_home_estimate(db: AsyncSession) -> dict:
    """Real recent average spend per CMA report (comp-analysis with photos + final valuation +
    follow-up passes), from the AI usage log. +15% margin for the cap check."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    r = await db.execute(select(func.sum(AIUsageEvent.estimated_cost_usd)).where(
        AIUsageEvent.event_type.in_(["cma_ai", "cma_reasoning"]), AIUsageEvent.created_at >= since,
        AIUsageEvent.blocked.is_(False)))
    total = float(r.scalar() or 0)
    n = (await db.execute(select(func.count()).where(
        AIUsageEvent.event_type == "cma_reasoning", AIUsageEvent.created_at >= since,
        AIUsageEvent.blocked.is_(False), AIUsageEvent.estimated_cost_usd > 0))).scalar() or 0
    avg = round(total / n, 3) if n >= 3 else FALLBACK_COST_PER_HOME_USD
    return {"average_per_report_usd": avg, "per_home_with_margin_usd": round(avg * 1.15, 3), "based_on_reports": int(n)}


# ---- the agent flow, as of a past date ----------------------------------------------------------

def _readable_status(raw) -> Optional[str]:
    s = str(raw or "").upper().replace("-BRIGHT", "").replace("_", " ").strip()
    return s.title() if s else None


def map_property_to_subject(p: dict) -> dict:
    """Port of the Studio wizard's mapPropertyToSubject (RESO lookup payload -> CMA subject)."""
    photos = p.get("photos") if isinstance(p.get("photos"), list) and p.get("photos") else ([p["ListPictureURL"]] if p.get("ListPictureURL") else [])
    b = p.get("Basement")
    if isinstance(b, list) and b:
        basement = ", ".join(b)
    elif p.get("BasementYN") is True:
        basement = "Yes"
    elif p.get("BasementYN") is False:
        basement = "None"
    elif p.get("BelowGradeFinishedArea"):
        basement = "Finished"
    else:
        basement = "None"
    garage = (f"{p['GarageSpaces']} Car" if p.get("GarageSpaces") else "Yes") if p.get("GarageYN") else ("None" if p.get("GarageYN") is False else None)
    return {
        "listing_key": p.get("ListingKey"), "source": "mls",
        "photo_url": p.get("ListPictureURL") or (photos[0] if photos else None), "photos": photos,
        "street_address": p.get("FullStreetAddress"), "city": p.get("City"), "state": p.get("StateOrProvince"),
        "zipcode": p.get("PostalCode"), "latitude": p.get("Latitude"), "longitude": p.get("Longitude"),
        "list_price": p.get("ListPrice"), "beds": p.get("BedroomsTotal"), "baths": p.get("BathroomsTotal"),
        "sqft": p.get("LivingArea"), "above_grade_finished_area": p.get("AboveGradeFinishedArea"),
        "below_grade_finished_area": p.get("BelowGradeFinishedArea"), "lot_size": p.get("LotSizeSquareFeet"),
        "year_built": p.get("YearBuilt"), "property_type": p.get("PropertyType"),
        "architectural_style": p.get("ArchitecturalStyle"), "township": p.get("Township"),
        "subdivision_name": p.get("SubdivisionName"), "garage": garage, "basement": basement,
        "days_on_market": p.get("DaysOnMarket"), "mls_status": _readable_status(p.get("MlsStatus")),
        "last_sale_date": None, "last_sale_price": None, "school_district": p.get("SchoolDistrictName"),
        "annual_taxes": p.get("TaxAnnualAmount"), "water_source": p.get("WaterSource"), "sewer": p.get("Sewer"),
        "public_remarks": p.get("PublicRemarks"),
    }


async def lookup_subject(db: AsyncSession, item: dict) -> tuple[dict, str]:
    """What the agent does: type the address, pick it, the app looks the listing up."""
    addr = f"{item['address']}, {item['city']}, {item.get('state') or 'PA'} {item.get('zipcode') or ''}".strip()
    found = await PropertyService.get_property_by_address(db, addr)
    note = "address lookup returned this listing"
    if not found or found.listing_key != item["listing_key"]:
        note = (f"address lookup returned listing {found.listing_key if found else 'nothing'} (a later or other record "
                "of this address) - used this sale's own listing, as an agent picking it from the list would")
        found = await PropertyService.get_property_by_listing_key(db, item["listing_key"])
    if not found:
        raise ValueError("listing not found in the mirror")
    return map_property_to_subject(found.model_dump(by_alias=True)), note


def _score_subject(s: dict) -> dict:
    keys = ("listing_key", "source", "sqft", "above_grade_finished_area", "lot_size", "city", "school_district", "township",
            "subdivision_name", "latitude", "longitude", "year_built", "property_type", "architectural_style", "public_remarks")
    return {k: s.get(k) for k in keys}


def _pickable(c) -> bool:
    d = c.similarity_details or {}
    return not d.get("property_type_incompatible") and d.get("valuation_eligible") is not False


async def wizard_comps(db: AsyncSession, subject: dict, as_of: date) -> tuple[list, dict]:
    """The Studio wizard's comp step: sold comps, price band +/-35% of the list price, beds - 1,
    radius 1 mi widening while < 5 usable 70%+ matches, sold-within 200 days widening while < 5
    results; then auto-pick the top 3 + up to 5 at 60%+. Sales are capped at the as-of date."""
    lp = subject.get("list_price") or 0
    min_price = max(0, int(lp * 0.65 // 10000) * 10000) if lp else None
    max_price = -(-int(lp * 1.35) // 10000) * 10000 if lp else None
    beds = subject.get("beds") or 0
    min_beds = max(0, beds - (2 if beds >= 8 else 1)) if beds else None
    ri, si, auto_r, auto_s, extra_step = 0, 1, True, True, False
    steps = []
    res: list = []
    for _ in range(30):
        req = CmaCompSearchRequest(
            listing_key=subject.get("listing_key"), subject=CmaSubjectProperty(**{k: v for k, v in _score_subject(subject).items() if v is not None}),
            latitude=subject["latitude"], longitude=subject["longitude"],
            subject_sqft=subject.get("above_grade_finished_area") or subject.get("sqft"),
            property_type=subject.get("property_type"), radius_miles=_RADIUS_STEPS[ri], statuses=["sold"],
            sale_date_from=(as_of - timedelta(days=_SOLD_DAYS_STEPS[si])).isoformat(), sale_date_to=as_of.isoformat(),
            min_price=min_price, max_price=max_price, min_beds=min_beds or None,
        )
        res = [c for c in await cma_service.search_comparables(db, req) if isinstance(c.match_score, int)]
        strong = sum(1 for c in res if c.match_score >= 70 and _pickable(c))
        steps.append({"radius_mi": _RADIUS_STEPS[ri], "sold_within_days": _SOLD_DAYS_STEPS[si], "results": len(res), "usable_70": strong})
        widen = False
        if auto_s and len(res) < 5 and si < len(_SOLD_DAYS_STEPS) - 1:
            si += 1
            widen = True
        if auto_r:
            # Same as the wizard (10/20): one step past "enough", since every in-circle sale is
            # scored and a wider circle only adds candidates.
            enough = strong >= _MIN_STRONG_MATCHES
            if ri < len(_RADIUS_STEPS) - 1 and (not enough or not extra_step):
                if enough:
                    extra_step = True
                ri += 1
                widen = True
            else:
                auto_r = False
        if not widen:
            break
    res.sort(key=lambda c: -c.match_score)
    eligible = [c for c in res if _pickable(c)]
    picks = [c for i, c in enumerate(eligible) if i < 3 or (i < _MAX_COMPS and c.match_score >= _AUTO_PICK_EXTRA_MIN_SCORE)]
    return picks, {"search_steps": steps, "price_band": [min_price, max_price], "min_beds": min_beds, "candidates": len(res)}


def _candidate_to_comparable(c, i: int) -> dict:
    d = c.model_dump()
    keep = set(CmaComparable.model_fields) & set(d)
    comp = {k: d[k] for k in keep}
    comp.update({"source": "mls", "is_included": True, "order_index": i, "home_type": c.property_type,
                 "quality": {}, "strength": None, "commentary": None})
    return CmaComparable(**comp).model_dump()


def summarize(items: list[dict]) -> dict:
    done = [i for i in items if i.get("status") == "done" and i.get("value")]

    def stats(rows):
        if not rows:
            return {"n": 0}
        errs = [abs(r["error_pct"]) for r in rows]
        lerr = [abs(r["list_error_pct"]) for r in rows if r.get("list_error_pct") is not None]
        return {
            "n": len(rows),
            "median_abs_error_pct": round(statistics.median(errs), 1),
            "mean_error_pct": round(statistics.mean(r["error_pct"] for r in rows), 1),
            "within_5_pct": round(100 * sum(e <= 5 for e in errs) / len(errs)),
            "within_10_pct": round(100 * sum(e <= 10 for e in errs) / len(errs)),
            "range_hit_pct": round(100 * sum(bool(r.get("in_range")) for r in rows) / len(rows)),
            "asking_price_median_abs_error_pct": round(statistics.median(lerr), 1) if lerr else None,
            "asking_price_within_5_pct": round(100 * sum(e <= 5 for e in lerr) / len(lerr)) if lerr else None,
        }
    out = {"overall": stats(done), "by_segment": {}}
    for dim in ("town", "price_band", "type"):
        groups: dict = {}
        for r in done:
            groups.setdefault((r.get("segment") or {}).get(dim) or "?", []).append(r)
        out["by_segment"][dim] = {k: stats(v) for k, v in sorted(groups.items())}
    return out


async def _run_one(batch_id: str, idx: int) -> None:
    async with AsyncSessionLocal() as db:
        batch = await db.get(CmaBacktestBatch, batch_id)
        item = dict(batch.items[idx])
        settings = batch.settings or {}
        dry = bool(settings.get("dry_run"))
        owner = await db.get(User, batch.owner_id)
        item.update({"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        batch.items[idx] = item
        flag_modified(batch, "items")
        await db.commit()

    as_of = date.fromisoformat(item["as_of"])
    tm = cma_time_machine.TimeMachine(
        listing_key=item["listing_key"], as_of=as_of, list_price=item["list_price"], dom=item.get("dom_as_of"),
        close_price=item["close_price"], close_date=_d(item["close_date"]), contract_date=_d(item.get("contract_date")),
    )
    token = cma_time_machine.activate(tm)
    result: dict = {}
    report_id = None
    try:
        async with AsyncSessionLocal() as db:
            subject, lookup_note = await lookup_subject(db, item)
            subject, removed = cma_time_machine.apply_to_subject(subject, tm)
            _row = (await db.execute(select(Property).where(Property.listing_key == item["listing_key"]))).scalar_one_or_none()
            _raw = (_row.raw_mls_data or {}) if _row else {}
            _kept = cma_time_machine.masked_raw(_raw, tm)
            result["hidden_fields"] = sorted(k for k in _raw if k not in _kept)
            picks, search_meta = await wizard_comps(db, subject, as_of)
            if not picks:
                raise ValueError("the wizard found no comparable sales before the as-of date")
            comps = [_candidate_to_comparable(c, i) for i, c in enumerate(picks)]
            report = CmaReport(
                id=str(uuid.uuid4()), owner_id=owner.id, client_name="CMA accuracy test",
                mode=settings.get("mode") or "buyer", status="DRAFT", is_backtest=True,
                subject=CmaSubjectProperty(**subject).model_dump(), comparables=comps,
                pricing={}, market_context={}, improvements=[], presentation={},
            )
            db.add(report)
            await db.commit()
            report_id = report.id
            result.update({"report_id": report_id, "lookup_note": lookup_note, "remarks_removed": removed,
                           "search": search_meta,
                           "comps": [{"address": c.street_address, "city": c.city, "sale_price": c.sale_price,
                                      "close_date": c.close_date, "match": c.match_score, "distance_mi": c.distance_miles} for c in picks]})
        if dry:
            tm.fake_client = StubAnthropic({"list_price": item["list_price"], "comps": [
                {"listing_key": c.listing_key, "address": c.street_address, "price": c.sale_price or c.list_price} for c in picks]})
            tm.fake_reasoning = stub_reasoning
        async with AsyncSessionLocal() as db:
            await cma_service.mark_generation_started(db, owner.id, report_id)
            await cma_service.drop_subject_from_comps(db, owner.id, report_id)
            # Same call the Studio's "Create AI Analysis" makes (background route) - photos on,
            # map images on (off in a dry run: those are the one paid Google call in a generation).
            await cma_ai_service.generate_ai_analysis(
                db, owner.id, report_id, plan_tier=owner.plan_tier or "PREMIUM",
                max_photos_per_property=None, include_map_images=not dry, force_regenerate=False,
            )
        await cma_service.mark_generation_finished(owner.id, report_id, "complete")
        # Every request that reached a (stub) model, scanned again end to end.
        for i, cap in enumerate(tm.captured):
            cma_time_machine.check_payload(cap, f"captured model request {i + 1} ({cap.get('provider')})", raise_now=False)
        async with AsyncSessionLocal() as db:
            rep = await db.get(CmaReport, report_id)
            rd = {"subject": rep.subject, "comparables": rep.comparables, "pricing": rep.pricing, "improvements": rep.improvements}
            est = (rep.pricing or {}).get("ai_estimate") or {}
            v = est.get("value")
            cp = item["close_price"]
            findings = audit_report(rd, as_of=as_of, subject_dom=item.get("dom_as_of"))
            result.update({
                "status": "leak" if tm.leak_log else "done",
                "value": v, "range_min": est.get("range_min"), "range_max": est.get("range_max"),
                "confidence": est.get("confidence"),
                "error_pct": round(100 * (v - cp) / cp, 1) if v else None,
                "in_range": bool(v and est.get("range_min") <= cp <= est.get("range_max")),
                "list_error_pct": round(100 * (item["list_price"] - cp) / cp, 1),
                "export_allowed": (est.get("qa_result") or {}).get("export_allowed"),
                "qa_blocking": est.get("qa_blocking") or [],
                "cost_usd": (rep.ai_analysis or {}).get("cost_usd") or 0.0,
                "images_sent": sum(len(x) for x in ((rep.ai_analysis or {}).get("images_sent") or {}).values()),
                "ai_steps": ["comp analysis + photos (Claude)"] + ((rep.ai_analysis or {}).get("followup_calls") or []) + ["final valuation (reasoning model)"],
                "audit_findings": findings,
                # Everything the word-for-word review needs, frozen with the result.
                "review": {"client_text": client_text_sections(rd), "subject_as_of": rep.subject,
                           "comps": [{k: c.get(k) for k in ("street_address", "city", "status", "sale_price", "list_price", "close_date",
                                                            "sqft", "above_grade_finished_area", "below_grade_finished_area", "beds", "baths",
                                                            "year_built", "lot_size", "days_on_market", "distance_miles", "strength",
                                                            "public_remarks")} for c in (rep.comparables or [])],
                           "final": {k: est.get(k) for k in ("value", "range_min", "range_max", "confidence", "offer_strategy_fields")},
                           "pricing_fields": {k: v for k, v in (rep.pricing or {}).items() if k != "ai_estimate"}},
            })
    except cma_time_machine.TimeMachineLeak as exc:
        result.update({"status": "leak", "error": str(exc)})
        if report_id:
            await cma_service.mark_generation_finished(batch.owner_id, report_id, "failed", error="accuracy test stopped: leak check")
    except Exception as exc:  # noqa: BLE001 - one bad home must not stop the batch
        logger.exception("CMA backtest item failed", extra={"batch_id": batch_id, "listing_key": item.get("listing_key")})
        msg = getattr(exc, "detail", None) or getattr(exc, "user_message", None) or str(exc)
        result.update({"status": "failed", "error": str(msg)[:400]})
        if report_id:
            await cma_service.mark_generation_finished(batch.owner_id, report_id, "failed", error=str(msg)[:200])
    finally:
        result.update({"leak_checks": tm.checks, "leak_log": tm.leak_log, "captured_requests": len(tm.captured),
                       "ai_input_excerpt": tm.prompt_excerpt,
                       "finished_at": datetime.now(timezone.utc).isoformat()})
        cma_time_machine.deactivate(token)

    async with AsyncSessionLocal() as db:
        batch = await db.get(CmaBacktestBatch, batch_id)
        item.update(result)
        batch.items[idx] = item
        flag_modified(batch, "items")
        batch.actual_cost_usd = round(sum(float(i.get("cost_usd") or 0) for i in batch.items), 4)
        await db.commit()


_WORKER: Optional[asyncio.Task] = None


async def _worker() -> None:
    while True:
        async with AsyncSessionLocal() as db:
            # Browser-run ("ui") batches belong to scripts/cma_backtest_ui_runner.py, never this worker.
            batch = (await db.execute(select(CmaBacktestBatch).where(
                CmaBacktestBatch.status.in_(["queued", "running"]),
                func.coalesce(CmaBacktestBatch.settings["runner"].astext, "api") != "ui",
            ).order_by(CmaBacktestBatch.created_at))).scalars().first()
            if not batch:
                return
            batch.status = "running"
            items = list(batch.items or [])
            for i, it in enumerate(items):          # a home left 'running' by a restart is not re-billed
                if it.get("status") == "running":
                    items[i] = {**it, "status": "interrupted"}
            batch.items = items
            flag_modified(batch, "items")
            await db.commit()
            batch_id = batch.id
        while True:
            async with AsyncSessionLocal() as db:
                batch = await db.get(CmaBacktestBatch, batch_id)
                if batch.status != "running":
                    break
                nxt = next((i for i, it in enumerate(batch.items) if it.get("status") == "queued"), None)
                if nxt is None:
                    batch.status = "done"
                    await db.commit()
                    break
                cap = float((batch.settings or {}).get("cost_cap_usd") or DEFAULT_COST_CAP_USD)
                per = float((batch.settings or {}).get("per_home_estimate_usd") or 0)
                spent = float(batch.actual_cost_usd or 0)
                if not (batch.settings or {}).get("dry_run") and spent + per > cap:
                    batch.status = "stopped"
                    batch.error = f"Stopped before the next home: ${spent:.2f} spent + ~${per:.2f} would pass the ${cap:.2f} cap."
                    await db.commit()
                    break
            await _run_one(batch_id, nxt)


def ensure_worker() -> None:
    global _WORKER
    if _WORKER is None or _WORKER.done():
        _WORKER = asyncio.create_task(_worker())


def worker_running() -> bool:
    return _WORKER is not None and not _WORKER.done()


async def delete_batch_reports(db: AsyncSession, owner_id: str, report_ids: Optional[list] = None) -> int:
    q = select(CmaReport).where(CmaReport.owner_id == owner_id, CmaReport.is_backtest.is_(True))
    if report_ids is not None:
        q = q.where(CmaReport.id.in_(report_ids or ["-"]))
    reports = (await db.execute(q)).scalars().all()
    for r in reports:
        await db.delete(r)
    return len(reports)
