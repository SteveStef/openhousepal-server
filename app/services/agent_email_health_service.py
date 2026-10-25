"""Agent email health - is each agent actually opening OpenHousePal's emails? (user, 10/24)

Mailbox providers (Gmail, Outlook...) rank a sender by whether people open what it sends. An agent
whose OpenHousePal emails sit unopened - usually because they land in junk - drags our sender score
down for everyone. So, for emails TO agents (email_agent: briefings, alerts, round-ups...):

  tracking  every one carries our own 1x1 open pixel (/e/a/<id>.gif - `track_open`) and its main
            button (`link`) goes through /e/a/<id>/go, which counts as an open too (images blocked).
            The EmailLog row id is the token.
  normal    the usual state.
  warned    FIRST_STRIKE tracked emails in a row unopened (each given GRACE to be opened): in-app
            banner + a bell alert "they may be landing in junk", with a Send-me-a-test-email button.
  weekly    SECOND_STRIKE more unopened after the warning: non-critical emails (DIGESTIBLE) wait
            for one weekly summary. CRITICAL emails - a new lead, a tour / showing request, an RSVP,
            account and billing notices - always go out at once, whatever the state.

Any open or click puts the agent straight back on normal (the count starts again); the agent can
also tap "Turn daily emails back on".

Honest counting:
- A template only counts once at least one open of it has ever been recorded (proven_templates):
  emails whose Mailgun template doesn't carry the pixel yet can never make an agent look like they
  ignore us.
- Apple Mail's privacy protection loads images on delivery, so those agents always look "opened" -
  the safe direction (never a false warning). Mail scanners that fetch within SCANNER_WINDOW of
  sending are ignored.

Never raises into a send: if anything here fails, the email goes out as before."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, unquote

from sqlalchemy import select, update

from app.models.database import AgentEmailHealth, EmailLog, Notification, User

logger = logging.getLogger(__name__)

FIRST_STRIKE = 5            # unopened in a row -> warning
SECOND_STRIKE = 5           # more unopened after the warning -> weekly
GRACE = timedelta(hours=24)  # an email only counts as unopened a day after it was sent
SCANNER_WINDOW = timedelta(seconds=20)
SUMMARY_GAP = timedelta(days=7)
SUMMARY_HOUR = 9            # Eastern - the weekly summary goes out from 9 AM
HELD_CAP = 60
TEST_GAP = timedelta(minutes=10)
LOOKBACK = timedelta(days=90)

SUMMARY_TEMPLATE = "agent_weekly_summary"
TEST_TEMPLATE = "agent_email_test"
NOTIFY_TYPE = "EMAIL_HEALTH"

# ---------------------------------------------------------------- which emails can wait (ONE place)
# CRITICAL: always sent the moment they happen, even to an agent on weekly. A client or lead did
# something / is waiting on the agent, or it's about their account, money or security.
CRITICAL_TEMPLATES = frozenset({
    # leads + client activity that needs an answer
    "new_lead_captured", "form_submission_received", "open_house_form_request", "open_house_recap",
    "tour_request", "similar_property_visitor_tour_request", "similar_property_visitor_message",
    "property_comment", "refine_search_request", "invite_rsvp_agent", "invite_day_before_agent",
    # hosting requests between agents (time-sensitive: the open house is days away)
    "direct_host_request", "host_request_received", "host_request_approved", "host_request_countered",
    "host_request_declined", "host_request_update", "team_broadcast_received", "team_invite_received",
    # account, billing, security, things that broke
    "password_reset", "verify_code", "account_approved", "agent_welcome", "payment_failed",
    "subscription_cancelled", "subscription_suspended", "subscription_plan_changed", "trial_ending_soon",
    "domain_verified", "domain_verification_failed", "domain_update", "calendar_disconnected",
    "crm_push_failing", "support_ticket_reply",
    # this system's own emails
    SUMMARY_TEMPLATE, TEST_TEMPLATE,
})
# DIGESTIBLE: fine to wait for the weekly summary (the bell still shows each one in the app).
DIGESTIBLE_TEMPLATES = frozenset({
    "showcase_daily_digest_agent", "liked_homes_roundup", "spotlight_weekly_report", "studio_media_ready",
    "studio_media_failed", "team_new_listing", "new_review_received", "team_broadcast_claimed",
    "team_invite_accepted", "my_listing_updated",
})

LABELS = {
    "showcase_daily_digest_agent": "Daily briefing", "liked_homes_roundup": "Liked-homes round-up",
    "spotlight_weekly_report": "Spotlight weekly report", "studio_media_ready": "Studio video ready",
    "studio_media_failed": "Studio video couldn't finish", "team_new_listing": "Teammate's new listing",
    "new_review_received": "New review", "team_broadcast_claimed": "Broadcast claimed",
    "team_invite_accepted": "Team invite accepted", "my_listing_updated": "Listing updated",
    "new_lead_captured": "New lead", "form_submission_received": "Form response", "open_house_recap": "Open house wrap-up",
    "open_house_form_request": "Open house request", "invite_rsvp_agent": "Invite RSVP", "invite_day_before_agent": "Event tomorrow",
    "tour_request": "Tour request", "property_comment": "Client comment", "refine_search_request": "Search change request",
    SUMMARY_TEMPLATE: "Weekly summary (email health)", TEST_TEMPLATE: "Test email (email health)",
}


def label(template: str) -> str:
    return LABELS.get(template) or template.replace("_v2", "").replace("_", " ").capitalize()


def is_critical(template: str, always: bool = False) -> bool:
    """Never held back. Unknown templates count as critical - missing a lead is worse than one extra email."""
    return always or template in CRITICAL_TEMPLATES or template not in DIGESTIBLE_TEMPLATES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _api_base() -> str:
    from app.services.client_email_service import api_base
    return api_base()


# ---------------------------------------------------------------- tracking variables

def with_tracking(log_id: str, variables: dict) -> dict:
    """The open pixel + the main button through our click counter. Only app links are wrapped, and
    the redirect only ever goes back to the app (no open redirect)."""
    from app.services.agent_email_service import app_url
    out = dict(variables)
    base = _api_base()
    out["track_open"] = f"{base}/e/a/{log_id}.gif"
    link = out.get("link")
    home = app_url()
    if isinstance(link, str) and (link == home or link.startswith(home + "/")):
        path = link[len(home):] or "/"
        out["_orig_link"] = link
        out["link"] = f"{base}/e/a/{log_id}/go?to={quote(path, safe='')}"
    return out


def safe_target(to: Optional[str]) -> str:
    """/e/a/<id>/go?to=<path> -> an address in the app, never anywhere else."""
    from app.services.agent_email_service import app_url
    p = unquote(to or "")
    if not p.startswith("/") or p.startswith("//") or "\\" in p or any(c in p for c in "\r\n\t"):
        p = "/"
    return app_url() + p


# ---------------------------------------------------------------- the decision (pure, testable)

def decide(emails: list[dict], *, mode: str = "normal", warned_at: Optional[datetime] = None, weekly_at: Optional[datetime] = None,
           reset_at: Optional[datetime] = None, proven: Optional[set] = None, now: Optional[datetime] = None) -> dict:
    """emails: this agent's emails [{sent_at, opened_at, clicked_at, template, tracked}].
    -> {mode, unopened, after_warning, code}"""
    now = now or _now()
    opens = [e.get("opened_at") or e.get("clicked_at") for e in emails if e.get("opened_at") or e.get("clicked_at")]
    marks = [d for d in (reset_at, max(opens) if opens else None) if d]
    start = max(marks) if marks else None
    unopened = [e for e in emails
                if e.get("tracked") and e.get("sent_at") and (proven is None or e.get("template") in proven)
                and e["sent_at"] <= now - GRACE and (start is None or e["sent_at"] > start)
                and not e.get("opened_at") and not e.get("clicked_at")]
    n = len(unopened)
    engaged_since = lambda t: bool(t and start and start > t)  # noqa: E731
    after = [e for e in unopened if warned_at and e["sent_at"] > warned_at]
    if mode == "weekly" and weekly_at and not engaged_since(weekly_at):
        return {"mode": "weekly", "unopened": n, "after_warning": len(after), "code": "weekly"}
    if mode in ("warned", "weekly") and warned_at and not engaged_since(warned_at):
        if len(after) >= SECOND_STRIKE:
            return {"mode": "weekly", "unopened": n, "after_warning": len(after), "code": "weekly"}
        return {"mode": "warned", "unopened": n, "after_warning": len(after), "code": "warned"}
    if n >= FIRST_STRIKE:
        return {"mode": "warned", "unopened": n, "after_warning": 0, "code": "warned"}
    return {"mode": "normal", "unopened": n, "after_warning": 0, "code": "ok"}


# ---------------------------------------------------------------- reading state

_proven_cache: dict[str, Any] = {"at": 0.0, "set": set()}


async def proven_templates(db, *, fresh: bool = False) -> set:
    """Agent templates we've seen at least one open of - their pixel provably works."""
    if not fresh and _proven_cache["set"] and time.monotonic() - _proven_cache["at"] < 600:
        return _proven_cache["set"]
    rows = (await db.execute(select(EmailLog.template).where(
        EmailLog.audience == "agent", EmailLog.tracked.is_(True), EmailLog.opened_at.isnot(None),
        EmailLog.created_at >= _now() - LOOKBACK * 2).distinct())).scalars().all()
    _proven_cache.update(at=time.monotonic(), set=set(rows))
    return _proven_cache["set"]


