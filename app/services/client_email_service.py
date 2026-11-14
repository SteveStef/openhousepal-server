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
  off     they also left WEEKLY_STRIKES weekly digests unopened - no automatic emails at all, and
          their Showcases with this agent go to sleep (showcase_sleep.py).

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
    "visitor_confirmation", "showcase_ready", "showcase_shared", "agent_comment_response", "tour_confirmed", "spotlight_alert_confirm",
}
# Template variables that are the email's real links (checked against every client template's
# href="{{ ... }}"; unsubscribe, mailto: and tel: links are never wrapped).
LINK_VARS = ("showcase_link", "collection_link", "site_url")

STRIKE_DAYS = 3
WEEKLY_STRIKES = 5
GRACE = timedelta(hours=12)
WEEKLY_GAP = timedelta(days=6, hours=12)
BIWEEKLY_GAP = timedelta(days=13, hours=12)
# How often someone gets automatic emails when it was picked on purpose (owner, 11/07). Daily is the
# normal rules with the count starting over; weekly and every two weeks stay put, opened or not.
FREQUENCIES = ("daily", "weekly", "biweekly")
FREQUENCY_WORDS = {"daily": "daily", "weekly": "weekly", "biweekly": "every two weeks"}
# When the client wants their Showcase emails (Eastern). The day's updates are gathered at 4:30 PM;
# a later time sends them that evening, an earlier one the next morning (owner, 11/11).
SEND_TIMES = ("08:00", "12:00", "16:30", "19:00")
DEFAULT_SEND_TIME = "16:30"
MAX_AUTO_PER_DAY = 2
# Mail scanners (Outlook Safe Links, corporate filters) fetch images and follow links the moment a
# message arrives - that isn't the person.
SCANNER_WINDOW = timedelta(seconds=20)
# An email sent from the agent's own Gmail / Outlook keeps a copy - tracking image and all - in their
# Sent folder, so scrolling through Sent "opens" it (owner, 10/26: four clients "opened" within one
# minute, one of them a deleted lead). That looks like several different people's older emails
# opening within a few minutes of each other: not counted, and any just counted are taken back. A
# client opening the evening update right after it arrives is still counted (it isn't stale yet).
SENT_FOLDER_WINDOW = timedelta(minutes=3)
SENT_FOLDER_STALE = timedelta(minutes=30)

# Lead history entries that aren't the person doing something.
_NOT_ENGAGEMENT = {"showcase_emails_off", "email_unsubscribed", "email_weekly", "email_paused", "email_resumed"}
WEEKLY_TEMPLATE = "showcase_weekly_digest"
# ClientEmailPref.manual when the agent deleted this person's lead (lead_cleanup_service): off like
# "off", but lifted by itself when they become a lead again or the agent turns their Showcase back on.
LEAD_DELETED = "lead_deleted"
# ClientEmailPref.manual on a client's NEW address when the agent changed their email while emails to
# the old one were off (bounced, unsubscribed, ignored, turned off): held until the agent says to turn
# them back on, which starts the unopened count over (user, 11/12).
EMAIL_CHANGED = "email_changed"
MANUAL_OFF = ("off", LEAD_DELETED, EMAIL_CHANGED)


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
           last_engaged: Optional[datetime], unsubscribed: bool = False, now: Optional[datetime] = None,
           frequency: Optional[str] = None, frequency_by: Optional[str] = None) -> dict:
    """emails: this person's sent emails [{sent_at, opened_at, clicked_at, template}].
    -> {mode, reason, unopened, unopened_days, weekly_unopened}"""
    now = now or _now()
    if unsubscribed:
        return {"mode": "off", "reason": "Unsubscribed from emails", "unopened": 0, "unopened_days": 0, "weekly_unopened": 0, "code": "unsubscribed"}
    if manual == "off":
        return {"mode": "off", "reason": "Turned off by you", "unopened": 0, "unopened_days": 0, "weekly_unopened": 0, "code": "manual_off"}
    if manual == LEAD_DELETED:
        return {"mode": "off", "reason": "You deleted their lead, so automatic emails to them stopped", "unopened": 0, "unopened_days": 0,
                "weekly_unopened": 0, "code": "lead_deleted"}
    if manual == EMAIL_CHANGED:
        return {"mode": "off", "reason": "Paused when you changed their email. Turn them back on to restart.", "unopened": 0,
                "unopened_days": 0, "weekly_unopened": 0, "code": "email_changed"}
    if frequency in ("weekly", "biweekly"):
        who = "They chose" if frequency_by == "client" else "You chose"
        return {"mode": frequency, "code": "chosen", "reason": f"{who} {FREQUENCY_WORDS[frequency]} emails, so it stays that way whether they open them or not.",
                "unopened": 0, "unopened_days": 0, "weekly_unopened": 0}
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


PROBLEM_DELIVERY = ("bounced", "spam", "blocked")
_DEDUPE_PENDING: dict[str, str] = {}   # ClientEmail id -> email_dedupe_service key (mailbox sends)


async def _problem(db, owner_id: str, email: str) -> Optional[ClientEmail]:
    """Their latest email Mailgun said bounced / was blocked / was marked as spam (webhooks)."""
    return (await db.execute(select(ClientEmail).where(
        ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email, ClientEmail.delivery.in_(PROBLEM_DELIVERY))
        .order_by(ClientEmail.delivery_at.desc()).limit(1))).scalar_one_or_none()


