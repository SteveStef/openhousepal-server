"""Admin "Codes" - sign-up codes an admin makes for a team, a brokerage or a one-off deal.
A code is reusable until it's switched off, used up (optional limit) or past its end date, and does
one of two things at checkout:
  paypal_plan  - the agent subscribes on a special PayPal plan made for the deal (e.g. IDX with
                 no setup fee). We read that plan's real terms from PayPal when the code is saved.
  billed_by_us - the account switches on with no PayPal; we bill the agent ourselves at the custom
                 price kept here (Discord gets a "send them an invoice" ping on each sign-up)."""
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import BillingGroup, PromoCode, User
from app.services.promo_code_service import clean_code
from app.utils.auth import require_admin_user
from app.utils.plan_ids import display_name_for_tier

router = APIRouter(prefix="/admin/promo-codes", tags=["admin"])

PLAN_TIERS = {"NO_MLS", "PRO_NO_MLS", "BASIC", "PREMIUM", "IDX"}
KINDS = {"paypal_plan", "billed_by_us"}
CYCLES = {"monthly", "quarterly", "yearly"}

# The plans already set up on this server, offered as quick picks for a "special PayPal plan" code.
_KNOWN_PLANS = [
    ("IDX", "PAYPAL_IDX_NO_TRIAL_PLAN_ID", "IDX - no setup fee"),
    ("IDX", "PAYPAL_IDX_PLAN_ID", "IDX - with setup fee"),
    ("PREMIUM", "PAYPAL_PREMIUM_V2_NO_TRIAL_PLAN_ID", "Premium - no trial"),
    ("PREMIUM", "PAYPAL_PREMIUM_V2_PLAN_ID", "Premium - 14-day trial"),
    ("BASIC", "PAYPAL_BASIC_V2_NO_TRIAL_PLAN_ID", "Basic - no trial"),
    ("BASIC", "PAYPAL_BASIC_V2_PLAN_ID", "Basic - 14-day trial"),
    ("PRO_NO_MLS", "PAYPAL_PRO_NO_MLS_NO_TRIAL_PLAN_ID", "No-MLS Premium - no trial"),
    ("PRO_NO_MLS", "PAYPAL_PRO_NO_MLS_PLAN_ID", "No-MLS Premium - 14-day trial"),
    ("NO_MLS", "PAYPAL_NO_MLS_NO_TRIAL_PLAN_ID", "No-MLS Basic - no trial"),
    ("NO_MLS", "PAYPAL_NO_MLS_PLAN_ID", "No-MLS Basic - 14-day trial"),
]


class CodeIn(BaseModel):
    code: Optional[str] = None
    label: Optional[str] = None
    kind: Optional[str] = None
    plan_tier: Optional[str] = None
    paypal_plan_id: Optional[str] = None
    custom_price: Optional[float] = None
    billing_cycle: Optional[str] = None
    billing_group_id: Optional[str] = None
    max_uses: Optional[int] = None
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


def _out(c: PromoCode, agents: list, groups: dict) -> dict:
    now = datetime.now(timezone.utc)
    state = "active"
    if not c.is_active:
        state = "off"
    elif c.expires_at and c.expires_at <= now:
        state = "expired"
    elif c.max_uses is not None and c.uses >= c.max_uses:
        state = "used_up"
    return {
        "id": c.id, "code": c.code, "label": c.label, "kind": c.kind, "plan_tier": c.plan_tier,
        "plan_name": display_name_for_tier(c.plan_tier), "paypal_plan_id": c.paypal_plan_id, "trial_days": c.trial_days,
        "custom_price": c.custom_price, "billing_cycle": c.billing_cycle, "billing_group_id": c.billing_group_id,
        "billing_group_name": groups.get(c.billing_group_id), "max_uses": c.max_uses, "uses": c.uses,
        "expires_at": c.expires_at, "is_active": c.is_active, "state": state, "notes": c.notes, "created_at": c.created_at,
        "agents": [{
            "id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email, "email": u.email,
            "plan_tier": u.plan_tier, "subscription_status": u.subscription_status, "joined_at": u.subscription_started_at or u.created_at,
            "has_paypal_subscription": bool(u.subscription_id),
        } for u in sorted(agents, key=lambda u: u.subscription_started_at or u.created_at, reverse=True)],
    }


async def _lookup_terms(plan_id: str) -> dict:
    from app.utils.paypal_plan_terms import get_plan_terms
    try:
        return await get_plan_terms(plan_id, fresh=True)
    except Exception:
        raise HTTPException(status_code=400, detail="PayPal doesn't know that plan ID. Copy it again from PayPal (it starts with P-).")


