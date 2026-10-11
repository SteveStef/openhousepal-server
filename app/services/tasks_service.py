"""Agent tasks / follow-up reminders.

The agent works these from the Leads page ("Follow-ups" tab) and a dashboard widget. A task can
be tied to a lead (shows on the lead profile, can carry a suggested outreach message). Some are
created automatically - notably a next-day "follow up" task the moment an open house sign-in
creates or updates a lead. Everything here is also exposed read-only through the agent's private
iCalendar (.ics) feed so it lands on their phone calendar with no OAuth.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from app.models.database import AgentTask, Lead, Notification, OpenHouseEvent, User

logger = logging.getLogger(__name__)

# Bright MLS clock times (open-house scheduled_start_time/end_time) are always Eastern
# wall-clock time regardless of where the property sits - anchor here rather than treat as UTC.
EASTERN = ZoneInfo("America/New_York")

# When an open house sign-in makes a lead, the follow-up task is due the NEXT day at this hour,
# Eastern time (a per-agent timezone is a later refinement - every agent today is Eastern).
FOLLOWUP_DUE_HOUR = 9


# --------------------------------------------------------------------------- automatic follow-up rules
# Every situation that drops a follow-up on the agent's list (and calendar) by itself. Each can be
# switched off per agent (User.followup_rules = {"<key>": false}); a missing key means ON.
# `tiers` = the plans the triggering feature exists on (None = every plan) - the Follow-ups page
# only lists rules the agent's plan can actually fire.
AUTO_FOLLOWUP_RULES: list[dict] = [
    {"key": "open_house_signin", "label": "Open house sign-in", "timing": "Next day",
     "description": "Every guest without an agent who signs in at your open houses - plus any guest you leave a note on, even one who has an agent.", "tiers": None},
    {"key": "similar_homes_qr", "label": "Similar Homes QR scan", "timing": "Next day",
     "description": "A guest without an agent scans the QR on your Similar Homes handout to see more homes like the one from your open house.", "tiers": None},
    {"key": "second_visit_request", "label": "Second visit request", "timing": "Right away",
     "description": "A guest requests a second visit to a home from your recent open house. The message quotes the day and time they picked.", "tiers": None},
    {"key": "disclosure_request", "label": "Disclosure request", "timing": "Right away",
     "description": "A guest asks for the seller disclosure - from their Guest Pass, the QR on your Cheat Sheet, or your second-visit form. Their email is ready to copy, plus a short text to send once you've emailed it.", "tiers": None},
    {"key": "showcase_setup", "label": "Setup My Own Showcase", "timing": "Next day",
     "description": "A guest fills out your Setup My Own Showcase form and it builds their own Showcase - whether they scanned the QR on your Brag Sheet or opened the form's link.", "tiers": "showcase"},
    {"key": "buyer_form", "label": "I'm Buying form", "timing": "Right away",
     "description": "Someone without an agent fills out your I'm Buying form - a Showcase is built for them quietly from their answers.", "tiers": None},
    {"key": "how_can_we_help", "label": "How Can We Help form", "timing": "Right away",
     "description": "Someone without an agent fills out your How Can We Help form. Buyers and sellers each get their own message - sellers are flagged as a seller lead.", "tiers": None},
    {"key": "form_submission", "label": "Any other form", "timing": "Right away",
     "description": "Someone submits one of your other forms (onboarding, seller, home value, custom).", "tiers": None},
    {"key": "showing_request", "label": "Showing request", "timing": "Right away",
     "description": "Someone asks to tour a home - from their Showcase, a Spotlight site or your Schedule a Showing form. The message quotes every day and time they picked.", "tiers": None},
    {"key": "showcase_comment", "label": "Showcase comment", "timing": "Next day",
     "description": "A client comments on a home in their Showcase. You get an alert with what they said, which home and a link to it - reply in your own words.", "tiers": "showcase"},
    {"key": "showcase_search_request", "label": "Showcase search change", "timing": "Right away",
     "description": "A client messages you from their Showcase asking you to change their search. You get an alert with exactly what they asked for and a link to their Showcase - update it, then let them know.", "tiers": "showcase"},
    {"key": "listing_inquiry", "label": "Listing inquiry", "timing": "Right away",
     "description": "Someone asks about one of your listings - through a Spotlight listing site, or a Zillow, Realtor.com or other portal lead sent to your OpenHousePal leads address.", "tiers": None},
    {"key": "private_feedback", "label": "Unhappy review (1-3 stars)", "timing": "Right away",
     "description": "A client leaves private 1-3 star feedback through your review link.", "tiers": "reviews"},
]
# Plans without Spotlight sites only get portal leads here - say just that.
_NO_IDX_DESCRIPTION = {
    "listing_inquiry": "A Zillow, Realtor.com or other portal lead reaches your OpenHousePal leads address.",
}
# Situations that used to be one (or two) others: their on/off, day and alert-style settings carry
# over until the agent changes the new one. (Messages don't - the new ones say something different.)
_LEGACY_SETTINGS: dict[str, tuple[str, ...]] = {
    "second_visit_request": ("open_house_request",),
    "disclosure_request": ("open_house_request",),
    "listing_inquiry": ("spotlight_inquiry", "portal_lead"),
}
# An alert for the agent, not a message to send: fixed wording, nothing to edit or copy.
ALERT_ONLY_RULES = {"showcase_comment", "showcase_search_request", "private_feedback"}
# Situations whose card notes are a fixed "what to do" (the disclosure also gets a message to send).
ALERT_NOTE_RULES = ALERT_ONLY_RULES | {"disclosure_request"}
# A concrete ask from the person (send the disclosure / book the showing) - always gets its own
# follow-up, even while a general "reach out" one is still open for them.
ACTION_REQUEST_RULES = {"disclosure_request", "second_visit_request", "showing_request", "showcase_search_request"}
# Forms that mean "I might sell" - a known buyer filling one in gets its own follow-up.
SELLER_FORM_TYPES = {"home_value", "selling", "make_me_sell", "seller_onboarding"}
# "Book the showing" follow-ups - the alert and card say to book it in ShowingTime first (with the
# MLS# to search there).
SHOWING_RULES = {"second_visit_request", "showing_request"}
# How Can We Help, seller side: its own message + clearly marked as a seller lead.
SELLER_SOURCE = "how_can_we_help_seller"
# The part of a message the agent must fill in before sending (the showing they booked).
FILL_IN_TIME = "[ADD DATE & TIME]"
# Left in a message where the agent answers the question the person asked (Spotlight / listing inquiries).
FILL_IN_ANSWER = "[ANSWER THEIR QUESTION]"
_RULE_KEYS = {r["key"] for r in AUTO_FOLLOWUP_RULES}

# The Follow-ups page lists the situations under these headings, in this order.
RULE_GROUPS: list[dict] = [
    {"key": "open_house", "label": "Open houses",
     "description": "Guests from your open houses and the scannable sheets in your kit.",
     "rules": ["open_house_signin", "similar_homes_qr", "second_visit_request", "disclosure_request"]},
    {"key": "forms", "label": "Form responses",
     "description": "Someone fills out one of your forms. The first three build the guest a Showcase.",
     "rules": ["showcase_setup", "buyer_form", "how_can_we_help", "form_submission"]},
    {"key": "other", "label": "Showcases, listings & reviews",
     "description": "Activity on your Showcases, listing sites, portals and review link.",
     "rules": ["showing_request", "showcase_comment", "showcase_search_request", "listing_inquiry", "private_feedback"]},
]
_RULE_GROUP = {k: g["key"] for g in RULE_GROUPS for k in g["rules"]}

# LeadEvent.source_type -> (rule key, days after the touch point it's due). Open house sign-ins are
# NOT here: the sign-in route calls create_open_house_followup itself (it knows the visitor id).
_SOURCE_RULES: dict[str, tuple[str, int]] = {
    "open_house_disclosure": ("disclosure_request", 0),
    "open_house_second_visit": ("second_visit_request", 0),
    "form_submission": ("form_submission", 0),
    "portal_lead": ("listing_inquiry", 0),
    "property_tour": ("showing_request", 0),
    "property_website_tour_request": ("showing_request", 0),
    "property_comment": ("showcase_comment", 1),
    "showcase_refine": ("showcase_search_request", 0),
    "property_website_inquiry": ("listing_inquiry", 0),
    "property_page_inquiry": ("listing_inquiry", 0),
    "property_page_tour_request": ("showing_request", 0),
    # A reader's question from a blog article's contact form.
    "blog_inquiry": ("form_submission", 0),
}

# form_submission events from these premade forms get their own situation (own switch, message
# and Showcase pitch); every other form stays under "form_submission".
FORM_TYPE_RULES: dict[str, str] = {
    "showing": "showing_request",
    "showcase_request": "showcase_setup",
    "buying": "buyer_form",
    "how_can_i_help": "how_can_we_help",
}

# The only situations whose message pitches the guest's Showcase - the ones that actually MAKE a
# Showcase for the person. "reminder" = they know about it already (signed in / built it
# themselves), so the pitch is a thank-you + nudge; "intro" = it was built quietly from their form
# answers, so the pitch introduces it.
PITCH_RULES: dict[str, str] = {
    "open_house_signin": "reminder",
    "similar_homes_qr": "reminder",
    "showcase_setup": "reminder",
    "buyer_form": "intro",
    "how_can_we_help": "intro",
}


def rule_available(rule: dict, plan_tier: Optional[str]) -> bool:
    from app.utils.auth import REVIEWS_ACCESS_TIERS, SHOWCASE_ACCESS_TIERS
    need = rule.get("tiers")
    if need is None:
        return True
    if need == "showcase":
        return plan_tier in SHOWCASE_ACCESS_TIERS
    if need == "idx":
        return plan_tier == "IDX"
    if need == "reviews":
        return plan_tier in REVIEWS_ACCESS_TIERS
    return True


# Per-rule agent settings live in User.followup_rules[key]. Older rows stored a bare bool (on/off);
# now it's {"enabled": bool, "when": "same_day"|"next_day", "style": "timed"|"all_day"}.
WHEN_CHOICES = ("same_day", "next_day")
STYLE_CHOICES = ("timed", "all_day")
_DEFAULT_WHEN = {r["key"]: ("next_day" if r["timing"] == "Next day" else "same_day") for r in AUTO_FOLLOWUP_RULES}


def rule_settings(user: Optional[User], key: str) -> dict:
    out = {"enabled": True, "when": _DEFAULT_WHEN.get(key, "same_day"), "style": "timed",
           "template": DEFAULT_TEMPLATES.get(key, ""), "custom_template": False,
           "pitch": DEFAULT_PITCHES.get(key, ""), "custom_pitch": False}
    if key == "how_can_we_help":
        out.update({"seller_template": SELLER_TEMPLATE, "custom_seller_template": False})
    prefs = getattr(user, "followup_rules", None) if user is not None else None
    val = prefs.get(key) if isinstance(prefs, dict) else None
    if val is None and isinstance(prefs, dict):
        # A situation split out of / merged from older ones: start from the old switch settings.
        old = next((prefs[k] for k in _LEGACY_SETTINGS.get(key, ()) if k in prefs), None)
        if isinstance(old, bool):
            val = old
        elif isinstance(old, dict):
            val = {k: old[k] for k in ("enabled", "when", "style") if k in old}
    if isinstance(val, bool):
        out["enabled"] = val
    elif isinstance(val, dict):
        if "enabled" in val:
            out["enabled"] = bool(val["enabled"])
        if val.get("when") in WHEN_CHOICES:
            out["when"] = val["when"]
        if val.get("style") in STYLE_CHOICES:
            out["style"] = val["style"]
        tpl = val.get("template")
        if isinstance(tpl, str) and tpl.strip() and tpl.strip() not in _RETIRED_DEFAULTS:
            out["template"] = tpl
            out["custom_template"] = True
        pitch = val.get("pitch")
        if key in PITCH_RULES and isinstance(pitch, str) and pitch.strip() and pitch.strip() not in _RETIRED_PITCHES:
            out["pitch"] = pitch
            out["custom_pitch"] = True
        seller = val.get("seller_template")
        if key == "how_can_we_help" and isinstance(seller, str) and seller.strip():
            out["seller_template"] = seller
            out["custom_seller_template"] = True
    if key in ALERT_ONLY_RULES:
        out["template"], out["custom_template"] = "", False
    return out


# Master switch (like the IDX website switch): an agent who doesn't use Follow-ups turns the whole
# feature off - no automatic follow-ups are made and it drops out of the Dash/briefing. Stored as
# User.followup_rules["_enabled"] = False (no migration); missing means ON. Each situation's own
# on/off is kept untouched underneath, so turning it back on restores exactly what they had.
def followups_enabled(user: Optional[User]) -> bool:
    prefs = getattr(user, "followup_rules", None) if user is not None else None
    return not (isinstance(prefs, dict) and prefs.get("_enabled") is False)


def rule_enabled(user: Optional[User], key: str) -> bool:
    return followups_enabled(user) and rule_settings(user, key)["enabled"]


def rules_payload(user: User) -> list[dict]:
    tier = getattr(user, "plan_tier", None)
    tier = getattr(tier, "value", tier)
    out = []
    for r in AUTO_FOLLOWUP_RULES:
        if not rule_available(r, tier):
            continue
        description = r["description"] if tier == "IDX" else _NO_IDX_DESCRIPTION.get(r["key"], r["description"])
        row = {"key": r["key"], "label": r["label"], "description": description, "group": _RULE_GROUP.get(r["key"], "other"),
               "default_when": _DEFAULT_WHEN[r["key"]], **rule_settings(user, r["key"]),
               **template_payload(r["key"], user)}
        if r["key"] in ALERT_ONLY_RULES:
            row["alert_only"] = {"sample": ALERT_SAMPLES[r["key"]]}
        if r["key"] == "how_can_we_help":
            row["seller"] = {
                "default_template": SELLER_TEMPLATE,
                "tokens": _token_list(SELLER_TOKENS, user),
            }
        if row["pitch_style"] is None:
            row["template"] = _without_showcase(row["template"])
            row.pop("pitch", None)
            row.pop("custom_pitch", None)
        if r["key"] == NOTED_GUEST_RULE:
            row["background_alert"] = {
                "label": "Guest with an agent + your note",
                "description": ("When you leave a note on a guest who has an agent, you get this heads-up instead - "
                                "no Showcase, no pitch, nothing to send. It's automatic and can't be edited."),
                "sample": noted_guest_sample(user),
            }
        out.append(row)
    return out


def merge_rules(existing: Optional[dict], payload: dict) -> dict:
    """Apply a client update ({key: bool | {enabled?, when?, style?}}) over what's stored,
    keeping only known rules/fields/values. Always writes the dict form."""
    merged = dict(existing or {})
    for key, val in (payload or {}).items():
        if key not in _RULE_KEYS:
            continue
        cur = merged.get(key)
        cur = {"enabled": cur} if isinstance(cur, bool) else dict(cur or {})
        if isinstance(val, bool):
            cur["enabled"] = val
        elif isinstance(val, dict):
            if "enabled" in val:
                cur["enabled"] = bool(val["enabled"])
            if val.get("when") in WHEN_CHOICES:
                cur["when"] = val["when"]
            if val.get("style") in STYLE_CHOICES:
                cur["style"] = val["style"]
            if "template" in val:
                tpl = val["template"] if isinstance(val["template"], str) else ""
                tpl = tpl.replace("\r\n", "\n").strip()[:TEMPLATE_MAX]
                # Empty or identical to the default = back to the default (so future default
                # improvements still reach agents who never really customised it).
                # (The default with the Showcase pill taken out is what a plan without Showcases
                # is shown, so saving that unchanged is a reset too.)
                if not tpl or tpl in (DEFAULT_TEMPLATES.get(key, "").strip(), _without_showcase(DEFAULT_TEMPLATES.get(key, ""))):
                    cur.pop("template", None)
                else:
                    cur["template"] = tpl
            if "seller_template" in val and key == "how_can_we_help":
                tpl = val["seller_template"] if isinstance(val["seller_template"], str) else ""
                tpl = tpl.replace("\r\n", "\n").strip()[:TEMPLATE_MAX]
                if not tpl or tpl == SELLER_TEMPLATE.strip():
                    cur.pop("seller_template", None)
                else:
                    cur["seller_template"] = tpl
            if key in ALERT_ONLY_RULES:
                cur.pop("template", None)  # an alert - nothing to customise
            if "pitch" in val and key in PITCH_RULES:
                pitch = val["pitch"] if isinstance(val["pitch"], str) else ""
                pitch = pitch.replace("\r\n", "\n").strip()[:TEMPLATE_MAX]
                if not pitch or pitch == DEFAULT_PITCHES.get(key, "").strip():
                    cur.pop("pitch", None)
                else:
                    cur["pitch"] = pitch
        merged[key] = cur
    return merged


# Google Calendar's event colours (colorId -> name, hex) - the agent picks one for follow-ups.
CALENDAR_COLORS: dict[str, tuple[str, str]] = {
    "5": ("Banana", "#f6bf26"), "6": ("Tangerine", "#f4511e"), "11": ("Tomato", "#d50000"),
    "4": ("Flamingo", "#e67c73"), "3": ("Grape", "#8e24aa"), "1": ("Lavender", "#7986cb"),
    "9": ("Blueberry", "#3f51b5"), "7": ("Peacock", "#039be5"), "2": ("Sage", "#33b679"),
    "10": ("Basil", "#0b8043"), "8": ("Graphite", "#616161"),
}
DEFAULT_COLOR = "5"


# Phone alerts on every timed follow-up (automatic or hand-added): a heads-up N minutes before
# and/or one when it starts. Stored in User.followup_rules["_alerts"] (no migration).
ALERT_BEFORE_CHOICES = (0, 5, 10, 15, 30, 60)  # 0 = no heads-up
DEFAULT_ALERTS = {"before": 15, "at_start": True}


def alert_settings(user: Optional[User]) -> dict:
    out = dict(DEFAULT_ALERTS)
    prefs = getattr(user, "followup_rules", None) if user is not None else None
    val = prefs.get("_alerts") if isinstance(prefs, dict) else None
    if isinstance(val, dict):
        if val.get("before") in ALERT_BEFORE_CHOICES:
            out["before"] = val["before"]
        if isinstance(val.get("at_start"), bool):
            out["at_start"] = val["at_start"]
    return out


def clean_alerts(val: dict) -> dict:
    out = {}
    if isinstance(val, dict):
        if val.get("before") in ALERT_BEFORE_CHOICES:
            out["before"] = val["before"]
        if isinstance(val.get("at_start"), bool):
            out["at_start"] = val["at_start"]
    return out


def reminder_overrides(user: Optional[User]) -> list[dict]:
    """Calendar popups for a timed follow-up. The heads-up goes first: Outlook only takes one
    reminder per event and uses the first."""
    a = alert_settings(user)
    mins = ([a["before"]] if a["before"] else []) + ([0] if a["at_start"] else [])
    return [{"method": "popup", "minutes": m} for m in mins]


def followup_color(user: Optional[User]) -> str:
    c = getattr(user, "followup_color", None) if user is not None else None
    return c if c in CALENDAR_COLORS else DEFAULT_COLOR


def _due_for_day(occurred_at: datetime, days_after: int) -> tuple[datetime, datetime]:
    """(due_at, remind_at) for a whole-day follow-up `days_after` days after the touch point, by
    the Eastern calendar. due_at is stored as noon Eastern so the day never slips across a UTC
    boundary; the in-app reminder still fires at FOLLOWUP_DUE_HOUR that morning (or right away
    if that's already past)."""
    base = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
    day = (base.astimezone(EASTERN) + timedelta(days=days_after)).date()
    due = datetime.combine(day, time(12, 0), tzinfo=EASTERN).astimezone(timezone.utc)
    remind = datetime.combine(day, time(FOLLOWUP_DUE_HOUR, 0), tzinfo=EASTERN).astimezone(timezone.utc)
    return due, max(remind, datetime.now(timezone.utc))


# Timed follow-ups land on a free 15-minute slot inside working hours (Eastern) - never at night,
# never on top of the agent's own busy time or another follow-up. They're written to the calendar
# as "Free" (see calendar_sync_service), so they alert the phone without blocking availability.
# (An all-day event can't alert at a set time on the day - Google clamps it to midnight - which is
# why timed is the default.)
WORK_START_HOUR = 8
WORK_END_HOUR = 17
SLOT_MINUTES = 15
SAME_DAY_MIN_LEAD = timedelta(minutes=30)
SAME_DAY_WINDOW = timedelta(hours=2)


def _quarter_up(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    if dt.minute % SLOT_MINUTES:
        dt += timedelta(minutes=SLOT_MINUTES - dt.minute % SLOT_MINUTES)
    return dt


def _work_window(day: date) -> tuple[datetime, datetime]:
    return (datetime.combine(day, time(WORK_START_HOUR, 0), tzinfo=EASTERN),
            datetime.combine(day, time(WORK_END_HOUR, 0), tzinfo=EASTERN))


def followup_window(when: str, occurred_at: datetime) -> tuple[datetime, datetime]:
    """Eastern [start, end) the follow-up's slot is picked from.
    same_day: 30 min to ~2.5 h after the touch point, inside 8am-5pm (speed to lead); outside
    those hours it becomes the first two hours of the next working morning.
    next_day: anywhere in 8am-5pm tomorrow."""
    base = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
    local = max(base, datetime.now(timezone.utc)).astimezone(EASTERN)
    if when == "next_day":
        return _work_window(local.date() + timedelta(days=1))
    day_start, day_end = _work_window(local.date())
    start = _quarter_up(local + SAME_DAY_MIN_LEAD)
    if start < day_start:
        return day_start, day_start + SAME_DAY_WINDOW
    if start + timedelta(minutes=SLOT_MINUTES) > day_end:
        nxt, _ = _work_window(local.date() + timedelta(days=1))
        return nxt, nxt + SAME_DAY_WINDOW
    return start, min(start + SAME_DAY_WINDOW, day_end)


async def _pick_slot(db: AsyncSession, owner_id: str, start: datetime, end: datetime) -> datetime:
    """A random free quarter-hour in [start, end): skips the agent's busy time on their connected
    calendar(s) and slots other open follow-ups already hold. Falls back to any slot in range."""
    import asyncio
    import random
    slots = []
    t = start
    while t + timedelta(minutes=SLOT_MINUTES) <= end:
        slots.append(t)
        t += timedelta(minutes=SLOT_MINUTES)
    if not slots:
        return start.astimezone(timezone.utc)
    taken: list[tuple[datetime, datetime]] = []
    try:
        from app.services import calendar_sync_service
        busy = await asyncio.wait_for(calendar_sync_service.get_busy(db, owner_id, start, end), timeout=4)
        for b in busy:
            taken.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                          datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
    except Exception:
        pass  # no calendar / slow provider - just avoid other follow-ups
    rows = (await db.execute(select(AgentTask.due_at).where(
        AgentTask.owner_id == owner_id, AgentTask.completed_at.is_(None), AgentTask.all_day.is_(False),
        AgentTask.due_at >= start - timedelta(minutes=SLOT_MINUTES), AgentTask.due_at < end,
    ))).scalars().all()
    for d in rows:
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        taken.append((d, d + timedelta(minutes=SLOT_MINUTES)))

    def free(s: datetime) -> bool:
        e = s + timedelta(minutes=SLOT_MINUTES)
        return all(not (s < te and ts < e) for ts, te in taken)

    open_slots = [s for s in slots if free(s)]
    return random.choice(open_slots or slots).astimezone(timezone.utc)


async def plan_followup(db: AsyncSession, user: User, rule: str, occurred_at: datetime) -> tuple[datetime, datetime, bool]:
    """(due_at, remind_at, all_day) for a new automatic follow-up, from the agent's settings for
    this rule: same day / next day, and a timed alert (default) or an all-day event."""
    s = rule_settings(user, rule)
    if s["style"] == "all_day":
        base = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
        local = max(base, datetime.now(timezone.utc)).astimezone(EASTERN)
        days = 1 if s["when"] == "next_day" or local.hour >= WORK_END_HOUR else 0
        due, remind = _due_for_day(local, days)
        return due, remind, True
    start, end = followup_window(s["when"], occurred_at)
    due = await _pick_slot(db, user.id, start, end)
    return due, due, False


def new_done_code() -> str:
    return secrets.token_urlsafe(6)


def done_url(code: str) -> str:
    import os
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    return f"{base}/d/{code}"


def text_url(code: str) -> str:
    """One tap from the calendar event -> the agent's own Messages app opens with the guest's
    number and the suggested text already written (sent from the agent's real cell)."""
    import os
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    return f"{base}/d/{code}/text"


def contact_url(code: str) -> str:
    """One tap from the calendar event -> the phone offers to save this person as a contact."""
    import os
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    return f"{base}/d/{code}/contact"


async def lead_profile(db: AsyncSession, lead: Lead) -> dict:
    """Where this person came from, what they've done since, and the agent's own notes on them
    (general notes + the ones written next to their name on an open house's visitor list)."""
    from app.models.database import LeadEvent, LeadNote
    events = (await db.execute(
        select(LeadEvent).where(LeadEvent.lead_id == lead.id).order_by(LeadEvent.occurred_at.asc())
    )).scalars().all()
    rows = (await db.execute(
        select(LeadNote, OpenHouseEvent.address)
        .outerjoin(OpenHouseEvent, OpenHouseEvent.id == LeadNote.open_house_event_id)
        .where(LeadNote.lead_id == lead.id)
        .order_by(LeadNote.created_at.desc())
    )).all()
    notes = [(f"At {addr}" if addr else None, n.text.strip()) for n, addr in rows if (n.text or "").strip()]
    if (lead.notes or "").strip():
        notes.append((None, lead.notes.strip()))

    def when(dt):
        if not dt:
            return ""
        d = dt.astimezone(EASTERN)
        return f"{d:%b} {d.day}, {d.year}"
    return {
        "source": f"{events[0].summary} ({when(events[0].occurred_at)})" if events else None,
        "activity": [f"{when(e.occurred_at)} - {e.summary}" for e in reversed(events[1:])][:8],
        "notes": notes[:10],
    }


def _qp(text: str) -> str:
    """Quoted-printable (UTF-8) for a vCard 2.1 value: line breaks travel as =0D=0A, so every
    contacts app shows real line breaks (vCard 3.0's backslash-n escape showed up literally in
    Windows Contacts). One unbroken line - no folding, which Windows didn't rejoin (the note got
    cut off at the fold)."""
    out = []
    for b in (text or "").replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"):
        c = chr(b)
        out.append(c if (32 <= b <= 126 and c not in "=;") else f"={b:02X}")
    return "".join(out)


def _vc21(text: str) -> str:
    """A single-line vCard 2.1 value (anything but NOTE)."""
    return (text or "").replace("\r", " ").replace("\n", " ").replace(";", ",").strip()


def build_vcard(lead: Lead, profile: dict, showcase_url: Optional[str] = None, situation: Optional[list] = None) -> str:
    """A vCard the phone opens straight into "Create New Contact", notes included - starting with
    THIS follow-up's situation (what they asked for, the time they picked, what they said).
    Version 2.1 on purpose: iPhone, Android, Google, Outlook AND Windows Contacts all read it, and
    its quoted-printable NOTE keeps line breaks everywhere."""
    name = (lead.name or "").strip() or (lead.email or lead.phone or "New lead")
    first, _, last = name.partition(" ")
    note = []
    if situation:
        note.append("\n".join(situation))
    # Skip "where they came from" when it's this same touch point (it would repeat line 1).
    if profile.get("source") and not (situation and profile["source"].split(" (")[0] in situation[0]):
        note.append(f"Where they came from: {profile['source']}")
    if profile.get("activity"):
        note.append("What they've done:\n" + "\n".join(f"• {a}" for a in profile["activity"]))
    if profile.get("notes"):
        note.append("Your notes:\n" + "\n".join(f"• {'(' + label + ') ' if label else ''}{text}" for label, text in profile["notes"]))
    if showcase_url:
        note.append(f"Their Showcase: {showcase_url}")
    note.append("Saved from OpenHousePal")
    lines = ["BEGIN:VCARD", "VERSION:2.1",
             f"N;CHARSET=UTF-8:{_vc21(last)};{_vc21(first)};;;", f"FN;CHARSET=UTF-8:{_vc21(name)}"]
    if lead.phone:
        lines.append(f"TEL;CELL:{_vc21(lead.phone)}")
    if lead.email:
        lines.append(f"EMAIL;INTERNET:{_vc21(lead.email)}")
    if showcase_url:
        lines.append(f"URL:{_vc21(showcase_url)}")
    lines.append("NOTE;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:" + _qp("\n\n".join(note)))
    lines.append("CATEGORIES:OpenHousePal")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


async def ensure_done_code(db: AsyncSession, task: AgentTask) -> str:
    if not task.done_code:
        task.done_code = new_done_code()
        db.add(task)
        await db.commit()
    return task.done_code


# A missed automatic follow-up is carried forward onto TODAY on the calendar (as "Overdue") for
# this many days, so it doesn't sit forgotten in the past; after that it only lives in the app.
CARRY_FORWARD_DAYS = 7


def carried_forward(task: AgentTask, now: Optional[datetime] = None) -> bool:
    """Open follow-up (automatic or hand-added) whose day has passed, within the carry-forward
    window - the calendar shows it again on today until it's done."""
    if task.completed_at is not None or task.due_at is None:
        return False
    today = (now or datetime.now(timezone.utc)).astimezone(EASTERN).date()
    day = task_day(task)
    return day < today and (today - day).days <= CARRY_FORWARD_DAYS


def task_day(task: AgentTask) -> Optional[date]:
    """The calendar day an all-day task belongs to (Eastern), or None."""
    if task.due_at is None:
        return None
    due = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=timezone.utc)
    return due.astimezone(EASTERN).date()


def compute_bucket(task: AgentTask, now: Optional[datetime] = None) -> str:
    """overdue | today | upcoming | someday | done - the UI groups on this."""
    if task.completed_at is not None:
        return "done"
    if task.due_at is None:
        return "someday"
    now = now or datetime.now(timezone.utc)
    due = task.due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if task.all_day or task.auto_source:
        # Whole-day and automatic follow-ups are due for the day - only overdue once it's over
        # (a same-day "reach out by 3:15" shouldn't flip to red at 3:16).
        day, today = task_day(task), now.astimezone(EASTERN).date()
        return "overdue" if day < today else "today" if day == today else "upcoming"
    if due < now:
        return "overdue"
    # The agent's (Eastern) calendar day - comparing UTC dates filed tomorrow's 1:30 PM under
    # "Today" every evening after 8pm Eastern (already tomorrow in UTC).
    if due.astimezone(EASTERN).date() == now.astimezone(EASTERN).date():
        return "today"
    return "upcoming"


def task_prefix(task: AgentTask) -> str:
    """Calendar title prefix. Automatic follow-ups read "Reach Out" - call, text or email is the
    agent's call, so "[Call]" was misleading."""
    if task.auto_source == SELLER_SOURCE:
        base = "Reach Out - Seller Lead"
    elif task.auto_source == "disclosure_request":
        base = "Send Disclosure"
    elif task.auto_source == "showcase_comment":
        base = "Reply to Comment"
    elif task.auto_source == "showcase_search_request":
        base = "Update Search"
    elif task.auto_source == "private_feedback":
        base = "Reach Out - Unhappy Review"
    elif task.auto_source:
        base = "Reach Out"
    else:
        base = {"call": "Call", "text": "Text", "email": "Email", "showing": "Showing"}.get(task.kind, "To-do")
    return f"Overdue - {base}" if carried_forward(task) else base


def short_person(name: Optional[str]) -> str:
    """"Jane Buyer" -> "Jane B." (an email or a single name stays as it is)."""
    parts = (name or "").strip().split()
    if len(parts) < 2 or "@" in parts[0]:
        one = (name or "").strip()
        return one.title() if one and "@" not in one and (one.isupper() or one.islower()) else one
    first = parts[0].title() if parts[0].isupper() or parts[0].islower() else parts[0]
    return f"{first} {parts[-1][0].upper()}."


def _first(name: Optional[str]) -> str:
    return (name or "").strip().split(" ")[0] if name else ""


# ---------------------------------------------------------------- editable message templates
# Each situation's suggested message is a template the agent can edit on the Follow-ups page.
# {tokens} are filled per lead when the follow-up is made; {showcase} stays as a marker until the
# message is shown, because the guest's Showcase often only exists a few seconds later.
MESSAGE_TOKENS: dict[str, dict] = {
    "first_name": {"label": "First name", "kind": "person", "sample": "Jane", "fallback": "there"},
    "full_name": {"label": "Full name", "kind": "person", "sample": "Jane Buyer", "fallback": "there"},
    "address": {"label": "Property address", "kind": "property", "sample": "12 Oak St", "fallback": "the home"},
    "form_name": {"label": "Form name", "kind": "property", "sample": "Home Value", "fallback": ""},
    # The showing time for the text - only an exact time they picked; otherwise [ADD DATE & TIME] for
    # the agent to fill in with the time they booked (see message_time).
    "requested_time": {"label": "Showing day & time", "kind": "property", "sample": FILL_IN_TIME, "fallback": FILL_IN_TIME},
    "price": {"label": "Price they want", "kind": "property", "sample": "$650,000", "fallback": ""},
    # Built from their form answers - towns, schools, budget, bedrooms, must-haves, timing - so the
    # message shows the agent read what they sent ("a home in Villanova in the Radnor Township
    # school district, between $2.5M and $5M, with a pool, as soon as possible").
    "what_they_want": {"label": "What they're looking for", "kind": "property",
                       "sample": "a 3+ bedroom home in Ardmore or Wynnewood, between $600,000 and $750,000, in the next 1-3 months", "fallback": ""},
    # They asked something - the message leaves a clear spot for the agent's answer instead of
    # asking "any questions?" right back.
    "answer": {"label": "Your answer to their question", "kind": "property", "sample": FILL_IN_ANSWER, "fallback": ""},
    "showcase": {"label": "Their Showcase + link", "kind": "link", "sample": "", "fallback": ""},
    "my_name": {"label": "Your name", "kind": "you", "sample": "", "fallback": ""},
}
# A sentence holding one of these is dropped when it has nothing to say ("You mentioned a price of
# around ." would read broken).
_SENTENCE_TOKENS = {"price", "what_they_want"}
SHOWCASE_MARKER = "{showcase}"
TEMPLATE_MAX = 1200

# Written to be answered by TEXT - a question they can reply to in a few words. No "can we hop on a
# call" (too pushy for a first touch), except "any other form" and a seller, where a call is the
# natural next step. The agent's name + brokerage are added underneath every message (message_footer).
DEFAULT_TEMPLATES: dict[str, str] = {
    "open_house_signin": "Hi {first_name}, thanks for stopping by my open house at {address}! If you have any questions about the home or would like to see it a second time, I'd be happy to set up a tour for you.\n\n{showcase}",
    "similar_homes_qr": "Hi {first_name}, thanks for stopping by {address} and asking to see more homes like it! What caught your eye about it? It helps me point you to the right ones.\n\n{showcase}",
    "showcase_setup": "Hi {first_name}, thanks for requesting a Showcase!\n\n{showcase}\n\nFeel free to like, comment or request a tour on any home in it - or just text me here.",
    # The Showcase part (pitch) carries the how-to + "request a tour under it or text me" close; with
    # no Showcase at all, NO_SHOWCASE_CLOSING stands in for it (see full_message).
    "buyer_form": "Hi {first_name}, thanks for filling out my I'm Buying form! I see you're looking for {what_they_want}.\n\n{showcase}",
    "how_can_we_help": "Hi {first_name}, thanks for reaching out! I see you're looking for {what_they_want}.\n\n{showcase}",
    "second_visit_request": "Hi {first_name}, thanks for wanting another look at {address}! You're all set for {requested_time} - I'll send you the details beforehand. If anything changes, feel free to text here.",
    "form_submission": "Hi {first_name}, thanks for filling out my {form_name} form - I've got everything you sent over. When's a good time for a quick call to go through it together?",
    "showing_request": "Hi {first_name}, thanks for asking to see {address}! You're all set for {requested_time} - I'll send you the details beforehand. If anything changes, feel free to text here.",
    "listing_inquiry": "Hi {first_name}, thanks for your message about {address}! {answer} Would you like to see it in person? Just reply with a day and time that works for you.",
    # Sent once the agent has emailed it (the card says so).
    "disclosure_request": "Hi {first_name}, I just emailed you the seller's disclosure for {address}. Let me know if you need anything else!",
}
# What's My Home Worth (an "any other form" form): it names their home and the next step, instead
# of the generic "thanks for filling out my form". Used while that situation's message is still
# the default.
HOME_VALUE_TEMPLATE = ("Hi {first_name}, thanks for asking what {address} is worth! I'm pulling the recent sales "
                       "around you now and I'll send over your home's value shortly. Would you like me to stop by "
                       "for a quick walkthrough so the number is as accurate as possible?")
# A reader's question from an article's contact form: thank them for the question and leave the
# spot to answer it (used while "any other form"'s message is still the default).
BLOG_QUESTION_TEMPLATE = ("Hi {first_name}, thanks for your question about my article \u201c{form_name}\u201d! {answer} "
                          "If it's easier to talk it through, just reply with a good time to call.")
# They asked to SEE it (a tour, a showing, a walkthrough) - don't ask them back whether they'd like
# to see it in person; answer, then ask for the day and time to book.
LISTING_TOUR_TEMPLATE = ("Hi {first_name}, thanks for reaching out about {address} - I'd be happy to show it to you! "
                         "{answer} Just reply with the day and time that works best and I'll get it booked.")
_TOUR_ASK_RE = re.compile(
    r"\b(tour|showing|walk[- ]?through|see (it|this|the (home|house|place|property))|take a look|come (by|see)|"
    r"stop by|visit|view(ing)? (it|the)|schedule)\b", re.I)
# "Follow this home" on a Spotlight (price & open house alerts) - they didn't ask anything, so
# no "thanks for your message" and no spot for an answer.
SPOTLIGHT_ALERTS_TEMPLATE = ("Hi {first_name}, thanks for following {address}! You'll hear from me the moment anything "
                             "changes - the price, an open house, or its status. Would you like to see it in person before then?")
# The visitor picked a virtual (video) tour - say so, and promise the link instead of "the details".
VIRTUAL_TOUR_TEMPLATE = ("Hi {first_name}, thanks for asking to tour {address}! Your video tour is set for {requested_time} - "
                         "I'll video call you then. If anything changes, feel free to text here.")
# I'm Selling / Make Me Sell / Seller Onboarding with an address: name their home and the next step.
SELLER_FORM_TEMPLATE = ("Hi {first_name}, thanks for telling me about {address}! I'm pulling together what homes "
                        "like yours are selling for right now. When's a good time for a quick call to talk through your plans?")
# Earlier defaults that ignored what the person had just told us. An agent whose saved message is
# still exactly one of these gets the new default instead (they never actually customized it).
_RETIRED_DEFAULTS: set[str] = {
    "Hi {first_name}, thanks for requesting a Showcase! Feel free to like, comment or request a tour on any home in it - or just text me here.\n\n{showcase}",
    "Hi {first_name}, thanks for your message about {address}! {answer} Would you like to see it in person? Just reply with a day and time that works for you.",
    "Hi {first_name}, thanks for wanting another look at {address}! You're all set for {requested_time} - I'll send you the details beforehand. If anything changes, just reply here.",
    "Hi {first_name}, thanks for asking to see {address}! You're all set for {requested_time} - I'll send you the details beforehand. If anything changes, just reply here.",
    "Hi {first_name}, thanks for filling out my I'm Buying form! I see you're looking for {what_they_want} - I'm already on it.\n\n{showcase}",
    "Hi {first_name}, thanks for reaching out! I see you're looking for {what_they_want} - I'm already on it.\n\n{showcase}",
    "Hi {first_name}, thanks for filling out my I'm Buying form! I see you're looking for {what_they_want} - I'm already on it.\n\n{showcase}\n\nIf there's anything else on your wish list, or a home you'd like to see in person, just reply here.",
    "Hi {first_name}, thanks for reaching out! I see you're looking for {what_they_want} - I'm already on it.\n\n{showcase}\n\nIf there's anything else on your wish list, or a home you'd like to see in person, just reply here.",
    "Hi {first_name}, thanks for setting up your Showcase! I'll be keeping an eye out for you too. If a home stands out or you'd like to see one in person, just reply to this text.\n\n{showcase}",
    "Hi {first_name}, it was great meeting you at my open house for {address}! What did you think of the home? Would you like to see it a second time? I'm happy to set that up for you.\n\n{showcase}",
    "Hi {first_name}, I saw you're looking for a second visit to {address}. I've scheduled our showing and will see you there on {requested_time}.",
    "Hi {first_name}, I saw you'd like to see {address}. I've scheduled our showing and will see you there on {requested_time}.",
    "Hi {first_name}, thanks for filling out my I'm Buying form! What's the one thing your next home has to have? Just reply here - it helps me find the right ones for you.\n\n{showcase}",
    "Hi {first_name}, thanks for reaching out! What's the one thing your next home has to have? Just reply here and I'll keep an eye out for it.\n\n{showcase}",
    "Hi {first_name}, thanks for filling out my {form_name} form. I'd love to connect - when's a good time for a quick call?",
    "Hi {first_name}, thanks for reaching out about {address}! Do you have any specific questions about the property, or a day and time you'd like to stop by for a tour?",
    "Hi {first_name}, thank you for your honest feedback - I really appreciate it. I'd like to make this right. What's one thing I could have done better?",
}
# How Can We Help, when they picked SELLING (their own message - see SELLER_SOURCE).
SELLER_TEMPLATE = ("Hi {first_name}, thanks for reaching out about selling {address}! You mentioned a price of around "
                   "{price}. I'd love to hear more about the home and your plans - when would be the best time for a "
                   "quick chat over the phone?")
SELLER_TOKENS = ["first_name", "full_name", "address", "price", "my_name"]

# The alerts that replace a message (ALERT_ONLY_RULES) - what the agent sees, with a sample lead.
ALERT_SAMPLES: dict[str, str] = {
    "disclosure_request": ("Jane asked for the seller disclosure for 12 Oak St.\n"
                           "Email her the seller disclosure - press and hold her email in the contact card to copy it. Nothing to write.\n"
                           "Email: jane@example.com\nPhone: (555) 123-4567"),
    "showcase_comment": ('Jane commented on 12 Oak St in her Showcase:\n"Love the backyard - is the basement finished?"\n'
                         "Reply to her in your own words - every comment is different, so there's no set message. "
                         "The comment, the home and a link to it are on the alert, with her contact card."),
    "showcase_search_request": ('Jane asked you to change her Showcase search:\n"Look closer to downtown. Raise my max price."\n'
                                "To do: open her Showcase, update the search, then let her know - new matches show up for her right away."),
    "private_feedback": ("Jane left a 2-star review through your review link. It stayed private - it isn't posted anywhere.\n"
                         'Their feedback: "Took a while to hear back after the showing."\n'
                         "To do: reach out in your own words - every review is different, so there's no set message. "
                         "A quick call usually goes further than a text."),
}

# Which pills make sense for each situation (a review has no property; only "any other form" has a
# form name; only situations that make a Showcase can pitch one).
_RULE_TOKENS: dict[str, list[str]] = {
    key: ["first_name", "full_name", "address", "my_name"] for key in DEFAULT_TEMPLATES
}
_RULE_TOKENS["form_submission"] = ["first_name", "full_name", "form_name", "address", "my_name"]
for _k in ("second_visit_request", "showing_request"):
    _RULE_TOKENS[_k] = ["first_name", "full_name", "address", "requested_time", "my_name"]
for _k in ("showcase_setup", "buyer_form", "how_can_we_help"):
    _RULE_TOKENS[_k] = ["first_name", "full_name", "my_name"]
for _k in ("buyer_form", "how_can_we_help"):
    _RULE_TOKENS[_k] = ["first_name", "full_name", "what_they_want", "my_name"]
_RULE_TOKENS["listing_inquiry"] = ["first_name", "full_name", "address", "answer", "my_name"]
for _k in PITCH_RULES:
    _RULE_TOKENS[_k] = _RULE_TOKENS[_k] + ["showcase"]


def message_footer(user: Optional[User]) -> str:
    """Signed off under every suggested message: "Thanks," then the agent's full name | brokerage."""
    name = _my_name(user)
    brokerage = (getattr(user, "brokerage", None) or "").strip() if user is not None else ""
    line = " | ".join(x for x in [name, brokerage] if x)
    return f"Thanks,\n{line}" if line else ""


def with_footer(message: Optional[str], user: Optional[User]) -> Optional[str]:
    if not message:
        return message
    footer = message_footer(user)
    if not footer or message.rstrip().endswith(footer):
        return message
    return f"{message.rstrip()}\n\n{footer}"


# ---- the Showcase pitch: the SECOND part of the message, also editable per situation ----
# Filled from the guest's live Showcase each time the message is shown. A sentence holding
# {best_home} is dropped when there's no standout home; {why_now} is a sentence of its own (or
# nothing). The link is always included - appended if the agent's version leaves it out.
PITCH_TOKENS: dict[str, dict] = {
    "home_count": {"label": "Number of homes", "kind": "property", "sample": "14 homes"},
    "best_home": {"label": "Newest best match (only if listed in the last 3 weeks)", "kind": "property", "sample": "12 Oak St in Ardmore (4 bd / 2.5 ba, $650,000)"},
    "why_now": {"label": "How new it is", "kind": "property", "sample": "It just hit the market 2 days ago."},
    "showcase_link": {"label": "Showcase link", "kind": "link", "sample": "https://openhousepal.com/showcase/a1b2c3"},
    "first_name": {"label": "First name", "kind": "person", "sample": "Jane"},
    "my_name": {"label": "Your name", "kind": "you", "sample": ""},
}
# "{home_count} matching ..." reads right for 1 home and for 14 ("1 home that fit" doesn't).
# Old open house pitch - a saved copy of it is treated as the default (so it upgrades).
_RETIRED_PITCHES = {
    "I also set up a personal home search for you, called a Showcase. It already has {home_count} matching what you're looking for, and it updates the moment something new hits the market. The one I'd look at first is {best_home}. {why_now} Here's your link: {showcase_link}\n\nInside it, you can like, comment on or request a tour of any home - or just text me here.",
    "I also put together a personal home search for you. It has {home_count} matching what you're "
    "looking for, and it updates the moment something new hits the market. The one I'd look at first "
    "is {best_home}. {why_now} Take a look and tap the heart on any you like: {showcase_link}",
}
DEFAULT_PITCHES: dict[str, str] = {
    # Same intro + how-to as the forms; the message above it already offers a tour of this home.
    # Built from the home they toured - they never said what they're looking for.
    "open_house_signin": ("I also set up a personal home search for you, called a Showcase. It already has "
                          "{home_count} similar to the one you toured, and it updates the moment something new "
                          "hits the market. The one I'd look at first is {best_home}. {why_now} Here's your link: "
                          "{showcase_link}" + "\n\n" + "Inside it, you can like, comment on or request a tour of any "
                          "home - or just text me here."),
    "similar_homes_qr": ("The homes like it that you asked to see are ready - {home_count} so far, and the list updates "
                         "the moment something new hits the market. The one I'd look at first is {best_home}. {why_now} "
                         "Here's your link: {showcase_link}"),
    # They asked for it - no selling a home, just: it's ready, how many, it's in their email, the link.
    "showcase_setup": ("Your Showcase is ready - it already has {home_count} matching what you asked for, and I also "
                       "sent it to your email. Here's your link: {showcase_link}"),
    # Introduces the Showcase they didn't know about, teaches it (like / comment / tour) and closes.
    "buyer_form": ("I also set up a personal home search for you, called a Showcase. It already has "
                   "{home_count} matching what you're looking for, and it updates the moment something new hits the "
                   "market. The one I'd look at first is {best_home}. {why_now} Here's your link: {showcase_link}"
                   "\n\n" + "Inside it, you can like, comment on or request a tour of any home. If you have any questions "
                   "or find one you'd like to see in person, feel free to request a tour right under that home - or just "
                   "text me here."),
}
DEFAULT_PITCHES["how_can_we_help"] = DEFAULT_PITCHES["buyer_form"]
# A Showcase with no homes in it yet can't be sold on its homes - this goes out instead.
EMPTY_PITCH = ("I also set up a personal home search for you - you'll get an alert the moment a home that fits "
               "what you're looking for hits the market. Take a look here: {showcase_link}")
# ...except where a situation needs its own.
EMPTY_PITCHES = {
    "open_house_signin": ("I also set up a personal home search for you, called a Showcase. Nothing matches just yet, "
                          "but the moment a home that fits hits the market, it shows up there and you'll get an alert. "
                          "Here's your link: {showcase_link}" + "\n\n" + "Inside it, you can like, comment on or "
                          "request a tour of any home - or just text me here."),
    "showcase_setup": ("Your Showcase is ready - nothing matches just yet, but you'll get an alert the moment a home "
                       "that fits hits the market. I also sent it to your email. Here's your link: {showcase_link}"),
    "buyer_form": ("I also set up a personal home search for you, called a Showcase. Nothing matches just yet, "
                   "but the moment a home that fits hits the market, it shows up there and you'll get an alert. Here's "
                   "your link: {showcase_link}" + "\n\n" + "Inside it, you can like, comment on or request a tour of any "
                   "home. If you have any questions, just text me here."),
}
EMPTY_PITCHES["how_can_we_help"] = EMPTY_PITCHES["buyer_form"]
# I'm Buying / How Can We Help with no Showcase at all (plan without Showcases): the close the pitch
# would have carried.
NO_SHOWCASE_CLOSING = {
    "buyer_form": "If you have any questions or find a home you'd like to see in person, just text me here.",
    "how_can_we_help": "If you have any questions or find a home you'd like to see in person, just text me here.",
}

_TOKEN_RE = re.compile(r"\{(\w+)\}")


def _without_showcase(template: str) -> str:
    return _tidy((template or "").replace(SHOWCASE_MARKER, ""))


def render_pitch(template: str, parts: dict) -> str:
    """Fill a pitch template for one guest (see PITCH_TOKENS)."""
    tpl = template or ""
    if "{showcase_link}" not in tpl:
        tpl = tpl.rstrip() + " {showcase_link}"
    if not parts.get("best_home"):
        # Drop the sentence that names the standout home (it would read "...first is .") - and the
        # "why now" line about it - paragraph by paragraph, so the pitch's paragraphs stay apart.
        tpl = "\n\n".join(
            " ".join(s for s in re.split(r"(?<=[.!?])\s+", para) if "{best_home}" not in s and s.strip() != "{why_now}")
            for para in tpl.split("\n\n"))

    def repl(m):
        key = m.group(1)
        if key not in PITCH_TOKENS:
            return m.group(0)
        return (parts.get(key) or "").strip()
    return _tidy(_TOKEN_RE.sub(repl, tpl))


def _tidy(text: str) -> str:
    """Clean up after empty tokens: empty "( )", doubled spaces, space before punctuation, 3+ blank lines."""
    text = re.sub(r"\s*\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?;:])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_template(template: str, values: dict) -> str:
    """Fill {tokens}. Unknown {words} are left as typed; {showcase} stays as a marker. A sentence
    whose _SENTENCE_TOKENS value is empty is left out."""
    template = template or ""
    for key in _SENTENCE_TOKENS:
        if f"{{{key}}}" in template and not (values.get(key) or "").strip():
            template = "\n".join(
                " ".join(x for x in re.split(r"(?<=[.!?])\s+", line) if f"{{{key}}}" not in x)
                for line in template.split("\n"))

    def repl(m):
        key = m.group(1)
        if key == "showcase":
            return SHOWCASE_MARKER
        if key not in MESSAGE_TOKENS:
            return m.group(0)
        val = (values.get(key) or "").strip()
        return val or MESSAGE_TOKENS[key]["fallback"]
    return _tidy(_TOKEN_RE.sub(repl, template or ""))


def render_rule_message(user: Optional[User], rule: str, *, name: Optional[str] = None,
                        address: Optional[str] = None, form_name: Optional[str] = None,
                        requested_time: Optional[str] = None, price: Optional[str] = None,
                        seller: bool = False, what_they_want: Optional[str] = None,
                        asked: bool = False, form_type: Optional[str] = None, virtual: bool = False,
                        wants_tour: bool = False) -> Optional[str]:
    """The agent's template for this situation (or the default), filled for one lead. None for an
    alert-only situation (there's no message to send). `what_they_want` = their search in words
    (see search_summary); `asked` = they wrote a question, so the message leaves a spot to answer
    it; `form_type` lets a premade form under "any other form" (Home Value) get its own default."""
    if rule in ALERT_ONLY_RULES:
        return None
    settings = rule_settings(user, rule)
    template = settings["seller_template"] if seller and rule == "how_can_we_help" else settings["template"]
    if rule == "form_submission" and form_type == "home_value" and not settings["custom_template"] and address:
        template = HOME_VALUE_TEMPLATE
    if rule == "showing_request" and virtual and not settings["custom_template"]:
        template = VIRTUAL_TOUR_TEMPLATE
    if rule == "listing_inquiry" and form_type == "spotlight_alerts" and not settings["custom_template"] and address:
        template = SPOTLIGHT_ALERTS_TEMPLATE
    elif rule == "listing_inquiry" and wants_tour and not settings["custom_template"]:
        template = LISTING_TOUR_TEMPLATE
    if rule == "form_submission" and form_type in ("selling", "make_me_sell", "seller_onboarding") and not settings["custom_template"] and address:
        template = SELLER_FORM_TEMPLATE
    if rule == "form_submission" and form_type == "blog_question" and not settings["custom_template"] and form_name:
        template = BLOG_QUESTION_TEMPLATE
    if rule not in PITCH_RULES or seller:
        template = template.replace(SHOWCASE_MARKER, "")  # only Showcase-making situations pitch it
    return render_template(template, {
        "first_name": _first(name), "full_name": (name or "").strip(),
        "address": address, "form_name": form_name, "my_name": _my_name(user),
        "requested_time": requested_time, "price": price,
        "what_they_want": what_they_want, "answer": FILL_IN_ANSWER if asked else "",
    })


def _my_name(user: Optional[User]) -> str:
    return " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x) if user else ""


def owner_has_showcases(user: Optional[User]) -> bool:
    from app.utils.auth import SHOWCASE_ACCESS_TIERS, owner_has_access
    return bool(user) and owner_has_access(user, SHOWCASE_ACCESS_TIERS)


def _token_list(keys: list[str], user: Optional[User]) -> list[dict]:
    my_name = _my_name(user)
    return [{"key": k, "label": MESSAGE_TOKENS[k]["label"], "kind": MESSAGE_TOKENS[k]["kind"],
             "sample": my_name if k == "my_name" else MESSAGE_TOKENS[k]["sample"]} for k in keys]


def template_payload(rule_key: str, user: Optional[User]) -> dict:
    """What the message editor needs for one situation. On a plan without Showcases the Showcase
    pill and the pitch are left out entirely (there's nothing to pitch)."""
    my_name = _my_name(user)
    showcases = owner_has_showcases(user)
    pitchable = rule_key in PITCH_RULES and showcases
    keys = [k for k in _RULE_TOKENS.get(rule_key, []) if k != "showcase" or pitchable]
    out = {"default_template": DEFAULT_TEMPLATES.get(rule_key, ""), "tokens": _token_list(keys, user), "pitch_style": None}
    if not pitchable:
        out["default_template"] = _without_showcase(out["default_template"])
        return out
    out.update({
        "pitch_style": PITCH_RULES[rule_key],
        "default_pitch": DEFAULT_PITCHES[rule_key],
        "pitch_tokens": [{"key": k, "label": t["label"], "kind": t["kind"],
                          "sample": my_name if k == "my_name" else t["sample"]} for k, t in PITCH_TOKENS.items()],
    })
    return out


async def _open_auto_task_for_lead(db: AsyncSession, owner_id: str, lead_id: str) -> bool:
    """One open automatic follow-up per person at a time - a guest who fills out a form, then
    comments, then asks for a showing gets ONE reminder to reach out, not three."""
    row = await db.execute(
        select(AgentTask.id).where(
            AgentTask.owner_id == owner_id,
            AgentTask.lead_id == lead_id,
            AgentTask.auto_source.is_not(None),
            AgentTask.completed_at.is_(None),
        ).limit(1)
    )
    return row.scalar_one_or_none() is not None


async def _has_agent(db: AsyncSession, lead_id: str) -> bool:
    """Someone already working with another agent never gets an automatic follow-up (or a
    calendar event) - the agent can't represent them, so it would only be noise."""
    lead = await db.get(Lead, lead_id)
    return lead is not None and lead.represented == "yes"


def says_has_agent(snapshot: Optional[dict]) -> bool:
    """This form submission says they're already working with an agent (buying or selling)."""
    for qid in ("has_agent", "sc_has_agent", "buy_working_with_agent", "sell_working_with_agent"):
        if (_answer(snapshot, qid) or "").strip().lower().startswith("yes"):
            return True
    return False


async def create_auto_followup(
    db: AsyncSession,
    *,
    owner_id: str,
    rule: str,
    source_id: str,
    title: str,
    context: Optional[str],
    notes: Optional[str],
    message: Optional[str],
    occurred_at: datetime,
    days_after: int = 0,
    lead_id: Optional[str] = None,
    source: Optional[str] = None,
    needs_reply: bool = False,
) -> Optional[AgentTask]:
    """Create (and commit) one automatic follow-up for `rule`, unless the agent switched that rule
    off, this exact trigger already made one, or this person already has an open automatic
    follow-up. Mirrors it onto the connected calendar. Never raises.
    `source` = a different auto_source than the rule (e.g. an unfinished form: the form's own
    situation decides on/off + timing, but it must never pitch a Showcase)."""
    auto_source = source or rule
    # MLS capitals in the reason line ("... - 26 OLD GULPH RD") read as shouting on the card,
    # calendar event and Mark-as-done page - same tidy as every email subject.
    from app.services.email_service import _tidy_subject
    context = _tidy_subject(context) if context else context
    try:
        if lead_id:
            # Two touches from one person a moment apart (a question + a showing ask) run side by
            # side in the background - without this both see "no open follow-up" and make two.
            # Held until this transaction commits.
            from sqlalchemy import text as _sql
            await db.execute(_sql("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"followup:{lead_id}"})
        user = await db.get(User, owner_id)
        if user is None or not rule_enabled(user, rule):
            return None
        if lead_id and await _has_agent(db, lead_id):
            return None
        dup = await db.execute(
            select(AgentTask.id).where(
                AgentTask.owner_id == owner_id,
                AgentTask.auto_source == auto_source,
                AgentTask.auto_source_id == source_id,
            ).limit(1)
        )
        if dup.scalar_one_or_none():
            return None
        # needs_reply: they asked something in writing - that gets its own follow-up even when
        # an earlier one is still open (otherwise the question only sits on their timeline).
        # But a second message of the SAME kind while that card is still open (asked twice, or
        # signed up for alerts again) updates the open card with the newest message - never two
        # identical "Follow up with ..." cards for one person.
        if lead_id and needs_reply and rule not in ACTION_REQUEST_RULES:
            same = (await db.execute(select(AgentTask).where(
                AgentTask.owner_id == owner_id, AgentTask.lead_id == lead_id, AgentTask.auto_source == auto_source,
                AgentTask.completed_at.is_(None)).order_by(AgentTask.created_at.desc()).limit(1))).scalar_one_or_none()
            if same is not None:
                same.auto_source_id = source_id
                same.title = title
                same.context = (context or "")[:240] or same.context
                same.notes = notes or same.notes
                same.suggested_message = message or same.suggested_message
                await db.commit()
                try:
                    from app.services import calendar_sync_service
                    calendar_sync_service.sync_task_bg(owner_id, same.id)
                except Exception:
                    pass
                return same
        if lead_id and rule in ACTION_REQUEST_RULES:
            # The same request again for the same home (sent twice, or re-picked the times) moves
            # the open card to the newest one; a request for a different home gets its own card.
            from app.models.database import LeadEvent as _LE
            new_ev = await db.get(_LE, source_id)
            new_addr = ((getattr(new_ev, "property_address", None) or "").split(",")[0].strip().lower())
            if new_addr:
                for same in (await db.execute(select(AgentTask).where(
                        AgentTask.owner_id == owner_id, AgentTask.lead_id == lead_id, AgentTask.auto_source == auto_source,
                        AgentTask.completed_at.is_(None)))).scalars().all():
                    old_ev = await db.get(_LE, same.auto_source_id) if same.auto_source_id else None
                    if old_ev is not None and (old_ev.property_address or "").split(",")[0].strip().lower() == new_addr:
                        same.auto_source_id = source_id
                        same.title, same.notes = title, notes or same.notes
                        same.context = (context or "")[:240] or same.context
                        same.suggested_message = message or same.suggested_message
                        await db.commit()
                        try:
                            from app.services import calendar_sync_service
                            calendar_sync_service.sync_task_bg(owner_id, same.id)
                        except Exception:
                            pass
                        return same
        if lead_id and rule not in ACTION_REQUEST_RULES and not needs_reply and await _open_auto_task_for_lead(db, owner_id, lead_id):
            return None
        # When + how is the agent's per-rule setting (Follow-ups page); days_after is ignored now.
        due, remind, all_day = await plan_followup(db, user, rule, occurred_at)
        task = AgentTask(
            owner_id=owner_id,
            lead_id=lead_id,
            title=title,
            notes=notes,
            context=(context or "")[:240] or None,
            kind="call",
            priority="high",
            due_at=due,
            all_day=all_day,
            remind_at=remind,
            auto_source=auto_source,
            auto_source_id=source_id,
            suggested_message=message,
            done_code=new_done_code(),
        )
        db.add(task)
        await db.commit()
        try:
            from app.services import calendar_sync_service
            calendar_sync_service.sync_task_bg(owner_id, task.id)
        except Exception:
            pass
        return task
    except Exception:
        logger.exception("create_auto_followup failed (non-fatal)")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


_AVAILABLE = {"active", "activebright", "comingsoon", "comingsoonbright"}


def _visible_on_showcase(p: dict) -> bool:
    """Mirrors the Showcase page's default tab: not disliked + isPropertyAvailableStatus()."""
    status = p.get("MlsStatus")
    available = (not status) or re.sub(r"[_\s-]", "", str(status).lower()) in _AVAILABLE
    return available and not p.get("disliked")


def _money(v) -> str:
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return ""
    if not v:
        return ""
    return f"${v / 1_000_000:.2f}M".replace(".00M", "M") if v >= 1_000_000 else f"${v:,.0f}"


PITCH_FRESH_DAYS = 21


def _pick_standout(payload: dict) -> tuple[int, Optional[dict], Optional[int], Optional[int]]:
    """(homes shown, standout home, its match %, its days on market). Leads with the home the
    guest's own Showcase ranks highest ("X% Match" - same math, showcase_match_service); if the
    Showcase has no match score (no comparison home, or the agent turned the badge off), the
    newest on the market (fewest days). Never the home they already toured."""
    from app.services.showcase_match_service import compute_match_score
    shown = [p for p in (payload.get("matchedProperties") or []) if _visible_on_showcase(p)]
    if not shown:
        return 0, None, None, None
    subject = payload.get("originalProperty") or None
    toured = (subject or {}).get("listingKey")
    candidates = [p for p in shown if not toured or p.get("ListingKey") != toured] or shown
    show_match = (payload.get("preferences") or {}).get("show_match_score") is not False

    def dom(p):
        try:
            return int(p.get("DaysOnMarket"))
        except (TypeError, ValueError):
            return None

    # Only a home that's actually new gets pitched by name - "the one I'd look at first ... it's
    # been on the market 200 days" sells nothing. Nothing listed in the last 3 weeks -> no home is
    # named at all; the message just gives the count and the link.
    candidates = [p for p in candidates if dom(p) is not None and dom(p) <= PITCH_FRESH_DAYS]
    if not candidates:
        return len(shown), None, None, None

    if subject and show_match:
        scored = [(compute_match_score(p, subject, shown), p) for p in candidates]
        # Highest match wins; a tie goes to the newer listing.
        best_score, best = max(scored, key=lambda sp: (sp[0], -(dom(sp[1]) if dom(sp[1]) is not None else 10**6)))
        return len(shown), best, best_score, dom(best)
    best = min(candidates, key=lambda p: dom(p) if dom(p) is not None else 10**6)
    return len(shown), best, None, dom(best)


def _pitch_parts(count: int, best: Optional[dict], days: Optional[int], url: str) -> dict:
    """The pieces a pitch template is filled with (see PITCH_TOKENS) - how many homes fit, the one
    to open first, why it's worth a click today, and the link."""
    parts = {"home_count": "", "best_home": "", "why_now": "", "showcase_link": url, "count": count}
    if not count:
        return parts
    parts["home_count"] = f"{count} {'home' if count == 1 else 'homes'}"
    if not best:
        return parts  # nothing new enough to name (see _pick_standout) - count + link only
    street = str(best.get("FullStreetAddress") or "").strip()
    street = street.title() if street.isupper() else street  # MLS sends ALL CAPS
    if not re.search(r"\d", street):
        street = ""  # a few MLS records carry just "PA" here - "Pa in Media" read as broken
    city = str(best.get("City") or "").strip()
    city = city.title() if city.isupper() else city
    where = (street + (f" in {city}" if city else "")) if street else (f"a home in {city}" if city else "")
    bits = []
    for key, label in (("BedroomsTotal", "bd"), ("BathroomsTotal", "ba")):
        try:
            if best.get(key):
                bits.append(f"{float(best[key]):g} {label}")
        except (TypeError, ValueError):
            pass
    detail = ", ".join(x for x in [" / ".join(bits), _money(best.get("ListPrice"))] if x)
    # The match % picks the home, but is never said - it means nothing to the guest reading this.
    parts["best_home"] = where + (f" ({detail})" if detail else "") if where else ""
    # Just the days on market - a fact, not a sales line.
    if days is not None:
        if days == 0:
            parts["why_now"] = "It just hit the market today."
        elif days <= 7:
            parts["why_now"] = f"It just hit the market {days} day{'s' if days != 1 else ''} ago."
        else:
            parts["why_now"] = f"It came on the market {days} days ago."
    return parts


def pitch_for(rule: Optional[str], owner: Optional[User], showcase: Optional[dict]) -> str:
    """This situation's pitch (the agent's version or the default), filled for one guest."""
    if not showcase or rule not in PITCH_RULES:
        return ""
    parts = dict(showcase.get("parts") or {})
    parts["first_name"] = showcase.get("first_name") or ""
    parts["my_name"] = _my_name(owner)
    if not parts.get("home_count"):
        return render_pitch(EMPTY_PITCHES.get(rule, EMPTY_PITCH), parts)
    template = rule_settings(owner, rule)["pitch"] if owner is not None else DEFAULT_PITCHES[rule]
    return render_pitch(template, parts)


async def lead_showcase_info(db: AsyncSession, owner: User, leads: list) -> dict[str, dict]:
    """{lead_id: {"url", "count", "parts"}} for leads WITHOUT an agent who have a live Showcase.
    Built from the exact data the guest's Showcase page loads (CollectionsService.
    get_shared_collection, read-only), so the count and the "X% match" in the message are the
    ones they'll see. Nothing when the owner's plan doesn't serve Showcases."""
    from app.models.database import Collection, LeadEvent
    from app.services.collections_service import CollectionsService
    from app.utils.auth import SHOWCASE_ACCESS_TIERS, owner_has_access
    by_id = {l.id: l for l in leads if l is not None and getattr(l, "represented", None) != "yes"}
    if not by_id or not owner_has_access(owner, SHOWCASE_ACCESS_TIERS):
        return {}
    rows = (await db.execute(
        select(LeadEvent.lead_id, Collection.share_token, Collection.created_at)
        .join(Collection, Collection.id == LeadEvent.collection_id)
        .where(
            LeadEvent.lead_id.in_(list(by_id)),
            Collection.owner_id == owner.id,
            Collection.status == "ACTIVE",
            Collection.share_token.is_not(None),
        )
        .order_by(Collection.created_at.asc())
    )).all()
    if not rows:
        return {}
    latest: dict[str, str] = {}
    for lead_id, token, _ in rows:  # ascending, so the newest Showcase wins
        latest[lead_id] = token
    from app.services.website.site_service import resolve_owner_public_base_url
    base = (await resolve_owner_public_base_url(db, owner)).rstrip("/")
    out: dict[str, dict] = {}
    cache: dict[str, tuple] = {}
    for lead_id, token in latest.items():
        if token not in cache:
            try:
                payload = await CollectionsService.get_shared_collection(db, token) or {}
            except Exception:
                logger.warning("showcase payload failed for %s", token, exc_info=True)
                payload = {}
            cache[token] = _pick_standout(payload)
        count, best, _match, days = cache[token]
        url = f"{base}/showcase/{token}"
        out[lead_id] = {"url": url, "count": count, "parts": _pitch_parts(count, best, days, url),
                        "first_name": _first(by_id[lead_id].name)}
    return out


async def lead_showcase_urls(db: AsyncSession, owner: User, leads: list) -> dict[str, str]:
    return {k: v["url"] for k, v in (await lead_showcase_info(db, owner, leads)).items()}


def full_message(task: AgentTask, showcase: Optional[dict], owner: Optional[User] = None) -> Optional[str]:
    """The suggested message, with this situation's Showcase pitch (link last) wherever the agent's
    template put the {showcase} pill - or nothing there when this guest has no Showcase, or the
    situation isn't one that makes a Showcase. Older follow-ups made before templates had no
    marker: the pitch is appended at the end for those."""
    msg = task.suggested_message or ""
    if not msg:
        return None
    pitch = pitch_for(task.auto_source, owner, showcase)
    if not pitch and SHOWCASE_MARKER in msg and task.auto_source in SIGNIN_MERGE_RULES:
        pitch = pitch_for("open_house_signin", owner, showcase)  # folded-in open house follow-up
    if not pitch and SHOWCASE_MARKER in msg:
        pitch = NO_SHOWCASE_CLOSING.get(task.auto_source or "", "")
    if SHOWCASE_MARKER in msg:
        # The pitch is a paragraph of its own wherever the pill sits (never glued mid-sentence).
        out = _tidy(msg.replace(SHOWCASE_MARKER, f"\n\n{pitch}\n\n" if pitch else ""))
    else:
        out = f"{msg}\n\n{pitch}" if pitch else msg
    # Signed off with the agent's name | brokerage (auto follow-ups only - a hand-added task has
    # no message of its own).
    return (with_footer(out, owner) if task.auto_source and owner is not None else out) or None


def resync_lead_followups(owner_id: str, lead_id: str, delay: float = 0) -> None:
    """A Showcase was just created for this lead (usually seconds AFTER its open-house follow-up)
    - re-push their open follow-ups so the calendar event picks up the Showcase link. `delay`:
    called again once the Showcase's homes are loaded, a little later, so the pitch (home count,
    best match) is the real one - and the follow-up itself (made in the background) exists."""
    import asyncio

    async def run():
        if delay:
            await asyncio.sleep(delay)
        from app.database import AsyncSessionLocal
        from app.services import calendar_sync_service
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(AgentTask.id).where(
                AgentTask.owner_id == owner_id, AgentTask.lead_id == lead_id, AgentTask.completed_at.is_(None),
            ))).scalars().all()
        for tid in ids:
            calendar_sync_service.sync_task_bg(owner_id, tid)

    try:
        t = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _PENDING.add(t)
    t.add_done_callback(_PENDING.discard)


LINK_REFRESH_DAYS = 2


def refresh_followup_links(owner_id: str) -> None:
    """The agent's Showcase links just changed domain (went IDX -> their own domain, left IDX ->
    back to openhousepal.com, or their custom domain changed). Re-push their open follow-ups from
    the last couple of days, plus anything still upcoming, so the calendar events carry the new
    link. Both domains keep working; in-app cards build the link fresh on every load anyway."""
    import asyncio

    async def run():
        from sqlalchemy import or_
        from app.database import AsyncSessionLocal
        from app.services import calendar_sync_service
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(AgentTask.id).where(
                AgentTask.owner_id == owner_id,
                AgentTask.lead_id.is_not(None),
                AgentTask.completed_at.is_(None),
                or_(AgentTask.created_at >= now - timedelta(days=LINK_REFRESH_DAYS), AgentTask.due_at >= now),
            ).limit(500))).scalars().all()
        for tid in ids:
            calendar_sync_service.sync_task_bg(owner_id, tid)

    try:
        t = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _PENDING.add(t)
    t.add_done_callback(_PENDING.discard)


