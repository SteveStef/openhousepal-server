"""Invites (Forms & Invites -> Invites) - the agent's side under /invites and the guest's public
page's endpoints under /invites/public/{code}. See app/services/invites_service.py."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import EventInvite, EventInviteRecipient, EventInviteRsvp, User
from app.schemas.invites import InvitePublicRequest, InviteSave, InviteSendRequest, InviteStatusUpdate, RsvpSubmit
from app.services import invites_service as svc
from app.utils.auth import owner_has_access, require_active_plan

logger = get_logger(__name__)

router = APIRouter(prefix="/invites", tags=["invites"])


async def _own(db: AsyncSession, user: User, invite_id: str) -> EventInvite:
    inv = await db.get(EventInvite, invite_id)
    if inv is None or inv.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Invite not found")
    return inv


async def _out(db: AsyncSession, user: User, inv: EventInvite) -> dict:
    base = await svc.share_base(db, user)
    counts = (await svc.counts_for(db, [inv.id])).get(inv.id)
    return svc.invite_out(inv, share_url=f"{base}/i/{inv.code}", counts=counts)


# ------------------------------------------------------------------------------------- agent side

@router.get("/presets")
async def get_presets(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    """The four invite types with their pre-made questions, plus the agent's own colour and the
    footer every invite shows (photo, name, service area, brokerage - the same profile the Agent
    Hub and the signature use, owner 10/24 round 5)."""
    from app.services.website import content_adapters
    profile = await content_adapters.get_agent_profile(db, current_user)
    return {"presets": svc.presets_out(), "brand": await svc.brand_defaults(db, current_user),
            "share_base": await svc.share_base(db, current_user),
            "host": {"name": profile.get("name") or "", "brokerage": profile.get("brokerage") or "",
                     "market": profile.get("market") or "", "photo_url": profile.get("photo_url")}}


@router.get("/scheduled")
async def scheduled_for_home(listing_key: Optional[str] = Query(default=None, max_length=64), address: Optional[str] = Query(default=None, max_length=300),
                             db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    """The date step's first suggestions: this home's open houses already on the calendar (the
    agent's own in OpenHousePal, then Bright's)."""
    return await svc.scheduled_slots(db, current_user, listing_key=listing_key, address=address)


async def _check_open_house(db: AsyncSession, user: User, payload: InviteSave) -> Optional[str]:
    oid = (payload.open_house_event_id or "").strip() or None
    if not oid:
        return None
    from app.models.database import OpenHouseEvent
    oh = await db.get(OpenHouseEvent, oid)
    return oid if oh is not None and oh.agent_id == user.id and not oh.is_deleted else None


@router.get("")
async def list_invites(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    rows = (await db.execute(
        select(EventInvite).where(EventInvite.owner_id == current_user.id).order_by(EventInvite.created_at.desc())
    )).scalars().all()
    base = await svc.share_base(db, current_user)
    counts = await svc.counts_for(db, [r.id for r in rows])
    return [svc.invite_out(r, share_url=f"{base}/i/{r.code}", counts=counts.get(r.id)) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_invite(payload: InviteSave, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    count = len((await db.execute(select(EventInvite.id).where(EventInvite.owner_id == current_user.id))).all())
    if count >= 300:
        raise HTTPException(status_code=422, detail="You have 300 invites - delete a few old ones to make a new one.")
    brand = await svc.brand_defaults(db, current_user)
    inv = EventInvite(owner_id=current_user.id, code=await svc.unique_code(db))
    svc.apply_save(inv, payload, brand["accent_color"])
    inv.open_house_event_id = await _check_open_house(db, current_user, payload)
    db.add(inv)
    await db.commit()
    await db.refresh(inv)
    return await _out(db, current_user, inv)


@router.get("/{invite_id}")
async def get_invite(invite_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    inv = await _own(db, current_user, invite_id)
    rsvps = (await db.execute(
        select(EventInviteRsvp).where(EventInviteRsvp.invite_id == inv.id).order_by(EventInviteRsvp.created_at.desc())
    )).scalars().all()
    recips = (await db.execute(select(EventInviteRecipient).where(EventInviteRecipient.invite_id == inv.id))).scalars().all()
    answered = {(r.email or "").lower() for r in rsvps if r.email}
    out = await _out(db, current_user, inv)
    out["rsvps"] = [svc.rsvp_out(r) for r in rsvps]
    out["recipients"] = {"total": len(recips), "waiting": sum(1 for r in recips if r.email not in answered)}
    return out


@router.put("/{invite_id}")
async def update_invite(invite_id: str, payload: InviteSave, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    inv = await _own(db, current_user, invite_id)
    svc.apply_save(inv, payload, inv.accent_color or svc.DEFAULT_ACCENT)
    inv.open_house_event_id = await _check_open_house(db, current_user, payload)
    db.add(inv)
    await db.commit()
    await db.refresh(inv)
    return await _out(db, current_user, inv)


@router.post("/{invite_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_invite(invite_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    src = await _own(db, current_user, invite_id)
    copy = EventInvite(owner_id=current_user.id, code=await svc.unique_code(db))
    for col in ("invite_type", "audience", "message", "event_date", "start_time", "end_time", "place_name", "address",
                "linked_property", "cover_image_url", "design", "accent_color", "questions",
                "extra_dates", "motion", "luxe_metal", "cover_position", "open_house_event_id", "subtitle"):
        setattr(copy, col, getattr(src, col))
    copy.title = f"{src.title} (copy)"[:140]
    copy.status = "live"
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return await _out(db, current_user, copy)


@router.post("/{invite_id}/status")
async def set_status(invite_id: str, payload: InviteStatusUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    inv = await _own(db, current_user, invite_id)
    inv.status = payload.status
    db.add(inv)
    await db.commit()
    await db.refresh(inv)
    return await _out(db, current_user, inv)


@router.post("/{invite_id}/seen", status_code=status.HTTP_204_NO_CONTENT)
async def mark_seen(invite_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    inv = await _own(db, current_user, invite_id)
    await db.execute(update(EventInviteRsvp).where(EventInviteRsvp.invite_id == inv.id, EventInviteRsvp.is_seen.is_(False)).values(is_seen=True))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invite(invite_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    """The invite, its RSVPs and its email list go; Leads made from RSVPs stay (their history
    keeps the RSVP line)."""
    inv = await _own(db, current_user, invite_id)
    await db.delete(inv)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{invite_id}/send")
async def send_invite(invite_id: str, payload: InviteSendRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    inv = await _own(db, current_user, invite_id)
    if inv.status != "live":
        raise HTTPException(status_code=422, detail="RSVPs are closed on this invite - open them again to send it.")
    base = await svc.share_base(db, current_user)
    return await svc.send_invitations(db, current_user, inv, [(r.email, r.name) for r in payload.recipients], f"{base}/i/{inv.code}")


@router.post("/{invite_id}/resend")
async def resend_invite(invite_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_active_plan)):
    """A friendly reminder to everyone it was emailed to who hasn't answered yet."""
    inv = await _own(db, current_user, invite_id)
    if inv.status != "live":
        raise HTTPException(status_code=422, detail="RSVPs are closed on this invite - open them again to send a reminder.")
    waiting = await svc.non_responders(db, inv)
    if not waiting:
        return {"sent": 0, "sent_to": [], "skipped": []}
    base = await svc.share_base(db, current_user)
    return await svc.send_invitations(db, current_user, inv, waiting, f"{base}/i/{inv.code}", resend=True)


