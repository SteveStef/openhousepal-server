"""DB-backed brute-force protection for /auth/login - two independent sliding windows, checked
before authentication is attempted and updated after every attempt.

Email is the primary, tightly-scoped signal: a handful of wrong passwords against one account
in a short window is a strong brute-force signal with no legitimate false-positive story.

IP is a much looser secondary net specifically because a shared office network, university wifi,
or a VPN/NAT exit node can put dozens of unrelated real agents behind the same address - a strict
per-IP limit would lock all of them out for one bad actor's (or one bot's) attempts. The IP
threshold exists only to catch a genuine large-scale distributed-credential-stuffing burst from a
single address, not to police normal shared-IP traffic.

Both scopes use the same fixed-window-that-resets-on-expiry shape as SignupVerification's own
rate limit (see verification_service.py) rather than a true sliding window, for consistency with
the one rate limiter already in this codebase.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import LoginRateLimit

WINDOW_MINUTES = 15
EMAIL_MAX_ATTEMPTS = 5
IP_MAX_ATTEMPTS = 30


async def _get_row(db: AsyncSession, scope: str, identifier: str) -> Optional[LoginRateLimit]:
    result = await db.execute(
        select(LoginRateLimit).where(LoginRateLimit.scope == scope, LoginRateLimit.identifier == identifier)
    )
    return result.scalar_one_or_none()


def _blocked(row: Optional[LoginRateLimit], now: datetime, max_attempts: int) -> Tuple[bool, int]:
    if not row:
        return False, 0
    age = now - row.window_started_at
    if age >= timedelta(minutes=WINDOW_MINUTES):
        return False, 0
    if row.attempts < max_attempts:
        return False, 0
    wait_minutes = max(1, int(WINDOW_MINUTES - age.total_seconds() / 60))
    return True, wait_minutes


async def check_login_allowed(db: AsyncSession, email: str, ip: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Read-only check, called before attempting authentication. Blocks on whichever scope is
    currently over its threshold within the rolling window."""
    now = datetime.now(timezone.utc)

    email_row = await _get_row(db, 'email', email)
    is_blocked, wait_minutes = _blocked(email_row, now, EMAIL_MAX_ATTEMPTS)
    if is_blocked:
        return False, f"Too many failed login attempts. Please try again in {wait_minutes} minute(s)."

    if ip:
        ip_row = await _get_row(db, 'ip', ip)
        is_blocked, wait_minutes = _blocked(ip_row, now, IP_MAX_ATTEMPTS)
        if is_blocked:
            return False, f"Too many login attempts from your network. Please try again in {wait_minutes} minute(s)."

    return True, None


async def record_attempt(db: AsyncSession, email: str, ip: Optional[str], success: bool) -> None:
    """Called after every login attempt, success or failure, to update the window counters.
    A successful login resets that email's own counter - strong evidence the real owner is
    back - but never resets the IP counter, since other bad actors could still share it."""
    now = datetime.now(timezone.utc)

    email_row = await _get_row(db, 'email', email)
    if success:
        if email_row:
            email_row.attempts = 0
            email_row.window_started_at = now
        await db.commit()
        return

    if email_row is None:
        db.add(LoginRateLimit(scope='email', identifier=email, attempts=1, window_started_at=now))
    elif (now - email_row.window_started_at) >= timedelta(minutes=WINDOW_MINUTES):
        email_row.attempts = 1
        email_row.window_started_at = now
    else:
        email_row.attempts += 1

    if ip:
        ip_row = await _get_row(db, 'ip', ip)
        if ip_row is None:
            db.add(LoginRateLimit(scope='ip', identifier=ip, attempts=1, window_started_at=now))
        elif (now - ip_row.window_started_at) >= timedelta(minutes=WINDOW_MINUTES):
            ip_row.attempts = 1
            ip_row.window_started_at = now
        else:
            ip_row.attempts += 1

    await db.commit()
