from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.schemas.seller_net_sheet import (
    SellerNetSheetResponse, SellerNetSheetListItem, SellerNetSheetCreate, SellerNetSheetUpdate,
    SnsShareResponse, SellerNetSheetPublicResponse,
)
from app.services import seller_net_sheet_service as service
from app.utils.auth import require_basic_plan, require_broker_authorization

# No router-level auth dependency - the public share-link route below is unauthenticated, like cma_routes.
router = APIRouter(prefix="/api/seller-net-sheets", tags=["seller-net-sheets"])


@router.get("", response_model=List[SellerNetSheetListItem])
async def list_seller_net_sheets(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.list_sheets(db, current_user.id)


@router.post("", response_model=SellerNetSheetResponse, status_code=201)
async def create_seller_net_sheet(
    payload: SellerNetSheetCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.create_sheet(db, current_user.id, payload)


@router.get("/public/{slug}", response_model=SellerNetSheetPublicResponse)
async def get_public_seller_net_sheet(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - the client-facing share link, no login required."""
    return await service.get_public_sheet(db, slug)


@router.get("/{sheet_id}", response_model=SellerNetSheetResponse)
async def get_seller_net_sheet(
    sheet_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.get_sheet(db, current_user.id, sheet_id)


@router.patch("/{sheet_id}", response_model=SellerNetSheetResponse)
async def update_seller_net_sheet(
    sheet_id: str,
    payload: SellerNetSheetUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.update_sheet(db, current_user.id, sheet_id, payload)


@router.delete("/{sheet_id}", status_code=204)
async def delete_seller_net_sheet(
    sheet_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    await service.delete_sheet(db, current_user.id, sheet_id)


@router.post("/{sheet_id}/share", response_model=SnsShareResponse)
async def share_seller_net_sheet(
    sheet_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.share_sheet(db, current_user.id, sheet_id)
