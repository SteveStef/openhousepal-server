"""Who pays for the paid lookups made during this piece of work (user, 10/25: "all things that use
AI usage should charge the agent's usage bar").

Signing in (app.utils.auth.get_current_user) marks the request with the agent's id, and so does any
agent-started job that runs outside a request (bill_to). Deep helpers that call a paid Google API -
geocoding, Street View, the town-research place checks and commute times - then call
charge_google(), and the cost lands on that agent's Monthly AI Usage bar as "Maps & place lookups".
Work nobody signed in started (the MLS sync, the nightly town upkeep, a visitor's page view) has no
owner and stays on the platform budget only.

Features that already record their own cost with an explicit owner (cheat sheet Google lookups,
Agent Hub reviews, every AI writer) don't call charge_google, so nothing is charged twice.
"""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_owner: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("usage_owner", default=None)


def set_usage_owner(owner_id: Optional[str]) -> None:
    """Mark the rest of this request (and the tasks it starts) as this agent's."""
    if owner_id:
        _owner.set(str(owner_id))


def current_usage_owner() -> Optional[str]:
    return _owner.get()


@contextmanager
def bill_to(owner_id: Optional[str]) -> Iterator[None]:
    """Charge paid lookups inside the block to `owner_id` (None: nobody - the platform pays)."""
    token = _owner.set(str(owner_id) if owner_id else None)
    try:
        yield
    finally:
        _owner.reset(token)


async def charge_google(kind: str, calls: int = 1) -> None:
    """Record `calls` paid Google requests of `kind` (a key of external_api_prices_usd) on the
    current agent's usage. No owner: nothing recorded. Writes on a session of its own - callers
    are often deep helpers with no session, and a failed write must never touch the caller's.
    Never raises."""
    owner_id = _owner.get()
    if not owner_id or calls <= 0:
        return
    try:
        from app.database import AsyncSessionLocal
        from app.services.ai.ai_config_service import ai_config_service
        from app.services.ai.ai_rate_limit_service import record_external_api_cost
        async with AsyncSessionLocal() as db:
            config = await ai_config_service.get_config(db)
            await record_external_api_cost(db, owner_id, kind=kind, calls=calls, config=config)
    except Exception as exc:  # noqa: BLE001 - never fail the feature over cost logging
        logger.warning(f"charge_google({kind}) failed: {exc}")
