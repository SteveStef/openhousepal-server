"""An agent's personal library of manually-typed-in properties, so a property entered once (e.g.
via the Forms 'Enter it manually' picker) can be re-attached elsewhere with one click instead of
retyped. Available to every active plan, not just NO_MLS - any agent may have an off-market or
not-yet-listed property to attach. Saving here is purely a personal convenience cache: it never
creates a real Open House Manager listing (Property/is_manual) or an Agent Hub recent sale
(the config.recentSales array) - the agent still adds those deliberately on their own pages.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import SavedManualProperty, User as UserModel
from app.schemas.saved_property import SavedManualPropertyCreate, SavedManualPropertyResponse
from app.services import s3_service, storage_ledger_service
from app.utils.auth import require_active_plan
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/saved-properties", tags=["saved-properties"])


@router.get("", response_model=List[SavedManualPropertyResponse])
async def list_saved_properties(
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(require_active_plan),
):
    result = await db.execute(
        select(SavedManualProperty)
        .where(SavedManualProperty.agent_id == current_user.id)
        .order_by(SavedManualProperty.created_at.desc())
    )
    return result.scalars().all()


@router.post("", response_model=SavedManualPropertyResponse)
async def create_saved_property(
    payload: SavedManualPropertyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(require_active_plan),
):
    # Same rule as Open House Manager's manual-listing photo: an image_url must be one this app
    # itself generated via the S3 upload endpoint, never an arbitrary URL or base64 blob smuggled
    # into the JSON body.
    if payload.image_url and not payload.image_url.startswith(f"https://{s3_service.PUBLIC_URL_HOST}/"):
        raise HTTPException(status_code=400, detail="Photo must be uploaded first.")

    entry = SavedManualProperty(agent_id=current_user.id, **payload.model_dump())
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_property(
    property_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(require_active_plan),
):
    result = await db.execute(
        select(SavedManualProperty).where(
            SavedManualProperty.id == property_id,
            SavedManualProperty.agent_id == current_user.id,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Saved property not found")

    photo_url = entry.image_url
    await db.execute(delete(SavedManualProperty).where(SavedManualProperty.id == property_id))
    await db.commit()

    # Best-effort - a saved property's photo isn't shared with anything else once removed, but a
    # failed S3 delete shouldn't block the user from removing the entry itself.
    try:
        s3_service.delete_by_url(photo_url)
        await storage_ledger_service.release(db, photo_url)
    except Exception as e:
        logger.warning("saved property photo cleanup failed", extra={"error": str(e)})