async def _why_blocked(db, owner_id: str, email: str, d: dict) -> dict:
    """Blocked because an email bounced or they called it spam: say that, not just "Unsubscribed"."""
    if d.get("code") == "unsubscribed":
        p = await _problem(db, owner_id, email)
        if p is not None and p.delivery_note:
            return {**d, "reason": p.delivery_note, "code": "bounced" if p.delivery in ("bounced", "blocked") else "spam"}
    return d


async def refresh_status(db, owner_id: str, email: str, *, announce: bool = True, trigger: Optional[str] = None) -> dict:
    """Work out daily / weekly / off now; when it changed, record it once (lead history + bell).
    `trigger` names what brought them back ("opened an email") for the resume message. Commits."""
    email = _norm(email)
    pref = await _pref(db, owner_id, email)
    unsub = await _is_unsubscribed(db, email)
    d = decide(await _emails_for(db, owner_id, email), manual=pref.manual, reset_at=pref.reset_at,
               last_engaged=await _last_engaged(db, owner_id, email), unsubscribed=unsub,
               frequency=pref.frequency, frequency_by=pref.frequency_by)
    d = await _why_blocked(db, owner_id, email, d)
    old = pref.mode or "daily"
    woke: list[str] = []
    if d.get("code") == "chosen":
        # Picked on purpose: nothing to announce, and nothing steps it down.
        if old != d["mode"]:
            pref.mode, pref.changed_at = d["mode"], _now()
        pref.reason = d["reason"]
    elif d["mode"] != old and not unsub and pref.manual not in MANUAL_OFF:
        pref.mode, pref.changed_at = d["mode"], _now()
        # Tell the client on their Showcase next time they're there (they may never see the agent's note).
        if (d["mode"], d.get("code")) in (("weekly", "unopened_days"), ("off", "weekly_unopened")):
            pref.client_notice, pref.client_notice_at = d["mode"], _now()
        pref.reason = d["reason"] or (f"Back on - {trigger}" if trigger else "Back on")
        # Ignored 3 weekly emails -> their Showcases go to sleep; any sign of life wakes them (showcase_sleep).
        from app.services import showcase_sleep
        slept = 0
        if d["mode"] == "off" and d.get("code") == "weekly_unopened":
            slept = await showcase_sleep.sleep_for_no_response(db, owner_id, email)
        elif old == "off":
            woke = await showcase_sleep.wake_no_response(db, owner_id, email)
        # Not opening the emails, but still using their Showcase: it stays open - say so (10/24).
        using = d["mode"] in ("weekly", "off") and not slept and await showcase_sleep.client_still_using(db, owner_id, email)
        if announce:
            await _announce(db, owner_id, email, old, d["mode"], pref.reason, trigger, slept=slept, still_using=using)
    elif d["mode"] != "daily":
        pref.reason = d["reason"]
        # Still ignoring everything: a Showcase kept awake while the agent was minding it (starred /
        # opened lately) falls asleep once they stop - checked whenever its updates come round.
        if d["mode"] == "off" and d.get("code") == "weekly_unopened" and not unsub and pref.manual not in MANUAL_OFF:
            from app.services import showcase_sleep
            if await showcase_sleep.sleep_for_no_response(db, owner_id, email) and announce:
                await _announce(db, owner_id, email, "off", "off", pref.reason, trigger, slept=1)
    await db.commit()
    if woke:
        from app.services.showcase_sleep import catch_up
        await catch_up(db, woke)
    return {**d, "since": pref.changed_at, "manual": pref.manual, "frequency": pref.frequency, "frequency_by": pref.frequency_by}


async def _announce(db, owner_id: str, email: str, old: str, new: str, reason: str, trigger: Optional[str], slept: int = 0,
                    still_using: bool = False) -> None:
    name = await _person_name(db, owner_id, email)
    who = _first(name, email)
    lead = await _lead(db, owner_id, email)
    if new == "weekly":
        source, title, msg = "email_weekly", f"{who}: emails now weekly", f"{who} hasn't opened their last few emails, so Showcase updates now go once a week. That keeps your emails out of junk."
        if still_using:
            msg += f" {who} keeps using their Showcase, so it stays open."
    elif new == "off":
        if still_using and not slept:
            source, title, msg = "email_paused", f"{who}: automatic emails stopped", f"{who} didn't open {WEEKLY_STRIKES} weekly emails in a row, so automatic emails stopped - but {who} keeps using their Showcase, so it stays open and keeps updating."
        elif slept:
            source, title, msg = "email_paused", f"{who}: Showcase asleep", f"{who} didn't open {WEEKLY_STRIKES} weekly emails in a row, so automatic emails stopped and their Showcase went to sleep (Showcases -> Inactive). It wakes up by itself if {who} opens it or an old email."
        else:
            source, title, msg = "email_paused", f"{who}: automatic emails stopped", f"{who} didn't open {WEEKLY_STRIKES} weekly emails in a row, so automatic emails stopped. They restart by themselves if {who} opens one or visits their Showcase."
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
               last_engaged=await _last_engaged(db, owner_id, email), unsubscribed=await _is_unsubscribed(db, email),
               frequency=pref.frequency if pref else None, frequency_by=pref.frequency_by if pref else None)
    d = await _why_blocked(db, owner_id, email, d)
    if d.get("code") == "email_changed" and pref is not None and pref.reason:
        d = {**d, "reason": pref.reason}
    return {**d, "since": pref.changed_at if pref else None, "manual": pref.manual if pref else None,
            "frequency": pref.frequency if pref else None, "frequency_by": pref.frequency_by if pref else None,
            "send_time": (pref.send_time if pref else None) or DEFAULT_SEND_TIME,
            "notice": pref.client_notice if pref else None, "notice_at": pref.client_notice_at if pref else None}