# ------------------------------------------------------------------------------------- public side

async def _public(db: AsyncSession, code: str) -> tuple[EventInvite, User]:
    inv = (await db.execute(select(EventInvite).where(EventInvite.code == (code or "").strip().lower()))).scalar_one_or_none()
    owner = await db.get(User, inv.owner_id) if inv is not None else None
    if inv is None or owner is None or not owner_has_access(owner):
        raise HTTPException(status_code=404, detail="This invitation isn't available.")
    return inv, owner


@router.get("/public/{code}")
async def public_invite(code: str, host: Optional[str] = Query(default=None, max_length=255), db: AsyncSession = Depends(get_db)):
    """What the guest's invitation page shows. `host` = the domain the page was opened on - an
    agent's own IDX domain only serves that agent's invites (host_ok False -> the page goes to the
    openhousepal.com link instead)."""
    inv, owner = await _public(db, code)
    from app.services.website import content_adapters
    profile = await content_adapters.get_agent_profile(db, owner)
    host_ok = True
    if host:
        h = host.strip().lower().split(":")[0]
        from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN, resolve_hostname
        if h not in (OHP_BASE_DOMAIN, f"www.{OHP_BASE_DOMAIN}", f"app.{OHP_BASE_DOMAIN}", "localhost", "127.0.0.1"):
            site = await resolve_hostname(db, h)
            host_ok = site is not None and site.owner_id == owner.id
    base = await svc.share_base(db, owner)
    brand = await svc.brand_defaults(db, owner)
    out = svc.invite_out(inv, share_url=f"{base}/i/{inv.code}")
    out.pop("counts", None)
    out.pop("view_count", None)
    out["host_ok"] = host_ok
    out["agent"] = {
        "name": profile.get("name") or "", "title": profile.get("title") or "", "brokerage": profile.get("brokerage") or "",
        "market": profile.get("market") or "",
        "photo_url": profile.get("photo_url"), "phone": profile.get("phone") or "", "text_phone": profile.get("text_phone") or "",
        "email": profile.get("email") or "", "hub_slug": profile.get("hub_slug"), "logo_url": brand.get("logo_url"),
        "id": owner.id, "first_name": owner.first_name or (profile.get("name") or "").split(" ")[0],
    }
    # The Guest Pass's extras on the invitation (10/24): the open house's Cheat Sheet when it has one.
    out["cheat_sheet"] = await svc.cheat_sheet_of(db, inv)
    out["home_details"] = await svc.home_details_of(db, inv)
    out.pop("open_house_event_id", None)
    return out


