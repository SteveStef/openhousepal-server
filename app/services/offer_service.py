"""Sign-up offers (admin, 11/11): "at a presentation, scan the QR code, sign up today and get the
deal I picked". Admin > People > Offers makes one; it gets a private page /offer/<token> (16 random
characters, never listed by Google) showing exactly what they get.

An offer = which plans + percent off each, how long the discount lasts (first payment / first N
months / forever), the IDX setup fee waived or not, the free trial on or off, a number of spots and
an optional end date. Each plan is its own PayPal plan (OfferPlan), made when the offer is saved,
so checkout charges exactly what the page says and renews at the right price by itself.

Spots: a spot is HELD the moment someone signs up through the offer (users.offer_id), so nobody
pays and then finds the offer full. Someone who got a spot keeps the deal at checkout even if the
offer ends in the meantime (they signed up in time) - unless the admin switches the offer off."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Offer, OfferPlan, User

logger = get_logger(__name__)

# The site's own prices - keep in step with frontend lib/pricing.ts (what the pricing page shows).
FULL_PRICE = {"NO_MLS": 19.95, "PRO_NO_MLS": 49.95, "BASIC": 24.95, "PREMIUM": 49.95, "IDX": 99.95}
IDX_SETUP_FEE = 500.0
TIERS = ("BASIC", "PREMIUM", "IDX", "NO_MLS", "PRO_NO_MLS")
PLAN_NAMES = {"NO_MLS": "No-MLS Basic", "PRO_NO_MLS": "No-MLS Premium", "BASIC": "Basic", "PREMIUM": "Premium", "IDX": "IDX"}
DURATIONS = ("first", "months", "forever")


class OfferError(Exception):
    pass


def _ping(msg: str) -> None:
    try:
        from app.services.discord_notifier import notifier
        notifier.send(msg)
    except Exception:
        pass


def trial_days_default() -> int:
    try:
        return int(os.getenv("TRIAL_PERIOD_DAYS", "14"))
    except ValueError:
        return 14


def new_token() -> str:
    return secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")   # 16 characters


def offer_price(full: float, percent: int) -> float:
    """Percent off, rounded to the cent the way a person would (half up)."""
    from decimal import ROUND_HALF_UP, Decimal
    return float((Decimal(str(full)) * (Decimal(100 - int(percent)) / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def discount_months(offer: Offer) -> Optional[int]:
    """How many monthly payments get the discount (None = every payment, forever)."""
    if offer.duration == "first":
        return 1
    if offer.duration == "months":
        return int(offer.months or 1)
    return None


def money(v: float) -> str:
    return f"${v:,.2f}"


# ------------------------------------------------------------------ PayPal plans

async def _product(db: AsyncSession) -> str:
    from app.services import app_cache
    from app.services.domain_purchase_service import _cached, _plan_ids
    from app.services.paypal_service import paypal_service
    key = f"paypal_custom_product:{os.getenv('PAYPAL_MODE', 'sandbox')}"
    product = await _cached(db, key)
    if not product:
        product = await paypal_service.create_custom_product()
        await app_cache.save(key, product)
        _plan_ids[key] = product
    return product


async def build_plans(db: AsyncSession, offer: Offer, percents: dict) -> list[OfferPlan]:
    """Make one PayPal plan per chosen plan. Nothing is saved if PayPal refuses any of them."""
    from app.services.paypal_service import paypal_service
    product = await _product(db)
    months = discount_months(offer)
    rows = []
    for tier in TIERS:
        if tier not in percents:
            continue
        pct = int(percents[tier])
        full = FULL_PRICE[tier]
        price = offer_price(full, pct)
        fee = 0.0 if (tier != "IDX" or offer.waive_idx_fee) else IDX_SETUP_FEE
        trial = trial_days_default() if offer.trial else 0
        pid = await paypal_service.create_offer_plan(
            product, f"OpenHousePal {PLAN_NAMES[tier]} - offer {offer.name}", full_price=full, offer_price=price,
            discount_months=months if pct else None, trial_days=trial, setup_fee=fee)
        rows.append(OfferPlan(tier=tier, percent_off=pct, base_price=full, offer_price=price, setup_fee=fee,
                              trial_days=trial, paypal_plan_id=pid, is_current=True))
    return rows


# ------------------------------------------------------------------ what's shown

def status(offer: Offer, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not offer.is_active:
        return "off"
    exp = offer.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp is not None and exp <= now:
        return "ended"
    if offer.uses >= offer.max_uses:
        return "full"
    return "live"


def deal_words(offer: Offer, pct: int) -> Optional[str]:
    """"20% off forever" / "20% off your first payment" / "20% off your first 3 months"."""
    if not pct:
        return None
    if offer.duration == "first":
        return f"{pct}% off your first month"
    if offer.duration == "months":
        n = int(offer.months or 1)
        return f"{pct}% off your first {n} months" if n > 1 else f"{pct}% off your first month"
    return f"{pct}% off every month"


def plan_view(offer: Offer, p: OfferPlan, *, with_plan_id: bool = False) -> dict:
    d = {
        "tier": p.tier, "name": PLAN_NAMES[p.tier], "percent_off": p.percent_off,
        "full_price": p.base_price, "offer_price": p.offer_price, "setup_fee": p.setup_fee,
        "normal_setup_fee": IDX_SETUP_FEE if p.tier == "IDX" else 0.0,
        "trial_days": p.trial_days, "deal": deal_words(offer, p.percent_off),
        "discount_months": discount_months(offer) if p.percent_off else None,
    }
    if with_plan_id:
        d["plan_id"] = p.paypal_plan_id
    return d


async def current_plans(db: AsyncSession, offer: Offer) -> list[OfferPlan]:
    rows = (await db.execute(select(OfferPlan).where(OfferPlan.offer_id == offer.id, OfferPlan.is_current.is_(True)))).scalars().all()
    return sorted(rows, key=lambda r: TIERS.index(r.tier))


async def public_view(db: AsyncSession, offer: Offer) -> dict:
    plans = await current_plans(db, offer)
    st = status(offer)
    return {
        "headline": offer.headline, "status": st,
        "duration": offer.duration, "months": offer.months, "trial": offer.trial,
        "trial_days": trial_days_default() if offer.trial else 0,
        "waive_idx_fee": offer.waive_idx_fee, "idx_setup_fee": IDX_SETUP_FEE,
        "expires_at": offer.expires_at,
        "spots_left": max(0, offer.max_uses - offer.uses) if st == "live" else 0,
        "plans": [plan_view(offer, p) for p in plans] if st == "live" else [],
    }


async def admin_view(db: AsyncSession, offer: Offer) -> dict:
    from app.services.agent_email_service import app_url
    plans = await current_plans(db, offer)
    paid = (await db.execute(select(User.id).where(User.offer_id == offer.id, User.subscription_id.isnot(None)))).all()
    return {
        "id": offer.id, "token": offer.token, "link": f"{app_url()}/offer/{offer.token}",
        "name": offer.name, "headline": offer.headline, "duration": offer.duration, "months": offer.months,
        "trial": offer.trial, "trial_days": trial_days_default(), "waive_idx_fee": offer.waive_idx_fee,
        "max_uses": offer.max_uses, "uses": offer.uses, "paid": len(paid),
        "expires_at": offer.expires_at, "is_active": offer.is_active, "status": status(offer),
        "plans": [plan_view(offer, p) for p in plans], "created_at": offer.created_at,
    }


# ------------------------------------------------------------------ spots

def can_take_offer(user: User) -> bool:
    """Only accounts that haven't paid for a plan yet (an offer is a sign-up deal)."""
    return not user.subscription_id and user.subscription_status in (None, "PENDING_PAYMENT")


