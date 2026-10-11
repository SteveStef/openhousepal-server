"""Agents' own domains through Cloudflare for SaaS ("custom hostnames").

openhousepal.com lives on Cloudflare. An agent points www.theirdomain.com at
sites.openhousepal.com (a CNAME at their own domain company); we register that hostname with
Cloudflare here, Cloudflare issues and renews its HTTPS certificate and sends the traffic to our
server with the agent's hostname intact, and domain_resolution_service serves their site.

Configured by two env vars (both set in production once custom hostnames are switched on in the
Cloudflare dashboard):
  CLOUDFLARE_API_TOKEN  - a token with "SSL and Certificates: Edit" on the openhousepal.com zone
  CLOUDFLARE_ZONE_ID    - the openhousepal.com zone id
Without them every call is a no-op, so local dev and a not-yet-configured deployment behave as
before. Every function is best-effort: it logs and never raises into the request.
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
_API = "https://api.cloudflare.com/client/v4"


def configured() -> bool:
    return bool(os.getenv("CLOUDFLARE_API_TOKEN") and os.getenv("CLOUDFLARE_ZONE_ID"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}", "Content-Type": "application/json"}


def _zone_url(path: str = "") -> str:
    return f"{_API}/zones/{os.getenv('CLOUDFLARE_ZONE_ID')}/custom_hostnames{path}"


async def find(hostname: str) -> Optional[dict]:
    """The custom hostname record for `hostname`, or None."""
    if not configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_zone_url(), headers=_headers(), params={"hostname": hostname})
            resp.raise_for_status()
            items = resp.json().get("result") or []
            return next((h for h in items if (h.get("hostname") or "").lower() == hostname.lower()), None)
    except Exception:  # noqa: BLE001
        logger.warning("Cloudflare custom hostname lookup failed", exc_info=True, extra={"hostname": hostname})
        return None


async def register(hostname: str) -> Optional[dict]:
    """Make sure Cloudflare knows `hostname` (idempotent). Validation runs over HTTP as soon as
    the agent's CNAME is in place, so registering before their DNS exists is fine."""
    if not configured():
        return None
    existing = await find(hostname)
    if existing:
        return existing
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_zone_url(), headers=_headers(), json={
                "hostname": hostname,
                "ssl": {"method": "http", "type": "dv", "settings": {"min_tls_version": "1.2"}},
            })
            resp.raise_for_status()
            return resp.json().get("result")
    except Exception:  # noqa: BLE001
        logger.warning("Cloudflare custom hostname registration failed", exc_info=True, extra={"hostname": hostname})
        return None


async def remove(hostname: str) -> None:
    if not configured():
        return
    existing = await find(hostname)
    if not existing or not existing.get("id"):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            (await client.delete(_zone_url(f"/{existing['id']}"), headers=_headers())).raise_for_status()
    except Exception:  # noqa: BLE001
        logger.warning("Cloudflare custom hostname removal failed", exc_info=True, extra={"hostname": hostname})