# ---------------------------------------------------------------- the send path

async def before_send(owner_id: Optional[str], to: Optional[str], template: str, subject: str,
                      variables: dict) -> Optional[tuple[Optional[str], dict]]:
    """None = the rules hold this one back (don't send). Otherwise (tracking id or None, variables
    with tracked links + the open pixel)."""
    # One of each kind per person per event per day (email_dedupe_service) - a repeat isn't sent.
    # "_manual": the agent pressed Send invite (again) themselves - it goes, same day or not, and a
    # Studio -> Email switch for that kind doesn't hold it back (only an unsubscribe would).
    from app.services import email_dedupe_service as dd
    manual = bool(variables.get("_manual"))
    variables = {k: v for k, v in variables.items() if k != "_manual"}
    dkey = dd.key_for(to, template, subject, variables)
    if not manual and await dd.is_repeat(dkey, template, "client", owner_id):
        return None
    if dkey:
        variables = {**variables, "_dedupe_key": dkey}
    if not owner_id or not to or template not in TRACKED_TEMPLATES:
        return None, variables
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            email = _norm(to)
            # The agent's own Studio -> Email settings can only hold emails back, never add any.
            from app.models.database import User
            from app.services.email_settings_service import client_rule
            rule = client_rule(await db.get(User, owner_id), template)
            if rule is not None and not rule["on"] and not manual:
                logger.info("client email held (agent turned it off)", extra={"template": template, "owner_id": owner_id})
                dd.forget(dkey)
                return None
            kind = "auto" if template in AUTO_TEMPLATES else "personal"
            if kind == "auto":
                st = await refresh_status(db, owner_id, email)
                if rule is not None and rule.get("mode") == "weekly" and st["mode"] == "daily":
                    st = {**st, "mode": "weekly"}
                if st["mode"] == "off":
                    logger.info("client email held (off)", extra={"template": template, "owner_id": owner_id, "code": st.get("code")})
                    dd.forget(dkey)
                    return None
                today_start = datetime.combine(_now().astimezone(EASTERN).date(), datetime.min.time(), EASTERN)
                autos_today = (await db.execute(select(func.count()).select_from(ClientEmail).where(
                    ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email, ClientEmail.kind == "auto",
                    ClientEmail.status.in_(("sent", "sending")),
                    func.coalesce(ClientEmail.sent_at, ClientEmail.created_at) >= today_start))).scalar() or 0
                if autos_today >= MAX_AUTO_PER_DAY:
                    logger.info("client email held (daily limit)", extra={"template": template, "owner_id": owner_id})
                    dd.forget(dkey)
                    return None
                if st["mode"] in ("weekly", "biweekly") and template != WEEKLY_TEMPLATE:
                    gap = BIWEEKLY_GAP if st["mode"] == "biweekly" else WEEKLY_GAP
                    last_auto = (await db.execute(select(func.max(ClientEmail.sent_at)).where(
                        ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email, ClientEmail.kind == "auto",
                        ClientEmail.status == "sent"))).scalar()
                    if template == "showcase_daily_digest" or (last_auto and _now() - last_auto < gap):
                        logger.info("client email held (weekly)", extra={"template": template, "owner_id": owner_id})
                        dd.forget(dkey)
                        return None
            links = {k: variables[k] for k in LINK_VARS if isinstance(variables.get(k), str) and variables[k].startswith("http")}
            rec = ClientEmail(owner_id=owner_id, recipient_email=email, template=template, subject=(subject or "")[:500],
                              kind=kind, status="sending", links=links or None, about=about_from(variables) or None)
            db.add(rec)
            if dkey:
                _DEDUPE_PENDING[rec.id] = dkey   # recorded with the email if it goes from their own mailbox
            await db.commit()
            base = api_base()
            out = dict(variables)
            from app.services.personal_email_service import PERSONAL_TEMPLATES, with_marker
            if template in PERSONAL_TEMPLATES:
                # Plain personal note: the real link (+ a marker the page reports), no pixel.
                for k in links:
                    out[k] = with_marker(links[k], rec.id)
            else:
                for k in links:
                    out[k] = f"{base}/e/c/{rec.id}/{k}"
                out["track_open"] = f"{base}/e/o/{rec.id}.gif"
            out["_track_id"] = rec.id
            return rec.id, out
    except Exception:  # noqa: BLE001 - tracking must never stop an email
        logger.warning("client email tracking failed - sending untracked", exc_info=True)
        return None, variables


_ABOUT_KEYS = ("street_address", "city", "state", "image", "price", "beds", "baths", "sqft", "is_price_drop", "old_price", "new_price")


def about_from(variables: dict) -> list[dict]:
    """The homes an email was about, from its own variables (digest / Showcase emails carry
    `properties` or `homes`; a sign-in thank-you or tour email names one address) - so the agent
    can see what someone was opening and clicking (owner, 11/13). At most 8, small fields only."""
    out: list[dict] = []
    try:
        for key in ("properties", "homes"):
            for h in variables.get(key) or []:
                if isinstance(h, dict) and (h.get("street_address") or h.get("address")):
                    row = {k: h.get(k) for k in _ABOUT_KEYS if h.get(k) not in (None, "")}
                    row.setdefault("street_address", h.get("address"))
                    out.append(row)
            if out:
                break
        if not out:
            addr = variables.get("property_address") or variables.get("home_address") or variables.get("street")
            if isinstance(addr, str) and addr.strip():
                row = {"street_address": addr.strip()[:200]}
                img = variables.get("property_image") or variables.get("home_image")
                if isinstance(img, str) and img.startswith("http"):
                    row["image"] = img
                out.append(row)
    except Exception:  # noqa: BLE001 - nice to have; never stops an email
        return []
    return [{k: (str(v)[:300] if isinstance(v, str) else v) for k, v in r.items()} for r in out[:8]]