async def claim(db: AsyncSession, offer: Offer, user: User) -> None:
    """Hold a spot for this person. Same offer again = nothing changes. A different offer they
    held before gives its spot back. Atomic: two people can't take the last spot together."""
    if user.offer_id == offer.id:
        return
    if not can_take_offer(user):
        raise OfferError("This offer is for new accounts - your account already has a plan.")
    now = datetime.now(timezone.utc)
    offer_id = offer.id
    got = (await db.execute(
        update(Offer).where(
            Offer.id == offer_id, Offer.is_active.is_(True), Offer.uses < Offer.max_uses,
            or_(Offer.expires_at.is_(None), Offer.expires_at > now),
        ).values(uses=Offer.uses + 1).returning(Offer.id)
    )).first()
    if got is None:
        await db.rollback()
        fresh = await db.get(Offer, offer_id)
        if fresh is not None:
            await db.refresh(fresh)
        st = status(fresh, now) if fresh is not None else "off"
        raise OfferError({"ended": "This offer has ended.", "full": "All the spots for this offer are taken.",
                          "off": "This offer isn't available any more."}.get(st, "This offer isn't available any more."))
    if user.offer_id:
        await db.execute(update(Offer).where(Offer.id == user.offer_id, Offer.uses > 0).values(uses=Offer.uses - 1))
    user.offer_id = offer.id
    await db.commit()
    await db.refresh(offer)
    left = offer.max_uses - offer.uses
    _ping(f":tickets: {user.first_name or ''} {user.last_name or ''} ({user.email}) took a spot in offer '{offer.name}' - {offer.uses} of {offer.max_uses} taken"
          + (" - the offer is now FULL." if left <= 0 else "."))


async def my_offer(db: AsyncSession, user: User) -> Optional[dict]:
    """The offer this person holds, with the plan ids checkout pays with - while they haven't paid
    and the admin hasn't switched it off (an end date passing doesn't take a held spot away)."""
    if not user.offer_id or not can_take_offer(user):
        return None
    offer = await db.get(Offer, user.offer_id)
    if offer is None or not offer.is_active:
        return None
    from app.utils.plan_ids import tier_allowed_for
    plans = [plan_view(offer, p, with_plan_id=True) for p in await current_plans(db, offer) if tier_allowed_for(user, p.tier)]
    if not plans:
        return None
    return {"token": offer.token, "headline": offer.headline, "duration": offer.duration, "months": offer.months,
            "trial": offer.trial, "waive_idx_fee": offer.waive_idx_fee, "plans": plans, "custom_id": f"offer:{user.id}"}


