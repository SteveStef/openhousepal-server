"""Area-level market statistics computed entirely from our own local `properties` table - no
Bright MLS calls (see module docstrings across app/services/areas/ for why: the website layer
must never touch Bright directly, per the Website Builder architecture). This is intentionally
NOT a reuse of cma_service.search_comparables (a radius comp search around one property with no
rollup) - it's a new, small, aggregate query, the kind of thing that genuinely didn't exist
anywhere in this codebase yet (confirmed during Phase 3 planning research).
"""
import re
from app.services import mls_mirror
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Numeric, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property
from app.services.areas import geography
from app.services.open_house_manager_service import INACTIVE_STATUSES

_SOLD_STATUSES = {"CLOSED-BRIGHT", "CLOSED"}
# "For sale" means a listing a buyer can still make a first offer on. Homes under contract or
# pending are "not inactive" in Bright, but counting them as for sale overstated inventory by
# 30-50% on town pages and in articles (10/24: Wayne read 76 "for sale"; 46 really were).
FOR_SALE_STATUSES = ("ACTIVE-BRIGHT", "ACTIVE")
UNDER_CONTRACT_STATUSES = ("ACTIVE UNDER CONTRACT-BRIGHT", "PENDING-BRIGHT")


SOLD_STATUSES = tuple(sorted(_SOLD_STATUSES))


def sales_window(today=None, months: int = 12):
    """THE "last 12 months" of every town-page number (owner, 10/27: Ardmore's gauge said 125 sales
    while its monthly chart said 118 - the gauge counted back 365 days, the chart from the 1st of
    a month, the days-on-market box a third way). From the 1st of the month `months-1` months ago
    through today, so the chart's monthly bars and every count/median agree. Returns (start, end)."""
    from datetime import date
    today = today or date.today()
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1), today


def closed_sales_filter(model, start, end):
    """THE "a sale in this window" filter, shared by the stats, the trends chart and the market
    gauge: a closed residential sale, closing on or after `start` and no later than `end` (a few
    listings carry future close dates), at a real price (geography.MIN_PLAUSIBLE_SALE_PRICE -
    $1 transfers and data-entry rows are not sales). Combine with a location filter."""
    from sqlalchemy import and_
    close_date = mls_mirror.close_date(model)
    close_price = mls_mirror.close_price(model)
    return and_(
        geography.residential_for_sale_filter(model),
        model.home_status.in_(SOLD_STATUSES),
        close_date >= start.isoformat(),
        func.substr(close_date, 1, 10) <= end.isoformat(),
        close_price >= geography.MIN_PLAUSIBLE_SALE_PRICE,
    )


def _location_filter(zip_codes: list[str], city: Optional[str]):
    clauses = []
    if zip_codes:
        clauses.append(Property.zipcode.in_(zip_codes))
    if city:
        clauses.append(Property.city.ilike(city))
    return or_(*clauses) if clauses else None


async def get_area_market_stats(db: AsyncSession, zip_codes: list[str] | dict, city: Optional[str] = None) -> dict:
    if isinstance(zip_codes, dict):
        location_filter = geography.location_filter(Property, zip_codes)
    else:
        location_filter = _location_filter(zip_codes, city)
    if location_filter is None:
        return {}

    # Residential for-sale rows only (see geography.residential_for_sale_filter) - rentals,
    # commercial space, land and sub-$50K data-entry rows all skewed these numbers before.
    for_sale = geography.residential_for_sale_filter(Property)

    active_row = (await db.execute(
        select(
            func.count(Property.id),
            func.percentile_cont(0.5).within_group(Property.price.asc()),
            # today's DOM for Active homes (the stored count is as of when we last read the listing)
            func.percentile_cont(0.5).within_group(case(
                (Property.home_status == "ACTIVE-BRIGHT", Property.days_on_market + func.greatest(0,
                    func.current_date() - func.date(func.timezone("America/New_York", Property.updated_at)))),
                else_=Property.days_on_market).asc()),
            func.avg(Property.price_per_square_feet),
            func.min(Property.price),
            func.max(Property.price),
        ).where(location_filter, for_sale, Property.home_status.in_(FOR_SALE_STATUSES))
    )).first()
    under_contract = (await db.execute(
        select(func.count(Property.id)).where(location_filter, for_sale, Property.home_status.in_(UNDER_CONTRACT_STATUSES))
    )).scalar() or 0

    sold_row = (await db.execute(
        select(
            func.count(Property.id),
            func.percentile_cont(0.5).within_group(Property.price.asc()),
        ).where(location_filter, for_sale, Property.home_status.in_(_SOLD_STATUSES))
    )).first()

    # The town's price range (user, 10/17 - Radnor read "$140K - $10M"): what homes actually SOLD
    # for in the last two years, the middle 90% of them, leaving out units under 800 sq ft (studio
    # condos, parking spaces) - not the min/max of whatever happens to be listed today.
    from datetime import date, timedelta
    close_price = mls_mirror.close_price()
    range_row = (await db.execute(
        select(
            func.count(close_price),
            func.percentile_cont(0.05).within_group(close_price.asc()),
            func.percentile_cont(0.95).within_group(close_price.asc()),
        ).where(
            location_filter, for_sale, Property.home_status.in_(_SOLD_STATUSES),
            mls_mirror.close_date() >= (date.today() - timedelta(days=730)).isoformat(),
            or_(Property.living_area.is_(None), Property.living_area >= 800),
        )
    )).first()

    # How long homes took to sell (closed in the last 12 months - the shared sales_window, so it
    # matches the trends chart and the gauge). median_dom above is how long today's listings have
    # sat so far - Malvern's FAQ called that 63 "days on the market" while its sold homes took 12.
    win_start, win_end = sales_window()
    sold_doms = [float(v) for v in (await db.execute(
        select(mls_mirror.col(Property.days_on_market, "DaysOnMarket")).where(
            location_filter, closed_sales_filter(Property, win_start, win_end),
        )
    # (`v not in (None, "")`, not `if v`: a home that sold in 0 days counts - the column reads 0 where
    # the payload read "0")
    )).scalars().all() if v not in (None, "") and re.fullmatch(r"\d+(?:\.\d+)?", str(v).strip())]

    # Real MLS field per listing (School District Name), not the NCES attendance-boundary lookup
    # baked into MarketArea.facts.schools - reuses data already ingested for every property, so
    # it's as reliable/fresh as anything else on this page rather than depending on a separate
    # enrichment pipeline (NCES/Census) with its own availability gaps.
    district_rows = (await db.execute(
        select(Property.school_district_name)
        .where(location_filter, Property.home_status.notin_(INACTIVE_STATUSES), Property.school_district_name.isnot(None))
        .distinct()
    )).scalars().all()

    active_count = active_row[0] or 0
    sold_count = sold_row[0] or 0
    if not active_count and not sold_count and not under_contract:
        return {}

    stats: dict = {"source": "ohp_local", "under_contract_count": int(under_contract)}
    if active_count:
        stats["active_count"] = active_count
        if active_row[1] is not None:
            stats["median_price"] = round(float(active_row[1]))
        if active_row[2] is not None:
            stats["median_dom"] = round(float(active_row[2]))
        if active_row[3] is not None:
            stats["avg_price_per_sqft"] = round(float(active_row[3]))
    if range_row and (range_row[0] or 0) >= 5 and range_row[1] is not None and range_row[2] is not None:
        stats["min_price"] = round(float(range_row[1]))
        stats["max_price"] = round(float(range_row[2]))
        stats["price_range_basis"] = "sold_2y"
        stats["price_range_sales"] = int(range_row[0])
    if sold_doms:
        stats["median_sold_dom"] = round(_median(sold_doms))
        stats["sold_window_start"] = win_start.isoformat()
    if sold_count:
        stats["recently_sold_count"] = sold_count
        if sold_row[1] is not None:
            stats["median_sold_price"] = round(float(sold_row[1]))
    if district_rows:
        stats["school_districts"] = sorted(district_rows)

    if len(stats) <= 2:  # only "source" / "under_contract_count" - nothing usable came back
        return {}

    stats["computed_at"] = datetime.now(timezone.utc).isoformat()
    return stats


