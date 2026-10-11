import logging
from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import DesignProject, User
from app.schemas.design import (
    DesignProjectCreate,
    DesignProjectResponse,
    DesignProjectUpdate,
    DesignStatusUpdate,
)
from app.utils.auth import create_access_token, get_current_active_user, require_studio_design_access, luxe_theme_allowed
from app.services.design_export_service import PNG_DIMENSIONS_PX, PRINT_DIMENSIONS_IN, render_design_pdf, render_design_png

router = APIRouter(prefix="/studio/designs", tags=["design"])
logger = logging.getLogger(__name__)


async def get_owned_design(db: AsyncSession, design_id: str, owner_id: str) -> DesignProject:
    result = await db.execute(select(DesignProject).where(DesignProject.id == design_id, DesignProject.owner_id == owner_id))
    design = result.scalar_one_or_none()
    if not design:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Design not found")
    return design


async def _check_campaign(db: AsyncSession, campaign_id: Optional[str], owner_id: str) -> None:
    """A design's tracked QR may only point at one of this agent's own Form Campaigns."""
    if not campaign_id:
        return
    from app.models.database import FormCampaign
    owned = (await db.execute(select(FormCampaign.id).where(FormCampaign.id == campaign_id, FormCampaign.owner_id == owner_id))).scalar_one_or_none()
    if not owned:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")


@router.get("/me", response_model=List[DesignProjectResponse])
async def get_my_designs(
    status_filter: Optional[str] = Query(None, alias="status"),
    design_type: Optional[str] = Query(None),
    situation: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    query = select(DesignProject).where(DesignProject.owner_id == current_user.id)
    if status_filter:
        query = query.where(DesignProject.status == status_filter.upper())
    if design_type:
        query = query.where(DesignProject.design_type == design_type)
    if situation:
        query = query.where(DesignProject.situation == situation)
    if campaign_id:
        query = query.where(DesignProject.campaign_id == campaign_id)
    # Newest first - a design you just made always lands at the top, however old ones get edited.
    result = await db.execute(query.order_by(DesignProject.created_at.desc()))
    return result.scalars().all()


def _uses_luxe(template_id: Optional[str], color_mode: Optional[str]) -> bool:
    return str(template_id or "").endswith("__luxe") or color_mode == "luxe"


def _guard_luxe(user: User, template_id: Optional[str], color_mode: Optional[str]) -> None:
    """The Luxe layout and Luxe color theme are IDX only (other plans can preview them in the
    wizard but not make them)."""
    if _uses_luxe(template_id, color_mode) and not luxe_theme_allowed(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Studio Luxe is part of the IDX plan.")


@router.post("", response_model=DesignProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_design(
    payload: DesignProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    _guard_luxe(current_user, payload.template_id, payload.color_mode)
    await _check_campaign(db, payload.campaign_id, current_user.id)
    design = DesignProject(
        owner_id=current_user.id,
        design_type=payload.design_type,
        style_id=payload.style_id,
        template_id=payload.template_id,
        situation=payload.situation,
        color_mode=payload.color_mode,
        name=payload.name.strip() or "Untitled Design",
        linked_property=payload.linked_property.model_dump() if payload.linked_property else None,
        blocks=[block.model_dump() for block in payload.blocks] if payload.blocks else [],
        branding_mode=payload.branding_mode,
        campaign_id=payload.campaign_id,
    )
    db.add(design)
    await db.commit()
    await db.refresh(design)
    return design


@router.get("/{design_id}", response_model=DesignProjectResponse)
async def get_design(
    design_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    return await get_owned_design(db, design_id, current_user.id)


@router.put("/{design_id}", response_model=DesignProjectResponse)
async def update_design(
    design_id: str,
    payload: DesignProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    design = await get_owned_design(db, design_id, current_user.id)
    data = payload.model_dump(exclude_unset=True)
    if ("template_id" in data and _uses_luxe(data.get("template_id"), None)) or data.get("color_mode") == "luxe":
        _guard_luxe(current_user, data.get("template_id"), data.get("color_mode"))
    if data.get("campaign_id"):
        await _check_campaign(db, data["campaign_id"], current_user.id)
    for field, value in data.items():
        setattr(design, field, value)
    if design.status == "EXPORTED" or design.status == "READY":
        design.status = "DRAFT"
    await db.commit()
    await db.refresh(design)
    return design


@router.patch("/{design_id}/status", response_model=DesignProjectResponse)
async def update_design_status(
    design_id: str,
    payload: DesignStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    design = await get_owned_design(db, design_id, current_user.id)
    design.status = payload.status
    await db.commit()
    await db.refresh(design)
    return design


@router.post("/{design_id}/duplicate", response_model=DesignProjectResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_design(
    design_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    original = await get_owned_design(db, design_id, current_user.id)
    copy = DesignProject(
        owner_id=current_user.id,
        design_type=original.design_type,
        style_id=original.style_id,
        template_id=original.template_id,
        situation=original.situation,
        color_mode=original.color_mode,
        name=f"{original.name} (Copy)",
        status="DRAFT",
        linked_property=original.linked_property,
        blocks=original.blocks,
        branding_mode=original.branding_mode,
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return copy


@router.delete("/{design_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_design(
    design_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    design = await get_owned_design(db, design_id, current_user.id)
    await db.delete(design)
    await db.commit()


@router.post("/{design_id}/export-pdf")
async def export_design_pdf(
    design_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Real, physically-sized print PDF - not a screenshot. Launches a headless browser against
    this design's own /print route (the real DesignCanvas component) so the export can never
    drift from what the editor shows. Only offered for print formats (see PRINT_DIMENSIONS_IN) -
    social/digital formats keep the existing client-side PNG export, which is what they actually
    need (a screen-resolution image, not a paginated print document)."""
    design = await get_owned_design(db, design_id, current_user.id)
    if design.design_type not in PRINT_DIMENSIONS_IN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This format doesn't support PDF export.")

    # A short-lived, single-purpose token - the headless browser has no access to the agent's own
    # session (no cookies/localStorage), so it authenticates the /print route's data fetches with
    # this instead. 2 minutes is comfortably more than one export ever needs.
    export_token = create_access_token({"sub": current_user.id}, expires_delta=timedelta(minutes=2))

    try:
        pdf_bytes = await render_design_pdf(design.design_type, export_token, design.id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    if design.status == "DRAFT":
        design.status = "EXPORTED"
        await db.commit()

    filename = "".join(c for c in design.name if c.isalnum() or c in " -_").strip() or "design"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )


@router.post("/{design_id}/export-png")
async def export_design_png(
    design_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_studio_design_access),
):
    """Full-resolution PNG at the format's exact publishing size (1080x1080 Instagram, 300 DPI
    print...). Rendered by the same headless browser + /print route as the PDF export, so it is
    pixel-identical to the editor. The editor falls back to a client-side capture if this fails."""
    design = await get_owned_design(db, design_id, current_user.id)
    if design.design_type not in PNG_DIMENSIONS_PX:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This format doesn't support PNG export.")

    export_token = create_access_token({"sub": current_user.id}, expires_delta=timedelta(minutes=2))
    try:
        png_bytes = await render_design_png(design.design_type, export_token, design.id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    if design.status == "DRAFT":
        design.status = "EXPORTED"
        await db.commit()

    filename = "".join(c for c in design.name if c.isalnum() or c in " -_").strip() or "design"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}.png"'},
    )