async def _emails_for(db, user_id: str) -> list[dict]:
    rows = (await db.execute(select(EmailLog.created_at, EmailLog.opened_at, EmailLog.clicked_at, EmailLog.template, EmailLog.tracked).where(
        EmailLog.owner_id == user_id, EmailLog.audience == "agent", EmailLog.status != "failed",
        EmailLog.created_at >= _now() - LOOKBACK).order_by(EmailLog.created_at.desc()).limit(300))).all()
    return [{"sent_at": r[0], "opened_at": r[1], "clicked_at": r[2], "template": r[3], "tracked": r[4]} for r in rows]


async def _row(db, user_id: str, create: bool = False) -> Optional[AgentEmailHealth]:
    h = await db.get(AgentEmailHealth, user_id)
    if h is None and create:
        # Two requests at once (the Dash banner + an email going out) may both create it - one wins.
        from sqlalchemy.dialects.postgresql import insert
        await db.execute(insert(AgentEmailHealth).values(user_id=user_id, mode="normal", held_total=0)
                         .on_conflict_do_nothing(index_elements=["user_id"]))
        h = await db.get(AgentEmailHealth, user_id, populate_existing=True)
    return h


def _bell(db, user_id: str, title: str, message: str, ref: str) -> None:
    db.add(Notification(agent_id=user_id, type=NOTIFY_TYPE, reference_type="EMAIL_HEALTH", reference_id=ref,
                        title=title, message=message, link="/dash#email-health", is_read=False, created_at=_now()))


