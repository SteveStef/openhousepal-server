"""Client emails: open / click tracking and the engagement rules that protect inbox placement.

Every email to an agent's client (Showcase invite + digests, tour confirmations, comment replies,
Spotlight alerts - the CLIENT templates) is logged in client_emails, whether it goes from the agent's
own Gmail / Outlook or from OpenHousePal. Its id is the tracking token:

- a 1x1 image (`track_open` - every client template has `{{#if track_open}}<img ...>{{/if}}`)
  records the open,
- the email's links (LINK_VARS) are swapped for /e/c/<id>/<key>, which records the click and
  redirects to the stored address (never to anything taken from the URL - no open redirect).

The rules (user, 10/06): mailbox providers rank a sender by whether people open what it sends, so
we stop sending automatic updates to people who never open them.

  daily   the normal state.
  weekly  they left emails unopened on STRIKE_DAYS different days (each given GRACE to be opened) -
          the daily Showcase digest becomes one weekly digest, Spotlight alerts at most one a week.
  off     they also left WEEKLY_STRIKES weekly digests unopened - no automatic emails at all.

Any sign of life - opening or clicking an email, using their Showcase, signing in at an open house -
puts them straight back on daily (the count starts again from there). The agent can turn a person
off by hand (stays off) or back on (restarts the count). Personal emails - a tour confirmation, a
reply to their comment, the thank-you after signing in - always send: the person just asked for
them. Only automatic ones (AUTO_TEMPLATES) are slowed or stopped, and never more than
MAX_AUTO_PER_DAY a day per agent + person.

Never raises into the send path: if tracking fails, the email goes out untracked."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from app.models.database import ClientEmail, ClientEmailPref, Lead, LeadEvent, Notification
from app.services.tasks_service import EASTERN

logger = logging.getLogger(__name__)

AUTO_TEMPLATES = {"showcase_daily_digest", "showcase_weekly_digest", "spotlight_listing_update"}
TRACKED_TEMPLATES = AUTO_TEMPLATES | {
    "visitor_confirmation", "showcase_ready", "agent_comment_response", "tour_confirmed", "spotlight_alert_confirm",
}
# Template variables that are the email's real links (checked against every client template's
# href="{{ ... }}"; unsubscribe, mailto: and tel: links are never wrapped).
LINK_VARS = ("showcase_link", "collection_link", "site_url")

STRIKE_DAYS = 3
WEEKLY_STRIKES = 3
GRACE = timedelta(hours=12)
WEEKLY_GAP = timedelta(days=6, hours=12)
MAX_AUTO_PER_DAY = 2
# Mail scanners (Outlook Safe Links, corporate filters) fetch images and follow links the moment a
# message arrives - that isn't the person.
SCANNER_WINDOW = timedelta(seconds=20)

# Lead history entries that aren't the person doing something.
_NOT_ENGAGEMENT = {"showcase_emails_off", "email_unsubscribed", "email_weekly", "email_paused", "email_resumed"}
WEEKLY_TEMPLATE = "showcase_weekly_digest"


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def api_base() -> str:
    if os.getenv("API_URL"):
        return os.getenv("API_URL").rstrip("/")
    return "http://127.0.0.1:8080" if os.getenv("DEV", "false") == "true" else "https://api.openhousepal.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(d: datetime) -> str:
    return d.astimezone(EASTERN).date().isoformat()


def _first(name: Optional[str], email: str) -> str:
    n = (name or "").strip().split(" ")[0]
    return n[:1].upper() + n[1:] if n else email


# ---------------------------------------------------------------- the decision (pure, testable)

def decide(emails: list[dict], *, manual: Optional[str], reset_at: Optional[datetime],
           last_engaged: Optional[datetime], unsubscribed: bool = False, now: Optional[datetime] = None) -> dict:
    """emails: this person's sent emails [{sent_at, opened_at, clicked_at, template}].
    -> {mode, reason, unopened, unopened_days, weekly_unopened}"""
    now = now or _now()
    if unsubscribed:
        return {"mode": "off", "reason": "Unsubscribed from emails", "unopened": 0, "unopened_days": 0, "weekly_unopened": 0, "code": "unsubscribed"}
    if manual == "off":
        return {"mode": "off", "reason": "Turned off by you", "unopened": 0, "unopened_days": 0, "weekly_unopened": 0, "code": "manual_off"}
    start = max([d for d in (reset_at, last_engaged) if d] or [None]) if (reset_at or last_engaged) else None
    unopened = [e for e in emails
                if e.get("sent_at") and (start is None or e["sent_at"] > start) and e["sent_at"] <= now - GRACE
                and not e.get("opened_at") and not e.get("clicked_at")]
    days = {_day(e["sent_at"]) for e in unopened}
    weekly = sum(1 for e in unopened if e.get("template") == WEEKLY_TEMPLATE)
    out = {"unopened": len(unopened), "unopened_days": len(days), "weekly_unopened": weekly}
    if weekly >= WEEKLY_STRIKES:
        return {**out, "mode": "off", "code": "weekly_unopened",
                "reason": f"Didn't open {weekly} weekly emails in a row, so automatic emails stopped. They restart by themselves when this person opens an email or uses their Showcase."}
    if len(days) >= STRIKE_DAYS:
        return {**out, "mode": "weekly", "code": "unopened_days",
                "reason": f"Didn't open {len(unopened)} emails sent on {len(days)} different days, so updates switched to one email a week. Back to daily as soon as they open one."}
    return {**out, "mode": "daily", "code": "ok", "reason": None}


# ---------------------------------------------------------------- reading state

async def _last_engaged(db, owner_id: str, email: str) -> Optional[datetime]:
    a = (await db.execute(select(func.max(func.greatest(ClientEmail.opened_at, ClientEmail.clicked_at))).where(
        ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email))).scalar()
    b = (await db.execute(select(func.max(LeadEvent.occurred_at)).join(Lead, Lead.id == LeadEvent.lead_id).where(
        Lead.owner_id == owner_id, Lead.email == email, LeadEvent.source_type.notin_(_NOT_ENGAGEMENT)))).scalar()
    return max([d for d in (a, b) if d] or [None]) if (a or b) else None


async def _emails_for(db, owner_id: str, email: str, since_days: int = 180) -> list[dict]:
    rows = (await db.execute(select(ClientEmail.sent_at, ClientEmail.opened_at, ClientEmail.clicked_at, ClientEmail.template).where(
        ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email, ClientEmail.status == "sent",
        ClientEmail.sent_at >= _now() - timedelta(days=since_days)))).all()
    return [{"sent_at": r[0], "opened_at": r[1], "clicked_at": r[2], "template": r[3]} for r in rows]


async def _pref(db, owner_id: str, email: str, create: bool = True) -> Optional[ClientEmailPref]:
    p = (await db.execute(select(ClientEmailPref).where(ClientEmailPref.owner_id == owner_id, ClientEmailPref.email == email))).scalar_one_or_none()
    if p is None and create:
        p = ClientEmailPref(owner_id=owner_id, email=email, mode="daily")
        db.add(p)
        await db.flush()
    return p


async def _is_unsubscribed(db, email: str) -> bool:
    try:
        from app.services.blacklist_service import BlacklistService
        return await BlacklistService.is_blacklisted(db, email)
    except Exception:  # noqa: BLE001
        return False


async def _lead(db, owner_id: str, email: str) -> Optional[Lead]:
    return (await db.execute(select(Lead).where(Lead.owner_id == owner_id, Lead.email == email).limit(1))).scalar_one_or_none()


async def _person_name(db, owner_id: str, email: str) -> str:
    lead = await _lead(db, owner_id, email)
    if lead and lead.name:
        return lead.name
    from app.models.database import Collection
    name = (await db.execute(select(Collection.visitor_name).where(Collection.owner_id == owner_id, func.lower(Collection.visitor_email) == email)
                             .order_by(Collection.created_at.desc()).limit(1))).scalar()
    return name or ""


async def refresh_status(db, owner_id: str, email: str, *, announce: bool = True, trigger: Optional[str] = None) -> dict:
    """Work out daily / weekly / off now; when it changed, record it once (lead history + bell).
    `trigger` names what brought them back ("opened an email") for the resume message. Commits."""
    email = _norm(email)
    pref = await _pref(db, owner_id, email)
    unsub = await _is_unsubscribed(db, email)
    d = decide(await _emails_for(db, owner_id, email), manual=pref.manual, reset_at=pref.reset_at,
               last_engaged=await _last_engaged(db, owner_id, email), unsubscribed=unsub)
    old = pref.mode or "daily"
    if d["mode"] != old and not unsub and pref.manual != "off":
        pref.mode, pref.changed_at = d["mode"], _now()
        pref.reason = d["reason"] or (f"Back on - {trigger}" if trigger else "Back on")
        if announce:
            await _announce(db, owner_id, email, old, d["mode"], pref.reason, trigger)
    elif d["mode"] != "daily":
        pref.reason = d["reason"]
    await db.commit()
    return {**d, "since": pref.changed_at, "manual": pref.manual}


async def _announce(db, owner_id: str, email: str, old: str, new: str, reason: str, trigger: Optional[str]) -> None:
    name = await _person_name(db, owner_id, email)
    who = _first(name, email)
    lead = await _lead(db, owner_id, email)
    if new == "weekly":
        source, title, msg = "email_weekly", f"{who}: emails now weekly", f"{who} hasn't opened their last few emails, so Showcase updates now go once a week. That keeps your emails out of junk."
    elif new == "off":
        source, title, msg = "email_paused", f"{who}: automatic emails stopped", f"{who} didn't open 3 weekly emails in a row, so automatic emails stopped. They restart by themselves if {who} opens one or visits their Showcase."
    else:
        source, title, msg = "email_resumed", f"{who}: emails back to daily", f"{who} {trigger or 'is active again'}, so their updates are back to normal."
    if lead is not None:
        from app.services.leads_service import log_activity
        await log_activity(db, owner_id=owner_id, email=email, lead=lead, source_type=source, summary=title.split(": ", 1)[1].capitalize() + (f" - {trigger}" if trigger and new == "daily" else ""),
                           raw_snapshot={"reason": reason, "from": old, "to": new})
    db.add(Notification(agent_id=owner_id, type="EMAIL_STATUS", reference_type="CLIENT_EMAIL_PREF", reference_id=email,
                        title=title, message=msg, visitor_name=name or None,
                        link=f"/leads?lead={lead.id}" if lead else "/leads/emails", is_read=False, created_at=_now()))


async def last_auto_sent(db, owner_id: str, email: str) -> Optional[datetime]:
    return (await db.execute(select(func.max(ClientEmail.sent_at)).where(
        ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == _norm(email), ClientEmail.kind == "auto",
        ClientEmail.status == "sent"))).scalar()


async def status_for(db, owner_id: str, email: str) -> dict:
    """Read-only state for one person (no announcement)."""
    email = _norm(email)
    pref = await _pref(db, owner_id, email, create=False)
    d = decide(await _emails_for(db, owner_id, email), manual=pref.manual if pref else None, reset_at=pref.reset_at if pref else None,
               last_engaged=await _last_engaged(db, owner_id, email), unsubscribed=await _is_unsubscribed(db, email))
    return {**d, "since": pref.changed_at if pref else None, "manual": pref.manual if pref else None}


# ---------------------------------------------------------------- the send path

async def before_send(owner_id: Optional[str], to: Optional[str], template: str, subject: str,
                      variables: dict) -> Optional[tuple[Optional[str], dict]]:
    """None = the rules hold this one back (don't send). Otherwise (tracking id or None, variables
    with tracked links + the open pixel)."""
    if not owner_id or not to or template not in TRACKED_TEMPLATES:
        return None, variables
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            email = _norm(to)
            kind = "auto" if template in AUTO_TEMPLATES else "personal"
            if kind == "auto":
                st = await refresh_status(db, owner_id, email)
                if st["mode"] == "off":
                    logger.info("client email held (off)", extra={"template": template, "owner_id": owner_id, "code": st.get("code")})
                    return None
                today_start = datetime.combine(_now().astimezone(EASTERN).date(), datetime.min.time(), EASTERN)
                autos_today = (await db.execute(select(func.count()).select_from(ClientEmail).where(
                    ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email, ClientEmail.kind == "auto",
                    ClientEmail.status.in_(("sent", "sending")),
                    func.coalesce(ClientEmail.sent_at, ClientEmail.created_at) >= today_start))).scalar() or 0
                if autos_today >= MAX_AUTO_PER_DAY:
                    logger.info("client email held (daily limit)", extra={"template": template, "owner_id": owner_id})
                    return None
                if st["mode"] == "weekly" and template != WEEKLY_TEMPLATE:
                    last_auto = (await db.execute(select(func.max(ClientEmail.sent_at)).where(
                        ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email, ClientEmail.kind == "auto",
                        ClientEmail.status == "sent"))).scalar()
                    if template == "showcase_daily_digest" or (last_auto and _now() - last_auto < WEEKLY_GAP):
                        logger.info("client email held (weekly)", extra={"template": template, "owner_id": owner_id})
                        return None
            links = {k: variables[k] for k in LINK_VARS if isinstance(variables.get(k), str) and variables[k].startswith("http")}
            rec = ClientEmail(owner_id=owner_id, recipient_email=email, template=template, subject=(subject or "")[:500],
                              kind=kind, status="sending", links=links or None)
            db.add(rec)
            await db.commit()
            base = api_base()
            out = dict(variables)
            for k in links:
                out[k] = f"{base}/e/c/{rec.id}/{k}"
            out["track_open"] = f"{base}/e/o/{rec.id}.gif"
            out["_track_id"] = rec.id
            return rec.id, out
    except Exception:  # noqa: BLE001 - tracking must never stop an email
        logger.warning("client email tracking failed - sending untracked", exc_info=True)
        return None, variables


async def after_send(track_id: Optional[str], ok: bool, via: str) -> None:
    if not track_id:
        return
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            rec = await db.get(ClientEmail, track_id)
            if rec is None:
                return
            rec.status, rec.via = ("sent" if ok else "failed"), via
            rec.sent_at = _now() if ok else None
            if ok and rec.template == WEEKLY_TEMPLATE:
                pref = await _pref(db, rec.owner_id, rec.recipient_email)
                pref.last_weekly_at = rec.sent_at
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("client email after_send failed", exc_info=True)


def strip_private(variables: dict) -> dict:
    return {k: v for k, v in variables.items() if k != "_track_id"}


# ---------------------------------------------------------------- opens + clicks

def _scanner(rec: ClientEmail) -> bool:
    return bool(rec.sent_at and _now() - rec.sent_at < SCANNER_WINDOW)


async def record_open(db, track_id: str) -> None:
    rec = await db.get(ClientEmail, track_id)
    if rec is None or rec.status != "sent" or _scanner(rec):
        return
    rec.open_count = (rec.open_count or 0) + 1
    first = rec.opened_at is None
    if first:
        rec.opened_at = _now()
    await db.commit()
    if first:
        await _engaged(db, rec, "opened")


async def record_click(db, track_id: str, key: str) -> Optional[str]:
    rec = await db.get(ClientEmail, track_id)
    if rec is None:
        return None
    url = (rec.links or {}).get(key)
    if rec.status != "sent" or _scanner(rec):
        return url
    rec.click_count = (rec.click_count or 0) + 1
    first = rec.clicked_at is None
    if first:
        rec.clicked_at = _now()
    first_open = rec.opened_at is None
    if first_open:
        rec.opened_at = rec.clicked_at   # a click is an open too (images may be blocked)
    await db.commit()
    if first:
        await _engaged(db, rec, "clicked")
    return url


_TEMPLATE_LABEL = {
    "showcase_daily_digest": "daily Showcase update", WEEKLY_TEMPLATE: "weekly Showcase update",
    "showcase_ready": "Showcase invite", "visitor_confirmation": "open house thank-you", "tour_confirmed": "tour confirmation",
    "agent_comment_response": "reply to their comment", "spotlight_alert_confirm": "Spotlight sign-up email",
    "spotlight_listing_update": "Spotlight listing alert",
}


def template_label(t: str) -> str:
    return _TEMPLATE_LABEL.get(t, "email")


async def _engaged(db, rec: ClientEmail, what: str) -> None:
    """First open / first click of one email: lead history, one bell row per person per day, and
    back to daily if they had been slowed down."""
    try:
        name = await _person_name(db, rec.owner_id, rec.recipient_email)
        who = _first(name, rec.recipient_email)
        lead = await _lead(db, rec.owner_id, rec.recipient_email)
        label = template_label(rec.template)
        if lead is not None:
            # One row per email in their history: a click turns that email's "Opened" row into
            # "Opened and clicked" (a click is an open too) instead of adding a second row.
            opened_row = None
            if what == "clicked":
                opened_row = (await db.execute(select(LeadEvent).where(
                    LeadEvent.lead_id == lead.id, LeadEvent.source_type == "email_opened", LeadEvent.source_id == rec.id,
                ).limit(1))).scalar_one_or_none()
            if opened_row is not None:
                opened_row.source_type = "email_clicked"
                opened_row.summary = f"Opened and clicked a link in your {label}"
                opened_row.occurred_at = _now()
                opened_row.agent_viewed_at = None
                await db.commit()
            else:
                from app.services.leads_service import log_activity
                await log_activity(db, owner_id=rec.owner_id, email=rec.recipient_email, lead=lead,
                                   source_type="email_clicked" if what == "clicked" else "email_opened",
                                   summary=f"{'Opened and clicked a link in' if what == 'clicked' else 'Opened'} your {label}",
                                   source_id=rec.id, raw_snapshot={"subject": rec.subject, "template": rec.template})
        # one bell row per person per day, updated as they open / click more
        ref = f"{rec.recipient_email}|{_day(_now())}"
        n = (await db.execute(select(Notification).where(Notification.agent_id == rec.owner_id, Notification.type == "EMAIL_ACTIVITY",
                                                         Notification.reference_id == ref))).scalar_one_or_none()
        day_start = datetime.combine(_now().astimezone(EASTERN).date(), datetime.min.time(), EASTERN)
        opened = (await db.execute(select(func.count()).select_from(ClientEmail).where(
            ClientEmail.owner_id == rec.owner_id, ClientEmail.recipient_email == rec.recipient_email, ClientEmail.opened_at >= day_start))).scalar() or 0
        clicked = (await db.execute(select(func.count()).select_from(ClientEmail).where(
            ClientEmail.owner_id == rec.owner_id, ClientEmail.recipient_email == rec.recipient_email, ClientEmail.clicked_at >= day_start))).scalar() or 0
        title = f"{who} {'opened and clicked your email' if clicked else 'opened your email'}"
        bits = [f"opened {opened} email{'s' if opened != 1 else ''}"] + ([f"clicked {clicked}"] if clicked else [])
        message = f"{who} {' and '.join(bits)} today - latest: your {label}."
        link = f"/leads?lead={lead.id}" if lead else "/leads/emails"
        if n is None:
            db.add(Notification(agent_id=rec.owner_id, type="EMAIL_ACTIVITY", reference_type="CLIENT_EMAIL_DAY", reference_id=ref,
                                title=title, message=message, visitor_name=name or None, link=link, is_read=False, created_at=_now()))
        else:
            n.title, n.message, n.link, n.is_read, n.read_at, n.created_at = title, message, link, False, None, _now()
        await db.commit()
        await refresh_status(db, rec.owner_id, rec.recipient_email, trigger="opened an email" if what == "opened" else "clicked an email")
    except Exception:  # noqa: BLE001
        logger.warning("client email engagement logging failed", exc_info=True)
        await db.rollback()


# ---------------------------------------------------------------- agent controls + lists

async def set_manual(db, owner_id: str, email: str, on: bool) -> dict:
    email = _norm(email)
    pref = await _pref(db, owner_id, email)
    lead = await _lead(db, owner_id, email)
    from app.services.leads_service import log_activity
    if on:
        pref.manual, pref.reset_at, pref.mode, pref.changed_at = None, _now(), "daily", _now()
        pref.reason = "Turned back on by you"
        if lead:
            await log_activity(db, owner_id=owner_id, email=email, lead=lead, source_type="email_resumed",
                               summary="Automatic emails turned back on by you", raw_snapshot={"to": "daily"})
    else:
        pref.manual, pref.mode, pref.changed_at, pref.reason = "off", "off", _now(), "Turned off by you"
        if lead:
            await log_activity(db, owner_id=owner_id, email=email, lead=lead, source_type="email_paused",
                               summary="Automatic emails turned off by you", raw_snapshot={"to": "off"})
    await db.commit()
    return await status_for(db, owner_id, email)


def _rate(sent: int, opened: int) -> Optional[int]:
    return round(100 * opened / sent) if sent else None


async def people(db, owner_id: str) -> dict[str, dict]:
    """Everyone this agent has emailed (last 180 days): counts, latest activity and current mode -
    one pass, for the Emails page and the Leads list."""
    since = _now() - timedelta(days=180)
    rows = (await db.execute(select(ClientEmail).where(ClientEmail.owner_id == owner_id, ClientEmail.status == "sent",
                                                       ClientEmail.sent_at >= since).order_by(ClientEmail.sent_at.desc()))).scalars().all()
    by: dict[str, list[ClientEmail]] = {}
    for r in rows:
        by.setdefault(r.recipient_email, []).append(r)
    prefs = {p.email: p for p in (await db.execute(select(ClientEmailPref).where(ClientEmailPref.owner_id == owner_id))).scalars().all()}
    emails = set(by) | set(prefs)
    if not emails:
        return {}
    eng = dict((await db.execute(select(Lead.email, func.max(LeadEvent.occurred_at)).join(LeadEvent, LeadEvent.lead_id == Lead.id).where(
        Lead.owner_id == owner_id, Lead.email.in_(emails), LeadEvent.source_type.notin_(_NOT_ENGAGEMENT)).group_by(Lead.email))).all())
    from app.models.database import BlacklistedEmail
    unsub = {e for (e,) in (await db.execute(select(func.lower(BlacklistedEmail.email)).where(func.lower(BlacklistedEmail.email).in_(emails)))).all()}
    out = {}
    for email in emails:
        rs = by.get(email, [])
        p = prefs.get(email)
        opens = [r.opened_at for r in rs if r.opened_at] + [r.clicked_at for r in rs if r.clicked_at]
        last_engaged = max([d for d in (max(opens) if opens else None, eng.get(email)) if d] or [None]) if (opens or eng.get(email)) else None
        d = decide([{"sent_at": r.sent_at, "opened_at": r.opened_at, "clicked_at": r.clicked_at, "template": r.template} for r in rs],
                   manual=p.manual if p else None, reset_at=p.reset_at if p else None, last_engaged=last_engaged, unsubscribed=email in unsub)
        sent90 = [r for r in rs if r.sent_at >= _now() - timedelta(days=90)]
        opened90 = sum(1 for r in sent90 if r.opened_at or r.clicked_at)
        latest = rs[0] if rs else None
        out[email] = {
            "email": email, "mode": d["mode"], "reason": d["reason"], "code": d["code"], "manual": p.manual if p else None,
            "since": p.changed_at.isoformat() if p and p.changed_at and d["mode"] != "daily" else None,
            "sent": len(rs), "opened": sum(1 for r in rs if r.opened_at or r.clicked_at), "clicked": sum(1 for r in rs if r.clicked_at),
            "rate": _rate(len(sent90), opened90), "unopened_in_a_row": d["unopened"],
            "last_sent_at": latest.sent_at.isoformat() if latest else None,
            "last_subject": latest.subject if latest else None,
            "last_template": template_label(latest.template) if latest else None,
            "last_opened_at": max(opens).isoformat() if opens else None,
            "last_clicked_at": max([r.clicked_at for r in rs if r.clicked_at] or [None]).isoformat() if any(r.clicked_at for r in rs) else None,
        }
    return out


def email_out(r: ClientEmail) -> dict:
    return {"id": r.id, "to": r.recipient_email, "subject": r.subject, "template": r.template, "label": template_label(r.template),
            "kind": r.kind, "via": r.via, "status": r.status,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None, "opened_at": r.opened_at.isoformat() if r.opened_at else None,
            "open_count": r.open_count or 0, "clicked_at": r.clicked_at.isoformat() if r.clicked_at else None, "click_count": r.click_count or 0}
