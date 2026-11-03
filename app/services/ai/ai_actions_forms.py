"""ai_actions_forms - Forms & Invites follow-through cards for the AI helper (11/01, capability
review: "it stops halfway on Forms & Invites"). Loaded at the end of ai_actions_catalog.py.

Each card's `run` calls the Forms & Invites page's own route as the agent (plan, ownership and
validation are the app's own); `prepare` only reads. Anything that emails people says so on the
card, with how many, and the main button says it too ("Email 6 people").

Left manual on purpose: deleting a form, a response or an invite (and deleting a campaign).
"""
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import select

from app.models.database import FormSubmission, User
from app.services.ai.ai_actions import ActionError, Prepared
from app.services.ai.ai_actions_catalog import RouteError, _obj, _run_route, _s, _share, _simple, call_as_agent
from app.services.ai.ai_lookups_forms import (
    match_forms, my_forms, person_matches, person_words, resolve_invite, unfinished_for,
)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MAX_ROWS = 300          # people listed on the send card
SEND_CHUNK = 100        # the send route takes 100 addresses per call


def _people(n: int) -> str:
    return f"{n} {'person' if n == 1 else 'people'}"


def _day(iso_or_dt: Any) -> str:
    try:
        d = iso_or_dt if isinstance(iso_or_dt, datetime) else datetime.fromisoformat(str(iso_or_dt))
        return f"{d:%b} {d.day}"
    except (TypeError, ValueError):
        return ""


def _few(names: List[str], n: int = 5) -> str:
    names = [x for x in names if x]
    return ", ".join(names[:n]) + (f" +{len(names) - n} more" if len(names) > n else "")


def _invite_link(iid: Optional[str]) -> str:
    return f"/studio/forms?view=invites&invite={iid}" if iid else "/studio/forms?view=invites"


async def _invite(user: User, a: dict) -> dict:
    inv, err = await resolve_invite(user, a)
    if err:
        names = "; ".join(f"{i.get('title')} ({i.get('when') or 'no date'}) id={i.get('invite_id')}" for i in (err.get("invites") or [])[:8])
        raise ActionError(err.get("message", "Which invite?") + (f" Their invites: {names}" if names else ""))
    return inv


async def _form(user: User, a: dict) -> dict:
    forms, err = await my_forms(user)
    if err:
        raise ActionError(err["message"])
    fid = _s(a.get("form_id"))
    if fid:
        hit = next((f for f in forms if f.get("id") == fid), None)
        if hit:
            return hit
    hits = match_forms(forms, _s(a.get("form"))) if _s(a.get("form")) else []
    if len(hits) == 1:
        return hits[0]
    pool = hits or forms
    raise ActionError(("Which form? " if hits else "I couldn't tell which form. ")
                      + "; ".join(f"{f.get('title')} (form_id={f.get('id')})" for f in pool[:15]))


async def _send_from(db, user: User) -> str:
    """Where the emails come from - their own mailbox when connected (send-as-agent), else their name."""
    from app.models.database import MailConnection
    conn = (await db.execute(select(MailConnection).where(MailConnection.owner_id == user.id))).scalar_one_or_none()
    if conn is not None and conn.enabled and conn.status == "ok" and conn.account_email:
        return f"from your own {'Gmail' if conn.provider == 'google' else 'Outlook'} ({conn.account_email})"
    name = " ".join(x for x in (user.first_name, user.last_name) if x) or "you"
    return f"from you (as {name}) - replies come to you"


# ================================================================ 3. mark a form response reviewed

