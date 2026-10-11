"""Town photos for a Listing Spotlight's "About the town" section - free, credited photographs of
the town itself (its station, main street, landmarks) from the town's Wikipedia article, via
Wikimedia Commons. Never a photo of the house.

Returns a short, ranked list so the editor can pre-pick the best one and let the agent swap it.
Every photo carries its author + license line, shown under the photo on the public page (Commons
licences - CC BY / BY-SA - require that credit).
"""
from __future__ import annotations

import re
import time
from html import unescape
from typing import Optional
from urllib.parse import quote

import httpx

from app.config.logging import get_logger

logger = get_logger(__name__)

_UA = {"User-Agent": "OpenHousePal/1.0 (https://openhousepal.com; support@openhousepal.com)"}
_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_CACHE_SECONDS = 24 * 3600

# Maps, seals, flags, logos, diagrams - never a picture of the place.
_SKIP = re.compile(r"(\.svg$|map|locator|location|highlighted|seal|flag|logo|coat[_ ]of[_ ]arms|diagram|chart|census|signature|portrait|reagan)", re.I)
# What a buyer wants to see first: the downtown, a main street, the train station, a park.
_GOOD = re.compile(r"(downtown|main[_ ]street|street|station|square|park|village|historic|view|skyline|green|lake|river|creek|trail|library|theater|theatre|shops?)", re.I)

_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _plain(html: Optional[str], limit: int = 80) -> str:
    text = unescape(re.sub(r"<[^>]+>", "", html or "")).strip()
    return re.sub(r"\s+", " ", text)[:limit]


async def _media_titles(client: httpx.AsyncClient, article: str) -> list[tuple[str, str]]:
    r = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/media-list/{quote(article.replace(' ', '_'), safe=',_()')}")
    if r.status_code != 200:
        return []
    out = []
    for item in (r.json().get("items") or []):
        title = item.get("title") or ""
        if item.get("type") != "image" or not title.startswith("File:") or _SKIP.search(title):
            continue
        out.append((title, _plain((item.get("caption") or {}).get("text"), 120)))
    return out


async def town_photos(town: str, state: Optional[str]) -> list[dict]:
    """[{url, thumb, caption, credit, source_url}] - best first, at most 8. Empty when the town
    has no usable article photos (the section then simply shows no photo)."""
    town = (town or "").strip().title()
    st = (state or "").strip().upper()
    if not town:
        return []
    key = (town.lower(), st)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    articles = [f"{town}, {_STATES[st]}"] if st in _STATES else []
    articles.append(town)
    photos: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=12, headers=_UA, follow_redirects=True) as client:
            titles: list[tuple[str, str]] = []
            for article in articles:
                titles = await _media_titles(client, article)
                if titles:
                    break
            if not titles:
                _CACHE[key] = (time.time(), [])
                return []
            captions = dict(titles)
            r = await client.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "format": "json", "prop": "imageinfo",
                "titles": "|".join(t for t, _ in titles[:20]),
                "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1800",
            })
            pages = (r.json().get("query") or {}).get("pages") or {}
            for page in pages.values():
                info = (page.get("imageinfo") or [{}])[0]
                w, h = info.get("width") or 0, info.get("height") or 0
                if info.get("mime") not in ("image/jpeg", "image/png") or w < 900 or w < h * 1.15:
                    continue  # small, portrait-shaped or not a photo - reads badly as a banner
                meta = info.get("extmetadata") or {}
                artist = _plain((meta.get("Artist") or {}).get("value"), 60)
                if re.search(r"unknown|no machine-readable", artist, re.I):
                    artist = ""
                half = len(artist) // 2
                if half and artist[:half] == artist[half:]:
                    artist = artist[:half]  # Commons sometimes repeats the name
                lic = _plain((meta.get("LicenseShortName") or {}).get("value"), 30)
                caption = captions.get(page.get("title") or "", "") or _plain((meta.get("ImageDescription") or {}).get("value"), 120)
                caption = caption.rstrip(". ")
                photos.append({
                    "url": info.get("thumburl") or info.get("url"),
                    "thumb": (info.get("thumburl") or info.get("url") or "").replace("/1800px-", "/480px-"),
                    "caption": caption,
                    "credit": " · ".join(x for x in (f"Photo: {artist}" if artist else "Photo: Wikimedia Commons", lic) if x),
                    "source_url": info.get("descriptionurl"),
                    "_rank": (0 if _GOOD.search(f"{page.get('title')} {caption}") else 1, titles.index((page.get("title"), captions.get(page.get("title"), ""))) if (page.get("title"), captions.get(page.get("title"), "")) in titles else 99),
                })
    except Exception as exc:  # noqa: BLE001 - a missing town photo is never an error for the agent
        logger.warning("town photo lookup failed", extra={"town": town, "error": str(exc)[:200]})
        return []
    photos.sort(key=lambda p: p["_rank"])
    for p in photos:
        p.pop("_rank", None)
    photos = photos[:8]
    _CACHE[key] = (time.time(), photos)
    return photos
