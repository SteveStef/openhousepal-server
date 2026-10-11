import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.nearby_amenities_service import (
    DEFAULT_SEARCH_RADIUS_MILES,
    NEARBY_CANDIDATE_POOL,
    TYPED_FIELD_MASK,
    _fetch_grocery_stores,
    _fetch_medical_care,
    _haversine_miles,
    _is_full_service_grocery,
    _is_legitimate_full_service_hospital,
    _non_hospital_medical_label,
    _place_name,
    _place_primary_type,
    _search_nearby,
    _search_nearby_types,
    _search_text,
)
from app.services.google_geocoding_service import GEOCODING_URL


async def geocode(client: httpx.AsyncClient, api_key: str, address: str) -> tuple[float, float]:
    response = await client.get(GEOCODING_URL, params={"address": address, "key": api_key}, timeout=12.0)
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        raise RuntimeError(f"No geocode result for {address}")
    rooftop = next((r for r in results if (r.get("geometry") or {}).get("location_type") == "ROOFTOP"), results[0])
    location = rooftop["geometry"]["location"]
    print(f"Geocode: {rooftop.get('formatted_address')} | {rooftop['geometry'].get('location_type')} | {location['lat']},{location['lng']}")
    return float(location["lat"]), float(location["lng"])


def print_place(prefix: str, place: dict, lat: float, lng: float, category: str | None) -> None:
    location = place.get("location") or {}
    distance = None
    if location.get("latitude") is not None and location.get("longitude") is not None:
        distance = round(_haversine_miles(lat, lng, location["latitude"], location["longitude"]), 1)
    label = ((place.get("primaryTypeDisplayName") or {}).get("text") or "")
    print(
        f"{prefix}: {category or 'REJECT'} | {_place_name(place)} | {distance} mi | "
        f"primaryType={_place_primary_type(place)!r} | primaryTypeDisplayName={label!r} | types={place.get('types')}"
    )


async def main() -> None:
    address = " ".join(sys.argv[1:]).strip() or "271 Janet Ct, Lothian, MD 20711"
    api_key = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured")

    async with httpx.AsyncClient() as client:
        lat, lng = await geocode(client, api_key, address)

        grocery_raw = []
        for included_type in ["supermarket", "grocery_store"]:
            grocery_raw.extend(await _search_nearby(
                client, api_key, lat, lng, included_type,
                radius_miles=DEFAULT_SEARCH_RADIUS_MILES,
                max_result_count=NEARBY_CANDIDATE_POOL,
                field_mask=TYPED_FIELD_MASK,
            ))
        seen = set()
        print("\nGROCERY CANDIDATES")
        for place in grocery_raw:
            key = (_place_name(place).lower(), place.get("formattedAddress") or "")
            if key in seen:
                continue
            seen.add(key)
            print_place("grocery", place, lat, lng, "GROCERY" if _is_full_service_grocery(place) else None)

        hospital_raw = []
        for hospital_type in ["general_hospital", "hospital"]:
            hospital_raw.extend(await _search_nearby(
                client, api_key, lat, lng, hospital_type,
                radius_miles=24,
                max_result_count=NEARBY_CANDIDATE_POOL,
                field_mask=TYPED_FIELD_MASK,
            ))
        non_hospital_raw = await _search_nearby_types(
            client, api_key, lat, lng, ["medical_clinic", "doctor", "medical_center"],
            max_result_count=NEARBY_CANDIDATE_POOL,
            radius_miles=24,
            field_mask=TYPED_FIELD_MASK,
        )
        urgent_raw = await _search_text(client, api_key, lat, lng, "urgent care", 24, field_mask=TYPED_FIELD_MASK)

        print("\nMEDICAL CANDIDATES")
        seen = set()
        for place in [*hospital_raw, *non_hospital_raw, *urgent_raw]:
            key = (_place_name(place).lower(), place.get("formattedAddress") or "")
            if key in seen:
                continue
            seen.add(key)
            category = "HOSPITAL" if _is_legitimate_full_service_hospital(place) else _non_hospital_medical_label(place)
            print_place("medical", place, lat, lng, category)

        print("\nSELECTED")
        for place in await _fetch_grocery_stores(client, api_key, lat, lng):
            print(f"grocery selected: {place.name} | {place.distance_miles} mi | {place.place_type}")
        for place in await _fetch_medical_care(client, api_key, lat, lng):
            print(f"medical selected: {place.place_type} | {place.name} | {place.distance_miles} mi")


if __name__ == "__main__":
    asyncio.run(main())
