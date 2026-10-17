"""AI health check (user, 10/17): every Claude feature broke on production after a server rebuild
pulled a new major version of the `anthropic` library (1.x needs a newer anyio than fastapi 0.104
allows - every call failed as "Connection error") and nobody knew until an agent tried a feature.

Auto-updating the library is what CAUSED that, so the fix is the opposite: requirements.txt pins
`anthropic` to an exact version that is known to work, and this check makes any break loud:

- once ~1 minute after the server starts (every deploy), then every 30 minutes,
- one tiny real request through the same AsyncAnthropic client the features use (Haiku, a few
  tokens - fractions of a cent a day; never on any agent's meter),
- if it fails, the admin gets a bell alert + Discord message (admin_alerts, throttled) with the
  library version and the real error, and a recovery note when it works again.

ANTHROPIC_HEALTH_CHECK=false turns it off (e.g. a second local copy)."""
from __future__ import annotations

import os
from typing import Optional

from app.config.logging import get_logger

logger = get_logger(__name__)

_last_ok: Optional[bool] = None
MODEL = os.getenv("ANTHROPIC_HEALTH_CHECK_MODEL", "claude-haiku-4-5")


async def check_once() -> tuple[bool, str]:
    """(ok, detail). Never raises."""
    try:
        import anthropic
        version = getattr(anthropic, "__version__", "?")
    except Exception as e:  # noqa: BLE001
        return False, f"the anthropic library won't import: {e}"
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY is not set on this server"
    try:
        # Same library + transport as every feature (AsyncAnthropic over httpx), its own client.
        client = anthropic.AsyncAnthropic(timeout=30.0, max_retries=1)
        resp = await client.messages.create(model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "Reply with OK"}])
        if not getattr(resp, "content", None):
            return False, f"anthropic {version}: empty reply"
        return True, f"anthropic {version}: ok"
    except Exception as e:  # noqa: BLE001
        return False, f"anthropic {version}: {type(e).__name__}: {str(e)[:400]}"


async def scheduled_ai_health_check() -> None:
    global _last_ok
    if os.getenv("ANTHROPIC_HEALTH_CHECK", "true").lower() == "false" or os.getenv("DEV", "false") == "true":
        return
    ok, detail = await check_once()
    from app.services.admin_alerts import alert_admin
    if not ok:
        logger.error("AI health check failed", extra={"detail": detail})
        await alert_admin("ai", "Claude AI features are DOWN", f"A test request from the server failed - every AI feature "
                          f"(blogs, CMA AI, copywriting, assistant) is likely failing for agents right now. {detail}")
    elif _last_ok is False:
        await alert_admin("ai", "Claude AI features are working again", detail)
    _last_ok = ok
