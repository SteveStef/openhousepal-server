"""Sign-up offers - admin makes them (Admin > People > Offers), visitors open /offer/<token>.
See offer_service for the rules."""
from datetime import date, datetime, time, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import Offer, OfferPlan, User
from app.services import offer_service as osvc
from app.utils.auth import get_current_user, require_admin_user

logger = get_logger(__name__)
admin_router = APIRouter(prefix="/admin/offers", tags=["admin"])
public_router = APIRouter(prefix="/offers", tags=["offers"])
EASTERN = ZoneInfo("America/New_York")


class OfferIn(BaseModel):
    name: Optional[str] = None
    headline: Optional[str] = None
    percents: Optional[Dict[str, int]] = None     # tier -> percent off (0-90); the plans shown
    duration: Optional[str] = None                # first | months | forever
    months: Optional[int] = None
    trial: Optional[bool] = None
    waive_idx_fee: Optional[bool] = None
    max_uses: Optional[int] = None
    ends_on: Optional[date] = None                # last day (Eastern time) - None = no end date
    clear_end: bool = False
    is_active: Optional[bool] = None


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=EASTERN).astimezone(timezone.utc)


def _check(body: OfferIn, creating: bool) -> dict:
    data = body.model_dump(exclude_unset=True)
    if creating or "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Give the offer a name (only you see it).")
        data["name"] = name[:120]
    if "headline" in data:
        data["headline"] = (data.get("headline") or "").strip()[:140] or None
    if creating or "percents" in data:
        pc = data.get("percents") or {}
        pc = {t: v for t, v in pc.items() if v is not None}
        if not pc:
            raise HTTPException(400, "Pick at least one plan for the offer.")
        for t, v in pc.items():
            if t not in osvc.TIERS:
                raise HTTPException(400, "Unknown plan")
            if not isinstance(v, int) or v < 0 or v > 90:
                raise HTTPException(400, "Percent off must be 0 to 90.")
        data["percents"] = pc
    if creating or "duration" in data:
        if data.get("duration", "forever") not in osvc.DURATIONS:
            raise HTTPException(400, "Pick how long the discount lasts.")
    if data.get("duration") == "months":
        m = data.get("months")
        if not m or m < 2 or m > 24:
            raise HTTPException(400, "Number of months must be 2 to 24.")
    if creating or "max_uses" in data:
        mu = data.get("max_uses")
        if not mu or mu < 1 or mu > 10000:
            raise HTTPException(400, "Set how many people can use it (1 to 10,000).")
    if data.get("ends_on"):
        if data["ends_on"] < datetime.now(EASTERN).date():
            raise HTTPException(400, "The end date is in the past.")
    return data


async def _get(db: AsyncSession, offer_id: str) -> Offer:
    o = await db.get(Offer, offer_id)
    if o is None:
        raise HTTPException(404, "Offer not found")
    return o


