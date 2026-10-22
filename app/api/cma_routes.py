import asyncio
import logging
from typing import List

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

logger = logging.getLogger(__name__)
from app.models.database import User
from app.schemas.cma import (
    CmaReportResponse, CmaReportListItem, CmaReportCreate, CmaReportUpdate,
    CmaCompSearchRequest, CmaCompCandidate, CmaShareResponse, CmaPublicResponse,
    CmaGenerateAiAnalysisRequest, CmaThemeUpdate,
)
from app.services import cma_ai_service, cma_service as service, cma_time_machine
from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded
from app.utils.auth import require_mls_access, require_broker_authorization

# No router-level auth dependency (unlike open-house-manager) because this router also serves
# the unauthenticated public share-link route below - each protected route declares its own
# require_basic_plan + require_broker_authorization instead.
router = APIRouter(prefix="/api/cma", tags=["cma"])

# Background AI runs in this process (kept referenced so they're never garbage-collected mid-run).
_BACKGROUND: set = set()


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
    from app.services import cma_backtest_arms as _arms
    arm = await _arms.find_arm(db, current_user.id, listing_key=payload.listing_key)
    if arm:   # CMA accuracy test only: the search window ends on the as-of date
        payload = _arms.shift_search_window(payload, arm)
    with _arms.activated(arm):
        return await service.search_comparables(db, payload)


