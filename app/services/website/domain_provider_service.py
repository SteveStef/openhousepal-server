"""Which company runs an agent's domain - read from its public nameservers, so the setup guide can
open on the right company's steps (agents often don't remember where they bought it) and the
domain check doesn't nag about something their company can't do (Wix, Shopify, Route 53 can't
forward the bare domain to an outside website). Never raises; None when unknown.
"""
import time
from typing import Optional

import httpx

from app.services.website.domain_verification_service import _doh_lookup, apex_of

# nameserver suffix -> the guide's provider id
_NS = [
    ("domaincontrol.com", "godaddy"),
    ("registrar-servers.com", "namecheap"),
    ("squarespacedns.com", "squarespace"), ("googledomains.com", "squarespace"),
    ("ns.cloudflare.com", "cloudflare"),
    ("wixdns.net", "wix"),
    ("ui-dns.com", "ionos"), ("ui-dns.de", "ionos"), ("ui-dns.org", "ionos"), ("ui-dns.biz", "ionos"),
    ("worldnic.com", "networksolutions"),
    ("porkbun.com", "porkbun"),
    ("dns-parking.com", "hostinger"), ("hostinger.com", "hostinger"),
    ("bluehost.com", "bluehost"),
    ("hostgator.com", "hostgator"),
    ("awsdns", "route53"),
    ("name.com", "namecom"),
    ("dynadot.com", "dynadot"),
    ("hover.com", "hover"),
]

# Can't forward the bare domain to an outside website - only the www address works there.
NO_FORWARDING = {"wix", "route53", "shopify"}

_CACHE: dict[str, tuple[float, Optional[str]]] = {}


async def detect_provider(hostname: str) -> Optional[str]:
    apex = apex_of(hostname)
    hit = _CACHE.get(apex)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    provider = None
    try:
        async with httpx.AsyncClient() as client:
            ns = await _doh_lookup(client, apex, "NS")
        for server in ns:
            provider = next((p for suffix, p in _NS if suffix in server), None)
            if provider:
                break
    except Exception:  # noqa: BLE001 - no network / bad answer: just unknown
        return None
    _CACHE[apex] = (time.monotonic() + 6 * 3600, provider)
    return provider