async def plan_for_checkout(db: AsyncSession, user: User, plan_id: str) -> Optional[OfferPlan]:
    """The OfferPlan this person may pay with (their held offer's plan) - else None."""
    if not user.offer_id or not plan_id:
        return None
    row = (await db.execute(select(OfferPlan).where(OfferPlan.paypal_plan_id == plan_id, OfferPlan.offer_id == user.offer_id))).scalar_one_or_none()
    return row


# ------------------------------------------------------------------ coming back after cancelling

def _paid_at(t: dict, price: float) -> bool:
    """A PayPal subscription payment that went through, for this amount."""
    if str(t.get("status") or "").upper() not in ("COMPLETED", "PARTIALLY_REFUNDED"):
        return False
    try:
        gross = float(((t.get("amount_with_breakdown") or {}).get("gross_amount") or {}).get("value"))
    except (TypeError, ValueError):
        return False
    return abs(gross - price) < 0.005


async def comeback_offer(db: AsyncSession, user: User, tier: str, now: Optional[datetime] = None) -> Optional[OfferPlan]:
    """Owner, 11/11: someone on a sign-up offer who cancels can restart at the offer price while their
    paid time hasn't run out - for whatever discount they had left. Once it runs out (status EXPIRED,
    or the end date passed), it's normal prices. Same plan only; never a second free trial or setup
    fee. None = no offer price applies. Raises OfferError if PayPal can't be checked (never guess)."""
    now = now or datetime.now(timezone.utc)
    end = user.next_billing_date
    if user.subscription_status != "CANCELLED" or not user.plan_id or not user.offer_id or not end:
        return None
    if (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) <= now:
        return None
    row = (await db.execute(select(OfferPlan).where(OfferPlan.paypal_plan_id == user.plan_id))).scalar_one_or_none()
    if row is None or row.tier != tier or row.offer_id != user.offer_id or row.percent_off <= 0:
        return None
    offer = await db.get(Offer, row.offer_id)
    if offer is None or not offer.is_active:
        return None
    # A restart plan carries only the discount that was left when it was made (never the offer's full count again).
    from app.services import app_cache
    months = discount_months(offer)
    if not row.is_current:
        saved = await app_cache.load(f"offer_plan_months:{row.paypal_plan_id}")
        months = None if saved in (None, "forever") else int(saved)
        if saved is None and discount_months(offer) is not None:
            return None   # unknown leftover on a limited deal: normal price rather than a guess
    left: Optional[int] = None
    if months is not None:
        from app.services.paypal_service import paypal_service
        try:
            txns = await paypal_service.list_subscription_transactions(user.subscription_id) if user.subscription_id else []
        except Exception as e:  # noqa: BLE001
            raise OfferError("We couldn't check your offer price with PayPal just now. Please try again in a minute.") from e
        used = sum(1 for t in txns if _paid_at(t, row.offer_price))
        left = months - used
        if left <= 0:
            return None
    return await _comeback_plan(db, offer, row, left)


async def _comeback_plan(db: AsyncSession, offer: Offer, row: OfferPlan, left: Optional[int]) -> OfferPlan:
    """The PayPal plan for a comeback: no trial, no setup fee, the offer price for `left` more
    payments (None = forever), then the full price. Made once per offer + plan + months left."""
    from app.services import app_cache
    from app.services.paypal_service import paypal_service
    key = f"offer_comeback:{os.getenv('PAYPAL_MODE', 'sandbox')}:{offer.id}:{row.tier}:{left if left is not None else 'forever'}:{row.offer_price:.2f}"
    pid = await app_cache.load(key)
    if pid:
        hit = (await db.execute(select(OfferPlan).where(OfferPlan.paypal_plan_id == str(pid)))).scalar_one_or_none()
        if hit is not None:
            return hit
    product = await _product(db)
    pid = await paypal_service.create_offer_plan(
        product, f"OpenHousePal {PLAN_NAMES[row.tier]} - offer {offer.name} (restart)", full_price=row.base_price,
        offer_price=row.offer_price, discount_months=left, trial_days=0, setup_fee=0.0)
    new = OfferPlan(offer_id=offer.id, tier=row.tier, percent_off=row.percent_off, base_price=row.base_price,
                    offer_price=row.offer_price, setup_fee=0.0, trial_days=0, paypal_plan_id=pid, is_current=False)
    db.add(new)
    await db.commit()
    await app_cache.save(key, pid)
    await app_cache.save(f"offer_plan_months:{pid}", left if left is not None else "forever")
    return new


async def tier_for_offer_plan(db: AsyncSession, plan_id: Optional[str]) -> tuple[Optional[str], bool]:
    """(tier, has free trial) for any offer plan, current or old - PayPal notices and syncs use it."""
    if not plan_id:
        return None, False
    row = (await db.execute(select(OfferPlan.tier, OfferPlan.trial_days).where(OfferPlan.paypal_plan_id == plan_id).limit(1))).first()
    if row is None:
        return None, False
    return row[0], bool(row[1])
