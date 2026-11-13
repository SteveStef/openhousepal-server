"""Is the bare-domain redirect server up? (owner, 11/13)

Every agent's own domain without the www (mainlinerealty.co, ...) is answered by a separate small
server (WEBSITE_APEX_IP, Caddy - see public_site_routes apex-check). On 11/12 it was switched off
at the hosting company and nobody knew until the owner happened to look - every bare domain was
dead meanwhile.

Every 3 minutes this asks that server for a page (plain http, straight to its IP). Up = it answers
with its usual "go to the www address" redirect. Two misses in a row (~6 minutes, so one slow
answer never alarms) = down:

- bell + Discord to the admin (admin_alerts, repeats every 30 min while down),
- an email to ADMIN_ALERT_EMAIL (else ADMIN_EMAIL) when it goes down, again every 6 hours while it
  stays down, and one when it's back.

Plain-text email, no Mailgun template (the live domain is at its template cap). Off on a dev copy
(DEV=true) and when WEBSITE_APEX_IP isn't set; APEX_MONITOR=false turns it off.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config.logging import get_logger

logger = get_logger(__name__)

MISSES_BEFORE_ALERT = 2
EMAIL_REPEAT_SECONDS = 6 * 60 * 60

_misses = 0
_down_since: Optional[datetime] = None
_last_email = 0.0
_last_detail = ""


def _ip() -> Optional[str]:
    # The IP never changes (every agent's DNS points at it) - watched even before WEBSITE_APEX_IP is
    # set on the live server.
    from app.services.website.domain_verification_service import WEBSITE_APEX_IP
    return os.getenv("APEX_MONITOR_IP") or WEBSITE_APEX_IP or "40.160.227.141"


async def check_once(ip: str) -> tuple[bool, str]:
    """(up, detail). Never raises."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            r = await client.get(f"http://{ip}/")
        where = r.headers.get("location", "")
        if 300 <= r.status_code < 400 and "://www." in where:
            return True, f"answered {r.status_code} -> {where}"
        return False, f"answered {r.status_code} without the usual www redirect ({where or 'no location'})"
    except Exception as e:  # noqa: BLE001
        return False, f"no answer: {type(e).__name__}: {str(e)[:200] or 'timed out'}"


def _email(subject: str, text: str) -> None:
    to = os.getenv("ADMIN_ALERT_EMAIL") or os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")
    try:
        from app.services.email_service import EmailService
        code, body = EmailService().send_raw_message(to, subject, text, from_name="OpenHousePal alerts")
        if code >= 300:
            logger.error("apex monitor email failed", extra={"code": code, "body": (body or "")[:300]})
    except Exception:  # noqa: BLE001
        logger.error("apex monitor email failed", exc_info=True)


def _eastern(dt: datetime) -> str:
    from zoneinfo import ZoneInfo
    t = dt.astimezone(ZoneInfo("America/New_York"))
    return f"{t:%b} {t.day}, {t.hour % 12 or 12}:{t:%M %p} ET"


async def scheduled_apex_redirect_check() -> None:
    global _misses, _down_since, _last_email, _last_detail
    ip = _ip()
    if not ip or os.getenv("APEX_MONITOR", "true").lower() == "false" or os.getenv("DEV", "false") == "true":
        return
    up, detail = await check_once(ip)
    from app.services.admin_alerts import alert_admin
    now = datetime.now(timezone.utc)

    if up:
        if _down_since is not None:
            mins = max(1, int((now - _down_since).total_seconds() // 60))
            logger.info("apex redirect server back", extra={"ip": ip, "down_minutes": mins})
            await alert_admin("apex", "Bare-domain redirect server is back up",
                              f"{ip} is answering again after about {mins} minutes down. {detail}")
            _email("Fixed: bare domains work again",
                   f"The redirect server ({ip}) is answering again, after about {mins} minutes down.\n\n"
                   f"Agents' own domains without the www (like example.com) open their sites again.\n")
        _misses, _down_since, _last_email = 0, None, 0.0
        return

    _misses += 1
    _last_detail = detail
    logger.warning("apex redirect check missed", extra={"ip": ip, "misses": _misses, "detail": detail})
    if _misses < MISSES_BEFORE_ALERT:
        return
    if _down_since is None:
        _down_since = now
    await alert_admin("apex", "Bare-domain redirect server is DOWN",
                      f"{ip} isn't answering ({detail}). Agents' own domains without the www won't open until "
                      f"it's back. Check the server at OVH (it may have been stopped) and start/reboot it.")
    if time.monotonic() - _last_email >= EMAIL_REPEAT_SECONDS or _last_email == 0.0:
        _last_email = time.monotonic()
        _email("DOWN: bare-domain redirect server",
               f"The redirect server ({ip}) stopped answering at about {_eastern(_down_since)}.\n\n"
               f"What breaks: agents' own website addresses typed WITHOUT the www (like example.com) "
               f"don't open. Addresses with www still work.\n\n"
               f"What to do: open OVH, find the VPS with IP {ip}, and click Start (or Reboot) if it says Stopped. "
               f"Also check Billing that it's paid / on auto-renew.\n\n"
               f"You'll get another email when it's back. Detail: {detail}\n")
