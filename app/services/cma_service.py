import random
import string
from datetime import datetime, date
from typing import Optional, List

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import CmaReport, HomeType, Property, User, StudioBrandKit, AgentHub
from app.schemas.cma import (
    CmaReportCreate, CmaReportUpdate, CmaCompSearchRequest, CmaCompCandidate,
    CmaShareResponse, CmaPublicResponse, CmaPublicAgent, CmaReportListItem,
)
from app.utils.geo import get_lat_long_offsets, haversine_distance, is_within_distance


def _agent_display_name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip()


async def list_reports(db: AsyncSession, owner_id: str) -> List[CmaReportListItem]:
    result = await db.execute(
        select(CmaReport)
        .where(CmaReport.owner_id == owner_id)
        .order_by(CmaReport.updated_at.desc().nullslast(), CmaReport.created_at.desc())
    )
    reports = result.scalars().all()
    items = []
    for r in reports:
        subject = r.subject or {}
        pricing = r.pricing or {}
        address_parts = [subject.get('street_address'), subject.get('city'), subject.get('state')]
        address = ", ".join([p for p in address_parts if p]) or None
        items.append(CmaReportListItem(
            id=r.id,
            client_name=r.client_name,
            mode=r.mode,
            status=r.status,
            subject_address=address,
            subject_photo_url=subject.get('photo_url'),
            comparable_count=len([c for c in (r.comparables or []) if c.get('is_included', True)]),
            value_range_min=pricing.get('recommended_min') if pricing.get('recommended_min') is not None else pricing.get('comparable_suggested_min'),
            value_range_max=pricing.get('recommended_max') if pricing.get('recommended_max') is not None else pricing.get('comparable_suggested_max'),
            updated_at=r.updated_at,
            created_at=r.created_at,
        ))
    return items