async def _p_reviewed(db, user, a):
    on = a.get("reviewed") is not False
    ids = [i for i in (a.get("submission_ids") or []) if isinstance(i, str) and _ID.match(i)][:50]
    if _s(a.get("submission_id")) and _ID.match(_s(a["submission_id"])):
        ids = [_s(a["submission_id"])] + ids
    forms, err = await my_forms(user)
    if err:
        raise ActionError(err["message"])
    titles = {f["id"]: f.get("title") for f in forms}
    stmt = select(FormSubmission).where(FormSubmission.owner_id == user.id, FormSubmission.form_id.in_(list(titles) or [""]))
    if ids:
        rows = (await db.execute(stmt.where(FormSubmission.id.in_(ids)))).scalars().all()
    else:
        words = person_words(_s(a.get("person")))
        form_ids = [f["id"] for f in match_forms(forms, a["form"])] if _s(a.get("form")) else ([_s(a["form_id"])] if _s(a.get("form_id")) else None)
        if not words and not form_ids and not a.get("all_unreviewed"):
            raise ActionError("Which response? Look it up with get_form_responses first (I need its submission_id), or name the person or form.")
        if form_ids is not None:
            stmt = stmt.where(FormSubmission.form_id.in_(form_ids or [""]))
        rows = (await db.execute(stmt.where(FormSubmission.is_reviewed.is_(not on)).order_by(FormSubmission.created_at.desc()).limit(200))).scalars().all()
        rows = [r for r in rows if person_matches(words, r.contact_name, r.contact_email)][:50]
    rows = [r for r in rows if bool(r.is_reviewed) != on]
    if not rows:
        raise ActionError("Those are already marked reviewed." if on else "Those aren't marked reviewed.")
    lines = [f"{r.contact_name or r.contact_email or 'Someone'} - {titles.get(r.form_id) or 'form'}{(' (' + _day(r.created_at) + ')') if r.created_at else ''}" for r in rows[:6]]
    if len(rows) > 6:
        lines.append(f"+{len(rows) - 6} more")
    them = "it" if len(rows) == 1 else "them"
    lines.append(f"Clears {them} from your Inbox and the red number" if on else f"Shows {them} as new in your Inbox again")
    lines.append("Doesn't email anyone")
    one = len(rows) == 1
    whose = f"{rows[0].contact_name}{chr(8217)}s response" if one and rows[0].contact_name else "this response" if one else f"{len(rows)} responses"
    title = f"Mark {whose} reviewed" if on else f"Mark {whose} as new again"
    return Prepared(title, lines, {"ids": [r.id for r in rows], "on": on, "form_id": rows[0].form_id if one else None},
                    confirm_label="Mark reviewed" if on else "Mark as new")


async def _r_reviewed(db, user, p):
    done = 0
    for sid in p["ids"]:
        await _run_route(user, "PATCH", f"/agent-hub/forms/submissions/{sid}/reviewed", {"is_reviewed": bool(p.get("on", True))})
        done += 1
    link = f"/studio/forms?formId={p['form_id']}&submission={p['ids'][0]}" if p.get("form_id") else "/studio/forms"
    return {"message": f"Marked {f'{done} responses' if done > 1 else 'it'} {'reviewed' if p.get('on', True) else 'as new'}.", "link": link}

_simple("mark_form_response_reviewed",
        "Mark form responses as reviewed (clears them from the Forms Inbox and the red number) - or back to new with reviewed=false. "
        "Best with submission_ids from get_form_responses; or name the person and/or form ('mark the Smiths' buyer form reviewed'), "
        "or all_unreviewed=true with a form for 'mark all my home value responses reviewed'. Doesn't email anyone.",
        _obj({"submission_id": {"type": "string"}, "submission_ids": {"type": "array", "items": {"type": "string"}},
              "person": {"type": "string"}, "form_id": {"type": "string"}, "form": {"type": "string"},
              "all_unreviewed": {"type": "boolean"}, "reviewed": {"type": "boolean"}}),
        _p_reviewed, _r_reviewed)


# ================================================================ 4. duplicate a form