def _watch_plan_changes() -> None:
    """Every plan change (PayPal webhook, upgrade/downgrade routes, admin) goes through a flush of
    User.plan_tier - catch it there once instead of at each call site."""
    from sqlalchemy import event, inspect
    from sqlalchemy.orm import Session

    @event.listens_for(Session, "before_flush")
    def _note(session, _ctx, _instances):
        for obj in list(session.dirty):
            if isinstance(obj, User) and inspect(obj).attrs.plan_tier.history.has_changes():
                session.info.setdefault("ohp_plan_changed", set()).add(obj.id)

    @event.listens_for(Session, "after_commit")
    def _refresh(session):
        for uid in session.info.pop("ohp_plan_changed", None) or ():
            refresh_followup_links(uid)

    @event.listens_for(Session, "after_rollback")
    def _forget(session):
        session.info.pop("ohp_plan_changed", None)


_watch_plan_changes()


# Completed follow-ups older than this are deleted for good (nightly) - the list stays about
# what's current instead of stacking up forever. Recently completed ones are still listed.
COMPLETED_RETENTION_DAYS = 60


async def carry_forward_overdue(db: AsyncSession) -> int:
    """Morning job: re-push open automatic follow-ups whose day has passed, so the calendar shows
    them on TODAY as "Overdue - Reach Out" (see carried_forward). Covers one day past the window
    too, so a follow-up that ages out goes back to its original date instead of staying pinned
    to yesterday. Returns how many were queued."""
    from app.services import calendar_sync_service
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(AgentTask).where(
        AgentTask.completed_at.is_(None),
        AgentTask.due_at.is_not(None),
        AgentTask.due_at < now,
        AgentTask.due_at >= now - timedelta(days=CARRY_FORWARD_DAYS + 2),
    ).limit(2000))).scalars().all()
    for t in rows:
        calendar_sync_service.sync_task_bg(t.owner_id, t.id)
    return len(rows)