def failure_reason(code: Optional[int], body: Optional[str]) -> str:
    """A plain reason the agent can act on, from the provider's answer."""
    t = (body or "").lower()
    if any(x in t for x in ("not a valid address", "invalid to", "invalid address", "invalid recipient", "recipient address rejected",
                             "no such user", "does not exist", "mailbox unavailable", "invalid email")):
        return "That email address doesn't exist - check it on their lead."
    if "suppress" in t or "unsubscribed" in t or "bounce" in t:
        return "This address bounced before, so it's blocked to protect your sending."
    if code == 429 or "limit" in t or "quota" in t:
        return "Sending limit reached - it will be retried."
    return f"The email service refused it ({code or 'no answer'})."


async def after_send(track_id: Optional[str], ok: bool, via: str, *, code: Optional[int] = None, detail: Optional[str] = None) -> None:
    if not track_id:
        return
    from app.database import AsyncSessionLocal
    dkey = _DEDUPE_PENDING.pop(track_id, None)
    if not ok and via == "mailbox" and dkey:
        from app.services.email_dedupe_service import forget
        forget(dkey)   # a retry later today is not a repeat
    try:
        async with AsyncSessionLocal() as db:
            rec = await db.get(ClientEmail, track_id)
            if rec is None:
                return
            rec.status, rec.via = ("sent" if ok else "failed"), via
            rec.sent_at = _now() if ok else None
            if via == "mailbox":  # Mailgun sends are counted in EmailService itself
                from app.services import email_log_service
                email_log_service.record(rec.template, "client", ok=ok, owner_id=rec.owner_id, client_email_id=rec.id, via="mailbox",
                                         dedupe_key=dkey if ok else None)
            rec.error = None if ok else failure_reason(code, detail)[:300]
            if not ok:
                logger.warning("client email failed", extra={"template": rec.template, "via": via, "code": code, "detail": (detail or "")[:300]})
            if ok and rec.template == WEEKLY_TEMPLATE:
                pref = await _pref(db, rec.owner_id, rec.recipient_email)
                pref.last_weekly_at = rec.sent_at
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("client email after_send failed", exc_info=True)


def strip_private(variables: dict) -> dict:
    return {k: v for k, v in variables.items() if k not in ("_track_id", "_log_owner", "_log_audience", "_dedupe_key", "_log_id")}


# ---------------------------------------------------------------- opens + clicks

def _scanner(rec: ClientEmail) -> bool:
    return bool(rec.sent_at and _now() - rec.sent_at < SCANNER_WINDOW)


def _stale(rec: ClientEmail, at: datetime) -> bool:
    return bool(rec.sent_at and at - rec.sent_at > SENT_FOLDER_STALE)


async def _sent_folder_burst(db, rec: ClientEmail, at: datetime) -> Optional[list[ClientEmail]]:
    """The other emails opening alongside this one if this looks like the agent's Sent folder (see
    SENT_FOLDER_WINDOW), else None. Only emails sent from the agent's own mailbox have a Sent copy."""
    if rec.via != "mailbox" or rec.clicked_at or not _stale(rec, at):
        return None
    since = at - SENT_FOLDER_WINDOW
    from sqlalchemy import or_
    rows = (await db.execute(select(ClientEmail).where(
        ClientEmail.owner_id == rec.owner_id, ClientEmail.id != rec.id, ClientEmail.via == "mailbox",
        ClientEmail.recipient_email != rec.recipient_email, ClientEmail.clicked_at.is_(None),
        or_(ClientEmail.opened_at >= since, ClientEmail.sent_folder_seen_at >= since),
    ))).scalars().all()
    burst = [r for r in rows if _stale(r, max(d for d in (r.opened_at, r.sent_folder_seen_at) if d))]
    return burst or None


async def _drop_resumed(db, owner_id: str, email: str, when: datetime, lead=None) -> int:
    """The "emails back to daily" notice (bell + lead history) an open set off, when that open turns
    out to be the agent's Sent folder (owner, 10/26: "it said Nishit opened his email today but I
    don't see what email he opened"). Doesn't commit."""
    from sqlalchemy import delete
    lo, hi = when - timedelta(minutes=1), when + SENT_FOLDER_WINDOW
    res = await db.execute(delete(Notification).where(
        Notification.agent_id == owner_id, Notification.type == "EMAIL_STATUS", Notification.reference_id == email,
        Notification.title.like("%emails back to daily"), Notification.created_at >= lo, Notification.created_at <= hi,
    ).execution_options(synchronize_session=False))
    if lead is not None:
        await db.execute(delete(LeadEvent).where(
            LeadEvent.lead_id == lead.id, LeadEvent.source_type == "email_resumed",
            LeadEvent.occurred_at >= lo, LeadEvent.occurred_at <= hi,
        ).execution_options(synchronize_session=False))
    return res.rowcount or 0


