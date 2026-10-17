"""Client email tracking (public pixel + link redirect), the agent's Emails page, and Mailgun's
bounce / spam-complaint webhook - see client_email_service."""
import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import ClientEmail, Lead, User
from app.services import client_email_service as ce
from app.services.agent_email_service import app_url
from app.utils.auth import get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["client-emails"])

_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, private", "Pragma": "no-cache"}


# ---------------------------------------------------------------- public tracking

@router.get("/e/o/{track_id}.gif")
async def track_open(track_id: str, db: AsyncSession = Depends(get_db)):
    try:
        await ce.record_open(db, track_id[:64])
    except Exception:  # noqa: BLE001 - the image always loads
        logger.warning("open tracking failed", exc_info=True)
    return Response(content=_GIF, media_type="image/gif", headers=_NO_CACHE)


@router.get("/e/c/{track_id}/{key}")
async def track_click(track_id: str, key: str, db: AsyncSession = Depends(get_db)):
    url = None
    try:
        url = await ce.record_click(db, track_id[:64], key[:64])
    except Exception:  # noqa: BLE001 - the link always works
        logger.warning("click tracking failed", exc_info=True)
    # Only ever an address we stored when sending; anything else goes to the home page.
    if not url or not url.startswith(("http://", "https://")):
        url = app_url()
    return RedirectResponse(url, status_code=302, headers=_NO_CACHE)


@router.post("/e/v/{track_id}")
async def track_visit(track_id: str, db: AsyncSession = Depends(get_db)):
    """The Showcase page was opened from a personal email's own link (?ce=<id>) - no redirect hop."""
    try:
        await ce.record_visit(db, track_id[:64])
    except Exception:  # noqa: BLE001
        logger.warning("visit tracking failed", exc_info=True)
    return {"ok": True}


# ---------------------------------------------------------------- Mailgun bounces / complaints

@router.post("/webhooks/mailgun-events")
async def mailgun_events(request: Request, db: AsyncSession = Depends(get_db)):
    """Mailgun -> Sending -> Webhooks: "Permanent failure" and "Spam complaints". Either one means
    never email that address again (it doesn't exist, or they called it spam) - the fastest way to
    lose inbox placement is to keep sending to those."""
    from app.api.webhook_routes import _verify_mailgun_inbound
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Expected JSON")
    if not _verify_mailgun_inbound(dict(body.get("signature") or {})):
        raise HTTPException(status_code=403, detail="Bad signature")
    ev = body.get("event-data") or {}
    kind = ev.get("event")
    if kind == "failed" and ev.get("severity") != "permanent":
        return {"ok": True, "ignored": "temporary"}
    if kind not in ("failed", "complained"):
        return {"ok": True, "ignored": kind}
    recipient = ce._norm(ev.get("recipient"))
    if not recipient:
        return {"ok": True}
    from app.services.blacklist_service import BlacklistService
    await BlacklistService.blacklist_email(db, recipient)
    track_id = ((ev.get("user-variables") or {}).get("ce"))
    owner_id = None
    if track_id:
        rec = await db.get(ClientEmail, str(track_id)[:64])
        if rec is not None:
            owner_id = rec.owner_id
            if kind == "failed":
                rec.status = "failed"
            await db.commit()
    if owner_id:
        lead = await ce._lead(db, owner_id, recipient)
        if lead is not None:
            from app.services.leads_service import log_activity
            await log_activity(db, owner_id=owner_id, email=recipient, lead=lead, source_type="email_paused",
                               summary=("Email address bounced - no more emails will be sent to it" if kind == "failed"
                                        else "Marked your email as spam - no more emails will be sent to them"),
                               raw_snapshot={"to": "off", "mailgun": kind})
    logger.info("mailgun event: address blocked", extra={"event": kind, "recipient_domain": recipient.split("@")[-1]})
    return {"ok": True}


# ---------------------------------------------------------------- the agent's Emails page

