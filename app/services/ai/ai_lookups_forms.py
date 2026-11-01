"""ai_lookups_forms - Forms & Invites read lookups for the AI helper (11/01, capability review:
"it can make a form or invite, but it can't show who answered or RSVP'd"). Loaded at the end of
ai_lookups.py and added into ai_lookups.LOOKUPS.

Every lookup first calls the page's own GET route as the agent (so the plan check and ownership
are the app's own), then reads only rows that belong to the forms / invite that route returned.

- get_form_responses: what people put on the agent's forms - by person and/or form, newest first.
- get_unfinished_form_responses: half-filled forms (left contact info, never pressed Submit).
- get_invite_guests: who's coming to an invite - yes / maybe / can't / no answer, with counts.
"""
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import FormSubmission, FormViewSession, User
from app.services.ai.ai_lookups import LOOKUPS, _obj
from app.services.ai.ai_route_bridge import RouteError, call_as_agent, compact

_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FILLER = {"the", "a", "an", "my", "mr", "mrs", "ms", "dr", "family", "and", "&", "form", "forms"}


def _s(v: Any) -> str:
    return str(v or "").strip()


def _plain_id(v: Any) -> Optional[str]:
    v = _s(v)
    return v if _ID.match(v) else None


# ---------------------------------------------------------------- matching helpers (shared with ai_actions_forms)

def person_words(text: str) -> List[str]:
    """'the Smiths' -> ['smiths']; 'Dana Whitfield' -> ['dana', 'whitfield']."""
    return [w for w in re.findall(r"[a-z0-9@._'-]+", (text or "").lower()) if w not in _FILLER and len(w) >= 2]


def person_matches(words: List[str], *fields: Optional[str]) -> bool:
    """Every word is in the name or email ('Smiths' also finds 'Smith' - a family said as a plural)."""
    hay = " ".join((f or "").lower() for f in fields)
    if not words:
        return True
    return all(w in hay or (len(w) > 3 and w.endswith("s") and w[:-1] in hay) for w in words)


_FORM_HINTS = {  # what an agent calls a premade form -> its form_type
    "buy": "buying", "buyer": "buying", "buyers": "buying", "buying": "buying",
    "sell": "selling", "seller": "selling", "sellers": "selling", "selling": "selling",
    "worth": "home_value", "value": "home_value", "valuation": "home_value",
    "showing": "showing", "tour": "showing", "schedule": "showing",
    "help": "how_can_i_help", "showcase": "showcase_request", "mailer": "make_me_sell",
}


def match_forms(forms: List[dict], text: str) -> List[dict]:
    """The forms a phrase like 'the buyer form' / 'home value' / 'open house survey' means - best
    title word overlap (and the premade kind it names), ties all kept."""
    words = [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _FILLER]
    if not words:
        return []
    kinds = {_FORM_HINTS[w] for w in words if w in _FORM_HINTS}
    if "onboarding" in words:
        kinds = {("buyer_onboarding" if k == "buying" else "seller_onboarding" if k == "selling" else k) for k in kinds}
    scored = []
    for f in forms:
        title = (f.get("title") or "").lower()
        tw = re.findall(r"[a-z0-9']+", title)
        score = sum(1 for w in words if any(t.startswith(w[:4]) or w.startswith(t[:4]) for t in tw if len(t) >= 3))
        if f.get("form_type") in kinds:
            score += 2
        if title == " ".join(words):
            score += 5
        if score:
            scored.append((score, f))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    return [f for s, f in scored if s == top]


def show_value(v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, list):
        return ", ".join(show_value(x) for x in v if show_value(x))
    if isinstance(v, dict):
        for k in ("formatted", "address", "label", "value", "text"):
            if v.get(k):
                return show_value(v[k])
        return ", ".join(f"{k}: {show_value(x)}" for k, x in v.items() if show_value(x))[:300]
    return str(v)[:500]


def answer_rows(answers: Iterable[dict], skip_contact: bool = True) -> List[dict]:
    out = []
    for a in answers or []:
        if not isinstance(a, dict):
            continue
        label = _s(a.get("label")) or _s(a.get("questionId"))
        if skip_contact and (a.get("questionId") in ("full_name", "name", "email", "phone") or label.lower() in ("full name", "email", "phone", "email address", "phone number")):
            continue
        val = show_value(a.get("value"))
        if label and val and a.get("type") != "statement":
            out.append({"question": label[:200], "answer": val})
    return out