async def _undo_open(db, rec: ClientEmail) -> None:
    """An open that turned out to be the agent's Sent folder: off the email, the lead's history and
    the bell (and the "back to daily" it set off). Doesn't commit."""
    when = rec.opened_at
    rec.opened_at, rec.open_count = None, 0
    lead = await _lead(db, rec.owner_id, rec.recipient_email)
    if lead is not None:
        from sqlalchemy import delete
        await db.execute(delete(LeadEvent).where(LeadEvent.lead_id == lead.id, LeadEvent.source_type == "email_opened",
                                                 LeadEvent.source_id == rec.id).execution_options(synchronize_session=False))
    await db.flush()
    if when is not None:
        await _drop_resumed(db, rec.owner_id, rec.recipient_email, when, lead)
        await _day_note(db, rec.owner_id, rec.recipient_email, when, lead)


async def record_open(db, track_id: str) -> None:
    rec = await db.get(ClientEmail, track_id)
    if rec is None or rec.status != "sent" or _scanner(rec):
        return
    now = _now()
    burst = await _sent_folder_burst(db, rec, now)
    if burst is not None or (rec.sent_folder_seen_at and now - rec.sent_folder_seen_at < SENT_FOLDER_WINDOW and not rec.clicked_at):
        rec.sent_folder_seen_at = now
        undone = set()
        if rec.opened_at and now - rec.opened_at < SENT_FOLDER_WINDOW and not rec.clicked_at:
            await _undo_open(db, rec)
            undone.add(rec.recipient_email)
        for other in burst or []:
            if other.opened_at and other.opened_at >= now - SENT_FOLDER_WINDOW:
                other.sent_folder_seen_at = other.opened_at
                await _undo_open(db, other)
                undone.add(other.recipient_email)
        await db.commit()
        for email in undone:
            await refresh_status(db, rec.owner_id, email, announce=False)
        logger.info("Email open ignored - the agent's Sent folder", extra={"client_email": rec.id, "undone": len(undone)})
        return
    rec.open_count = (rec.open_count or 0) + 1
    rec.last_engaged_at = _now()
    first = rec.opened_at is None
    if first:
        rec.opened_at = _now()
    await db.commit()
    if first:
        await _engaged(db, rec, "opened")
    elif rec.open_count >= 2:
        await _repeat(db, rec)


async def record_click(db, track_id: str, key: str) -> Optional[str]:
    rec = await db.get(ClientEmail, track_id)
    if rec is None:
        return None
    url = (rec.links or {}).get(key)
    if rec.status != "sent" or _scanner(rec):
        return url
    rec.click_count = (rec.click_count or 0) + 1
    rec.last_engaged_at = _now()
    first = rec.clicked_at is None
    if first:
        rec.clicked_at = _now()
    first_open = rec.opened_at is None
    if first_open:
        rec.opened_at = rec.clicked_at   # a click is an open too (images may be blocked)
    await db.commit()
    if first:
        await _engaged(db, rec, "clicked")
    if (rec.click_count or 0) >= 2 or ((rec.open_count or 0) >= 2 and not first):
        await _repeat(db, rec)
    return url


async def _repeat(db, rec: ClientEmail) -> None:
    """A second (third...) open or click of the same email - worth telling the agent about, quietly,
    with a check-in to add (lead_interest_service, owner 11/13). Never stops the open / click."""
    try:
        from app.services import lead_interest_service
        await lead_interest_service.note_repeat(db, rec)
    except Exception:  # noqa: BLE001
        logger.warning("repeat engagement note failed", exc_info=True)
        await db.rollback()


async def record_visit(db, track_id: str) -> None:
    """The Showcase page opened from a personal email's link (?ce=<id>) - a click (and an open)."""
    rec = await db.get(ClientEmail, track_id)
    if rec is None or rec.status != "sent":
        return
    key = next(iter(rec.links or {}), None)
    if key:
        await record_click(db, track_id, key)


_TEMPLATE_LABEL = {
    "showcase_daily_digest": "daily Showcase update", WEEKLY_TEMPLATE: "weekly Showcase update",
    "showcase_ready": "Showcase invite", "showcase_shared": "Showcase from you", "visitor_confirmation": "open house thank-you", "tour_confirmed": "tour confirmation",
    "agent_comment_response": "reply to their comment", "spotlight_alert_confirm": "Spotlight sign-up email",
    "spotlight_listing_update": "Spotlight listing alert",
}


def template_label(t: str) -> str:
    return _TEMPLATE_LABEL.get(t, "email")


async def _day_note(db, owner_id: str, email: str, at: datetime, lead: Optional[Lead], *, latest_label: Optional[str] = None,
                    name: Optional[str] = None, fresh: bool = False) -> None:
    """The bell row "Jane opened 2 emails today" for `email` on the (Eastern) day of `at`, from what
    is counted now - removed when nothing is left that day. Doesn't commit."""
    ref = f"{email}|{_day(at)}"
    n = (await db.execute(select(Notification).where(Notification.agent_id == owner_id, Notification.type == "EMAIL_ACTIVITY",
                                                     Notification.reference_id == ref))).scalar_one_or_none()
    day_start = datetime.combine(at.astimezone(EASTERN).date(), datetime.min.time(), EASTERN)
    day_end = day_start + timedelta(days=1)
    mine = (ClientEmail.owner_id == owner_id, ClientEmail.recipient_email == email)
    opened_rows = (await db.execute(select(ClientEmail).where(*mine, ClientEmail.opened_at >= day_start, ClientEmail.opened_at < day_end)
                                    .order_by(ClientEmail.opened_at.desc()))).scalars().all()
    clicked = (await db.execute(select(func.count()).select_from(ClientEmail).where(
        *mine, ClientEmail.clicked_at >= day_start, ClientEmail.clicked_at < day_end))).scalar() or 0
    opened = len(opened_rows)
    if lead is None or (not opened and not clicked):
        if n is not None:
            await db.delete(n)
        return
    who = _first(name or lead.name, email)
    label = latest_label or template_label(opened_rows[0].template if opened_rows else "")
    title = f"{who} {'opened and clicked your email' if clicked else 'opened your email'}"
    bits = [f"opened {opened} email{'s' if opened != 1 else ''}"] + ([f"clicked {clicked}"] if clicked else [])
    message = f"{who} {' and '.join(bits)} today - latest: your {label}."
    link = f"/leads?lead={lead.id}"
    if n is None:
        if fresh:
            db.add(Notification(agent_id=owner_id, type="EMAIL_ACTIVITY", reference_type="CLIENT_EMAIL_DAY", reference_id=ref,
                                title=title, message=message, visitor_name=name or lead.name or None, link=link, is_read=False, created_at=_now()))
    elif fresh:
        n.title, n.message, n.link, n.is_read, n.read_at, n.created_at = title, message, link, False, None, _now()
    else:
        n.title, n.message = title, message