async def purge_old_completed(db: AsyncSession) -> int:
    from sqlalchemy import delete
    cutoff = datetime.now(timezone.utc) - timedelta(days=COMPLETED_RETENTION_DAYS)
    res = await db.execute(delete(AgentTask).where(AgentTask.completed_at.is_not(None), AgentTask.completed_at < cutoff))
    await db.commit()
    return res.rowcount or 0


def schedule_followup_for_lead_event(owner_id: str, lead_id: str, event_id: str, source_type: str) -> None:
    """Called by leads_service.upsert_lead after every captured touch point. Runs on its own
    session in the background so it can never slow down or break lead capture."""
    if source_type == "collection_visitor":
        resync_lead_followups(owner_id, lead_id)
        return
    if source_type not in _SOURCE_RULES:
        return
    import asyncio

    async def run():
        from app.database import AsyncSessionLocal
        from app.models.database import AgentForm, LeadEvent
        rule, days_after = _SOURCE_RULES[source_type]
        async with AsyncSessionLocal() as db:
            lead = await db.get(Lead, lead_id)
            event = await db.get(LeadEvent, event_id)
            if lead is None or event is None:
                return
            if says_has_agent(event.raw_snapshot):
                return  # this time they told us they have an agent - nothing to follow up on
            form_title = None
            context = event.summary
            seller = False
            address = event.property_address
            name = lead.name or lead.email or "new lead"
            title = f"Follow up with {name}"
            form_type = None
            snap = event.raw_snapshot or {}
            if source_type == "blog_inquiry":
                form_type, form_title = "blog_question", snap.get("blog_title")
            alerts_signup = bool(snap.get("alerts"))
            if alerts_signup:
                form_type = "spotlight_alerts"
            if event.form_id:
                form = await db.get(AgentForm, event.form_id)
                form_title = getattr(form, "title", None)
                form_type = getattr(form, "form_type", None)
                # Premade forms that build a Showcase have their own situation (switch + message).
                rule = FORM_TYPE_RULES.get(getattr(form, "form_type", None), rule) if rule == "form_submission" else rule
                if rule == "how_can_we_help":
                    intent = (_answer(event.raw_snapshot, "intent") or "").lower()
                    seller = "sell" in intent and "buy" not in intent
                    if seller:
                        # Unmistakably a SELLER lead - on the list, the calendar title and the alert.
                        address = _answer(event.raw_snapshot, "property_address") or address
                        price = _price(_answer(event.raw_snapshot, "price_expectation"))
                        title = f"\U0001F3E1 SELLER LEAD - follow up with {name}"
                        context = ("Seller lead - wants to sell" + (f" {address}" if address else " their home")
                                   + (f", hoping for {price}" if price else ""))
                    elif intent:
                        context = f"{event.summary} - buying"
            if rule == "showcase_comment" and event.property_address:
                # The comment itself + a link to that home ride along on the card and the calendar
                # event (lead_comment_details) - this line just says where it happened.
                context = f"Commented on {event.property_address.split(',')[0]} in their Showcase"
            if rule == "listing_inquiry":
                context = listing_inquiry_context(source_type, snap, event.property_address) or context
            if not seller:
                probe = AgentTask(owner_id=owner_id, lead_id=lead.id, auto_source=rule, auto_source_id=event.id)
                prop = await followup_property(db, probe)
                if prop:
                    address = prop["short"]
            else:
                address = display_address(address)[1] or address
            if rule == "showcase_search_request":
                context = "Asked you to change their Showcase search"
                title = f"Update {name}'s Showcase search"
                # Another message while one is still open: put the newest ask on that one.
                open_one = (await db.execute(select(AgentTask).where(
                    AgentTask.owner_id == owner_id, AgentTask.lead_id == lead.id,
                    AgentTask.auto_source == rule, AgentTask.completed_at.is_(None)).limit(1))).scalar_one_or_none()
                if open_one is not None:
                    open_one.notes = _alert_notes(rule, lead, event)
                    await db.commit()
                    resync_lead_followups(owner_id, lead.id)
                    return
            if rule == "disclosure_request":
                title = f"Send the disclosure to {name}"
            elif rule in ("second_visit_request", "showing_request"):
                # Short on the calendar: "Tour 732 Buck Ln - Jane B." - the event's location and
                # notes already carry the full address and the details.
                street = (address or "").split(",")[0].strip()
                title = f"Tour {street} - {short_person(name)}" if street else f"Book the showing for {short_person(name)}"
            contact = "  |  ".join(b for b in [lead.email, lead.phone] if b)
            notes = f"Auto-created: {event.summary}" + (f"\nContact: {contact}" if contact else "")
            if rule in ALERT_NOTE_RULES:
                notes = _alert_notes(rule, lead, event)
            owner = await db.get(User, owner_id)
            made = await create_auto_followup(
                db,
                owner_id=owner_id,
                rule=rule,
                # The event, not event.source_id: some sources reuse one id for every person
                # (Spotlight inquiries use the site id), which would allow only one follow-up ever.
                source_id=event.id,
                title=title,
                context=context,
                notes=notes,
                message=render_rule_message(owner, rule, name=lead.name, address=address, form_name=form_title,
                                            requested_time=message_time(event), seller=seller,
                                            price=_price(_answer(event.raw_snapshot, "price_expectation")) if seller else None,
                                            what_they_want=search_summary(event.raw_snapshot) if rule in ("buyer_form", "how_can_we_help") and not seller else None,
                                            asked=bool(str(snap.get("message") or "").strip()) if (rule == "listing_inquiry" or source_type == "blog_inquiry") and not alerts_signup else False,
                                            form_type=form_type,
                                            virtual=str(snap.get("tour_type") or "").lower() == "virtual",
                                            wants_tour=rule == "listing_inquiry" and bool(_TOUR_ASK_RE.search(str(snap.get("message") or "")))),
                occurred_at=event.occurred_at or datetime.now(timezone.utc),
                days_after=days_after,
                lead_id=lead.id,
                source=SELLER_SOURCE if seller else None,
                needs_reply=(rule == "listing_inquiry" and not alerts_signup and bool(str(snap.get("message") or "").strip()))
                or source_type == "blog_inquiry"
                or form_type in SELLER_FORM_TYPES or seller,
            )
            if made is not None and rule in SIGNIN_MERGE_RULES:
                await _merge_open_signin_followup(db, owner_id, lead.id, made)
            if made is None and rule == "showcase_comment":
                # They already have an open follow-up - re-push it so the new comment shows on it.
                resync_lead_followups(owner_id, lead.id)

    try:
        t = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    # create_task only keeps a weak ref - hold it until done so it isn't collected mid-flight.
    _PENDING.add(t)
    t.add_done_callback(_PENDING.discard)