def _month_key(iso: str) -> str:
    return str(iso)[:7]


async def get_area_market_trends(db: AsyncSession, scope: dict, months: int = 12) -> dict:
    """The neighborhood page's "Market Trends" section, computed from our own MLS mirror on every
    request: closed sales by month for the last `months` months (count + median close price),
    the same window one year earlier for a year-over-year read, sale-to-list and sold days on
    market, plus a snapshot of what's for sale right now (price bands, property-type mix, new in
    the last 30 days). Residential for-sale rows only. Three bounded queries over the indexed
    location filter - no full-table work."""
    from datetime import date, timedelta

    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    resid = geography.residential_for_sale_filter(Property)
    close_date = mls_mirror.close_date()
    # The shared window (sales_window): the 1st of the month `months-1` months ago through today;
    # the year-earlier comparison is the same stretch of days one year back.
    window_start, today = sales_window(months=months)
    prior_start = date(window_start.year - 1, window_start.month, 1)
    try:
        prior_end = today.replace(year=today.year - 1)
    except ValueError:  # Feb 29
        prior_end = today.replace(year=today.year - 1, day=28)

    rows = (await db.execute(
        select(close_date, mls_mirror.text_field("ClosePrice"), mls_mirror.col(Property.price, "ListPrice"),
               mls_mirror.col(Property.days_on_market, "DaysOnMarket"), Property.living_area)
        .where(location_filter, closed_sales_filter(Property, prior_start, today))
    )).all()

    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    by_month: dict[str, list[float]] = {}
    current: list[float] = []
    prior: list[float] = []
    ratios: list[float] = []
    doms: list[float] = []
    ppsf: list[float] = []
    for cd, cp, lp, dom, sqft in rows:
        if not cd:
            continue
        price = _f(cp)
        if not price or price < geography.MIN_PLAUSIBLE_SALE_PRICE:
            continue
        day = str(cd)[:10]
        if day >= window_start.isoformat():
            by_month.setdefault(_month_key(day), []).append(price)
            current.append(price)
            lpv = _f(lp)
            if lpv and lpv > 0:
                ratios.append(price / lpv)
            domv = _f(dom)
            if domv is not None:
                doms.append(domv)
            if sqft and sqft > 0:
                ppsf.append(price / sqft)
        elif day >= prior_start.isoformat() and day <= prior_end.isoformat():
            prior.append(price)

    series = []
    yy, mm = window_start.year, window_start.month
    for _ in range(months):
        key = f"{yy:04d}-{mm:02d}"
        prices = by_month.get(key, [])
        med = _median(prices)
        series.append({"month": key, "label": date(yy, mm, 1).strftime("%b"), "closed": len(prices), "median_price": round(med) if med else None})
        mm += 1
        if mm > 12:
            yy, mm = yy + 1, 1

    median_now = _median(current)
    # A year-earlier median from only a handful of sales isn't a comparison (Rittenhouse showed a
    # "70.7% drop" from a thin year): it needs 10+ sales and at least half the current year's count.
    median_prior = _median(prior) if len(prior) >= max(10, len(current) // 2) else None
    yoy = round(((median_now - median_prior) / median_prior) * 100, 1) if median_now and median_prior else None

    active_rows = (await db.execute(
        select(Property.price, Property.home_type, Property.mls_list_date)
        .where(location_filter, resid, Property.home_status.in_(FOR_SALE_STATUSES))
    )).all()
    bands = [("Under $500K", 0, 500_000), ("$500K-$1M", 500_000, 1_000_000), ("$1M-$2M", 1_000_000, 2_000_000), ("$2M-$3M", 2_000_000, 3_000_000), ("$3M+", 3_000_000, None)]
    band_counts = {label: 0 for label, _, _ in bands}
    type_counts: dict[str, int] = {}
    new_30 = 0
    cutoff_30 = today - timedelta(days=30)
    for price, home_type, listed in active_rows:
        if price:
            for label, lo, hi in bands:
                if price >= lo and (hi is None or price < hi):
                    band_counts[label] += 1
                    break
        type_counts[home_type or "OTHER"] = type_counts.get(home_type or "OTHER", 0) + 1
        try:
            listed_day = listed.date() if hasattr(listed, "date") else listed
        except Exception:
            listed_day = None
        if listed_day and listed_day >= cutoff_30:
            new_30 += 1

    type_labels = {"SINGLE_FAMILY": "Single family", "Single Family": "Single family", "TOWNHOUSE": "Townhouse", "CONDO": "Condo", "MULTI_FAMILY": "Multi-family", "FARM": "Farm", "OTHER": "Other"}
    type_mix = sorted(({"label": type_labels.get(k, k.title()), "count": v} for k, v in type_counts.items()), key=lambda t: -t["count"])

    if not current and not active_rows:
        return {}
    return {
        "months": months,
        "series": series,
        "closed_count": len(current),
        "median_sold_price": round(median_now) if median_now else None,
        "prior_median_sold_price": round(median_prior) if median_prior else None,
        "yoy_median_change_pct": yoy,
        "median_sale_to_list_pct": round(_median(ratios) * 100, 1) if ratios else None,
        "median_sold_dom": round(_median(doms)) if doms else None,
        "median_sold_price_per_sqft": round(_median(ppsf)) if ppsf else None,
        "active_count": len(active_rows),
        "new_last_30_days": new_30,
        "price_bands": [{"label": label, "count": band_counts[label]} for label, _, _ in bands],
        "type_mix": type_mix,
        "window_start": window_start.isoformat(),
        "window_end": today.isoformat(),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- MLS coverage guard
# Some towns sit mostly in ANOTHER MLS's territory, so our Bright feed sees a sliver of their
# market (10/27 Charlottesville, VA - the CAAR MLS area: Bright stopped carrying CAAR's shared
# listings on 7/15/2026, leaving 13 homes for sale and ~30 sales a year in a city of ~20,000
# households, and locally 200+ "under contract" rows frozen since that day). A buyer's/seller's
# verdict or a sales chart built on that is wrong, so such a town's page keeps its homes for sale
# but drops the sales-based numbers.
STALE_CONTRACT_DAYS = 90       # an under-contract/pending listing with no MLS update in 90+ days
MIN_STALE_CONTRACTS = 20       # ...counts against a town only when there are 20+ of them
THIN_PER_1000_HOUSEHOLDS = 5   # homes for sale now + sales in the last 12 months, per 1,000 households
_SALES_STAT_KEYS = ("under_contract_count", "median_sold_dom", "sold_window_start", "recently_sold_count",
                    "median_sold_price", "min_price", "max_price", "price_range_basis", "price_range_sales")


def coverage_verdict(active: int, sales_12: int, under_contract: int, stale_under_contract: int,
                     households: Optional[float]) -> dict:
    """Pure rule (tested): thin when stale under-contract rows are 20+ and at least half of all
    under-contract rows (a feed that stopped updating), or when the town has a real household count
    (1,000+) and fewer than 5 homes for sale + 12-month sales per 1,000 households (a normal town
    in our data runs 17-35; Charlottesville 1-2)."""
    reasons = []
    if stale_under_contract >= MIN_STALE_CONTRACTS and stale_under_contract * 2 >= max(under_contract, 1):
        reasons.append("stale_under_contract")
    per_1000 = None
    if households and households >= 1000:
        per_1000 = round((active + sales_12) * 1000 / households, 1)
        if per_1000 < THIN_PER_1000_HOUSEHOLDS:
            reasons.append("low_mls_volume")
    return {"thin": bool(reasons), "reasons": reasons, "active": active, "sales_12": sales_12,
            "under_contract": under_contract, "stale_under_contract": stale_under_contract,
            "households": households, "per_1000_households": per_1000}


async def get_area_market_coverage(db: AsyncSession, scope: dict, households: Optional[float] = None) -> dict:
    from datetime import date, timedelta
    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {"thin": False, "reasons": []}
    resid = geography.residential_for_sale_filter(Property)
    stale_before = (date.today() - timedelta(days=STALE_CONTRACT_DAYS)).isoformat()
    mod = Property.raw_mls_data["ModificationTimestamp"].astext
    active = (await db.execute(select(func.count(Property.id)).where(
        location_filter, resid, Property.home_status.in_(FOR_SALE_STATUSES)))).scalar() or 0
    uc_row = (await db.execute(select(func.count(Property.id), func.sum(case((mod < stale_before, 1), else_=0))).where(
        location_filter, resid, Property.home_status.in_(UNDER_CONTRACT_STATUSES)))).first()
    start, end = sales_window()
    sales_12 = (await db.execute(select(func.count(Property.id)).where(
        location_filter, closed_sales_filter(Property, start, end)))).scalar() or 0
    out = coverage_verdict(int(active), int(sales_12), int(uc_row[0] or 0), int(uc_row[1] or 0), households)
    out["window_start"] = start.isoformat()
    return out


def town_households(area) -> Optional[float]:
    try:
        v = (((area.facts or {}).get("demographics") or {}).get("household_count") or {}).get("value")
        return float(v) if v else None
    except (TypeError, ValueError, AttributeError):
        return None


def apply_coverage_guard(payload: dict, coverage: dict) -> dict:
    """Town page payload with the sales-based market numbers taken off when coverage is thin:
    the trends chart, the buyer's/seller's gauge and what-your-money-buys (both built on sales),
    and the stats' sold / under-contract figures. Homes for sale, taxes and recent sales stay."""
    if not (coverage or {}).get("thin"):
        return payload
    facts = payload.get("facts")
    if isinstance(facts, dict) and isinstance(facts.get("market_stats"), dict):
        ms = dict(facts["market_stats"])
        for k in _SALES_STAT_KEYS:
            ms.pop(k, None)
        facts["market_stats"] = ms
    payload["market_trends"] = {}
    if isinstance(payload.get("insights"), dict):
        ins = dict(payload["insights"])
        ins.pop("gauge", None)
        ins.pop("money_buys", None)
        payload["insights"] = ins
    payload["market_coverage"] = {
        "thin": True, "reasons": coverage.get("reasons") or [],
        "note": "Our MLS feed covers only part of this area's home sales, so sales statistics and market trends aren't shown here.",
    }
    return payload


async def guard_town_market(db: AsyncSession, area, scope: dict, payload: dict) -> dict:
    """The page-time hook (public_site_routes.area_page_payload): coverage cached like the other
    live market numbers, then apply_coverage_guard. Never breaks the page - on any error the
    payload goes out unchanged."""
    from app.services.website import content_adapters
    try:
        coverage = await content_adapters.cached_market(
            f"coverage:{area.id}", lambda d: get_area_market_coverage(d, scope, town_households(area)), db)
    except Exception:
        return payload
    return apply_coverage_guard(payload, coverage)


def _median(values: list[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Whole-table figures (the newest close date, all closings in a window) take ~5 s each and don't
# depend on the town - computed once per hour, not once per town per article. They only move when the
# MLS sync adds closings; 10 minutes made nearly every article pay for them again (owner 11/13: an
# article sat on "Gathering" until it timed out while these crawled on a busy database).
_GLOBAL_TTL_S = 3600
_global_cache: dict = {}


async def _global(key, compute):
    import time
    hit = _global_cache.get(key)
    if hit and time.monotonic() - hit[0] < _GLOBAL_TTL_S:
        return hit[1]
    value = await compute()
    _global_cache[key] = (time.monotonic(), value)
    return value


async def _closed_count_all(db: AsyncSession, start: str, stop: str) -> int:
    close_date = mls_mirror.close_date()

    async def compute():
        return (await db.execute(select(func.count(Property.id)).where(
            geography.residential_for_sale_filter(Property), Property.home_status.in_(_SOLD_STATUSES), close_date > start, close_date <= stop,
        ))).scalar() or 0
    return await _global(("closed_all", start, stop), compute)


async def _closed_counts_by_slice(db: AsyncSession, end, slice_days: int, n_slices: int) -> list[int]:
    """All closings in each `slice_days` step back from `end`, newest first - ONE pass over the
    table for every slice (at least a year's worth, so the 90-day snapshot and the 12-month cost
    profile share it). It was one full pass per slice: ~15 passes, two minutes on a cold cache."""
    from datetime import timedelta
    from sqlalchemy import and_
    close_date = mls_mirror.close_date()
    n = max(n_slices, 12)
    bounds = [((end - timedelta(days=(i + 1) * slice_days)).isoformat(), (end - timedelta(days=i * slice_days)).isoformat()) for i in range(n)]

    async def compute():
        row = (await db.execute(select(*[
            func.count(Property.id).filter(and_(close_date > start, close_date <= stop)) for start, stop in bounds
        ]).where(
            geography.residential_for_sale_filter(Property), Property.home_status.in_(_SOLD_STATUSES),
            close_date > bounds[-1][0], close_date <= bounds[0][1],
        ))).one()
        return [int(v or 0) for v in row]
    return await _global(("closed_slices", end.isoformat(), slice_days, n), compute)


async def covered_days(db: AsyncSession, end, max_days: int, slice_days: int = 30) -> int:
    """How far back from `end` our closed-sales mirror is complete, in whole `slice_days` steps
    (at most `max_days`). 11/01 grader: "283 sales since Nov 1, 2025" was really ~2 months -
    the mirror held almost nothing for Nov 2025-Jun 2026, so every count and mix built on a
    longer window undercounted while its label promised a year. A slice counts as covered when
    it holds 60%+ as many closings (across ALL our sales, not one small town) as the fullest
    slice; the newest slice always counts (recording lag makes it look thin)."""
    from datetime import timedelta

    n_slices = max(1, max_days // slice_days)
    slices = (await _closed_counts_by_slice(db, end, slice_days, n_slices))[:n_slices]
    top = max(slices) if slices else 0
    n = 1
    while n < len(slices) and top and slices[n] >= 0.6 * top:
        n += 1
    return n * slice_days


async def get_area_sales_snapshot(db: AsyncSession, scope: dict, days: int = 90) -> dict:
    """A DATED closed-sales snapshot for articles (unlike get_area_market_stats, whose sold numbers
    span every sale on record): closed sales in the `days` window ending on the newest CloseDate in
    our data (never later than today - a few listings carry future close dates), the same window
    one year earlier, and today's inventory. Every figure carries the window it covers, so an
    article can say "in the 90 days ending Sep 10, 2026".

    Same home definition as the town pages (geography.residential_for_sale_filter: houses,
    townhomes, condos, multi-family - no rentals, land or commercial, nothing under $50K) and the
    same "for sale" (active listings only; under contract / pending counted separately), so the
    article and its Town Spotlight never disagree. `prior_year_reliable` is False when our MLS
    mirror holds far fewer closings a year back than now (history before the sync started is
    thin - 10/17 an article got "0 sales a year ago" for Radnor and Wayne)."""
    from datetime import date, timedelta

    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    resid = geography.residential_for_sale_filter(Property)
    close_date = mls_mirror.close_date()
    base = (location_filter, resid, Property.home_status.in_(_SOLD_STATUSES))
    today = date.today()
    # One window end for every town (the newest close date in our data), so several towns in one
    # article share the same "90 days ending ..." - not each town's own last sale.
    async def newest():
        return (await db.execute(select(func.max(close_date)).where(
            Property.home_status.in_(_SOLD_STATUSES), resid, close_date <= today.isoformat()))).scalar()
    latest = await _global(("latest_close", today.isoformat()), newest)
    if not latest:
        return {}
    end = date.fromisoformat(str(latest)[:10])
    # Only as far back as our records are complete (a 60-day window beats a 90-day one that's
    # missing a month of sales but still says "90 days").
    days = min(days, await covered_days(db, end, days))

    async def window(start: date, stop: date) -> dict:
        rows = (await db.execute(
            select(mls_mirror.text_field("ClosePrice"), mls_mirror.col(Property.price, "ListPrice"),
                   mls_mirror.col(Property.days_on_market, "DaysOnMarket"), Property.home_type)
            .where(*base, close_date > start.isoformat(), close_date <= stop.isoformat())
        )).all()
        prices, ratios, doms = [], [], []
        by_type: dict[str, list[float]] = {"condo": [], "townhouse": [], "house": []}
        for cp, lp, dom, home_type in rows:
            cpv = _num(cp)
            if not cpv or cpv < geography.MIN_PLAUSIBLE_SALE_PRICE:
                continue
            prices.append(cpv)
            kind = {"CONDO": "condo", "TOWNHOUSE": "townhouse", "SINGLE_FAMILY": "house", "Single Family": "house"}.get(home_type)
            if kind:
                by_type[kind].append(cpv)
            lpv = _num(lp)
            if lpv and lpv > 0:
                ratios.append(cpv / lpv)
            domv = _num(dom)
            if domv is not None:
                doms.append(domv)
        out: dict = {"closed_count": len(prices), "from": (start + timedelta(days=1)).isoformat(), "to": stop.isoformat()}
        if prices:
            out["median_close_price"] = round(_median(prices))
        if ratios:
            out["median_sale_to_list_pct"] = round(_median(ratios) * 100, 1)
            # Real counts - a 100% median doesn't mean half sold above asking (11/01 grader: many
            # sell at exactly asking).
            out["vs_asking"] = {"above": sum(1 for r in ratios if r > 1.0005), "at": sum(1 for r in ratios if abs(r - 1) <= 0.0005),
                                "below": sum(1 for r in ratios if r < 0.9995), "of": len(ratios)}
        if doms:
            out["median_days_on_market"] = round(_median(doms))
        # What each kind of home sold for (11/01 Arlington condo article: the only sold median mixed
        # in $1.5M houses, so the writer reached for a news site's one-month condo average).
        types = {k: {"count": len(v), "median_close_price": round(_median(v))} for k, v in by_type.items() if len(v) >= 5}
        if len(types) > 1:
            out["by_type"] = types
        return out

    current = await window(end - timedelta(days=days), end)
    prior_end = end - timedelta(days=365)
    prior = await window(prior_end - timedelta(days=days), prior_end)
    # Coverage check across ALL our closed sales (not just this town, which can be tiny): if the
    # mirror holds under 60% as many closings a year back, last year's numbers are a data gap.
    # (this window is whole 30-day steps back from `end` - covered_days - so the one-pass slice counts
    # already hold it; only last year's window needs a pass of its own)
    now_all = sum((await _closed_counts_by_slice(db, end, 30, days // 30))[:days // 30]) if days % 30 == 0 \
        else await _closed_count_all(db, (end - timedelta(days=days)).isoformat(), end.isoformat())
    then_all = await _closed_count_all(db, (prior_end - timedelta(days=days)).isoformat(), prior_end.isoformat())
    prior_ok = bool(now_all) and then_all >= 0.6 * now_all

    active = (await db.execute(
        select(func.count(Property.id), func.percentile_cont(0.5).within_group(Property.price.asc()),
               func.percentile_cont(0.25).within_group(Property.price.asc()))
        .where(location_filter, resid, Property.home_status.in_(FOR_SALE_STATUSES))
    )).first()
    under_contract = (await db.execute(
        select(func.count(Property.id)).where(location_filter, resid, Property.home_status.in_(UNDER_CONTRACT_STATUSES))
    )).scalar() or 0
    return {
        "window_days": days,
        "data_through": end.isoformat(),
        "current": current,
        "prior_year": prior,
        "prior_year_reliable": prior_ok,
        "active_count": active[0] or 0,
        "active_median_list_price": round(float(active[1])) if active[1] is not None else None,
        "active_p25_list_price": round(float(active[2])) if active[2] is not None else None,
        "under_contract_count": int(under_contract),
        "inventory_as_of": today.isoformat(),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _one_level(levels) -> bool:
    """Bright's Levels field (["1"], ["2.5"], "1") - a one-level home or condo unit."""
    vals = levels if isinstance(levels, list) else [levels]
    return any(str(v).strip() in ("1", "1.0") for v in vals if v is not None)


def _home_mix(homes: list[dict]) -> dict:
    """What kinds of homes are for sale - what a downsizer or first-time buyer is really shopping
    for: condos/townhomes vs single-family houses (count + median list price), how much of the
    lowest-priced quarter is condos/townhomes (11/01: Wayne's quarter-price of $296K next to an
    $876K median sale read as a typo without it), one-level homes, and 55+ communities."""
    if len(homes) < 8:
        return {}
    out: dict = {"homes": len(homes)}
    for key in ("attached", "house", "one_level", "manufactured"):
        group = [h["price"] for h in homes if h[key]]
        out[key] = {"count": len(group), "median": round(_median(group)) if len(group) >= 5 else None}
    prices = sorted(h["price"] for h in homes)
    pos = 0.25 * (len(prices) - 1)  # same interpolation as percentile_cont in the sales snapshot
    lo = int(pos)
    p25 = prices[lo] + (prices[min(lo + 1, len(prices) - 1)] - prices[lo]) * (pos - lo)
    low = [h for h in homes if h["price"] <= p25]
    out["low_quarter"] = {"count": len(low), "attached": sum(1 for h in low if h["attached"])}
    recorded = [h for h in homes if h["senior"] is not None]
    # Only when most listings fill the field in - otherwise "none in a 55+ community" could just be blanks.
    if len(recorded) >= 0.6 * len(homes):
        senior = [h["price"] for h in recorded if h["senior"]]
        out["senior"] = {"count": len(senior), "median": round(_median(senior)) if len(senior) >= 5 else None}
    return out


def _fee_monthly(fee: Optional[float], freq: Optional[str]) -> Optional[float]:
    if not fee or fee <= 0:
        return None
    f = (freq or "").lower()
    if "month" in f:
        return fee
    if "quarter" in f:
        return fee / 3
    if "annual" in f or "year" in f:
        return fee / 12
    return None  # unknown frequency - not comparable


def parts_fit(total: Optional[int], parts: list[int]) -> bool:
    """A breakdown is only usable when its parts, counted from the same records as the total, never
    add up to more than it (11/01 grader: Lancaster's "195 tax bills" next to 49 + 146 + 23 by
    township - 2026 bills vs every year's bills, and a city filed under the township's name)."""
    return total is not None and all(p >= 0 for p in parts) and sum(parts) <= total


def _place_of(lat, lng, munis: list[dict]) -> Optional[str]:
    """The census municipality a home's map location falls in (munis = the town's stored TIGER
    outlines: [{name, rings, box}]), or None when it has no location or sits outside all of them."""
    from app.services.areas import boundary_service as bs
    if lat is None or lng is None:
        return None
    return next((m["name"] for m in munis if bs.contains(m["rings"], float(lng), float(lat), m["box"])), None)


async def get_area_cost_profile(db: AsyncSession, scope: dict, munis: Optional[list[dict]] = None) -> dict:
    """What owning a home here costs, from the listings themselves (Bright MLS fields agents fill
    in): annual property tax on homes for sale (overall, as a share of list price, and per
    municipality - it sets the millage), condo/townhome association fees, and how buyers paid for
    the homes that closed recently (BuyerFinancing). Medians only, and only from groups of 5+
    homes, so one odd listing can't make a headline.

    The per-municipality tax split places each home by its MAP LOCATION in the town's census
    outlines (`munis`, from the town map's TIGER boundaries) - never Bright's township name, which
    files Lancaster City and Lancaster Township both as "LANCASTER". No outlines, no split; a home
    with no location (or outside every outline) is left out of the split, never guessed."""
    from collections import Counter
    from datetime import date, timedelta
    from app.services.areas import boundary_service as bs

    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    shapes = [{"name": m["name"], "rings": m["rings"], "box": bs.bbox(m["rings"])} for m in (munis or []) if m.get("rings") and m.get("name")]
    resid = geography.residential_for_sale_filter(Property)
    rows = (await db.execute(
        select(Property.price, Property.latitude, Property.longitude, Property.home_type,
               mls_mirror.col(Property.tax_annual_amount, "TaxAnnualAmount"), mls_mirror.col(Property.tax_year, "TaxYear"),
               mls_mirror.col(Property.association_fee, "AssociationFee"),
               mls_mirror.col(Property.association_fee_frequency, "AssociationFeeFrequency"),
               mls_mirror.col(Property.levels, "Levels", as_json=True),
               mls_mirror.col(Property.senior_community_yn, "SeniorCommunityYN"), mls_mirror.text_field("AssociationFee2"),
               mls_mirror.text_field("AssociationFee2Frequency"),
               mls_mirror.col(Property.association_fee_includes, "AssociationFeeIncludes", as_json=True),
               mls_mirror.col(Property.mls_structure_design_type, "StructureDesignType"))
        .where(location_filter, resid, Property.home_status.in_(FOR_SALE_STATUSES))
    )).all()
    taxes, years, homes = [], Counter(), []
    fees: dict[str, list[float]] = {"CONDO": [], "TOWNHOUSE": []}
    covers: Counter = Counter()
    town_own: Counter = Counter()
    for price, lat, lng, home_type, tax, tax_year, fee, freq, levels, senior, fee2, freq2, includes, structure in rows:
        tax_v, price_v = _num(tax), _num(price)
        if tax_v and tax_v > 100 and price_v and price_v >= geography.MIN_PLAUSIBLE_SALE_PRICE:
            year = str(tax_year)[:4] if tax_year else None
            taxes.append((tax_v, year, tax_v / price_v, _place_of(lat, lng, shapes) if shapes else None))
            if year:
                years[year] += 1
        if home_type in ("CONDO", "TOWNHOUSE"):
            # Bright keeps a condo's fee in AssociationFee2 (11/01 Arlington: 336 of 346 condos) and a
            # community HOA in AssociationFee - reading only the first left condos out of the median.
            parts = [m for m in (_fee_monthly(_num(fee), freq), _fee_monthly(_num(fee2), freq2)) if m]
            if home_type == "TOWNHOUSE":
                # Townhome = a building style, not ownership (11/01 grader): a condo fee (Fee2) marks
                # one owned as a condo unit; an HOA fee alone, or none, one owned with its land.
                town_own["condo_fee" if (_num(fee2) or 0) > 0 else "hoa_only" if (_num(fee) or 0) > 0 else "no_fee"] += 1
            if parts:
                fees[home_type].append(sum(parts))
                for c in (includes if isinstance(includes, list) else []):
                    if c and str(c).strip().lower() not in ("none", "other", "see remarks"):
                        covers[(home_type, str(c).strip())] += 1
        if price_v and price_v >= geography.MIN_PLAUSIBLE_SALE_PRICE:
            # 11/01 grader: Rehoboth's "one-level homes" median mixed in manufactured homes (often on
            # leased land, with lot rent and different financing) - they're counted on their own.
            manufactured = "manufactured" in str(structure or "").lower()
            homes.append({"price": price_v, "attached": home_type in ("CONDO", "TOWNHOUSE"), "manufactured": manufactured,
                          "house": home_type in ("SINGLE_FAMILY", "Single Family") and not manufactured,
                          "one_level": _one_level(levels) and not manufactured,
                          "senior": None if senior in (None, "") else str(senior).lower() == "true"})
    out: dict = {"homes_for_sale": len(rows), "as_of": date.today().isoformat()}
    if len(taxes) >= 5:
        # One tax year per town (11/01: Villanova's median mixed 2025 and 2026 bills next to towns
        # on 2026): the newest year with 5+ listings and a quarter of the bills (Frederick: 9 bills
        # marked 2027 out of hundreds), else every bill labeled with the usual year.
        newest = max((y for y, n in years.items() if n >= max(5, 0.25 * len(taxes))), default=None)
        # One set of bills for the total, the median, the share of list price AND the split.
        bills = [b for b in taxes if b[1] == newest] if newest else list(taxes)
        by_town: dict[str, list[float]] = {}
        for _, _, ratio, place in bills:
            if place:
                by_town.setdefault(place, []).append(ratio)
        split = sorted(({"township": t, "homes": len(v), "median_pct_of_list": round(_median(v) * 100, 2)}
                        for t, v in by_town.items() if len(v) >= 5), key=lambda r: -r["homes"])
        out["tax"] = {
            "homes": len(bills), "median_annual": round(_median([b[0] for b in bills])),
            "median_pct_of_list": round(_median([b[2] for b in bills]) * 100, 2),
            "tax_year": newest or (years.most_common(1)[0][0] if years else None),
            "placed": sum(1 for b in bills if b[3]),
            # Parts that don't fit inside the total are a data error - leave the split out.
            "by_township": split if parts_fit(len(bills), [r["homes"] for r in split]) else [],
        }
    hoa = {}
    listed = {"CONDO": sum(1 for r in rows if r[2] == "CONDO"), "TOWNHOUSE": sum(1 for r in rows if r[2] == "TOWNHOUSE")}
    for kind, vals in fees.items():
        # A fee median needs 10+ listings that show a fee, and says how many of the listings that is.
        if len(vals) >= 10:
            top = [(c, round(n * 100 / len(vals))) for (k, c), n in covers.most_common() if k == kind and n * 2 >= len(vals)][:6]
            hoa[kind.lower()] = {"homes": len(vals), "of": listed[kind], "median_monthly": round(_median(vals)), "covers": top}
    if hoa:
        out["hoa"] = hoa
    if sum(town_own.values()) >= 10:
        out["townhome_fees"] = {"of": sum(town_own.values()), **{k: town_own.get(k, 0) for k in ("condo_fee", "hoa_only", "no_fee")}}
    mix = _home_mix(homes)
    if mix:
        mix["homes"] = len(rows)  # the same count as the snapshot's "homes for sale"
        out["mix"] = mix

    # The same complete-records window as the sales snapshot (up to 12 months), ending on the
    # newest close date - so "how buyers paid" never claims a year our mirror doesn't hold.
    close_date = mls_mirror.close_date()
    today_iso = date.today().isoformat()

    async def newest():  # the same whole-table figure (and cache entry) as the sales snapshot's window end
        return (await db.execute(select(func.max(close_date)).where(
            Property.home_status.in_(_SOLD_STATUSES), resid, close_date <= today_iso))).scalar()
    latest = await _global(("latest_close", today_iso), newest)
    end = date.fromisoformat(str(latest)[:10]) if latest else date.today()
    span = await covered_days(db, end, 360)
    since = (end - timedelta(days=span)).isoformat()
    # The same sales the snapshot counts - real prices only ($1 transfers and data-entry rows aren't
    # sales). 11/01 round 13 Baltimore: "1,198 sales" next to "1,205 sales that recorded how the buyer paid".
    fin_rows = [fin for fin, cp in (await db.execute(
        select(mls_mirror.json_field("BuyerFinancing"), mls_mirror.text_field("ClosePrice")).where(
            location_filter, resid, Property.home_status.in_(_SOLD_STATUSES), close_date > since, close_date <= end.isoformat())
    )).all() if (_num(cp) or 0) >= geography.MIN_PLAUSIBLE_SALE_PRICE]
    kinds: Counter = Counter()
    for raw in fin_rows:
        vals = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) and raw.strip() else [])
        vals = [str(v).strip() for v in vals if str(v).strip()]
        if not vals:
            continue
        # "Cash" + "Conventional" on one sale = financed; the loan type is what matters to a buyer.
        loan = next((v for v in vals if v.lower() != "cash"), vals[0])
        label = {"adjustable rate mortgage": "Conventional"}.get(loan.lower(), loan)
        kinds[label] += 1
    recorded = sum(kinds.values())
    # Only when most sales recorded it - a mix read off a sliver of the sales would mislead.
    # 11/01 grader: a mix from a minority of sales isn't the market - most sales must record it.
    if recorded >= 20 and recorded >= 0.6 * len(fin_rows):
        out["financing"] = {
            "closed_sales": len(fin_rows), "recorded": recorded, "since": since, "through": end.isoformat(), "days": span,
            "mix": [{"type": k, "count": v, "pct": round(v * 100 / recorded)} for k, v in kinds.most_common() if v * 100 / recorded >= 1],
        }
    return out


async def count_shared_for_sale(db: AsyncSession, scope_a: dict, scope_b: dict) -> int:
    """Homes for sale that fall inside BOTH areas (Wayne mailing addresses inside Radnor
    Township) - articles say so instead of letting readers add the two counts together."""
    fa, fb = geography.location_filter(Property, scope_a), geography.location_filter(Property, scope_b)
    if fa is None or fb is None:
        return 0
    return (await db.execute(select(func.count(Property.id)).where(
        fa, fb, geography.residential_for_sale_filter(Property), Property.home_status.in_(FOR_SALE_STATUSES),
    ))).scalar() or 0


async def area_geography_mix(db: AsyncSession, scope: dict) -> dict:
    """Which townships / counties / school districts the homes of an area actually sit in, from
    the MLS records themselves (every home on record there, any status): the shares, plus the
    mailing towns inside it. This is the ground truth an article's geography is checked against -
    10/17 an article said Wayne lies partly in Lower Merion; no Wayne home on record does."""
    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    rows = (await db.execute(
        select(Property.township, Property.county, Property.school_district_name, Property.city, func.count(Property.id))
        .where(location_filter).group_by(Property.township, Property.county, Property.school_district_name, Property.city)
    )).all()
    total = sum(r[4] for r in rows)
    if not total:
        return {}

    def tally(idx: int) -> list[dict]:
        acc: dict[str, int] = {}
        for r in rows:
            if r[idx]:
                key = str(r[idx]).strip()
                acc[key] = acc.get(key, 0) + r[4]
        return [{"name": k, "homes": v, "share": round(v * 100 / total)} for k, v in sorted(acc.items(), key=lambda kv: -kv[1])]

    # Each township's county = the county most of its homes are recorded in.
    county_of: dict[str, dict[str, int]] = {}
    for r in rows:
        if r[0] and r[1]:
            c = county_of.setdefault(str(r[0]).strip(), {})
            c[str(r[1]).strip()] = c.get(str(r[1]).strip(), 0) + r[4]
    townships = tally(0)
    for t in townships:
        cs = county_of.get(t["name"]) or {}
        t["county"] = max(cs, key=cs.get) if cs else None
    return {"homes": total, "townships": townships, "counties": tally(1), "school_districts": tally(2), "mailing_towns": tally(3)}


async def older_homes_share(db: AsyncSession, scopes: list[dict], before: int = 1978) -> Optional[float]:
    """The share of homes for sale across these areas built before `before` (of those that list a
    year built) - None under 20 such listings. Triggers the federal lead-paint disclosure item."""
    filters = [f for f in (geography.location_filter(Property, s) for s in scopes) if f is not None]
    if not filters:
        return None
    years = (await db.execute(
        select(Property.year_built).where(or_(*filters), geography.residential_for_sale_filter(Property),
                                          Property.home_status.in_(FOR_SALE_STATUSES), Property.year_built.isnot(None), Property.year_built > 1700)
    )).scalars().all()
    return round(sum(1 for y in years if y < before) / len(years), 3) if len(years) >= 20 else None


async def entry_price_profile(db: AsyncSession, scopes: list[dict]) -> dict:
    """Homes for sale across several areas at once (each home once, even where the areas
    overlap): the 25th-percentile and median list price, and the median annual tax as a share of
    list price for homes priced near that 25th percentile - the inputs of an article's worked
    payment example."""
    filters = [f for f in (geography.location_filter(Property, s) for s in scopes) if f is not None]
    if not filters:
        return {}
    rows = (await db.execute(
        select(Property.price, mls_mirror.col(Property.tax_annual_amount, "TaxAnnualAmount"), Property.home_type,
               mls_mirror.col(Property.mls_structure_design_type, "StructureDesignType"))
        .where(or_(*filters), geography.residential_for_sale_filter(Property), Property.home_status.in_(FOR_SALE_STATUSES), Property.price.isnot(None))
    )).all()
    # Manufactured homes aren't counted as single-family houses - the same rule as the article's own
    # house count (get_area_cost_profile). 11/02 round 14 Lancaster: the card said 134 houses at $485K
    # while the article said 122 houses at $520K.
    rows = [(p, t, None if "manufactured" in str(s or "").lower() and ht in ("SINGLE_FAMILY", "Single Family") else ht) for p, t, ht, s in rows]
    # A move-up or single-family article's example is a typical single-family house, a condo /
    # townhome article's a typical attached home - never the cheapest quarter of every type (11/01
    # grader: a Frederick single-family article showed a $390,000 example while houses ran $600,000+).
    # Each type's tax estimate comes from that same type's listings.
    typed = {}
    for kind, types in (("house", ("SINGLE_FAMILY", "Single Family")), ("attached", ("CONDO", "TOWNHOUSE"))):
        typed[kind] = [(float(p), _num(t)) for p, t, ht in rows if p and ht in types]
    rows = [(p, t) for p, t, _ in rows]
    prices = sorted(float(p) for p, _ in rows if p)
    if len(prices) < 10:
        return {}
    p25 = prices[int(0.25 * (len(prices) - 1))]
    near = [(float(p), _num(t)) for p, t in rows if p and 0.75 * p25 <= float(p) <= 1.25 * p25]
    ratios = [t / p for p, t in near if t and t > 100]
    if len(ratios) < 5:
        ratios = [_num(t) / float(p) for p, t in rows if p and _num(t) and _num(t) > 100]
    out = {"homes": len(prices), "p25": round(p25), "median": round(_median(prices)),
           "tax_pct_near_p25": round(_median(ratios) * 100, 2) if len(ratios) >= 5 else None,
           "tax_homes": len(ratios)}
    for kind, homes in typed.items():
        if len(homes) < 10:
            continue
        km = _median([p for p, _ in homes])
        near_k = [t / p for p, t in homes if 0.75 * km <= p <= 1.25 * km and t and t > 100]
        if len(near_k) < 5:
            near_k = [t / p for p, t in homes if t and t > 100]
        out.update({("houses" if kind == "house" else "attached"): len(homes), f"{kind}_median": round(km),
                    f"tax_pct_near_{kind}": round(_median(near_k) * 100, 2) if len(near_k) >= 5 else None})
    return out
