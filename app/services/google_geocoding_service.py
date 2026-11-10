import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Property
from app.services.usage_owner import charge_google


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
        config = await ai_config_service.get_config(db)
        await usage_budget_service.reserve(db, {
            "google_geocode": (config.get("google_geocode_daily_limit", 400), config.get("google_geocode_monthly_limit", 8000), None, None),
            "platform_emergency": (None, None, config["platform_emergency_daily_cost_ceiling_usd"], config["platform_emergency_monthly_cost_ceiling_usd"]),
        }, config.get("google_geocode_estimated_cost_usd", 0.005), count_delta=1)
    except Exception:  # noqa: BLE001 - over the cap (BudgetExceeded) or the counter failed: no Google call
        return property_record

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(GEOCODING_URL, params={"address": address, "key": api_key})
            response.raise_for_status()
            payload = response.json()
        await charge_google("geocoding")
    except Exception as exc:
        logger.warning("Google geocoding request failed", extra={"listing_key": property_record.listing_key, "error": str(exc)})
        return property_record

    results = payload.get("results") or []
    best: Optional[dict] = next(
        (result for result in results if (result.get("geometry") or {}).get("location_type") == "ROOFTOP"),
        None,
    )
    if not best:
        location_type = ((results[0].get("geometry") or {}).get("location_type") if results else None)
        property_record.google_geocode_location_type = location_type
        property_record.google_geocoded_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(property_record)
        return property_record

    location = (best.get("geometry") or {}).get("location") or {}
    lat = location.get("lat")
    lng = location.get("lng")
    if lat is None or lng is None:
        return property_record

    property_record.google_rooftop_latitude = float(lat)
    property_record.google_rooftop_longitude = float(lng)
    property_record.google_geocode_location_type = "ROOFTOP"
    property_record.google_geocoded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(property_record)
    return property_record