async def _p_dup_form(db, user, a):
    f = await _form(user, a)
    premade = f.get("form_type") != "custom"
    lines = [f"Makes “{f.get('title')} - Copy” in your Custom Forms",
             "Same questions, wording and look - and you can change every question" + (" (a premade form's own questions can't be changed)" if premade else ""),
             "It gets its own new link; responses and tracking links stay with the original",
             "The original stays exactly as it is"]
    return Prepared(f"Copy the “{f.get('title')}” form", lines, {"form_id": f["id"], "title": f.get("title")}, confirm_label="Make a copy")


async def _r_dup_form(db, user, p):
    out = await _run_route(user, "POST", f"/agent-hub/forms/{p['form_id']}/duplicate")
    fid = (out or {}).get("id")
    share = None
    if (out or {}).get("slug"):
        from app.services.website.site_service import owner_links
        links = await owner_links(db, user)
        if links.hub_slug:
            share = await _share(db, user, f"/{links.hub_slug}/{out['slug']}", url=links.form(out["slug"]))
    return {"message": "Copy made - it's in Custom Forms. Open it to change the questions, title or look.",
            "link": f"/studio/forms?edit={fid}" if fid else "/studio/forms", "share": share}

_simple("duplicate_form",
        "Copy one of the agent's forms into a new, fully editable Custom Form (Duplicate on the form card). This is how to change a premade form's questions "
        "('copy my buyer form so I can change the questions', 'duplicate the home value form'). form_id from list_my_forms, or form = words from its name.",
        _obj({"form_id": {"type": "string"}, "form": {"type": "string"}}), _p_dup_form, _r_dup_form)


# ================================================================ 5. a tracking link (campaign) for a form

_AFTER = {"form_setting": None, "agent_hub": "agent_hub", "sales": "sales", "stay": "none", "own_link": "custom"}
_AFTER_LABEL = {"form_setting": "the form's own setting", "agent_hub": "your Agent Hub", "sales": "your Sales & Listings page",
                "stay": "the thank-you screen (they stay there)", "own_link": "your own link"}


async def _p_campaign(db, user, a):
    from app.api.forms_routes import CAMPAIGN_ELIGIBLE_FORM_TYPES
    f = await _form(user, a)
    if f.get("form_type") not in CAMPAIGN_ELIGIBLE_FORM_TYPES:
        raise ActionError("Tracking links work on the premade lead forms (I'm Buying, I'm Selling, What's My Home Worth?, Schedule a Showing, "
                          "How Can We Help, Make Me Sell) - not on custom, onboarding or Showcase forms.")
    name = _s(a.get("name"))[:80]
    if not name:
        raise ActionError("What should the tracking link be called? (Where it'll be used - e.g. “September Postcard” or “Facebook Ad”.)")
    after = _s(a.get("after_submit")) or "form_setting"
    if after not in _AFTER:
        after = "form_setting"
    body = {"name": name}
    if _AFTER[after]:
        body["redirect_mode"] = _AFTER[after]
    if after == "own_link":
        url = _s(a.get("own_link"))
        if not url:
            raise ActionError("Which web address should people go to after they submit?")
        body["redirect_url"] = url[:500]
    lines = [f"Form: {f.get('title')}", f"Name: {name}",
             "A new link (and QR code) for this form - visits and responses through it are counted separately in the form's Stats and Results",
             f"After submitting, people go to {_AFTER_LABEL[after]}" + (f": {body['redirect_url']}" if after == "own_link" else "")]
    return Prepared(f"Make a tracking link: {name}", lines, {"form_id": f["id"], "slug": f.get("slug"), "body": body}, confirm_label="Make the link")


async def _r_campaign(db, user, p):
    out = await _run_route(user, "POST", f"/agent-hub/forms/{p['form_id']}/campaigns", p["body"])
    token = (out or {}).get("token")
    share = None
    if token and p.get("slug"):
        from app.services.website.site_service import owner_links
        links = await owner_links(db, user)
        if links.hub_slug:
            share = await _share(db, user, f"/{links.hub_slug}/{p['slug']}?c={token}", url=f"{links.form(p['slug'])}?c={token}")
    msg = f"Tracking link “{p['body']['name']}” made."
    if share:
        msg += f" Its link: {share['url']} - its QR code is in the form's All Campaigns window."
    return {"message": msg, "link": "/studio/forms", "share": share}

