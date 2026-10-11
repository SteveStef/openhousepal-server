import secrets
import string
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import SellerNetSheet, User, StudioBrandKit, AgentHub
from app.schemas.seller_net_sheet import (
    SellerNetSheetCreate, SellerNetSheetUpdate, SnsShareResponse, SellerNetSheetPublicResponse,
    SnsPublicAgent, SellerNetSheetListItem, DEFAULT_DISCLAIMER,
)


def _agent_display_name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip()


async def list_sheets(db: AsyncSession, owner_id: str) -> List[SellerNetSheetListItem]:
    result = await db.execute(
        select(SellerNetSheet)
        .where(SellerNetSheet.owner_id == owner_id)
        .order_by(SellerNetSheet.updated_at.desc().nullslast(), SellerNetSheet.created_at.desc())
    )
    sheets = result.scalars().all()
    items = []
    for s in sheets:
        prop = s.property or {}
        address_parts = [prop.get('street_address'), prop.get('city'), prop.get('state')]
        address = ", ".join([p for p in address_parts if p]) or None
        scenarios = s.scenarios or []
        top_sale_price = scenarios[0].get('sale_price') if scenarios else prop.get('list_price')
        items.append(SellerNetSheetListItem(
            id=s.id,
            client_name=s.client_name,
            status=s.status,
            property_address=address,
            property_photo_url=prop.get('photo_url'),
            scenario_count=len(scenarios),
            top_sale_price=top_sale_price,
            share_slug=s.share_slug,
            updated_at=s.updated_at,
            created_at=s.created_at,
        ))
    return items


async def _get_owned(db: AsyncSession, owner_id: str, sheet_id: str) -> SellerNetSheet:
    result = await db.execute(select(SellerNetSheet).where(SellerNetSheet.id == sheet_id, SellerNetSheet.owner_id == owner_id))
    sheet = result.scalar_one_or_none()
    if not sheet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Seller Net Sheet not found")
    return sheet


async def create_sheet(db: AsyncSession, owner_id: str, payload: SellerNetSheetCreate) -> SellerNetSheet:
    sheet = SellerNetSheet(
        owner_id=owner_id,
        client_name=payload.client_name,
        property=(payload.property.model_dump() if payload.property else {}),
        disclaimer=DEFAULT_DISCLAIMER,
    )
    db.add(sheet)
    await db.commit()
    await db.refresh(sheet)
    return sheet


async def get_sheet(db: AsyncSession, owner_id: str, sheet_id: str) -> SellerNetSheet:
    return await _get_owned(db, owner_id, sheet_id)


async def update_sheet(db: AsyncSession, owner_id: str, sheet_id: str, payload: SellerNetSheetUpdate) -> SellerNetSheet:
    sheet = await _get_owned(db, owner_id, sheet_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ('client_name', 'status', 'property', 'scenarios', 'mortgage_payoffs', 'compensation', 'closing_costs', 'tax_proration', 'repairs', 'notes', 'disclaimer'):
        if field in data:
            setattr(sheet, field, data[field])
    await db.commit()
    await db.refresh(sheet)
    return sheet


async def delete_sheet(db: AsyncSession, owner_id: str, sheet_id: str) -> None:
    sheet = await _get_owned(db, owner_id, sheet_id)
    await db.delete(sheet)
    await db.commit()


def _generate_slug() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(10))


async def share_sheet(db: AsyncSession, owner_id: str, sheet_id: str) -> SnsShareResponse:
    sheet = await _get_owned(db, owner_id, sheet_id)
    if not sheet.share_slug:
        for _ in range(10):
            candidate = _generate_slug()
            existing = await db.execute(select(SellerNetSheet.id).where(SellerNetSheet.share_slug == candidate))
            if existing.scalar_one_or_none() is None:
                sheet.share_slug = candidate
                break
    sheet.status = 'SHARED'
    await db.commit()
    await db.refresh(sheet)
    return SnsShareResponse(share_slug=sheet.share_slug, status=sheet.status, public_path=f"/net-sheet/{sheet.share_slug}")


async def get_public_sheet(db: AsyncSession, slug: str) -> SellerNetSheetPublicResponse:
    result = await db.execute(select(SellerNetSheet).where(SellerNetSheet.share_slug == slug))
    sheet = result.scalar_one_or_none()
    if not sheet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This Seller Net Sheet link isn't active.")

    owner_result = await db.execute(select(User).where(User.id == sheet.owner_id))
    owner = owner_result.scalar_one_or_none()
    # A client link only works while the agent has an active plan (same as every other share link).
    from app.utils.auth import owner_has_access
    if not owner or not owner_has_access(owner):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link isn't active.")
    brand_result = await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == sheet.owner_id))
    brand = brand_result.scalar_one_or_none()
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == sheet.owner_id))
    hub = hub_result.scalar_one_or_none()

    agent = SnsPublicAgent(
        name=_agent_display_name(owner) if owner else "Your Agent",
        headshot_url=owner.photo_url if owner else None,
        brokerage=owner.brokerage if owner else None,
        phone=None,
        email=owner.email if owner else None,
        logo_url=((brand.personal_logo_url or brand.brokerage_logo_url) if brand else None),
        primary_color=brand.primary_color if brand else None,
        secondary_color=brand.secondary_color if brand else None,
        agent_hub_slug=hub.slug if hub else None,
    )

    return SellerNetSheetPublicResponse(
        client_name=sheet.client_name,
        property=sheet.property or {},
        scenarios=sheet.scenarios or [],
        mortgage_payoffs=sheet.mortgage_payoffs or [],
        compensation=sheet.compensation or {},
        closing_costs=sheet.closing_costs or [],
        tax_proration=sheet.tax_proration or {},
        repairs=sheet.repairs or [],
        notes=sheet.notes,
        disclaimer=sheet.disclaimer or DEFAULT_DISCLAIMER,
        agent=agent,
        updated_at=sheet.updated_at,
    )
