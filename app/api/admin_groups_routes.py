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
from app.models.database import BillingGroup, BillingGroupEmail, User
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
    ai_budget_per_agent_usd: Optional[float] = None
    ai_budget_total_usd: Optional[float] = None


class EmailsIn(BaseModel):
    # Pasted list - one per line, or separated by commas / spaces.
    emails: str
    apply_plan: bool = True
    send_invite: bool = True


class MemberAiIn(BaseModel):
    # This person's own monthly AI allowance ($) inside the group; None = the group's / plan's.
    ai_budget_usd: Optional[float] = None


class MembersIn(BaseModel):
    user_ids: List[str]
    # Also give them the group's plan as a brokerage-paid (managed) plan.
    apply_plan: bool = True


def _member(u: User, ai: Optional[dict] = None) -> dict:
    ai = ai or {}
    return {
        "ai_used_usd": round(ai.get("spend", {}).get(u.id, 0.0), 2),
        "ai_budget_override_usd": u.ai_budget_override_usd,
        "ai_limit_usd": u.ai_budget_override_usd or ai.get("per_agent") or (ai.get("defaults") or {}).get(u.plan_tier),
        "id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email, "email": u.email,
        "brokerage": u.brokerage, "plan_tier": u.plan_tier, "subscription_status": u.subscription_status,
        # Still has their own PayPal subscription -> they're probably being charged twice.
        "has_paypal_subscription": bool(u.subscription_id), "last_seen_at": u.last_seen_at,
        "joined_at": u.created_at, "broker_authorized": bool(u.broker_authorized),
    }


