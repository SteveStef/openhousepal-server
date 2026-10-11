"""The real terms of a PayPal plan (trial length, monthly price, one-time setup fee, status),
read from PayPal and cached for an hour."""
from datetime import datetime, timedelta, timezone

from app.services.paypal_service import paypal_service

_CACHE: dict = {}
_UNIT_DAYS = {"DAY": 1, "WEEK": 7, "MONTH": 30, "YEAR": 365}


def parse_plan_terms(plan: dict) -> dict:
    trial_days, price, interval = 0, None, None
    for cycle in plan.get("billing_cycles") or []:
        freq = cycle.get("frequency") or {}
        if cycle.get("tenure_type") == "TRIAL":
            trial_days += _UNIT_DAYS.get(freq.get("interval_unit"), 0) * int(freq.get("interval_count") or 1) * int(cycle.get("total_cycles") or 1)
        elif cycle.get("tenure_type") == "REGULAR":
            price = ((cycle.get("pricing_scheme") or {}).get("fixed_price") or {}).get("value")
            interval = freq.get("interval_unit")
    setup = ((plan.get("payment_preferences") or {}).get("setup_fee") or {}).get("value")
    return {"trial_days": trial_days, "price": float(price) if price else None, "interval": interval,
            "setup_fee": float(setup) if setup else 0.0, "status": plan.get("status"), "name": plan.get("name")}


async def get_plan_terms(plan_id: str, fresh: bool = False) -> dict:
    """Raises whatever paypal_service raises when PayPal can't be reached or the plan doesn't exist."""
    hit = _CACHE.get(plan_id)
    if hit and not fresh and hit[0] > datetime.now(timezone.utc):
        return hit[1]
    terms = parse_plan_terms(await paypal_service.get_plan(plan_id))
    _CACHE[plan_id] = (datetime.now(timezone.utc) + timedelta(hours=1), terms)
    return terms