async def refresh(db, user_id: str, *, announce: bool = True) -> dict:
    """Work out normal / warned / weekly now; on a change, record it (and tell the agent once). Commits."""
    from app.services import email_log_service
    await email_log_service.flush()
    h = await _row(db, user_id)
    d = decide(await _emails_for(db, user_id), mode=h.mode if h else "normal", warned_at=h.warned_at if h else None,
               weekly_at=h.weekly_at if h else None, reset_at=h.reset_at if h else None, proven=await proven_templates(db))
    old = h.mode if h else "normal"
    if d["mode"] != old:
        h = h or await _row(db, user_id, create=True)
        now = _now()
        h.mode, h.changed_at, h.dismissed_at = d["mode"], now, None
        if d["mode"] == "warned":
            h.warned_at = now
            if announce:
                _bell(db, user_id, "Are our emails landing in junk?",
                      f"We've emailed you, but you haven't opened our last {FIRST_STRIKE} emails. They may be landing in junk/spam - "
                      "please find one, mark it Not spam and move it to your inbox.", f"warned|{now.date().isoformat()}")
        elif d["mode"] == "weekly":
            h.weekly_at = now
            if announce:
                _bell(db, user_id, "Your OpenHousePal emails are now weekly",
                      "You haven't opened our recent emails, so routine ones (briefings, round-ups) now come as one weekly summary. "
                      "New leads, tour requests and account emails still arrive right away. Tap to turn daily emails back on.",
                      f"weekly|{now.date().isoformat()}")
        else:
            h.warned_at = h.weekly_at = None
        await db.commit()
    return {**d, "warned_at": h.warned_at if h else None, "weekly_at": h.weekly_at if h else None,
            "dismissed_at": h.dismissed_at if h else None, "changed_at": h.changed_at if h else None,
            "held": len(h.held or []) if h else 0, "last_test_at": h.last_test_at if h else None}


