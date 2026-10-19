"""Is the agent's own domain actually working? (user, 10/19 - "a normal person would've never known
to do this"). Peter's www address was connected, but his short address forwarded to http:// and
every link 404'd - nothing told him.

Two parts:
  * `run_health_checks` (scheduled): opens each connected www address like a visitor would -
    every 2 hours for the first 4 days after it's added, then once a day for good. Two failed
    looks in a row = "site_down" (one blip isn't enough).
  * `domain_issues`: what's wrong right now, in plain words with the fix - read from the domain
    rows the verification poll and this check keep current. Shown as a banner on the Dash, on the
    domain screen, and to the admin (who can reach out).

The bare-domain forward ("forward_http") and DNS states come from domain_verification_service,
which already re-checks them every few minutes.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import SiteDomain
from app.services.website.domain_verification_service import is_www

logger = logging.getLogger(__name__)

NEW_DAYS = 4                       # checked often while new
GRACE = timedelta(hours=24)        # time to finish setting up before "still not working" shows


async def site_opens(hostname: str) -> bool:
    """The address opens the website (200) - over https, like a visitor's browser."""
    try:
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "OpenHousePal domain check"}) as client:
            resp = await client.get(f"https://{hostname}/", follow_redirects=True)
            return resp.status_code < 400
    except httpx.HTTPError:
        return False


async def run_health_checks(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(SiteDomain).where(
        SiteDomain.status == "ACTIVE", SiteDomain.redirect_target_domain_id.is_(None),
    ))).scalars().all()
    checked = 0
    for d in rows:
        created = d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=timezone.utc)
        last = d.last_checked_at and (d.last_checked_at if d.last_checked_at.tzinfo else d.last_checked_at.replace(tzinfo=timezone.utc))
        new = created > now - timedelta(days=NEW_DAYS)
        due = new or d.last_error or not last or last < now - timedelta(hours=20)
        if not due:
            continue
        ok = await site_opens(d.hostname)
        d.last_checked_at = now
        if ok:
            if d.last_error in ("site_down", "site_down_once"):
                d.last_error = None
        else:
            d.last_error = "site_down" if d.last_error in ("site_down", "site_down_once") else "site_down_once"
            logger.warning("custom domain not opening", extra={"hostname": d.hostname, "state": d.last_error})
        db.add(d)
        checked += 1
    await db.commit()
    return checked


def _apex(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def issues_for(rows: list[SiteDomain]) -> list[dict]:
    """[{hostname, code, title, fix, since}] for one agent's domain rows."""
    now = datetime.now(timezone.utc)
    out = []
    for d in rows:
        old = _aware(d.created_at) < now - GRACE
        apex = _apex(d.hostname)
        bare = bool(d.redirect_target_domain_id) and not is_www(d.hostname)
        issue = None
        if bare:
            if d.last_error == "forward_http":
                issue = ("forward_http", f"{apex} forwards to http:// - visitors who type it get \"page not found\"",
                         f"At your domain company, open the forwarding for {apex} and switch http:// to https:// "
                         f"(on GoDaddy it's the small dropdown on the left of the address box). Forward to https://www.{apex}.")
            elif d.status != "ACTIVE" and old:
                issue = ("forward_none", f"{apex} (without the www) doesn't open your website yet",
                         f"Add a forward from {apex} to https://www.{apex} at your domain company - the exact steps "
                         f"for your company are on the IDX Website > Domain screen. (Wix can't forward to outside "
                         f"websites - there, people need to type the www.)")
        elif d.status == "ACTIVE" and d.last_error == "site_down":
            issue = ("site_down", f"{d.hostname} isn't opening your website right now",
                     f"Check the www record at your domain company still points to the address on the Domain screen. "
                     f"If it does, contact OpenHousePal support - we're looking into it too.")
        elif d.status == "FAILED":
            issue = ("dns_mismatch", f"The www record for {apex} points somewhere else",
                     f"At your domain company, edit the existing \"www\" record so it points to the address on the "
                     f"Domain screen (edit the one that's there - don't add a second).")
        elif d.status in ("PENDING", "VERIFIED") and old:
            issue = ("dns_waiting" if d.status == "PENDING" else "https_waiting",
                     f"{d.hostname} still isn't connected",
                     "We can't see the www record yet. Check it was saved exactly as shown on the Domain screen."
                     if d.status == "PENDING" else
                     "Your record is right - the secure (https) part is still being set up. If this is still here "
                     "tomorrow, contact OpenHousePal support.")
        if issue:
            out.append({"hostname": d.hostname, "code": issue[0], "title": issue[1], "fix": issue[2],
                        "since": (_aware(d.updated_at) or _aware(d.created_at)).isoformat()})
    return out


async def domain_issues(db: AsyncSession, owner_id: str) -> list[dict]:
    rows = (await db.execute(select(SiteDomain).where(SiteDomain.owner_id == owner_id))).scalars().all()
    return issues_for(list(rows))


async def all_domain_issues(db: AsyncSession) -> list[dict]:
    """Every agent with a domain problem right now (admin) - newest first."""
    from app.models.database import User
    rows = (await db.execute(select(SiteDomain, User).join(User, User.id == SiteDomain.owner_id))).all()
    by_owner: dict[str, tuple] = {}
    for d, u in rows:
        by_owner.setdefault(u.id, (u, []))[1].append(d)
    out = []
    for u, ds in by_owner.values():
        for i in issues_for(ds):
            out.append({**i, "agent_id": u.id, "agent_name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email,
                        "agent_email": u.email, "brokerage": u.brokerage})
    return sorted(out, key=lambda x: x["since"], reverse=True)
