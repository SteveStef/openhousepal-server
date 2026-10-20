"""A home's price history: every listing it has had in our MLS mirror (stitched by parcel), plus
every price / status change the sync logged (property_price_events).

What Bright gives per listing: the list date (MLSListDate), the current list price, the date of
the latest price change (without the old price - OriginalListPrice / PreviousListPrice are never
filled in our feed), the contract date when present, and the close date + sold price. Exact
"was $X, now $Y" changes come from our own log, from the day it started.

Two parts:
  get_history()   - the timeline for one listing (read-only, no Bright calls)
  backfill_batch() - pulls a batch of parcels' 2012+ listings from Bright into the mirror
                     (off unless PRICE_HISTORY_BACKFILL=true; slow, resumable, never alerts anyone)
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PriceHistoryParcel, Property, PropertyPriceEvent

logger = logging.getLogger(__name__)

_CURRENT_STATUSES = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "PENDING-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT")
_OFF_STATUSES = ("WITHDRAWN-BRIGHT", "EXPIRED-BRIGHT", "CANCELED-BRIGHT", "OFF_MARKET-BRIGHT", "TEMP OFF MARKET-BRIGHT")
# Placeholder parcel ids shared by thousands of unrelated listings ("NO TAX RECORD", "0", "000.00-000.00").
_MAX_LISTINGS_PER_PARCEL = 25


def usable_tax_id(tax_id: Optional[str]) -> bool:
    t = (tax_id or "").strip()
    return len(t) >= 6 and bool(re.search(r"[1-9]", t)) and "no tax" not in t.lower()


def _unit(street: Optional[str]) -> str:
    """'431 N 5TH ST UNIT 2B' -> '2b', '12 OAK ST #305' -> '305' ('' when none)."""
    m = re.search(r"(?:#|\bunit\b|\bapt\b|\bste\b)\s*\[?([a-z0-9-]+)", (street or "").lower())
    return m.group(1) if m else ""


def _street_key(street: Optional[str]) -> str:
    """'14 UNITED MEWS #[534]' -> '14 united mews' (unit and punctuation dropped) - two listings are
    the same home only if this matches, so a parcel id shared by a whole development never mixes
    in the neighbors' townhouses."""
    s = re.sub(r"\s*(#|\bunit\b|\bapt\b|\bste\b).*$", "", (street or "").lower())
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", s)).strip()


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v)
    return s[:10] if len(s) >= 10 else None


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _sibling_listings(db: AsyncSession, subject: Property) -> list[Property]:
    """Every listing of the same home: same parcel id (and same house number + ZIP, so a shared
    or junk parcel id never mixes in a neighbor), else the same street address + ZIP."""
    q = select(Property)
    if usable_tax_id(subject.listing_tax_id):
        n = (await db.execute(select(func.count(Property.id)).where(Property.listing_tax_id == subject.listing_tax_id))).scalar_one()
        if n <= _MAX_LISTINGS_PER_PARCEL:
            q = q.where(Property.listing_tax_id == subject.listing_tax_id)
        else:
            q = q.where(Property.id == subject.id)
    elif subject.street_address and subject.zipcode:
        q = q.where(func.lower(Property.street_address) == subject.street_address.lower(), Property.zipcode == subject.zipcode)
    else:
        q = q.where(Property.id == subject.id)
    rows = (await db.execute(q.limit(_MAX_LISTINGS_PER_PARCEL))).scalars().all()
    sk, su, z = _street_key(subject.street_address), _unit(subject.street_address), subject.zipcode

    def same_home(r: Property) -> bool:
        if r.id == subject.id:
            return True
        ru = _unit(r.street_address)
        # different apartments in one building share the street address - the unit must match too
        return (not sk or _street_key(r.street_address) == sk) and (not z or r.zipcode == z) and (not su or not ru or su == ru)
    return [r for r in rows if same_home(r)]


