import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Property
from app.services.usage_owner import charge_google


def _own_session():
    """A short session of its own for this module's writes (stand-in in tests)."""
    from app.database import AsyncSessionLocal
    return AsyncSessionLocal()


async def _save(property_record: Property, **values) -> None:
    """Save the geocode on its own session and copy it onto the caller's home without making it dirty.
    Never commits the CALLER's session: that commit expired every object the request had already loaded
    (the agent, the Showcase...) and the next plain read of one failed with a 500 - the first open of a
    brand-new listing's details (Day 7 audit, 11/19: New Matches mailbox)."""
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value
    async with _own_session() as s:
        await s.execute(update(Property).where(Property.id == property_record.id).values(**values))
        await s.commit()
    for k, v in values.items():
        try:
            set_committed_value(property_record, k, v)
        except Exception:  # noqa: BLE001 - a plain stand-in object (tests)
            setattr(property_record, k, v)


logger = get_logger(__name__)

GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def _property_address(property_record: Property) -> str:
    return (
        property_record.full_address
        or ", ".join(part for part in [
            property_record.street_address,
            property_record.city,
            property_record.state,
            property_record.zipcode,
        ] if part)
    )


async def _usable(db: AsyncSession, property_record: Property) -> Property:
    """The home, readable again. The daily-count reservation below rolls the session back when the cap is
    reached, and a rollback marks every loaded field of the home as "to be re-read" - the caller then read one
    outside an await and the whole page failed with a 500 (found 11/14 making a test CMA: on a day the 400-a-day
    address-lookup cap was reached, every home without a stored map point returned "Something went wrong" - the
    CMA's "use this listing" step, and an agent's website home page - instead of simply keeping the MLS's point)."""
    try:
        from sqlalchemy import inspect as _inspect
        if _inspect(property_record).expired_attributes:
            await db.refresh(property_record)
    except Exception:  # noqa: BLE001 - the caller's own read will say what is wrong
        logger.warning("could not re-read a home after a rolled-back geocode reservation", exc_info=True)
    return property_record


async def ensure_google_rooftop_geocode(db: AsyncSession, property_record: Property) -> Property:
    """
    Cache a Google ROOFTOP geocode for the property.
    If Google only returns an interpolated/approximate point, leave Bright MLS coordinates in place.
    """
    if property_record.google_rooftop_latitude is not None and property_record.google_rooftop_longitude is not None:
        return property_record
    # Asked before and Google had no rooftop point: don't ask again on every view (11/09 money audit - a
    # home Google only places approximately was re-geocoded, and billed, each time its page opened).
    # Retried after 180 days in case Google's map improved.
    tried = property_record.google_geocoded_at
    if tried is not None:
        tried = tried if tried.tzinfo else tried.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - tried).days < 180:
            return property_record

    api_key = os.getenv("GOOGLE_GEOCODING_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        logger.info("Google geocoding skipped: no API key configured")
        return property_record

    address = _property_address(property_record)
    if not address:
        return property_record

    # Home pages on agents' websites call this for VISITORS (and search-engine robots) - a day/month count
    # cap under Google's monthly free geocoding allowance (owner rule: Google free tier only). Over it the
    # home just keeps the MLS's own map point.
    try:
        from app.services import usage_budget_service
        from app.services.ai.ai_config_service import ai_config_service
        # The daily-count reservation commits (or rolls back over the cap) - on its own session, so the
        # caller's loaded objects stay readable (see _save).
        async with _own_session() as own:
            config = await ai_config_service.get_config(own)
            await usage_budget_service.reserve(own, {
                "google_geocode": (config.get("google_geocode_daily_limit", 400), config.get("google_geocode_monthly_limit", 8000), None, None),
                "platform_emergency": (None, None, config["platform_emergency_daily_cost_ceiling_usd"], config["platform_emergency_monthly_cost_ceiling_usd"]),
            }, config.get("google_geocode_estimated_cost_usd", 0.005), count_delta=1)
    except Exception:  # noqa: BLE001 - over the cap (BudgetExceeded) or the counter failed: no Google call
        return await _usable(db, property_record)

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(GEOCODING_URL, params={"address": address, "key": api_key})
            response.raise_for_status()
            payload = response.json()
        await charge_google("geocoding")
    except Exception as exc:
        property_record = await _usable(db, property_record)
        logger.warning("Google geocoding request failed", extra={"listing_key": property_record.listing_key, "error": str(exc)})
        return property_record

    results = payload.get("results") or []
    best: Optional[dict] = next(
        (result for result in results if (result.get("geometry") or {}).get("location_type") == "ROOFTOP"),
        None,
    )
    if not best:
        location_type = ((results[0].get("geometry") or {}).get("location_type") if results else None)
        await _save(property_record, google_geocode_location_type=location_type, google_geocoded_at=datetime.now(timezone.utc))
        return property_record

    location = (best.get("geometry") or {}).get("location") or {}
    lat = location.get("lat")
    lng = location.get("lng")
    if lat is None or lng is None:
        return await _usable(db, property_record)

    await _save(property_record, google_rooftop_latitude=float(lat), google_rooftop_longitude=float(lng),
                google_geocode_location_type="ROOFTOP", google_geocoded_at=datetime.now(timezone.utc))
    return property_record
