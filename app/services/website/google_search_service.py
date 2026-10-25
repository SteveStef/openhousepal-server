"""Put every agent's IDX website in Google (and Bing) without the agent doing anything.

Google only fully trusts a site once someone proves they own it in Search Console and hands it a
sitemap. Agents aren't technical, so OpenHousePal does it for them with ONE Google service account
of our own (GOOGLE_SEARCH_SERVICE_ACCOUNT_JSON - the key file's JSON, pasted whole):

  1. Site Verification API: ask Google for a "meta tag" code for https://<site host>/ and save it
     on the site (sites.google_search.auto_code). The public home page renders it as
     <meta name="google-site-verification"> (public_site_routes.resolve -> page.tsx metadata).
  2. A minute later (the live page caches for 30s), ask Google to check the tag -> our service
     account now owns that site in Search Console.
  3. Search Console API: add the site, submit https://<host>/sitemap.xml.

Runs right after a publish (site_routes.publish_my_site) and on a 15-minute sweep that catches new
own domains, restarts and anything that failed (with back-off). An agent who'd rather use their own
Search Console can paste their own code too (manual_code) - both tags are rendered.

Also pings IndexNow (Bing, Yandex, Seznam...) on every publish - no account needed, just a public
key file at https://<host>/indexnow-key.txt (frontend route) holding indexnow_key(site).

Pure HTTP via httpx + python-jose (already used for our own JWTs) - no Google SDK.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Site, User
from app.services.website import domain_resolution_service

logger = logging.getLogger(__name__)

_TOKEN = "https://oauth2.googleapis.com/token"
_VERIFY = "https://www.googleapis.com/siteVerification/v1"
_WEBMASTERS = "https://www.googleapis.com/webmasters/v3"
_INDEXNOW = "https://api.indexnow.org/indexnow"
_SCOPES = "https://www.googleapis.com/auth/siteverification https://www.googleapis.com/auth/webmasters"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# The live page caches the site for 30s (page.tsx resolveSite revalidate) - wait past that before
# asking Google to look for a freshly saved tag.
TAG_SETTLE_SECONDS = 45
MAX_CODES_LEN = 120
_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{10,%d}$" % MAX_CODES_LEN)


class GoogleSearchError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config / helpers

def _service_account() -> Optional[dict]:
    raw = (os.getenv("GOOGLE_SEARCH_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    # Forgiving about how it was pasted into the server settings: wrapped in quotes, or the
    # key file base64-encoded onto one line (no spaces/quotes for an env editor to mangle).
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1].strip()
    try:
        info = json.loads(raw)
    except ValueError:
        try:
            import base64
            info = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            logger.error("GOOGLE_SEARCH_SERVICE_ACCOUNT_JSON isn't valid JSON - Google auto-register is off")
            return None
    return info if info.get("client_email") and info.get("private_key") else None


def is_configured() -> bool:
    return _service_account() is not None


def parse_verification_code(text: str) -> Optional[str]:
    """What an agent pastes from Search Console's "HTML tag" method - the whole
    <meta name="google-site-verification" content="..."> tag or just the code - to the bare code.
    '' -> None (clears it). Raises ValueError on anything that isn't a code."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r'content\s*=\s*["\']([^"\']+)["\']', text, re.I)
    code = (m.group(1) if m else text).strip()
    if not _CODE_RE.match(code):
        raise ValueError("That doesn't look like a Google code - copy the whole line Google shows under \"HTML tag\".")
    return code


def verification_codes(google_search: Optional[dict]) -> list[str]:
    """Every google-site-verification code the home page should carry (ours + the agent's)."""
    gs = google_search or {}
    out = []
    for c in (gs.get("auto_code"), gs.get("manual_code")):
        if c and c not in out:
            out.append(c)
    return out


def indexnow_key(site_id: str) -> str:
    """Stable per-site IndexNow key (public by design - it's served from the site itself)."""
    secret = (os.getenv("JWT_SECRET_KEY") or "dev-insecure").encode()
    return hmac.new(secret, f"indexnow:{site_id}".encode(), hashlib.sha256).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _is_public_host(host: str) -> bool:
    h = (host or "").split(":")[0]
    return bool(h) and "." in h and not h.endswith((".localhost", ".test", ".local")) and h not in ("localhost", "127.0.0.1")


# --------------------------------------------------------------------------- Google HTTP

_token_cache: dict[str, Any] = {"token": None, "exp": 0.0}