@router.post("/comparables/auto-search")
async def auto_search_cma_comparables(
    payload: CmaCompSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """The comp step's first search: picks its own radius and sold window (time first, then
    distance, never past this product's limit) and says when comps are limited - see
    cma_service.auto_search_comparables. Manual changes after that use /comparables/search."""
    from app.services import cma_backtest_arms as _arms
    arm = await _arms.find_arm(db, current_user.id, listing_key=payload.listing_key)
    if arm:
        payload = _arms.shift_search_window(payload, arm)
    with _arms.activated(arm):
        out = await service.auto_search_comparables(db, payload)
    return {**out, "results": [c.model_dump() for c in out["results"]]}


@router.get("/public/{slug}", response_model=CmaPublicResponse)
async def get_public_cma_report(
    slug: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - this is the client-facing share link, no login required."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get_public_report(db, slug)


@router.get("/ai-estimate")
async def cma_ai_estimate(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    """What one "Create AI Analysis" usually costs, as a share of this agent's monthly AI allowance -
    shown by the button before they spend it (user, 10/17). Typical = the average real cost of recent
    runs across the platform (photo review + reasoning); "up to" = the per-run cap."""
    from sqlalchemy import func, select
    from app.models.database import AIUsageEvent
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    limit = (config.get("agent_monthly_ai_budget_usd_by_tier") or {}).get(current_user.plan_tier)
    cap = float(config.get("cma_ai_max_cost_per_generation_usd") or 0.75)

    async def avg(kind: str) -> float:
        sub = (select(AIUsageEvent.estimated_cost_usd).where(AIUsageEvent.event_type == kind, AIUsageEvent.blocked.is_(False),
                                                             AIUsageEvent.estimated_cost_usd > 0)
               .order_by(AIUsageEvent.created_at.desc()).limit(40)).subquery()
        return float((await db.execute(select(func.avg(sub.c.estimated_cost_usd)))).scalar() or 0)

    typical = (await avg("cma_ai")) + (await avg("cma_reasoning"))
    if typical <= 0:
        typical = cap * 0.6
    typical = min(typical, cap)
    pct = (lambda usd: round(usd / float(limit) * 100, 1) if limit else None)
    return {"usd": round(typical, 3), "percent": pct(typical), "max_percent": pct(cap)}


# ---- Manual subject (a home not on the MLS yet) - see cma_manual_subject_service ----

@router.get("/subject-area")
async def cma_subject_area(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """School district / township / water / sewer most nearby MLS homes share, in the MLS's own
    spelling - pre-fills a manually entered subject so it matches comps the way an MLS one does."""
    from app.services import cma_manual_subject_service
    return await cma_manual_subject_service.area_suggestions(db, lat, lng)


@router.get("/subject-from-listing/{listing_key}")
async def cma_subject_from_listing(
    listing_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """The full CMA subject for an MLS listing, straight from our Bright record - location, school
    district, township, county, lot, basement, garage, water/sewer, taxes, style, levels, the
    listing description and every photo. Used when an agent picks one of their own listings or a
    searched address (10/20: picking "My listing" filled only address, price, beds/baths/sqft and a
    photo, so the home "wasn't on the map" and most facts were blank)."""
    subject = await service.subject_from_listing(db, listing_key)
    if not subject:
        raise HTTPException(status_code=404, detail="That listing isn't in our MLS data yet.")
    return subject


@router.get("/locate")
async def cma_locate_address(
    address: str = Query(..., min_length=5, max_length=300),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Typed address -> location, for when Google's suggestions don't load in the browser."""
    from app.services import cma_manual_subject_service
    found = await cma_manual_subject_service.geocode_address(address)
    if not found:
        raise HTTPException(status_code=404, detail="Couldn't find that address on the map - check the spelling and ZIP.")
    return found


@router.post("/{report_id}/subject-photo")
async def upload_cma_subject_photo(
    report_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """A photo of a manually entered subject (JPG, PNG or WebP - stored as JPG, which the PDF needs)."""
    from app.services import cma_manual_subject_service as manual
    from app.utils.image_validation import photo_to_jpeg
    await service._get_owned(db, current_user.id, report_id)
    contents = await file.read()
    if len(contents) > manual.SUBJECT_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=422, detail="Photo must be 10MB or smaller.")
    try:
        jpeg = photo_to_jpeg(contents)
    except ValueError:
        raise HTTPException(status_code=422, detail="Please upload a JPG, PNG or WebP photo.")
    return {"photo_url": await manual.save_subject_photo(db, current_user, jpeg)}


class CmaGooglePhotoRequest(BaseModel):
    kind: Literal["street_view", "aerial"] = "street_view"
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@router.post("/{report_id}/subject-photo/google")
async def google_cma_subject_photo(
    report_id: str,
    payload: CmaGooglePhotoRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Street View photo (or an aerial view) of the subject's location, saved as its photo."""
    from app.services import cma_manual_subject_service as manual
    report = await service._get_owned(db, current_user.id, report_id)
    subject = report.subject or {}
    lat = payload.latitude if payload.latitude is not None else subject.get("latitude")
    lng = payload.longitude if payload.longitude is not None else subject.get("longitude")
    if lat is None or lng is None:
        raise HTTPException(status_code=422, detail="Pick the address first - the photo is taken at its location.")
    try:
        jpeg = await manual.google_photo(float(lat), float(lng), payload.kind)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"photo_url": await manual.save_subject_photo(db, current_user, jpeg), "kind": payload.kind}


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


async def _generate_maybe_as_of(db, owner_id: str, report_id: str, tier, opts: dict):
    """The normal generation. Only for an accuracy-test report with a live arm (see
    cma_backtest_arms) it runs inside the time machine - in a dry run with zero-cost AI stand-ins."""
    from app.services import cma_backtest_arms as _arms
    arm = await _arms.find_arm(db, owner_id, report_id=report_id)
    if not arm:
        return await cma_ai_service.generate_ai_analysis(db, owner_id, report_id, plan_tier=tier, **opts)
    with _arms.activated(arm) as tm:
        if arm.dry_run:
            from app.services.cma_backtest_stubs import StubAnthropic, stub_reasoning
            rep = await service.get_report(db, owner_id, report_id)
            tm.fake_client = StubAnthropic({"list_price": (arm.facts or {}).get("list_price"), "comps": [
                {"listing_key": c.get("listing_key"), "address": c.get("street_address"), "price": c.get("sale_price") or c.get("list_price")}
                for c in (rep.comparables or []) if c.get("is_included", True)]})
            tm.fake_reasoning = stub_reasoning
            opts = {**opts, "include_map_images": False}   # the one paid Google call - off in a dry run
        try:
            return await cma_ai_service.generate_ai_analysis(db, owner_id, report_id, plan_tier=tier, **opts)
        finally:
            for i, cap in enumerate(tm.captured):
                cma_time_machine.check_payload(cap, f"captured model request {i + 1} ({cap.get('provider')})", raise_now=False)
            arm.facts = {**(arm.facts or {}), "leak_checks": tm.checks, "leak_log": tm.leak_log,
                         "captured_requests": len(tm.captured), "ai_input_excerpt": tm.prompt_excerpt}
            await db.commit()


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
    if payload.background:
        # Runs on its own session, untied to this request: the answer comes back at once
        # ('generating') and the page polls GET /{id} until it's 'complete' or 'failed' (with
        # generation_error saying why). Nothing the network does can break it any more.
        await service.get_report(db, current_user.id, report_id)   # 404 for someone else's report
        await service.mark_generation_started(db, current_user.id, report_id)
        owner_id, tier = current_user.id, current_user.plan_tier
        opts = dict(max_photos_per_property=payload.max_photos_per_property, include_map_images=payload.include_map_images,
                    force_regenerate=payload.force_regenerate)

        async def run():
            ok, err = False, None
            from app.database import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as s:
                    await service.drop_subject_from_comps(s, owner_id, report_id)
                    await _generate_maybe_as_of(s, owner_id, report_id, tier, opts)
                ok = True
            except AIRateLimitExceeded as e:
                err = e.user_message
            except HTTPException as e:
                err = str(e.detail)
            except Exception:
                logger.exception("CMA AI analysis failed unexpectedly (background)", extra={"report_id": report_id})
                err = "The AI analysis hit an unexpected error partway through - please try again."
                try:
                    from app.services.admin_alerts import alert_admin
                    await alert_admin("cma", "CMA AI analysis crashed", f"report {report_id}", owner_id=owner_id)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                await service.mark_generation_finished(owner_id, report_id, "complete" if ok else "failed", error=err)

        task = asyncio.create_task(run())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return await service.get_report(db, current_user.id, report_id)

    success = False
    try:
        await service.drop_subject_from_comps(db, current_user.id, report_id)
        result = await _generate_maybe_as_of(db, current_user.id, report_id, current_user.plan_tier, dict(
            max_photos_per_property=payload.max_photos_per_property,
            include_map_images=payload.include_map_images,
            force_regenerate=payload.force_regenerate,
        ))
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
