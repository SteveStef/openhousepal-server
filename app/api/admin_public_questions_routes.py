"""Admin-side inbox for signed-out visitors' "Ask Us a Question" submissions - a plain read/mark-
read list, not a reply thread (there's no account to notify back). Kept as its own admin tab,
separate from agents' own SupportTicket inbox in admin_support_routes.py.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import PublicQuestion
from app.schemas.public_question import PublicQuestionResponse, PublicQuestionUnreadCountResponse
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/public-questions", tags=["admin", "public-questions"])


@router.get("", response_model=List[PublicQuestionResponse])
async def list_public_questions(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin_user),
):
    result = await db.execute(select(PublicQuestion).order_by(PublicQuestion.created_at.desc()))
    return result.scalars().all()


@router.get("/unread-count", response_model=PublicQuestionUnreadCountResponse)
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin_user),
):
    result = await db.execute(select(func.count()).select_from(PublicQuestion).where(PublicQuestion.is_read.is_(False)))
    return {"unread_count": result.scalar_one()}


@router.patch("/{question_id}/read", response_model=PublicQuestionResponse)
async def mark_read(
    question_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin_user),
):
    result = await db.execute(select(PublicQuestion).where(PublicQuestion.id == question_id))
    question = result.scalar_one_or_none()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    question.is_read = True
    await db.commit()
    await db.refresh(question)
    return question