async def _access_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["token"]
    sa = _service_account()
    if not sa:
        raise GoogleSearchError("not configured")
    from jose import jwt
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": sa["client_email"], "scope": _SCOPES, "aud": _TOKEN, "iat": now, "exp": now + 3600},
        sa["private_key"], algorithm="RS256",
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(_TOKEN, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if r.status_code >= 300:
        raise GoogleSearchError(f"token: {r.status_code} {r.text[:200]}")
    j = r.json()
    _token_cache.update(token=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]


async def _google(method: str, url: str, *, json_body=None, params=None) -> dict:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.request(method, url, headers={"Authorization": f"Bearer {token}"}, json=json_body, params=params)
    if r.status_code >= 300:
        raise GoogleSearchError(f"{method} {url.split('googleapis.com')[-1][:80]} -> {r.status_code} {r.text[:200]}")
    return r.json() if r.text else {}


async def get_meta_token(site_url: str) -> str:
    j = await _google("POST", f"{_VERIFY}/token", json_body={"site": {"type": "SITE", "identifier": site_url}, "verificationMethod": "META"})
    code = parse_verification_code(j.get("token") or "")
    if not code:
        raise GoogleSearchError("Google returned no code")
    return code


async def verify_site(site_url: str) -> None:
    await _google("POST", f"{_VERIFY}/webResource", params={"verificationMethod": "META"},
                  json_body={"site": {"type": "SITE", "identifier": site_url}})


async def add_to_search_console(site_url: str) -> None:
    await _google("PUT", f"{_WEBMASTERS}/sites/{quote(site_url, safe='')}")


async def submit_sitemap(site_url: str, sitemap_url: str) -> None:
    await _google("PUT", f"{_WEBMASTERS}/sites/{quote(site_url, safe='')}/sitemaps/{quote(sitemap_url, safe='')}")


# --------------------------------------------------------------------------- the per-site flow

def _short_error(e: Exception) -> str:
    s = str(e)
    if "403" in s or "401" in s:
        return "Google said no - the OpenHousePal Google account needs the Site Verification and Search Console APIs turned on."
    if "verification" in s.lower() or "400" in s:
        return "Google couldn't see the code on your home page yet - we'll try again shortly."
    return "Couldn't reach Google - we'll try again shortly."


def _due(gs: dict) -> bool:
    """Back-off between failed attempts: 15m, 30m, 1h, 2h ... capped at a day."""
    last = _parse(gs.get("last_attempt_at"))
    tries = int(gs.get("failures") or 0)
    if not last or tries <= 0:
        return True
    wait = min(timedelta(minutes=15) * (2 ** (tries - 1)), timedelta(hours=24)) - timedelta(minutes=1)
    return _now() - last >= wait


async def _save(db: AsyncSession, site: Site, gs: dict) -> None:
    site.google_search = dict(gs)
    db.add(site)
    await db.commit()


async def register_site(db: AsyncSession, site: Site, *, wait_for_tag: bool = False, force: bool = False) -> dict:
    """Make sure Google has this site: code on the page -> verified -> in Search Console -> sitemap
    submitted. Safe to call repeatedly; does only the steps still missing for the current host.
    wait_for_tag: after saving a brand-new code, wait for the live page to show it and carry on
    (publish path). Without it the next sweep finishes the job."""
    if not is_configured() or site.status != "PUBLISHED" or site.is_enabled is False:
        return dict(site.google_search or {})
    if ((site.published_snapshot or {}).get("seo") or {}).get("allow_indexing") is False:
        return dict(site.google_search or {})
    host = await domain_resolution_service.canonical_hostname(db, site)
    if not _is_public_host(host):
        return dict(site.google_search or {})
    gs = dict(site.google_search or {})
    if gs.get("host") != host:
        # New site or a new own domain - start over for it (the old code stays harmless).
        gs = {k: gs[k] for k in ("manual_code",) if gs.get(k)}
        gs["host"] = host
    if gs.get("sitemap_submitted_at") and not force:
        return gs
    if not force and not _due(gs):
        return gs

    site_url = f"https://{host}/"
    gs["last_attempt_at"] = _iso(_now())
    try:
        if not gs.get("auto_code"):
            gs["auto_code"] = await get_meta_token(site_url)
            await _save(db, site, gs)
            domain_resolution_service.invalidate_site(site.id)
            if not wait_for_tag:
                return gs
            await asyncio.sleep(TAG_SETTLE_SECONDS)
        if not gs.get("verified_at"):
            await verify_site(site_url)
            gs["verified_at"] = _iso(_now())
        await add_to_search_console(site_url)
        await submit_sitemap(site_url, f"https://{host}/sitemap.xml")
        gs["sitemap_submitted_at"] = _iso(_now())
        gs.pop("last_error", None)
        gs["failures"] = 0
    except Exception as e:  # noqa: BLE001
        logger.warning("Google auto-register failed for %s: %s", host, e)
        gs["last_error"] = _short_error(e)
        gs["failures"] = int(gs.get("failures") or 0) + 1
    await _save(db, site, gs)
    return gs


async def register_site_by_id(site_id: str, *, wait_for_tag: bool = True, force: bool = False) -> None:
    """Background-task entry point (own DB session)."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            site = (await db.execute(select(Site).where(Site.id == site_id))).scalar_one_or_none()
            if site:
                await register_site(db, site, wait_for_tag=wait_for_tag, force=force)
    except Exception:
        logger.error("Google auto-register task failed", exc_info=True)


async def scheduled_google_search_sweep() -> None:
    """Every 15 minutes: any live site Google doesn't have yet (new site, new own domain, a
    previous failure past its back-off)."""
    if not is_configured():
        return
    from app.database import AsyncSessionLocal
    from app.utils.auth import owner_has_access
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Site, User).join(User, User.id == Site.owner_id)
                .where(Site.status == "PUBLISHED", Site.is_enabled.is_(True))
            )).all()
            for site, owner in rows:
                if not owner_has_access(owner, {"IDX"}):
                    continue
                try:
                    await register_site(db, site)
                except Exception:
                    logger.error("Google sweep failed for site %s", site.id, exc_info=True)
    except Exception:
        logger.error("Google sweep failed", exc_info=True)


# --------------------------------------------------------------------------- IndexNow (Bing etc.)

def indexnow_urls(host: str, snapshot: dict) -> list[str]:
    origin = f"https://{host}"
    urls = [f"{origin}/", *(f"{origin}/{p}" for p in ("search", "properties", "neighborhoods", "blog", "about", "contact"))]
    for p in (snapshot or {}).get("pages") or []:
        slug = (p.get("slug") or "").strip("/")
        if slug and not p.get("is_home") and f"{origin}/{slug}" not in urls:
            urls.append(f"{origin}/{slug}")
    return urls[:100]


async def ping_indexnow(db: AsyncSession, site: Site) -> bool:
    """Tell Bing & co. the site changed. Skipped for hidden sites and local/dev hosts."""
    if site.status != "PUBLISHED" or site.is_enabled is False:
        return False
    if os.getenv("INDEXNOW_DISABLED", "").lower() == "true" or os.getenv("DEV", "false") == "true":
        return False
    snapshot = site.published_snapshot or {}
    if (snapshot.get("seo") or {}).get("allow_indexing") is False:
        return False
    host = await domain_resolution_service.canonical_hostname(db, site)
    if not _is_public_host(host):
        return False
    key = indexnow_key(site.id)
    body = {"host": host, "key": key, "keyLocation": f"https://{host}/indexnow-key.txt", "urlList": indexnow_urls(host, snapshot)}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(_INDEXNOW, json=body)
        ok = r.status_code in (200, 202)
        if not ok:
            logger.info("IndexNow answered %s for %s: %s", r.status_code, host, r.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.info("IndexNow ping failed for %s: %s", host, e)
        return False
    if ok:
        gs = dict(site.google_search or {})
        gs["indexnow_pinged_at"] = _iso(_now())
        await _save(db, site, gs)
    return ok


async def after_publish(site_id: str) -> None:
    """Publish background task: Bing ping now, then make sure Google has the site."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            site = (await db.execute(select(Site).where(Site.id == site_id))).scalar_one_or_none()
            if not site:
                return
            await ping_indexnow(db, site)
            await register_site(db, site, wait_for_tag=True)
    except Exception:
        logger.error("after-publish search tasks failed", exc_info=True)


