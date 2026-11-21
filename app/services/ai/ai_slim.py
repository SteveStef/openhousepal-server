"""What the AI chat's list lookups hand the model, cut to what it uses to answer (owner 11/19: "if we're feeding the AI
a ton of stuff it doesn't even want to know ... we shouldn't be doing it" - without the answers getting any worse).

A list says enough to answer about the items and pick one (who, what, when, status, counts, ids); everything else
about one item comes from that item's own lookup (get_form_questions, get_showcase, get_lead_detail, a follow-up list
filtered to one person...). Empty fields (null) are left out everywhere - a missing field means none.

Tested 11/19-20 in the real chat, every question asked the old way and slim minutes apart (57 questions): same answers,
18% less AI cost, 28% less text read.
"""
import re
from typing import Any


def drop_none(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: drop_none(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [drop_none(v) for v in x]
    return x


def _items(data: Any, fn) -> Any:
    """Apply fn to each item of a route's list (bare list, or the first list in a dict)."""
    if isinstance(data, list):
        return [fn(i) if isinstance(i, dict) else i for i in data]
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                return {**data, k: [fn(i) if isinstance(i, dict) else i for i in v]}
    return data


def _keep(d: dict, keys) -> dict:
    return {k: d[k] for k in keys if k in d}


# --- Follow-ups -----------------------------------------------------------------------------------------------------
_CONTACT_LINE = re.compile(r"^\s*Contact:.*$\n?", re.M)


def followups(data: Any, one_person: bool = False) -> Any:
    """A list of follow-ups: what, who, when, about which home. The written message (~250 characters each) only when the
    list is one person's - that's when it's asked for ("what should I text her?"); the contact line in auto notes repeats
    the lead's own phone/email; the listing agent's details are the home's own (get_property_detail)."""
    def one(t: dict) -> dict:
        out = _keep(t, ("id", "title", "kind", "priority", "due_at", "all_day", "bucket", "completed_at", "auto_source",
                        "context", "has_message", "details", "requested_times", "visits", "request", "showcase_url"))
        if t.get("remind_at") and t.get("remind_at") != t.get("due_at"):
            out["remind_at"] = t["remind_at"]
        if t.get("on_calendar"):
            out["on_calendar"] = True
        notes = _CONTACT_LINE.sub("", t.get("notes") or "").replace("Auto-created: ", "").strip()
        if notes:
            out["notes"] = notes
        if t.get("comments"):
            out["comments"] = t["comments"]
        lead = t.get("lead")
        if isinstance(lead, dict):
            out["lead"] = _keep(lead, ("id", "name", "phone", "email"))
        elif t.get("lead_id"):
            out["lead_id"] = t["lead_id"]
        p = t.get("property")
        if isinstance(p, dict):
            out["property"] = _keep(p, ("short", "mls", "where", "url") if p.get("short") else ("address", "mls", "where", "url"))
        if one_person and t.get("suggested_message"):
            out["suggested_message"] = t["suggested_message"]
        return out
    out = _items(data, one)
    if not one_person and isinstance(out, list) and any(isinstance(t, dict) and t.get("has_message") for t in data or []):
        out = {"followups": out, "note": "Each follow-up's written message (the text the agent sends) is left out of this list. When they ask what to say, text, email or write to someone here, call list_followups with that person's lead_id and give them that written message word for word (has_message says one exists) - never make one up."}
    if isinstance(out, dict) and isinstance(out.get("followups"), list):
        out["count"] = len(out["followups"])   # the model miscounted long lists (A/B 11/19) - the number is given
    return out


def followup_rules(data: Any) -> Any:
    """Each rule's message once: its default is only different when the agent changed it (custom_*)."""
    def one(r: dict) -> dict:
        r = dict(r)
        if not r.get("custom_template"):
            r.pop("default_template", None)
        if not r.get("custom_pitch"):
            r.pop("default_pitch", None)
        return r
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        return {**data, "rules": [one(r) if isinstance(r, dict) else r for r in data["rules"]]}
    return data


# --- Showcases ------------------------------------------------------------------------------------------------------
def showcases(data: Any) -> Any:
    """Every Showcase: who, status, counts and its search (only the parts set). Its homes are get_showcase_homes'."""
    def one(c: dict) -> dict:
        c = {k: v for k, v in c.items() if k not in ("matchedProperties", "originalProperty")}
        if isinstance(c.get("preferences"), dict):
            c["preferences"] = {k: v for k, v in c["preferences"].items() if v not in (None, [], {}, False, "")}
        return c
    out = _items(data, one)
    return {"showcases": out, "count": len(out)} if isinstance(out, list) else out


# --- Forms / Studio ---------------------------------------------------------------------------------------------------
_FORM_KEYS = ("id", "form_type", "slug", "title", "headline", "is_enabled", "linked_property", "additional_campaign_count",
              "redirect_mode", "redirect_url", "look_mode", "created_at", "responses", "response_count", "submissions")


def forms(data: Any) -> Any:
    """Each form: name, kind, on/off, link, how many questions. Its questions are get_form_questions'."""
    def one(f: dict) -> dict:
        out = _keep(f, _FORM_KEYS)
        if isinstance(f.get("questions"), list):
            out["question_count"] = len(f["questions"])
        return out
    out = _items(data, one)
    if isinstance(out, list):
        out = {"forms": out, "count": len(out), "note": "Questions are left out - get_form_questions has one form's questions."}
    return out


def spotlights(data: Any) -> Any:
    """Each Spotlight: which home, its design, status and link - not the whole page's words (draft_data)."""
    def one(s: dict) -> dict:
        out = _keep(s, ("id", "listing_key", "template_id", "slug", "status", "published_at", "created_at", "updated_at", "public_url"))
        dd = s.get("draft_data") if isinstance(s.get("draft_data"), dict) else {}
        for k in ("address", "title", "headline", "price"):
            if dd.get(k):
                out[k] = dd[k]
        return out
    return _items(data, one)


def designs(data: Any) -> Any:
    """Each design: kind, situation, name, status, which home - not its blocks or the home's full listing."""
    def one(d: dict) -> dict:
        out = {k: v for k, v in d.items() if k not in ("blocks", "linked_property")}
        lp = d.get("linked_property")
        if isinstance(lp, dict):
            out["linked_property"] = _keep(lp, ("listing_key", "address", "city", "price", "status"))
        return out
    return _items(data, one)


def email_log(data: Any) -> Any:
    """Each email: to, subject, kind, sent / opened / clicked - not a copy of what was inside it."""
    return _items(data, lambda e: {k: v for k, v in e.items() if k != "contents"})


# --- Leads (search_leads) -------------------------------------------------------------------------------------------
_LEAD_FLAGS = ("needs_attention", "has_liked_property", "has_tour_request", "has_commented")


def lead_row(r: dict) -> dict:
    """One lead in a list: the yes/no flags only when yes, counts only when there are some."""
    out = {}
    for k, v in r.items():
        if v is None or v == [] or (k in _LEAD_FLAGS and v is False) or (k == "showcase_new_matches" and not v):
            continue
        out[k] = v
    return out


# "What should I say to <them>?" without reading their follow-up first (ai_orchestrator_service._ASKS_WHAT_TO_SAY).
WHAT_TO_SAY_NOTE = ("(App note: they asked what to say - give that person's follow-up written message. Call list_followups with their "
                    "lead_id (the person they named, or the first one in your last answer) and give the message word for word, then "
                    "offer to change it. Only if they have no follow-up message, write a short one and say it's a suggestion.)")
