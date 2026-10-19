from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from typing import List, Dict, Any

from app.database import AsyncSessionLocal, get_db
from app.models.database import MarketArea, User as UserModel, CmaReport
from app.schemas.areas import MarketAreaResponse
from app.schemas.user import User as UserSchema
from app.schemas.cma import CmaReportResponse
from app.services.areas import area_enrichment_service
from app.utils.auth import require_admin_user
from app.config.logging import get_logger

router = APIRouter(prefix="/admin", tags=["admin"])
logger = get_logger(__name__)

@router.get("/domain-issues")
async def list_domain_issues(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    """Agents whose own domain isn't working right now - so the admin can reach out."""
    from app.services.website.domain_health_service import all_domain_issues
    return {"issues": await all_domain_issues(db)}


@router.get("/users", response_model=List[UserSchema])
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user)
):
    """
    List all registered users (Admin only).
    """
    try:
        stmt = select(UserModel).order_by(UserModel.created_at.desc())
        result = await db.execute(stmt)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users list"
        )

@router.patch("/users/{user_id}/authorize")
async def toggle_broker_authorization(
    user_id: str,
    authorized: bool,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user)
):
    """
    Grant or revoke broker authorization for a specific user (Admin only).
    """
    try:
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
            
        newly_approved = authorized and not user.broker_authorized
        user.broker_authorized = authorized
        await db.commit()
        await db.refresh(user)

        # Tell them they can finish signing up (only on the not-approved -> approved switch, and
        # only if they still need to pick a plan).
        if newly_approved and not user.subscription_id:
            from app.services.agent_email_service import absolute, email_agent
            email_agent(user, None, "account_approved", "You're approved - pick your OpenHousePal plan",
                        {"link": absolute("/checkout")}, always=True)
        
        return {
            "success": True,
            "user_id": user_id,
            "broker_authorized": user.broker_authorized
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to authorize user {user_id}: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update authorization status"
        )

@router.patch("/users/{user_id}/plan")
async def update_user_plan(
    user_id: str,
    plan_tier: str,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user)
):
    """
    Update a user's plan tier and status (Admin only).
    Setting a plan manually clears the subscription_id, making it a "lifetime" 
    or "admin-injected" plan that doesn't expire via PayPal sync.
    """
    try:
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
            
        # reset the user to nothing
        if plan_tier == "FREE":
            user.plan_tier = None
            user.subscription_status = "PENDING_PAYMENT"
            user.subscription_id = None
            user.plan_id = None
            user.trial_ends_at = None
            user.next_billing_date = None
        else:
            # For BASIC or PREMIUM, set to ACTIVE and clear subscription_id 
            # and plan_id to make it "injected" (it won't be synced from PayPal)
            user.plan_tier = plan_tier
            if plan_tier == "IDX" and not user.idx_setup_settled_at:
                from datetime import datetime, timezone
                user.idx_setup_settled_at = datetime.now(timezone.utc)  # admin-set plan: no fee owed
            user.subscription_status = "ACTIVE"
            user.subscription_id = None
            user.plan_id = None
            user.trial_ends_at = None
            user.next_billing_date = None
            
        await db.commit()
        await db.refresh(user)
        # Same as a real upgrade: any showcases missed on Basic get built + sent.
        from app.services.open_house_service import schedule_showcase_backfill
        schedule_showcase_backfill(user.id)

        return {
            "success": True,
            "user_id": user_id,
            "plan_tier": user.plan_tier,
            "subscription_status": user.subscription_status
        }
    except Exception as e:
        logger.error(f"Failed to update plan for user {user_id}: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user plan"
        )