def when_text(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    d = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return d.isoformat(timespec="minutes")


async def my_forms(user: User) -> tuple[Optional[List[dict]], Optional[dict]]:
    """The agent's forms through the Forms page's own route (its plan check) -> (forms, error)."""
    try:
        forms = await call_as_agent(user, "GET", "/agent-hub/forms/me")
    except RouteError as e:
        return None, {"error": "lookup_failed", "message": str(e)}
    return [f for f in (forms or []) if isinstance(f, dict)], None


def _pick_forms(forms: List[dict], args: dict) -> tuple[List[dict], Optional[dict]]:
    fid = _plain_id(args.get("form_id"))
    if fid:
        hit = [f for f in forms if f.get("id") == fid]
        return (hit, None) if hit else ([], {"error": "not_found", "message": "That isn't one of their forms - use list_my_forms for the ids."})
    if _s(args.get("form")):
        hit = match_forms(forms, args["form"])
        if not hit:
            return [], {"error": "no_such_form", "message": f"None of their forms sounds like “{_s(args['form'])}”.",
                        "forms": [{"form_id": f["id"], "title": f.get("title")} for f in forms][:25]}
        return hit, None
    return forms, None


# ---------------------------------------------------------------- 1. form responses

async def _form_responses(db: AsyncSession, user: User, args: dict) -> Any:
    forms, err = await my_forms(user)
    if err:
        return err
    picked, err = _pick_forms(forms, args)
    if err:
        return err
    by_id = {f["id"]: f for f in picked}
    if not by_id:
        return {"count": 0, "responses": [], "note": "They have no forms yet."}
    try:
        limit = max(1, min(int(args.get("limit") or 10), 25))
    except (TypeError, ValueError):
        limit = 10
    words = person_words(_s(args.get("person")))
    stmt = select(FormSubmission).where(FormSubmission.owner_id == user.id, FormSubmission.form_id.in_(list(by_id)))
    if args.get("only_unreviewed"):
        stmt = stmt.where(FormSubmission.is_reviewed.is_(False))
    if words:
        # a cheap first cut in the database (any word), then the exact check below
        stmt = stmt.where(or_(*[or_(FormSubmission.contact_name.ilike(f"%{w.rstrip('s') if len(w) > 3 else w}%"),
                                    FormSubmission.contact_email.ilike(f"%{w.rstrip('s') if len(w) > 3 else w}%")) for w in words]))
    rows = (await db.execute(stmt.order_by(FormSubmission.created_at.desc()).limit(300))).scalars().all()
    rows = [r for r in rows if person_matches(words, r.contact_name, r.contact_email)]
    unreviewed = sum(1 for r in rows if not r.is_reviewed)
    out = []
    for r in rows[:limit]:
        f = by_id.get(r.form_id) or {}
        item = {"submission_id": r.id, "form_id": r.form_id, "form": f.get("title"), "name": r.contact_name, "email": r.contact_email,
                "phone": r.contact_phone, "when": when_text(r.created_at), "reviewed": bool(r.is_reviewed), "answers": answer_rows(r.answers)}
        if r.property_address:
            item["home"] = r.property_address
        if isinstance(r.showing, dict) and r.showing:
            item["showing"] = {k: r.showing.get(k) for k in ("preferred", "confirmed_date", "confirmed_time", "note") if r.showing.get(k)}
        out.append(item)
    res = {"count": len(rows), "showing": len(out), "not_reviewed": unreviewed, "responses": compact(out, max_list=25, max_str=400)}
    if not rows:
        res["note"] = ("Nobody by that name has filled in " + ("that form" if args.get("form_id") or args.get("form") else "any of their forms")
                       + ". Unfinished (half-filled) ones are in get_unfinished_form_responses.")
    elif len(rows) > len(out):
        res["note"] = f"Showing the newest {len(out)} of {len(rows)} - ask for more or narrow it by form or person."
    if len(picked) > 1 and (args.get("form") or ""):
        res["forms_matched"] = [f.get("title") for f in picked]
    return res


LOOKUPS["get_form_responses"] = (
    "Read what people actually put on the agent's forms (Forms & Invites -> Results): each response with the form, the person's name, email, phone, "
    "when, whether it's been reviewed, and every question with their answer, newest first. Filter by person (a name or email - 'the Smiths' works) "
    "and/or form (form_id from list_my_forms, or words like 'buyer form', 'home value', 'showing'). "
    "Use for: 'what did the Smiths put on the buyer form?', 'any new form responses?', 'what did Dana say she's looking for?'. "
    "submission_id is what mark_form_response_reviewed needs.",
    _obj({"person": {"type": "string"}, "form_id": {"type": "string"}, "form": {"type": "string"},
          "only_unreviewed": {"type": "boolean"}, "limit": {"type": "integer"}}),
    None, _form_responses)


# ---------------------------------------------------------------- unfinished (half-filled) responses

async def unfinished_for(user: User, db: AsyncSession, forms: List[dict]) -> List[dict]:
    """Unfinished responses through the Results -> Unfinished route, for the forms that have any."""
    ids = [f["id"] for f in forms]
    have = set((await db.execute(select(FormViewSession.form_id).where(
        FormViewSession.owner_id == user.id, FormViewSession.form_id.in_(ids or [""]), FormViewSession.completed_at.is_(None),
        FormViewSession.dismissed_at.is_(None), FormViewSession.partial_answers.is_not(None)).distinct())).scalars().all())
    out = []
    for f in forms:
        if f["id"] not in have:
            continue
        try:
            data = await call_as_agent(user, "GET", f"/agent-hub/forms/{f['id']}/unfinished")
        except RouteError:
            continue
        for it in (data or {}).get("items") or []:
            out.append({**it, "_form_id": f["id"], "_form": f.get("title")})
    out.sort(key=lambda it: it.get("lastActivityAt") or "", reverse=True)
    return out


async def _unfinished(db: AsyncSession, user: User, args: dict) -> Any:
    forms, err = await my_forms(user)
    if err:
        return err
    picked, err = _pick_forms(forms, args)
    if err:
        return err
    words = person_words(_s(args.get("person")))
    items = [it for it in await unfinished_for(user, db, picked) if person_matches(words, it.get("contactName"), it.get("contactEmail"))]
    reachable = lambda it: bool(it.get("contactEmail") or it.get("contactPhone"))  # noqa: E731
    items.sort(key=lambda it: not reachable(it))   # people who left an email or phone first (stable: newest first within)
    out = [{"session_id": it["id"], "form_id": it["_form_id"], "form": it["_form"], "name": it.get("contactName"), "email": it.get("contactEmail"),
            "phone": it.get("contactPhone"), "last_active": it.get("lastActivityAt"), "filling_it_out_now": it.get("inProgress"),
            "answered": f"{min(it.get('answeredCount') or 0, it.get('totalQuestions') or 0) or it.get('answeredCount')} of {it.get('totalQuestions')}", "stopped_at": it.get("lastQuestionLabel"),
            "finished_later": it.get("finishedLater"), "already_in_leads": bool(it.get("leadId")), "can_add_to_leads": reachable(it) and not it.get("leadId"),
            "answers": [{"question": a.get("label"), "answer": show_value(a.get("value"))} for a in (it.get("answers") or []) if isinstance(a, dict)][:15]}
           for it in items[:20]]
    return {"count": len(items), "with_contact_info": sum(1 for it in items if reachable(it)), "unfinished": compact(out, max_list=20, max_str=300),
            "note": "These people started a form but never pressed Submit. add_unfinished_form_response_to_leads adds one to Leads (needs session_id and form_id)."}


LOOKUPS["get_unfinished_form_responses"] = (
    "Half-filled forms: people who started one of the agent's forms and left contact info but never pressed Submit - name, email, phone, how far they got, "
    "what they answered, and whether they're already in Leads. Filter by person and/or form (form_id or words like 'seller form'). "
    "Use for: 'who started my home value form but didn't finish?', 'any unfinished forms?'.",
    _obj({"person": {"type": "string"}, "form_id": {"type": "string"}, "form": {"type": "string"}}),
    None, _unfinished)


# ---------------------------------------------------------------- 2. who's coming to an invite

async def resolve_invite(user: User, args: dict) -> tuple[Optional[dict], Optional[dict]]:
    """The invite they mean: invite_id, or words from its title, or - when they have just one
    upcoming invite - that one. -> (invite from the list route, error)."""
    try:
        invites = await call_as_agent(user, "GET", "/invites")
    except RouteError as e:
        return None, {"error": "lookup_failed", "message": str(e)}
    invites = [i for i in invites or [] if isinstance(i, dict)]
    brief = lambda rows: [{"invite_id": i["id"], "title": i.get("title"), "when": i.get("when_label"), "status": i.get("status")} for i in rows][:15]  # noqa: E731
    iid = _plain_id(args.get("invite_id"))
    if iid:
        hit = next((i for i in invites if i.get("id") == iid), None)
        return (hit, None) if hit else (None, {"error": "not_found", "message": "That isn't one of their invites.", "invites": brief(invites)})
    text = _s(args.get("invite"))
    exact = [i for i in invites if text and (i.get("title") or "").strip().lower() == text.lower()]
    if len(exact) == 1:
        return exact[0], None
    if text:
        words = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _FILLER and w not in ("invite", "invitation", "rsvp", "rsvps")]
        scored = [(sum(1 for w in words if w in (i.get("title") or "").lower() or w in (i.get("address") or "").lower()), i) for i in invites]
        top = max((s for s, _ in scored), default=0)
        hits = [i for s, i in scored if s == top and s > 0]
        if len(hits) == 1:
            return hits[0], None
        if hits:
            return None, {"error": "which_invite", "message": "More than one invite matches - ask which one.", "invites": brief(hits)}
        return None, {"error": "which_invite", "message": f"None of their invites sounds like “{text}”.", "invites": brief(invites)}
    upcoming = [i for i in invites if not i.get("is_past")]
    if len(upcoming) == 1:
        return upcoming[0], None
    if not invites:
        return None, {"error": "no_invites", "message": "They haven't made an invite yet."}
    return None, {"error": "which_invite", "message": "Which invite? Ask them (or use the newest that fits what they said).", "invites": brief(upcoming or invites)}


