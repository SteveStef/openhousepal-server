"""Public market data that isn't tied to one agent - e.g. today's mortgage rates for the
Spotlight payment estimator. Straight from Freddie Mac's published survey (free, weekly), cached
in-process for 6 hours by town_knowledge_service."""
from fastapi import APIRouter

from app.services.areas import town_knowledge_service as tk

router = APIRouter(prefix="/public/market", tags=["market-data"])


@router.get("/mortgage-rates")
async def get_mortgage_rates():
    """Latest Freddie Mac PMMS averages: {"rate_30", "rate_15", "as_of", "source"}; nulls if the
    survey couldn't be read (the estimator then keeps its own default)."""
    try:
        latest = await tk.freddie_mac_latest()
    except Exception:
        latest = None
    if not latest:
        return {"rate_30": None, "rate_15": None, "as_of": None, "source": None}
    return {**latest, "source": "Freddie Mac Primary Mortgage Market Survey"}
