"""Lightweight abuse throttle for the unauthenticated visitor-facing POST endpoints
(open-house sign-in, public form submissions).

In-memory sliding window, keyed by client IP. This is intentionally simple and dependency-free:
it caps a bot from hammering these endpoints (each sign-in also spawns an auto-showcase, which
does real work), without needing Redis. The trade-off is that it's per-process — with N backend
workers a caller gets N x the limit — so for a hard guarantee at scale this should move to a
shared store. Still, it takes the endpoint from "unlimited" to "a few dozen per minute per IP",
which is the point.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request, status

from app.utils.request_ip import get_client_ip

# window seconds -> max requests in that window, per IP
_WINDOW_SECONDS = 60
_MAX_PER_WINDOW = 20
# a slower long window so a patient bot can't just trickle
_LONG_WINDOW_SECONDS = 3600
_MAX_PER_LONG_WINDOW = 120

_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _prune(dq: Deque[float], now: float, horizon: float) -> None:
    while dq and now - dq[0] > horizon:
        dq.popleft()


def check_public_submit(request: Request) -> None:
    """Raise 429 if this IP has submitted too often. Call at the top of a public POST handler."""
    ip = get_client_ip(request) or "unknown"
    now = time.time()
    dq = _hits[ip]
    _prune(dq, now, _LONG_WINDOW_SECONDS)

    recent = sum(1 for t in dq if now - t <= _WINDOW_SECONDS)
    if recent >= _MAX_PER_WINDOW or len(dq) >= _MAX_PER_LONG_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many submissions from your network. Please wait a minute and try again.",
        )

    dq.append(now)

    # Keep the dict from growing unbounded on a long-running process.
    if len(_hits) > 50_000:
        for k in [k for k, v in list(_hits.items()) if not v or now - v[-1] > _LONG_WINDOW_SECONDS][:10_000]:
            _hits.pop(k, None)