_PENDING: set = set()

# A concrete ask (second visit / disclosure) right after an open house sign-in: one conversation,
# not two texts - the unsent "thanks for stopping by" follow-up folds into the new one, which
# carries their Showcase link instead (it would otherwise offer a second visit they just asked for).
SIGNIN_MERGE_RULES = {"second_visit_request", "disclosure_request"}


async def _merge_open_signin_followup(db: AsyncSession, owner_id: str, lead_id: str, made: AgentTask) -> None:
    try:
        old = (await db.execute(select(AgentTask).where(
            AgentTask.owner_id == owner_id, AgentTask.lead_id == lead_id,
            AgentTask.auto_source == "open_house_signin", AgentTask.completed_at.is_(None),
        ))).scalars().all()
        if not old:
            return
        from app.services import calendar_sync_service
        for t in old:
            await db.delete(t)
        if made.suggested_message and SHOWCASE_MARKER not in made.suggested_message:
            made.suggested_message = made.suggested_message.rstrip() + "\n\n" + SHOWCASE_MARKER
        await db.commit()
        for t in old:
            calendar_sync_service.unsync_task_bg(owner_id, t.id)
        calendar_sync_service.sync_task_bg(owner_id, made.id)
    except Exception:
        logger.exception("merging the open house follow-up failed (non-fatal)")
        await db.rollback()


