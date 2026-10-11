import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Property


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

    api_key = os.getenv("GOOGLE_GEOCODING_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        logger.info("Google geocoding skipped: no API key configured")
        return property_record

    address = _property_address(property_record)
    if not address:
        return property_record

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(GEOCODING_URL, params={"address": address, "key": api_key})
            response.raise_for_status()
            payload = response.json()
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
