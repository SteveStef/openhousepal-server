"""Lightweight abuse throttle for the unauthenticated visitor-facing POST endpoints
(open-house sign-in, public form submissions).

Sliding window keyed by client IP: at most 20 submissions a minute and 120 an hour from one network.
It caps a bot from hammering these endpoints (each sign-in also spawns an auto-showcase, which does
real work). Counted in Postgres (app/services/cluster.py, 11/14), so the limit is the same no matter
how many copies of the API are running - it used to live in each copy's memory, which gave a
caller N x the limit with N copies.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.utils.request_ip import get_client_ip

# window seconds -> max requests in that window, per IP
_WINDOW_SECONDS = 60
_MAX_PER_WINDOW = 20
# a slower long window so a patient bot can't just trickle
_LONG_WINDOW_SECONDS = 3600
_MAX_PER_LONG_WINDOW = 120


async def check_public_submit(request: Request) -> None:
    """Raise 429 if this IP has submitted too often. `await` it at the top of a public POST handler."""
    from app.services import cluster
    ip = get_client_ip(request) or "unknown"
    ok = await cluster.rate_hits([
        (f"public_submit_m:{ip}", _MAX_PER_WINDOW, _WINDOW_SECONDS),
        (f"public_submit_h:{ip}", _MAX_PER_LONG_WINDOW, _LONG_WINDOW_SECONDS),
    ])
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many submissions from your network. Please wait a minute and try again.",
        )