async def correct_sent_folder_opens(db) -> int:
    """One-time fix of opens counted before the Sent-folder rule (owner, 10/26): among emails sent
    from agents' own mailboxes, two or more different people's older emails first "opened" within
    SENT_FOLDER_WINDOW of each other, none clicked, were the agent's Sent folder. Commits."""
    rows = (await db.execute(select(ClientEmail).where(
        ClientEmail.via == "mailbox", ClientEmail.opened_at.isnot(None), ClientEmail.clicked_at.is_(None),
    ).order_by(ClientEmail.owner_id, ClientEmail.opened_at))).scalars().all()
    groups: list[list[ClientEmail]] = []
    for r in rows:
        if not _stale(r, r.opened_at):
            continue
        g = groups[-1] if groups else None
        if g and g[-1].owner_id == r.owner_id and r.opened_at - g[-1].opened_at <= SENT_FOLDER_WINDOW:
            g.append(r)
        else:
            groups.append([r])
    fixed, touched = 0, set()
    for g in groups:
        if len({r.recipient_email for r in g}) < 2:
            continue
        for r in g:
            r.sent_folder_seen_at = r.opened_at
            await _undo_open(db, r)
            touched.add((r.owner_id, r.recipient_email))
            fixed += 1
    # Opens set aside before the notice was taken back with them (Nishit, 10/26).
    for r in (await db.execute(select(ClientEmail).where(ClientEmail.sent_folder_seen_at.isnot(None)))).scalars().all():
        lead = await _lead(db, r.owner_id, r.recipient_email)
        if await _drop_resumed(db, r.owner_id, r.recipient_email, r.sent_folder_seen_at, lead):
            touched.add((r.owner_id, r.recipient_email))
            fixed += 1
    if fixed:
        await db.commit()
        for owner_id, email in touched:
            await refresh_status(db, owner_id, email, announce=False)
    return fixed


async def scheduled_correct_sent_folder_opens() -> None:
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            n = await correct_sent_folder_opens(db)
        if n:
            logger.info("Corrected email opens that were the agent's Sent folder: %s", n)
    except Exception:  # noqa: BLE001
        logger.exception("correcting Sent-folder email opens failed")


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
        # One bell row per person per day, updated as they open / click more - only for someone who is
        # one of the agent's leads. A deleted lead (or an address that never was one) opening an old
        # email is no news to the agent (owner, 10/26).
        if lead is None:
            await db.commit()
            return
        await _day_note(db, rec.owner_id, rec.recipient_email, _now(), lead, latest_label=label, name=name, fresh=True)
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
    woke: list[str] = []
    if on:
        pref.manual, pref.reset_at, pref.mode, pref.changed_at = None, _now(), "daily", _now()
        pref.client_notice = None
        pref.reason = "Turned back on by you"
        if lead:
            await log_activity(db, owner_id=owner_id, email=email, lead=lead, source_type="email_resumed",
                               summary="Automatic emails turned back on by you", raw_snapshot={"to": "daily"})
        # Their Showcase fell asleep because they ignored the emails - back on means awake again.
        from app.services.showcase_sleep import wake_no_response
        woke = await wake_no_response(db, owner_id, email)
    else:
        pref.manual, pref.mode, pref.changed_at, pref.reason = "off", "off", _now(), "Turned off by you"
        if lead:
            await log_activity(db, owner_id=owner_id, email=email, lead=lead, source_type="email_paused",
                               summary="Automatic emails turned off by you", raw_snapshot={"to": "off"})
    await db.commit()
    if woke:
        from app.services.showcase_sleep import catch_up
        await catch_up(db, woke)
    return await status_for(db, owner_id, email)