_simple("create_form_tracking_link",
        "Make a tracking link (a Campaign) for one of the agent's premade lead forms, so visits and responses from one place are counted on their own "
        "('make a link for my home value form for the September postcard', 'a tracking link for my Facebook ad'). Returns the link. "
        "name = where it'll be used. after_submit only when they say where people should go after submitting (form_setting, agent_hub, sales, stay, own_link + own_link URL). "
        "Custom forms can't have tracking links.",
        _obj({"form_id": {"type": "string"}, "form": {"type": "string"}, "name": {"type": "string"},
              "after_submit": {"type": "string", "enum": list(_AFTER)}, "own_link": {"type": "string"}}),
        _p_campaign, _r_campaign)


# ================================================================ 6. add an unfinished (half-filled) form to Leads

async def _p_unfinished(db, user, a):
    forms, err = await my_forms(user)
    if err:
        raise ActionError(err["message"])
    fid = _s(a.get("form_id"))
    pool = [f for f in forms if f.get("id") == fid] if fid else (match_forms(forms, a["form"]) if _s(a.get("form")) else forms)
    items = await unfinished_for(user, db, pool)
    sid = _s(a.get("session_id"))
    if sid:
        items = [it for it in items if it.get("id") == sid]
    else:
        words = person_words(_s(a.get("person")))
        if not words:
            raise ActionError("Whose unfinished form? Look them up with get_unfinished_form_responses first.")
        items = [it for it in items if person_matches(words, it.get("contactName"), it.get("contactEmail"))]
    if not items:
        raise ActionError("I couldn't find that unfinished form - it may have been added, hidden or cleared already.")
    if len(items) > 1:
        raise ActionError("More than one matches: " + "; ".join(
            f"{it.get('contactName') or it.get('contactEmail')} on {it['_form']} (session_id={it['id']}, form_id={it['_form_id']})" for it in items[:6]))
    it = items[0]
    who = it.get("contactName") or it.get("contactEmail") or it.get("contactPhone") or "This person"
    if it.get("leadId"):
        raise ActionError(f"{who} is already in your Leads from that form.")
    if not (it.get("contactEmail") or it.get("contactPhone")):
        raise ActionError("They didn't leave an email or phone number, so there's no one to add yet.")
    lines = [" · ".join(x for x in (it.get("contactEmail"), it.get("contactPhone")) if x),
             f"{it['_form']}: answered {min(it.get('answeredCount') or 0, it.get('totalQuestions') or 0) or it.get('answeredCount')} of {it.get('totalQuestions')} questions"
             + (f", stopped at “{it['lastQuestionLabel']}”" if it.get("lastQuestionLabel") else ""),
             "Adds them to Leads with everything they answered, plus a follow-up (unless they said they have an agent or that follow-up is off)",
             "Doesn't email them"]
    warn = None
    if it.get("inProgress"):
        warn = f"{who} may still be filling it out right now."
    elif it.get("finishedLater"):
        warn = f"{who} looks like they came back and finished it later - they may already be in your Leads."
    return Prepared(f"Add {who} to your Leads", lines, {"form_id": it["_form_id"], "session_id": it["id"], "who": who},
                    warning=warn, confirm_label="Add to Leads")


async def _r_unfinished(db, user, p):
    out = await _run_route(user, "POST", f"/agent-hub/forms/{p['form_id']}/unfinished/{p['session_id']}/add-to-leads") or {}
    lid = out.get("leadId")
    if out.get("alreadyAdded"):
        msg = f"{p['who']} was already in your Leads."
    else:
        msg = f"{p['who']} is in your Leads now" + (" with a follow-up." if out.get("followup") else ".")
    return {"message": msg, "link": f"/leads/{lid}" if lid else "/leads"}

