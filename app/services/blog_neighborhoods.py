"""Blog <-> IDX neighborhood pages.

An IDX agent writing about a town can link that town's page on their IDX website (the
Neighborhoods section, <site>/<area-slug>) from the article: a "Explore the neighborhoods" row at
the end and a link on the town's first mention. Links open in a new tab so the reader keeps the
article. Stored per article in Blog.linked_areas (area slugs).

`linkable_areas` also tells the blog flow WHY linking isn't available yet (no IDX plan, no site,
site not published / switched off, no neighborhoods), so it can show the right warning.
"""
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Site, User
from app.services.website import content_adapters
from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN
from app.services.website.site_service import connected_custom_domain


async def website_base_url(db: AsyncSession, user: User) -> tuple[str | None, str | None]:
    """(base url of the agent's live IDX website, reason it isn't available)."""
    if (user.plan_tier or "").upper() != "IDX":
        return None, "no_plan"
    site = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalars().first()
    if not site:
        return None, "no_site"
    if site.is_enabled is False:
        return None, "site_off"
    if site.status != "PUBLISHED":
        return None, "not_published"
    custom = await connected_custom_domain(db, site)
    if custom:
        return f"https://{custom}", None
    host = f"{site.subdomain_slug}.{OHP_BASE_DOMAIN}"
    platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000"))
    # Locally the site is served under the app (no wildcard DNS) - same path the editor uses.
    if "localhost" in platform or "127.0.0.1" in platform:
        return f"{platform.rstrip('/')}/ohp-site-render/{host}", None
    return f"https://{host}", None


async def linkable_areas(db: AsyncSession, user: User) -> dict:
    base, reason = await website_base_url(db, user)
    areas = await content_adapters.get_served_areas_summary(db, user, 50, with_stats=True) if base or reason in ("not_published", "site_off") else []
    if base and not areas:
        reason = "no_areas"
    return {
        "ready": bool(base and areas),
        "reason": reason,
        "site_url": base,
        "areas": [
            {"slug": a["slug"], "name": a["name"], "state": a.get("state"), "photo": a.get("photo"),
             "median_price": a.get("median_price"), "active_count": a.get("active_count"),
             "url": f"{base}/{a['slug']}" if base else None}
            for a in areas
        ],
    }


async def neighborhood_links(db: AsyncSession, user: User, slugs: list | None) -> list[dict]:
    """The article's linked neighborhoods as public cards - only while the IDX site is live."""
    picked = [s for s in (slugs or []) if isinstance(s, str) and s]
    if not picked:
        return []
    info = await linkable_areas(db, user)
    if not info["ready"]:
        return []
    by_slug = {a["slug"]: a for a in info["areas"]}
    return [by_slug[s] for s in picked if s in by_slug]
