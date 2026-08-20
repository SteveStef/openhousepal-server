from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.schemas.cma import (
    CmaReportResponse, CmaReportListItem, CmaReportCreate, CmaReportUpdate,
    CmaCompSearchRequest, CmaCompCandidate, CmaShareResponse, CmaPublicResponse,
    CmaGenerateAiAnalysisRequest,
)
from app.services import cma_ai_service, cma_service as service
from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded
from app.utils.auth import require_basic_plan, require_broker_authorization

# No router-level auth dependency (unlike open-house-manager) because this router also serves
# the unauthenticated public share-link route below - each protected route declares its own
# require_basic_plan + require_broker_authorization instead.
router = APIRouter(prefix="/api/cma", tags=["cma"])


@router.get("", response_model=List[CmaReportListItem])
async def list_cma_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.list_reports(db, current_user.id)


@router.post("", response_model=CmaReportResponse, status_code=201)
async def create_cma_report(
    payload: CmaReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.create_report(db, current_user.id, payload)


@router.post("/comparables/search", response_model=List[CmaCompCandidate])
async def search_cma_comparables(
    payload: CmaCompSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    """Suggests candidate comparables from the local MLS mirror around a location - the agent
    decides what actually gets added to the CMA, this just narrows the search."""
    return await service.search_comparables(db, payload)


@router.get("/public/{slug}", response_model=CmaPublicResponse)
async def get_public_cma_report(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - this is the client-facing share link, no login required."""
    return await service.get_public_report(db, slug)


@router.get("/{report_id}", response_model=CmaReportResponse)
async def get_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.get_report(db, current_user.id, report_id)


@router.patch("/{report_id}", response_model=CmaReportResponse)
async def update_cma_report(
    report_id: str,
    payload: CmaReportUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.update_report(db, current_user.id, report_id, payload)


@router.delete("/{report_id}", status_code=204)
async def delete_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    await service.delete_report(db, current_user.id, report_id)


@router.post("/{report_id}/generate-ai-analysis", response_model=CmaReportResponse)
async def generate_cma_ai_analysis(
    report_id: str,
    payload: CmaGenerateAiAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    """Writes a short AI-drafted note per comp (ai_commentary) and a proposed value estimate
    (pricing.ai_estimate) - never overwrites the agent's own commentary/pricing, see
    cma_ai_service.py."""
    try:
        return await cma_ai_service.generate_ai_analysis(
            db, current_user.id, report_id,
            max_photos_per_property=payload.max_photos_per_property,
            include_map_images=payload.include_map_images,
            force_regenerate=payload.force_regenerate,
        )
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)


@router.post("/{report_id}/share", response_model=CmaShareResponse)
async def share_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    """Generates (or reuses) a public link. The agent keeps editing the same report afterward -
    no separate 'republish' step, the link always reflects the latest saved state."""
    return await service.share_report(db, current_user.id, report_id)
