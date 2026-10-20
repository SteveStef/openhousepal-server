"""One-click domain connect (Domain Connect, the open standard - domainconnect.org).

Instead of typing records at their domain company, the agent clicks one button, signs in to that
company, and approves; the company adds the records for them and sends them back. Works at any
company that has switched on our two templates (deploy/domain-connect/):

  openhousepal.com / website           CNAME <host> -> sites.openhousepal.com (host = "www", or the
                                       subdomain they chose, like "homes")
  openhousepal.com / website-redirect  REDIR301 on the bare domain -> https://www.<domain>

Each company decides on its own whether to switch a template on (Cloudflare, IONOS and others do;
GoDaddy may only work through Entri). We never assume: every time, we look up the domain's company
(the _domainconnect TXT record -> /v2/<domain>/settings) and ask it whether it has our template.
No support = the button doesn't show and the manual guide is used, exactly as before.

Requests are signed (every company requires it): RS256 over the query string with the private key
in DOMAIN_CONNECT_PRIVATE_KEY (PEM; "\\n" escapes are fine). The matching public key is published
as TXT records at <DOMAIN_CONNECT_KEY_HOST>.openhousepal.com - see deploy/domain-connect/README.md.
Nothing here ever changes anyone's DNS by itself.
"""
import base64
import logging
import os
import time
from typing import Optional
from urllib.parse import quote

import httpx

from app.services.website.domain_verification_service import _doh_lookup

logger = logging.getLogger(__name__)

PROVIDER_ID = os.getenv("DOMAIN_CONNECT_PROVIDER_ID", "openhousepal.com")
SERVICE_SITE = "website"
SERVICE_REDIRECT = "website-redirect"
KEY_HOST = os.getenv("DOMAIN_CONNECT_KEY_HOST", "_dck1")

_SETTINGS_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_SUPPORT_CACHE: dict[str, tuple[float, bool]] = {}


def configured() -> bool:
    return bool(os.getenv("DOMAIN_CONNECT_PRIVATE_KEY"))


def _private_key():
    from cryptography.hazmat.primitives import serialization
    pem = (os.getenv("DOMAIN_CONNECT_PRIVATE_KEY") or "").replace("\\n", "\n").encode()
    return serialization.load_pem_private_key(pem, password=None)


def sign(query: str) -> str:
    """base64 RS256 signature of the query string (everything after "?", minus sig/key)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return base64.b64encode(_private_key().sign(query.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()


def public_key_txt_records() -> list[str]:
    """The TXT values to publish at <KEY_HOST>.openhousepal.com for the current private key."""
    from cryptography.hazmat.primitives import serialization
    der = _private_key().public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    data = base64.b64encode(der).decode()
    chunks = [data[i:i + 200] for i in range(0, len(data), 200)]
    return [f"p={n},a=RS256,d={c}" for n, c in enumerate(chunks, start=1)]


async def discover(apex: str) -> Optional[dict]:
    """The domain company's Domain Connect settings for this domain, or None (no support / unknown).
    {providerId, providerName, providerDisplayName?, urlSyncUX?, urlAPI, width?, height?}"""
    apex = apex.lower().strip(".")
    hit = _SETTINGS_CACHE.get(apex)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    settings = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            txt = await _doh_lookup(client, f"_domainconnect.{apex}", "TXT")
            prefix = next((t.strip('"').strip() for t in txt if t.strip('"').strip()), None)
            if prefix:
                prefix = prefix.removeprefix("https://").removeprefix("http://").rstrip("/")
                resp = await client.get(f"https://{prefix}/v2/{apex}/settings", follow_redirects=True)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("urlAPI") and data.get("providerId"):
                        settings = data
    except Exception:  # noqa: BLE001 - any failure = no one-click for this domain
        logger.info("Domain Connect discovery failed", exc_info=True, extra={"domain": apex})
    _SETTINGS_CACHE[apex] = (time.monotonic() + 30 * 60, settings)
    return settings


async def supports(settings: dict, service_id: str) -> bool:
    """Has this company switched on our template? (cached a few hours - it rarely changes)"""
    if not settings or not settings.get("urlSyncUX"):
        return False
    url = f"{settings['urlAPI'].rstrip('/')}/v2/domainTemplates/providers/{PROVIDER_ID}/services/{service_id}"
    hit = _SUPPORT_CACHE.get(url)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    ok = False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            ok = (await client.get(url, follow_redirects=True)).status_code < 300
    except httpx.HTTPError:
        ok = False
    _SUPPORT_CACHE[url] = (time.monotonic() + 6 * 3600, ok)
    return ok


def apply_url(settings: dict, apex: str, host: str, service_id: str, redirect_uri: str, state: str) -> str:
    """The signed link that opens the company's "OpenHousePal wants to connect - Approve?" screen."""
    params = [("domain", apex), ("host", host), ("redirect_uri", redirect_uri), ("state", state)]
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    sig = sign(query)
    base = settings["urlSyncUX"].rstrip("/")
    return (f"{base}/v2/domainTemplates/providers/{PROVIDER_ID}/services/{service_id}/apply?{query}"
            f"&sig={quote(sig, safe='')}&key={quote(KEY_HOST, safe='')}")


def company_name(settings: Optional[dict]) -> Optional[str]:
    if not settings:
        return None
    return settings.get("providerDisplayName") or settings.get("providerName")
