import secrets
import string
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import BuyerCashNeeded, User, StudioBrandKit, AgentHub
from app.schemas.buyer_cash_needed import (
    BuyerCashNeededCreate, BuyerCashNeededUpdate, BcnShareResponse, BuyerCashNeededPublicResponse,
    BcnPublicAgent, BuyerCashNeededListItem, DEFAULT_DISCLAIMER,
)


def _agent_display_name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip()


async def list_sheets(db: AsyncSession, owner_id: str) -> List[BuyerCashNeededListItem]:
    result = await db.execute(
        select(BuyerCashNeeded)
        .where(BuyerCashNeeded.owner_id == owner_id)
        .order_by(BuyerCashNeeded.updated_at.desc().nullslast(), BuyerCashNeeded.created_at.desc())
    )
    sheets = result.scalars().all()
    items = []
    for s in sheets:
        prop = s.property or {}
        address_parts = [prop.get('street_address'), prop.get('city'), prop.get('state')]
        address = ", ".join([p for p in address_parts if p]) or None
        items.append(BuyerCashNeededListItem(
            id=s.id,
            client_name=s.client_name,
            status=s.status,
            property_address=address,
            property_photo_url=prop.get('photo_url'),
            purchase_price=(s.purchase_price or {}).get('amount'),
            share_slug=s.share_slug,
            updated_at=s.updated_at,
            created_at=s.created_at,
        ))
    return items


async def _get_owned(db: AsyncSession, owner_id: str, sheet_id: str) -> BuyerCashNeeded:
    result = await db.execute(select(BuyerCashNeeded).where(BuyerCashNeeded.id == sheet_id, BuyerCashNeeded.owner_id == owner_id))
    sheet = result.scalar_one_or_none()
    if not sheet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Buyer Cash Needed sheet not found")
    return sheet


async def create_sheet(db: AsyncSession, owner_id: str, payload: BuyerCashNeededCreate) -> BuyerCashNeeded:
    prop = payload.property.model_dump() if payload.property else {}
    sheet = BuyerCashNeeded(
        owner_id=owner_id,
        client_name=payload.client_name,
        property=prop,
        purchase_price={'amount': prop.get('list_price')} if prop.get('list_price') else {},
        disclaimer=DEFAULT_DISCLAIMER,
    )
    db.add(sheet)
    await db.commit()
    await db.refresh(sheet)
    return sheet


async def get_sheet(db: AsyncSession, owner_id: str, sheet_id: str) -> BuyerCashNeeded:
    return await _get_owned(db, owner_id, sheet_id)


async def update_sheet(db: AsyncSession, owner_id: str, sheet_id: str, payload: BuyerCashNeededUpdate) -> BuyerCashNeeded:
    sheet = await _get_owned(db, owner_id, sheet_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ('client_name', 'status', 'property', 'purchase_price', 'down_payment_scenarios', 'loan_type',
                  'closing_costs', 'prepaid_expenses', 'inspections', 'earnest_money', 'credits', 'monthly_context',
                  'notes', 'disclaimer'):
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


async def share_sheet(db: AsyncSession, owner_id: str, sheet_id: str) -> BcnShareResponse:
    sheet = await _get_owned(db, owner_id, sheet_id)
    if not sheet.share_slug:
        for _ in range(10):
            candidate = _generate_slug()
            existing = await db.execute(select(BuyerCashNeeded.id).where(BuyerCashNeeded.share_slug == candidate))
            if existing.scalar_one_or_none() is None:
                sheet.share_slug = candidate
                break
    sheet.status = 'SHARED'
    await db.commit()
    await db.refresh(sheet)
    return BcnShareResponse(share_slug=sheet.share_slug, status=sheet.status, public_path=f"/cash-needed/{sheet.share_slug}")


async def get_public_sheet(db: AsyncSession, slug: str) -> BuyerCashNeededPublicResponse:
    result = await db.execute(select(BuyerCashNeeded).where(BuyerCashNeeded.share_slug == slug))
    sheet = result.scalar_one_or_none()
    if not sheet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This Buyer Cash Needed link isn't active.")

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

    agent = BcnPublicAgent(
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

    return BuyerCashNeededPublicResponse(
        client_name=sheet.client_name,
        property=sheet.property or {},
        purchase_price=sheet.purchase_price or {},
        down_payment_scenarios=sheet.down_payment_scenarios or [],
        loan_type=sheet.loan_type,
        closing_costs=sheet.closing_costs or [],
        prepaid_expenses=sheet.prepaid_expenses or [],
        inspections=sheet.inspections or [],
        earnest_money=sheet.earnest_money or {},
        credits=sheet.credits or {},
        monthly_context=sheet.monthly_context or {},
        notes=sheet.notes,
        disclaimer=sheet.disclaimer or DEFAULT_DISCLAIMER,
        agent=agent,
        updated_at=sheet.updated_at,
    )
