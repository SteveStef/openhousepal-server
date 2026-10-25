"""Shared public-form submission logic - the actual question validation, FormSubmission
persistence, and Lead upsert, extracted verbatim from forms_routes.submit_public_form so a
second public entry point (the website's Form block, see public_site_routes.py) can submit into
the exact same pipeline instead of re-implementing it. Nothing about this function is
Agent-Hub-specific - it only needs the AgentForm row and the submitted payload."""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import case, func, literal_column, null, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentForm, FormCampaign, FormViewSession, Notification
from app.schemas.forms import FormProgressUpdate, FormSubmissionCreate, FormSubmissionResponse
from app.models.database import FormSubmission
from app.services.leads_service import upsert_lead

logger = logging.getLogger(__name__)


CAMPAIGN_REDIRECT_MODES = ("agent_hub", "sales", "none", "custom")


def normalize_campaign_redirect(mode: Optional[str], url: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Validates a campaign's after-submit choice. Returns (mode, url) to store - (None, None)
    means "same as the form". A custom link gets https:// added when typed without it and must be
    a real web address (no javascript:, mailto:, bare words). Raises HTTPException 422 otherwise."""
    from urllib.parse import urlparse
    mode = (mode or "").strip().lower() or None
    if mode in (None, "default"):
        return None, None
    if mode not in CAMPAIGN_REDIRECT_MODES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Pick where people go after they submit.")
    if mode != "custom":
        return mode, None
    url = (url or "").strip()
    if url and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or "." not in host or len(url) > 500 or any(c.isspace() for c in url):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Enter a full web address, like https://yourwebsite.com/page.")
    return mode, url


def campaign_after_submit(campaign: Optional[FormCampaign], hub_live: bool) -> tuple[Optional[bool], Optional[str], Optional[str]]:
    """What a campaign link does after submitting: (redirect_enabled, after_submit_mode, after_submit_url).
    (None, None, None) = no override, the form's own setting applies. Agent Hub / Sales only redirect
    when the hub page actually loads (otherwise the form's setting applies, which has the same rule)."""
    mode = campaign.redirect_mode if campaign else None
    if not mode:
        return None, None, None
    if mode == "none":
        return False, "none", None
    if mode == "custom":
        return (True, "custom", campaign.redirect_url) if campaign.redirect_url else (None, None, None)
    if mode in ("agent_hub", "sales"):
        return (True, mode, None) if hub_live else (None, None, None)
    return None, None, None


async def active_campaign_for_token(db: AsyncSession, form_id: str, token: Optional[str]) -> Optional[FormCampaign]:
    """The live (non-deleted) campaign a ?c= token belongs to on this form, or None."""
    if not token:
        return None
    result = await db.execute(
        select(FormCampaign).where(FormCampaign.form_id == form_id, FormCampaign.token == token, FormCampaign.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def resolve_campaign_id(db: AsyncSession, form_id: str, token: Optional[str]) -> Optional[str]:
    """Resolves a campaign token to its FormCampaign.id, scoped to this form, among active
    (non-deleted) campaigns only - a deleted campaign's token no longer attributes new traffic,
    falling back to General (None) instead of reviving it. Never raises; a blank, missing,
    unrecognized, or deleted token all just mean General. Shared by both public entry points
    (Agent Hub and the website) and by both progress-tracking and submission."""
    if not token:
        return None
    result = await db.execute(
        select(FormCampaign.id).where(
            FormCampaign.form_id == form_id,
            FormCampaign.token == token,
            FormCampaign.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def resolve_submission_campaign_id(
    db: AsyncSession, form_id: str, session_key: Optional[str], campaign_token: Optional[str],
) -> Optional[str]:
    """Session attribution is authoritative: a visitor's FormViewSession (created on first view,
    campaign_id fixed then and never changed again) is the single source of truth for which
    campaign a later submission belongs to - a stale, changed, or missing token arriving fresh at
    submit time must never move the submission into a different campaign than the one its session
    was already attributed to. Only resolves campaign_token directly as a fallback when no session
    row exists at all for this visitor (e.g. a submit with no prior progress ping)."""
    if session_key:
        result = await db.execute(
            select(FormViewSession.campaign_id).where(
                FormViewSession.form_id == form_id,
                FormViewSession.session_key == session_key,
            )
        )
        session_row = result.first()
        if session_row is not None:
            return session_row[0]
    return await resolve_campaign_id(db, form_id, campaign_token)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


async def assert_form_is_live(db: AsyncSession, form: AgentForm) -> None:
    """Public forms (and their QR codes) only work while the owning agent's subscription is live,
    and the 'Setup My Own Showcase' form only while they're on a Showcase plan - otherwise a guest
    would fill it out expecting a Showcase that never gets built. Shared by every public entry
    point (Agent Hub URL + IDX-domain URL) through submit_form and the public GETs."""
    from app.models.database import User
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    owner = (await db.execute(select(User).where(User.id == form.owner_id))).scalar_one_or_none()
    tiers = SHOWCASE_ACCESS_TIERS if form.form_type == "showcase_request" else None
    if not owner_has_access(owner, tiers):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form isn't accepting responses right now.")
    # An older I'm Buying / How Can We Help form picks up the Showcase home-search questions the
    # first time anyone opens it (agents who never visit their Forms page included).
    from app.api.forms_routes import add_showcase_criteria
    upgraded = add_showcase_criteria(form.form_type, form.questions or [])
    if upgraded is not None:
        form.questions = upgraded
        await db.commit()
        await db.refresh(form)


async def onboarding_criteria(db: AsyncSession, form: AgentForm, answers: dict) -> Optional[dict]:
    """Buyer Onboarding answers -> the Showcase search keys I'm Buying uses (cities, budget_min/max,
    min_bedrooms). Towns come out of the written "neighborhoods / municipalities" answer, matched
    whole-word against real towns in the states the agent serves. None when no town is found."""
    import re as _re
    from sqlalchemy import select as _select
    from app.models.database import AgentMarketArea, City, MarketArea
    text_ = " ".join(str(answers.get(k) or "") for k in ("location_preferences", "lifestyle_needs"))
    if not text_.strip():
        return None
    states = {st.strip().upper() for (st,) in (await db.execute(
        _select(MarketArea.state).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
        .where(AgentMarketArea.owner_id == form.owner_id)
    )).all() if st} or {"PA"}
    hay = " " + _re.sub(r"[^a-z0-9 ]+", " ", text_.lower()) + " "
    found: list[str] = []
    for name, state in (await db.execute(_select(City.name, City.state).where(City.state.in_(states)))).all():
        n = _re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).strip()
        if len(n) >= 4 and f" {n} " in hay:
            label = f"{name}, {state}"
            if label not in found:
                found.append(label)
    if not found:
        return None
    out = {"cities": found[:8], "budget_min": answers.get("budget_min"), "budget_max": answers.get("budget_max"),
           "min_bedrooms": answers.get("desired_bedrooms")}
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _showcase_gate(form: AgentForm, answers_by_id: dict) -> Optional[str]:
    """For the forms that build a buyer a Showcase: 'no' (not working with an agent - build it),
    'yes' (has an agent - don't), or None (not a buyer / didn't say). Mirrors the question
    gating in forms_routes.add_showcase_criteria."""
    def ans(qid):
        return str(answers_by_id.get(qid) or "").strip().lower()
    if form.form_type == "buying":
        a = ans("sc_has_agent")
    elif form.form_type == "how_can_i_help":
        if "buy" not in ans("intent"):
            return None
        a = ans("buy_working_with_agent") or ans("sc_has_agent")
    else:
        return None
    if not a:
        return None
    return "yes" if a.startswith("yes") else "no"


PATH_CAP = 300               # path entries kept per visit
ANSWER_TEXT_CAP = 2000       # characters kept per text answer
ANSWER_LIST_CAP = 50         # items kept per multi-select answer
UNFINISHED_RETENTION_DAYS = 90  # unfinished answers + contact details are cleared after this


def _question_ids(form: AgentForm) -> set:
    ids = set()
    for q in form.questions or []:
        if q.get("type") == "contact_group":
            ids.update(f.get("id") for f in q.get("fields") or [])
        elif q.get("type") != "statement":
            ids.add(q.get("id"))
    ids.discard(None)
    return ids


def _clean_answers(form: AgentForm, raw: Optional[dict]) -> dict:
    """Only this form's own questions, blanks dropped, sizes capped - it's public input."""
    if not isinstance(raw, dict):
        return {}
    allowed = _question_ids(form)
    out = {}
    for key, value in raw.items():
        if key not in allowed or value is None:
            continue
        if isinstance(value, list):
            items = [str(v)[:200] for v in value if str(v).strip()][:ANSWER_LIST_CAP]
            if items:
                out[key] = items
        elif isinstance(value, (int, float, bool)):
            out[key] = value
        else:
            text = str(value).strip()[:ANSWER_TEXT_CAP]
            if text:
                out[key] = text
    return out


def _contact_from_answers(answers: dict) -> dict:
    """Same field conventions as submit_form's contact extraction."""
    def first(*ids):
        for i in ids:
            v = answers.get(i)
            if v not in (None, "") and not isinstance(v, list):
                return str(v).strip()[:200]
        return None
    name = None
    for f, l in (("buyer_first_name", "buyer_last_name"), ("buyer1_first_name", "buyer1_last_name")):
        combined = f"{answers.get(f) or ''} {answers.get(l) or ''}".strip()
        if combined:
            name = combined[:200]
            break
    # Only count contact details once they look real - autosave catches half-typed ones ("quin").
    email = first("email", "seller_email", "seller1_email", "buyer_email", "buyer1_email")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
        email = None
    phone = first("phone", "seller_phone", "seller1_phone", "buyer_phone", "buyer1_phone")
    if phone and len(re.sub(r"\D", "", phone)) < 7:
        phone = None
    return {"name": name or first("full_name", "seller_name", "seller1_name"), "email": email, "phone": phone}


_CONTACT_IDS = {"full_name", "seller_name", "seller1_name", "buyer_first_name", "buyer_last_name", "buyer1_first_name",
                "buyer1_last_name", "email", "seller_email", "seller1_email", "buyer_email", "buyer1_email",
                "phone", "seller_phone", "seller1_phone", "buyer_phone", "buyer1_phone"}
PARTIAL_SOURCE = "form_partial"  # LeadEvent.source_type for a lead added from an unfinished form
UNFINISHED_FOLLOWUP_SOURCE = "form_unfinished"  # AgentTask.auto_source for its follow-up
# The suggested message is the form's own (the agent's version or the default) - written as if
# they'd finished, because naming what they were doing ("thanks for filling out my I'm Buying
# form") is the stronger opener. Only the Showcase pitch is left out: none is built for them.


def _show_value(v) -> str:
    if isinstance(v, list):
        return ", ".join(_show_value(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_show_value(x)}" for k, x in v.items() if x not in (None, ""))
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v).strip()


def partial_snapshot(form: AgentForm, answers: dict) -> list[dict]:
    """What they answered, in the form's order, shaped like a real submission's snapshot."""
    out = []
    for q in form.questions or []:
        if q.get("type") == "statement":
            continue
        fields = (q.get("fields") or []) if q.get("type") == "contact_group" else [q]
        for f in fields:
            v = answers.get(f.get("id"))
            if v in (None, "", []):
                continue
            out.append({"questionId": f["id"], "label": f.get("label") or f["id"], "type": f.get("type"),
                        "value": v, "sectionTitle": q.get("section_title") or (q.get("label") if f is not q else None)})
    return out


async def add_unfinished_to_leads(db: AsyncSession, form: AgentForm, session: FormViewSession) -> dict:
    """The agent pressed "Add to Leads" on an unfinished response. Treated like a finished one:
    the person becomes a lead (or the existing lead gets this touch point) with every answer
    they gave, and a follow-up lands on the agent's list + calendar under the form's own
    situation - listing what was captured and where they stopped. Never builds a Showcase
    (they didn't finish telling us what they want), so there's no pitch either."""
    from app.models.database import LeadEvent, User
    from app.services import tasks_service
    from app.services.form_insights_service import question_order, session_path

    existing = (await db.execute(select(LeadEvent).where(
        LeadEvent.source_type == PARTIAL_SOURCE, LeadEvent.source_id == session.id))).scalars().first()
    if existing is not None:
        return {"leadId": existing.lead_id, "alreadyAdded": True, "followup": None}

    answers = session.partial_answers if isinstance(session.partial_answers, dict) else {}
    contact = _contact_from_answers(answers)
    name = contact["name"] or session.contact_name
    email = contact["email"] or session.contact_email
    phone = contact["phone"] or session.contact_phone
    if not (email or phone):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="They didn't leave an email or phone number, so there's no one to add yet.")

    snapshot = partial_snapshot(form, answers)
    qs = question_order(form.questions)
    number = {q["id"]: i for i, q in enumerate(qs, start=1)}
    labels = {q["id"]: q.get("label") for q in qs}
    path = session_path(session, form.questions)
    last_q = path[-1][0] if path else session.furthest_question_id
    stopped = (f"stopped at question {number[last_q]} of {len(qs)}" + (f' ("{labels[last_q]}")' if labels.get(last_q) else "")
               if last_q in number else "didn't finish")
    gate = _showcase_gate(form, answers)
    represented = gate or ("yes" if str(answers.get("sell_working_with_agent") or "").strip().lower().startswith("yes") else None)
    when = session.last_activity_at or session.updated_at or session.created_at or datetime.now(timezone.utc)

    lead = await upsert_lead(
        db,
        owner_id=form.owner_id,
        name=name,
        email=email,
        phone=phone,
        source_type=PARTIAL_SOURCE,
        source_id=session.id,
        summary=f"Started your {form.title} form (didn't finish)",
        occurred_at=when,
        property_address=answers.get("property_address") if isinstance(answers.get("property_address"), str) else None,
        form_id=form.id,
        raw_snapshot={"answers": snapshot, "form_type": form.form_type, "unfinished": True, "stopped": stopped},
        represented=represented,
    )
    if lead is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Couldn't add this person to your Leads.")
    event = (await db.execute(select(LeadEvent).where(
        LeadEvent.source_type == PARTIAL_SOURCE, LeadEvent.source_id == session.id))).scalars().first()

    # The follow-up: same situation (switch, day, alert style) and same title as a finished one.
    rule = tasks_service.FORM_TYPE_RULES.get(form.form_type, "form_submission")
    owner = await db.get(User, form.owner_id)
    reason = None
    if not tasks_service.rule_enabled(owner, rule):
        reason = "rule_off"
    elif lead.represented == "yes" or tasks_service.says_has_agent({"answers": snapshot}):
        reason = "has_agent"
    elif await tasks_service._open_auto_task_for_lead(db, form.owner_id, lead.id):
        reason = "already_open"
    task = None
    if reason is None:
        # Name / email / phone are already on the Contact line (and the contact card).
        rest = [a for a in snapshot if a["questionId"] not in _CONTACT_IDS]
        captured = "\n".join(f"- {a['label']}{' ' if str(a['label']).rstrip().endswith('?') else ': '}{_show_value(a['value'])[:200]}"
                             for a in rest[:15])
        more = f"\n- ...and {len(rest) - 15} more (see Leads)" if len(rest) > 15 else ""
        contact_line = "  |  ".join(b for b in [lead.email, lead.phone] if b)
        task = await tasks_service.create_auto_followup(
            db,
            owner_id=form.owner_id,
            rule=rule,
            source=UNFINISHED_FOLLOWUP_SOURCE,
            source_id=event.id if event else session.id,
            title=f"Follow up with {lead.name or lead.email or lead.phone}",
            context=f"Started your {form.title} form but didn't finish - {stopped}",
            notes=(f"Auto-created: added from an unfinished {form.title} form"
                   + (f"\nContact: {contact_line}" if contact_line else "")
                   + ("\nWhat they answered before stopping:\n" + captured + more if captured else "")
                   + "\nThey didn't press Submit - the suggested message still reads as if they did."),
            message=tasks_service._without_showcase(tasks_service.render_rule_message(
                owner, rule, name=lead.name, form_name=form.title,
                address=answers.get("property_address") if isinstance(answers.get("property_address"), str) else None)),
            occurred_at=datetime.now(timezone.utc),
            lead_id=lead.id,
        )
        if task is None:
            reason = "not_created"
    return {"leadId": lead.id, "alreadyAdded": False,
            "followup": {"created": task is not None, "reason": reason, "rule": rule}}


async def purge_old_unfinished(db: AsyncSession) -> int:
    """Nightly: unfinished responses keep what the visitor typed for UNFINISHED_RETENTION_DAYS,
    then the answers and contact details are cleared (the visit itself still counts in stats)."""
    from datetime import timedelta
    from sqlalchemy import update
    cutoff = datetime.now(timezone.utc) - timedelta(days=UNFINISHED_RETENTION_DAYS)
    res = await db.execute(update(FormViewSession).where(
        FormViewSession.completed_at.is_(None),
        FormViewSession.partial_answers.is_not(None),
        func.coalesce(FormViewSession.last_activity_at, FormViewSession.created_at) < cutoff,
    ).values(partial_answers=null(), contact_name=None, contact_email=None, contact_phone=None))
    await db.commit()
    return res.rowcount or 0


async def record_form_progress(db: AsyncSession, form: AgentForm, payload: FormProgressUpdate) -> None:
    """Best-effort per-question progress ping (view / step-advance / completion) that powers the
    Stats drop-off funnel, shared by both public entry points (the Agent Hub URL and the website's
    Form block) so a visitor's funnel data is canonical no matter which door they came through.
    Callers wrap this in their own try/except - never raises on ordinary bad input, but a real DB
    outage still propagates so the caller's rollback runs.

    A single atomic INSERT .. ON CONFLICT DO UPDATE, not a SELECT-then-branch: two progress pings
    for the same (form, session_key) arriving at the same instant (e.g. a page that mounts twice)
    both used to see no existing row, both insert, and the second violated
    uq_form_view_sessions_form_session. Catching that after the fact and merging by hand is still
    a race - two concurrent "losers" reading-then-writing the same row can lose an update, letting
    a lower step_index clobber a higher one moments later, which is worse than the crash it was
    meant to fix. Postgres resolves the conflict itself, in one statement, so there's no window
    for either problem. GREATEST keeps the furthest step reached even if this ping is behind
    where a concurrent one already got to; completed_at is sticky (COALESCE) once set.
    """
    # Resolved on every call, but only USED by ON CONFLICT when this is the row's first insert -
    # the DO UPDATE clause below deliberately never touches campaign_id, so a later ping can never
    # move an existing session to a different campaign (see resolve_campaign_id's own note).
    campaign_id = await resolve_campaign_id(db, form.id, payload.campaign_token)
    now = datetime.now(timezone.utc)
    completed_at = now if payload.completed else None
    answers = _clean_answers(form, payload.answers)
    contact = _contact_from_answers(answers)
    # One path entry per question landed on (not for the intro "view" or the completion ping); a
    # repeat of the last entry (answer autosaves while they type) is skipped in SQL below.
    step_entry = ([{"q": payload.question_id, "s": payload.step_index, "t": now.isoformat()}]
                  if payload.question_id and payload.step_index >= 1 and not payload.completed else [])
    stmt = pg_insert(FormViewSession).values(
        form_id=form.id,
        owner_id=form.owner_id,
        session_key=payload.session_key,
        furthest_step_index=payload.step_index,
        furthest_question_id=payload.question_id,
        campaign_id=campaign_id,
        completed_at=completed_at,
        # null(), not None: a JSONB None is stored as JSON null, which `||` would turn into a list
        partial_answers=answers or null(),
        path=step_entry,
        started_at=now if payload.step_index >= 1 else None,
        last_activity_at=now,
        source=payload.source if payload.source in ("qr", "link", "website") else None,
        device=payload.device if payload.device in ("mobile", "tablet", "desktop") else None,
        referrer=(payload.referrer or "").strip().lower()[:120] or None,
        contact_name=contact["name"],
        contact_email=contact["email"],
        contact_phone=contact["phone"],
    )
    t = FormViewSession.__table__.name
    stmt = stmt.on_conflict_do_update(
        constraint="uq_form_view_sessions_form_session",
        set_={
            "furthest_step_index": func.greatest(FormViewSession.furthest_step_index, stmt.excluded.furthest_step_index),
            "furthest_question_id": case(
                (stmt.excluded.furthest_step_index > FormViewSession.furthest_step_index, stmt.excluded.furthest_question_id),
                else_=FormViewSession.furthest_question_id,
            ),
            "completed_at": func.coalesce(FormViewSession.completed_at, stmt.excluded.completed_at),
            "updated_at": func.now(),
            # Merge, newest answer wins per question - an answer they change stays current.
            "partial_answers": literal_column(
                f"coalesce(nullif({t}.partial_answers, 'null'::jsonb), '{{}}'::jsonb)"
                f" || coalesce(nullif(excluded.partial_answers, 'null'::jsonb), '{{}}'::jsonb)"),
            "path": literal_column(
                f"CASE WHEN jsonb_array_length(coalesce(excluded.path, '[]'::jsonb)) = 0 THEN {t}.path"
                f" WHEN coalesce({t}.path -> -1 ->> 'q', '') = excluded.path -> 0 ->> 'q' THEN {t}.path"
                f" WHEN jsonb_array_length(coalesce({t}.path, '[]'::jsonb)) >= {PATH_CAP} THEN {t}.path"
                f" ELSE coalesce({t}.path, '[]'::jsonb) || excluded.path END"),
            "started_at": func.coalesce(FormViewSession.started_at, stmt.excluded.started_at),
            "last_activity_at": stmt.excluded.last_activity_at,
            "source": func.coalesce(FormViewSession.source, stmt.excluded.source),
            "device": func.coalesce(FormViewSession.device, stmt.excluded.device),
            "referrer": func.coalesce(FormViewSession.referrer, stmt.excluded.referrer),
            "contact_name": func.coalesce(stmt.excluded.contact_name, FormViewSession.contact_name),
            "contact_email": func.coalesce(stmt.excluded.contact_email, FormViewSession.contact_email),
            "contact_phone": func.coalesce(stmt.excluded.contact_phone, FormViewSession.contact_phone),
        },
    )
    await db.execute(stmt)
    await db.commit()


async def submit_form(db: AsyncSession, form: AgentForm, payload: FormSubmissionCreate) -> FormSubmissionResponse:
    await assert_form_is_live(db, form)
    answers_by_id = {answer.question_id: answer.value for answer in payload.answers}

    # Setup My Own Showcase's first question (has_agent) already skips every other question in the
    # wizard once answered "Yes" (see their depends_on in DEFAULT_FORMS), so in practice only
    # has_agent itself gets submitted - nothing worth recording as a Lead. This is also the
    # authoritative server-side gate: a Showcase must never get built for someone already
    # represented, regardless of what the frontend did or didn't skip.
    if form.form_type == "showcase_request" and str(answers_by_id.get("has_agent") or "").strip().lower().startswith("yes"):
        return FormSubmissionResponse(
            success=True,
            message="Since you're already working with an agent, we're not able to build you a Showcase - reach out to them directly for anything you need. Thanks for stopping by!",
            submission_id=None,
            redirect_url=None,
        )

    def is_visible(question: dict) -> bool:
        depends_on = question.get("depends_on")
        if not depends_on:
            return True
        dep_value = answers_by_id.get(depends_on["question_id"])
        if dep_value is None:
            return False
        values = depends_on.get("values", [])
        if isinstance(dep_value, list):
            return any(item in values for item in dep_value)
        return dep_value in values

    def is_blank(value) -> bool:
        if value is None:
            return True
        if isinstance(value, list):
            return len(value) == 0
        return str(value).strip() == ""

    def check_email(item: dict, value) -> None:
        # The lead's email, the follow-up and the agent's reply all hang off this - a typo'd or
        # made-up address used to be saved as-is (the form page checks too; this is the gate).
        if item.get("type") == "email" and not is_blank(value) and not _EMAIL_RE.match(str(value).strip()):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{item['label']}: enter a valid email address.")

    snapshot = []
    for question in form.questions:
        if not is_visible(question):
            continue
        if question.get("type") == "statement":
            continue
        if question.get("type") == "contact_group":
            group_title = question.get("section_title") or question.get("label")
            for field in question.get("fields", []):
                value = answers_by_id.get(field["id"])
                if field.get("required") and is_blank(value):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field['label']} is required.")
                check_email(field, value)
                snapshot.append({
                    "questionId": field["id"],
                    "label": field["label"],
                    "type": field["type"],
                    "value": value,
                    "sectionTitle": group_title,
                })
            continue
        value = answers_by_id.get(question["id"])
        if question.get("required") and is_blank(value):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{question['label']} is required.")
        check_email(question, value)
        snapshot.append({
            "questionId": question["id"],
            "label": question["label"],
            "type": question["type"],
            "value": value,
            "sectionTitle": question.get("section_title"),
        })

    def contact_value(*question_ids: str):
        for question_id in question_ids:
            value = answers_by_id.get(question_id)
            if value not in (None, "") and not isinstance(value, list):
                return str(value).strip()
        return None

    showing = None
    if form.form_type == "showing":
        # The showing flow (pick the home, then the Showcase-style tour window) can send a third
        # time and a note even on forms saved before those existed - keep them in the record.
        in_snapshot = {entry["questionId"] for entry in snapshot}
        times_title = next((q.get("section_title") or q.get("label") for q in form.questions if q.get("id") == "preferred_times"), None)
        for extra_id, label, kind, section in (
            ("preferred_date_3", "Third choice - date", "date", times_title),
            ("preferred_time_3", "Third choice - time", "time", times_title),
            ("showing_note", "Note", "long_text", None),
        ):
            value = answers_by_id.get(extra_id)
            if extra_id not in in_snapshot and not is_blank(value):
                snapshot.append({"questionId": extra_id, "label": label, "type": kind, "value": str(value).strip(), "sectionTitle": section})
        preferred = []
        for n in (1, 2, 3):
            d, t = contact_value(f"preferred_date_{n}"), contact_value(f"preferred_time_{n}")
            if d:
                preferred.append({"date": d, "time": t})
        showing = {
            "listing_key": contact_value("property_listing_key"),
            "preferred": preferred,
            "note": contact_value("showing_note"),
            "confirmed_date": None,
            "confirmed_time": None,
        }

    if form.form_type == "showcase_request":
        # The IDX "Save this search" / heart window sends the visitor's search as a few extra
        # answers - keep a readable copy on the submission so the agent sees what they searched.
        in_snapshot = {entry["questionId"] for entry in snapshot}
        for extra_id, label in (("saved_search", "Saved from the website search"), ("property_of_interest", "Home they liked")):
            value = answers_by_id.get(extra_id)
            if extra_id not in in_snapshot and not is_blank(value):
                snapshot.append({"questionId": extra_id, "label": label, "type": "long_text", "value": str(value).strip()[:600], "sectionTitle": None})

    def contact_name_value():
        for first_id, last_id in (("buyer_first_name", "buyer_last_name"), ("buyer1_first_name", "buyer1_last_name")):
            first = answers_by_id.get(first_id)
            last = answers_by_id.get(last_id)
            combined = f"{first or ''} {last or ''}".strip()
            if combined:
                return combined
        return contact_value("full_name", "seller_name", "seller1_name")

    campaign_id = await resolve_submission_campaign_id(db, form.id, payload.session_key, payload.campaign_token)

    submission = FormSubmission(
        form_id=form.id,
        owner_id=form.owner_id,
        form_type=form.form_type,
        answers=snapshot,
        contact_name=contact_name_value(),
        contact_email=contact_value("email", "seller_email", "seller1_email", "buyer_email", "buyer1_email"),
        contact_phone=contact_value("phone", "seller_phone", "seller1_phone", "buyer_phone", "buyer1_phone"),
        property_address=contact_value("property_address", "address_line1", "property_of_interest"),
        campaign_id=campaign_id,
        showing=showing,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    gate = _showcase_gate(form, answers_by_id)
    # A seller already working with another agent is represented too (no follow-up for them).
    represented = gate or ("yes" if str(answers_by_id.get("sell_working_with_agent") or "").strip().lower().startswith("yes") else None)
    lead = await upsert_lead(
        db,
        owner_id=form.owner_id,
        name=submission.contact_name,
        email=submission.contact_email,
        phone=submission.contact_phone,
        source_type="form_submission",
        source_id=submission.id,
        summary=f"Submitted {form.title} form",
        occurred_at=submission.created_at or datetime.now(timezone.utc),
        property_address=submission.property_address,
        form_id=form.id,
        raw_snapshot={"answers": snapshot, "form_type": form.form_type},
        represented=represented,
    )

    redirect_url = None
    if form.form_type == "showcase_request":
        # Best-effort: auto-builds (or updates, if this contact already has a Showcase) a
        # personalized Collection from the submitted preferences. Never raises - a failure here
        # must not break the visitor's form submission, which has already succeeded above.
        from app.services.collections_service import CollectionsService
        showcase_result = await CollectionsService.create_or_update_from_form_submission(db, form, submission, answers_by_id)
        if showcase_result and showcase_result.get("share_token"):
            if getattr(form, "redirect_to_showcase", True):
                redirect_url = f"/showcase/{showcase_result['share_token']}"
            if lead is not None:
                from app.services.tasks_service import resync_lead_followups
                resync_lead_followups(form.owner_id, lead.id, delay=20)
    elif gate == "no":
        # I'm Buying / How Can We Help (as a buyer, no agent): the same Showcase build. They're sent
        # to it after the thank-you unless the agent turned "Open their Showcase" off (user, 10/24).
        # Only when they actually told us what they want, so a blank search never overwrites a
        # Showcase they already have.
        from app.services.collections_service import CollectionsService
        built = await CollectionsService.create_or_update_from_form_submission(
            db, form, submission, answers_by_id, require_criteria=True)
        if built and built.get("share_token") and getattr(form, "redirect_to_showcase", True):
            redirect_url = f"/showcase/{built['share_token']}"
        # The follow-up introduces this Showcase - once its homes are in, re-push the follow-up
        # (made in the background right after the lead) so the calendar alert has the real pitch.
        if built and lead is not None:
            from app.services.tasks_service import resync_lead_followups
            resync_lead_followups(form.owner_id, lead.id, delay=20)
    elif form.form_type == "buyer_onboarding":
        # Buyer Onboarding asks the same search questions as I'm Buying (budget, bedrooms, where),
        # so it builds the client a Showcase too. "Where" is a written answer here - the towns in it
        # are picked out (only real towns in the agent's market's state); none found -> no build.
        criteria = await onboarding_criteria(db, form, answers_by_id)
        if criteria:
            from app.services.collections_service import CollectionsService
            built = await CollectionsService.create_or_update_from_form_submission(
                db, form, submission, criteria, require_criteria=True)
            if built and lead is not None:
                from app.services.tasks_service import resync_lead_followups
                resync_lead_followups(form.owner_id, lead.id, delay=20)

    try:
        notification = Notification(
            agent_id=form.owner_id,
            type="FORM_SUBMISSION",
            reference_type="FORM_SUBMISSION",
            reference_id=submission.id,
            title=f"New {form.title} submission",
            message=f"{submission.contact_name or 'Someone'} submitted your {form.title} form",
            visitor_name=submission.contact_name,
            property_address=submission.property_address,
            link=f"/studio/forms?tab=results&formId={form.id}",
            is_read=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(notification)
        await db.commit()
        from app.models.database import User as _User
        from app.services.agent_email_service import absolute, email_agent
        email_agent(await db.get(_User, form.owner_id), "FORM_SUBMISSION", "form_submission_received", f"New response: {form.title}",
                    {"form_name": form.title, "respondent_name": submission.contact_name, "respondent_email": submission.contact_email,
                     "respondent_phone": submission.contact_phone, "property_address": submission.property_address,
                     "link": absolute(f"/leads/{lead.id}" if lead is not None else f"/studio/forms?tab=results&formId={form.id}")})
    except Exception as e:
        logger.error("creating form submission notification failed", extra={"error": str(e)})

    return FormSubmissionResponse(
        success=True,
        message=form.success_message or "Thanks! I'll be in touch soon.",
        submission_id=submission.id,
        redirect_url=redirect_url,
    )
