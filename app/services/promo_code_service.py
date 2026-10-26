"""Admin-made sign-up codes (admin "Codes"). Reusable until switched off, used up or expired.
Checkout redeems them two ways:
  paypal_plan  - the agent subscribes on a special PayPal plan (e.g. IDX with no setup fee)
  billed_by_us - the account switches on with no PayPal; OpenHousePal bills the agent itself
The use is counted atomically, so a code with 10 uses can't be redeemed 11 times at once."""
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PromoCode


def clean_code(code: Optional[str]) -> str:
    return "".join((code or "").split()).upper()


def _usable_clause(now):
    return and_(
        PromoCode.is_active.is_(True),
        or_(PromoCode.expires_at.is_(None), PromoCode.expires_at > now),
        or_(PromoCode.max_uses.is_(None), PromoCode.uses < PromoCode.max_uses),
    )


async def find_usable(db: AsyncSession, code: str) -> Tuple[Optional[PromoCode], str]:
    """The code, or None plus a plain-English reason it can't be used."""
    row = (await db.execute(select(PromoCode).where(PromoCode.code == clean_code(code)))).scalar_one_or_none()
    if row is None:
        return None, "That code isn't valid. Check the spelling and try again."
    now = datetime.now(timezone.utc)
    if not row.is_active:
        return None, "That code has been switched off."
    if row.expires_at and row.expires_at <= now:
        return None, "That code has expired."
    if row.max_uses is not None and row.uses >= row.max_uses:
        return None, "That code has already been used the maximum number of times."
    return row, ""


async def claim(db: AsyncSession, code_id: str) -> bool:
    """Count one use, only if the code is still usable right now. Caller commits."""
    now = datetime.now(timezone.utc)
    res = await db.execute(
        update(PromoCode).where(PromoCode.id == code_id, _usable_clause(now))
        .values(uses=PromoCode.uses + 1).returning(PromoCode.id)
    )
    return res.first() is not None


async def tier_for_promo_plan(db: AsyncSession, plan_id: Optional[str]) -> Tuple[Optional[str], bool]:
    """(plan tier, has trial) for a special PayPal plan set on any code (even a switched-off one -
    agents already on it keep their plan)."""
    if not plan_id:
        return None, False
    row = (await db.execute(
        select(PromoCode.plan_tier, PromoCode.trial_days).where(PromoCode.paypal_plan_id == plan_id).limit(1)
    )).first()
    if row is None:
        return None, False
    return row[0], bool(row[1])


async def test_copy_source(db: AsyncSession, code: str, mls_id: Optional[str]):
    """A test-account code at Bright sign-up: (real agent being copied, code, "") when the code is
    usable and made for this MLS ID, else (None, None, reason). The use is counted at checkout."""
    from app.models.database import User

    promo, why = await find_usable(db, code)
    if promo is None:
        return None, None, why
    source = await db.get(User, promo.test_copy_of_user_id) if promo.test_copy_of_user_id else None
    if source is None or source.test_copy_of_user_id or not source.mls_id:
        return None, None, "That code isn't a test account code."
    if (mls_id or "").strip() != source.mls_id:
        return None, None, "That code is for a different MLS ID."
    return source, promo, ""


async def idx_waiver(db: AsyncSession, code: str):
    """A code that lets an agent move onto IDX without the setup fee: a usable 'special PayPal
    plan' code for the IDX plan. Returns (code, "") or (None, reason)."""
    promo, why = await find_usable(db, code)
    if promo is None:
        return None, why
    if promo.kind != "paypal_plan" or promo.plan_tier != "IDX" or not promo.paypal_plan_id:
        return None, "That code isn't for the IDX plan."
    return promo, ""
