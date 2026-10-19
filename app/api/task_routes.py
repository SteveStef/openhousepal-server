"""Agent tasks / follow-ups + the private iCalendar feed.

`/tasks/*` is the authenticated CRUD the Leads page and the dashboard widget use.
`/calendar/{token}.ics` is UNauthenticated on purpose - a calendar app can't send a bearer
token, so the opaque token in the URL is the credential. Rotatable from Settings.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.database import AgentTask, CalendarConnection, CalendarEventLink, Lead, User
from app.schemas.tasks import TaskCounts, TaskCreate, TaskOut, TaskUpdate
from app.services import calendar_sync_service, tasks_service
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/tasks", tags=["tasks"])
# Mounted separately (no prefix) so the feed can live at /calendar/{token}.ics
calendar_router = APIRouter(tags=["tasks"])
logger = logging.getLogger(__name__)


def _to_out(task: AgentTask) -> TaskOut:
    data = TaskOut.model_validate(task)
    data.bucket = tasks_service.compute_bucket(task)
    # Open house follow-ups made before the context column existed carry it in their notes.
    if not data.context and task.auto_source == "open_house_signin" and task.notes:
        for line in task.notes.splitlines():
            if line.startswith("Open house: "):
                data.context = f"Signed in at your open house - {line[len('Open house: '):]}"
                break
    # Older follow-ups were saved with the MLS's capitals ("26 OLD GULPH RD") - tidy on the way out.
    if data.context:
        data.context = tasks_service._tidy_context(data.context)
    return data


# Open tasks (not-yet-done) are a naturally bounded backlog, but completed tasks accumulate
# forever - an agent a year in could have thousands. Capping the done side keeps the payload
# (and the "Show completed" grid it renders into) from growing unbounded.
DONE_TASKS_LIMIT = 50
# How far back "Show older completed" reaches.
DONE_VISIBLE_DAYS = 14


@router.get("", response_model=List[TaskOut])
async def list_tasks(
    lead_id: Optional[str] = Query(None),
    include_done: bool = Query(False),
    bucket: Optional[str] = Query(None, description="overdue|today|upcoming|someday"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # A follow-up the agent deleted from their calendar counts as done - check before listing.
    await calendar_sync_service.reconcile_deleted_followups_throttled(current_user.id)
    base = (
        select(AgentTask)
        .options(selectinload(AgentTask.lead))
        .where(AgentTask.owner_id == current_user.id)
    )
    if lead_id:
        base = base.where(AgentTask.lead_id == lead_id)

    # Same-time ties broken by when they were made (then id) - a stable order, so checking one off
    # doesn't reshuffle the rest (the Dash fills the freed spot with the next in line).
    open_stmt = base.where(AgentTask.completed_at.is_(None)).order_by(
        func.coalesce(AgentTask.due_at, AgentTask.created_at).asc(), AgentTask.created_at.asc(), AgentTask.id.asc()
    )
    rows = await db.execute(open_stmt)
    tasks = list(rows.scalars().all())
    # Open "check in (has an agent)" follow-ups made before they had a text (user, 10/19) get it
    # now - and their calendar event is redone with it.
    from app.services.tasks_service import NOTED_GUEST_SOURCE, backfill_noted_message
    if any(t.auto_source == NOTED_GUEST_SOURCE and not t.suggested_message for t in tasks):
        await backfill_noted_message(db, current_user, [t for t in tasks if t.auto_source == NOTED_GUEST_SOURCE and not t.suggested_message])

    if include_done:
        # Only recently completed ones - older history isn't useful on a to-do list and is
        # purged nightly anyway (tasks_service.purge_old_completed).
        recent = datetime.now(timezone.utc) - timedelta(days=DONE_VISIBLE_DAYS)
        done_stmt = (
            base.where(AgentTask.completed_at.isnot(None), AgentTask.completed_at >= recent)
            .order_by(AgentTask.completed_at.desc())
            .limit(DONE_TASKS_LIMIT)
        )
        done_rows = await db.execute(done_stmt)
        tasks += list(done_rows.scalars().all())

    out = [_to_out(t) for t in tasks]
    if bucket:
        out = [t for t in out if t.bucket == bucket]
    # Which of these are actually on the agent's connected calendar (so the card can say so).
    if out:
        linked = await db.execute(
            select(CalendarEventLink.source_id)
            .join(CalendarConnection, CalendarConnection.id == CalendarEventLink.connection_id)
            .where(
                CalendarConnection.owner_id == current_user.id,
                CalendarEventLink.kind == "task",
                CalendarEventLink.source_id.in_([t.id for t in out]),
            )
        )
        on_cal = set(linked.scalars().all())
        showcases = await tasks_service.lead_showcase_info(
            db, current_user, list({t.lead.id: t.lead for t in tasks if t.lead is not None}.values())
        )
        by_id = {t.id: t for t in tasks}
        comments = await tasks_service.lead_comment_details(db, current_user.id, tasks)
        for t in out:
            t.comments = comments.get(t.id, [])
            t.on_calendar = t.id in on_cal
            sc = showcases.get(t.lead_id) if t.lead_id and not t.completed_at else None
            if sc:
                t.showcase_url = sc["url"]
            # The copy button sends the whole thing: the reply with the Showcase pitch (link last)
            # where the agent's template puts it - or the {showcase} marker removed if there's none.
            t.suggested_message = tasks_service.full_message(by_id[t.id], sc, current_user)
            if not t.completed_at and by_id[t.id].auto_source:
                try:
                    t.property = await tasks_service.followup_property(db, by_id[t.id], current_user)
                    t.requested_times = await tasks_service.task_requested_times(db, by_id[t.id]) or None
                    if by_id[t.id].auto_source != "form_unfinished":  # its notes already list the answers so far
                        t.details = tasks_service.card_details(await tasks_service.request_details(db, by_id[t.id]), t.requested_times) or None
                except Exception:
                    logger.warning("property link failed for task %s", t.id, exc_info=True)
    return out


def _settings_payload(user: User) -> dict:
    return {
        "enabled": tasks_service.followups_enabled(user),
        "rules": tasks_service.rules_payload(user),
        "color": tasks_service.followup_color(user),
        "colors": [{"id": k, "name": n, "hex": h} for k, (n, h) in tasks_service.CALENDAR_COLORS.items()],
        "work_hours": {"start": tasks_service.WORK_START_HOUR, "end": tasks_service.WORK_END_HOUR},
        "alerts": tasks_service.alert_settings(user),
        "groups": [{k: g[k] for k in ("key", "label", "description")} for g in tasks_service.RULE_GROUPS],
        "alert_before_choices": list(tasks_service.ALERT_BEFORE_CHOICES),
        # Signed under every suggested message (shown, read-only, in the message editor's preview).
        "footer": tasks_service.message_footer(user),
    }


@router.get("/auto-rules")
async def get_auto_rules(current_user: User = Depends(get_current_active_user)):
    """Every situation that creates a follow-up automatically (for this agent's plan) with the
    agent's settings for each (on/off, same/next day, timed/all-day), plus their calendar colour."""
    return _settings_payload(current_user)


@router.post("/auto-rules/{key}/preview")
async def preview_calendar_alert(
    key: str,
    payload: dict,
    current_user: User = Depends(get_current_active_user),
):
    """The calendar alert this situation makes, for a sample lead (Jane Buyer), built by the same
    code as the real event - with the message the agent is editing right now (unsaved)."""
    payload = payload if isinstance(payload, dict) else {}
    return tasks_service.sample_event(current_user, key, template=payload.get("template"),
                                      pitch=payload.get("pitch"), seller=bool(payload.get("seller")))


# Outlook can't colour single events through our API - it colours the whole calendar from a fixed
# palette. Closest match to each Google colour.
_OUTLOOK_COLOR = {"5": "lightYellow", "6": "lightOrange", "11": "lightRed", "4": "lightPink", "3": "lightPink",
                  "1": "lightBlue", "9": "lightBlue", "7": "lightTeal", "2": "lightGreen", "10": "lightGreen", "8": "lightGray"}


@router.put("/auto-rules")
async def update_auto_rules(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    payload = payload if isinstance(payload, dict) else {}
    if isinstance(payload.get("rules"), dict):
        current_user.followup_rules = tasks_service.merge_rules(current_user.followup_rules, payload["rules"])
    if isinstance(payload.get("enabled"), bool):
        rules = dict(current_user.followup_rules or {})
        if payload["enabled"]:
            rules.pop("_enabled", None)
        else:
            rules["_enabled"] = False
        current_user.followup_rules = rules  # new dict so the JSONB change is saved
    recolor = False
    if isinstance(payload.get("alerts"), dict):
        cleaned = tasks_service.clean_alerts(payload["alerts"])
        if cleaned:
            before = tasks_service.alert_settings(current_user)
            rules = dict(current_user.followup_rules or {})
            rules["_alerts"] = {**before, **cleaned}
            current_user.followup_rules = rules  # new dict so the JSONB change is saved
            recolor = rules["_alerts"] != before  # re-push events so their alerts change too
    color = payload.get("color")
    if color in tasks_service.CALENDAR_COLORS and color != tasks_service.followup_color(current_user):
        current_user.followup_color = color
        recolor = True
    db.add(current_user)
    await db.commit()
    if recolor:
        # Repaint every open follow-up already on their calendar(s) in the new colour.
        ids = (await db.execute(select(AgentTask.id).where(
            AgentTask.owner_id == current_user.id, AgentTask.completed_at.is_(None), AgentTask.due_at.is_not(None),
        ))).scalars().all()
        for tid in ids:
            calendar_sync_service.sync_task_bg(current_user.id, tid)
        try:
            from app.services import microsoft_calendar_service as mscal
            conn = await calendar_sync_service.get_connection(db, current_user.id, "microsoft")
            if conn and conn.calendar_id:
                token = await calendar_sync_service._valid_access_token(db, conn)
                await mscal._req(token, "PATCH", f"/me/calendars/{conn.calendar_id}", json={"color": _OUTLOOK_COLOR.get(color, "auto")})
        except Exception:
            logger.info("outlook calendar recolor skipped", exc_info=True)
    return _settings_payload(current_user)


# --------------------------------------------------------------------------- one-tap "Mark as done"
# Linked from every follow-up's calendar event (openhousepal.com/d/<code>). No login: the code is
# the credential, and all it can do is check that ONE follow-up off or undo that.
public_router = APIRouter(prefix="/public/followups", tags=["tasks"])


async def _task_by_code(db: AsyncSession, code: str) -> AgentTask:
    task = (await db.execute(
        select(AgentTask).options(selectinload(AgentTask.lead)).where(AgentTask.done_code == code)
    )).scalar_one_or_none() if code and len(code) <= 32 else None
    if not task:
        raise HTTPException(status_code=404, detail="This follow-up link isn't active anymore.")
    return task


async def _situation(db: AsyncSession, task: AgentTask) -> list[str]:
    """The top of their contact card's notes: this follow-up's situation, e.g. "Requested a second
    visit - 12 Oak St" + "Preferred visit time: Sat, Oct 4 - Afternoon" + their Showcase comment."""
    lines = []
    when = task.created_at.astimezone(tasks_service.EASTERN) if task.created_at else None
    if task.context:
        lines.append(task.context + (f" ({when:%b} {when.day})" if when else ""))
    try:
        lines += [f"- {d}" for d in await tasks_service.request_details(db, task)]
        for c in (await tasks_service.lead_comment_details(db, task.owner_id, [task])).get(task.id, []):
            lines.append(f'- Commented on {c["home"]}: "{c["text"][:300]}"')
    except Exception:
        logger.warning("contact card situation lookup failed for task %s", task.id, exc_info=True)
    return lines


def _public_view(task: AgentTask, others: Optional[list] = None) -> dict:
    lead = task.lead
    out = {
        "title": task.title,
        "context": tasks_service._tidy_context(task.context) if task.context else task.context,
        "lead_name": lead.name if lead else None,
        # Their contact info stays on the page (done or not) - with the one-tap "save to contacts".
        "contact": {"name": lead.name, "phone": lead.phone, "email": lead.email} if lead else None,
        "done": task.completed_at is not None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
    if others is not None:
        out["others"] = others
    return out


async def _others_due(db: AsyncSession, task: AgentTask) -> list[dict]:
    """"Did you also do these?" - the agent's OTHER follow-ups that are due today or overdue, each
    with its own done code, so one tap from the calendar can clear several at once."""
    rows = (await db.execute(
        select(AgentTask).where(
            AgentTask.owner_id == task.owner_id,
            AgentTask.id != task.id,
            AgentTask.completed_at.is_(None),
            AgentTask.due_at.is_not(None),
        ).order_by(AgentTask.due_at.asc()).limit(60)
    )).scalars().all()
    out = []
    for t in rows:
        bucket = tasks_service.compute_bucket(t)
        if bucket not in ("overdue", "today"):
            continue
        out.append({
            "code": await tasks_service.ensure_done_code(db, t),
            "title": t.title,
            "context": t.context,
            "overdue": bucket == "overdue",
            "due_at": t.due_at.isoformat() if t.due_at else None,
            "all_day": t.all_day,
        })
        if len(out) >= 12:
            break
    return out


@public_router.get("/{code}")
async def public_followup(code: str, db: AsyncSession = Depends(get_db)):
    task = await _task_by_code(db, code)
    return _public_view(task, await _others_due(db, task))


@public_router.get("/{code}/contact.vcf")
async def public_followup_contact(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The "save to your contacts" link in the calendar event: a vCard the phone opens straight
    into Create New Contact - name, phone, email, and a note with where they came from, what
    they've done and the agent's own notes. Built live, so notes added since the event was made
    are included."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    task = await _task_by_code(db, code)
    if task.lead is None:
        raise HTTPException(status_code=404, detail="This follow-up isn't tied to a contact.")
    owner = await db.get(User, task.owner_id)
    profile = await tasks_service.lead_profile(db, task.lead)
    sc = (await tasks_service.lead_showcase_info(db, owner, [task.lead])).get(task.lead.id) if owner else None
    card = tasks_service.build_vcard(task.lead, profile, sc["url"] if sc else None,
                                     situation=await _situation(db, task))
    safe = "".join(ch for ch in (task.lead.name or "contact") if ch.isalnum() or ch in " -_").strip() or "contact"
    return Response(
        content=card.encode("utf-8"),
        media_type="text/vcard; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{safe}.vcf"', "Cache-Control": "no-store"},
    )


@public_router.get("/{code}/text")
async def public_followup_text(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The tap-to-text page: the guest's number and the ready-to-send message (the same text the
    calendar event shows), so the agent's own Messages app opens with it written. Opening this
    never checks anything off - only the page's button does (a POST to /done)."""
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    task = await _task_by_code(db, code)
    lead = task.lead
    if lead is None or not (lead.phone or "").strip():
        raise HTTPException(status_code=404, detail="There's no phone number on this follow-up.")
    owner = await db.get(User, task.owner_id)
    sc = None
    try:
        sc = (await tasks_service.lead_showcase_info(db, owner, [lead])).get(lead.id) if owner else None
    except Exception:
        logger.warning("showcase lookup failed for text link %s", task.id, exc_info=True)
    message = tasks_service.full_message(task, sc, owner) or ""
    fill_ins = [f for f in (tasks_service.FILL_IN_TIME, tasks_service.FILL_IN_ANSWER) if f in message]
    digits = "".join(ch for ch in lead.phone if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return {
        **_public_view(task),
        "first_name": (lead.name or "").split(" ")[0] or None,
        "phone": lead.phone,
        "sms_to": f"+{digits}" if digits else lead.phone,
        "message": message,
        "fill_ins": fill_ins,
    }


@public_router.get("/{code}/card")
async def public_followup_card(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """One follow-up in full, opened from the "Did you also do these?" list on the check-off page
    (user, 10/19 - "click that follow-up and it takes me exactly to where it has the pre-made
    message and situation"): why, the details, the ready message, who to reach - and where its
    event is on the agent's calendar (Google's exact event; the time, for the iPhone Calendar).
    Read-only like GET /{code}, so not on the shared submit limit - opening a few in a row from
    the check-off page ran into "Too many submissions"."""
    task = await _task_by_code(db, code)
    owner = await db.get(User, task.owner_id)
    lead = task.lead
    sc = None
    if lead is not None and owner is not None:
        try:
            sc = (await tasks_service.lead_showcase_info(db, owner, [lead])).get(lead.id)
        except Exception:
            logger.warning("showcase lookup failed for follow-up card %s", task.id, exc_info=True)
    message = (tasks_service.full_message(task, sc, owner) or "") if task.completed_at is None else ""
    situation = await _situation(db, task)
    google_url = None
    try:
        import base64
        row = (await db.execute(
            select(CalendarEventLink.external_event_id, CalendarConnection.calendar_id)
            .join(CalendarConnection, CalendarConnection.id == CalendarEventLink.connection_id)
            .where(CalendarConnection.owner_id == task.owner_id, CalendarConnection.provider == "google",
                   CalendarEventLink.kind == "task", CalendarEventLink.source_id == task.id)
        )).first()
        if row and row[0] and row[1]:
            eid = base64.urlsafe_b64encode(f"{row[0]} {row[1]}".encode()).decode().rstrip("=")
            google_url = f"https://www.google.com/calendar/event?eid={eid}"
    except Exception:
        logger.warning("calendar link lookup failed for follow-up card %s", task.id, exc_info=True)
    bucket = tasks_service.compute_bucket(task)
    return {
        **_public_view(task),
        "code": code,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "all_day": task.all_day,
        "overdue": bucket == "overdue",
        "details": situation[1:] if situation else [],
        "message": message,
        "fill_ins": [f for f in (tasks_service.FILL_IN_TIME, tasks_service.FILL_IN_ANSWER, tasks_service.FILL_IN_MESSAGE) if f in message],
        "google_event_url": google_url,
    }


@public_router.post("/{code}/done")
async def public_followup_done(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    task = await _task_by_code(db, code)
    if task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)
        await tasks_service.mark_lead_contacted(db, task)
        await db.commit()
        calendar_sync_service.sync_task_bg(task.owner_id, task.id)  # takes it off the calendar
    return _public_view(task, await _others_due(db, task))


@public_router.post("/{code}/undo")
async def public_followup_undo(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    task = await _task_by_code(db, code)
    if task.completed_at is not None:
        task.completed_at = None
        await db.commit()
        calendar_sync_service.sync_task_bg(task.owner_id, task.id)  # puts it back
    return _public_view(task, await _others_due(db, task))


@router.get("/counts", response_model=TaskCounts)
async def task_counts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    await calendar_sync_service.reconcile_deleted_followups_throttled(current_user.id, timeout=1.5)
    rows = await db.execute(
        select(AgentTask).where(
            AgentTask.owner_id == current_user.id,
            AgentTask.completed_at.is_(None),
        )
    )
    tasks = rows.scalars().all()
    buckets = [tasks_service.compute_bucket(t) for t in tasks]
    return TaskCounts(
        overdue=buckets.count("overdue"),
        today=buckets.count("today"),
        upcoming=buckets.count("upcoming"),
        someday=buckets.count("someday"),
        open_total=len(tasks),
    )


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(
    payload: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if not payload.title.strip():
        raise HTTPException(status_code=422, detail="Title is required")
    if payload.lead_id:
        # Only the agent's own leads - otherwise the task would show someone else's client's
        # name, email and phone back to them.
        owned = (await db.execute(select(Lead.id).where(Lead.id == payload.lead_id, Lead.owner_id == current_user.id))).first()
        if not owned:
            raise HTTPException(status_code=404, detail="Lead not found")
    task = AgentTask(
        owner_id=current_user.id,
        lead_id=payload.lead_id,
        title=payload.title.strip(),
        notes=payload.notes,
        kind=payload.kind,
        priority=payload.priority,
        due_at=payload.due_at,
        all_day=payload.all_day,
        remind_at=payload.remind_at,
        suggested_message=payload.suggested_message,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task, attribute_names=["lead"])
    calendar_sync_service.sync_task_bg(current_user.id, task.id)
    return _to_out(task)


async def _load_owned(db: AsyncSession, task_id: str, owner_id: str) -> AgentTask:
    row = await db.execute(
        select(AgentTask).options(selectinload(AgentTask.lead)).where(
            AgentTask.id == task_id, AgentTask.owner_id == owner_id
        )
    )
    task = row.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: str,
    payload: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    task = await _load_owned(db, task_id, current_user.id)
    data = payload.model_dump(exclude_unset=True)
    if "title" in data:
        if not (data["title"] or "").strip():
            raise HTTPException(status_code=422, detail="Title is required")
        data["title"] = data["title"].strip()
    completed = data.pop("completed", None)
    for k, v in data.items():
        setattr(task, k, v)
    if completed is True and task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)
        await tasks_service.mark_lead_contacted(db, task)
    elif completed is False:
        task.completed_at = None
    # a moved due date should be able to remind again
    if "due_at" in data or "remind_at" in data:
        task.reminder_sent_at = None
    await db.commit()
    await db.refresh(task, attribute_names=["lead"])
    calendar_sync_service.sync_task_bg(current_user.id, task.id)
    return _to_out(task)


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    task = await _load_owned(db, task_id, current_user.id)
    await db.delete(task)
    await db.commit()
    calendar_sync_service.unsync_task_bg(current_user.id, task_id)
    return Response(status_code=204)


def _https_outside_dev(url: str) -> str:
    # Behind the production proxy the request can arrive as plain http://; public links must be https.
    if os.getenv("DEV", "false") != "true" and url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _feed_urls(request: Request, token: str) -> dict:
    # Prefer the configured public API URL; fall back to the request's own host (keeps dev
    # working even when the backend port has moved).
    base = (os.getenv("API_URL") or _https_outside_dev(str(request.base_url))).rstrip("/")
    url = f"{base}/calendar/{token}.ics"
    return {"url": url, "webcal_url": url.replace("http://", "webcal://").replace("https://", "webcal://")}


@router.get("/calendar-feed")
async def calendar_feed_info(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    token = await tasks_service.get_or_create_feed_token(db, current_user)
    return _feed_urls(request, token)


@router.post("/calendar-feed/rotate")
async def calendar_feed_rotate(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    token = await tasks_service.rotate_feed_token(db, current_user)
    return _feed_urls(request, token)


@calendar_router.get("/calendar/{token}.ics")
async def calendar_ics(token: str, db: AsyncSession = Depends(get_db)):
    row = await db.execute(select(User).where(User.calendar_feed_token == token))
    user = row.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Calendar feed not found")
    body = await tasks_service.render_ics(db, user)
    return Response(
        content=body,
        media_type="text/calendar",
        headers={
            "Content-Disposition": 'inline; filename="openhousepal.ics"',
            "Cache-Control": "no-cache, max-age=0",
        },
    )
