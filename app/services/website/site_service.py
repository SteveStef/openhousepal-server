import os
import re
import urllib.parse
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import OpenHouseEvent, Site, SiteDomain, StudioBrandKit, User
from app.services.website import domain_resolution_service
from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HOSTNAME_PATTERN = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

# Top-level names a subdomain slug must never collide with - literal app routes (dashboard
# pages, the public-site rewrite target itself) and conventional reserved hostnames. Not
# exhaustive, but cheap insurance against an agent picking e.g. "app" or "admin".
RESERVED_SLUGS = {
    "www", "app", "api", "admin", "mail", "ftp", "assets", "static", "cdn",
    "ohp-site-render", "dash", "leads", "studio", "assistant", "login", "register",
    "settings", "showcase", "showcases", "open-house", "open-houses", "discovery",
    "checkout", "terms", "privacy", "unsubscribe", "agent-hub", "setup",
}


def _slugify(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return normalized or "site"


async def _unique_subdomain_slug(db: AsyncSession, base: str) -> str:
    base = _slugify(base)
    candidate = base
    suffix = 2
    while True:
        if candidate not in RESERVED_SLUGS and SLUG_PATTERN.fullmatch(candidate):
            existing = await db.execute(select(Site.id).where(Site.subdomain_slug == candidate))
            if existing.scalar_one_or_none() is None:
                return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


async def get_or_create_site(db: AsyncSession, user: User) -> Site:
    result = await db.execute(select(Site).where(Site.owner_id == user.id))
    site = result.scalar_one_or_none()
    if site:
        return site

    slug = await _unique_subdomain_slug(db, f"{user.first_name}-{user.last_name}")

    # New sites start on the clean 'default' look in light mode (an agent can switch to Luxe or
    # dark on the Design tab); sites created before the template field existed stay on Luxe.
    draft_data = {"pages": [], "theme": {
        "primary_color": "#111827", "secondary_color": "#C9A24D",
        "heading_font": "serif", "body_font": "sans", "logo_url": None,
        "template": "default", "mode": "light", "allow_mode_toggle": True,
    }}
    brand_kit = (await db.execute(
        select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id)
    )).scalar_one_or_none()
    if brand_kit:
        draft_data["theme"] = {
            "primary_color": brand_kit.primary_color,
            "secondary_color": brand_kit.secondary_color,
            "heading_font": brand_kit.heading_font,
            "body_font": brand_kit.body_font,
            "logo_url": brand_kit.personal_logo_url,
            "template": "default", "mode": "light", "allow_mode_toggle": True,
        }

    site = Site(owner_id=user.id, subdomain_slug=slug, draft_data=draft_data, published_snapshot={"pages": [], "theme": draft_data["theme"]})
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return site


class InvalidHostname(ValueError):
    pass


def validate_custom_hostname(hostname: str) -> str:
    """Normalizes and validates a hostname an agent wants to connect. Raises InvalidHostname
    with an agent-safe message on anything malformed or that would let someone claim an OHP
    subdomain (own or another tenant's) through the custom-domain path instead of the
    dedicated, auto-assigned one."""
    hostname = hostname.strip().lower().rstrip(".")
    if hostname.startswith("http://") or hostname.startswith("https://"):
        hostname = hostname.split("://", 1)[1]
    hostname = hostname.split("/")[0].split(":")[0]

    if not HOSTNAME_PATTERN.fullmatch(hostname):
        raise InvalidHostname("That doesn't look like a valid domain name.")
    if hostname == OHP_BASE_DOMAIN or hostname.endswith(f".{OHP_BASE_DOMAIN}"):
        raise InvalidHostname(f"{hostname} is an OpenHousePal-managed address and can't be connected as a custom domain.")
    return hostname


