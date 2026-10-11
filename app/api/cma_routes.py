import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

logger = logging.getLogger(__name__)
from app.models.database import User
from app.schemas.cma import (
    CmaReportResponse, CmaReportListItem, CmaReportCreate, CmaReportUpdate,
    CmaCompSearchRequest, CmaCompCandidate, CmaShareResponse, CmaPublicResponse,
    CmaGenerateAiAnalysisRequest, CmaThemeUpdate,
)
from app.services import cma_ai_service, cma_service as service
from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded
from app.utils.auth import require_mls_access, require_broker_authorization

# No router-level auth dependency (unlike open-house-manager) because this router also serves
# the unauthenticated public share-link route below - each protected route declares its own
# require_basic_plan + require_broker_authorization instead.
router = APIRouter(prefix="/api/cma", tags=["cma"])


@router.get("", response_model=List[CmaReportListItem])
async def list_cma_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.list_reports(db, current_user.id)


@router.post("", response_model=CmaReportResponse, status_code=201)
async def create_cma_report(
    payload: CmaReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.create_report(db, current_user.id, payload)


@router.post("/comparables/search", response_model=List[CmaCompCandidate])
async def search_cma_comparables(
    payload: CmaCompSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Suggests candidate comparables from the local MLS mirror around a location - the agent
    decides what actually gets added to the CMA, this just narrows the search."""
    return await service.search_comparables(db, payload)


@router.get("/public/{slug}", response_model=CmaPublicResponse)
async def get_public_cma_report(
    slug: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - this is the client-facing share link, no login required."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get_public_report(db, slug)


@router.get("/{report_id}", response_model=CmaReportResponse)
async def get_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.get_report(db, current_user.id, report_id)


@router.patch("/{report_id}", response_model=CmaReportResponse)
async def update_cma_report(
    report_id: str,
    payload: CmaReportUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.update_report(db, current_user.id, report_id, payload)


@router.patch("/{report_id}/theme", status_code=204)
async def update_cma_default_theme(
    report_id: str,
    payload: CmaThemeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    await service.update_default_theme(db, current_user.id, report_id, payload.default_theme)


@router.delete("/{report_id}", status_code=204)
async def delete_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    await service.delete_report(db, current_user.id, report_id)


@router.post("/{report_id}/generate-ai-analysis", response_model=CmaReportResponse)
async def generate_cma_ai_analysis(
    report_id: str,
    payload: CmaGenerateAiAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Writes a short AI-drafted note per comp (ai_commentary) and a proposed value estimate
    (pricing.ai_estimate) - never overwrites the agent's own commentary/pricing, see
    cma_ai_service.py."""
    success = False
    try:
        await service.drop_subject_from_comps(db, current_user.id, report_id)
        result = await cma_ai_service.generate_ai_analysis(
            db, current_user.id, report_id,
            plan_tier=current_user.plan_tier,
            max_photos_per_property=payload.max_photos_per_property,
            include_map_images=payload.include_map_images,
            force_regenerate=payload.force_regenerate,
        )
        success = True
        return result
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    except HTTPException:
        raise
    except Exception:
        # A bug anywhere in the (long, paid) generation pipeline must come back as a readable
        # JSON error - otherwise it propagates through the logging middleware as a torn stream
        # and the browser only sees "Failed to fetch" with no way to tell what went wrong.
        logger.exception("CMA AI analysis failed unexpectedly", extra={"report_id": report_id})
        raise HTTPException(
            status_code=502,
            detail="The AI analysis hit an unexpected error partway through. Your usage for this run was still counted - try again, and if it keeps failing, tell support which CMA.",
        )
    finally:
        # Clears the in-flight marker regardless of how this exited (success, a rate limit, a
        # validation HTTPException, or an unexpected error) - a no-op if generation never actually
        # started (e.g. failed validation before mark_generation_started ran).
        await service.mark_generation_finished(current_user.id, report_id, "complete" if success else "failed")


@router.post("/{report_id}/recompute-valuation", response_model=CmaReportResponse)
async def recompute_cma_valuation(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Re-runs the deterministic evidence/confidence/value-drivers pipeline plus one lightweight
    (text-only, no photos) AI call to keep the value/methodology text consistent with whatever
    comp overrides the agent has made since the initial generation (strength reclassifications,
    Mark Irrelevant) - see cma_ai_service.recompute_valuation. Repeatable, unlike the one-time
    initial generation; gated by the same shared monthly AI budget as every other metered tool."""
    success = False
    try:
        result = await cma_ai_service.recompute_valuation(db, current_user.id, report_id, plan_tier=current_user.plan_tier)
        success = True
        return result
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    finally:
        await service.mark_generation_finished(current_user.id, report_id, "complete" if success else "failed")


@router.post("/{report_id}/resync-qa", response_model=CmaReportResponse)
async def resync_cma_qa(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Free re-stamp for a report whose only problem is a stale artifact hash (a save
    round-tripped after final QA with nothing about the content actually changing) - no AI call,
    no cost. Returns 409 if a real content finding is also present; that needs
    recompute-valuation or a full regenerate instead. See cma_service.resync_qa."""
    return await service.resync_qa(db, current_user.id, report_id)


@router.post("/{report_id}/share", response_model=CmaShareResponse)
async def share_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Generates (or reuses) a public link. The agent keeps editing the same report afterward -
    no separate 'republish' step, the link always reflects the latest saved state."""
    return await service.share_report(db, current_user.id, report_id)