@router.post("/public/{code}/view", status_code=status.HTTP_204_NO_CONTENT)
async def public_view(code: str, db: AsyncSession = Depends(get_db)):
    inv, _ = await _public(db, code)
    await db.execute(update(EventInvite).where(EventInvite.id == inv.id).values(view_count=EventInvite.view_count + 1))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/public/{code}/rsvp/{token}")
async def public_my_rsvp(code: str, token: str, db: AsyncSession = Depends(get_db)):
    """A returning guest's own answer (from their browser or their confirmation email's link)."""
    inv, _ = await _public(db, code)
    r = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.token == token, EventInviteRsvp.invite_id == inv.id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail="RSVP not found")
    return {**svc.rsvp_out(r, private=False), "email": r.email, "phone": r.phone, "token": r.token}


@router.get("/public/{code}/ics")
async def public_ics(code: str, days: Optional[str] = Query(default=None, max_length=200), db: AsyncSession = Depends(get_db)):
    """Apple / Outlook calendar file - every day, or just ?days=YYYY-MM-DD,YYYY-MM-DD (the ones
    this guest said they're coming to)."""
    inv, owner = await _public(db, code)
    base = await svc.share_base(db, owner)
    from app.services.agent_email_service import full_name
    only = [d for d in (svc._as_date(x) for x in (days or "").split(",") if x) if d]
    text = svc.ics_text(inv, agent_name=full_name(owner), url=f"{base}/i/{inv.code}", only=only or None)
    if not text:
        raise HTTPException(status_code=404, detail="This invitation has no date yet.")
    return Response(content=text, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="invite-{inv.code}.ics"'})


@router.post("/public/{code}/rsvp")
async def public_rsvp(code: str, payload: RsvpSubmit, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    inv, owner = await _public(db, code)
    if inv.status != "live":
        raise HTTPException(status_code=422, detail="RSVPs are closed for this event.")
    if svc.is_past(inv):
        raise HTTPException(status_code=422, detail="This event has already happened.")
    name = payload.name.strip()[:140]
    email = (payload.email or "").strip().lower()[:200] or None
    phone = (payload.phone or "").strip()[:40] or None
    if not name:
        raise HTTPException(status_code=422, detail="Add your name.")
    if email and not svc._EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    # "Can't make it" + "not interested" = "no worries" - their name is enough (10/24).
    declined_quietly = payload.response == "no" and not payload.still_interested
    if not email and not phone and not declined_quietly:
        raise HTTPException(status_code=422, detail="Add an email or a phone number so the host can reach you.")
    questions = inv.questions or []
    snapshot, given = svc.answers_snapshot(questions, payload.answers)
    if payload.response != "no":
        missing = [q["label"] for q in questions if q.get("required") and q["id"] not in given]
        if missing:
            raise HTTPException(status_code=422, detail=f"Please answer: {missing[0]}")
    else:
        snapshot = [a for a in snapshot if any(q["id"] == a["questionId"] and q.get("role") == "brokerage" for q in questions)]

    rsvp: Optional[EventInviteRsvp] = None
    if payload.token:
        rsvp = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.token == payload.token, EventInviteRsvp.invite_id == inv.id))).scalar_one_or_none()
    if rsvp is None and email:
        # The same person answering again from another device updates their RSVP, not a second one.
        rsvp = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.invite_id == inv.id, EventInviteRsvp.email == email)
                                 .order_by(EventInviteRsvp.created_at.asc()))).scalars().first()
    changed = rsvp is not None
    state = lambda r: (r.response, bool(r.still_interested), bool(r.keep_updated), tuple(r.days or []))  # noqa: E731
    previous = state(rsvp) if rsvp is not None else None
    if rsvp is None:
        rsvp = EventInviteRsvp(invite_id=inv.id, owner_id=owner.id, token=secrets.token_urlsafe(24))
    rsvp.response = payload.response
    rsvp.name = name
    rsvp.email = email
    rsvp.phone = phone
    rsvp.answers = snapshot
    rsvp.guest_count = svc.guest_count_of(questions, given, payload.response)
    rsvp.is_seen = False
    # Several days: which ones they're coming to (only real days of this invite).
    real_days = {d["date"] for d in svc.all_days(inv)}
    rsvp.days = sorted({d.isoformat() for d in payload.days if d in real_days}) if payload.response != "no" and len(real_days) > 1 else None
    rsvp.is_agent = payload.is_agent if inv.audience == "clients" else True
    rsvp.has_agent = None if rsvp.is_agent else payload.has_agent
    rsvp.still_interested = bool(payload.still_interested) if payload.response == "no" else None
    rsvp.keep_updated = bool(payload.keep_updated) if payload.keep_updated is not None else rsvp.keep_updated
    if changed:
        rsvp.updated_at = datetime.now(timezone.utc)
    db.add(rsvp)
    await db.commit()
    await db.refresh(rsvp)

    lead_id = rsvp.lead_id
    # A new RSVP, or a changed answer, goes on the lead's history (a re-save with the same answer doesn't).
    if not changed or previous != state(rsvp):
        lead_id = await svc.attach_lead(db, owner, inv, rsvp, changed=changed)
        await svc.notify_agent(db, owner, inv, rsvp, changed=changed, lead_id=lead_id)
        base = await svc.share_base(db, owner)
        await svc.email_guest_confirmation(db, owner, inv, rsvp, f"{base}/i/{inv.code}")
        if rsvp.keep_updated and email and not (rsvp.requests or {}).get("updates"):
            # "Keep me updated on this property" - the same as the page's Stay updated button.
            try:
                from app.schemas.invites import InvitePublicRequest
                await svc.public_request(db, owner, inv, InvitePublicRequest(kind="updates", token=rsvp.token), quiet=True)
            except Exception:  # noqa: BLE001 - the RSVP itself is saved
                logger.warning("invite keep-updated failed", exc_info=True)
    await db.refresh(rsvp)
    return {**svc.rsvp_out(rsvp, private=False), "email": rsvp.email, "phone": rsvp.phone, "token": rsvp.token, "updated": changed}


@router.post("/public/{code}/request")
async def public_request(code: str, payload: InvitePublicRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """The Guest Pass's one-tap asks on the invitation: seller disclosure, second tour, stay updated."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    inv, owner = await _public(db, code)
    try:
        return await svc.public_request(db, owner, inv, payload)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