async def get_history(db: AsyncSession, listing_key: str) -> list[dict]:
    """Newest first: [{date, event, price, change, change_pct}] - event is one of "Listed for sale",
    "Price reduced", "Price increased", "Price changed", "Under contract", "Sold", "Listing removed",
    "Coming soon". Prices the MLS doesn't give are None (never guessed)."""
    subject = (await db.execute(select(Property).where(Property.listing_key == str(listing_key)))).scalar_one_or_none()
    if not subject:
        return []
    listings = await _sibling_listings(db, subject)
    keys = [p.listing_key for p in listings if p.listing_key]
    logged = (await db.execute(
        select(PropertyPriceEvent).where(PropertyPriceEvent.listing_key.in_(keys)).order_by(PropertyPriceEvent.observed_at)
    )).scalars().all() if keys else []
    by_key: dict[str, list[PropertyPriceEvent]] = {}
    for e in logged:
        by_key.setdefault(e.listing_key, []).append(e)

    events: list[dict] = []
    for p in listings:
        raw = p.raw_mls_data or {}
        # A seller who kept the listing off the internet stays off the history too.
        if raw.get("InternetEntireListingDisplayYN") is False:
            continue
        status = (p.home_status or "").upper()
        rental = (p.home_type or "").upper() == "RESIDENTIAL_LEASE"
        mine = by_key.get(p.listing_key, [])
        price_changes = [e for e in mine if e.kind == "price" and e.price and e.previous_price]
        first_price = price_changes[0].previous_price if price_changes else p.price
        listed = _iso(p.mls_list_date)
        if listed:
            label = "Listed for rent" if rental else ("Coming soon" if status.startswith("COMING SOON") and not price_changes else "Listed for sale")
            events.append({"date": listed, "event": label, "price": first_price if (price_changes or not p.price_change_timestamp) else None,
                           "rental": rental})
        for e in price_changes:
            delta = e.price - e.previous_price
            events.append({"date": _iso(e.observed_at), "event": "Price reduced" if delta < 0 else "Price increased", "price": e.price,
                           "change": delta, "change_pct": round(delta / e.previous_price * 100, 1)})
        if not price_changes and p.price_change_timestamp and _iso(p.price_change_timestamp) != listed:
            # Bright says the price changed but not from what - show the date and today's price only.
            events.append({"date": _iso(p.price_change_timestamp), "event": "Price changed", "price": p.price})
        contract = _iso(raw.get("PurchaseContractDate"))
        if contract:
            events.append({"date": contract, "event": "Under contract", "price": p.price})
        if status.startswith("CLOSED"):
            # The sold date/price ride on Bright's closed-status records; older rows can lack them -
            # then the day it closed in the MLS and no price (never the old asking price as "sold").
            close_date = _iso(raw.get("CloseDate")) or _iso(raw.get("StatusChangeTimestamp")) or _iso(p.modification_timestamp)
            if close_date:
                events.append({"date": close_date, "event": "Rented" if rental else "Sold", "price": _num(raw.get("ClosePrice")), "rental": rental})
        if status in _OFF_STATUSES:
            off = _iso(raw.get("OffMarketDate")) or _iso(raw.get("StatusChangeTimestamp")) or _iso(p.modification_timestamp)
            if off:
                events.append({"date": off, "event": "Listing removed", "price": p.price})
    seen, out = set(), []
    for ev in sorted(events, key=lambda x: (x["date"] or "", x["event"] != "Listed for sale"), reverse=True):
        k = (ev["date"], ev["event"], ev.get("price"))
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return out[:40]


# ------------------------------------------------------------------ 2012+ history backfill

def backfill_enabled() -> bool:
    return os.getenv("PRICE_HISTORY_BACKFILL", "").lower() == "true"


async def backfill_batch(db: AsyncSession, max_parcels: int = 300, pause_seconds: float = 0.6) -> dict:
    """Pull every 2012+ listing Bright has for up to `max_parcels` parcels that have a listing on
    the market now and haven't been pulled yet. Inserts them into the mirror (no client alerts, no
    Showcase matching - history only), marks each parcel done. One Bright request per parcel with
    a pause between, so it runs quietly in the background over a few days."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService

    rows = (await db.execute(text("""
        SELECT DISTINCT p.listing_tax_id FROM properties p
        LEFT JOIN price_history_parcels h ON h.tax_id = p.listing_tax_id
        WHERE p.home_status = ANY(:current) AND h.tax_id IS NULL
          AND length(p.listing_tax_id) >= 6 AND p.listing_tax_id ~ '[1-9]' AND p.listing_tax_id !~* 'no tax'
        ORDER BY p.listing_tax_id
        LIMIT :n"""), {"current": list(_CURRENT_STATUSES), "n": max_parcels})).scalars().all()
    syncer = PropertySyncService()
    done = added = failed = 0
    for tax_id in rows:
        try:
            listings = await bright_mls_service.get_listings_for_tax_id(tax_id)
        except Exception as e:  # noqa: BLE001 - one parcel failing (Bright blip) never stops the batch
            failed += 1
            logger.warning("price history backfill: parcel lookup failed", extra={"tax_id": tax_id})
            # Bright refusing the query itself (a 400) will refuse it every time - record the parcel
            # as checked so it isn't retried at the front of every batch forever. A blip (5xx,
            # timeout) is left for the next batch.
            if "400" in str(getattr(e, "detail", e)):
                db.add(PriceHistoryParcel(tax_id=tax_id, listings_found=None, checked_at=datetime.now(timezone.utc)))
                await db.commit()
            await asyncio.sleep(pause_seconds * 4)
            continue
        if len(listings) <= _MAX_LISTINGS_PER_PARCEL:
            for raw in listings:
                try:
                    # New listings are added; ones we already have are refreshed too - the same pass
                    # fills in the internet-display flags, Coming Soon dates and missing sold prices.
                    known = (await db.execute(select(Property.id).where(Property.listing_key == str(raw.get("ListingKey"))))).scalar()
                    await syncer._sync_single_property(db, raw, {}, allow_inactive_insert=True)
                    if not known:
                        added += 1
                except Exception:  # noqa: BLE001
                    await db.rollback()
                    logger.warning("price history backfill: listing insert failed", exc_info=True, extra={"tax_id": tax_id})
        db.add(PriceHistoryParcel(tax_id=tax_id, listings_found=len(listings), checked_at=datetime.now(timezone.utc)))
        await db.commit()
        done += 1
        await asyncio.sleep(pause_seconds)
    result = {"parcels": done, "listings_added": added, "failed": failed, "remaining_in_batch": len(rows) - done - failed}
    logger.info("price history backfill batch", extra={"event": "price_history_backfill", **result})
    return result