def listing_inquiry_context(source_type: str, snap: Optional[dict], address: Optional[str]) -> Optional[str]:
    """What happened, in one line, for each kind of listing inquiry - so the agent knows whether
    they asked a question, only followed the home, or came in from a portal (and where)."""
    snap = snap or {}
    street = (address or "").split(",")[0].strip()
    street = _nice_case(street) if street.isupper() else street
    home = street or "your listing"
    asked = bool(str(snap.get("message") or "").strip())
    if source_type == "property_website_inquiry":
        if snap.get("alerts"):
            return (f'Tapped "Follow this home" on the {home} Spotlight - they get an email when its '
                    "price, open houses or status change. No question asked.")
        return (f"Asked a question about {home} on its Spotlight site" if asked
                else f"Left their contact info on the {home} Spotlight site - no question asked")
    if source_type == "property_page_inquiry":
        came = snap.get("came_from") if isinstance(snap.get("came_from"), dict) else {}
        where = "your website" if came.get("src") == "site" else "its listing page"
        return f"Asked a question about {home} on {where}" if asked else f"Reached out about {home} on {where}"
    if source_type == "portal_lead":
        portal = snap.get("portal_title") or "Portal"
        return (f"{portal} lead - sent a message about {home}" if asked and street
                else f"{portal} lead - interested in {home}")
    return None


def _answer(snapshot: Optional[dict], question_id: str) -> Optional[str]:
    for a in (snapshot or {}).get("answers") or []:
        if isinstance(a, dict) and a.get("questionId") == question_id and a.get("value"):
            return str(a["value"])
    return None


def _answer_value(snapshot: Optional[dict], *question_ids: str):
    """The raw answer (a list stays a list) for the first of these question ids that was answered."""
    for qid in question_ids:
        for a in (snapshot or {}).get("answers") or []:
            if isinstance(a, dict) and a.get("questionId") == qid and a.get("value") not in (None, "", [], {}):
                return a["value"]
    return None


def _place_name(value: str) -> str:
    """ "VILLANOVA, PA" -> "Villanova"; "RADNOR TOWNSHIP, PA" -> "Radnor Township"."""
    first = str(value).split(",")[0].strip()
    return first.title() if first.isupper() or first.islower() else first