_simple("add_unfinished_form_response_to_leads",
        "Add someone who started a form but never pressed Submit (an unfinished / half-filled response) to Leads, with what they answered and a follow-up - "
        "the Add to Leads button under Results -> Unfinished ('add the guy who started my home value form to my leads'). "
        "Use session_id + form_id from get_unfinished_form_responses, or the person's name. Doesn't email them.",
        _obj({"session_id": {"type": "string"}, "form_id": {"type": "string"}, "form": {"type": "string"}, "person": {"type": "string"}}),
        _p_unfinished, _r_unfinished)


# ================================================================ 7. send an invite to chosen leads (picker "invite_send")

_SOURCE_TAG = {"open_house": "Open house guest", "showcase": "Showcase", "form": "Form", "website": "Website", "tour": "Tour request"}
_GROUPS = {"open_house_guests": "open_house", "showcase_clients": "showcase", "form_leads": "form", "website_leads": "website"}


def _short(addr: Optional[str]) -> str:
    return (addr or "").split(",")[0].strip()[:40]


async def _send_rows(db, user: User, inv_id: str, leads: List[dict], extra: List[str]) -> List[dict]:
    """Every lead (and any typed address) as a row on the card: who can get it, and why not."""
    from app.models.database import BlacklistedEmail, EventInviteRecipient, EventInviteRsvp
    emails = {(l.get("email") or "").strip().lower() for l in leads} | set(extra)
    emails.discard("")
    blocked = set((await db.execute(select(BlacklistedEmail.email).where(BlacklistedEmail.email.in_(list(emails) or [""])))).scalars().all())
    sent = {e: at for e, at in (await db.execute(select(EventInviteRecipient.email, EventInviteRecipient.last_sent_at)
                                                .where(EventInviteRecipient.invite_id == inv_id))).all()}
    answered = {(e or "").lower() for e in (await db.execute(select(EventInviteRsvp.email).where(EventInviteRsvp.invite_id == inv_id))).scalars().all() if e}
    rows, seen = [], set()

    def why(e: str) -> Optional[str]:
        if not e:
            return "No email"
        if not _EMAIL.match(e):
            return "Email looks wrong"
        if e in blocked:
            return "Unsubscribed"
        if e in answered:
            return "Already answered"
        if e in seen:
            return "Same email as someone above"
        return None

    for e in extra:
        w = why(e)
        rows.append({"id": f"e:{e}", "name": e.split("@")[0], "email": e, "tag": "Added by you", **({"why": w} if w else {}),
                     **({"invited": _day(sent[e]) or "before"} if e in sent else {})})
        seen.add(e)
    for l in leads:
        e = (l.get("email") or "").strip().lower()
        src = l.get("primary_source") or ""
        sub = None
        if src == "open_house" and l.get("open_houses"):
            sub = _short((l["open_houses"][0] or {}).get("address"))
        elif src == "form" and l.get("form_title"):
            sub = str(l["form_title"])[:40]
        w = why(e)
        row = {"id": l["id"], "name": (l.get("name") or e or l.get("phone") or "No name")[:80], "email": e or None,
               "tag": _SOURCE_TAG.get(src, "Lead"), **({"sub": sub} if sub else {}), **({"why": w} if w else {}),
               **({"invited": _day(sent[e]) or "before"} if e and e in sent else {}),
               "oh": [o.get("id") for o in (l.get("open_houses") or []) if isinstance(o, dict)][:5]}
        if l.get("represented") == "yes":
            row["agent"] = True
        rows.append(row)
        if e:
            seen.add(e)
    # people who can get it first (in the Leads page's order), then the greyed ones
    return ([r for r in rows if not r.get("why")] + [r for r in rows if r.get("why")])[:MAX_ROWS]


