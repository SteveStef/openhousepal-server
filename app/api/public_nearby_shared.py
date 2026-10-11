"""Shared HTTP-layer glue for every public Explore Nearby endpoint - the visitor device-id
cookie and the ExploreNearby*Exception -> HTTPException mapping. Kept out of
nearby_amenities_service.py (a pure service module with no FastAPI/Request/Response imports)
and out of any one router module, since both the website-builder public site (public_site_routes.py)
and the Spotlight property site (public_property_site_routes.py) need the exact same budget-safe
behavior - one copy of this glue means both stay identical rather than drifting apart."""
import re
import uuid
from typing import Optional

import httpx
from fastapi import HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.nearby_amenities_service import (
    EXPLORE_NEARBY_CATEGORIES,
    ExploreNearbyBudgetExceeded,
    ExploreNearbyLookupUnavailable,
    get_explore_nearby,
    get_explore_place_details,
)

VISITOR_ID_COOKIE = "ohp_vid"
VISITOR_ID_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def visitor_device_id(request: Request, response: Response) -> str:
    """A soft per-browser identity for the device-scoped daily budget bucket - not a real device
    fingerprint (nothing here can defeat a determined bot that drops cookies), but it does two
    things a bare IP alone can't: keeps multiple real visitors sharing one IP (office wifi,
    carrier-grade NAT) from sharing one budget, and keeps catching one abusive client that rotates
    IPs (e.g. over mobile data) as long as it keeps the cookie. First-party, HttpOnly (no reason
    for page JS to ever read it), long-lived."""
    existing = request.cookies.get(VISITOR_ID_COOKIE)
    if existing and re.fullmatch(r"[A-Za-z0-9\-]{8,64}", existing):
        return existing
    new_id = str(uuid.uuid4())
    response.set_cookie(
        VISITOR_ID_COOKIE, new_id, max_age=VISITOR_ID_COOKIE_MAX_AGE,
        httponly=True, samesite="lax", secure=True,
    )
    return new_id


async def explore_nearby_response(
    db: AsyncSession, lat: float, lng: float, category: str,
    ip: Optional[str], device_id: str, owner_id: Optional[str],
):
    try:
        return await get_explore_nearby(db, lat, lng, category, ip=ip, device_id=device_id, owner_id=owner_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported category. Use one of: {', '.join(sorted(EXPLORE_NEARBY_CATEGORIES))}.",
        )
    except ExploreNearbyBudgetExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except ExploreNearbyLookupUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Nearby places are unavailable right now.")
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Nearby places lookup failed.")


async def explore_place_details_response(
    db: AsyncSession, place_id: str,
    ip: Optional[str], device_id: str, owner_id: Optional[str],
):
    try:
        return await get_explore_place_details(db, place_id, ip=ip, device_id=device_id, owner_id=owner_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid place ID.")
    except ExploreNearbyBudgetExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except ExploreNearbyLookupUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Nearby place details are unavailable right now.")
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Nearby place details lookup failed.")
