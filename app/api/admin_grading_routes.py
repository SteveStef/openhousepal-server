"""Admin -> Tools -> Grading (owner, 11/01): every blog and CMA any agent makes, newest first, with
who made it and when - so the admin can open it, save it as a PDF for an outside grader, then
mark it graded and write down the grade it got. The PDFs are drawn in the browser (same
documents the agent's own Save PDF uses); this file only lists, loads and stores the grades.
Grades live in admin_grades and are never shown to agents.
"""
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import AdminGrade, AgentHub, Blog, CmaReport, User
from app.services import cma_service
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/grading", tags=["admin"])

Kind = Literal["blog", "cma"]


def _agent(u: Optional[User]) -> dict:
    if not u:
        return {"id": None, "name": "Deleted agent", "email": None, "brokerage": None}
    name = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email
    return {"id": u.id, "name": name, "email": u.email, "brokerage": u.brokerage}


def _grade(g: Optional[AdminGrade]) -> dict:
    return {
        "graded": bool(g and g.graded),
        "grade": g.grade if g else None,
        "notes": g.notes if g else None,
        "graded_at": g.graded_at if g else None,
    }


def _cma_title(r: CmaReport) -> str:
    s = r.subject or {}
    street = (s.get("street_address") or "").strip()
    place = ", ".join(x for x in [(s.get("city") or "").strip(), (s.get("state") or "").strip()] if x)
    return ", ".join(x for x in [street, place] if x) or "CMA (no address yet)"


@router.get("/items")
async def list_items(
    kind: Optional[Kind] = None,
    q: Optional[str] = Query(None, max_length=120),
    limit: int = Query(150, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin_user),
):
    """Newest first. Blogs with no content yet (an unfinished AI interview) and the CMA accuracy
    test's own reports are left out - only things an agent actually made."""
    rows: list[dict] = []
    needle = f"%{q.strip()}%" if q and q.strip() else None

    if kind in (None, "blog"):
        stmt = (
            select(Blog, User)
            .join(User, User.id == Blog.owner_id, isouter=True)
            .where(func.jsonb_array_length(Blog.blocks) > 0)
            .order_by(Blog.created_at.desc())
            .limit(limit)
        )
        if needle:
            stmt = stmt.where(Blog.title.ilike(needle) | User.email.ilike(needle) | User.first_name.ilike(needle) | User.last_name.ilike(needle))
        blogs = (await db.execute(stmt)).all()
        hubs = {}
        owner_ids = {b.owner_id for b, _ in blogs}
        if owner_ids:
            hubs = {h.owner_id: h.slug for h in (await db.execute(select(AgentHub).where(AgentHub.owner_id.in_(owner_ids)))).scalars()}
        for b, u in blogs:
            review = b.ai_review if isinstance(b.ai_review, dict) else {}
            live = b.status == "PUBLISHED" and b.agent_hub_visible and hubs.get(b.owner_id)
            rows.append({
                "kind": "blog", "id": b.id, "title": b.title or "Untitled article", "subtitle": b.subtitle,
                "status": b.status, "created_at": b.created_at, "updated_at": b.updated_at,
                "agent": _agent(u),
                "ai_made": bool(review.get("run_finished_at") or review.get("status") == "done"),
                "cost_usd": review.get("run_cost_usd"),
                "live_path": f"/{hubs[b.owner_id]}/blogs/{b.slug}" if live else None,
            })

    if kind in (None, "cma"):
        stmt = (
            select(CmaReport, User)
            .join(User, User.id == CmaReport.owner_id, isouter=True)
            .where(CmaReport.is_backtest.is_(False), func.jsonb_array_length(CmaReport.comparables) > 0)
            .order_by(CmaReport.created_at.desc())
            .limit(limit)
        )
        if needle:
            stmt = stmt.where(
                CmaReport.subject["street_address"].astext.ilike(needle) | CmaReport.client_name.ilike(needle)
                | User.email.ilike(needle) | User.first_name.ilike(needle) | User.last_name.ilike(needle)
            )
        for r, u in (await db.execute(stmt)).all():
            ai = r.ai_analysis if isinstance(r.ai_analysis, dict) else {}
            rows.append({
                "kind": "cma", "id": r.id, "title": _cma_title(r),
                "subtitle": (f"For {r.client_name} · " if r.client_name else "") + ("Buyer CMA" if r.mode == "buyer" else "Listing CMA"),
                "status": r.status, "created_at": r.created_at, "updated_at": r.updated_at,
                "agent": _agent(u),
                "ai_made": bool(ai),
                "cost_usd": ai.get("cost_usd"),
                "live_path": f"/cma/{r.share_slug}" if r.status == "SHARED" and r.share_slug else None,
            })

    rows.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    rows = rows[:limit]
    if rows:
        grades = (await db.execute(select(AdminGrade).where(AdminGrade.item_id.in_([x["id"] for x in rows])))).scalars().all()
        by_key = {(g.item_type, g.item_id): g for g in grades}
        for x in rows:
            x.update(_grade(by_key.get((x["kind"], x["id"]))))
    return {"items": rows}


@router.get("/blog/{blog_id}")
async def get_blog(blog_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """The whole article (any status) + who wrote it - for the PDF."""
    row = (await db.execute(select(Blog, User).join(User, User.id == Blog.owner_id, isouter=True).where(Blog.id == blog_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="That article no longer exists.")
    b, u = row
    return {
        "id": b.id, "title": b.title, "subtitle": b.subtitle, "category": b.category, "status": b.status,
        "cover_image_url": b.cover_image_url, "blocks": b.blocks or [], "meta_description": b.meta_description,
        "created_at": b.created_at, "published_at": b.published_at, "agent": _agent(u),
    }


@router.get("/cma/{report_id}")
async def get_cma(report_id: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    """The client view of the CMA (shared or not) - for the same PDF the agent saves."""
    report = (await db.execute(select(CmaReport).where(CmaReport.id == report_id))).scalar_one_or_none()
    if not report or report.is_backtest:
        raise HTTPException(status_code=404, detail="That CMA no longer exists.")
    owner = (await db.execute(select(User).where(User.id == report.owner_id))).scalar_one_or_none()
    payload = await cma_service.build_report_payload(db, report, owner, map_ref=report.id)
    return {**payload.model_dump(mode="json"), "id": report.id, "status": report.status, "created_at": report.created_at}


class GradeIn(BaseModel):
    graded: bool = False
    grade: Optional[str] = Field(None, max_length=40)
    notes: Optional[str] = Field(None, max_length=4000)


@router.put("/{kind}/{item_id}")
async def save_grade(kind: Kind, item_id: str, body: GradeIn, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin_user)):
    model = Blog if kind == "blog" else CmaReport
    if not (await db.execute(select(model.id).where(model.id == item_id))).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="That item no longer exists.")
    g = (await db.execute(select(AdminGrade).where(AdminGrade.item_type == kind, AdminGrade.item_id == item_id))).scalar_one_or_none()
    if not g:
        g = AdminGrade(item_type=kind, item_id=item_id)
        db.add(g)
    was = bool(g.graded)
    g.graded = body.graded
    g.grade = (body.grade or "").strip() or None
    g.notes = (body.notes or "").strip() or None
    # The day it was first marked graded; un-ticking clears it.
    if body.graded and not was:
        g.graded_at = datetime.now(timezone.utc)
    elif not body.graded:
        g.graded_at = None
    g.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return _grade(g)
