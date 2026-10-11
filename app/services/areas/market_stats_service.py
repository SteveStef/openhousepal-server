"""Area-level market statistics computed entirely from our own local `properties` table - no
Bright MLS calls (see module docstrings across app/services/areas/ for why: the website layer
must never touch Bright directly, per the Website Builder architecture). This is intentionally
NOT a reuse of cma_service.search_comparables (a radius comp search around one property with no
rollup) - it's a new, small, aggregate query, the kind of thing that genuinely didn't exist
anywhere in this codebase yet (confirmed during Phase 3 planning research).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property
from app.services.areas import geography
from app.services.open_house_manager_service import INACTIVE_STATUSES

_SOLD_STATUSES = {"CLOSED-BRIGHT", "CLOSED"}


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
            func.percentile_cont(0.5).within_group(Property.days_on_market.asc()),
            func.avg(Property.price_per_square_feet),
            func.min(Property.price),
            func.max(Property.price),
        ).where(location_filter, for_sale, Property.home_status.notin_(INACTIVE_STATUSES))
    )).first()

    sold_row = (await db.execute(
        select(
            func.count(Property.id),
            func.percentile_cont(0.5).within_group(Property.price.asc()),
        ).where(location_filter, for_sale, Property.home_status.in_(_SOLD_STATUSES))
    )).first()

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
    if not active_count and not sold_count:
        return {}

    stats: dict = {"source": "ohp_local"}
    if active_count:
        stats["active_count"] = active_count
        if active_row[1] is not None:
            stats["median_price"] = round(float(active_row[1]))
        if active_row[2] is not None:
            stats["median_dom"] = round(float(active_row[2]))
        if active_row[3] is not None:
            stats["avg_price_per_sqft"] = round(float(active_row[3]))
        if active_row[4] is not None:
            stats["min_price"] = round(float(active_row[4]))
        if active_row[5] is not None:
            stats["max_price"] = round(float(active_row[5]))
    if sold_count:
        stats["recently_sold_count"] = sold_count
        if sold_row[1] is not None:
            stats["median_sold_price"] = round(float(sold_row[1]))
    if district_rows:
        stats["school_districts"] = sorted(district_rows)

    if len(stats) <= 1:  # only "source" present - nothing usable came back
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
    close_date = Property.raw_mls_data["CloseDate"].astext
    today = date.today()
    first_of_month = today.replace(day=1)
    # First day of the month `months-1` months ago, then the same again one year earlier.
    y, m = first_of_month.year, first_of_month.month - (months - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    window_start = date(y, m, 1)
    prior_start = date(y - 1, m, 1)

    rows = (await db.execute(
        select(close_date, Property.raw_mls_data["ClosePrice"].astext, Property.raw_mls_data["ListPrice"].astext,
               Property.raw_mls_data["DaysOnMarket"].astext, Property.living_area)
        .where(location_filter, resid, Property.home_status.in_(_SOLD_STATUSES), close_date >= prior_start.isoformat())
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
        elif day >= prior_start.isoformat() and day < (date(prior_start.year + 1, prior_start.month, 1)).isoformat():
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
    median_prior = _median(prior)
    yoy = round(((median_now - median_prior) / median_prior) * 100, 1) if median_now and median_prior else None

    active_rows = (await db.execute(
        select(Property.price, Property.home_type, Property.mls_list_date)
        .where(location_filter, resid, Property.home_status.notin_(INACTIVE_STATUSES), Property.home_status.notin_(_SOLD_STATUSES))
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


def _median(values: list[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


async def get_area_sales_snapshot(db: AsyncSession, scope: dict, days: int = 90) -> dict:
    """A DATED closed-sales snapshot for articles (unlike get_area_market_stats, whose sold numbers
    span every sale on record): closed sales in the `days` window ending on the newest CloseDate in
    our data, the same window one year earlier, and today's active inventory. Every figure carries
    the window it covers, so an article can say "in the 90 days ending Sep 10, 2026". For-sale
    only (no rentals, nothing under $50K)."""
    from datetime import date, timedelta

    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    close_date = Property.raw_mls_data["CloseDate"].astext
    base = (location_filter, Property.home_type != "RESIDENTIAL_LEASE", Property.home_status.in_(_SOLD_STATUSES))
    # One window end for every town (the newest close date anywhere in our data), so several towns
    # in one article share the same "90 days ending ..." - not each town's own last sale.
    latest = (await db.execute(select(func.max(close_date)).where(Property.home_status.in_(_SOLD_STATUSES), Property.home_type != "RESIDENTIAL_LEASE"))).scalar()
    if not latest:
        return {}
    end = date.fromisoformat(str(latest)[:10])

    async def window(start: date, stop: date) -> dict:
        rows = (await db.execute(
            select(Property.raw_mls_data["ClosePrice"].astext, Property.raw_mls_data["ListPrice"].astext, Property.raw_mls_data["DaysOnMarket"].astext)
            .where(*base, close_date > start.isoformat(), close_date <= stop.isoformat())
        )).all()
        prices, ratios, doms = [], [], []
        for cp, lp, dom in rows:
            try:
                cpv = float(cp) if cp else None
            except ValueError:
                cpv = None
            if not cpv or cpv < 50_000:
                continue
            prices.append(cpv)
            try:
                if lp and float(lp) > 0:
                    ratios.append(cpv / float(lp))
            except ValueError:
                pass
            try:
                if dom is not None:
                    doms.append(float(dom))
            except ValueError:
                pass
        out: dict = {"closed_count": len(prices), "from": (start + timedelta(days=1)).isoformat(), "to": stop.isoformat()}
        if prices:
            out["median_close_price"] = round(_median(prices))
        if ratios:
            out["median_sale_to_list_pct"] = round(_median(ratios) * 100, 1)
        if doms:
            out["median_days_on_market"] = round(_median(doms))
        return out

    current = await window(end - timedelta(days=days), end)
    prior_end = end - timedelta(days=365)
    prior = await window(prior_end - timedelta(days=days), prior_end)

    active = (await db.execute(
        select(func.count(Property.id), func.percentile_cont(0.5).within_group(Property.price.asc()))
        .where(location_filter, Property.home_type != "RESIDENTIAL_LEASE", Property.home_status.notin_(INACTIVE_STATUSES),
               Property.home_status.notin_(_SOLD_STATUSES), Property.price >= 50_000)
    )).first()
    return {
        "window_days": days,
        "data_through": end.isoformat(),
        "current": current,
        "prior_year": prior,
        "active_count": active[0] or 0,
        "active_median_list_price": round(float(active[1])) if active[1] is not None else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