def _group(g: BillingGroup, members: list, ai: Optional[dict] = None, emails: Optional[list] = None) -> dict:
    per = None
    if g.amount is not None and members:
        per = round(g.amount / len(members), 2)
    return {
        "id": g.id, "name": g.name, "brokerage": g.brokerage, "contact_name": g.contact_name, "contact_email": g.contact_email,
        "contact_phone": g.contact_phone, "paypal_reference": g.paypal_reference, "amount": g.amount, "billing_cycle": g.billing_cycle,
        "seats": g.seats, "plan_tier": g.plan_tier, "status": g.status, "started_on": g.started_on, "next_invoice_on": g.next_invoice_on,
        "notes": g.notes, "created_at": g.created_at, "per_agent": per,
        "ai_budget_per_agent_usd": g.ai_budget_per_agent_usd, "ai_budget_total_usd": g.ai_budget_total_usd,
        "ai_used_month_usd": round(sum((ai or {}).get("spend", {}).values()), 2),
        "pending_emails": [{"id": e.id, "email": e.email, "apply_plan": e.apply_plan, "invited_at": e.invited_at, "created_at": e.created_at}
                           for e in sorted(emails or [], key=lambda e: e.email)],
        "members": [_member(u, {**(ai or {}), "per_agent": g.ai_budget_per_agent_usd})
                    for u in sorted(members, key=lambda u: (u.first_name or u.email or '').lower())],
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
        if k in ("ai_budget_per_agent_usd", "ai_budget_total_usd") and v is not None:
            if v < 1 or v > 10000:
                raise HTTPException(status_code=400, detail="AI usage must be between $1 and $10,000 a month (blank = normal).")
            v = round(float(v), 2)
        if k == "status" and v not in ("active", "paused", "ended"):
            raise HTTPException(status_code=400, detail="Status must be active, paused or ended")
        if isinstance(v, str):
            v = v.strip() or None if k != "name" else v
        setattr(g, k, v)


def _give_managed_plan(u: User, tier: str) -> None:
    """Same as the admin plan switch: an ACTIVE plan with no PayPal subscription on the agent."""
    from app.services.website.site_service import reset_idx_site_question_on_upgrade
    reset_idx_site_question_on_upgrade(u, u.plan_tier, tier)   # moving onto IDX asks "use your IDX website?"
    if not u.subscription_started_at:
        u.subscription_started_at = datetime.now(timezone.utc)
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


async def _ai(db: AsyncSession, group_ids: list) -> dict:
    """This calendar month's AI spend per member + each plan's normal allowance."""
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_rate_limit_service import group_month_spend
    spend: dict = {}
    for gid in group_ids:
        spend.update(await group_month_spend(db, gid))
    config = await ai_config_service.get_config(db)
    return {"spend": spend, "defaults": config.get("agent_monthly_ai_budget_usd_by_tier") or {}}


async def _out(db: AsyncSession, g: BillingGroup) -> dict:
    members = (await db.execute(select(User).where(User.billing_group_id == g.id))).scalars().all()
    emails = (await db.execute(select(BillingGroupEmail).where(BillingGroupEmail.group_id == g.id))).scalars().all()
    return _group(g, list(members), await _ai(db, [g.id]), list(emails))


@router.get("")
async def list_groups(db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    groups = (await db.execute(select(BillingGroup).order_by(BillingGroup.name))).scalars().all()
    members = (await db.execute(select(User).where(User.billing_group_id.isnot(None)))).scalars().all()
    by_group: dict = {}
    for u in members:
        by_group.setdefault(u.billing_group_id, []).append(u)
    ai = await _ai(db, [g.id for g in groups])
    emails: dict = {}
    for e in (await db.execute(select(BillingGroupEmail))).scalars().all():
        emails.setdefault(e.group_id, []).append(e)
    return [_group(g, by_group.get(g.id, []), ai, emails.get(g.id, [])) for g in groups]


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
        u.ai_budget_override_usd = None
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
        u.ai_budget_override_usd = None   # their own AI amount was for this group only
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


@router.patch("/{group_id}/members/{user_id}")
async def set_member_ai(group_id: str, user_id: str, body: MemberAiIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """One person's own monthly AI allowance inside this group (blank = the group's / plan's)."""
    g = await _load(db, group_id)
    u = await db.get(User, user_id)
    if u is None or u.billing_group_id != g.id:
        raise HTTPException(status_code=404, detail="They're not in this group.")
    v = body.ai_budget_usd
    if v is not None and (v < 1 or v > 10000):
        raise HTTPException(status_code=400, detail="AI usage must be between $1 and $10,000 a month (blank = normal).")
    u.ai_budget_override_usd = round(float(v), 2) if v is not None else None
    await db.commit()
    return await _out(db, g)


def _split_emails(text: str) -> tuple:
    import re
    good, bad = [], []
    for raw in re.split(r"[\s,;]+", text or ""):
        e = raw.strip().strip("<>\"'").lower()
        if not e:
            continue
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", e):
            if e not in good:
                good.append(e)
        else:
            bad.append(raw.strip())
    return good, bad


@router.post("/{group_id}/emails")
async def add_emails(group_id: str, body: EmailsIn, db: AsyncSession = Depends(get_db), admin=Depends(require_admin_user)):
    """Paste people's emails. Anyone who already has an account joins the group now (with its plan,
    brokerage-paid). Everyone else is kept on the group's list and joins the moment they sign up
    with that email - no checkout - and can be emailed an invite to sign up."""
    from sqlalchemy import func as sfunc
    from app.services.open_house_service import schedule_showcase_backfill
    g = await _load(db, group_id)
    emails, bad = _split_emails(body.emails)
    if not emails:
        raise HTTPException(status_code=400, detail="No email addresses found - put one per line.")
    if len(emails) > 500:
        raise HTTPException(status_code=400, detail="That's more than 500 - add them in smaller batches.")
    joined, listed, moved, had_paypal, planned = [], [], [], [], []
    for e in emails:
        u = (await db.execute(select(User).where(sfunc.lower(User.email) == e))).scalar_one_or_none()
        if u is not None:
            if u.email == admin.email:
                continue
            u.billing_group_id = g.id
            if body.apply_plan and g.plan_tier:
                if u.subscription_id:
                    had_paypal.append({"id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email,
                                       "subscription_id": u.subscription_id})
                _give_managed_plan(u, g.plan_tier)
                planned.append(u.id)
            joined.append(e)
            continue
        row = (await db.execute(select(BillingGroupEmail).where(BillingGroupEmail.email == e))).scalar_one_or_none()
        if row is not None and row.group_id != g.id:
            moved.append(e)
        if row is None:
            row = BillingGroupEmail(group_id=g.id, email=e)
            db.add(row)
        row.group_id = g.id
        row.apply_plan = body.apply_plan
        listed.append(row)
    await db.commit()
    for uid in planned:
        schedule_showcase_backfill(uid)
    invited = 0
    if body.send_invite and listed:
        invited = await _invite(db, g, listed, admin)
    out = await _out(db, g)
    out.update({"joined": joined, "listed": [r.email for r in listed], "moved": moved, "invalid": bad,
                "invited": invited, "had_paypal": had_paypal})
    return out


async def _invite(db: AsyncSession, g: BillingGroup, rows: list, admin: User) -> int:
    import asyncio
    from urllib.parse import quote
    from app.services import payment_request_service as prs
    from app.services.agent_email_service import app_url
    sent = 0
    plan = prs.PLAN_NAMES.get(g.plan_tier or "")
    for r in rows:
        v = {"group_name": g.brokerage or g.name, "plan_name": plan,
             "link": f"{app_url()}/register?email={quote(r.email)}"}
        ok = await asyncio.get_running_loop().run_in_executor(None, lambda v=v, to=r.email: prs.send_invite(to, v, admin.email))
        if ok:
            r.invited_at = datetime.now(timezone.utc)
            sent += 1
    await db.commit()
    return sent


@router.post("/{group_id}/emails/{email_id}/invite")
async def reinvite(group_id: str, email_id: str, db: AsyncSession = Depends(get_db), admin=Depends(require_admin_user)):
    g = await _load(db, group_id)
    row = await db.get(BillingGroupEmail, email_id)
    if row is None or row.group_id != g.id:
        raise HTTPException(status_code=404, detail="Not on this group's list.")
    if not await _invite(db, g, [row], admin):
        raise HTTPException(status_code=502, detail="The email didn't go out - try again.")
    return await _out(db, g)


@router.delete("/{group_id}/emails/{email_id}")
async def remove_email(group_id: str, email_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    g = await _load(db, group_id)
    row = await db.get(BillingGroupEmail, email_id)
    if row is not None and row.group_id == g.id:
        await db.delete(row)
        await db.commit()
    return await _out(db, g)


async def join_from_list(db: AsyncSession, user: User) -> Optional[BillingGroup]:
    """Sign-up: an email the admin listed on a group joins it now - with the group's plan,
    brokerage-paid (no checkout) when the plan fits them. Caller commits."""
    from app.utils.plan_ids import tier_allowed_for
    row = (await db.execute(select(BillingGroupEmail).where(BillingGroupEmail.email == (user.email or "").lower()))).scalar_one_or_none()
    if row is None:
        return None
    g = await db.get(BillingGroup, row.group_id)
    if g is None:
        return None
    user.billing_group_id = g.id
    if row.apply_plan and g.plan_tier and tier_allowed_for(user, g.plan_tier):
        _give_managed_plan(user, g.plan_tier)
    await db.delete(row)
    return g
