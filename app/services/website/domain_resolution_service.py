"""hostname -> Site resolution for the public Website Builder renderer.

No Redis exists in this stack and the backend runs a single uvicorn process (see
backend/Dockerfile), so a hand-rolled in-process TTL cache is the right level of
infrastructure here, not a new dependency. This is only meaningfully "global" because
there's one process today - the trigger to add Redis (or any shared invalidation channel)
is scaling to multiple workers/replicas *and* needing sub-TTL consistency, not before.
"""
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PropertyWebsite, Site, SiteDomain

OHP_BASE_DOMAIN = os.getenv("OHP_BASE_DOMAIN", "openhousepal.com")
_SUBDOMAIN_PATTERN = re.compile(rf"^(?P<slug>[a-z0-9-]+)\.{re.escape(OHP_BASE_DOMAIN)}$")

_HIT_TTL_SECONDS = 60
# Shorter TTL for misses so a typo'd/misconfigured domain can't hammer Postgres, but a
# not-yet-DNS-propagated domain also recovers quickly once it does resolve.
_MISS_TTL_SECONDS = 15


@dataclass
class ResolvedSite:
    site_id: str
    owner_id: str
    subdomain_slug: str
    published_snapshot: dict
    # Set when the requested hostname is the non-primary half of an apex/www pair (or any
    # other non-primary domain) - the caller should 301 to this hostname instead of
    # rendering, preserving the request path. See site_service.set_primary_domain.
    redirect_to: Optional[str] = None
    # The site's current canonical address (its primary domain if one is ACTIVE + primary,
    # otherwise its OHP subdomain) - used for <link rel="canonical"> regardless of which
    # hostname actually served the request, so every domain variant points search engines at
    # the one true URL instead of creating duplicate-content copies.
    canonical_hostname: Optional[str] = None


# host -> (expires_at monotonic seconds, resolved-or-None)
_cache: Dict[str, Tuple[float, Optional[ResolvedSite]]] = {}


def subdomain_slug_from_hostname(host: str) -> Optional[str]:
    """Returns the <slug> if host matches <slug>.OHP_BASE_DOMAIN, else None. Shared with the
    Caddy ask endpoint (public_site_routes.domain_allowed) so subdomain cert requests are
    gated the same way custom-domain ones are, not just derived twice."""
    match = _SUBDOMAIN_PATTERN.match((host or "").strip().lower())
    return match.group("slug") if match else None


def invalidate_site(site_id: str) -> None:
    """Called after publish and after any domain lifecycle change (verified, activated,
    removed, primary changed), so the next request re-fetches instead of serving a stale
    cached entry for up to the TTL window. Best-effort (runs after commit, not
    transactional) - acceptable at this feature's change frequency."""
    stale_hosts = [host for host, (_, resolved) in _cache.items() if resolved and resolved.site_id == site_id]
    for host in stale_hosts:
        _cache.pop(host, None)


async def resolve_hostname(db: AsyncSession, host: str) -> Optional[ResolvedSite]:
    host = (host or "").strip().lower()
    if not host:
        return None

    cached = _cache.get(host)
    if cached is not None:
        expires_at, resolved = cached
        if time.monotonic() < expires_at:
            return resolved
        _cache.pop(host, None)

    resolved = await _resolve_uncached(db, host)
    ttl = _HIT_TTL_SECONDS if resolved else _MISS_TTL_SECONDS
    _cache[host] = (time.monotonic() + ttl, resolved)
    return resolved


async def canonical_hostname(db: AsyncSession, site: Site) -> str:
    result = await db.execute(
        select(SiteDomain.hostname).where(
            SiteDomain.site_id == site.id, SiteDomain.is_primary.is_(True), SiteDomain.status == "ACTIVE",
        )
    )
    primary = result.scalar_one_or_none()
    return primary or f"{site.subdomain_slug}.{OHP_BASE_DOMAIN}"


async def _resolve_uncached(db: AsyncSession, host: str) -> Optional[ResolvedSite]:
    # OHP subdomain: derivable from the hostname string itself, one indexed query, never
    # touches site_domains. Never redirected away, even if a custom primary domain exists -
    # it has to keep working if the custom domain breaks (see canonical_hostname instead).
    slug = subdomain_slug_from_hostname(host)
    if slug:
        result = await db.execute(
            select(Site).where(Site.subdomain_slug == slug, Site.status == "PUBLISHED")
        )
        site = result.scalar_one_or_none()
        if not site:
            return None
        return await _to_resolved(db, site)

    # External custom domain: the only path that needs the join.
    result = await db.execute(
        select(Site, SiteDomain)
        .join(SiteDomain, SiteDomain.site_id == Site.id)
        .where(SiteDomain.hostname == host, SiteDomain.status == "ACTIVE", Site.status == "PUBLISHED")
    )
    row = result.first()
    if not row:
        return None
    site, domain = row
    if domain.redirect_target_domain_id:
        target = (await db.execute(
            select(SiteDomain.hostname).where(SiteDomain.id == domain.redirect_target_domain_id)
        )).scalar_one_or_none()
        if target:
            return ResolvedSite(site_id=site.id, owner_id=site.owner_id, subdomain_slug=site.subdomain_slug, published_snapshot={}, redirect_to=target)
    return await _to_resolved(db, site)


