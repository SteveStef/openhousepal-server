"""Never pay Google Maps (owner, 10/26: "I'm not paying for that after 5000 - we wait until it
refreshes"). Every Google Maps Platform call the backend makes goes through httpx; install() wraps
httpx.AsyncClient.send so each call is counted against its billing SKU's FREE monthly allowance
before it goes out. Once a SKU reaches FREE_SHARE of its allowance, calls of that kind stop for the
rest of the calendar month with GoogleFreeTierPaused (an httpx.RequestError, so every caller's
existing "Google failed" path handles it - nothing is cached as "no results") and start again on
the 1st, when Google's free allowance resets.

Google's free calls per SKU per month (Maps Platform pricing since March 2025): Essentials 10,000,
Pro 5,000, Enterprise 1,000. Which SKU a Places call bills under depends on the fields it asks for
(X-Goog-FieldMask), so the mask decides. Counts live in usage_budget_counters (bucket
"google_free:<sku>", month rows) - shared by every worker, correct across restarts.

Only the Maps hosts are counted; Gmail / Calendar / Search Console / sign-in are free and untouched.
Map pictures and the interactive map load in visitors' browsers with the website's own key and
never pass through here - those need a quota set in Google Cloud for that key.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FREE_PER_MONTH = {"essentials": 10_000, "pro": 5_000, "enterprise": 1_000}
FREE_SHARE = 0.95  # stop a little early: Google's own count and ours can differ by a few calls
_MAPS_HOSTS = {"places.googleapis.com", "routes.googleapis.com", "maps.googleapis.com"}

# Calls Google already counted this month that this counter never saw: the counter went live on
# 10/26, and local testing shares the live key (10/26 town checks: ~2,400 Text Search Pro, ~500
# Enterprise, ~2,800 route elements) plus live use before then. Rounded up so we stop early, not late.
USED_BEFORE_COUNTING = {
    "2026-10": {"text_search_pro": 3100, "text_search_enterprise": 680, "nearby_search_pro": 500,  # + Malvern's
                "place_details_pro": 300, "route_matrix_essentials": 3100, "route_matrix_pro": 1550},   # local research
}

# Places fields that move a call up a tier (Places API (New) pricing tables).
_ENTERPRISE_FIELDS = {
    "rating", "userratingcount", "websiteuri", "nationalphonenumber", "internationalphonenumber",
    "regularopeninghours", "currentopeninghours", "regularsecondaryopeninghours", "currentsecondaryopeninghours",
    "pricelevel", "pricerange", "reviews", "editorialsummary", "generativesummary", "reviewsummary",
    "takeout", "delivery", "dinein", "curbsidepickup", "reservable", "servesbreakfast", "serveslunch",
    "servesdinner", "servesbeer", "serveswine", "servesbrunch", "servesvegetarianfood", "outdoorseating",
    "livemusic", "menuforchildren", "servescocktails", "servesdessert", "servescoffee", "goodforchildren",
    "allowsdogs", "restroom", "goodforgroups", "goodforwatchingsports", "paymentoptions", "parkingoptions",
    "accessibilityoptions", "fueloptions", "evchargeoptions",
}
_ESSENTIAL_ONLY_FIELDS = {"id", "name", "attributions", "moved_place", "moved_place_id", "photos", "formattedaddress",
                          "addresscomponents", "location", "plus_code", "pluscode", "shortformattedaddress",
                          "types", "viewport", "postaladdress", "adrformataddress"}


class GoogleFreeTierPaused(httpx.RequestError):
    """This kind of Google call has used its free calls for the month - it waits for the 1st."""


def _fields(mask: str) -> set[str]:
    out = set()
    for part in (mask or "").split(","):
        f = part.strip().split(".")[-1].lower()  # "places.userRatingCount" -> "userratingcount"
        if f:
            out.add(f)
    return out


def _places_tier(mask: str, essentials_ok: bool) -> str:
    fields = _fields(mask)
    if fields & _ENTERPRISE_FIELDS:
        return "enterprise"
    if essentials_ok and fields and fields <= _ESSENTIAL_ONLY_FIELDS:
        return "essentials"
    return "pro"


def classify(request: httpx.Request) -> Optional[tuple[str, str, int]]:
    """(sku name, tier, billable units) for a Google Maps call; None for anything else or a call
    that is free with no cap (Text Search with place IDs only, Street View metadata)."""
    host, path = request.url.host, request.url.path
    if host not in _MAPS_HOSTS:
        return None
    mask = request.headers.get("X-Goog-FieldMask") or request.url.params.get("fields") or ""
    if host == "places.googleapis.com":
        if path.endswith(":searchText"):
            if _fields(mask) <= {"id", "name", "nextpagetoken"} and _fields(mask):
                return None  # Text Search Essentials (IDs only): free, no cap
            tier = _places_tier(mask, essentials_ok=False)
            return (f"text_search_{tier}", tier, 1)
        if path.endswith(":searchNearby"):
            tier = _places_tier(mask, essentials_ok=False)
            return (f"nearby_search_{tier}", tier, 1)
        if path.endswith(":autocomplete"):
            return ("autocomplete", "essentials", 1)
        if path.endswith("/media"):
            return ("place_photos", "enterprise", 1)
        if path.startswith("/v1/places/"):
            tier = _places_tier(mask, essentials_ok=True)
            return (f"place_details_{tier}", tier, 1)
        return ("places_other", "pro", 1)
    if host == "routes.googleapis.com":
        try:
            body = json.loads(request.content or b"{}")
        except (ValueError, TypeError):
            body = {}
        traffic = str(body.get("routingPreference") or "").startswith("TRAFFIC_AWARE")
        tier = "pro" if traffic else "essentials"
        if "computeRouteMatrix" in path:
            units = max(1, len(body.get("origins") or [1])) * max(1, len(body.get("destinations") or [1]))
            return (f"route_matrix_{tier}", tier, units)
        return (f"routes_{tier}", tier, 1)
    # maps.googleapis.com - the classic web services
    if "/streetview/metadata" in path:
        return None  # free
    for key, sku in (("/geocode/", "geocoding"), ("/staticmap", "static_maps"), ("/streetview", "street_view"),
                     ("/distancematrix/", "distance_matrix"), ("/timezone/", "time_zone"), ("/elevation/", "elevation")):
        if key in path:
            return (sku, "essentials", 1)
    if "/place/" in path:  # the legacy Places web service
        return ("legacy_places", "pro", 1)
    return ("maps_other", "essentials", 1)


def monthly_limit(tier: str) -> int:
    return int(FREE_PER_MONTH[tier] * FREE_SHARE)


# Town research runs on the owner's computer with the same Google key, and its counter lives in the
# local database. Before each push export_local_usage() writes that month's local counts here, and
# the live server adds them to its own - so both together never pass the free line. The local
# server ignores the file (its own counter already has those calls).
USED_LOCALLY_FILE = Path(__file__).resolve().parents[1] / "data" / "google_used_locally.json"
_used_locally: Optional[dict] = None


def _is_local() -> bool:
    url = os.getenv("DATABASE_URL", "")
    return "localhost" in url or "127.0.0.1" in url


def _local_counts() -> dict:
    global _used_locally
    if _used_locally is None:
        try:
            _used_locally = json.loads(USED_LOCALLY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _used_locally = {}
    return _used_locally


def _used_before(sku: str, month: str) -> int:
    before = USED_BEFORE_COUNTING.get(month, {}).get(sku, 0)
    if not _is_local():
        before += int((_local_counts().get(month) or {}).get(sku, 0))
    return before


async def export_local_usage(db) -> dict:
    """This computer's counted Google calls per month (not the hand-entered starting numbers), written
    to USED_LOCALLY_FILE for the live server. Run locally before a push."""
    from sqlalchemy import select
    from app.models.database import UsageBudgetCounter
    assert _is_local(), "run on the local database only"
    rows = (await db.execute(select(UsageBudgetCounter.bucket, UsageBudgetCounter.period_key, UsageBudgetCounter.count)
                             .where(UsageBudgetCounter.bucket.like("google_free:%"), UsageBudgetCounter.period_type == "month"))).all()
    out: dict = {}
    for bucket, month, count in rows:
        out.setdefault(month, {})[bucket.split(":", 1)[1]] = int(count)
    USED_LOCALLY_FILE.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _tier_of(sku: str) -> str:
    if sku.endswith("enterprise") or sku == "place_photos":
        return "enterprise"
    if sku.endswith("_pro") or sku in ("legacy_places", "places_other"):
        return "pro"
    return "essentials"


async def _take(sku: str, tier: str, units: int) -> bool:
    """Count `units` calls against this month's free allowance; False once it's used up."""
    from app.database import AsyncSessionLocal
    from app.services.usage_budget_service import _period_keys, reserve_bucket
    _day, month = _period_keys()
    async with AsyncSessionLocal() as db:
        room = max(0, monthly_limit(tier) - _used_before(sku, month))
        ok = await reserve_bucket(db, f"google_free:{sku}", "month", month, units, 0, room, None)
        await db.commit()
        return ok


_installed = False


def install() -> None:
    """Wrap httpx.AsyncClient.send once per process (app startup)."""
    global _installed
    if _installed or os.getenv("GOOGLE_FREE_TIER_GUARD", "on").lower() in ("off", "0", "false"):
        return
    original = httpx.AsyncClient.send

    async def send(self, request, *args, **kwargs):
        kind = classify(request)
        if kind:
            sku, tier, units = kind
            try:
                allowed = await _take(sku, tier, units)
            except Exception as exc:  # noqa: BLE001 - the counter itself failing never blocks the feature
                logger.warning(f"Google free-tier counter failed ({sku}): {exc}")
                allowed = True
            if not allowed:
                logger.warning("Google free calls used up for the month - call skipped", extra={"sku": sku})
                raise GoogleFreeTierPaused(f"Google {sku.replace('_', ' ')}: free calls for this month are used up", request=request)
        return await original(self, request, *args, **kwargs)

    httpx.AsyncClient.send = send
    _installed = True


async def month_usage(db) -> list[dict]:
    """This month's count per kind of Google call against its free allowance - for the admin page."""
    from sqlalchemy import select
    from app.models.database import UsageBudgetCounter
    from app.services.usage_budget_service import _period_keys
    _day, month = _period_keys()
    rows = (await db.execute(
        select(UsageBudgetCounter.bucket, UsageBudgetCounter.count)
        .where(UsageBudgetCounter.bucket.like("google_free:%"), UsageBudgetCounter.period_type == "month",
               UsageBudgetCounter.period_key == month)
    )).all()
    counted = {bucket.split(":", 1)[1]: count for bucket, count in rows}
    out = []
    for sku in set(counted) | set(USED_BEFORE_COUNTING.get(month, {})):
        tier = _tier_of(sku)
        out.append({"sku": sku, "used": counted.get(sku, 0) + _used_before(sku, month),
                    "free": FREE_PER_MONTH[tier], "stops_at": monthly_limit(tier)})
    return sorted(out, key=lambda r: -r["used"] / max(1, r["stops_at"]))