# --------------------------------------------------------------------------- the agent's Google report

REPORT_DAYS = 28
REPORT_TTL = timedelta(hours=12)


async def _search_analytics(site_url: str, start: str, end: str, dimensions: list[str], limit: int) -> list[dict]:
    j = await _google("POST", f"{_WEBMASTERS}/sites/{quote(site_url, safe='')}/searchAnalytics/query", json_body={
        "startDate": start, "endDate": end, "dimensions": dimensions, "rowLimit": limit, "dataState": "all",
    })
    return j.get("rows") or []


def _totals(rows: list[dict]) -> dict:
    """Sum day rows into one line. Average position is weighted by how often each day showed."""
    imp = sum(int(r.get("impressions") or 0) for r in rows)
    clicks = sum(int(r.get("clicks") or 0) for r in rows)
    pos = sum(float(r.get("position") or 0) * int(r.get("impressions") or 0) for r in rows) / imp if imp else None
    return {"impressions": imp, "clicks": clicks, "ctr": (clicks / imp) if imp else None, "position": round(pos, 1) if pos else None}


def build_report(host: str, today: datetime, daily_rows: list[dict], query_rows: list[dict], page_rows: list[dict]) -> dict:
    """Shape Search Console rows into the editor's report. `daily_rows` cover the last 2 x 28 days
    (the earlier half is the "vs. the 28 days before" comparison)."""
    end = today.date()
    start = end - timedelta(days=REPORT_DAYS - 1)
    prev_start = start - timedelta(days=REPORT_DAYS)
    by_day = {r["keys"][0]: r for r in daily_rows if r.get("keys")}
    days = []
    for i in range(REPORT_DAYS):
        d = (start + timedelta(days=i)).isoformat()
        r = by_day.get(d) or {}
        days.append({"date": d, "impressions": int(r.get("impressions") or 0), "clicks": int(r.get("clicks") or 0)})
    current = _totals([r for k, r in by_day.items() if k >= start.isoformat()])
    previous = _totals([r for k, r in by_day.items() if prev_start.isoformat() <= k < start.isoformat()])
    origin = f"https://{host}"
    pages = []
    for r in page_rows[:6]:
        url = (r.get("keys") or [""])[0]
        path = url[len(origin):] if url.startswith(origin) else url
        pages.append({"path": path or "/", "impressions": int(r.get("impressions") or 0), "clicks": int(r.get("clicks") or 0)})
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "totals": current, "previous": previous, "days": days,
        "queries": [{"query": (r.get("keys") or [""])[0], "impressions": int(r.get("impressions") or 0), "clicks": int(r.get("clicks") or 0),
                     "position": round(float(r.get("position") or 0), 1)} for r in query_rows[:10]],
        "pages": pages,
        "fetched_at": _iso(_now()),
    }