def _join_words(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + (" or " if len(items) == 2 else ", or ") + items[-1]


def _short_money(text) -> Optional[str]:
    digits = re.sub(r"[^\d.]", "", str(text or ""))
    try:
        n = float(digits)
    except ValueError:
        return None
    if n <= 0:
        return None
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"${v:g}M" if v == round(v, 2) else f"${v:.2f}M"
    return f"${int(round(n)):,}"


_TIMELINE_WORDS = {
    "asap": "as soon as possible", "as soon as possible": "as soon as possible", "immediately": "as soon as possible",
    "just browsing": "", "just looking": "", "not sure": "", "not sure yet": "", "just curious": "",
}


def _timeline_phrase(value) -> str:
    t = str(value or "").strip()
    if not t:
        return ""
    low = t.lower()
    if low in _TIMELINE_WORDS:
        return _TIMELINE_WORDS[low]
    if "month" in low or "year" in low or "week" in low:
        core = re.sub(r"^(in|within)\s+", "", low)
        if core.startswith(("6+", "12+", "more than", "over")):
            return f"in {core.replace('+', '+ ')}".replace("  ", " ").strip()
        return f"in the next {core}"
    return ""


# Features that read wrong with "a" in front ("a waterfront", "a new construction").
_NO_ARTICLE = {"waterfront", "new construction", "central air", "privacy", "land", "acreage", "water views"}


def _with_article(feature: str) -> str:
    f = feature.strip().lower()
    if f in _NO_ARTICLE or f.endswith("s") or f.startswith(("a ", "an ", "the ")):
        return f
    return ("an " if f[:1] in "aeiou" else "a ") + f


def search_summary(snapshot: Optional[dict]) -> Optional[str]:
    """What a buyer told us they want, as one phrase for the follow-up text:
    "a 4+ bedroom home in Villanova in the Radnor Township school district, between $2.5M and $5M,
    with a pool and a cul-de-sac, as soon as possible". Reads the premade buying forms' questions
    (How Can We Help, I'm Buying, Setup My Own Showcase). None when they told us nothing usable -
    the sentence is then left out of the message rather than reading half-empty."""
    towns = _answer_value(snapshot, "cities", "areas")
    if isinstance(towns, list):
        towns = [_place_name(t) for t in towns]  # picked from the town list: "VILLANOVA, PA"
    elif towns:
        # typed: "Ardmore, Wynnewood" (a trailing state like "PA" isn't a town)
        towns = [_place_name(t) for t in str(towns).split(",") if t.strip() and not (len(t.strip()) == 2 and t.strip().isalpha())]
    else:
        towns = []
    districts = _answer_value(snapshot, "school_districts")
    districts = [_place_name(d) for d in (districts if isinstance(districts, list) else [districts])] if districts else []
    lo = _short_money(_answer_value(snapshot, "budget_min", "min_price"))
    hi = _short_money(_answer_value(snapshot, "budget_max", "max_price"))
    free_price = _answer_value(snapshot, "price_range")
    beds = _answer_value(snapshot, "min_bedrooms", "buy_bedrooms")
    features = _answer_value(snapshot, "key_features")
    features = [str(f).lower() for f in (features if isinstance(features, list) else [features])][:2] if features else []
    when = _timeline_phrase(_answer_value(snapshot, "timeline", "buy_timeline"))

    try:
        beds_n = int(float(str(beds))) if beds not in (None, "") else 0
    except ValueError:
        beds_n = 0
    home = f"a {beds_n}+ bedroom home" if beds_n else "a home"
    parts = [home]
    # Up to 3 towns read as listening; a list of 10 reads like a form being read back - more than 3
    # towns (or 2 school districts) and the places are left out entirely.
    if towns and len(towns) <= 3:
        parts[0] += f" in {_join_words(towns)}"
    if districts and len(districts) <= 2:
        parts[0] += f" in the {_join_words(districts)} school district{'s' if len(districts) > 1 else ''}"
    if lo and hi:
        parts.append(f"between {lo} and {hi}")
    elif hi:
        parts.append(f"up to {hi}")
    elif lo:
        parts.append(f"from {lo}")
    elif free_price:
        parts.append(f"around {str(free_price).strip()}")
    if features:
        parts.append("with " + " and ".join(_with_article(f) for f in features))
    if when:
        parts.append(when)
    # Nothing beyond "a home" means they didn't tell us anything to repeat back.
    if len(parts) == 1 and parts[0] == "a home":
        return None
    if len(parts) > 1 and " in " not in parts[0]:
        # No place named - "a 3+ bedroom home between $600K and $750K" (no comma after "home").
        parts[:2] = [f"{parts[0]} {parts[1]}"]
    return ", ".join(parts)


def _answer_by_label(snapshot: Optional[dict], *labels: str) -> list[str]:
    """Open house / showing requests store answers by label only (no question id)."""
    out = []
    for a in (snapshot or {}).get("answers") or []:
        if isinstance(a, dict) and a.get("label") in labels and a.get("value"):
            out.append(str(a["value"]))
    return out


def _price(text: Optional[str]) -> Optional[str]:
    """A seller's hoped-for price as they typed it - bare digits read as dollars."""
    t = (text or "").strip()
    if not t:
        return None
    digits = re.sub(r"[,\s$]", "", t)
    if digits.isdigit() and int(digits) >= 1000:
        return f"${int(digits):,}"
    return t


def _pretty_time(text: str) -> str:
    # "Sat, Oct 4 · Afternoon" (the open house request form) reads better with a dash.
    return text.replace(" · ", " - ").strip()


def requested_time(event) -> Optional[str]:
    """Every day / time the person picked on a second-visit or showing request ("A or B") - for the
    alert and their contact card."""
    return " or ".join(requested_picks(event)) or None


_DAYPART_RE = re.compile(r"^(.*\S)\s+-\s+(morning|afternoon|evening|anytime)$", re.I)


def message_time(event) -> Optional[str]:
    """The time for the text itself - only a real, exact time they picked ("Sat, Oct 3 at 2:00 PM")
    when it's their one pick. A part of the day ("Sat, Oct 3 - Afternoon"), "anytime" or several
    picks -> None, and the message gets [ADD DATE & TIME] for the exact time the agent booked: the
    guest has to know when to show up, "in the afternoon" isn't a showing time."""
    picks = requested_picks(event)
    if len(picks) != 1 or _DAYPART_RE.match(picks[0]):
        return None
    pick = picks[0]
    # A window ("10am - 12pm", "after 5:30pm", "around 2") or a time with no day is what they'd
    # LIKE, not a showing time - only one exact day + clock time can go in "You're all set for".
    clocks = re.findall(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", pick, re.I)
    if len(clocks) != 1 or not _HAS_DAY_RE.search(pick):
        return None
    # Judge only the time part - the "Oct 5 - " between day and time isn't a range.
    if _VAGUE_TIME_RE.search(_HAS_DAY_RE.sub(" ", pick)):
        return None
    return re.sub(r"\s+-\s+(?=\d)", " at ", pick, count=1)  # "Mon, Oct 5 - 5:30pm" -> "... at 5:30pm"


# Words that make a picked time a window or a guess rather than an exact time.
_VAGUE_TIME_RE = re.compile(
    r"\b(after|before|around|about|between|until|till|by|or|anytime|any time|flexible|ish|earliest|latest|"
    r"morning|afternoon|evening|night|noon-?ish)\b|[~–—]|\d\s*-\s*\d|(?:am|pm)\s*-|\bto\b", re.I)
# A day they named: a weekday, a month + day, or a numeric date.
_HAS_DAY_RE = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\b\d{1,2}/\d{1,2}\b|\btoday\b|\btomorrow\b", re.I)


async def task_requested_times(db: AsyncSession, task: AgentTask) -> list[str]:
    """The day(s) / time(s) the person picked, for a showing / second-visit / disclosure-tour card."""
    if task.auto_source not in ACTION_REQUEST_RULES or not task.auto_source_id:
        return []
    from app.models.database import LeadEvent
    event = await db.get(LeadEvent, task.auto_source_id)
    if event is None or (task.lead_id and event.lead_id != task.lead_id):
        return []
    return requested_picks(event)


def requested_picks(event) -> list[str]:
    """The day / time the person picked on a second-visit or showing request, as they gave it."""
    snap = getattr(event, "raw_snapshot", None) or {}
    st = getattr(event, "source_type", None)
    if st == "open_house_second_visit":
        picks = _answer_by_label(snap, "Preferred visit time")
    elif st == "open_house_disclosure":
        picks = _answer_by_label(snap, "Preferred time")
    elif st == "property_tour":
        picks = _answer_by_label(snap, "Preferred Time 1", "Preferred Time 2", "Preferred Time 3")
    elif st == "property_website_tour_request":
        day, clock = snap.get("requested_date"), snap.get("preferred_time")
        picks = [" at ".join(x for x in [_nice_date(day), clock] if x)] if (day or clock) else []
    elif st == "property_page_tour_request":
        # Listing page "Schedule a tour": up to 3 date + time picks, stored flat on the snapshot.
        picks = []
        for n in (1, 2, 3):
            day, clock = snap.get(f"preferred_date_{n}"), snap.get(f"preferred_time_{n}")
            if day or clock:
                picks.append(" at ".join(x for x in [_nice_date(day), _nice_clock(clock)] if x))
    elif st == "form_submission" and (_answer(snap, "preferred_date_1") or _answer(snap, "preferred_time_1")):
        # Schedule a Showing form: first + second choice
        picks = []
        for n in (1, 2, 3):
            day, clock = _answer(snap, f"preferred_date_{n}"), _answer(snap, f"preferred_time_{n}")
            if day or clock:
                picks.append(" at ".join(x for x in [_nice_date(day), _nice_clock(clock)] if x))
    else:
        picks = []
    return [_nice_pick(p) for p in picks if p and p.strip()][:3]


_PICK_DATE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b")


def _nice_pick(value: str) -> str:
    """A picked time as people read it: a leading "10/03/2026" / "2026-10-03" -> "Sat, Oct 3"."""
    value = _pretty_time(value)
    # a 24-hour clock ("10/06/2026 at 09:30") reads as "9:30 AM"
    value = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[AaPp]\.?[Mm])", lambda mm: _nice_clock(mm.group(0)) or mm.group(0), value)
    m = _PICK_DATE_RE.match(value.strip())
    return (_nice_date(m.group(1)) + value.strip()[m.end():]) if m else value


def _nice_clock(value: Optional[str]) -> Optional[str]:
    """"14:00" -> "2:00 PM" (a form's time answer)."""
    if not value:
        return None
    try:
        t = datetime.strptime(value.strip(), "%H:%M")
        return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"
    except ValueError:
        return value


def _nice_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = datetime.strptime(value.strip(), fmt)
            return f"{d:%a}, {d:%b} {d.day}"
        except ValueError:
            continue
    return value


_CONTACT_QUESTION_IDS = {"full_name", "first_name", "last_name", "name", "email", "phone", "contact_info"}
_TIME_QUESTION_IDS = {f"preferred_{k}_{n}" for k in ("date", "time") for n in (1, 2, 3)}


_TIME_DETAIL_RE = re.compile(r"^((1st|2nd|3rd) choice|preferred (visit )?time|wants another tour)\b", re.I)


def card_details(details: list[str], times: Optional[list[str]] = None) -> list[str]:
    """request_details for the in-app card: the picked times are left out (the card shows them in
    their own block)."""
    times = [t for t in (times or []) if t]
    return [d for d in details if not _TIME_DETAIL_RE.match(d) and not any(t in d for t in times)]


async def request_details(db: AsyncSession, task: AgentTask) -> list[str]:
    """What this person told you in the touch point behind an automatic follow-up (form answers,
    the time they picked, their message) - for the calendar event and their contact card."""
    if not task.auto_source_id or not task.lead_id:
        return []
    from app.models.database import LeadEvent
    try:
        event = await db.get(LeadEvent, task.auto_source_id)
    except Exception:
        return []
    if event is None or event.lead_id != task.lead_id:
        return []
    snap = event.raw_snapshot or {}
    out = []
    if event.source_type == "form_submission" and (_answer(snap, "preferred_date_1") or _answer(snap, "preferred_time_1")):
        for n in (1, 2, 3):
            day, clock = _answer(snap, f"preferred_date_{n}"), _answer(snap, f"preferred_time_{n}")
            if day or clock:
                out.append(f"{('1st', '2nd', '3rd')[n - 1]} choice: " + " at ".join(x for x in [_nice_date(day), _nice_clock(clock)] if x))
    for a in snap.get("answers") or []:
        if not isinstance(a, dict) or a.get("questionId") in _CONTACT_QUESTION_IDS | _TIME_QUESTION_IDS or not a.get("value"):
            continue
        val = a["value"]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if a.get("type") == "currency":
                val = f"${val:,.0f}"  # 600000 -> "$600,000"
            else:
                val = f"{val:,.2f}".rstrip("0").rstrip(".")  # 2400 -> "2,400", 2.5 -> "2.5"
        val = ", ".join(str(v) for v in val) if isinstance(val, list) else str(val)
        label = str(a.get("label") or "").strip()
        val = _nice_pick(val)  # "Sat, Oct 3 · Afternoon" -> plain dash (older contact apps garble "·"); nicer dates
        tour_pick = re.fullmatch(r"Preferred Time ([123])", label, re.I)
        if tour_pick:  # Showcase tour request - same wording as the Schedule a Showing form
            label = ("1st", "2nd", "3rd")[int(tour_pick.group(1)) - 1] + " choice"
        out.append(f"{label}{' ' if label.endswith('?') else ': '}{val[:200]}" if label else val[:200])
    # Tied to an open house (its own disclosure / second-visit form, or a scan of its Similar
    # Homes QR) -> what they wrote is an "Open House Note".
    from_open_house = (event.source_type or "").startswith("open_house_") or \
        ((snap.get("came_from") or {}).get("src") == "similar_qr" if isinstance(snap.get("came_from"), dict) else False)
    note_label = "Open House Note" if from_open_house else None
    # A question typed on a Spotlight / listing page reads as one; a "Follow this home" tap carries
    # a message we wrote for them, not their words - leave it out.
    if event.source_type in ("property_website_inquiry", "property_page_inquiry"):
        note_label = "Their question"
    for key, label in (("tour_type", "Tour"), ("requested_date", "Day"), ("preferred_time", "Time"),
                       ("message", note_label or "Their message"), ("notes", note_label or "Their note")):
        if key == "message" and snap.get("alerts"):
            continue
        v = snap.get(key)
        if v and isinstance(v, str) and v.strip():
            if key == "tour_type":
                v = "In person" if v == "in_person" else "Virtual"
            elif key == "requested_date":
                v = _nice_date(v)
            out.append(f"{label}: {v.strip()[:300]}")
    return out[:10]


def _alert_notes(rule: str, lead: Lead, event) -> str:
    """The fixed alert text for an alert-only situation (no message to send)."""
    first = _first(lead.name) or "They"
    where = (event.property_address or "").split(",")[0] or "the home"
    where = _nice_case(where) if where.isupper() else where  # MLS sends ALL CAPS
    if rule == "disclosure_request":
        lines = [f"{first} asked for the seller disclosure for {where}."]
        if lead.email:
            lines.append("Email them the seller disclosure first - press and hold their email in the contact card above to copy it. Then send them the text below.")
            lines.append(f"Email: {lead.email}")
        else:
            lines.append("To do: they didn't leave an email - text them for the best address to send it to.")
        if lead.phone:
            lines.append(f"Phone: {_pretty_phone(lead.phone)}")
        tour = requested_time(event)
        if tour or _answer_by_label(event.raw_snapshot, "Wants another tour"):
            lines.append("They'd also like another tour" + (f": {tour}" if tour else "."))
        return "\n".join(lines)
    if rule == "private_feedback":
        stars = getattr(event, "rating", None)
        feedback = str(getattr(event, "body", None) or "").strip()
        lines = [f"{first} left a {stars}-star review through your review link. It stayed private - it isn't posted anywhere."
                 if stars else f"{first} left a low review through your review link. It stayed private - it isn't posted anywhere."]
        if feedback:
            lines.append(f'Their feedback: "{feedback[:1000]}"')
        lines.append("To do: reach out in your own words - every review is different, so there's no set message. "
                     "A quick call usually goes further than a text.")
        if lead.email:
            lines.append(f"Email: {lead.email}")
        if lead.phone:
            lines.append(f"Phone: {_pretty_phone(lead.phone)}")
        return "\n".join(lines)
    if rule == "showcase_search_request":
        asked = str((getattr(event, "raw_snapshot", None) or {}).get("message") or "").strip()
        lines = [f"{first} asked you to change their Showcase search" + (":" if asked else ".")]
        if asked:
            lines.append(f'"{asked[:1000]}"')
        lines.append("To do: open their Showcase, update the search, then let them know - new matches show up for them right away.")
        if getattr(event, "collection_id", None):
            from app.services.agent_email_service import absolute
            lines.append(f"Their Showcase: {absolute(f'/showcases?showcase={event.collection_id}')}")
        if lead.email:
            lines.append(f"Email: {lead.email}")
        if lead.phone:
            lines.append(f"Phone: {_pretty_phone(lead.phone)}")
        return "\n".join(lines)
    # showcase_comment
    return (f"{first} commented on {where} in their Showcase. Reply in your own words - every comment is "
            "different, so there's no set message. What they said and a link to the home are right here.")


async def _open_house_short(db: AsyncSession, visitor_id: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """"534 Holly Knoll, Churchville" for the open house this sign-in happened at."""
    from app.models.database import OpenHouseVisitor
    try:
        visitor = await db.get(OpenHouseVisitor, visitor_id) if visitor_id else None
        oh = await db.get(OpenHouseEvent, visitor.open_house_event_id) if visitor and visitor.open_house_event_id else None
        if oh is not None:
            return display_address(oh.address, oh.city, oh.state)[1] or fallback
    except Exception:
        pass
    return display_address(fallback)[1] or fallback


async def create_open_house_followup(
    db: AsyncSession,
    *,
    owner_id: str,
    lead: Lead,
    signin_id: Optional[str],
    property_address: Optional[str],
    occurred_at: datetime,
    commit: bool = False,
    rule: str = "open_house_signin",
) -> Optional[AgentTask]:
    """Create the day-after "follow up with this open house lead" task, one per real sign-in.
    `rule` is "similar_homes_qr" when the guest came in through a Similar Homes handout's QR.

    Dedup key is the sign-in (visitor) id, NOT the open house - so the SAME guest signing in at a
    DIFFERENT open house weeks later gets a fresh follow-up (a new, wanted touch point), while a
    repeat submit of the SAME sign-in doesn't double it. Same-guest / same-open-house repeats
    inside the short window never even reach here: the caller's own idempotency check returns
    before lead creation. Safe to call from the sign-in path; never raises."""
    try:
        owner = await db.get(User, owner_id)
        if owner is None or not rule_enabled(owner, rule) or lead.represented == "yes":
            return None
        dedup_key = signin_id or f"lead:{lead.id}:{int(occurred_at.timestamp())}"
        existing = await db.execute(
            select(AgentTask.id).where(
                AgentTask.owner_id == owner_id,
                AgentTask.auto_source.in_(("open_house_signin", "similar_homes_qr")),
                AgentTask.auto_source_id == dedup_key,
            )
        )
        if existing.scalar_one_or_none():
            return None

        # When + how is the agent's setting for this rule (default: next day, timed free slot).
        due, remind, all_day = await plan_followup(db, owner, rule, occurred_at)
        contact_bits = "  |  ".join(b for b in [lead.email, lead.phone] if b)
        title = f"Follow up with {lead.name or 'open house guest'}"
        notes = "Auto-created from an open house sign-in."
        if property_address:
            notes += f"\nOpen house: {property_address}"
        if contact_bits:
            notes += f"\nContact: {contact_bits}"

        task = AgentTask(
            owner_id=owner_id,
            lead_id=lead.id,
            title=title,
            notes=notes,
            kind="call",
            priority="high",
            due_at=due,
            all_day=all_day,
            remind_at=remind,
            done_code=new_done_code(),
            auto_source=rule,
            auto_source_id=dedup_key,
            suggested_message=render_rule_message(owner, rule, name=lead.name, address=await _open_house_short(db, signin_id, property_address)),
            # The sheet's bottom "Want to see more homes like these?" QR (-> sign-in -> Showcase), not
            # one of the per-home QRs (those open that home's listing page - tracked separately).
            context=(f"Scanned \"Want to see more homes like these?\" on your Similar Homes sheet and signed in{' - ' + property_address if property_address else ''}"
                     if rule == "similar_homes_qr" else
                     f"Signed in at your open house{' - ' + property_address if property_address else ''}")[:240],
        )
        db.add(task)
        if commit:
            await db.commit()
        else:
            await db.flush()
        # Mirror this auto follow-up onto the agent's connected calendar (no-op unless Google
        # is connected). Runs on its own session so it can't affect the sign-in.
        try:
            from app.services import calendar_sync_service
            calendar_sync_service.sync_task_bg(owner_id, task.id)
        except Exception:
            pass
        return task
    except Exception:
        logger.exception("create_open_house_followup failed (non-fatal)")
        return None


# ---------------------------------------------------------------- guest with an agent + a note
# A guest who says they already have an agent normally gets nothing (see _has_agent). But when the
# agent writes a note on one from the open house visitor list, that's a signal - so they get a
# heads-up follow-up instead: no Showcase, no pitch, no message to send, just what they need to
# know (they have an agent, the note, how to reach them). Switched on/off, timed and styled with
# the Open house sign-in situation; the wording is fixed (it's an alert to the agent, not a message).
NOTED_GUEST_SOURCE = "open_house_noted"
NOTED_GUEST_RULE = "open_house_signin"
NOTED_GUEST_ALERT = ("Heads up - {first} told you they're already working with another agent, so no Showcase was "
                     "made and there's no message to send. But you left a note on them, so there may still be an "
                     "opportunity worth a quick check-in.")


async def _visitor_notes(db: AsyncSession, visitor) -> list[str]:
    """The agent's notes on this guest - the ones written at this open house first, newest first."""
    from app.models.database import LeadNote
    rows = (await db.execute(
        select(LeadNote).where(LeadNote.lead_id == visitor.lead_id).order_by(LeadNote.created_at.desc())
    )).scalars().all()
    here = [n for n in rows if n.open_house_event_id == visitor.open_house_event_id]
    return [n.text.strip() for n in (here or rows) if (n.text or "").strip()]


def _noted_guest_notes(lead: Lead, visitor, address: Optional[str], notes: list[str]) -> str:
    first = _first(lead.name or visitor.full_name) or "This guest"
    lines = [NOTED_GUEST_ALERT.format(first=first)]
    if len(notes) == 1:
        lines.append(f'Your note: "{notes[0][:500]}"')
    elif notes:
        lines.append("Your notes:\n" + "\n".join(f'- "{n[:300]}"' for n in notes[:3]))
    when = visitor.created_at.astimezone(EASTERN) if visitor.created_at else None
    if address or when:
        lines.append("Signed in: " + " on ".join(x for x in [address, f"{when:%b} {when.day}" if when else None] if x))
    contact = "  |  ".join(b for b in [_pretty_phone(lead.phone or visitor.phone), lead.email or visitor.email] if b)
    if contact:
        lines.append(f"Contact: {contact}")
    return "\n".join(lines)


def _pretty_phone(phone: Optional[str]) -> Optional[str]:
    """Leads store bare digits - show a US number the way people read it."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else phone


async def on_visitor_note(db: AsyncSession, owner_id: str, visitor) -> None:
    """The agent added / edited / deleted a note on an open house guest.
    - No agent: they already have (or had) a sign-in follow-up - re-push it so the note shows.
    - Has an agent: create the heads-up follow-up (once per sign-in), or refresh its note text.
    Never raises - a note must always save."""
    from app.models.database import OpenHouseEvent as _OHE
    try:
        if not visitor.lead_id:
            return
        lead = await db.get(Lead, visitor.lead_id)
        if lead is None:
            return
        if (visitor.has_agent or "").upper() != "YES":
            resync_lead_followups(owner_id, lead.id)
            return
        notes = await _visitor_notes(db, visitor)
        oh = await db.get(_OHE, visitor.open_house_event_id) if visitor.open_house_event_id else None
        address = getattr(oh, "address", None)
        existing = (await db.execute(select(AgentTask).where(
            AgentTask.owner_id == owner_id,
            AgentTask.auto_source == NOTED_GUEST_SOURCE,
            AgentTask.auto_source_id == visitor.id,
        ))).scalar_one_or_none()
        if existing is not None:
            if existing.completed_at is None and notes:
                existing.notes = _noted_guest_notes(lead, visitor, address, notes)
                await db.commit()
                resync_lead_followups(owner_id, lead.id)
            return
        owner = await db.get(User, owner_id)
        if owner is None or not notes or not rule_enabled(owner, NOTED_GUEST_RULE):
            return
        if await _open_auto_task_for_lead(db, owner_id, lead.id):
            resync_lead_followups(owner_id, lead.id)
            return
        due, remind, all_day = await plan_followup(db, owner, NOTED_GUEST_RULE, datetime.now(timezone.utc))
        name = lead.name or visitor.full_name or "open house guest"
        task = AgentTask(
            owner_id=owner_id,
            lead_id=lead.id,
            title=f"Check in with {name} (has an agent)",
            notes=_noted_guest_notes(lead, visitor, address, notes),
            context=(f"Has an agent - you left a note on them at your open house"
                     f"{' - ' + address if address else ''}")[:240],
            kind="call",
            priority="normal",
            due_at=due,
            all_day=all_day,
            remind_at=remind,
            done_code=new_done_code(),
            auto_source=NOTED_GUEST_SOURCE,
            auto_source_id=visitor.id,
            suggested_message=None,
        )
        db.add(task)
        await db.commit()
        try:
            from app.services import calendar_sync_service
            calendar_sync_service.sync_task_bg(owner_id, task.id)
        except Exception:
            pass
    except Exception:
        logger.exception("on_visitor_note failed (non-fatal)")
        try:
            await db.rollback()
        except Exception:
            pass


def noted_guest_sample(user: Optional[User]) -> str:
    """What the heads-up looks like - shown (read-only) under the Open house sign-in situation."""
    return "\n".join([
        NOTED_GUEST_ALERT.format(first="Jane"),
        'Your note: "Loved the kitchen - her lease is up in March and she isn\'t sure about her agent."',
        "Signed in: 12 Oak St on Sep 27",
        "Contact: (555) 123-4567  |  jane@example.com",
    ])



# ---------------------------------------------------------------- the property a follow-up is about
# Every follow-up about a home shows its full address (street, city, state) as a link: that exact
# home inside the person's own Showcase when it's in there, otherwise the same smart listing link
# the Similar Homes sheet's QR codes use (the agent's Spotlight for it, else the OpenHousePal
# property page). A seller's own home / an address with no listing just shows the address.

def _nice_case(text: Optional[str]) -> str:
    """MLS sends ALL CAPS: "S 17TH ST" -> "S 17th St". Mixed case is left as typed."""
    t = (text or "").strip()
    if not t or not t.isupper():
        return t
    return " ".join(w.lower() if w[:1].isdigit() else w.capitalize() for w in t.split())


def display_address(street: Optional[str], city: Optional[str] = None, state: Optional[str] = None) -> tuple[str, str]:
    """(full "534 Holly Knoll, Churchville, PA", short "534 Holly Knoll, Churchville")."""
    street = (street or "").strip()
    if not street:
        return "", ""
    if "," in street:  # already a full address
        parts = [p.strip() for p in street.split(",") if p.strip()]
        nice = [_nice_case(p) if i < 2 else p.upper() if len(p) == 2 else p for i, p in enumerate(parts)]
        return ", ".join(nice), ", ".join(nice[:2])
    s, c = _nice_case(street), _nice_case(city)
    st = (state or "").strip().upper()
    return ", ".join(x for x in [s, c, st] if x), ", ".join(x for x in [s, c] if x)


# Situations where a typed-in address is a home they want to SEE (never a seller's own home).
_TYPED_ADDRESS_RULES = {"showing_request", "listing_inquiry", "second_visit_request", "disclosure_request"}


async def _listing_for_address(db: AsyncSession, typed: str, city: Optional[str] = None) -> tuple:
    """(listing_key, city, state) for a typed "847 Lesley Rd, Villanova" - matched on the street
    (MLS stores it upper-case, e.g. "847 LESLEY RD"), the typed city breaking a tie."""
    from sqlalchemy import func as _f
    from app.models.database import Property
    parts = [p.strip() for p in typed.split(",") if p.strip()]
    if not parts:
        return None, None, None
    street = re.sub(r"\s+", " ", parts[0]).upper()
    want_city = (city or (parts[1] if len(parts) > 1 else "")).strip().upper()
    rows = (await db.execute(select(Property.listing_key, Property.city, Property.state)
                             .where(_f.upper(Property.street_address) == street).limit(10))).all()
    if not rows:
        return None, None, None
    best = next((r for r in rows if want_city and (r[1] or "").upper() == want_city), None)
    if best is None and len(rows) == 1:
        best = rows[0]
    return (best[0], best[1], best[2]) if best else (None, None, None)


async def followup_property(db: AsyncSession, task: AgentTask, owner: Optional[User] = None) -> Optional[dict]:
    """{address, short, url, where: showcase|listing|None} for the home this follow-up is about, or
    None when it isn't about a home. The link opens that home in their Showcase, else the property
    details page (the same page a Similar Homes QR scan opens) - never a Spotlight."""
    from app.models.database import (AgentForm, Collection, LeadEvent, OpenHouseVisitor, Property, PropertyComment,
                                     PropertyTour, PropertyWebsite, collection_properties)
    src = task.auto_source
    if not src or not task.auto_source_id:
        return None
    oh = prop = site = None
    listing_key = street = city = state = None
    came_raw = origin = None
    try:
        if src in ("open_house_signin", "similar_homes_qr", NOTED_GUEST_SOURCE):
            visitor = await db.get(OpenHouseVisitor, task.auto_source_id)
            if visitor is not None and visitor.open_house_event_id:
                oh = await db.get(OpenHouseEvent, visitor.open_house_event_id)
        else:
            event = await db.get(LeadEvent, task.auto_source_id)
            if event is None or (task.lead_id and event.lead_id != task.lead_id):
                return None
            snap = event.raw_snapshot or {}
            if "came_from" in snap:
                came_raw = snap.get("came_from") or {}
            elif event.source_type == "property_tour":
                origin = "Requested it from their Showcase"
            if event.open_house_event_id:
                oh = await db.get(OpenHouseEvent, event.open_house_event_id)
            elif event.source_type in ("property_page_inquiry", "property_page_tour_request"):
                # Listing page (a Similar Homes per-home QR lands here): the home they asked about.
                lk = snap.get("listing_key") or event.source_id
                prop = (await db.execute(select(Property).where(Property.listing_key == lk).limit(1))).scalars().first() if lk else None
                if prop is None:
                    listing_key, street = lk, event.property_address
            elif event.source_type == "property_tour" and event.source_id:
                tour = await db.get(PropertyTour, event.source_id)
                prop = await db.get(Property, tour.property_id) if tour and tour.property_id else None
            elif event.source_type == "property_comment" and event.source_id:
                comment = await db.get(PropertyComment, event.source_id)
                prop = await db.get(Property, comment.property_id) if comment and comment.property_id else None
            elif event.source_type in ("property_website_inquiry", "property_website_tour_request"):
                site_id = snap.get("property_website_id") or event.source_id
                site = await db.get(PropertyWebsite, site_id) if site_id else None
            elif event.form_id:
                form = await db.get(AgentForm, event.form_id)
                linked = (getattr(form, "linked_property", None) or {}) if form else {}
                if linked.get("listing_key") or linked.get("address"):
                    listing_key, street, city, state = linked.get("listing_key"), linked.get("address"), linked.get("city"), linked.get("state")
                else:
                    street = _answer(snap, "property_of_interest") or _answer(snap, "property_address") or event.property_address
            else:
                street = event.property_address
    except Exception:
        logger.warning("follow-up property lookup failed for %s", task.id, exc_info=True)
        return None
    if oh is None and prop is None and site is None and not street and src not in ("open_house_signin", "similar_homes_qr", NOTED_GUEST_SOURCE):
        street = getattr(event, "property_address", None)
    if oh is not None:
        listing_key, street, city, state = oh.listing_key, oh.address, oh.city, oh.state
    elif prop is not None:
        listing_key, street, city, state = prop.listing_key, prop.street_address, prop.city, prop.state
    elif site is not None:
        snap = site.property_snapshot or {}
        listing_key, street, city, state = site.listing_key, snap.get("address"), snap.get("city"), snap.get("state")
    if not listing_key and street and src in _TYPED_ADDRESS_RULES:
        # A home they typed in (Schedule a Showing form, a portal lead) - match it to the listing.
        try:
            lk, lcity, lstate = await _listing_for_address(db, street, city)
            if lk:
                listing_key, city, state = lk, city or lcity, state or lstate
                street = street.split(",")[0]
        except Exception:
            logger.warning("address -> listing match failed for %s", task.id, exc_info=True)
    full, short = display_address(street, city, state)
    if not full:
        return None
    out = {"address": full, "short": short, "url": None, "where": None, "mls": None, "list_agent": None, "photo": None,
           "came_from": None}
    if came_raw is not None:
        try:
            from app.services.visit_source import describe_came_from
            out["came_from"] = await describe_came_from(db, came_raw, task.owner_id, home=short.split(",")[0])
        except Exception:
            logger.warning("came-from lookup failed for %s", task.id, exc_info=True)
    elif origin:
        out["came_from"] = origin  # e.g. a tour request made inside their own Showcase
    owner = owner or await db.get(User, task.owner_id)
    if listing_key:
        # The MLS number (what the agent searches in ShowingTime to book it) + the listing agent, so
        # they can call about the showing right away. Left out when it's the agent's own listing.
        try:
            row = (await db.execute(
                select(Property.listing_id, Property.list_agent_full_name, Property.list_agent_preferred_phone, Property.list_agent_mls_id,
                       Property.list_agent_email, Property.list_office_name, Property.img_src, Property.photos)
                .where(Property.listing_key == listing_key).order_by(Property.listing_id.is_(None)).limit(1))).first()
            if row:
                out["mls"] = row[0]
                photos = row[7] if isinstance(row[7], list) else []
                out["photo"] = row[6] or next((p for p in photos if isinstance(p, str) and p.startswith("http")), None)
                own = owner is not None and row[3] and row[3] == getattr(owner, "mls_id", None)
                if (row[1] or row[2] or row[4]) and not own:
                    out["list_agent"] = {"name": _nice_case(row[1]) if row[1] and row[1].isupper() else row[1],
                                         "phone": _pretty_phone(row[2]) if row[2] else None,
                                         "email": (row[4] or "").strip().lower() or None,
                                         "office": _nice_case(row[5]) if row[5] and row[5].isupper() else row[5]}
        except Exception:
            logger.warning("MLS number / listing agent lookup failed for %s", task.id, exc_info=True)
    try:
        # 1) that exact home inside their own Showcase
        if listing_key and task.lead_id and owner is not None and owner_has_showcases(owner):
            row = (await db.execute(
                select(Collection.share_token, Property.id)
                .join(LeadEvent, LeadEvent.collection_id == Collection.id)
                .join(collection_properties, collection_properties.c.collection_id == Collection.id)
                .join(Property, Property.id == collection_properties.c.property_id)
                .where(LeadEvent.lead_id == task.lead_id, Collection.owner_id == owner.id, Collection.status == "ACTIVE",
                       Collection.share_token.is_not(None), Property.listing_key == listing_key)
                .order_by(Collection.created_at.desc()).limit(1)
            )).first()
            if row:
                from app.services.website.site_service import resolve_owner_public_base_url
                base = (await resolve_owner_public_base_url(db, owner)).rstrip("/")
                out.update(url=f"{base}/showcase/{row[0]}?property={row[1]}", where="showcase")
                return out
        # 2) the property details page
        from app.services import property_website_service as pws
        if listing_key and owner is not None:
            info = await pws.describe_listing_link(db, owner, listing_key, full, skip_spotlight=True)
            if info.get("url"):
                out.update(url=info["url"], where="listing")
    except Exception:
        logger.warning("follow-up property link failed for %s", task.id, exc_info=True)
    return out



# ---------------------------------------------------------------- live calendar-alert preview
# The settings page shows each situation's calendar alert exactly as it will look - built by the
# real event builder (calendar_sync_service._task_event_body) for a sample lead, with the message
# the agent is editing (unsaved). Same spacing, emojis and order as the real thing.
def _sample_cases() -> dict:
    import os
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    in_showcase = {"address": "12 Oak St, Ardmore, PA", "short": "12 Oak St, Ardmore", "url": f"{base}/showcase/sample?property=12-oak", "where": "showcase", "mls": "PAMC2012345",
                   "list_agent": {"name": "Alex Lister", "phone": "(610) 555-0199", "email": "alex@example.com", "office": "Main Line Realty"}}
    listing = {"address": "12 Oak St, Ardmore, PA", "short": "12 Oak St, Ardmore", "url": f"{base}/property/sample", "where": "listing"}
    birch = {"address": "22 Birch Ln, Wayne, PA", "short": "22 Birch Ln, Wayne", "url": f"{base}/showcase/sample?property=22-birch", "where": "showcase", "mls": "PADE2054321",
             "list_agent": {"name": "Alex Lister", "phone": "(610) 555-0199", "email": "alex@example.com", "office": "Main Line Realty"}}
    return {
        "open_house_signin": ("Follow up with Jane Buyer", "Signed in at your open house - 12 Oak St, Ardmore", [], in_showcase),
        "similar_homes_qr": ("Follow up with Jane Buyer", "Scanned \"Want to see more homes like these?\" on your Similar Homes sheet and signed in - 12 Oak St, Ardmore", [], in_showcase),
        "second_visit_request": ("Book the showing for Jane Buyer", "Requested a second visit - 12 Oak St",
                                 ["Preferred visit time: Sat, Oct 3 - Afternoon"], in_showcase),
        "disclosure_request": ("Send the disclosure to Jane Buyer", "Requested seller disclosure - 12 Oak St", [], listing),
        "showcase_setup": ("Follow up with Jane Buyer", "Submitted Setup My Own Showcase",
                           ["Where are you looking? Ardmore, Wynnewood", "Price range: $600k - $750k", "Bedrooms: 3+"], None),
        "buyer_form": ("Follow up with Jane Buyer", "Submitted I'm Buying",
                       ["Where are you looking? Ardmore, Wynnewood", "When are you hoping to buy? 1-3 months"], None),
        "how_can_we_help": ("Follow up with Jane Buyer", "Submitted How Can We Help - buying",
                            ["How can I help you? I'm looking to buy", "Where are you looking? Ardmore"], None),
        SELLER_SOURCE: ("\U0001F3E1 SELLER LEAD - follow up with Jane Buyer", "Seller lead - wants to sell 48 Maple Ave, Wynnewood, hoping for $650,000",
                        ["How can I help you? I'm looking to sell", "When are you hoping to sell? 1-3 months"],
                        {"address": "48 Maple Ave, Wynnewood, PA", "short": "48 Maple Ave, Wynnewood", "url": None, "where": None}),
        "form_submission": ("Follow up with Jane Buyer", "Submitted What's My Home Worth?", ["Property address: 48 Maple Ave"], None),
        "showing_request": ("Book the showing for Jane Buyer", "Requested a showing - 22 Birch Ln",
                            ["1st choice: Sat, Oct 3 at 2:00 PM", "2nd choice: Sun, Oct 4 at 11:00 AM", "Their message: Can we see the basement too?"], birch),
        "showcase_comment": ("Follow up with Jane Buyer", "Commented on 22 Birch Ln in their Showcase", [], birch),
        "listing_inquiry": ("Follow up with Jane Buyer", "Asked a question about 12 Oak St on its Spotlight site",
                            ["Their question: Is the roof new?"], listing),
        "showcase_search_request": ("Update Jane Buyer's Showcase search", "Asked you to change their Showcase search", [], None),
        "private_feedback": ("Follow up with Jane Buyer", "Left a 2-star review through your review link - it stayed private, not posted anywhere", [], None),
    }


# Sample second visit = one pick (goes into the text); sample showing = two picks (the text gets the
# fill-in instead, the alert lists both).
_SAMPLE_MESSAGE_TIMES = {"second_visit_request": None, "showing_request": None}


def sample_event(owner: User, key: str, *, template: Optional[str] = None, pitch: Optional[str] = None, seller: bool = False) -> dict:
    from types import SimpleNamespace
    from app.services.calendar_sync_service import _task_event_body
    if key not in _RULE_KEYS:
        return {"summary": "", "description": ""}
    src = SELLER_SOURCE if seller and key == "how_can_we_help" else key
    title, context, details, prop = _sample_cases()[src]
    settings = rule_settings(owner, key)
    lead = SimpleNamespace(name="Jane Buyer", phone="6105550123", email="jane@example.com")
    notes = None
    message = ""
    if key in ALERT_NOTE_RULES:
        event = SimpleNamespace(property_address="12 Oak St" if key == "disclosure_request" else "22 Birch Ln",
                                raw_snapshot={"answers": [], "message": "Look closer to downtown. Raise my max price."}, source_type="",
                                rating=2, body="Took a while to hear back after the showing.")
        notes = _alert_notes(key, lead, event)
    if key in ALERT_ONLY_RULES:
        pass
    else:
        tpl = template if isinstance(template, str) and template.strip() else (
            settings["seller_template"] if src == SELLER_SOURCE else settings["template"])
        values = {"first_name": "Jane", "full_name": "Jane Buyer", "address": (prop or {}).get("short") or "12 Oak St, Ardmore",
                  "form_name": "What's My Home Worth?", "my_name": _my_name(owner),
                  "requested_time": _SAMPLE_MESSAGE_TIMES.get(key), "price": "$650,000",
                  "what_they_want": MESSAGE_TOKENS["what_they_want"]["sample"] if key in ("buyer_form", "how_can_we_help") else "",
                  "answer": FILL_IN_ANSWER if key == "listing_inquiry" else ""}
        msg = render_template(tpl, values)
        pitch_text = ""
        if key in PITCH_RULES and src != SELLER_SOURCE and owner_has_showcases(owner):
            parts = {k: PITCH_TOKENS[k]["sample"] for k in ("home_count", "best_home", "why_now", "showcase_link")}
            parts.update(first_name="Jane", my_name=_my_name(owner))
            pitch_text = render_pitch(pitch if isinstance(pitch, str) and pitch.strip() else settings["pitch"], parts)
        if not pitch_text and src != SELLER_SOURCE:
            pitch_text = NO_SHOWCASE_CLOSING.get(key, "")
        msg = _tidy(msg.replace(SHOWCASE_MARKER, f"\n\n{pitch_text}\n\n" if pitch_text else ""))
        message = with_footer(msg, owner) or ""
    start = datetime.combine((datetime.now(EASTERN) + timedelta(days=1)).date(), time(10, 15), tzinfo=EASTERN)
    task = AgentTask(owner_id=owner.id, title=title, context=context, notes=notes, kind="call", priority="high",
                     due_at=start.astimezone(timezone.utc), all_day=settings["style"] == "all_day",
                     auto_source=src, auto_source_id="sample", suggested_message=message or None)
    comments = None
    if key == "showcase_comment":
        comments = [{"text": "Love the backyard - is the basement finished?", "home": "22 Birch Ln, Wayne",
                     "url": done_url("sample").rsplit("/d/", 1)[0] + "/showcases?showcase=sample&property=22-birch"}]
    body = _task_event_body(task, None, followup_color(owner), done_url("sample"), owner=owner,
                            contact_link=contact_url("sample"), lead_name="Jane", comments=comments, lead=lead,
                            details=details, prop=prop, message_override=message)
    return {"summary": body.get("summary", ""), "description": body.get("description", ""), "all_day": task.all_day}


# ---------------------------------------------------------------- Showcase comments on a follow-up
async def lead_comment_details(db: AsyncSession, owner_id: str, tasks: list) -> dict[str, list[dict]]:
    """{task_id: [comment, ...]} - the Showcase comments this person left from just before the
    follow-up was made onward (newest first, max 3): what they said, which home, and a link that
    opens that exact home on the agent's Showcases page. Open follow-ups only."""
    import os
    from app.models.database import Collection, LeadEvent, Property, PropertyComment
    open_tasks = [t for t in tasks if t.lead_id and t.completed_at is None]
    if not open_tasks:
        return {}
    lead_ids = list({t.lead_id for t in open_tasks})
    rows = (await db.execute(
        select(LeadEvent.lead_id, LeadEvent.occurred_at, PropertyComment, Property)
        .join(PropertyComment, PropertyComment.id == LeadEvent.source_id)
        .join(Collection, Collection.id == PropertyComment.collection_id)
        .outerjoin(Property, Property.id == PropertyComment.property_id)
        .where(
            LeadEvent.lead_id.in_(lead_ids),
            LeadEvent.source_type == "property_comment",
            Collection.owner_id == owner_id,
        )
        .order_by(LeadEvent.occurred_at.desc())
    )).all()
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    by_lead: dict[str, list[tuple]] = {}
    for lead_id, at, comment, prop in rows:
        street = (getattr(prop, "street_address", None) or "").strip()
        city = (getattr(prop, "city", None) or "").strip()
        path = f"/showcases?showcase={comment.collection_id}&property={comment.property_id}&comments=1"  # opens the thread
        by_lead.setdefault(lead_id, []).append((at, {
            "text": (comment.content or "").strip()[:600],
            "home": ", ".join(x for x in [street, city] if x) or "a home in their Showcase",
            "path": path,
            "url": base + path,
        }))
    out: dict[str, list[dict]] = {}
    for t in open_tasks:
        made = t.created_at if t.created_at and t.created_at.tzinfo else (t.created_at or datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)
        since = made - timedelta(minutes=10)
        items = [c for at, c in by_lead.get(t.lead_id, []) if at and (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= since]
        if items:
            out[t.id] = items[:3]
    return out


async def fire_due_reminders(db: AsyncSession) -> int:
    """APScheduler job (every minute): for each open task whose remind_at has passed and whose
    reminder hasn't fired yet, drop an in-app Notification and stamp reminder_sent_at. Returns
    how many fired."""
    now = datetime.now(timezone.utc)
    rows = await db.execute(
        select(AgentTask).where(
            AgentTask.completed_at.is_(None),
            AgentTask.reminder_sent_at.is_(None),
            AgentTask.remind_at.is_not(None),
            AgentTask.remind_at <= now,
        ).limit(200)
    )
    tasks = rows.scalars().all()
    for t in tasks:
        db.add(Notification(
            agent_id=t.owner_id,
            type="TASK_REMINDER",
            reference_type="TASK",
            reference_id=t.id,
            title="Follow-up reminder",
            message=t.title,
            link="/leads/tasks",
            is_read=False,
        ))
        t.reminder_sent_at = now
    if tasks:
        await db.commit()
    return len(tasks)


# --------------------------------------------------------------------------- iCalendar feed

def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: no content line over 75 octets. Simple char-based fold is fine for our ASCII-ish text."""
    out = []
    while len(line) > 73:
        out.append(line[:73])
        line = " " + line[73:]
    out.append(line)
    return "\r\n".join(out)


def _dt_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def get_or_create_feed_token(db: AsyncSession, user: User) -> str:
    if not user.calendar_feed_token:
        user.calendar_feed_token = secrets.token_urlsafe(24)
        await db.commit()
    return user.calendar_feed_token


async def rotate_feed_token(db: AsyncSession, user: User) -> str:
    user.calendar_feed_token = secrets.token_urlsafe(24)
    await db.commit()
    return user.calendar_feed_token


async def render_ics(db: AsyncSession, user: User) -> str:
    """A read-only VCALENDAR of the agent's open tasks (that have a date) plus their upcoming
    scheduled open houses. Subscribed to by URL in Google / Outlook / Apple Calendar."""
    now = datetime.now(timezone.utc)
    horizon_past = now - timedelta(days=30)

    task_rows = await db.execute(
        select(AgentTask).where(
            AgentTask.owner_id == user.id,
            AgentTask.completed_at.is_(None),
            AgentTask.due_at.is_not(None),
            AgentTask.due_at >= horizon_past,
        ).order_by(AgentTask.due_at.asc()).limit(1000)
    )
    tasks = task_rows.scalars().all()
    lead_ids = list({t.lead_id for t in tasks if t.lead_id})
    showcases = {}
    phones: dict[str, bool] = {}
    if lead_ids:
        leads = (await db.execute(select(Lead).where(Lead.id.in_(lead_ids)))).scalars().all()
        showcases = await lead_showcase_info(db, user, list(leads))
        phones = {l.id: bool((l.phone or "").strip()) for l in leads}

    oh_rows = await db.execute(
        select(OpenHouseEvent).where(
            OpenHouseEvent.agent_id == user.id,
            OpenHouseEvent.is_deleted.is_(False),
            OpenHouseEvent.scheduled_date.is_not(None),
            OpenHouseEvent.scheduled_date >= (now.date() - timedelta(days=7)),
        ).limit(500)
    )
    opens = oh_rows.scalars().all()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OpenHousePal//Agent Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:OpenHousePal - {_ics_escape(user.first_name)} {_ics_escape(user.last_name)}",
        "X-PUBLISHED-TTL:PT15M",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
    ]

    for t in tasks:
        due = t.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:task-{t.id}@openhousepal")
        lines.append(f"DTSTAMP:{_dt_utc(t.updated_at or t.created_at or now)}")
        if carried_forward(t) or t.all_day:
            d = datetime.now(EASTERN).date() if carried_forward(t) else task_day(t)
            lines.append(f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{_dt_utc(due)}")
            lines.append(f"DTEND:{_dt_utc(due + timedelta(minutes=30))}")
        lines.append(_fold(f"SUMMARY:[{task_prefix(t)}] {_ics_escape(t.title)}"))
        sc = showcases.get(t.lead_id)
        code = await ensure_done_code(db, t)
        desc_parts = ["Mark as done: " + done_url(code)]
        if t.suggested_message and phones.get(t.lead_id or ""):
            desc_parts.append("Text them from your phone (opens Messages with the text written): " + text_url(code))
        if t.context:
            desc_parts.append(t.context)
        if sc:
            desc_parts.append("Their Showcase: " + sc["url"])
        if t.notes:
            desc_parts.append(t.notes)
        if t.suggested_message:
            desc_parts.append("Suggested message:\n" + (full_message(t, sc, user) or ""))
        if desc_parts:
            lines.append(_fold(f"DESCRIPTION:{_ics_escape(chr(10).join(desc_parts))}"))
        if t.priority == "high":
            lines.append("PRIORITY:1")
        lines.append("END:VEVENT")

    for oh in opens:
        d = oh.scheduled_date
        st = oh.scheduled_start_time or time(12, 0)
        et = oh.scheduled_end_time or time(14, 0)
        start = datetime.combine(d, st, tzinfo=EASTERN).astimezone(timezone.utc)
        end = datetime.combine(d, et, tzinfo=EASTERN).astimezone(timezone.utc)
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:openhouse-{oh.id}@openhousepal")
        lines.append(f"DTSTAMP:{_dt_utc(oh.created_at or now)}")
        lines.append(f"DTSTART:{_dt_utc(start)}")
        lines.append(f"DTEND:{_dt_utc(end)}")
        lines.append(_fold(f"SUMMARY:[Open House] {_ics_escape(oh.abbreviated_address or oh.address or 'Listing')}"))
        loc = ", ".join(b for b in [oh.address, oh.city, oh.state] if b)
        if loc:
            lines.append(_fold(f"LOCATION:{_ics_escape(loc)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


async def mark_lead_contacted(db: AsyncSession, task: AgentTask) -> None:
    """Checking off a follow-up means the agent reached out: stamp the lead's "last contacted" and
    move a New lead to Contacted (the Leads page and the follow-up stay in step). Caller commits."""
    if not task.lead_id:
        return
    lead = await db.get(Lead, task.lead_id)
    if lead is None:
        return
    lead.last_contacted_at = datetime.now(timezone.utc)
    if lead.status == "new":
        lead.status = "contacted"
    # The request this follow-up was made for is handled too (its to-do leaves the Leads page).
    if task.auto_source_id:
        from app.models.database import LeadEvent
        event = await db.get(LeadEvent, task.auto_source_id)
        if event is not None and event.lead_id == lead.id and event.agent_viewed_at is None:
            event.agent_viewed_at = lead.last_contacted_at