async def _to_resolved(db: AsyncSession, site: Site) -> ResolvedSite:
    return ResolvedSite(
        site_id=site.id,
        owner_id=site.owner_id,
        subdomain_slug=site.subdomain_slug,
        published_snapshot=site.published_snapshot or {},
        canonical_hostname=await canonical_hostname(db, site),
    )


# ---------------------------------------------------------------------------
# Single Property Websites - a separate, additive resolver. Never touches the Site cache/table
# lookups above, so it cannot regress existing Website Builder / Agent Hub routing. A property
# site gets its own dedicated <slug>.OHP_BASE_DOMAIN subdomain (one indexed query, exactly like
# Site's own subdomain path), and - once an owner has a verified custom domain on their Site -
# is also reachable at <that domain>/listings/<slug>, reusing SiteDomain's existing verification
# rather than building a second domain-ownership system.
# ---------------------------------------------------------------------------

@dataclass
class ResolvedPropertyWebsite:
    property_website_id: str
    owner_id: str
    slug: str
    published_snapshot: dict
    canonical_hostname: Optional[str] = None


_property_cache: Dict[str, Tuple[float, Optional[ResolvedPropertyWebsite]]] = {}


def invalidate_property_website(property_website_id: str) -> None:
    """Mirrors invalidate_site - called after publish/unpublish so the next request re-fetches."""
    stale_keys = [key for key, (_, resolved) in _property_cache.items() if resolved and resolved.property_website_id == property_website_id]
    for key in stale_keys:
        _property_cache.pop(key, None)


async def resolve_property_site(db: AsyncSession, host: str, path: str = "") -> Optional[ResolvedPropertyWebsite]:
    host = (host or "").strip().lower()
    if not host:
        return None
    # Only the custom-domain branch needs the path (to pick which of an owner's many property
    # sites); folded into the cache key so two different /listings/<slug> paths under the same
    # custom domain don't collide in the cache.
    parts = [p for p in (path or "").strip("/").split("/") if p]
    path_slug = parts[1][:200] if len(parts) >= 2 and parts[0] == "listings" and not subdomain_slug_from_hostname(host) else ""
    cache_key = f"{host}::{path_slug}"

    cached = _property_cache.get(cache_key)
    if cached is not None:
        expires_at, resolved = cached
        if time.monotonic() < expires_at:
            return resolved
        _property_cache.pop(cache_key, None)

    resolved = await _resolve_property_uncached(db, host, path)
    ttl = _HIT_TTL_SECONDS if resolved else _MISS_TTL_SECONDS
    _property_cache[cache_key] = (time.monotonic() + ttl, resolved)
    return resolved


async def _resolve_property_uncached(db: AsyncSession, host: str, path: str) -> Optional[ResolvedPropertyWebsite]:
    slug = subdomain_slug_from_hostname(host)
    if slug:
        result = await db.execute(
            select(PropertyWebsite).where(PropertyWebsite.slug == slug, PropertyWebsite.status == "PUBLISHED")
        )
        site = result.scalar_one_or_none()
        if not site:
            return None
        return ResolvedPropertyWebsite(
            property_website_id=site.id, owner_id=site.owner_id, slug=site.slug,
            published_snapshot=site.published_snapshot or {}, canonical_hostname=f"{site.slug}.{OHP_BASE_DOMAIN}",
        )

    # Custom domain: only reachable at /listings/<slug> under an owner's own connected, ACTIVE
    # custom domain (the same one their whole-agent Site uses) - never a bare hostname hit, since
    # one custom domain can host many property sites.
    parts = [p for p in (path or "").strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "listings":
        return None
    result = await db.execute(
        select(PropertyWebsite)
        .join(Site, Site.owner_id == PropertyWebsite.owner_id)
        .join(SiteDomain, SiteDomain.site_id == Site.id)
        .where(
            PropertyWebsite.slug == parts[1], PropertyWebsite.status == "PUBLISHED",
            SiteDomain.hostname == host, SiteDomain.status == "ACTIVE",
        )
    )
    site = result.scalars().first()
    if not site:
        return None
    return ResolvedPropertyWebsite(
        property_website_id=site.id, owner_id=site.owner_id, slug=site.slug,
        published_snapshot=site.published_snapshot or {}, canonical_hostname=f"{host}/listings/{site.slug}",
    )