async def _p_send_invite(db, user, a):
    inv = await _invite(user, a)
    if inv.get("status") != "live":
        raise ActionError(f"RSVPs are closed on “{inv.get('title')}” - reopen them first (set_invite_rsvps open=true), then send it.")
    if inv.get("is_past"):
        raise ActionError(f"“{inv.get('title')}” has already happened.")
    try:
        leads = await call_as_agent(user, "GET", "/leads", params={"sort": "activity", "limit": 500}) or []
    except RouteError as e:
        raise ActionError(str(e))
    extra = []
    for e in (a.get("emails") or []):
        e = _s(e).lower()
        if e and e not in extra:
            extra.append(e[:200])
    rows = await _send_rows(db, user, inv["id"], [l for l in leads if isinstance(l, dict)], extra[:50])
    if not rows:
        raise ActionError("You don't have any leads yet - paste email addresses on the invite's Share step, or give me their emails.")
    ok = {r["id"] for r in rows if not r.get("why")}
    # who starts checked: the ones they named, or a group they named (never someone already invited unless named)
    named = {i for i in (a.get("lead_ids") or []) if isinstance(i, str)}
    group = _GROUPS.get(_s(a.get("group")))
    ohid = _s(a.get("open_house_id"))
    chosen = []
    for r in rows:
        if r["id"] not in ok:
            continue
        if r["id"] in named or r["id"].startswith("e:"):
            chosen.append(r["id"])
        elif r.get("invited"):
            continue
        elif ohid and ohid in (r.get("oh") or []):
            chosen.append(r["id"])
        elif group and r.get("tag") == _SOURCE_TAG[group]:
            chosen.append(r["id"])
        elif _s(a.get("group")) == "everyone":
            chosen.append(r["id"])
    for r in rows:
        r.pop("oh", None)
    n = len(chosen)
    greyed = len(rows) - len(ok)
    frm = await _send_from(db, user)
    lines = [f"Each person you check gets a short note {frm}, with the invite's link to RSVP",
             f"{_people(n)} checked now - check or uncheck people on the card" if n else "Check the people to send it to on the card",
             "Skips anyone with no email or who unsubscribed" + (f" ({greyed} greyed out)" if greyed else "")]
    if inv.get("audience") == "agents":
        lines.append("This invite is for other agents (a broker's open) - their RSVPs don't go to your Leads")
    payload = {"invite_id": inv["id"], "invite_title": inv.get("title"), "when": inv.get("when_label"), "rows": rows, "selected": chosen, "from": frm}
    return Prepared(f"Email “{inv.get('title')}” to your leads", lines, payload,
                    warning=f"This emails everyone you check, right away, {frm.split(' - ')[0]}.",
                    confirm_label=f"Email {_people(n)}" if n else "Email people", picker="invite_send")


def _adjust_send(user, payload: dict, picks: dict) -> dict:
    ids = picks.get("lead_ids")
    if not isinstance(ids, list):
        return payload
    want = {str(i) for i in ids}
    ok = [r["id"] for r in payload.get("rows") or [] if r["id"] in want and not r.get("why") and r.get("email")]
    if not ok:
        raise ActionError("Check at least one person on the card first.")
    return {**payload, "selected": ok}


async def _r_send_invite(db, user, p):
    want = set(p.get("selected") or [])
    recips = [{"email": r["email"], "name": r.get("name") if not r["id"].startswith("e:") else None}
              for r in p.get("rows") or [] if r["id"] in want and not r.get("why") and r.get("email")]
    if not recips:
        raise ActionError("Nobody was checked - nothing was sent.")
    sent, skipped = 0, []
    for i in range(0, len(recips), SEND_CHUNK):
        out = await _run_route(user, "POST", f"/invites/{p['invite_id']}/send", {"recipients": recips[i:i + SEND_CHUNK]}) or {}
        sent += int(out.get("sent") or 0)
        skipped += out.get("skipped") or []
    msg = f"Emailed “{p.get('invite_title')}” to {_people(sent)}."
    if skipped:
        msg += f" Skipped {len(skipped)}: " + ", ".join(f"{s.get('email')} ({s.get('reason')})" for s in skipped[:3]) + ("..." if len(skipped) > 3 else "")
    msg += " Their RSVPs show on the invite as they come in."
    return {"message": msg, "link": _invite_link(p["invite_id"])}

