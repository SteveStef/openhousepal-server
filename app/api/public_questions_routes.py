"""Public "Ask Us a Question" submission - a signed-out homepage visitor's one-off email +
message, with no account and no reply thread (see PublicQuestion for why this differs from
SupportTicket). The admin-side inbox lives in admin_public_questions_routes.py.
"""
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.database import get_db
from app.models.database import PublicQuestion
from app.schemas.public_question import PublicQuestionCreate
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/public-questions", tags=["public-questions"])


@router.post("", status_code=201)
async def submit_public_question(
    payload: PublicQuestionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    email = payload.email.strip()
    message = payload.message.strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if not message:
        raise HTTPException(status_code=400, detail="Please enter a message.")
    if len(message) > 5000:
        raise HTTPException(status_code=400, detail="Message is too long.")

    question = PublicQuestion(email=email, message=message)
    db.add(question)
    await db.commit()
    return {"success": True}
