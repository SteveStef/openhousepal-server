"""One agent's email settings (Studio -> Email). Stored on User.email_settings (NULL = defaults).

Two sides, both only ever RESTRICT what the default rules already send - nothing here can make an
agent's clients (or the agent) get more email than an agent who never opened this page:

- Client emails (sending side): each kind can be switched off, Showcase updates can be slowed to
  weekly, and each kind can go out from the agent's own mailbox or from OpenHousePal. The Showcase
  invite after an open house can go out sooner than the 1-hour default (it is still exactly one
  email). Enforced in client_email_service.before_send (on / weekly - every client send passes
  through it) and agent_mailbox_service.send_guest (which address).
- Emails to the agent (receiving side): the existing per-category switches in
  notification_preferences_service, plus the time their daily briefing arrives.

Emails that can't be turned off (and so aren't listed): sign-up codes, password resets, billing and
account notices, and anything a person must get for the product to work."""
from __future__ import annotations

from typing import Any, Optional

# key -> what it is. tier 1 = the emails that win clients; tier 2 = automatic updates.
CLIENT_EMAILS: list[dict[str, Any]] = [
    {"key": "showcase_invite", "tier": 1, "templates": ["visitor_confirmation"],
     "label": "Showcase invite after your open house",
     "help": "A short personal note to each guest who signs in, with their own Showcase of homes like yours.",
     "delay": True},
    {"key": "showcase_shared", "tier": 1, "templates": ["showcase_shared"],
     "label": "Showcases you make for a client",
     "help": "When you build a Showcase by hand and choose to email it to them."},
    {"key": "showcase_ready", "tier": 1, "templates": ["showcase_ready"],
     "label": "Showcase link when someone makes their own",
     "help": "Someone filled in a form, your website or Guest Pass and asked for homes - they get their link right away."},
    {"key": "tour_confirmed", "tier": 1, "templates": ["tour_confirmed"],
     "label": "Tour confirmations",
     "help": "When you confirm a showing they asked for - the day and time, in writing."},
    {"key": "comment_reply", "tier": 1, "templates": ["agent_comment_response"],
     "label": "Your replies to their comments",
     "help": "When you answer a comment they left on a home in their Showcase."},
    {"key": "showcase_updates", "tier": 2, "templates": ["showcase_daily_digest", "showcase_weekly_digest"],
     "label": "New homes & price drops in their Showcase",
     "help": "One email a day at most (4:30 PM), only on days something changed. Weekly sends one round-up a week instead.",
     "modes": ["daily", "weekly"]},
    {"key": "spotlight_alerts", "tier": 2, "templates": ["spotlight_alert_confirm", "spotlight_listing_update"],
     "label": "Listing Spotlight alerts",
     "help": "People who asked to hear about one of your listings: price changes, open houses, status news."},
]
_BY_KEY = {c["key"]: c for c in CLIENT_EMAILS}
TEMPLATE_KEY = {t: c["key"] for c in CLIENT_EMAILS for t in c["templates"]}

VIA = ("mine", "openhousepal")
INVITE_DELAYS = (0, 15, 30, 60)          # minutes; 60 = the default
DEFAULT_INVITE_DELAY = 60
DEFAULT_BRIEFING = "16:30"               # Eastern
# Every quarter hour from 6 AM to 9 PM - the briefing job runs every few minutes.
BRIEFING_TIMES = tuple(f"{h:02d}:{m:02d}" for h in range(6, 21) for m in (0, 15, 30, 45)) + ("21:00",)

# Receiving side: notification_preferences_service categories, by how much they matter.
RECEIVE_TIERS: list[dict[str, Any]] = [
    {"tier": 1, "title": "Most important", "blurb": "Someone wants you right now - leave these on.",
     "categories": ["leads", "showcase_requests", "host_requests"]},
    {"tier": 2, "title": "Good to know", "blurb": "Round-ups and news, at most a few a day.",
     "categories": ["showcase_digest", "liked_homes", "reviews", "spotlight_weekly", "team"]},
    {"tier": 3, "title": "Extras", "blurb": "Nice to have - most agents leave these off.",
     "categories": ["showcase_activity", "listing_updates", "ai_media"]},
]
ALWAYS_ON = ["Sign-in codes and password resets", "Billing and plan notices", "Support replies and connection problems"]


def defaults() -> dict:
    return {
        "client": {c["key"]: {"on": True, "via": "mine", **({"mode": "daily"} if c.get("modes") else {})} for c in CLIENT_EMAILS},
        "invite_delay": DEFAULT_INVITE_DELAY,
        "briefing_time": DEFAULT_BRIEFING,
    }


def resolve(stored: Optional[dict]) -> dict:
    out = defaults()
    if not isinstance(stored, dict):
        return out
    for key, v in (stored.get("client") or {}).items():
        if key not in out["client"] or not isinstance(v, dict):
            continue
        row = out["client"][key]
        if isinstance(v.get("on"), bool):
            row["on"] = v["on"]
        if v.get("via") in VIA:
            row["via"] = v["via"]
        if "mode" in row and v.get("mode") in (_BY_KEY[key].get("modes") or ()):
            row["mode"] = v["mode"]
    if stored.get("invite_delay") in INVITE_DELAYS:
        out["invite_delay"] = stored["invite_delay"]
    if stored.get("briefing_time") in BRIEFING_TIMES:
        out["briefing_time"] = stored["briefing_time"]
    return out


def sanitize(incoming: dict, current: Optional[dict]) -> dict:
    """Merge an edit onto what's stored; anything unknown or out of range is dropped."""
    merged = resolve(current)
    patch = incoming if isinstance(incoming, dict) else {}
    for key, v in (patch.get("client") or {}).items():
        if key in merged["client"] and isinstance(v, dict):
            row = merged["client"][key]
            if isinstance(v.get("on"), bool):
                row["on"] = v["on"]
            if v.get("via") in VIA:
                row["via"] = v["via"]
            if "mode" in row and v.get("mode") in (_BY_KEY[key].get("modes") or ()):
                row["mode"] = v["mode"]
    if patch.get("invite_delay") in INVITE_DELAYS:
        merged["invite_delay"] = patch["invite_delay"]
    if patch.get("briefing_time") in BRIEFING_TIMES:
        merged["briefing_time"] = patch["briefing_time"]
    return merged


def client_rule(user, template: str) -> Optional[dict]:
    """The agent's setting for this client template ({"on", "via", "mode"?}) or None when the
    template isn't one an agent controls."""
    key = TEMPLATE_KEY.get(template)
    if not key or user is None:
        return None
    return resolve(getattr(user, "email_settings", None))["client"][key]


def invite_delay_minutes(user) -> int:
    return resolve(getattr(user, "email_settings", None))["invite_delay"] if user is not None else DEFAULT_INVITE_DELAY


def briefing_hm(user) -> tuple[int, int]:
    t = resolve(getattr(user, "email_settings", None) if user is not None else None)["briefing_time"]
    h, m = t.split(":")
    return int(h), int(m)


def briefing_label(user) -> str:
    h, m = briefing_hm(user)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