async def _get_owned(db: AsyncSession, owner_id: str, report_id: str) -> CmaReport:
    result = await db.execute(select(CmaReport).where(CmaReport.id == report_id, CmaReport.owner_id == owner_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CMA not found")
    return report


async def create_report(db: AsyncSession, owner_id: str, payload: CmaReportCreate) -> CmaReport:
    report = CmaReport(
        owner_id=owner_id,
        client_name=payload.client_name,
        mode=payload.mode,
        subject=(payload.subject.model_dump() if payload.subject else {}),
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


async def get_report(db: AsyncSession, owner_id: str, report_id: str) -> CmaReport:
    return await _get_owned(db, owner_id, report_id)


async def update_report(db: AsyncSession, owner_id: str, report_id: str, payload: CmaReportUpdate) -> CmaReport:
    report = await _get_owned(db, owner_id, report_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ('client_name', 'mode', 'status', 'subject', 'pricing', 'market_context', 'presentation', 'comparables', 'improvements'):
        if field in data:
            setattr(report, field, data[field])
    await db.commit()
    await db.refresh(report)
    return report


async def delete_report(db: AsyncSession, owner_id: str, report_id: str) -> None:
    report = await _get_owned(db, owner_id, report_id)
    await db.delete(report)
    await db.commit()


def _generate_slug() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(random.choices(alphabet, k=10))


async def share_report(db: AsyncSession, owner_id: str, report_id: str) -> CmaShareResponse:
    report = await _get_owned(db, owner_id, report_id)
    if not report.share_slug:
        for _ in range(10):
            candidate = _generate_slug()
            existing = await db.execute(select(CmaReport.id).where(CmaReport.share_slug == candidate))
            if existing.scalar_one_or_none() is None:
                report.share_slug = candidate
                break
    report.status = 'SHARED'
    await db.commit()
    await db.refresh(report)
    return CmaShareResponse(share_slug=report.share_slug, status=report.status, public_path=f"/cma/{report.share_slug}")


async def get_public_report(db: AsyncSession, slug: str) -> CmaPublicResponse:
    result = await db.execute(select(CmaReport).where(CmaReport.share_slug == slug))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This CMA link isn't active.")

    owner_result = await db.execute(select(User).where(User.id == report.owner_id))
    owner = owner_result.scalar_one_or_none()
    brand_result = await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == report.owner_id))
    brand = brand_result.scalar_one_or_none()
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == report.owner_id))
    hub = hub_result.scalar_one_or_none()

    agent = CmaPublicAgent(
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

    return CmaPublicResponse(
        client_name=report.client_name,
        mode=report.mode,
        subject=report.subject or {},
        comparables=report.comparables or [],
        pricing=report.pricing or {},
        market_context=report.market_context or {},
        improvements=report.improvements or [],
        presentation=report.presentation or {},
        agent=agent,
        updated_at=report.updated_at,
    )


def _clean_status(home_status: Optional[str]) -> str:
    return (home_status or "").upper().replace("-BRIGHT", "")


def _categorize_status(home_status: Optional[str]) -> Optional[str]:
    s = _clean_status(home_status)
    if s.startswith("CLOSED") or s.startswith("SOLD"):
        return "sold"
    if s.startswith("ACTIVE") and "CONTRACT" in s:
        return "active_under_contract"
    if s.startswith("PENDING") or "CONTRACT" in s:
        return "pending"
    if s.startswith("ACTIVE") or s.startswith("COMING SOON"):
        return "active"
    return None


def _parse_date(value) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        try:
            return datetime.strptime(value[:10], '%Y-%m-%d').date()
        except ValueError:
            return None


async def search_comparables(db: AsyncSession, req: CmaCompSearchRequest) -> List[CmaCompCandidate]:
    """Finds candidate comparables around the subject from the local MLS mirror. This suggests -
    it never decides - which properties belong in the CMA; the agent picks from these results."""
    if req.latitude is None or req.longitude is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Subject property location is required to search for comparables.")

    lat_offset, lng_offset = get_lat_long_offsets(req.latitude, req.radius_miles)
    query = select(Property).where(
        Property.latitude.between(req.latitude - lat_offset, req.latitude + lat_offset),
        Property.longitude.between(req.longitude - lng_offset, req.longitude + lng_offset),
        # A rental's completed lease also lands on MlsStatus=CLOSED-BRIGHT, same as an actual
        # sale - home_type is what actually distinguishes them, so a CMA (always sale comps)
        # must exclude leases explicitly rather than relying on status alone. NULL-safe (keeps
        # rows with no home_type at all) so this can only ever exclude a row we're confident is
        # actually a lease, never silently drop an unmapped sold comp.
        or_(Property.home_type.is_(None), Property.home_type != HomeType.RESIDENTIAL_LEASE.value),
    )
    if req.listing_key:
        query = query.where(Property.listing_key != req.listing_key)
    if req.exclude_listing_keys:
        query = query.where(Property.listing_key.notin_(req.exclude_listing_keys))
    if req.min_price is not None:
        query = query.where(Property.price >= req.min_price)
    if req.max_price is not None:
        query = query.where(Property.price <= req.max_price)
    if req.min_sqft is not None:
        query = query.where(Property.living_area >= req.min_sqft)
    if req.max_sqft is not None:
        query = query.where(Property.living_area <= req.max_sqft)
    if req.min_lot_size is not None:
        query = query.where(Property.lot_size >= req.min_lot_size)
    if req.max_lot_size is not None:
        query = query.where(Property.lot_size <= req.max_lot_size)
    if req.min_beds is not None:
        query = query.where(Property.bedrooms >= req.min_beds)
    if req.min_baths is not None:
        query = query.where(Property.bathrooms >= req.min_baths)
    if req.min_year_built is not None:
        query = query.where(Property.year_built >= req.min_year_built)
    if req.max_year_built is not None:
        query = query.where(Property.year_built <= req.max_year_built)

    result = await db.execute(query.limit(300))
    rows = result.scalars().all()

    date_from = _parse_date(req.sale_date_from)
    date_to = _parse_date(req.sale_date_to)

    candidates: List[CmaCompCandidate] = []
    for p in rows:
        if not is_within_distance(req.latitude, req.longitude, p.latitude, p.longitude, req.radius_miles):
            continue
        category = _categorize_status(p.home_status)
        if category not in req.statuses:
            continue

        raw = p.raw_mls_data or {}
        sale_price = raw.get('ClosePrice')
        sale_date = None
        if category == 'sold':
            # CloseDate is frequently missing from the mirrored MLS record for closed listings -
            # falling back to when we last saw/synced the record keeps the "sold in the last N
            # days" filter usable instead of silently excluding almost every sold comp.
            close_date_raw = raw.get('CloseDate')
            parsed = _parse_date(close_date_raw) or (p.updated_at.date() if p.updated_at else None)
            if date_from and (not parsed or parsed < date_from):
                continue
            if date_to and (not parsed or parsed > date_to):
                continue
            sale_date = close_date_raw or (p.updated_at.date().isoformat() if p.updated_at else None)
        else:
            sale_price = None

        list_price = p.price
        effective_price = sale_price or list_price
        sqft = p.living_area
        price_per_sqft = round(effective_price / sqft, 2) if effective_price and sqft else None
        distance = haversine_distance(req.latitude, req.longitude, p.latitude, p.longitude) if p.latitude and p.longitude else None

        garage = None
        if p.has_garage and p.garage_spaces:
            garage = f"{int(p.garage_spaces)} Car"
        elif p.has_garage:
            garage = "Yes"
        elif p.has_garage is False:
            garage = "None"

        basement = None
        raw_basement = raw.get('Basement')
        if isinstance(raw_basement, list) and raw_basement:
            basement = ", ".join(raw_basement)
        elif raw.get('BasementYN') is True:
            basement = "Yes"
        elif raw.get('BasementYN') is False:
            basement = "None"

        candidates.append(CmaCompCandidate(
            listing_key=p.listing_key,
            status=category,
            photo_url=p.img_src,
            photos=[url for url in (p.photos or []) if isinstance(url, str)],
            street_address=p.street_address,
            city=p.city,
            state=p.state,
            zipcode=p.zipcode,
            latitude=p.latitude,
            longitude=p.longitude,
            distance_miles=round(distance, 2) if distance is not None else None,
            sale_price=sale_price,
            list_price=list_price,
            sale_date=sale_date,
            days_on_market=raw.get('DaysOnMarket'),
            sqft=sqft,
            lot_size=p.lot_size,
            beds=p.bedrooms,
            baths=p.bathrooms,
            year_built=p.year_built,
            garage=garage,
            basement=basement,
            price_per_sqft=price_per_sqft,
            school_district=raw.get('SchoolDistrictName'),
            public_remarks=raw.get('PublicRemarks'),
            annual_taxes=raw.get('TaxAnnualAmount'),
        ))

    candidates.sort(key=lambda c: (c.distance_miles is None, c.distance_miles))
    return candidates[:60]