async def set_frequency(db, owner_id: str, email: str, value: str, by: str = "agent") -> dict:
    """Daily, weekly or every two weeks, picked on purpose by the agent or the client. Daily is the
    normal rules with the unopened count starting from now; the other two stay as picked. Picking
    one means they want emails, so an agent's own "off" lifts and a sleeping Showcase wakes.
    A deleted lead's hold and an unsubscribe still win. Commits."""
    if value not in FREQUENCIES:
        raise ValueError("Unknown email frequency")
    email = _norm(email)
    pref = await _pref(db, owner_id, email)
    if pref.manual == LEAD_DELETED:
        return await status_for(db, owner_id, email)
    lead = await _lead(db, owner_id, email)
    pref.frequency = None if value == "daily" else value
    pref.frequency_by = by
    pref.client_notice = None
    pref.manual = None
    pref.reset_at = pref.changed_at = _now()
    pref.mode = value
    words = FREQUENCY_WORDS[value]
    pref.reason = f"{'They' if by == 'client' else 'You'} chose {words} emails"
    from app.services.leads_service import log_activity
    from app.services.showcase_sleep import catch_up, wake_no_response
    if lead is not None:
        await log_activity(db, owner_id=owner_id, email=email, lead=lead, source_type="showcase_emails_on" if by == "client" else "email_resumed",
                           summary=f"Chose {words} Showcase emails" if by == "client" else f"You set their emails to {words}", raw_snapshot={"to": value, "by": by})
    if by == "client":
        name = await _person_name(db, owner_id, email)
        who = _first(name, email)
        db.add(Notification(agent_id=owner_id, type="EMAIL_STATUS", reference_type="CLIENT_EMAIL_PREF", reference_id=email,
                            title=f"{who}: picked {words} emails", message=f"{who} changed their Showcase emails to {words}.", visitor_name=name or None,
                            link=f"/leads?lead={lead.id}" if lead else "/leads/emails", is_read=False, created_at=_now()))
    woke = await wake_no_response(db, owner_id, email)
    await db.commit()
    if woke:
        await catch_up(db, woke)
    return await status_for(db, owner_id, email)


async def dismiss_client_notice(db, owner_id: str, email: str) -> dict:
    pref = await _pref(db, owner_id, _norm(email), create=False)
    if pref is not None and pref.client_notice:
        pref.client_notice = None
        await db.commit()
    return await status_for(db, owner_id, email)


async def set_send_time(db, owner_id: str, email: str, value: str) -> dict:
    """The client picks when their emails arrive (one of SEND_TIMES). Commits."""
    if value not in SEND_TIMES:
        raise ValueError("Unknown send time")
    pref = await _pref(db, owner_id, _norm(email))
    pref.send_time = None if value == DEFAULT_SEND_TIME else value
    await db.commit()
    return await status_for(db, owner_id, email)