async def _apply(db: AsyncSession, c: PromoCode, body: CodeIn) -> None:
    data = body.model_dump(exclude_unset=True)
    if "code" in data:
        code = clean_code(data["code"])
        if not (3 <= len(code) <= 32) or not code.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="Codes are 3-32 letters or numbers (dashes are fine).")
        taken = (await db.execute(select(PromoCode.id).where(PromoCode.code == code, PromoCode.id != (c.id or "")))).first()
        if taken:
            raise HTTPException(status_code=400, detail=f"{code} is already a code. Pick another.")
        c.code = code
    if "kind" in data:
        if data["kind"] not in KINDS:
            raise HTTPException(status_code=400, detail="Pick how they pay.")
        c.kind = data["kind"]
    if "plan_tier" in data:
        if data["plan_tier"] not in PLAN_TIERS:
            raise HTTPException(status_code=400, detail="Pick a plan.")
        c.plan_tier = data["plan_tier"]
    if "billing_cycle" in data:
        if data["billing_cycle"] is not None and data["billing_cycle"] not in CYCLES:
            raise HTTPException(status_code=400, detail="Billing cycle must be monthly, quarterly or yearly.")
        c.billing_cycle = data["billing_cycle"]
    if "custom_price" in data:
        if data["custom_price"] is not None and data["custom_price"] < 0:
            raise HTTPException(status_code=400, detail="The price can't be negative.")
        c.custom_price = data["custom_price"]
    if "max_uses" in data:
        if data["max_uses"] is not None and data["max_uses"] < 1:
            raise HTTPException(status_code=400, detail="Leave the limit empty for unlimited, or use 1 or more.")
        c.max_uses = data["max_uses"]
    if "billing_group_id" in data:
        gid = data["billing_group_id"] or None
        if gid and not await db.get(BillingGroup, gid):
            raise HTTPException(status_code=400, detail="That brokerage group no longer exists.")
        c.billing_group_id = gid
    for k in ("label", "notes"):
        if k in data:
            setattr(c, k, (data[k] or "").strip() or None)
    if "expires_at" in data:
        c.expires_at = data["expires_at"]
    if "is_active" in data and data["is_active"] is not None:
        c.is_active = data["is_active"]
    if "paypal_plan_id" in data:
        c.paypal_plan_id = (data["paypal_plan_id"] or "").strip() or None

    if not c.code:
        raise HTTPException(status_code=400, detail="Give the code a name, like SMITHTEAM.")
    if c.kind == "paypal_plan":
        if not c.paypal_plan_id:
            raise HTTPException(status_code=400, detail="Pick or paste the PayPal plan this code uses.")
        if "paypal_plan_id" in data or c.id is None:
            terms = await _lookup_terms(c.paypal_plan_id)
            if terms.get("status") and terms["status"] != "ACTIVE":
                raise HTTPException(status_code=400, detail="That PayPal plan is switched off in PayPal. Turn it on there first.")
            c.trial_days = int(terms.get("trial_days") or 0)
        c.custom_price = None
        c.billing_cycle = None
    else:
        c.paypal_plan_id = None
        c.trial_days = 0
        c.billing_cycle = c.billing_cycle or "monthly"


async def _render(db: AsyncSession, codes: list) -> list:
    ids = [c.id for c in codes]
    agents = (await db.execute(select(User).where(User.promo_code_id.in_(ids)))).scalars().all() if ids else []
    by_code: dict = {}
    for u in agents:
        by_code.setdefault(u.promo_code_id, []).append(u)
    groups = {g.id: g.name for g in (await db.execute(select(BillingGroup))).scalars().all()}
    return [_out(c, by_code.get(c.id, []), groups) for c in codes]


@router.get("")
async def list_codes(db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    codes = (await db.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))).scalars().all()
    return await _render(db, list(codes))


@router.get("/plans")
async def known_plans(_admin=Depends(require_admin_user)):
    """The PayPal plans set up on this server, as quick picks."""
    out = []
    for tier, env, label in _KNOWN_PLANS:
        plan_id = os.getenv(env)
        if plan_id:
            out.append({"plan_tier": tier, "plan_id": plan_id, "label": label})
    return out


@router.get("/plan-check")
async def plan_check(plan_id: str, _admin=Depends(require_admin_user)):
    """What PayPal will actually charge on a plan, so the admin sees it before saving the code."""
    return await _lookup_terms(plan_id.strip())


@router.post("")
async def create_code(body: CodeIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    c = PromoCode(code="", kind=body.kind or "", plan_tier=body.plan_tier or "", uses=0, is_active=True, trial_days=0)
    if body.kind is None or body.plan_tier is None:
        raise HTTPException(status_code=400, detail="Pick a plan and how they pay.")
    await _apply(db, c, body)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return (await _render(db, [c]))[0]


@router.patch("/{code_id}")
async def update_code(code_id: str, body: CodeIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    c = await db.get(PromoCode, code_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Code not found")
    await _apply(db, c, body)
    c.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(c)
    return (await _render(db, [c]))[0]


@router.delete("/{code_id}")
async def delete_code(code_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Delete a code. Agents who already used it keep their plan and what they pay - only the code
    (and its list of who used it) goes away; nobody new can use it."""
    c = await db.get(PromoCode, code_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Code not found")
    await db.delete(c)
    await db.commit()
    return {"success": True}