_simple("send_invite_to_leads",
        "Email one of the agent's invites to people they pick from their Leads (the card lists their leads with check boxes, a search box and Select all; "
        "people with no email or who unsubscribed are greyed out). Set it up as soon as they ask ('send my broker's open invite to my open house guests', "
        "'invite my Showcase clients to the holiday party', 'send the invite to Dana and Mike') - don't list people in chat first. "
        "Pick the invite by invite_id (list_my_invites) or words from its title. Pre-check people with lead_ids (from search_leads), "
        "group (open_house_guests, showcase_clients, form_leads, website_leads, everyone), open_house_id (that open house's guests), or emails for addresses not in Leads. "
        "Emails go out from the agent right away when they tap the button.",
        _obj({"invite_id": {"type": "string"}, "invite": {"type": "string"}, "lead_ids": {"type": "array", "items": {"type": "string"}},
              "group": {"type": "string", "enum": [*_GROUPS, "everyone"]}, "open_house_id": {"type": "string"},
              "emails": {"type": "array", "items": {"type": "string"}}}),
        _p_send_invite, _r_send_invite, adjust=_adjust_send)


# ================================================================ 8. remind people who haven't answered

async def _p_remind(db, user, a):
    from app.models.database import BlacklistedEmail, EventInvite, EventInviteRecipient
    from app.services import invites_service as svc
    inv = await _invite(user, a)
    if inv.get("status") != "live":
        raise ActionError(f"RSVPs are closed on “{inv.get('title')}” - reopen them first to send a reminder.")
    if inv.get("is_past"):
        raise ActionError(f"“{inv.get('title')}” has already happened.")
    row = await db.get(EventInvite, inv["id"])
    if row is None or row.owner_id != user.id:
        raise ActionError("I couldn't find that invite.")
    recs = {r.email: r for r in (await db.execute(select(EventInviteRecipient).where(EventInviteRecipient.invite_id == row.id))).scalars().all()}
    if not recs:
        raise ActionError(f"“{inv.get('title')}” hasn't been emailed to anyone from the invite yet, so there's nobody to remind - "
                          "reminders only reach people it was emailed to. I can send it to people first.")
    waiting = await svc.non_responders(db, row)
    if not waiting:
        raise ActionError(f"Nobody to remind - everyone you emailed “{inv.get('title')}” has answered.")
    blocked = set((await db.execute(select(BlacklistedEmail.email).where(BlacklistedEmail.email.in_([e for e, _ in waiting])))).scalars().all())
    now = datetime.now(timezone.utc)
    recent = [e for e, _ in waiting if recs.get(e) and recs[e].last_sent_at and now - recs[e].last_sent_at < svc.RESEND_GAP]
    gets = [(e, n) for e, n in waiting if e not in recent and e not in blocked]
    if not gets:
        raise ActionError(f"The {_people(len(waiting))} who haven't answered were all emailed in the last 12 hours"
                          + (" or unsubscribed" if blocked else "") + " - a reminder can go once every 12 hours.")
    frm = await _send_from(db, user)
    lines = [f"{_people(len(gets))} who haven't answered: {_few([n or e for e, n in gets])}",
             f"A friendly reminder {frm}, with the invite's link"]
    if recent or blocked:
        lines.append(f"Skips {_people(len(waiting) - len(gets))} (emailed in the last 12 hours or unsubscribed)")
    lines.append("Only people emailed from the invite - not anyone you texted or shared the link with")
    return Prepared(f"Remind {_people(len(gets))} about “{inv.get('title')}”", lines, {"invite_id": inv["id"], "invite_title": inv.get("title"), "count": len(gets)},
                    warning=f"This emails {_people(len(gets))} a reminder right away.", confirm_label=f"Remind {_people(len(gets))}")