async def _who(db: AsyncSession, user: User, emails) -> dict:
    """email -> {"id": lead id or None, "name": ...}. Their lead first; someone who isn't a lead
    (yet) still has the name they gave - open house sign-in, their Showcase, a tour request - and
    the agent's own address reads as them."""
    from app.models.database import Collection, OpenHouseEvent, OpenHouseVisitor, PropertyTour
    emails = sorted({(e or "").strip().lower() for e in emails if e})
    if not emails:
        return {}
    out: dict = {}
    for l in (await db.execute(select(Lead).where(Lead.owner_id == user.id, func.lower(Lead.email).in_(emails)))).scalars().all():
        out[(l.email or "").lower()] = {"id": l.id, "name": l.name}

    def give(email, name):
        email, name = (email or "").strip().lower(), (name or "").strip()
        if not name or "@" in name:
            return
        row = out.setdefault(email, {"id": None, "name": None})
        if not row.get("name"):
            row["name"] = name
    missing = [e for e in emails if not (out.get(e) or {}).get("name")]
    if missing:
        for email, name in (await db.execute(select(OpenHouseVisitor.email, OpenHouseVisitor.full_name)
                .join(OpenHouseEvent, OpenHouseEvent.id == OpenHouseVisitor.open_house_event_id)
                .where(OpenHouseEvent.agent_id == user.id, func.lower(OpenHouseVisitor.email).in_(missing))
                .order_by(OpenHouseVisitor.created_at.desc()))).all():
            give(email, name)
        for email, name in (await db.execute(select(Collection.visitor_email, Collection.visitor_name)
                .where(Collection.owner_id == user.id, func.lower(Collection.visitor_email).in_(missing)))).all():
            give(email, name)
        for email, name in (await db.execute(select(PropertyTour.visitor_email, PropertyTour.visitor_name)
                .join(Collection, Collection.id == PropertyTour.collection_id)
                .where(Collection.owner_id == user.id, func.lower(PropertyTour.visitor_email).in_(missing)))).all():
            give(email, name)
    me = (user.email or "").lower()
    if me in emails:
        mine = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x) or getattr(user, "full_name", None) or ""
        out.setdefault(me, {"id": None, "name": None})
        out[me]["name"] = out[me].get("name") or (f"{mine} (you)" if mine else "You")
    return out


@router.get("/client-emails/people")
async def email_people(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_active_user)):
    people = await ce.people(db, user.id)
    leads = await _who(db, user, people)
    out = []
    for email, p in people.items():
        lead = leads.get(email) or {}
        out.append({**p, "lead_id": lead.get("id"), "name": lead.get("name")})
    out.sort(key=lambda p: p.get("last_sent_at") or "", reverse=True)
    totals = {
        "people": len(out), "daily": sum(1 for p in out if p["mode"] == "daily"),
        "weekly": sum(1 for p in out if p["mode"] == "weekly"), "off": sum(1 for p in out if p["mode"] == "off"),
    }
    return {"people": out, "totals": totals, "rules": {"strike_days": ce.STRIKE_DAYS, "weekly_strikes": ce.WEEKLY_STRIKES, "max_auto_per_day": ce.MAX_AUTO_PER_DAY}}


@router.get("/client-emails")
async def email_log(email: Optional[str] = None, q: Optional[str] = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_active_user)):
    stmt = select(ClientEmail).where(ClientEmail.owner_id == user.id, ClientEmail.status.in_(("sent", "failed")))
    if email:
        stmt = stmt.where(ClientEmail.recipient_email == ce._norm(email))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(ClientEmail.recipient_email.like(like), func.lower(ClientEmail.subject).like(like)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await db.execute(stmt.order_by(ClientEmail.sent_at.desc().nullslast(), ClientEmail.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    emails = {r.recipient_email for r in rows}
    leads = await _who(db, user, emails)
    since = func.now() - func.make_interval(0, 0, 0, 30)
    stats = (await db.execute(select(func.count(), func.count(ClientEmail.opened_at), func.count(ClientEmail.clicked_at)).where(
        ClientEmail.owner_id == user.id, ClientEmail.status == "sent", ClientEmail.sent_at >= since))).one()
    return {
        "emails": [{**ce.email_out(r), "lead_id": (leads.get(r.recipient_email) or {}).get("id"), "name": (leads.get(r.recipient_email) or {}).get("name")} for r in rows],
        "total": total,
        "stats_30d": {"sent": stats[0], "opened": stats[1], "clicked": stats[2],
                      "open_rate": round(100 * stats[1] / stats[0]) if stats[0] else None,
                      "click_rate": round(100 * stats[2] / stats[0]) if stats[0] else None},
    }


class PersonSwitch(BaseModel):
    email: str
    on: bool


@router.put("/client-emails/people")
async def switch_person(body: PersonSwitch, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_active_user)):
    email = ce._norm(body.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email address needed")
    known = (await db.execute(select(func.count()).select_from(ClientEmail).where(ClientEmail.owner_id == user.id, ClientEmail.recipient_email == email))).scalar() \
        or (await db.execute(select(func.count()).select_from(Lead).where(Lead.owner_id == user.id, Lead.email == email))).scalar()
    if not known:
        raise HTTPException(status_code=404, detail="Not one of your contacts")
    return await ce.set_manual(db, user.id, email, body.on)
