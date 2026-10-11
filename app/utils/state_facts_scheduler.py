"""Keeps the shared state-wide facts (shared_knowledge "state:XX" - transfer taxes, buyer
programs, disclosure rules, property tax basics) fresh for every Bright MLS state, so articles
never lean on expired facts and nobody has to remember to re-research them.

Runs monthly - the 1st at 3 AM Eastern (registered in app/main.py's lifespan). Each item has a
topic shelf life (town_knowledge_service._BANK_TTL_DAYS); every state with fewer than MIN_FRESH
usable items is re-researched in that run with ai_blog_pipeline_service.seed_state_facts (about
$0.25 per state, platform-paid - typically each state comes due about once a year). Only one
worker does it (Postgres advisory lock), so a multi-worker deploy never pays twice.
"""
from sqlalchemy import text

from app.config.logging import get_logger
from app.database import AsyncSessionLocal

logger = get_logger(__name__)

BRIGHT_STATES = ("PA", "NJ", "DE", "MD", "VA", "DC", "WV")
MIN_FRESH = 5
_LOCK_KEY = 718_204_001  # arbitrary, unique to this job


async def scheduled_state_facts_refresh():
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        return
    from app.services.ai.ai_blog_pipeline_service import seed_state_facts
    from app.services.areas import town_knowledge_service as tk
    try:
        async with AsyncSessionLocal() as db:
            got = (await db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar()
            if not got:
                return
            try:
                for state in BRIGHT_STATES:
                    fresh = await tk.state_items(db, state)
                    if len(fresh) >= MIN_FRESH:
                        continue
                    result = await seed_state_facts(db, state, searches=12, fetches=5)
                    logger.info("state facts refreshed", extra={"state": state, "fresh_before": len(fresh), "added": result.get("added"), "cost_usd": result.get("cost_usd")})
            finally:
                await db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                await db.commit()
    except Exception:
        logger.warning("state facts refresh failed", exc_info=True)
