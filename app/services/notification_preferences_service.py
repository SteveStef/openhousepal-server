"""Per-agent notification preferences.

Model: the in-app bell always fires for every event - it is never gated here. This module only
decides whether the *email* and *push* channels are allowed for a given event, per agent.

Storage: User.notification_prefs, a JSONB dict shaped {category_id: {"email": bool, "push": bool}}.
A missing category, a missing channel, or a NULL column all mean "use the default" (see
_CATEGORY_DEFAULTS). So an agent who never touches Settings has every channel at its default and
we store nothing.

Wiring: every notification email goes through agent_email_service.email_agent, which calls
`channel_enabled(user, notification_type, "email")` before sending (push isn't built yet).
"""
from __future__ import annotations

from typing import Dict


# category id -> (label, help text, [notification .type strings that belong to it])
CATEGORIES: Dict[str, dict] = {
    "leads": {
        "label": "New leads & sign-ins",
        "help": "Someone submitted one of your forms or asked for something, and a wrap-up of each open house's guests (sent 2 hours after the last sign-in).",
        "types": [
            "OPEN_HOUSE_SIGN_IN", "FORM_SUBMISSION", "OPEN_HOUSE_FORM_REQUEST", "PROPERTY_INQUIRY",
            "OPEN_HOUSE_DISCLOSURE", "OPEN_HOUSE_SECOND_VISIT", "PORTAL_LEAD", "LEAD",
        ],
    },
    # Direct asks from a client - each one also makes a follow-up, so they email right away by
    # default (comments + showing requests always did; the switch now actually controls them).
    "showcase_requests": {
        "label": "Client requests in Showcases",
        "help": "A client commented on a home, asked to see one, or messaged you to change their search.",
        "types": [
            "PROPERTY_COMMENT", "COMMENT", "REFINE_SEARCH_REQUEST", "TOUR_REQUEST", "TOUR",
        ],
    },
    "showcase_activity": {
        "label": "Showcase activity",
        "help": "A client liked or passed on a home, or changed their own search, in a Showcase you sent (each one, as it happens).",
        "types": [
            "PROPERTY_INTERACTION", "INTERACTION", "SHOWCASE_SEARCH_CHANGED",
        ],
    },
    "showcase_digest": {
        "label": "Daily briefing",
        "help": "One email a day: your follow-ups, anything waiting on you, and new matches & price drops in your clients' Showcases. Skipped on days with nothing to report.",
        "types": ["SHOWCASE_DIGEST"],
    },
    "spotlight_weekly": {
        "label": "Spotlight weekly report (Mondays)",
        "help": "Monday morning: how many people visited each of your Listing Spotlight pages last week, and who reached out. Skipped on quiet weeks.",
        "types": ["SPOTLIGHT_WEEKLY"],
    },
    "liked_homes": {
        "label": "Liked homes (hourly)",
        "help": "When clients heart homes in their Showcases - one email per hour covering every like, never one per click.",
        "types": ["LIKED_HOMES"],
    },
    "host_requests": {
        "label": "Open house host requests",
        "help": "Another agent asked to host one of your listings, or responded to a request you sent.",
        "types": [
            "HOST_REQUEST", "HOST_REQUEST_RECEIVED", "HOST_REQUEST_GROUP", "DIRECT_HOST_REQUEST_RECEIVED",
            "HOST_REQUEST_APPROVED", "HOST_REQUEST_DECLINED",
            "HOST_REQUEST_COUNTERED", "HOST_REQUEST_COUNTER_RESPONSE", "HOST_REQUEST_WITHDRAWN",
        ],
    },
    "team": {
        "label": "Team & broadcasts",
        "help": "Team invites, and open house opportunities broadcast to your team.",
        "types": [
            "TEAM_INVITE_RECEIVED", "TEAM_INVITE_ACCEPTED", "TEAM_MEMBER",
            "TEAM_BROADCAST", "TEAM_BROADCAST_RECEIVED", "TEAM_BROADCAST_CLAIMED", "TEAM_BROADCAST_RESPONSE",
        ],
    },
    "listing_updates": {
        "label": "My listing updates",
        "help": "One of your MLS listings changed (price, status), or a teammate added a new listing.",
        "types": ["PROPERTY_SYNC_UPDATE", "TEAM_NEW_LISTING"],
    },
    "ai_media": {
        "label": "AI media ready",
        "help": "A Studio image or video you generated finished processing.",
        "types": ["STUDIO_MEDIA_ASSET", "STUDIO_MEDIA_VIDEO_READY", "STUDIO_MEDIA_VIDEO_FAILED"],
    },
    "reviews": {
        "label": "New reviews",
        "help": "A client left you a review (or private feedback) through your review link.",
        "types": ["NEW_REVIEW", "REVIEW"],
    },
    "account": {
        "label": "Account & support",
        "help": "Replies to your support tickets, your custom domain going live, and connection problems (calendar, CRM).",
        "types": ["SUPPORT_TICKET", "SUPPORT_TICKET_REPLY", "DOMAIN_VERIFIED", "DOMAIN_FAILED",
                  "CALENDAR_DISCONNECTED", "CRM_PUSH_FAILING"],
    },
}

