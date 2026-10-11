"""Town-page market insights computed from our own MLS mirror (no outside calls), on top of
market_stats_service's trends:

  - money_buys: what each price range actually gets you here - median beds / baths / living area /
    lot / year built of homes that sold (last 24 months) or are for sale now, per price band.
  - gauge: buyer's / balanced / seller's market - months of supply (homes for sale now vs the
    average monthly sales pace over the last 6 months; the standard < 3 / 3-6 / > 6 rule), the share
    of the last 12 months' sales that closed over asking, median sale-to-list and days on market.
  - taxes: annual property tax per square foot of living area (owner's call - by size, not by
    price), overall and per school district (in Pennsylvania most of the bill is the school tax, so
    a town split between districts has two very different rates), plus what that means for a
    typical-size home here.

Every figure says how many homes it's based on; bands / districts with too few homes are left out
rather than shown as a shaky number. Residential for-sale homes only (no rentals, land,
commercial). Grows sharper once the 3-year sold load is in production.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property
from app.services.areas import geography
from app.services.open_house_manager_service import INACTIVE_STATUSES

_SOLD = ("CLOSED-BRIGHT", "CLOSED")
_BANDS = [0, 300_000, 500_000, 750_000, 1_000_000, 1_500_000, 2_000_000, 3_000_000, 5_000_000, None]
MIN_PER_BAND = 5
MIN_PER_DISTRICT = 8


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _median(vals: list[float]) -> Optional[float]:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def _band_label(lo: float, hi: Optional[float]) -> str:
    def k(n):
        return f"${n / 1_000_000:g}M" if n >= 1_000_000 else f"${int(n / 1000)}K"
    if not lo:
        return f"Under {k(hi)}"
    return f"{k(lo)}+" if hi is None else f"{k(lo)} - {k(hi)}"


def _district_name(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    name = raw.strip().title()
    return name if "school" in name.lower() else f"{name} School District"


def _half(v: Optional[float]) -> Optional[float]:
    return round(v * 2) / 2 if v is not None else None


async def get_area_insights(db: AsyncSession, scope: dict, district_names: Optional[list[str]] = None) -> dict:
    """district_names: the town's checked school-district names, so Bright's different spellings
    of one district ("PHILADELPHIA CITY" / "THE SCHOOL DISTRICT OF PHILADELPHIA") land together
    under the name the page already uses."""
    from app.services.website.content_adapters import _district_key
    known = {_district_key(n): n for n in (district_names or []) if n}

    def district_label(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        k = _district_key(raw)
        hit = next((name for kk, name in known.items() if kk == k or kk in k or k in kk), None)
        return hit or _district_name(raw)

    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return {}
    resid = geography.residential_for_sale_filter(Property)
    close_date = Property.raw_mls_data["CloseDate"].astext
    today = date.today()
    since_24 = (today - timedelta(days=730)).isoformat()

    sold = (await db.execute(
        select(close_date, Property.raw_mls_data["ClosePrice"].astext, Property.raw_mls_data["ListPrice"].astext,
               Property.raw_mls_data["DaysOnMarket"].astext, Property.bedrooms, Property.bathrooms, Property.living_area,
               Property.lot_size_acres, Property.year_built, Property.tax_annual_amount, Property.school_district_name)
        .where(location_filter, resid, Property.home_status.in_(_SOLD), close_date >= since_24)
    )).all()
    active = (await db.execute(
        select(Property.price, Property.bedrooms, Property.bathrooms, Property.living_area, Property.lot_size_acres,
               Property.year_built, Property.tax_annual_amount, Property.school_district_name)
        .where(location_filter, resid, Property.home_status.notin_(INACTIVE_STATUSES), Property.home_status.notin_(_SOLD))
    )).all()

    # One row shape for both: (price, beds, baths, sqft, acres, year, tax, district)
    homes = []
    since_12 = (today - timedelta(days=365)).isoformat()
    since_6 = (today - timedelta(days=183)).isoformat()
    closed_12 = closed_6 = over = 0
    ratios, doms = [], []
    for cd, cp, lp, dom, beds, baths, sqft, acres, year, tax, dist in sold:
        price = _f(cp)
        if not price or price < geography.MIN_PLAUSIBLE_SALE_PRICE:
            continue
        homes.append((price, beds, baths, sqft, acres, year, tax, dist))
        day = str(cd or "")[:10]
        if day >= since_12:
            closed_12 += 1
            lpv = _f(lp)
            if lpv and lpv > 0:
                ratios.append(price / lpv)
                if price > lpv:
                    over += 1
            if _f(dom) is not None:
                doms.append(_f(dom))
        if day >= since_6:
            closed_6 += 1
    for price, beds, baths, sqft, acres, year, tax, dist in active:
        if price and price >= geography.MIN_PLAUSIBLE_SALE_PRICE:
            homes.append((float(price), beds, baths, sqft, acres, year, tax, dist))

    out: dict = {"computed_at": datetime.now(timezone.utc).isoformat(), "homes_counted": len(homes)}

    # ---- What your money buys
    bands = []
    for lo, hi in zip(_BANDS[:-1], _BANDS[1:]):
        rows = [h for h in homes if h[0] >= lo and (hi is None or h[0] < hi)]
        if len(rows) < MIN_PER_BAND:
            continue
        bands.append({
            "label": _band_label(lo, hi), "min": lo, "max": hi, "homes": len(rows),
            "beds": round(_median([_f(r[1]) for r in rows]) or 0) or None,
            "baths": _half(_median([_f(r[2]) for r in rows])),
            "sqft": round(_median([_f(r[3]) for r in rows if _f(r[3])]) or 0) or None,
            # Condos / rowhomes carry tiny or token lot sizes - only worth showing from ~1/20 acre.
            "acres": (lambda a: round(a, 2) if a and a >= 0.05 else None)(_median([_f(r[4]) for r in rows if _f(r[4])])),
            "year_built": round(_median([_f(r[5]) for r in rows if _f(r[5]) and _f(r[5]) > 1700]) or 0) or None,
        })
    if len(bands) >= 2:
        out["money_buys"] = bands[-6:]

    # ---- Buyer's / seller's market
    active_count = len([a for a in active if a[0] and a[0] >= geography.MIN_PLAUSIBLE_SALE_PRICE])
    if closed_12 >= 6:
        pace = closed_6 / 6 if closed_6 else closed_12 / 12
        months = round(active_count / pace, 1) if pace else None
        if months is not None:
            label = "Seller's market" if months < 3 else "Buyer's market" if months > 6 else "Balanced market"
        else:
            label = None
        out["gauge"] = {
            "label": label,
            "months_of_supply": months,
            "active_count": active_count,
            "sales_per_month": round(pace, 1) if pace else None,
            "over_asking_pct": round(100 * over / len(ratios)) if ratios else None,
            "median_sale_to_list_pct": round(_median(ratios) * 100, 1) if ratios else None,
            "median_days_on_market": round(_median(doms)) if doms else None,
            "sales_counted": closed_12,
        }

    # ---- Property taxes per square foot
    taxed = [(h[6] / h[3], h[3], h[7]) for h in homes if _f(h[6]) and _f(h[3]) and h[3] >= 400 and 0.2 <= h[6] / h[3] <= 60]
    if len(taxed) >= MIN_PER_DISTRICT:
        per_sqft = _median([t[0] for t in taxed])
        typical_sqft = round((_median([t[1] for t in taxed]) or 0) / 100) * 100
        by_district: dict[str, list[float]] = {}
        for rate, _, dist in taxed:
            if district_label(dist):
                by_district.setdefault(district_label(dist), []).append(rate)
        districts = sorted(
            ({"name": n, "per_sqft": round(_median(v), 2), "homes": len(v)} for n, v in by_district.items() if len(v) >= MIN_PER_DISTRICT),
            key=lambda d: -d["homes"],
        )
        out["taxes"] = {
            "per_sqft": round(per_sqft, 2),
            "typical_sqft": typical_sqft or None,
            "typical_annual": round(per_sqft * typical_sqft / 50) * 50 if typical_sqft else None,
            "homes": len(taxed),
            "districts": districts if len(districts) > 1 else [],
        }
    return out


async def get_area_municipalities(db: AsyncSession, scope: dict) -> list[dict]:
    """Which townships/boroughs this town's addresses actually sit in, by share of homes - a
    mailing-address town (how buyers search) can span two municipalities (Haverford, PA: Lower
    Merion Township and Haverford Township)."""
    if not hasattr(Property, "township"):
        return []
    from sqlalchemy import func
    location_filter = geography.location_filter(Property, scope)
    if location_filter is None:
        return []
    rows = (await db.execute(
        select(Property.township, func.count()).where(location_filter, Property.township.isnot(None)).group_by(Property.township)
    )).all()
    total = sum(c for _, c in rows)
    if total < 20:
        return []
    out = [{"name": str(t).strip().title(), "share": round(100 * c / total)} for t, c in rows if t and c / total >= 0.05]
    return sorted(out, key=lambda m: -m["share"]) if len(out) > 1 else []