async def set_primary_domain(db: AsyncSession, site: Site, domain: SiteDomain, user: User) -> None:
    """At most one primary per site. Only an ACTIVE domain (DNS confirmed AND actually
    serving over HTTPS) can be made primary - a broken domain should never become the
    canonical address."""
    if domain.status != "ACTIVE":
        raise ValueError("Only a fully connected (Active) domain can be made primary.")
    await db.execute(
        update(SiteDomain).where(SiteDomain.site_id == site.id, SiteDomain.id != domain.id).values(is_primary=False)
    )
    domain.is_primary = True
    db.add(domain)
    await db.commit()
    domain_resolution_service.invalidate_site(site.id)
    await regenerate_owner_idx_links(db, user)


async def resolve_owner_public_base_url(db: AsyncSession, user: User) -> str:
    """The base URL (scheme + host) new QR codes and shareable links for this agent embed.

    One rule for every plan: links live on openhousepal.com, exactly the same with or without IDX.
    The only thing that changes them is an IDX agent's OWN connected domain (ACTIVE + primary, with
    "use my own domain" on, on a published site that isn't switched off) - then everything they
    hand out (sign-in QR codes, forms, Showcases, CMAs, Spotlights, the hub) moves onto it. The
    free <slug>.openhousepal.com website address is never used for links. Pair with owner_links()
    for the paths, which are shorter on an own domain (/forms/x, /reviews, ...).
    Only fresh generation calls this - records already printed/shared keep their old address and
    keep working (see regenerate_owner_idx_links for the one case that rewrites: the domain changing)."""
    platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000"))
    if user.plan_tier != "IDX" or getattr(user, "links_use_own_domain", True) is False:
        return platform
    site = await get_or_create_site(db, user)
    if site.is_enabled is False or site.status != "PUBLISHED":
        return platform
    custom = await connected_custom_domain(db, site)
    # A QR code or link must never point at an address that doesn't open.
    if not custom or not await host_resolves(custom):
        return platform
    return f"https://{custom}"


class OwnerLinks:
    """Every agent link in one place. On openhousepal.com the Agent Hub pages sit under the hub's
    slug (/peter-stefanatos/selling); on the agent's own domain they're short (/forms/selling,
    /reviews, /sales, /hub). Showcases, open houses, CMAs keep the same path everywhere."""

    def __init__(self, base: str, own_domain: bool, hub_slug: str | None):
        self.base = base.rstrip("/")
        self.own_domain = own_domain
        self.hub_slug = hub_slug or ""

    def hub(self) -> str:
        return f"{self.base}/hub" if self.own_domain else f"{self.base}/{self.hub_slug}"

    def form(self, form_slug: str) -> str:
        return f"{self.base}/forms/{form_slug}" if self.own_domain else f"{self.base}/{self.hub_slug}/{form_slug}"

    def sales(self) -> str:
        return f"{self.base}/sales" if self.own_domain else f"{self.base}/{self.hub_slug}/sales"

    def reviews(self) -> str:
        return f"{self.base}/reviews" if self.own_domain else f"{self.base}/{self.hub_slug}/review"

    def showcase(self, token: str) -> str:
        return f"{self.base}/showcase/{token}"

    def cma(self, slug: str) -> str:
        return f"{self.base}/cma/{slug}"


