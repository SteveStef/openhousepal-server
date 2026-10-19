"""The agent's "Website" link fills itself in (user, 10/19 - "there's no sense in us having them put
their own IDX link in here if they have IDX").

  * IDX plan with a live site  -> their IDX website: their own domain once it's connected,
    otherwise <slug>.openhousepal.com. Always - it replaces whatever was typed.
  * everyone else              -> their Agent Hub page, unless they typed a website of their own
    (an address that isn't one of ours is left alone).

Upgrading, connecting a domain, switching the IDX site off or downgrading changes the link on the
next read - it's applied every time the hub config is read (`live_hub_config`: the public hub,
the agent's own editor, the email signature, the contact card), so it shows up everywhere at once
with nothing to keep in sync. Never raises.
"""
import logging
import os
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentHub, Site, SiteDomain, User

logger = logging.getLogger(__name__)


def _platform() -> str:
    return os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")


async def auto_website(db: AsyncSession, owner: User, hub_slug: Optional[str] = None) -> Optional[dict]:
    """{"url", "kind": "domain" | "idx" | "hub"} - what the agent's website link should be."""
    from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN
    from app.services.website.site_service import connected_custom_domain, default_hostname, host_resolves
    if owner.plan_tier == "IDX":
        site = (await db.execute(select(Site).where(Site.owner_id == owner.id))).scalar_one_or_none()
        if site is not None and site.is_enabled is not False and site.status == "PUBLISHED":
            custom = await connected_custom_domain(db, site)
            if custom and await host_resolves(custom):
                return {"url": f"https://{custom}", "kind": "domain"}
            if site.subdomain_slug and OHP_BASE_DOMAIN:
                return {"url": f"https://{default_hostname(site)}", "kind": "idx"}
    if hub_slug is None:
        hub_slug = (await db.execute(select(AgentHub.slug).where(AgentHub.owner_id == owner.id))).scalar_one_or_none()
    if not hub_slug:
        return None
    return {"url": f"{_platform()}/{hub_slug}", "kind": "hub"}


async def _our_hosts(db: AsyncSession, owner: User) -> set[str]:
    from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN
    hosts = {(urlparse(_platform()).hostname or "").lower(), "localhost", (OHP_BASE_DOMAIN or "").lower()}
    rows = (await db.execute(select(SiteDomain.hostname).where(SiteDomain.owner_id == owner.id))).scalars().all()
    for h in rows:
        h = (h or "").lower()
        hosts |= {h, h[4:] if h.startswith("www.") else f"www.{h}"}
    return {h for h in hosts if h}


def _host(url: str) -> str:
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    return (urlparse(u).hostname or "").lower()


async def apply_auto_website(db: AsyncSession, owner: Optional[User], cfg: dict, hub_slug: Optional[str] = None) -> dict:
    """The config with its "website" social link filled in (see module docstring), plus
    `websiteAuto` ({url, kind}) so the editor can say where it comes from."""
    if owner is None:
        return cfg
    try:
        auto = await auto_website(db, owner, hub_slug)
        if not auto:
            return cfg
        links = [dict(link) for link in (cfg.get("socialLinks") or []) if isinstance(link, dict)]
        site = next((link for link in links if link.get("id") == "website"), None)
        if site is None:
            site = {"id": "website", "label": "Website", "url": "", "enabled": True}
            links.append(site)
        current = str(site.get("url") or "").strip()
        from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN
        host = _host(current)
        ours = not current or (OHP_BASE_DOMAIN and host.endswith("." + OHP_BASE_DOMAIN.lower())) or host in await _our_hosts(db, owner)
        if auto["kind"] != "hub" or ours:
            site.update(url=auto["url"], enabled=True)
        return {**cfg, "socialLinks": links, "websiteAuto": {**auto, "applied": site.get("url") == auto["url"]}}
    except Exception:
        logger.warning("auto website link failed - serving the saved one", exc_info=True)
        return cfg