async def _invite_guests(db: AsyncSession, user: User, args: dict) -> Any:
    from app.models.database import EventInvite
    from app.services import invites_service as svc
    inv, err = await resolve_invite(user, args)
    if err:
        return err
    try:
        detail = await call_as_agent(user, "GET", f"/invites/{inv['id']}")
    except RouteError as e:
        return {"error": "lookup_failed", "message": str(e)}
    rsvps = [r for r in (detail.get("rsvps") or []) if isinstance(r, dict)]
    row = await db.get(EventInvite, inv["id"])
    waiting = await svc.non_responders(db, row) if row is not None and row.owner_id == user.id else []

    def guest(r: dict) -> dict:
        g = {"name": r.get("name"), "email": r.get("email"), "phone": r.get("phone"), "answered": (r.get("updated_at") or r.get("created_at") or "")[:16] or None}
        if r.get("response") != "no" and (r.get("guest_count") or 1) > 1:
            g["people"] = r.get("guest_count")
        if r.get("days"):
            g["days"] = r["days"]
        if r.get("is_agent"):
            g["is_agent"] = True
        if r.get("has_agent") is not None:
            g["has_agent"] = r["has_agent"]
        if r.get("still_interested"):
            g["still_interested"] = True
        asks = [k for k in (r.get("requests") or {}) if k]
        if asks:
            g["asked_for"] = asks
        ans = answer_rows(r.get("answers") or [])
        if ans:
            g["answers"] = ans
        if r.get("lead_id"):
            g["lead_id"] = r["lead_id"]
        return g

    group = lambda resp: [guest(r) for r in rsvps if r.get("response") == resp]  # noqa: E731
    counts = detail.get("counts") or {}
    out = {
        "invite_id": inv["id"], "invite": detail.get("title"), "when": detail.get("when_label"), "is_past": detail.get("is_past"),
        "rsvps_open": detail.get("status") == "live", "times_opened": detail.get("view_count"),
        "counts": {"coming": counts.get("yes", 0), "maybe": counts.get("maybe", 0), "cant_make_it": counts.get("no", 0),
                   "no_answer_yet": len(waiting), "people_coming_total": counts.get("guests_yes", 0), "emailed_from_the_invite": (detail.get("recipients") or {}).get("total", 0)},
        "coming": compact(group("yes"), max_list=40, max_str=200), "maybe": compact(group("maybe"), max_list=25, max_str=200),
        "cant_make_it": compact(group("no"), max_list=25, max_str=200),
        "no_answer_yet": [{"name": n, "email": e} for e, n in waiting][:40],
    }
    if waiting and detail.get("status") == "live":
        out["note"] = "remind_invite_non_responders emails the no-answer people a reminder (only people emailed from the invite)."
    return out


LOOKUPS["get_invite_guests"] = (
    "Who's coming to one of the agent's invites (Forms & Invites -> Invites): everyone who answered yes / maybe / can't make it with their name, email, phone, "
    "how many people, which days, their answers and requests, plus who was emailed and hasn't answered yet, and the counts. "
    "Pick the invite by invite_id (from list_my_invites) or words from its title/address ('the Elm St open house', 'holiday party'). "
    "Use for: 'who's coming to my broker's open?', 'how many RSVPs for Saturday?', 'who hasn't answered the party invite?'.",
    _obj({"invite_id": {"type": "string"}, "invite": {"type": "string"}}),
    None, _invite_guests)
