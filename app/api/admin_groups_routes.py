"""Admin-only brokerage groups - agents whose brokerage pays for them on a custom deal (a PayPal
invoice, often recurring). A group keeps the deal's details in one place and shows exactly which
agents it covers. Putting an agent in a group can also give them the group's plan as a managed
(brokerage-paid) plan - the same thing the admin plan switch does: ACTIVE, no PayPal subscription
on the agent. It never touches PayPal itself."""
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import BillingGroup, User
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/groups", tags=["admin"])

PLAN_TIERS = {"NO_MLS", "PRO_NO_MLS", "BASIC", "PREMIUM", "IDX"}


class GroupIn(BaseModel):
    name: Optional[str] = None
    brokerage: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    paypal_reference: Optional[str] = None
    amount: Optional[float] = None
    billing_cycle: Optional[str] = None
    seats: Optional[int] = None
    plan_tier: Optional[str] = None
    status: Optional[str] = None
    started_on: Optional[date] = None
    next_invoice_on: Optional[date] = None
    notes: Optional[str] = None


class MembersIn(BaseModel):
    user_ids: List[str]
    # Also give them the group's plan as a brokerage-paid (managed) plan.
    apply_plan: bool = True


def _member(u: User) -> dict:
    return {
        "id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email, "email": u.email,
        "brokerage": u.brokerage, "plan_tier": u.plan_tier, "subscription_status": u.subscription_status,
        # Still has their own PayPal subscription -> they're probably being charged twice.
        "has_paypal_subscription": bool(u.subscription_id), "last_seen_at": u.last_seen_at,
        "joined_at": u.created_at, "broker_authorized": bool(u.broker_authorized),
    }


def _group(g: BillingGroup, members: list) -> dict:
    per = None
    if g.amount is not None and members:
        per = round(g.amount / len(members), 2)
    return {
        "id": g.id, "name": g.name, "brokerage": g.brokerage, "contact_name": g.contact_name, "contact_email": g.contact_email,
        "contact_phone": g.contact_phone, "paypal_reference": g.paypal_reference, "amount": g.amount, "billing_cycle": g.billing_cycle,
        "seats": g.seats, "plan_tier": g.plan_tier, "status": g.status, "started_on": g.started_on, "next_invoice_on": g.next_invoice_on,
        "notes": g.notes, "created_at": g.created_at, "per_agent": per,
        "members": [_member(u) for u in sorted(members, key=lambda u: (u.first_name or u.email or '').lower())],
    }


def _apply(g: BillingGroup, data: GroupIn) -> None:
    for k, v in data.model_dump(exclude_unset=True).items():
        if k == "name":
            v = (v or "").strip()
            if not v:
                raise HTTPException(status_code=400, detail="Give the group a name.")
        if k == "plan_tier" and v not in PLAN_TIERS and v is not None:
            raise HTTPException(status_code=400, detail="Unknown plan")
        if k == "billing_cycle" and v not in ("monthly", "quarterly", "yearly"):
            raise HTTPException(status_code=400, detail="Billing cycle must be monthly, quarterly or yearly")
        if k == "status" and v not in ("active", "paused", "ended"):
            raise HTTPException(status_code=400, detail="Status must be active, paused or ended")
        if isinstance(v, str):
            v = v.strip() or None if k != "name" else v
        setattr(g, k, v)


def _give_managed_plan(u: User, tier: str) -> None:
    """Same as the admin plan switch: an ACTIVE plan with no PayPal subscription on the agent."""
    u.plan_tier = tier
    if tier == "IDX" and not u.idx_setup_settled_at:
        u.idx_setup_settled_at = datetime.now(timezone.utc)  # brokerage-paid: no fee owed
    u.subscription_status = "ACTIVE"
    u.subscription_id = None
    u.plan_id = None
    u.trial_ends_at = None
    u.next_billing_date = None


async def _load(db: AsyncSession, group_id: str) -> BillingGroup:
    g = await db.get(BillingGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return g


async def _out(db: AsyncSession, g: BillingGroup) -> dict:
    members = (await db.execute(select(User).where(User.billing_group_id == g.id))).scalars().all()
    return _group(g, list(members))


@router.get("")
async def list_groups(db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    groups = (await db.execute(select(BillingGroup).order_by(BillingGroup.name))).scalars().all()
    members = (await db.execute(select(User).where(User.billing_group_id.isnot(None)))).scalars().all()
    by_group: dict = {}
    for u in members:
        by_group.setdefault(u.billing_group_id, []).append(u)
    return [_group(g, by_group.get(g.id, [])) for g in groups]


@router.post("")
async def create_group(body: GroupIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    g = BillingGroup(name="", billing_cycle="monthly", status="active")
    _apply(g, body)
    if not g.name:
        raise HTTPException(status_code=400, detail="Give the group a name.")
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return await _out(db, g)


@router.patch("/{group_id}")
async def update_group(group_id: str, body: GroupIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    g = await _load(db, group_id)
    _apply(g, body)
    g.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await _out(db, g)


@router.delete("/{group_id}")
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Removes the group only - its agents keep their accounts and plans (change those by hand)."""
    g = await _load(db, group_id)
    for u in (await db.execute(select(User).where(User.billing_group_id == g.id))).scalars().all():
        u.billing_group_id = None
    await db.delete(g)
    await db.commit()
    return {"success": True}


@router.post("/{group_id}/members")
async def add_members(group_id: str, body: MembersIn, db: AsyncSession = Depends(get_db), admin=Depends(require_admin_user)):
    g = await _load(db, group_id)
    users = (await db.execute(select(User).where(User.id.in_(body.user_ids)))).scalars().all()
    from app.services.open_house_service import schedule_showcase_backfill
    planned, had_paypal = [], []
    for u in users:
        if u.email == admin.email:
            continue
        u.billing_group_id = g.id
        if body.apply_plan and g.plan_tier:
            if u.subscription_id:
                # Their own PayPal subscription keeps charging them until it's cancelled in PayPal -
                # say so now, since giving them the managed plan takes it off their account here.
                had_paypal.append({"id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email,
                                   "subscription_id": u.subscription_id})
            _give_managed_plan(u, g.plan_tier)
            planned.append(u.id)
    await db.commit()
    for uid in planned:
        schedule_showcase_backfill(uid)  # same as any upgrade: Showcases missed on a lower plan get built
    out = await _out(db, g)
    out["had_paypal"] = had_paypal
    return out


@router.delete("/{group_id}/members/{user_id}")
async def remove_member(group_id: str, user_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Out of the group only - their plan stays as it is until changed on the Agents tab."""
    g = await _load(db, group_id)
    u = await db.get(User, user_id)
    if u is not None and u.billing_group_id == g.id:
        u.billing_group_id = None
        await db.commit()
    return await _out(db, g)


@router.post("/{group_id}/apply-plan")
async def apply_plan_to_all(group_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Give every agent in the group the group's plan (brokerage-paid)."""
    g = await _load(db, group_id)
    if not g.plan_tier:
        raise HTTPException(status_code=400, detail="Pick the group's plan first.")
    from app.services.open_house_service import schedule_showcase_backfill
    members = (await db.execute(select(User).where(User.billing_group_id == g.id))).scalars().all()
    for u in members:
        _give_managed_plan(u, g.plan_tier)
    await db.commit()
    for u in members:
        schedule_showcase_backfill(u.id)
    return await _out(db, g)