async def _re_enrich_area_background(area_id: str, admin_user_id: str):
    """Same standalone-session background pattern as site_routes._enrich_area_background - runs
    after the response is already sent."""
    async with AsyncSessionLocal() as db:
        area = (await db.execute(select(MarketArea).where(MarketArea.id == area_id))).scalar_one_or_none()
        admin = (await db.execute(select(UserModel).where(UserModel.id == admin_user_id))).scalar_one_or_none()
        if not area or not admin:
            return
        try:
            await area_enrichment_service.enrich_area(db, area, admin)
        except Exception:
            logger.exception("Admin-triggered area re-enrichment failed", extra={"area_id": area_id})


@router.post("/areas/{area_id}/re-enrich", response_model=MarketAreaResponse)
async def re_enrich_area(
    area_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    """Admin-only forced refresh of a shared MarketArea - bypasses the PENDING-only gate
    add_my_served_area applies (site_routes.py), since an admin explicitly requesting a refresh
    means running it regardless of current status (READY, FAILED, whatever). Still goes through
    the same atomic claim as the agent-facing add flow, so hitting this twice in a row (or an
    agent adding a new town to the same area at the same moment) can't double-enqueue enrichment.
    Cost is attributed to the admin who triggered it, same accounting as any other enrichment run.
    """
    area = (await db.execute(select(MarketArea).where(MarketArea.id == area_id))).scalar_one_or_none()
    if not area:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MarketArea not found.")

    claim = await db.execute(
        update(MarketArea)
        .where(MarketArea.id == area.id, MarketArea.enrichment_status != "RESEARCHING")
        .values(enrichment_status="RESEARCHING")
    )
    await db.commit()
    if not claim.rowcount:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This area is already being researched.")

    background_tasks.add_task(_re_enrich_area_background, area.id, admin.id)
    await db.refresh(area)
    return area


# ---------------------------------------------------------------------------------------------
# CMA support-code lookup. Every CMA report shows the agent a short code (the first 12 hex
# characters of its own id, dashes stripped - see cmaSupportCode() in frontend/lib/cma.ts) at the
# bottom of the Summary step, next to a "Submit a Ticket" link, specifically so an agent hitting a
# real problem (bad valuation, garbled prose, wrong comp data) can hand support something exact to
# pull up - rather than support having to guess which of an agent's reports they mean. This is the
# ONLY way to reach a report's QA review + "how this value was calculated" trace once those are
# hidden from the agent's own view (see studio/cma/[id]/page.tsx) - no separate short-code column,
# no migration, since the report's own id already provides enough entropy to look up by prefix.
# ---------------------------------------------------------------------------------------------

def _normalize_cma_code(code: str) -> str:
    return "".join(ch for ch in code.strip().lower() if ch in "0123456789abcdef")


@router.get("/cma-reports/lookup/{code}", response_model=Dict[str, Any])
async def admin_lookup_cma_report(
    code: str,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    """Look up a CMA report by the short support code an agent read off their report and pasted
    into a support ticket. Returns the full report (including pricing.ai_estimate.qa_result and
    .valuation_trace - the two sections intentionally hidden from the agent's own Studio view) plus
    the owning agent's name/email for context."""
    normalized = _normalize_cma_code(code)
    if len(normalized) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code looks too short - paste the full code from the report.")
    stripped_id = func.replace(CmaReport.id, "-", "")
    result = await db.execute(select(CmaReport).where(stripped_id.ilike(f"{normalized}%")).limit(2))
    matches = result.scalars().all()
    if not matches:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No CMA report matches that code.")
    if len(matches) > 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "That code matches more than one report - double-check it was copied in full.")
    report = matches[0]
    owner = await db.get(UserModel, report.owner_id)
    payload = CmaReportResponse.model_validate(report).model_dump(mode="json")
    payload["owner_name"] = f"{owner.first_name} {owner.last_name}".strip() if owner else None
    payload["owner_email"] = owner.email if owner else None
    return payload


@router.get("/data-pulls")
async def get_data_pulls_status(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    """Where each production Bright data pull is (see bright_data_pulls_service)."""
    from app.services import bright_data_pulls_service
    return await bright_data_pulls_service.status(db)