async def _r_remind(db, user, p):
    out = await _run_route(user, "POST", f"/invites/{p['invite_id']}/resend") or {}
    sent = int(out.get("sent") or 0)
    msg = f"Reminder sent to {_people(sent)}." if sent else "Nobody needed a reminder - nothing was sent."
    if out.get("skipped"):
        msg += f" Skipped {len(out['skipped'])} (emailed in the last 12 hours or unsubscribed)."
    return {"message": msg, "link": _invite_link(p["invite_id"])}

_simple("remind_invite_non_responders",
        "Email a reminder about an invite to everyone it was emailed to who hasn't answered yet (the invite's Remind button) - "
        "'remind people who haven't RSVP'd to the party', 'nudge the no-answers for Saturday's broker's open'. "
        "Only reaches people emailed from the invite, at most once every 12 hours. invite_id from list_my_invites, or words from its title.",
        _obj({"invite_id": {"type": "string"}, "invite": {"type": "string"}}), _p_remind, _r_remind)


# ================================================================ 9. close / reopen RSVPs, duplicate an invite

async def _p_rsvps(db, user, a):
    inv = await _invite(user, a)
    want_open = a.get("open") is True
    if (inv.get("status") == "live") == want_open:
        raise ActionError(f"RSVPs on “{inv.get('title')}” are already {'open' if want_open else 'closed'}.")
    lines = (["The invite takes RSVPs again, and you can send it and send reminders"] if want_open else
             ["The invite page stops taking RSVPs - it says RSVPs are closed", "Everyone who already answered stays on your list",
              "You can't send it or remind people while it's closed - reopen it any time"])
    lines.append("Doesn't email anyone")
    return Prepared(f"{'Reopen' if want_open else 'Close'} RSVPs on “{inv.get('title')}”", lines,
                    {"invite_id": inv["id"], "status": "live" if want_open else "closed"}, confirm_label="Reopen RSVPs" if want_open else "Close RSVPs")


async def _r_rsvps(db, user, p):
    await _run_route(user, "POST", f"/invites/{p['invite_id']}/status", {"status": p["status"]})
    return {"message": "RSVPs are open again." if p["status"] == "live" else "RSVPs are closed.", "link": _invite_link(p["invite_id"])}

_simple("set_invite_rsvps",
        "Close RSVPs on one of the agent's invites (the page stops taking answers - 'close RSVPs for the party', 'stop taking RSVPs'), "
        "or reopen them with open=true. Doesn't email anyone. invite_id from list_my_invites, or words from its title.",
        _obj({"invite_id": {"type": "string"}, "invite": {"type": "string"}, "open": {"type": "boolean"}}), _p_rsvps, _r_rsvps)


async def _p_dup_invite(db, user, a):
    inv = await _invite(user, a)
    lines = [f"Makes “{inv.get('title')} (copy)” with the same design, details, questions and days",
             "It gets its own new link; no guests or RSVPs come along - change the date in it before you share it",
             "Nothing is sent"]
    return Prepared(f"Copy the “{inv.get('title')}” invite", lines, {"invite_id": inv["id"]}, confirm_label="Make a copy")


async def _r_dup_invite(db, user, p):
    out = await _run_route(user, "POST", f"/invites/{p['invite_id']}/duplicate") or {}
    code = out.get("code")
    share = await _share(db, user, f"/i/{code}", url=out.get("share_url")) if code else None
    return {"message": "Copy made - open it to change the date, then share or send it.", "link": _invite_link(out.get("id")), "share": share}

_simple("duplicate_invite",
        "Copy one of the agent's invites into a new one with its own link (same design, details and questions; no guests) - for a repeat event "
        "('copy last month's broker's open invite', 'duplicate the party invite'). Nothing is sent. invite_id from list_my_invites, or words from its title.",
        _obj({"invite_id": {"type": "string"}, "invite": {"type": "string"}}), _p_dup_invite, _r_dup_invite)
