"""APScheduler entry point for real domain verification - replaces Phase 1's manual
status-PATCH stand-in with an actual recurring DNS/HTTPS check. Same shape as
property_sync_scheduler.py: a thin async entry function, its own DB session (background jobs
have no request-scoped session), registered in app/main.py's lifespan()."""
from sqlalchemy import select

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.database import SiteDomain
from app.services.website import domain_verification_service

logger = get_logger(__name__)


async def scheduled_domain_verification():
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SiteDomain).where(SiteDomain.status.in_(["PENDING", "VERIFIED", "FAILED"]))
            )
            domains = result.scalars().all()

            checked = 0
            became_active = 0
            for domain in domains:
                try:
                    updated = await domain_verification_service.verify_domain(db, domain)
                    checked += 1
                    if updated.status == "ACTIVE" and domain.status != "ACTIVE":
                        became_active += 1
                except Exception as e:
                    logger.error(
                        "Domain verification check failed", exc_info=True,
                        extra={"domain_id": domain.id, "hostname": domain.hostname, "error": str(e)},
                    )

            if checked:
                logger.info(
                    "Domain verification poll completed",
                    extra={"event": "domain_verification_poll", "checked": checked, "became_active": became_active},
                )
    except Exception as e:
        logger.error("Scheduled domain verification failed", exc_info=True, extra={"error": str(e)})
