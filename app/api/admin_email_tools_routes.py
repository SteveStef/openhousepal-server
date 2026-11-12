"""Admin tools (owner, 11/11): unblock a bounced email address, and "Clean" the Failures list -
cleaned items move to a Past section (nothing is deleted)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin", tags=["admin"])

FAILURES_CLEANED_KEY = "admin_failures_cleaned_at"


class EmailBody(BaseModel):
    email: str


@router.get("/email-tools/check")
async def check_email(email: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    from app.services import email_unblock_service
    return await email_unblock_service.check(db, email)


@router.post("/email-tools/unblock")
async def unblock_email(body: EmailBody, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    from app.services import email_unblock_service
    out = await email_unblock_service.unblock(db, body.email)
    if not out.get("unblocked"):
        raise HTTPException(status_code=400, detail=out.get("why") or "Couldn't unblock that address")
    return out


@router.get("/failures/cleaned")
async def failures_cleaned(admin: User = Depends(require_admin_user)):
    from app.services import app_cache
    return {"cleaned_at": await app_cache.load(FAILURES_CLEANED_KEY)}


@router.post("/failures/clean")
async def clean_failures(admin: User = Depends(require_admin_user)):
    """Everything up to now moves to Past - the rows themselves stay."""
    from app.services import app_cache
    at = datetime.now(timezone.utc).isoformat()
    await app_cache.save(FAILURES_CLEANED_KEY, at)
    return {"cleaned_at": at}