async def owner_links(db: AsyncSession, user: User, hub_slug: str | None = None) -> OwnerLinks:
    base = await resolve_owner_public_base_url(db, user)
    platform = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000"))
    if hub_slug is None:
        from app.models.database import AgentHub
        hub_slug = (await db.execute(select(AgentHub.slug).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    return OwnerLinks(base, base.rstrip("/") != platform.rstrip("/"), hub_slug)


_DNS_CACHE: dict[str, tuple[float, bool]] = {}


async def host_resolves(hostname: str) -> bool:
    """Whether `hostname` exists in public DNS - cached (an hour when it does, 10 minutes when it
    doesn't, so links switch over soon after the DNS record is added). Never raises."""
    import asyncio
    import socket
    import time
    now = time.monotonic()
    hit = _DNS_CACHE.get(hostname)
    if hit and now < hit[0]:
        return hit[1]
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(hostname, 443, type=socket.SOCK_STREAM), timeout=3)
        ok = True
    except Exception:  # noqa: BLE001 - NXDOMAIN, timeout, no network
        ok = False
    _DNS_CACHE[hostname] = (now + (3600 if ok else 600), ok)
    return ok


def default_hostname(site: Site) -> str:
    """The free, auto-assigned IDX address - <slug>.openhousepal.com. Never removable."""
    return f"{site.subdomain_slug}.{OHP_BASE_DOMAIN}"


async def connected_custom_domain(db: AsyncSession, site: Site) -> str | None:
    """The agent's own uploaded domain, only once it's fully connected (ACTIVE) and primary -
    the only thing "your own domain" ever refers to. None while pending/unverified/absent."""
    result = await db.execute(
        select(SiteDomain.hostname).where(
            SiteDomain.site_id == site.id, SiteDomain.is_primary.is_(True), SiteDomain.status == "ACTIVE",
        )
    )
    return result.scalar_one_or_none()


async def set_site_enabled(db: AsyncSession, user: User, enabled: bool) -> Site:
    """Settings -> IDX Website on/off switch (and the post-upgrade "do you want your IDX site?"
    question). Re-points active open houses' QR codes at whichever address is now correct."""
    site = await get_or_create_site(db, user)
    site.is_enabled = enabled
    db.add(site)
    await db.commit()
    await db.refresh(site)
    domain_resolution_service.invalidate_site(site.id)
    await regenerate_owner_idx_links(db, user)
    return site


def reset_idx_site_question_on_upgrade(user: User, prev_tier: str | None, new_tier: str | None) -> None:
    """Moving ONTO IDX (a new purchase or an upgrade, not a renewal) re-asks the one-time
    "do you want to use your IDX website?" question (IdxDomainMigrationModal is shown while
    idx_domain_migration_agreed_at is null). Caller commits."""
    if new_tier == "IDX" and prev_tier != "IDX":
        user.idx_domain_migration_agreed_at = None


def build_qr_code_image_url(base_url: str, path: str) -> str:
    """Same qrserver.com-backed QR image every open house QR has always used - centralized so
    creation and post-domain-change regeneration never drift into two different QR styles."""
    return f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(base_url + path)}"


async def regenerate_owner_idx_links(db: AsyncSession, user: User) -> None:
    """Called right after an IDX agent's primary domain changes (a real custom domain taking
    over from the temporary <slug>.OHP_BASE_DOMAIN subdomain, or vice versa if they remove it).
    Rewrites qr_code on their currently-active open houses to the new domain, so a kit they're
    actively using picks up the new branding instead of staying stuck on the old one. form_url
    itself is always stored as a bare path (e.g. "/open-house/{id}"), never a full URL, so it
    needs no rewrite - only the QR image's encoded target domain does.
    Deliberately does NOT touch closed/deleted open houses - those are history, not something
    anyone's about to reprint, and old QR images (once printed) can't be un-printed anyway."""
    base_url = await resolve_owner_public_base_url(db, user)
    result = await db.execute(
        select(OpenHouseEvent).where(
            OpenHouseEvent.agent_id == user.id,
            OpenHouseEvent.is_active.is_(True),
            OpenHouseEvent.is_deleted.is_(False),
        )
    )
    for open_house in result.scalars().all():
        if open_house.form_url:
            open_house.qr_code = build_qr_code_image_url(base_url, open_house.form_url)
            db.add(open_house)
    await db.commit()
    # Recent follow-up calendar events carry a Showcase link on the old domain - re-push them.
    from app.services.tasks_service import refresh_followup_links
    refresh_followup_links(user.id)


async def publish_site(db: AsyncSession, site: Site) -> Site:
    """Phase 1's entire "optimization" step is a deep copy - deliberately trivial. This is the
    extension point for later CDN-rewriting/pre-resolved-IDX-data work, with no schema change."""
    site.published_snapshot = deepcopy(site.draft_data)
    site.status = "PUBLISHED"
    site.published_at = datetime.now(timezone.utc)
    db.add(site)
    await db.commit()
    await db.refresh(site)
    domain_resolution_service.invalidate_site(site.id)
    return site