async def _back_to_normal(db, user_id: str, why: str) -> None:
    """An open / click / the agent's own tap: back to normal, the count starting from now. Commits."""
    h = await _row(db, user_id)
    now = _now()
    if h is None:
        return
    was = h.mode
    h.reset_at = now
    if was != "normal":
        h.mode, h.changed_at, h.warned_at, h.weekly_at, h.dismissed_at = "normal", now, None, None, None
        h.held = None   # each of them is in the bell already
        # the warning / weekly alerts are answered now
        await db.execute(update(Notification).where(Notification.agent_id == user_id, Notification.type == NOTIFY_TYPE,
                                                    Notification.is_read.is_(False)).values(is_read=True, read_at=now))
        if was == "weekly":
            _bell(db, user_id, "Daily emails are back on", f"{why} - your OpenHousePal emails are back to normal.", f"resumed|{now.isoformat()}")
    await db.commit()


# ---------------------------------------------------------------- opens + clicks

async def record_open(db, log_id: str, *, click_to: Optional[str] = None) -> None:
    from app.services import email_log_service
    row = await db.get(EmailLog, log_id)
    if row is None:
        await email_log_service.flush()
        row = await db.get(EmailLog, log_id)
    if row is None or row.audience != "agent" or not row.tracked or not row.owner_id:
        return
    now = _now()
    if row.created_at and now - row.created_at < SCANNER_WINDOW:
        return   # a mail scanner fetching it on arrival, not the agent
    first = row.opened_at is None and row.clicked_at is None
    row.open_count = (row.open_count or 0) + 1
    if row.opened_at is None:
        row.opened_at = now
    if click_to is not None and row.clicked_at is None:
        row.clicked_at = now
    await db.commit()
    if first:
        if row.template not in _proven_cache["set"]:
            _proven_cache["at"] = 0.0
        await _back_to_normal(db, row.owner_id, "You opened one of our emails")


# ---------------------------------------------------------------- the send path

