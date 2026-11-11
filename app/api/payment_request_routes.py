"""Custom payment requests - admin makes and sends them, the payer pays at /pay/<token>.
See payment_request_service for how a request stays in step with PayPal."""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import BillingGroup, PaymentRequest, User
from app.services import payment_request_service as prs
from app.utils.auth import require_admin_user

logger = get_logger(__name__)
admin_router = APIRouter(prefix="/admin/payment-requests", tags=["admin"])
public_router = APIRouter(prefix="/pay", tags=["pay"])

PLAN_TIERS = {"NO_MLS", "PRO_NO_MLS", "BASIC", "PREMIUM", "IDX"}


class RequestIn(BaseModel):
    title: Optional[str] = None
    note: Optional[str] = None
    amount: Optional[float] = None
    cycle: Optional[str] = None
    total_payments: Optional[int] = None
    billing_group_id: Optional[str] = None
    user_id: Optional[str] = None
    payer_name: Optional[str] = None
    payer_email: Optional[str] = None
    plan_tier: Optional[str] = None
    seats: Optional[int] = None
    # Make a brokerage group for this deal on the spot (its name), instead of picking one.
    new_group_name: Optional[str] = None
    # This is a new deal for the same people: stop that older request once this one is paid.
    replaces_request_id: Optional[str] = None
    send_email: bool = False


class ConfirmIn(BaseModel):
    subscription_id: str


def _clean(body: RequestIn, req: Optional[PaymentRequest]) -> dict:
    data = body.model_dump(exclude_unset=True)
    data.pop("send_email", None)
    data.pop("new_group_name", None)
    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = v.strip() or None
    if "title" in data and not data["title"]:
        raise HTTPException(400, "Say what the payment is for.")
    if "amount" in data:
        a = data["amount"]
        if a is None or a < 1 or a > 100000:
            raise HTTPException(400, "The amount must be between $1 and $100,000.")
        data["amount"] = round(float(a), 2)
    if "cycle" in data and data["cycle"] not in prs.CYCLES:
        raise HTTPException(400, "Pick one time, monthly, every 3 months or yearly.")
    if data.get("total_payments") is not None and not (1 <= data["total_payments"] <= 120):
        raise HTTPException(400, "Number of payments must be 1 to 120 (or blank for until cancelled).")
    if data.get("seats") is not None and not (1 <= data["seats"] <= 10000):
        raise HTTPException(400, "People covered must be at least 1.")
    if data.get("plan_tier") and data["plan_tier"] not in PLAN_TIERS:
        raise HTTPException(400, "Unknown plan")
    if data.get("payer_email") and ("@" not in data["payer_email"] or "." not in data["payer_email"].split("@")[-1]):
        raise HTTPException(400, "That email address doesn't look right.")
    return data


async def _get(db: AsyncSession, request_id: str) -> PaymentRequest:
    req = await db.get(PaymentRequest, request_id)
    if req is None:
        raise HTTPException(404, "Payment request not found")
    return req


async def _send(db: AsyncSession, req: PaymentRequest, admin: User) -> bool:
    if not req.payer_email:
        raise HTTPException(400, "Add the payer's email first - or copy the link and send it yourself.")
    group_name, user_name = await prs._names(db, req)
    covers = prs.covers_line(req, group_name, user_name)
    ok = await asyncio.get_running_loop().run_in_executor(
        None, lambda: prs.send_email(req, covers=covers, reply_to=admin.email))
    if ok:
        from datetime import datetime, timezone
        req.sent_at = datetime.now(timezone.utc)
        req.sent_count = (req.sent_count or 0) + 1
        await db.commit()
        await db.refresh(req)
    return ok


