"""Fetching a URL an agent typed (link previews, source photos, logos/headshots) without letting it
reach our own network: only http(s) to PUBLIC addresses - never localhost, the private ranges or the
cloud metadata address - and every redirect hop is checked the same way (a public page could
otherwise redirect to an internal one). Our own API (API_URL, exact host AND port) is allowed, since
locally uploaded files are served from it in development - but only while every hop so far was our
own API: an outside page redirecting to the API's address is refused (security pass 11/18)."""
import asyncio
import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx


class UnsafeUrlError(httpx.HTTPError):
    """Raised for a URL we refuse to fetch. Subclasses httpx.HTTPError so callers' existing
    "couldn't load that link" handling covers it."""


def _origin(url: str) -> Optional[tuple]:
    try:
        p = urlparse(url)
        if not p.hostname:
            return None
        return (p.scheme.lower(), p.hostname.lower(), p.port or (443 if p.scheme.lower() == "https" else 80))
    except ValueError:
        return None


def _is_trusted(url: str) -> bool:
    mine = {_origin(os.getenv(env) or "") for env in ("API_URL", "PUBLIC_API_URL")} - {None}
    return _origin(url) in mine


def _blocked(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified)


async def assert_public_url(url: str, trust_own: bool = True) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError("Only http(s) links can be loaded.")
    host = parsed.hostname.lower()
    if trust_own and _is_trusted(url):
        return
    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80)))
    except socket.gaierror as exc:
        raise UnsafeUrlError("That link's address doesn't exist.") from exc
    if not infos or any(_blocked(info[4][0]) for info in infos):
        raise UnsafeUrlError("That link points somewhere we can't load.")


async def safe_get(client: httpx.AsyncClient, url: str, max_redirects: int = 5, **kwargs) -> httpx.Response:
    """client.get(url) that refuses non-public addresses, following redirects itself so each hop
    is checked too."""
    current: Optional[str] = url
    trust_own = True
    for _ in range(max_redirects + 1):
        trust_own = trust_own and _is_trusted(current)
        await assert_public_url(current, trust_own)
        resp = await client.get(current, follow_redirects=False, **kwargs)
        if resp.is_redirect and resp.headers.get("location"):
            current = urljoin(str(resp.url), resp.headers["location"])
            continue
        return resp
    raise UnsafeUrlError("Too many redirects.")