@admin_router.get("")
async def list_offers(db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    rows = (await db.execute(select(Offer).order_by(Offer.created_at.desc()))).scalars().all()
    return [await osvc.admin_view(db, o) for o in rows]


@admin_router.get("/settings")
async def offer_settings(_admin=Depends(require_admin_user)):
    """The numbers the New offer form shows (the same ones the PayPal plans use)."""
    return {"trial_days": osvc.trial_days_default(), "idx_setup_fee": osvc.IDX_SETUP_FEE, "prices": osvc.FULL_PRICE}


@admin_router.post("")
async def create_offer(body: OfferIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    data = _check(body, True)
    o = Offer(token=osvc.new_token(), name=data["name"], headline=data.get("headline"),
              duration=data.get("duration", "forever"), months=data.get("months") if data.get("duration") == "months" else None,
              trial=data.get("trial", True), waive_idx_fee=bool(data.get("waive_idx_fee")) and "IDX" in data["percents"],
              max_uses=data["max_uses"], expires_at=_end_of_day(data["ends_on"]) if data.get("ends_on") else None, is_active=True)
    try:
        rows = await osvc.build_plans(db, o, data["percents"])
    except Exception:
        logger.exception("PayPal offer plans failed")
        raise HTTPException(502, "PayPal didn't answer - nothing was made. Try again in a minute.")
    db.add(o)
    await db.flush()
    for r in rows:
        r.offer_id = o.id
        db.add(r)
    await db.commit()
    await db.refresh(o)
    osvc._ping(f":tickets: Offer made: '{o.name}' - {len(rows)} plan(s), {o.max_uses} spots.")
    return await osvc.admin_view(db, o)


@admin_router.patch("/{offer_id}")
async def update_offer(offer_id: str, body: OfferIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """Name, page headline, spots, end date and on/off change in place. A new deal (plans, percents,
    how long, trial, IDX fee) makes new PayPal plans - people who already paid keep theirs."""
    o = await _get(db, offer_id)
    data = _check(body, False)
    deal_keys = {"percents", "duration", "months", "trial", "waive_idx_fee"}
    new_deal = bool(deal_keys & set(data))
    if "max_uses" in data and data["max_uses"] < o.uses:
        raise HTTPException(400, f"{o.uses} people already took a spot - the number can't go below that.")
    for k in ("name", "headline", "max_uses", "is_active"):
        if k in data:
            setattr(o, k, data[k])
    if data.get("clear_end"):
        o.expires_at = None
    elif data.get("ends_on"):
        o.expires_at = _end_of_day(data["ends_on"])
    if new_deal:
        old = await osvc.current_plans(db, o)
        if "duration" in data:
            o.duration = data["duration"]
        o.months = data.get("months", o.months) if o.duration == "months" else None
        if "trial" in data:
            o.trial = bool(data["trial"])
        percents = data.get("percents") or {p.tier: p.percent_off for p in old}
        if "waive_idx_fee" in data:
            o.waive_idx_fee = bool(data["waive_idx_fee"])
        o.waive_idx_fee = o.waive_idx_fee and "IDX" in percents
        try:
            rows = await osvc.build_plans(db, o, percents)
        except Exception:
            logger.exception("PayPal offer plans failed")
            await db.rollback()
            raise HTTPException(502, "PayPal didn't answer - nothing was changed. Try again in a minute.")
        for p in old:
            p.is_current = False
        for r in rows:
            r.offer_id = o.id
            db.add(r)
    await db.commit()
    await db.refresh(o)
    return await osvc.admin_view(db, o)


@admin_router.delete("/{offer_id}")
async def delete_offer(offer_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    o = await _get(db, offer_id)
    if o.uses > 0:
        raise HTTPException(400, "People already took a spot - switch it off instead so their deal keeps working.")
    await db.delete(o)
    await db.commit()
    return {"success": True}


# ------------------------------------------------------------------ public

@public_router.get("/my-plan")
async def my_offer_plan(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """The offer price this subscriber is on (Billing shows it instead of the regular price)."""
    if not user.plan_id or not user.subscription_id:
        return {"plan": None}
    row = (await db.execute(select(OfferPlan).where(OfferPlan.paypal_plan_id == user.plan_id))).scalar_one_or_none()
    if row is None:
        return {"plan": None}
    offer = await db.get(Offer, row.offer_id)
    return {"plan": osvc.plan_view(offer, row) if offer else None}


@public_router.get("/mine")
async def held_offer(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """The offer the signed-in person holds (checkout uses its prices) - or none."""
    return {"offer": await osvc.my_offer(db, user)}


async def _by_token(db: AsyncSession, token: str) -> Offer:
    o = (await db.execute(select(Offer).where(Offer.token == token))).scalar_one_or_none()
    if o is None:
        raise HTTPException(404, "This offer link isn't valid.")
    return o


@public_router.get("/{token}")
async def view_offer(token: str, db: AsyncSession = Depends(get_db)):
    return await osvc.public_view(db, await _by_token(db, token))


@public_router.post("/{token}/claim")
async def claim_offer(token: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    o = await _by_token(db, token)
    try:
        await osvc.claim(db, o, user)
    except osvc.OfferError as e:
        raise HTTPException(400, str(e))
    await db.refresh(user)
    return {"offer": await osvc.my_offer(db, user)}
