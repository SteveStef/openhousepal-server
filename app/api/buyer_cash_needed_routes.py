from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.schemas.buyer_cash_needed import (
    BuyerCashNeededResponse, BuyerCashNeededListItem, BuyerCashNeededCreate, BuyerCashNeededUpdate,
    BcnShareResponse, BuyerCashNeededPublicResponse,
)
from app.services import buyer_cash_needed_service as service
from app.utils.auth import require_basic_plan, require_broker_authorization

router = APIRouter(prefix="/api/buyer-cash-needed", tags=["buyer-cash-needed"])


@router.get("", response_model=List[BuyerCashNeededListItem])
async def list_buyer_cash_needed_sheets(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.list_sheets(db, current_user.id)


@router.post("", response_model=BuyerCashNeededResponse, status_code=201)
async def create_buyer_cash_needed_sheet(
    payload: BuyerCashNeededCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.create_sheet(db, current_user.id, payload)


@router.get("/public/{slug}", response_model=BuyerCashNeededPublicResponse)
async def get_public_buyer_cash_needed_sheet(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - the client-facing share link, no login required."""
    return await service.get_public_sheet(db, slug)


@router.get("/{sheet_id}", response_model=BuyerCashNeededResponse)
async def get_buyer_cash_needed_sheet(
    sheet_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.get_sheet(db, current_user.id, sheet_id)


@router.patch("/{sheet_id}", response_model=BuyerCashNeededResponse)
async def update_buyer_cash_needed_sheet(
    sheet_id: str,
    payload: BuyerCashNeededUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.update_sheet(db, current_user.id, sheet_id, payload)


@router.delete("/{sheet_id}", status_code=204)
async def delete_buyer_cash_needed_sheet(
    sheet_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    await service.delete_sheet(db, current_user.id, sheet_id)


@router.post("/{sheet_id}/share", response_model=BcnShareResponse)
async def share_buyer_cash_needed_sheet(
    sheet_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.share_sheet(db, current_user.id, sheet_id)