async def report_for(db: AsyncSession, site: Site, *, refresh: bool = False) -> dict:
    """The last 28 days in Google for the editor - cached 12h on the site (Google's numbers only
    move once a day, 2-3 days behind). {"ready": False, "reason": ...} until there's anything to show."""
    status = await status_for(db, site)
    if status["state"] != "done":
        return {"ready": False, "reason": status["state"]}
    gs = dict(site.google_search or {})
    cached = gs.get("report") or {}
    fetched = _parse(cached.get("fetched_at"))
    if cached.get("host") == status["host"] and fetched and _now() - fetched < REPORT_TTL and not refresh:
        return {"ready": True, **cached}
    site_url = status["site_url"]
    today = _now()
    end = today.date().isoformat()
    start = (today.date() - timedelta(days=REPORT_DAYS - 1)).isoformat()
    prev_start = (today.date() - timedelta(days=2 * REPORT_DAYS - 1)).isoformat()
    try:
        daily, queries, pages = await asyncio.gather(
            _search_analytics(site_url, prev_start, end, ["date"], 2 * REPORT_DAYS + 5),
            _search_analytics(site_url, start, end, ["query"], 10),
            _search_analytics(site_url, start, end, ["page"], 6),
        )
    except Exception as e:  # noqa: BLE001
        logger.info("Google report failed for %s: %s", status["host"], e)
        if cached.get("host") == status["host"]:
            return {"ready": True, **cached}
        return {"ready": False, "reason": "unreachable"}
    report = {"host": status["host"], **build_report(status["host"], today, daily, queries, pages)}
    gs["report"] = report
    await _save(db, site, gs)
    return {"ready": True, **report}


# --------------------------------------------------------------------------- status for the editor

async def status_for(db: AsyncSession, site: Site) -> dict:
    gs = dict(site.google_search or {})
    host = await domain_resolution_service.canonical_hostname(db, site)
    current = gs.get("host") == host
    hidden = ((site.published_snapshot or {}).get("seo") or {}).get("allow_indexing") is False
    if site.status != "PUBLISHED" or site.is_enabled is False:
        state = "not_live"
    elif hidden:
        state = "hidden"
    elif not is_configured():
        state = "unavailable"
    elif current and gs.get("sitemap_submitted_at"):
        state = "done"
    elif current and gs.get("last_error"):
        state = "error"
    else:
        state = "working"
    return {
        "state": state,
        "host": host,
        "site_url": f"https://{host}/",
        "sitemap_url": f"https://{host}/sitemap.xml",
        "verified_at": gs.get("verified_at") if current else None,
        "sitemap_submitted_at": gs.get("sitemap_submitted_at") if current else None,
        "indexnow_pinged_at": gs.get("indexnow_pinged_at"),
        "last_error": gs.get("last_error") if current else None,
        "manual_code": gs.get("manual_code"),
    }