async def should_hold(user_id: str, template: str, subject: str, variables: dict, always: bool = False) -> bool:
    """True = this one waits for the weekly summary (and is noted for it). Refreshes the state on
    every agent email, so warnings fire as emails go out."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            st = await refresh(db, user_id)
            if st["mode"] != "weekly" or is_critical(template, always):
                return False
            h = await _row(db, user_id, create=True)
            item = {"t": template, "s": (subject or "")[:200], "l": variables.get("_orig_link") or variables.get("link"), "at": _now().isoformat()}
            h.held = ([item] + list(h.held or []))[:HELD_CAP]
            h.held_total = (h.held_total or 0) + 1
            await db.commit()
            logger.info("agent email held for the weekly summary", extra={"template": template, "user_id": user_id})
            return True
    except Exception:  # noqa: BLE001 - never stop an email because of this
        logger.warning("agent email health check failed - sending", exc_info=True)
        return False


async def send_gated(user_id: str, to: str, template: str, subject: str, variables: dict, reply_to: Optional[str], always: bool) -> None:
    # One of each kind per person per event per day (email_dedupe_service) - keyed on the real link,
    # not the per-email tracked one.
    from app.services import email_dedupe_service as dd
    key = dd.key_for(to, template, subject, {**variables, "link": variables.get("_orig_link") or variables.get("link")})
    if await dd.is_repeat(key, template, "agent", user_id):
        return
    if await should_hold(user_id, template, subject, variables, always):
        dd.forget(key)
        return
    from app.services.agent_email_service import send
    out = {k: v for k, v in variables.items() if k != "_orig_link"}
    if key:
        out["_dedupe_key"] = key
    send(to, template, subject, out, reply_to=reply_to)


# ---------------------------------------------------------------- agent controls

def status_out(st: dict, user) -> dict:
    mode = st["mode"]
    show = mode != "normal" and not (st.get("dismissed_at") and st.get("changed_at") and st["dismissed_at"] >= st["changed_at"])
    last_test = st.get("last_test_at")
    return {"mode": mode, "unopened": st["unopened"], "after_warning": st["after_warning"], "show_banner": show,
            "warned_at": st.get("warned_at"), "weekly_at": st.get("weekly_at"), "held": st.get("held", 0),
            "email": getattr(user, "email", None), "first_strike": FIRST_STRIKE, "second_strike": SECOND_STRIKE,
            "test_sent_at": last_test, "can_test": not last_test or _now() - last_test >= TEST_GAP}


async def status(db, user) -> dict:
    return status_out(await refresh(db, user.id), user)


async def dismiss(db, user) -> dict:
    h = await _row(db, user.id, create=True)
    h.dismissed_at = _now()
    await db.commit()
    return await status(db, user)


async def restore(db, user) -> dict:
    await _row(db, user.id, create=True)
    await _back_to_normal(db, user.id, "You turned daily emails back on")
    return await status(db, user)


async def send_test(db, user) -> dict:
    """"Send me a test email": one tracked email the agent can look for (and rescue from junk)."""
    h = await _row(db, user.id, create=True)
    now = _now()
    if h.last_test_at and now - h.last_test_at < TEST_GAP:
        return {**(await status(db, user)), "sent": False}
    h.last_test_at = now
    await db.commit()
    from app.services.agent_email_service import absolute, email_agent
    email_agent(user, None, TEST_TEMPLATE, "Test email from OpenHousePal - you found it!", {"link": absolute("/dash?email_test=1")}, always=True)
    return {**(await status(db, user)), "sent": True}


async def send_weekly_summaries(db) -> int:
    """Every minute from task_reminder_scheduler: agents on weekly with emails waiting get one
    summary a week (from SUMMARY_HOUR Eastern). Claimed with one UPDATE so it never goes twice."""
    from app.services.tasks_service import EASTERN
    now = _now()
    if now.astimezone(EASTERN).hour < SUMMARY_HOUR:
        return 0
    due = (await db.execute(select(AgentEmailHealth.user_id).where(
        AgentEmailHealth.mode == "weekly", AgentEmailHealth.held.isnot(None),
        (AgentEmailHealth.last_summary_at.is_(None) & (AgentEmailHealth.weekly_at <= now - SUMMARY_GAP))
        | (AgentEmailHealth.last_summary_at <= now - SUMMARY_GAP)))).scalars().all()
    sent = 0
    for uid in due:
        claimed = (await db.execute(update(AgentEmailHealth).where(
            AgentEmailHealth.user_id == uid, AgentEmailHealth.mode == "weekly",
            (AgentEmailHealth.last_summary_at.is_(None)) | (AgentEmailHealth.last_summary_at <= now - SUMMARY_GAP))
            .values(last_summary_at=now).returning(AgentEmailHealth.held))).first()
        await db.commit()
        if not claimed or not claimed[0]:
            continue
        user = await db.get(User, uid)
        if user is None or not user.email:
            continue
        items = list(claimed[0])
        await db.execute(update(AgentEmailHealth).where(AgentEmailHealth.user_id == uid).values(held=None))
        await db.commit()
        from app.services.agent_email_service import absolute, email_agent
        email_agent(user, None, SUMMARY_TEMPLATE, f"Your OpenHousePal week: {len(items)} update{'s' if len(items) != 1 else ''}",
                    {"items": items, "link": absolute("/dash"), "restore_link": absolute("/dash#email-health")}, always=True)
        sent += 1
    return sent


# ---------------------------------------------------------------- our two emails (rendered here)

def render(template: str, subject: str, v: dict) -> tuple[str, str, str]:
    from app.services import invite_email_service as ui
    app = v.get("app_url") or ""
    if template == TEST_TEMPLATE:
        title = "You found our test email"
        body = [ui.eyebrow("Email check"), ui.h1(title),
                ui.para("If this landed in <strong>junk or spam</strong>: mark it <strong>Not spam</strong> and move it to your inbox. "
                        "That one tap teaches your email to trust OpenHousePal - new leads and tour requests will land where you see them."),
                ui.para("Tap the button below so we know it arrived - your emails go straight back to normal.", top=12),
                ui.button("Yes, I got it", v.get("link") or app)]
        text = [title, "If this landed in junk or spam, mark it Not spam and move it to your inbox.", f"Let us know it arrived: {v.get('link') or app}"]
        html = ui.shell(title=title, preheader="Mark us Not spam so your leads land in your inbox.", header_cells=ui.ohp_header("Email check"),
                        body="".join(body), footer="You asked for this test email from your OpenHousePal Dash.")
        return subject, html, "\n".join(text)
    items = v.get("items") or []
    counts: dict[str, int] = {}
    for it in items:
        counts[label(it.get("t") or "")] = counts.get(label(it.get("t") or ""), 0) + 1
    title = f"Your week: {len(items)} update{'s' if len(items) != 1 else ''}"
    summary = ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    lines = []
    for it in items[:12]:
        try:
            d = datetime.fromisoformat(it["at"])
            when = f"{d.strftime('%a %b')} {d.day}"
        except Exception:  # noqa: BLE001
            when = ""
        s = ui.e(it.get("s") or label(it.get("t") or ""))
        s = ui.a_link(it.get("s") or label(it.get("t") or ""), it["l"]) if it.get("l") else s
        lines.append(f'<div style="padding:8px 0;border-bottom:1px solid #ede4d3;font-size:14.5px;line-height:1.45;" class="ohp-text">{s}'
                     f'<span style="color:{ui.MUTED};" class="ohp-muted"> &middot; {ui.e(when)}</span></div>')
    body = [ui.eyebrow("Weekly summary"), ui.h1(title),
            ui.para(f"You haven't been opening our emails lately, so routine ones now come together once a week: {ui.e(summary)}."),
            ui.card("".join(lines)) if lines else "",
            ui.para("New leads, tour requests and account emails still reach you right away.", top=16, muted=True),
            ui.button("Open OpenHousePal", v.get("link") or app),
            ui.para(f'Want them as they happen again? {ui.a_link("Turn daily emails back on", v.get("restore_link") or app)} '
                    "- or just open any of our emails.", top=18, center=True, size=13.5)]
    text = [title, summary] + [f"- {it.get('s') or label(it.get('t') or '')}" for it in items[:12]] + [f"Open OpenHousePal: {v.get('link') or app}"]
    html = ui.shell(title=title, preheader=summary[:120], header_cells=ui.ohp_header("This week"), body="".join(body),
                    footer=ui._settings_footer(app))
    return subject, html, "\n".join(text)