async def send_times_for(db, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """{(owner_id, email): "HH:MM"} for the ones that picked a time other than 4:30 PM."""
    if not pairs:
        return {}
    owners = {o for o, _ in pairs}
    rows = (await db.execute(select(ClientEmailPref.owner_id, ClientEmailPref.email, ClientEmailPref.send_time).where(
        ClientEmailPref.owner_id.in_(owners), ClientEmailPref.send_time.isnot(None)))).all()
    want = {(o, _norm(e)) for o, e in pairs}
    return {(o, e): t for o, e, t in rows if (o, e) in want and t in SEND_TIMES}


async def stop_for_deleted_lead(db, owner_id: str, email: Optional[str]) -> bool:
    """Their lead was deleted: no more automatic emails to them (shown as off, with why, on the
    Emails page). An agent's own "off" stays as it is. Doesn't commit."""
    email = _norm(email)
    if not email:
        return False
    pref = await _pref(db, owner_id, email)
    if pref.manual != "off":
        pref.manual, pref.mode, pref.changed_at = LEAD_DELETED, "off", _now()
        pref.reason = "You deleted their lead"
    return True


async def lift_lead_deleted(db, owner_id: str, email: Optional[str]) -> None:
    """They're a lead again (or the agent turned their Showcase back on): emails as normal, the
    unopened count starting over. Only undoes stop_for_deleted_lead. Doesn't commit."""
    email = _norm(email)
    if not email:
        return
    pref = await _pref(db, owner_id, email, create=False)
    if pref is not None and pref.manual == LEAD_DELETED:
        pref.manual, pref.mode, pref.reset_at, pref.changed_at = None, "daily", _now(), _now()
        pref.reason = "Back on - they're one of your leads again"


_HOLD_WHY = {
    "bounced": "Emails to their old address bounced",
    "spam": "They marked an email to their old address as spam - only turn emails back on if they asked you to",
    "unsubscribed": "They unsubscribed at their old address - only turn emails back on if they asked you to",
    "manual_off": "You had turned their emails off",
    "weekly_unopened": "Their old address stopped opening emails",
}


def needs_hold(old_state: Optional[dict]) -> bool:
    """Emails to the old address were off for a reason the agent should decide about again."""
    return bool(old_state) and old_state.get("mode") == "off" and old_state.get("code") in _HOLD_WHY


async def hold_for_new_address(db, owner_id: str, new_email: str, old_state: dict, lead: Optional[Lead] = None) -> bool:
    """The agent changed a client's email while emails to the old address were off: the new address
    starts paused (not silently back on) and the agent is asked whether to turn them back on. An
    off the agent already set on the new address stays as it is. Doesn't commit. -> True when held."""
    new_email = _norm(new_email)
    if not new_email or not needs_hold(old_state):
        return False
    pref = await _pref(db, owner_id, new_email)
    if pref.manual == "off":
        return False
    why = _HOLD_WHY[old_state["code"]]
    pref.manual, pref.mode, pref.changed_at = EMAIL_CHANGED, "off", _now()
    pref.client_notice = None
    pref.reason = f"{why}. Paused at the new address until you turn them back on."
    if lead is not None:
        from app.services.leads_service import log_activity
        await log_activity(db, owner_id=owner_id, email=new_email, lead=lead, source_type="email_paused",
                           summary="Automatic emails paused - new email address", raw_snapshot={"to": "off", "was": old_state["code"]})
    return True


async def listed_emails(db, owner_id: str, emails) -> set[str]:
    """The addresses the Email Tracker shows: the agent's current leads, nobody else (owner, 11/07)."""
    emails = {_norm(e) for e in emails if e}
    if not emails:
        return set()
    return set((await db.execute(select(Lead.email).where(Lead.owner_id == owner_id, Lead.email.in_(emails)))).scalars().all())


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
    problems: dict[str, ClientEmail] = {}   # latest bounced / blocked / marked-as-spam email per person (webhooks)
    for r in (await db.execute(select(ClientEmail).where(ClientEmail.owner_id == owner_id, ClientEmail.created_at >= since,
                                                         ClientEmail.delivery.in_(PROBLEM_DELIVERY)).order_by(ClientEmail.delivery_at.desc()))).scalars().all():
        problems.setdefault(r.recipient_email, r)
    # Someone whose only email bounced is still listed - that's who the agent most needs to see.
    emails = set(by) | set(prefs) | set(problems)
    # A lead whose address is on the do-not-email list but was never emailed shows as blocked, not
    # "Emails on" (11/12).
    from app.models.database import BlacklistedEmail
    emails |= {e for (e,) in (await db.execute(select(Lead.email).join(
        BlacklistedEmail, func.lower(BlacklistedEmail.email) == Lead.email).where(Lead.owner_id == owner_id))).all() if e}
    # A deleted lead disappears from the Email Tracker (user, 10/24) - the hold on their automatic
    # emails stays (prefs keep LEAD_DELETED), they're just not listed until they're a lead again.
    emails -= {e for e, p in prefs.items() if p.manual == LEAD_DELETED}
    # ...and so does anyone who isn't one of their leads at all (deleted before 10/24, or never a lead).
    emails &= await listed_emails(db, owner_id, emails)
    if not emails:
        return {}
    eng = dict((await db.execute(select(Lead.email, func.max(LeadEvent.occurred_at)).join(LeadEvent, LeadEvent.lead_id == Lead.id).where(
        Lead.owner_id == owner_id, Lead.email.in_(emails), LeadEvent.source_type.notin_(_NOT_ENGAGEMENT)).group_by(Lead.email))).all())
    unsub = {e for (e,) in (await db.execute(select(func.lower(BlacklistedEmail.email)).where(func.lower(BlacklistedEmail.email).in_(emails)))).all()}
    out = {}
    for email in emails:
        rs = by.get(email, [])
        p = prefs.get(email)
        opens = [r.opened_at for r in rs if r.opened_at] + [r.clicked_at for r in rs if r.clicked_at]
        last_engaged = max([d for d in (max(opens) if opens else None, eng.get(email)) if d] or [None]) if (opens or eng.get(email)) else None
        d = decide([{"sent_at": r.sent_at, "opened_at": r.opened_at, "clicked_at": r.clicked_at, "template": r.template} for r in rs],
                   manual=p.manual if p else None, reset_at=p.reset_at if p else None, last_engaged=last_engaged, unsubscribed=email in unsub,
                   frequency=p.frequency if p else None, frequency_by=p.frequency_by if p else None)
        pr = problems.get(email)
        if pr is not None and d["code"] == "unsubscribed" and pr.delivery_note:
            d = {**d, "reason": pr.delivery_note, "code": "bounced" if pr.delivery in ("bounced", "blocked") else "spam"}
        if d["code"] == "email_changed" and p is not None and p.reason:
            d = {**d, "reason": p.reason}
        sent90 = [r for r in rs if r.sent_at >= _now() - timedelta(days=90)]
        opened90 = sum(1 for r in sent90 if r.opened_at or r.clicked_at)
        latest = rs[0] if rs else None
        out[email] = {
            "email": email, "mode": d["mode"], "reason": d["reason"], "code": d["code"], "manual": p.manual if p else None,
            "frequency": p.frequency if p else None, "frequency_by": p.frequency_by if p else None,
            "since": p.changed_at.isoformat() if p and p.changed_at and d["mode"] != "daily" else None,
            "sent": len(rs), "opened": sum(1 for r in rs if r.opened_at or r.clicked_at), "clicked": sum(1 for r in rs if r.clicked_at),
            "rate": _rate(len(sent90), opened90), "unopened_in_a_row": d["unopened"],
            "last_sent_at": latest.sent_at.isoformat() if latest else None,
            "last_subject": latest.subject if latest else None,
            "last_template": template_label(latest.template) if latest else None,
            "last_opened_at": max(opens).isoformat() if opens else None,
            "last_clicked_at": max([r.clicked_at for r in rs if r.clicked_at] or [None]).isoformat() if any(r.clicked_at for r in rs) else None,
            # Mailgun said their address bounced / they marked it as spam - shown as a red problem line.
            "problem": {"delivery": pr.delivery, "note": pr.delivery_note, "at": pr.delivery_at.isoformat() if pr.delivery_at else None} if pr else None,
        }
    return out


def email_out(r: ClientEmail) -> dict:
    return {"id": r.id, "to": r.recipient_email, "subject": r.subject, "template": r.template, "label": template_label(r.template),
            "kind": r.kind, "via": r.via, "status": r.status, "error": r.error, "created_at": r.created_at.isoformat() if r.created_at else None,
            "delivery": r.delivery, "delivery_note": r.delivery_note, "delivery_at": r.delivery_at.isoformat() if r.delivery_at else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None, "opened_at": r.opened_at.isoformat() if r.opened_at else None,
            "open_count": r.open_count or 0, "clicked_at": r.clicked_at.isoformat() if r.clicked_at else None, "click_count": r.click_count or 0,
            "last_engaged_at": r.last_engaged_at.isoformat() if r.last_engaged_at else None, "about": r.about or []}