@admin_router.get("")
async def list_requests(db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    rows = (await db.execute(select(PaymentRequest).order_by(PaymentRequest.created_at.desc()))).scalars().all()
    return [await prs.admin_view(db, r) for r in rows]


@admin_router.post("")
async def create_request(body: RequestIn, db: AsyncSession = Depends(get_db), admin=Depends(require_admin_user)):
    data = _clean(body, None)
    if not data.get("title") or not data.get("amount"):
        raise HTTPException(400, "Say what it's for and the amount.")
    data.setdefault("cycle", "monthly")
    if data["cycle"] == "once":
        data["total_payments"] = None
    if data.get("billing_group_id") and not await db.get(BillingGroup, data["billing_group_id"]):
        raise HTTPException(400, "That group doesn't exist any more.")
    user = await db.get(User, data["user_id"]) if data.get("user_id") else None
    if data.get("user_id") and not user:
        raise HTTPException(400, "That agent doesn't exist any more.")
    if data.get("replaces_request_id"):
        old = await db.get(PaymentRequest, data["replaces_request_id"])
        if old is None or old.status not in ("open", "active", "past_due"):
            raise HTTPException(400, "The payment it replaces isn't running any more.")
    if (body.new_group_name or "").strip():
        g = BillingGroup(name=body.new_group_name.strip(), billing_cycle=data["cycle"] if data["cycle"] != "once" else "monthly",
                         status="paused", amount=data["amount"], seats=data.get("seats"), plan_tier=data.get("plan_tier"),
                         contact_name=data.get("payer_name"), contact_email=data.get("payer_email"))
        db.add(g)
        await db.flush()
        data["billing_group_id"] = g.id
    # Paying for one agent: fill in who pays from their account when left blank.
    if user and not data.get("payer_email"):
        data["payer_email"] = user.email
    if user and not data.get("payer_name"):
        data["payer_name"] = f"{user.first_name or ''} {user.last_name or ''}".strip() or None
    if data.get("billing_group_id") and not (data.get("payer_email") and data.get("payer_name")):
        g = await db.get(BillingGroup, data["billing_group_id"])
        if not data.get("payer_name"):
            data["payer_name"] = g.contact_name
        if not data.get("payer_email"):
            data["payer_email"] = g.contact_email
    req = PaymentRequest(token=prs.new_token(), status="open", **data)
    try:
        req.paypal_plan_id = await prs.make_plan(db, req)
    except Exception:
        logger.exception("PayPal plan for a payment request failed")
        await db.rollback()
        raise HTTPException(502, "PayPal didn't answer - nothing was made. Try again in a minute.")
    db.add(req)
    await db.commit()
    await db.refresh(req)
    prs._ping(f":receipt: Payment request made: '{req.title}' - {prs.price_line(req)} for {req.payer_name or req.payer_email or 'a payer'}.")
    out = await prs.admin_view(db, req)
    if body.send_email:
        out["emailed"] = await _send(db, req, admin)
        out = {**await prs.admin_view(db, req), "emailed": out["emailed"]}
    return out


@admin_router.patch("/{request_id}")
async def update_request(request_id: str, body: RequestIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Only while nobody has paid: a new price or schedule makes a new PayPal plan (same link)."""
    req = await _get(db, request_id)
    if req.status != "open" or req.paypal_subscription_id:
        raise HTTPException(400, "It's already been paid - cancel it and make a new one to change the deal.")
    data = _clean(body, req)
    price_changed = any(k in data and data[k] != getattr(req, k) for k in ("amount", "cycle", "total_payments", "title"))
    for k, v in data.items():
        setattr(req, k, v)
    if req.cycle == "once":
        req.total_payments = None
    if price_changed:
        try:
            req.paypal_plan_id = await prs.make_plan(db, req)
        except Exception:
            logger.exception("PayPal plan for a payment request failed")
            await db.rollback()
            raise HTTPException(502, "PayPal didn't answer - nothing was changed. Try again in a minute.")
    await db.commit()
    await db.refresh(req)
    return await prs.admin_view(db, req)


@admin_router.post("/{request_id}/send")
async def send_request(request_id: str, db: AsyncSession = Depends(get_db), admin=Depends(require_admin_user)):
    req = await _get(db, request_id)
    if req.status not in ("open", "past_due"):
        raise HTTPException(400, "Nothing to pay on this one.")
    ok = await _send(db, req, admin)
    if not ok:
        raise HTTPException(502, "The email didn't go out - copy the link and send it yourself.")
    return await prs.admin_view(db, req)


@admin_router.post("/{request_id}/cancel")
async def cancel_request(request_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    req = await _get(db, request_id)
    if req.status in ("cancelled", "ended"):
        return await prs.admin_view(db, req)
    try:
        req = await prs.cancel(db, req)
    except prs.PaymentRequestError as e:
        raise HTTPException(502, str(e))
    return await prs.admin_view(db, req)


@admin_router.post("/{request_id}/refresh")
async def refresh_request(request_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Check PayPal now (payments in, next charge, card failed)."""
    req = await _get(db, request_id)
    if req.paypal_subscription_id:
        try:
            await prs.sync(db, req)
            await db.commit()
            await db.refresh(req)
        except Exception:
            logger.warning("Payment request refresh failed", exc_info=True, extra={"request": req.id})
            raise HTTPException(502, "PayPal didn't answer - try again in a minute.")
    return await prs.admin_view(db, req)


@admin_router.delete("/{request_id}")
async def delete_request(request_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Only an unpaid one (anything paid stays for the record - cancel it instead)."""
    req = await _get(db, request_id)
    if req.paypal_subscription_id or req.payments_count:
        raise HTTPException(400, "Paid requests stay for your records - cancel it instead.")
    await db.delete(req)
    await db.commit()
    return {"success": True}


# ------------------------------------------------------------------ public pay page

async def _by_token(db: AsyncSession, token: str) -> PaymentRequest:
    req = (await db.execute(select(PaymentRequest).where(PaymentRequest.token == token))).scalar_one_or_none()
    if req is None:
        raise HTTPException(404, "This payment link isn't valid.")
    return req


@public_router.get("/{token}")
async def view_request(token: str, db: AsyncSession = Depends(get_db)):
    return await prs.public_view(db, await _by_token(db, token))


@public_router.post("/{token}/confirm")
async def confirm_request(token: str, body: ConfirmIn, db: AsyncSession = Depends(get_db)):
    req = await _by_token(db, token)
    try:
        req = await prs.attach(db, req, body.subscription_id)
    except prs.PaymentRequestError as e:
        raise HTTPException(400, str(e))
    return await prs.public_view(db, req)
