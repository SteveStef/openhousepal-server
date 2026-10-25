"""Real DNS verification for custom domains, replacing Phase 1's manual status-PATCH stand-in.

Two independent checks, matching the two things that actually have to be true before a
custom domain is safe to call ACTIVE:

  1. DNS check (-> VERIFIED): does the hostname's DNS point at our known delivery target?
     Done via DNS-over-HTTPS (reusing httpx, already a dependency - no new package) rather
     than a stdlib resolver, since this process has no guaranteed local DNS resolver access
     in every deployment environment and DoH is simple, dependency-light, and works anywhere
     outbound HTTPS works.
  2. Reachability check (-> ACTIVE): once DNS is right, does the hostname actually serve the
     site over HTTPS with a valid cert? This is what actually confirms Caddy's on-demand TLS
     finished, rather than just assuming SSL will work because DNS looks right.

Both checks are read-only and safe to run repeatedly - see the domain_verification_poll job.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import SiteDomain
from app.schemas.site import DnsInstruction
from app.services.website import domain_resolution_service

# The hostname agents CNAME their domain to. In production this is whatever hostname the
# reverse proxy (Caddy - see Caddyfile) actually listens on for custom-domain traffic.
WEBSITE_CNAME_TARGET = os.getenv("WEBSITE_CNAME_TARGET", "sites.openhousepal.com")
# Only set in a deployment that has a stable public IP - enables apex (root) A-record
# verification as an alternative to registrar-level forwarding. Left unset by default since
# self-hosted Docker deployments don't have one until real infra is chosen.
WEBSITE_APEX_IP = os.getenv("WEBSITE_APEX_IP") or None

_DOH_URL = "https://cloudflare-dns.com/dns-query"


def is_www(hostname: str) -> bool:
    return hostname.startswith("www.")


def sibling_hostname(hostname: str) -> str:
    """peterstefanatos.com <-> www.peterstefanatos.com"""
    return hostname[4:] if is_www(hostname) else f"www.{hostname}"


def apex_of(hostname: str) -> str:
    return hostname[4:] if is_www(hostname) else hostname


def build_instructions(hostname: str) -> List[DnsInstruction]:
    """What the agent adds at their own domain company. One record every company supports - a
    CNAME on "www" (or on their subdomain) - plus that company's built-in forwarding so the bare
    domain goes to the www address. No ALIAS/ANAME (GoDaddy and others don't have it) and no IP."""
    if not is_www(hostname) and hostname.count(".") >= 2 and not hostname.endswith((".co.uk", ".com.au")):
        # a subdomain they chose, like homes.janesmith.com - just the one record
        label = hostname.split(".")[0]
        return [DnsInstruction(type="CNAME", name=label, value=WEBSITE_CNAME_TARGET,
                               note=f"Add this record so {hostname} opens your website.")]
    apex = apex_of(hostname)
    www = DnsInstruction(type="CNAME", name="www", value=WEBSITE_CNAME_TARGET,
                         note=f"Points www.{apex} at your OpenHousePal website.")
    if WEBSITE_APEX_IP:
        # Our own redirect server answers the bare domain and sends every page - not just the home
        # page - to the same page on www (registrar forwarding drops the page, see _forwards_paths).
        return [www, DnsInstruction(type="A", name="@", value=WEBSITE_APEX_IP,
                                    note=f"Sends {apex} (without www) and every page on it to your website. Replaces any forwarding on {apex}.")]
    return [
        www,
        DnsInstruction(type="FORWARD", name="@", value=f"https://www.{apex}",
                       note=f"Sends people who type {apex} (without www) to the same website. Use your domain company's \"forwarding\" or \"redirect\" setting - permanent (301)."),
    ]


@dataclass
class DnsCheckResult:
    matched: bool
    found_value: Optional[str] = None
    error: Optional[str] = None  # short, agent-safe


async def _doh_lookup(client: httpx.AsyncClient, hostname: str, record_type: str) -> List[str]:
    resp = await client.get(
        _DOH_URL, params={"name": hostname, "type": record_type},
        headers={"accept": "application/dns-json"}, timeout=5.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return [a.get("data", "").rstrip(".").lower() for a in data.get("Answer", []) if a.get("data")]


async def check_dns(hostname: str) -> DnsCheckResult:
    target = WEBSITE_CNAME_TARGET.rstrip(".").lower()
    try:
        async with httpx.AsyncClient() as client:
            if is_www(hostname):
                values = await _doh_lookup(client, hostname, "CNAME")
                if not values:
                    return DnsCheckResult(matched=False)  # PENDING - not propagated / not added yet
                if target in values:
                    return DnsCheckResult(matched=True, found_value=values[0])
                return DnsCheckResult(matched=False, found_value=values[0], error="dns_mismatch")

            # Apex domain. A root domain can't have a CNAME (DNS spec), but many providers
            # (Cloudflare, Route 53, DNSimple, ...) offer an ALIAS/ANAME record that LOOKS like
            # a plain A record to any outside DNS query - it's resolved to the target's current
            # A record(s) *by the provider*, not statically stored. So rather than requiring a
            # hardcoded IP, look up the target's own current A records and check for overlap -
            # this verifies an ALIAS/ANAME setup automatically, with zero extra agent-facing
            # config, and still works if WEBSITE_APEX_IP is set as an explicit override (e.g.
            # a deployment that knows its IP before sites.<domain> itself is even live).
            apex_values = await _doh_lookup(client, hostname, "A")
            if not apex_values:
                return DnsCheckResult(matched=False)

            expected_ips = {WEBSITE_APEX_IP} if WEBSITE_APEX_IP else set(await _doh_lookup(client, target, "A"))
            if expected_ips and (set(apex_values) & expected_ips):
                return DnsCheckResult(matched=True, found_value=apex_values[0])
            return DnsCheckResult(matched=False, found_value=apex_values[0], error="dns_mismatch")
    except httpx.HTTPError:
        return DnsCheckResult(matched=False, error="lookup_failed")


FORWARD_PATH_PROBE = "/ohp-forward-check"


async def _forwards_paths(apex: str) -> bool:
    """Does https://<apex>/some/page land on https://www.<apex>/some/page? GoDaddy's forwarding
    only sends the home page - every other page on the bare domain is a 404 on GoDaddy's own server,
    so an old site's pages that Google still lists dead-end (user 10/20, mainlinerealty.co/about).
    Our site answers the probe path with its own "page not found", so only where it landed counts."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(f"https://{apex}{FORWARD_PATH_PROBE}")
            return (resp.url.host or "").lower() == f"www.{apex}" and resp.url.path.rstrip("/") == FORWARD_PATH_PROBE
    except httpx.HTTPError:
        return False


async def check_forwarding(apex: str) -> str:
    """Does the bare domain forward to its www address (the agent's domain-company forwarding)?
    'ok' = it lands on the working https site, pages included; 'no_paths' = only the home page
    forwards (other pages are "page not found" - see _forwards_paths); 'http' = it forwards to
    http://www... instead (GoDaddy's forwarding defaults to http:// - those visitors got a 404,
    user 10/19); 'none' = no forward yet."""
    target = f"www.{apex}"
    saw_http = False
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(f"{scheme}://{apex}/")
                hops = [*resp.history, resp]
                if any((h.url.host or "").lower() == target and h.url.scheme == "http" for h in hops):
                    saw_http = True
                if (resp.url.host or "").lower() == target and resp.url.scheme == "https" and resp.status_code < 400:
                    return "ok" if await _forwards_paths(apex) else "no_paths"
        except httpx.HTTPError:
            continue
    return "http" if saw_http else "none"


async def check_https_reachable(hostname: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"https://{hostname}/", follow_redirects=True)
            return resp.status_code < 500
    except httpx.HTTPError:
        return False


async def _get_or_create_sibling(db: AsyncSession, domain: SiteDomain) -> SiteDomain:
    sibling_host = sibling_hostname(domain.hostname)
    existing = (await db.execute(select(SiteDomain).where(SiteDomain.hostname == sibling_host))).scalar_one_or_none()
    if existing:
        return existing
    sibling = SiteDomain(
        site_id=domain.site_id, owner_id=domain.owner_id, hostname=sibling_host,
        status="PENDING", redirect_target_domain_id=domain.id,
    )
    db.add(sibling)
    await db.flush()
    return sibling


async def verify_domain(db: AsyncSession, domain: SiteDomain) -> SiteDomain:
    """Runs both checks and advances status accordingly. Never regresses ACTIVE straight to
    FAILED on one bad check - see the poll job for how transient failures are handled."""
    domain.last_checked_at = datetime.now(timezone.utc)
    prev_status = domain.status

    # The bare domain of a www pair: connected once it forwards to the working https www address
    # (or, on a company that offers ALIAS at the root, once its DNS points at us like the www
    # record). Re-checked while connected too: a forward to http:// isn't connected - it goes back
    # to waiting with "forward_http", so the panel says exactly what to change.
    if domain.redirect_target_domain_id and not is_www(domain.hostname):
        fwd = await check_forwarding(domain.hostname)
        if fwd in ("ok", "no_paths") or (fwd == "none" and domain.status == "ACTIVE"):
            # (one failed look at an already-working forward isn't enough to undo it)
            if fwd in ("ok", "no_paths"):
                # The home page forwards, so it's connected - but "forward_no_paths" makes the panel
                # say that other pages typed (or clicked in Google) without www don't.
                domain.status = "ACTIVE"
                domain.verified_at = domain.verified_at or datetime.now(timezone.utc)
                domain.last_error = "forward_no_paths" if fwd == "no_paths" else None
            db.add(domain)
            await db.commit()
            await db.refresh(domain)
            domain_resolution_service.invalidate_site(domain.site_id)
            return domain
        if fwd == "http":
            domain.status = "PENDING"
            domain.last_error = "forward_http"
            db.add(domain)
            await db.commit()
            await db.refresh(domain)
            domain_resolution_service.invalidate_site(domain.site_id)
            return domain

    if domain.status in ("PENDING", "FAILED"):
        result = await check_dns(domain.hostname)
        if result.matched:
            domain.status = "VERIFIED"
            domain.verified_at = domain.verified_at or datetime.now(timezone.utc)
            domain.last_error = None
            # DNS control of one variant (www or apex) implies control of the other's zone -
            # auto-link the sibling instead of making the agent connect both separately.
            sibling = await _get_or_create_sibling(db, domain)
            if sibling.status == "PENDING" and sibling.redirect_target_domain_id is None:
                sibling.redirect_target_domain_id = domain.id
        elif result.error == "dns_mismatch":
            domain.status = "FAILED"
            domain.last_error = "dns_mismatch"
        # else: still PENDING, no error surfaced - just still waiting/propagating

    if domain.status == "VERIFIED":
        # Cloudflare issues the HTTPS certificate once it knows the hostname (no-op when not set up).
        from app.services.website import cloudflare_saas_service
        await cloudflare_saas_service.register(domain.hostname)
        if await check_https_reachable(domain.hostname):
            domain.status = "ACTIVE"
            domain.last_error = None

    db.add(domain)
    await db.commit()
    await db.refresh(domain)
    domain_resolution_service.invalidate_site(domain.site_id)
    if domain.status != prev_status and domain.status in ("ACTIVE", "FAILED") and not domain.redirect_target_domain_id:
        await _email_domain_result(db, domain)
    if domain.status == "ACTIVE" and prev_status != "ACTIVE" and not domain.redirect_target_domain_id:
        # The first own domain that goes live becomes the website's default address (owner, 10/24 -
        # bought domains already did this, see domain_purchase_service.make_primary_when_live), so
        # shared links move onto it without a hidden "Make Default" click. Never steals it from
        # another domain the agent already made default.
        try:
            from sqlalchemy import select as _select
            from app.models.database import Site, User
            from app.services.website import site_service
            has_primary = (await db.execute(_select(SiteDomain.id).where(
                SiteDomain.site_id == domain.site_id, SiteDomain.is_primary.is_(True)))).first()
            if not has_primary and not domain.is_primary:
                site = await db.get(Site, domain.site_id)
                owner = await db.get(User, domain.owner_id)
                if site and owner:
                    await site_service.set_primary_domain(db, site, domain, owner)
        except Exception:  # noqa: BLE001 - the domain is live either way; "Make Default" still works
            pass
        # Pages from the site that used to live on this domain (still in Google) -> pages here.
        from app.services.website import old_links_service
        old_links_service.queue_scan(domain.site_id)
    return domain


async def _email_domain_result(db: AsyncSession, domain: SiteDomain) -> None:
    try:
        from app.models.database import User
        from app.services.agent_email_service import absolute, email_agent
        owner = await db.get(User, domain.owner_id)
        if domain.status == "ACTIVE":
            email_agent(owner, "DOMAIN_VERIFIED", "domain_verified", f"{domain.hostname} is live",
                        {"domain": domain.hostname, "link": f"https://{domain.hostname}"})
        else:
            rec = next((r for r in build_instructions(domain.hostname) if r.type == "CNAME"), None)
            email_agent(owner, "DOMAIN_FAILED", "domain_verification_failed", f"We couldn't verify {domain.hostname}",
                        {"domain": domain.hostname, "record_type": getattr(rec, "type", None), "record_host": getattr(rec, "name", None),
                         "record_value": getattr(rec, "value", None), "link": absolute("/settings#idx-domain")})
    except Exception:
        pass
