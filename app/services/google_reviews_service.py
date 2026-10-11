"""Import an agent's existing Google Business reviews so a brand-new OpenHousePal account isn't
showing zero testimonials on day one.

Uses the Places API (New) Place Details endpoint. Google caps this at 5 "most relevant" reviews
per place with no pagination or sorting - that's a hard platform limit (the Business Profile API
returns all of them but needs owner OAuth + a multi-week Google approval). 5 is still plenty to
seed an Agent Hub / IDX testimonials section.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
_FIELD_MASK = "id,displayName,rating,userRatingCount,reviews"
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)


class GoogleReviewsError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.getenv("GOOGLE_PLACES_API_KEY"))


async def fetch_reviews(place_id: str) -> dict:
    """Return {name, rating, total, reviews: [{author, rating, text, published_at(datetime|None)}]}.
    Raises GoogleReviewsError on a bad key / bad place id / no API access."""
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise GoogleReviewsError("GOOGLE_PLACES_API_KEY is not configured.")
    place_id = (place_id or "").strip()
    if not place_id:
        raise GoogleReviewsError("No Google Place ID set.")

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            _DETAILS_URL.format(place_id=place_id),
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _FIELD_MASK},
        )
    if resp.status_code == 404:
        raise GoogleReviewsError("That Google Place ID wasn't found. Double-check it.")
    if resp.status_code == 403:
        raise GoogleReviewsError("Google refused the request - the Places API (New) may not be enabled for this key.")
    if resp.status_code >= 300:
        raise GoogleReviewsError(f"Google Places API error ({resp.status_code}).")

    j = resp.json()
    out_reviews = []
    for r in (j.get("reviews") or []):
        text = ((r.get("originalText") or {}).get("text")
                or (r.get("text") or {}).get("text") or "").strip()
        author = ((r.get("authorAttribution") or {}).get("displayName") or "Google user").strip()
        rating = int(r.get("rating") or 0)
        published_at = None
        pub = r.get("publishTime")
        if pub:
            try:
                published_at = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                published_at = None
        if text and 1 <= rating <= 5:
            out_reviews.append({
                "author": author[:120],
                "rating": rating,
                "text": text,
                "published_at": published_at,
            })
    return {
        "name": (j.get("displayName") or {}).get("text"),
        "rating": j.get("rating"),
        "total": j.get("userRatingCount"),
        "reviews": out_reviews,
    }
