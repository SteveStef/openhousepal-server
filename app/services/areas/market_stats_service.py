"""Area-level market statistics computed entirely from our own local `properties` table - no
Bright MLS calls (see module docstrings across app/services/areas/ for why: the website layer
must never touch Bright directly, per the Website Builder architecture). This is intentionally
NOT a reuse of cma_service.search_comparables (a radius comp search around one property with no
rollup) - it's a new, small, aggregate query, the kind of thing that genuinely didn't exist
anywhere in this codebase yet (confirmed during Phase 3 planning research).
"""
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
    close_price = cast(func.nullif(Property.raw_mls_data["ClosePrice"].astext, ""), Numeric)
    range_row = (await db.execute(
        select(
            func.count(close_price),
            func.percentile_cont(0.05).within_group(close_price.asc()),
            func.percentile_cont(0.95).within_group(close_price.asc()),
        ).where(
            location_filter, for_sale, Property.home_status.in_(_SOLD_STATUSES),
            Property.raw_mls_data["CloseDate"].astext >= (date.today() - timedelta(days=730)).isoformat(),
            or_(Property.living_area.is_(None), Property.living_area >= 800),
        )
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
# depend on the town - computed once per 10 minutes, not once per town per article.
_GLOBAL_TTL_S = 600
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
    close_date = Property.raw_mls_data["CloseDate"].astext

    async def compute():
        return (await db.execute(select(func.count(Property.id)).where(
            geography.residential_for_sale_filter(Property), Property.home_status.in_(_SOLD_STATUSES), close_date > start, close_date <= stop,
        ))).scalar() or 0
    return await _global(("closed_all", start, stop), compute)


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
    close_date = Property.raw_mls_data["CloseDate"].astext
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

    async def window(start: date, stop: date) -> dict:
        rows = (await db.execute(
            select(Property.raw_mls_data["ClosePrice"].astext, Property.raw_mls_data["ListPrice"].astext, Property.raw_mls_data["DaysOnMarket"].astext)
            .where(*base, close_date > start.isoformat(), close_date <= stop.isoformat())
        )).all()
        prices, ratios, doms = [], [], []
        for cp, lp, dom in rows:
            cpv = _num(cp)
            if not cpv or cpv < geography.MIN_PLAUSIBLE_SALE_PRICE:
                continue
            prices.append(cpv)
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
        if doms:
            out["median_days_on_market"] = round(_median(doms))
        return out

    current = await window(end - timedelta(days=days), end)
    prior_end = end - timedelta(days=365)
    prior = await window(prior_end - timedelta(days=days), prior_end)
    # Coverage check across ALL our closed sales (not just this town, which can be tiny): if the
    # mirror holds under 60% as many closings a year back, last year's numbers are a data gap.
    now_all = await _closed_count_all(db, (end - timedelta(days=days)).isoformat(), end.isoformat())
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


async def get_area_cost_profile(db: AsyncSession, scope: dict) -> dict:
    """What owning a home here costs, from the listings themselves (Bright MLS fields agents fill
    in): annual property tax on homes for sale (overall, as a share of list price, and per
    township - the township decides the millage), condo/townhome association fees, and how buyers
    paid for the homes that closed in the last 12 months (BuyerFinancing). Medians only, and only
    from groups of 5+ homes, so one odd listing can't make a headline."""
    from collections import Counter
    from datetime import date, timedelta

    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    resid = geography.residential_for_sale_filter(Property)
    rows = (await db.execute(
        select(Property.price, Property.township, Property.home_type, Property.raw_mls_data["TaxAnnualAmount"].astext,
               Property.raw_mls_data["TaxYear"].astext, Property.raw_mls_data["AssociationFee"].astext,
               Property.raw_mls_data["AssociationFeeFrequency"].astext)
        .where(location_filter, resid, Property.home_status.in_(FOR_SALE_STATUSES))
    )).all()
    taxes, ratios, years, hoa = [], [], Counter(), []
    by_town: dict[str, list[float]] = {}
    for price, township, home_type, tax, tax_year, fee, freq in rows:
        tax_v, price_v = _num(tax), _num(price)
        if tax_v and tax_v > 100 and price_v and price_v >= geography.MIN_PLAUSIBLE_SALE_PRICE:
            taxes.append(tax_v)
            ratios.append(tax_v / price_v)
            if tax_year:
                years[str(tax_year)[:4]] += 1
            if township:
                by_town.setdefault(str(township).strip().title(), []).append(tax_v / price_v)
        if home_type in ("CONDO", "TOWNHOUSE"):
            monthly = _fee_monthly(_num(fee), freq)
            if monthly:
                hoa.append(monthly)
    out: dict = {"homes_for_sale": len(rows), "as_of": date.today().isoformat()}
    if len(taxes) >= 5:
        out["tax"] = {
            "homes": len(taxes), "median_annual": round(_median(taxes)), "median_pct_of_list": round(_median(ratios) * 100, 2),
            "tax_year": years.most_common(1)[0][0] if years else None,
            "by_township": sorted(({"township": t, "homes": len(v), "median_pct_of_list": round(_median(v) * 100, 2)}
                                   for t, v in by_town.items() if len(v) >= 5), key=lambda r: -r["homes"]),
        }
    if len(hoa) >= 5:
        out["hoa"] = {"homes": len(hoa), "median_monthly": round(_median(hoa))}

    since = (date.today() - timedelta(days=365)).isoformat()
    fin_rows = (await db.execute(
        select(Property.raw_mls_data["BuyerFinancing"]).where(
            location_filter, resid, Property.home_status.in_(_SOLD_STATUSES), Property.raw_mls_data["CloseDate"].astext > since)
    )).scalars().all()
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
    if recorded >= 20 and recorded >= 0.3 * len(fin_rows):
        out["financing"] = {
            "closed_sales": len(fin_rows), "recorded": recorded, "since": since,
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


async def entry_price_profile(db: AsyncSession, scopes: list[dict]) -> dict:
    """Homes for sale across several areas at once (each home once, even where the areas
    overlap): the 25th-percentile and median list price, and the median annual tax as a share of
    list price for homes priced near that 25th percentile - the inputs of an article's worked
    payment example."""
    filters = [f for f in (geography.location_filter(Property, s) for s in scopes) if f is not None]
    if not filters:
        return {}
    rows = (await db.execute(
        select(Property.price, Property.raw_mls_data["TaxAnnualAmount"].astext)
        .where(or_(*filters), geography.residential_for_sale_filter(Property), Property.home_status.in_(FOR_SALE_STATUSES), Property.price.isnot(None))
    )).all()
    prices = sorted(float(p) for p, _ in rows if p)
    if len(prices) < 10:
        return {}
    p25 = prices[int(0.25 * (len(prices) - 1))]
    near = [(float(p), _num(t)) for p, t in rows if p and 0.75 * p25 <= float(p) <= 1.25 * p25]
    ratios = [t / p for p, t in near if t and t > 100]
    if len(ratios) < 5:
        ratios = [_num(t) / float(p) for p, t in rows if p and _num(t) and _num(t) > 100]
    return {"homes": len(prices), "p25": round(p25), "median": round(_median(prices)),
            "tax_pct_near_p25": round(_median(ratios) * 100, 2) if len(ratios) >= 5 else None,
            "tax_homes": len(ratios)}