CHANNELS = ("email", "push")

# Default state per channel, per category. Push is off by default everywhere (it doesn't exist
# yet); email defaults on for everything except the noisier low-signal categories.
_CATEGORY_DEFAULTS: Dict[str, Dict[str, bool]] = {
    "leads":             {"email": True,  "push": False},
    "showcase_requests": {"email": True,  "push": False},
    "showcase_activity": {"email": False, "push": False},
    "showcase_digest":   {"email": True,  "push": False},
    "spotlight_weekly":  {"email": True,  "push": False},
    "liked_homes":       {"email": True,  "push": False},
    "host_requests":     {"email": True,  "push": False},
    "team":              {"email": True,  "push": False},
    "listing_updates":   {"email": False, "push": False},
    "ai_media":          {"email": False, "push": False},
    "reviews":           {"email": True,  "push": False},
    "account":           {"email": True,  "push": False},
}

_TYPE_TO_CATEGORY = {
    t: cat_id for cat_id, cat in CATEGORIES.items() for t in cat["types"]
}


def default_prefs() -> Dict[str, Dict[str, bool]]:
    """The full, resolved default matrix - what the Settings screen shows before any edit."""
    return {cat_id: dict(_CATEGORY_DEFAULTS[cat_id]) for cat_id in CATEGORIES}


def resolve_prefs(stored: dict | None) -> Dict[str, Dict[str, bool]]:
    """Merge whatever is stored on the user over the defaults, so the result always has every
    category and every channel present."""
    out = default_prefs()
    if isinstance(stored, dict):
        for cat_id, channels in stored.items():
            if cat_id in out and isinstance(channels, dict):
                for ch in CHANNELS:
                    if ch in channels:
                        out[cat_id][ch] = bool(channels[ch])
    return out


def sanitize_incoming(payload: dict | None) -> Dict[str, Dict[str, bool]]:
    """Keep only known categories/channels from a client payload, coerced to bools. Anything
    missing falls back to the default for that cell."""
    resolved = default_prefs()
    if isinstance(payload, dict):
        for cat_id, channels in payload.items():
            if cat_id in resolved and isinstance(channels, dict):
                for ch in CHANNELS:
                    if ch in channels:
                        resolved[cat_id][ch] = bool(channels[ch])
    return resolved


def channel_enabled(user, notification_type: str, channel: str) -> bool:
    """Should `channel` ("email" | "push") be sent to `user` for a notification of this type?
    Unknown types default to enabled for email (fail open - better a stray email than a silently
    dropped account notice). Call this from the email/push sender once those exist."""
    if channel not in CHANNELS:
        return False
    cat_id = _TYPE_TO_CATEGORY.get(notification_type)
    if cat_id is None:
        return channel == "email"
    return resolve_prefs(getattr(user, "notification_prefs", None))[cat_id][channel]


def categories_payload() -> list[dict]:
    """Static metadata the Settings screen renders its rows from."""
    return [
        {"id": cat_id, "label": cat["label"], "help": cat["help"]}
        for cat_id, cat in CATEGORIES.items()
    ]
