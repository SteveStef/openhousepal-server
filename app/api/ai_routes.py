from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import AIConversation, AIMessage, AIRealtimeSession, User
from app.schemas.ai import (
    AIConversationListItem, AIConversationOut, AIMessageOut,
    AISendMessageRequest, AISendMessageResponse, AIInsightsResponse, AISpeechRequest,
    AIRealtimeSessionOut, AIRealtimeToolCallRequest, AIRealtimeToolCallResponse,
    AIRealtimeUsageRequest, AIRealtimeUsageResponse, AIRealtimeHeartbeatRequest, AIRealtimeEndRequest,
    DailyDigestResponse, DigestDismissRequest, MonthlyAIUsageResponse, ChatUsageResponse,
    CopywriteRequest, CopywriteResponse, ComplianceCheckRequest, ComplianceCheckResponse,
)
from app.services.ai import ai_copywrite_service, ai_insights_service, ai_tools
from app.services.ai.ai_compaction_service import maybe_compact
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_orchestrator_service import (
    AIOrchestratorError, run_turn, _extract_record_refs, _extract_result_refs,
)
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, chat_usage_snapshot, check_and_reserve, check_input_length, monthly_ai_budget_snapshot,
    monthly_cost_so_far, record_usage_event, release, reconcile_budget, reserve_global_and_emergency,
)
from app.services.ai.ai_realtime_service import (
    create_ephemeral_session, end_realtime_session, record_realtime_usage, sweep_abandoned_sessions,
)
from app.services.ai.ai_tts_service import synthesize_speech
from app.services.ai.ai_helper_access import can_use_helper, require_ai_helper_user
from app.utils.auth import get_current_active_user, require_admin_user, require_active_plan

logger = get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


async def _get_owned_conversation(
    db: AsyncSession, conversation_id: str, owner_id: str, load_messages: bool = False
) -> AIConversation:
    stmt = select(AIConversation).where(
        AIConversation.id == conversation_id,
        AIConversation.owner_id == owner_id,
        AIConversation.archived_at.is_(None),
    )
    if load_messages:
        stmt = stmt.options(selectinload(AIConversation.messages))
    result = await db.execute(stmt)
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


_CARD_STATUS = {"pending": "showing in the chat right now, waiting for them to tap its button - you DID set it up", "running": "running",
                "working": "they tapped it and it is being made in the background now (a blog draft or an AI CMA) - when it finishes, a message with a button posts in this chat by itself and the chat button shows a red number, even if they close the site", "done": "the agent tapped Confirm and it went through - it is done",
                "failed": "the agent tapped Confirm but it failed - nothing changed", "cancelled": "the agent tapped Cancel - nothing changed", "expired": "expired untapped",
                "replaced": "replaced by the newer card you made after it (the agent asked for a change) - only the newer one can be confirmed"}


def form_card_questions(c: dict) -> list:
    """A form card's questions as they stand now - the agent's taps on the card (saved as they go) over
    what the card first showed."""
    draft = c.get("draft_picks") if isinstance(c.get("draft_picks"), dict) else {}
    qs = draft.get("questions") if isinstance(draft.get("questions"), list) else (c.get("payload") or {}).get("questions")
    return [q for q in (qs or []) if isinstance(q, dict) and q.get("label")]


FORM_CARD_TOOLS = ("create_form", "edit_form")
# What a replaced form card hands on to the new one: the model only re-sends the questions, so without
# this "add a question" quietly undid the card's other settings (owner, 11/04).
FORM_CARD_KEEPS = ("one_contact", "first_last", "background_effects", "welcome_text_effects", "dark", "style", "after_mode", "after_url", "listing_key")


def _form_card_lines(c: dict) -> str:
    """An open form card's numbered questions, so "change question 3" means the same thing to the
    helper as the preview the agent is flipping through (owner, 11/02)."""
    qs = form_card_questions(c)
    draft = c.get("draft_picks") if isinstance(c.get("draft_picks"), dict) else {}
    one = draft["one_contact"] if isinstance(draft.get("one_contact"), bool) else bool((c.get("payload") or {}).get("one_contact"))
    first = 2 if one else 4
    lines = [f"Q{i + first}. {q['label']} [{q.get('type') or 'short_text'}]" + (f" options: {' | '.join(map(str, q['options']))}" if q.get("options") else "")
             + (f" fields: {' | '.join(str(f.get('label')) for f in q['fields'])}" if q.get("fields") else "")
             + (f" show_if: {q['show_if']}" if q.get("show_if") else "")
             for i, q in enumerate(qs)]
    # The model once counted only the agent's own questions ("question 5" = their 5th) and changed the
    # wrong one - so the mapping is spelled out with this form's own example (11/02).
    ex = f" - e.g. “question {first + min(1, len(qs) - 1)}” is “{qs[min(1, len(qs) - 1)]['label']}”" if qs else ""
    return (" Its questions right now, numbered exactly as the form and the card's preview show them (" + ("Q1 is their name, Email and Phone together on one screen" if one else "Q1-Q3 are always Full name, Email, Phone") + "): "
            + "; ".join(lines) + f". “Question N” means QN here{ex}."
            ) if lines else ""


def _card_note(m: AIMessage) -> str:
    """What happened to the Confirm cards on a past reply (10/30) - otherwise the model only sees its
    own "tap Confirm" text and can't tell a done action from an untapped one. It rides on the NEXT
    user message, never inside the reply itself: written there, the model copied it as plain text
    ("[Card: ... waiting for them to tap Confirm]") instead of calling the tool - no button showed."""
    cards = [t for t in (m.tool_calls or []) if isinstance(t, dict) and t.get("record_type") == "action"]
    if not cards:
        return ""
    return "(App note, not from the agent - the cards your last reply showed: " + "; ".join(
        f"“{c.get('title')}” - {_CARD_STATUS.get(c.get('status'), c.get('status'))}"
        + (f" ({c.get('result_message')})" if c.get("status") == "failed" and c.get("result_message") else "")
        + (_form_card_lines(c) if c.get("tool") in FORM_CARD_TOOLS and c.get("status") == "pending" else "")
        + (f" - its link (shown as a preview card): {c['share']['url']}" if isinstance(c.get("share"), dict) and c["share"].get("url") else "") for c in cards) + ")\n\nThe agent says:\n"


def _ids_note(m: AIMessage) -> str:
    """The ids the last reply worked with (a lead, a Showcase). Only the words of a past reply are
    replayed, so "now remove the ones that don't have it" came in with no lead id - the model guessed
    one, got "not found" and asked the agent for the client's name (owner's test, 11/08)."""
    seen, parts = set(), []
    for t in m.tool_calls or []:
        if not isinstance(t, dict) or t.get("record_type") not in ("lead", "showcase") or not t.get("record_id"):
            continue
        key = (t["record_type"], str(t["record_id"]))
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{'lead_id' if key[0] == 'lead' else 'Showcase collection_id'} {key[1]}")
    # The Home search card under that reply, if the agent ran it - which homes it found.
    for t in m.tool_calls or []:
        if isinstance(t, dict) and t.get("record_type") == "home_search":
            r, f = t.get("result"), t.get("filters") or {}
            where = f.get("town") or f.get("district") or "?"
            if not r:
                parts.append(f"a Home search card for {where} the agent hasn't run yet")
            else:
                homes = r.get("homes") or []
                yes = [f"{h.get('street')} ({h['key']})" for h in homes if h.get("v") == "yes"][:30]
                maybe = [f"{h.get('street')} ({h['key']})" for h in homes if h.get("v") == "maybe"][:15]
                parts.append(f"RESULTS of the Home search card under your last reply - the agent ran it, and you know these results, so answer from "
                             f"them: {where}, '{f.get('question')}', {r.get('read')} listings read by AI, {r.get('yes', len(yes))} match(es): {'; '.join(yes) or 'none'}"
                             + (f"; {r.get('maybe', len(maybe))} maybe: {'; '.join(maybe)}" if maybe else ""))
    if not parts:
        return ""
    return "(App note, not from the agent - what your last reply used: " + "; ".join(parts[:8]) + ". Use these ids - never guess one.)\n\n"


async def _replace_open_form_cards(db: AsyncSession, conversation_id: str, refs: list) -> None:
    """A new form card in this chat replaces the one still waiting (the agent asked for a change) - so
    only the newest can be confirmed - and its preview opens on the first question that changed."""
    new = next((r for r in refs or [] if isinstance(r, dict) and r.get("record_type") == "action" and r.get("tool") in FORM_CARD_TOOLS), None)
    if not new:
        return
    rows = (await db.execute(select(AIMessage).where(AIMessage.conversation_id == conversation_id, AIMessage.role == "assistant")
                             .order_by(AIMessage.created_at.desc()).limit(20))).scalars().all()
    from sqlalchemy.orm.attributes import flag_modified
    title = str((new.get("payload") or {}).get("title") or "").strip().lower()
    for m in rows:
        hit = False
        for c in m.tool_calls or []:
            # the same form (same title) - a second, different form made in the same chat stays
            if (isinstance(c, dict) and c.get("record_type") == "action" and c.get("tool") in FORM_CARD_TOOLS and c.get("status") == "pending"
                    and str((c.get("payload") or {}).get("title") or "").strip().lower() == title):
                old, now = form_card_questions(c), (new.get("payload") or {}).get("questions") or []
                diff = next((i for i in range(max(len(old), len(now)))
                             if i >= len(old) or i >= len(now) or {k: old[i].get(k) for k in ("label", "type", "options")} != {k: now[i].get(k) for k in ("label", "type", "options")}), None)
                if diff is not None and "focus_q" not in (new.get("payload") or {}):
                    new["payload"]["focus_q"] = min(diff, max(len(now) - 1, 0))
                draft = c.get("draft_picks") if isinstance(c.get("draft_picks"), dict) else {}
                # a question it re-sent by the same words keeps its fork and its page
                was = {str(q.get("label")).strip().lower(): q for q in old}
                for q in now:
                    for k in ("show_if", "fields"):
                        prev = was.get(str(q.get("label")).strip().lower(), {}).get(k)
                        if prev and not q.get(k):
                            q[k] = prev
                told = set((new.get("payload") or {}).get("told") or [])
                for k in FORM_CARD_KEEPS:
                    v = draft[k] if k in draft else (c.get("payload") or {}).get(k)
                    if k not in told and v is not None:
                        new["payload"][k] = v
                c.update(status="replaced", result_message="Replaced by the updated card below.")
                hit = True
        if hit:
            flag_modified(m, "tool_calls")


async def _build_history(db: AsyncSession, conversation: AIConversation, max_messages: int) -> tuple[List[dict], str]:
    """Recent-message replay window, oldest first. Only the final text of each past turn is
    replayed - not the tool calls that produced it - so token cost stays flat as a
    conversation grows, per the configured max_conversation_messages cap. If the conversation
    has been auto-compacted (ai_compaction_service.py), a leading summary stands in for
    everything at or before summary_covers_through instead of that context just vanishing."""
    stmt = (
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation.id)
        .order_by(AIMessage.created_at.desc())
        .limit(max_messages)
    )
    if conversation.summary_covers_through:
        stmt = stmt.where(AIMessage.created_at > conversation.summary_covers_through)
    result = await db.execute(stmt)
    messages = list(reversed(result.scalars().all()))
    # Each reply's card note goes at the start of the message after it (the newest one's comes back
    # as `trailing`, for the message being sent now).
    history, note = [], ""
    for i, m in enumerate(messages):
        if m.role == "user":
            # A question that never got an answer (that turn failed - ran out of time, the AI was
            # down) is left out: replayed, the chat tried that same big job again on every later
            # message and failed again, so the whole chat seemed broken (owner, 11/08).
            if i + 1 >= len(messages) or messages[i + 1].role == "user":
                continue
            history.append({"role": "user", "content": note + m.content}); note = ""
        else:
            history.append({"role": m.role, "content": m.content})
            card, ids = _card_note(m), _ids_note(m)
            note = ids + (card or ("The agent says:\n" if ids else ""))
    if conversation.summary:
        history.insert(0, {"role": "user", "content": f"[Summary of earlier conversation: {conversation.summary}]"})
    return history, note


@router.get("/showcase-card/{collection_id}")
async def get_showcase_card_rows(collection_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_ai_helper_user)):
    """The chat's Showcase card, as it is now (11/08): opened later, a home removed or recommended
    since then shows that way. Owner-only; no AI."""
    from app.models.database import Collection
    from app.services.ai import ai_showcase_cards
    c = (await db.execute(select(Collection).where(Collection.id == collection_id, Collection.owner_id == current_user.id))).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="That Showcase isn't there any more.")
    return {"collection": ai_showcase_cards.collection_info(c), "homes": await ai_showcase_cards.showcase_rows(db, c)}


class HomeSearchFilters(BaseModel):
    town: Optional[str] = None
    district: Optional[str] = None
    min_beds: Optional[float] = None
    min_sqft: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    question: Optional[str] = None


def _home_search_mls(user: User) -> None:
    from app.utils.auth import tier_has_mls
    if not tier_has_mls(user.plan_tier):
        raise HTTPException(status_code=403, detail="Home search reads MLS listings, which aren't part of this plan.")


@router.get("/home-search/places")
async def home_search_places(q: str = "", db: AsyncSession = Depends(get_db), current_user: User = Depends(require_ai_helper_user)):
    """The Home search card's place box: towns, zips and school districts with homes for sale. No AI."""
    from app.services.ai import ai_home_search
    _home_search_mls(current_user)
    return {"places": await ai_home_search.places(db, q)}


@router.post("/home-search/count")
async def home_search_count(body: HomeSearchFilters, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_ai_helper_user)):
    """How many homes fit the card's boxes and how much chat usage reading them takes - a database
    count, no AI, free (owner, 11/08)."""
    from app.services.ai import ai_home_search
    _home_search_mls(current_user)
    config = await ai_config_service.get_config(db)
    return await ai_home_search.count(db, current_user, config, body.model_dump())


async def _home_search_card(db: AsyncSession, user: User, message_id: str, search_id: str):
    m = (await db.execute(select(AIMessage).join(AIConversation, AIConversation.id == AIMessage.conversation_id)
                          .where(AIMessage.id == message_id, AIConversation.owner_id == user.id))).scalar_one_or_none()
    ref = next((t for t in (m.tool_calls or []) if isinstance(t, dict) and t.get("record_type") == "home_search" and t.get("record_id") == search_id), None) if m else None
    if ref is None:
        raise HTTPException(status_code=404, detail="That search isn't there any more - ask the chat again.")
    return m, ref


@router.post("/home-search/{message_id}/{search_id}/run")
async def home_search_run(message_id: str, search_id: str, body: HomeSearchFilters, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(require_ai_helper_user)):
    """The agent tapped Search on the Home search card: the AI reads every home that fits (at most
    READ_CAP). Re-checks the boxes, the count and their chat usage first - nothing runs unless it all
    fits - then counts the real cost on their Chat usage bar and saves the results on the card."""
    from sqlalchemy.orm.attributes import flag_modified
    from app.services.ai import ai_home_reader, ai_home_search
    _home_search_mls(current_user)
    m, ref = await _home_search_card(db, current_user, message_id, search_id)
    config = await ai_config_service.get_config(db)
    info = await ai_home_search.count(db, current_user, config, body.model_dump())
    if info["missing"]:
        raise HTTPException(status_code=400, detail="Fill in the place, the least bedrooms, the least size and what to look for first.")
    if not info["count"]:
        raise HTTPException(status_code=400, detail="No homes for sale fit those boxes - widen them a little.")
    if info["count"] > info["cap"]:
        raise HTTPException(status_code=400, detail=f"{info['count']:,} homes fit - narrow it to {info['cap']} or fewer first.")
    if not info["enough"]:
        raise HTTPException(status_code=429, detail="There isn't enough chat usage left this month to read these listings. Narrow the search, or get more with your next billing day or a bigger plan.")
    try:
        reservation = await check_and_reserve(db, current_user.id, config, current_user.plan_tier)
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    spent = 0.0
    try:
        out = await ai_home_search.run(db, current_user, config, info["filters"])
        cost = out.pop("_cost")
        spent = float(cost.get("usd") or 0)
        _, limit = await ai_home_reader.room(db, current_user, config)
        out["used_percent"] = ai_home_reader.percent(spent, limit)
        out["ran_at"] = datetime.now(timezone.utc).isoformat()
        # Saved on the card BEFORE the usage row: that commit expires the message, and a change made
        # after it was lost (11/08 test: results gone on reload).
        m, ref = await _home_search_card(db, current_user, message_id, search_id)
        ref["filters"], ref["count"], ref["result"] = info["filters"], info["count"], out
        conv_id = m.conversation_id
        flag_modified(m, "tool_calls")
        await db.commit()
        await record_usage_event(db, current_user.id, conv_id, config["model"], cost.get("in"), cost.get("out"), spent, 0, False, None,
                                 tool_names=["home_search"])
        return out
    finally:
        await reconcile_budget(db, reservation, spent)
        await release(current_user.id)


@router.get("/suggestions")
async def get_ai_suggestions(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_ai_helper_user)):
    """The chat start screen's "Try asking", from their own account (11/04) - no AI, costs nothing."""
    from app.services.ai.ai_suggestions import suggestions_for
    return {"suggestions": await suggestions_for(db, current_user)}


@router.get("/helper/status")
async def get_ai_helper_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Whether to show the bottom-right AI helper for this agent (every plan since 10/30, unless
    the Admin switch is off) and whether its voice parts show (admin only - pricier models)."""
    # Voice (read-aloud, Talk to OHP) is off for everyone, admin too, until it's polished (owner, 10/30).
    # Typing by voice with the mic is the browser's own dictation and stays.
    return {"enabled": await can_use_helper(db, current_user), "voice": False}


@router.get("/unread")
async def get_ai_unread(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Chats where the helper finished something while the agent was away (an AI CMA) - the chat
    button shows the count on every device until that chat is opened (ai_notify) - and the jobs
    still running, for the chat's progress strip."""
    from app.services.ai import ai_notify
    working = await ai_notify.working(db, current_user)
    await db.refresh(current_user, ["setup_state"])   # working() may have just closed a dead job
    return {"conversations": ai_notify.unread(current_user), "working": working, "news": ai_notify.news(current_user)}


@router.get("/progress")
async def get_ai_progress(current_user: User = Depends(get_current_active_user)):
    """What the helper is doing right now for this agent's reply ("Searching your open house
    guests") - the chat polls it while a reply is coming (owner, 11/01). Older than 3 minutes = none."""
    from app.services import app_cache
    v = await app_cache.load(f"ai_progress:{current_user.id}")
    if not isinstance(v, dict) or not v.get("label"):
        return {"label": None}
    try:
        fresh = datetime.now(timezone.utc) - datetime.fromisoformat(v.get("at")) < timedelta(minutes=3)
    except (TypeError, ValueError):
        fresh = False
    return {"label": v["label"] if fresh else None}


@router.post("/attachments")
async def add_ai_attachment(file: UploadFile = File(...), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_ai_helper_user)):
    """A photo dropped into the chat or onto a card. Kept like any Media Lab upload (same size and
    storage limits), so it can be used for anything the chat can make."""
    from app.services.ai import ai_attachments
    return await ai_attachments.add(db, current_user, await file.read(), file.content_type or "")


class NewsSeenRequest(BaseModel):
    ids: list[str]


@router.post("/news/seen")
async def mark_ai_news_seen(body: NewsSeenRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """The chat showed these finished-job notices, so they never show again."""
    from app.services.ai import ai_notify
    await ai_notify.mark_news_seen(db, current_user, body.ids[:20])
    return {"ok": True}


@router.post("/unread/{conversation_id}/read")
async def mark_ai_chat_read(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    from app.services.ai import ai_notify
    return {"conversations": await ai_notify.mark_read(db, current_user, conversation_id)}


@router.post("/actions/{action_id}/confirm")
async def confirm_ai_action(
    action_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    """The agent tapped Confirm on something the helper set up - the only way an action runs."""
    from app.services.ai import ai_actions
    try:
        picks = payload.get("picks") if isinstance(payload.get("picks"), dict) else None   # choices made on the card
        return await ai_actions.confirm(db, current_user, str(payload.get("message_id") or ""), action_id, picks)
    except ai_actions.ActionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/actions/{action_id}/draft")
async def save_ai_action_draft(
    action_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    """The agent's taps on a card still waiting (a form's questions) - kept on the card so the helper
    works from what they see when they ask for a change. Nothing runs."""
    from app.services.ai import ai_actions
    picks = payload.get("picks") if isinstance(payload.get("picks"), dict) else None
    if not picks or len(str(picks)) > 20000:
        raise HTTPException(status_code=400, detail="Nothing to save.")
    try:
        msg, entry = await ai_actions._owned_entry(db, current_user, str(payload.get("message_id") or ""), action_id)
    except ai_actions.ActionError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if entry.get("status") != "pending":
        return {"saved": False}
    await ai_actions._save(db, msg, entry, draft_picks=picks)
    return {"saved": True}


@router.post("/actions/{action_id}/revise-form")
async def revise_ai_form_card(
    action_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    """A form card's own "What should change or be added?" line (owner, 11/03): changes the card's
    questions right where they are - no new card under it. Saved on the card like its taps."""
    from app.services.ai import ai_actions, ai_copywrite_service
    try:
        msg, entry = await ai_actions._owned_entry(db, current_user, str(payload.get("message_id") or ""), action_id)
    except ai_actions.ActionError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if entry.get("tool") not in FORM_CARD_TOOLS or entry.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This card is finished - ask me in the chat instead.")
    picks = payload.get("picks") if isinstance(payload.get("picks"), dict) else dict(entry.get("draft_picks") or {})
    questions = [q for q in (picks.get("questions") or (entry.get("payload") or {}).get("questions") or []) if isinstance(q, dict)]
    at = payload.get("at") if isinstance(payload.get("at"), int) else None
    out = await ai_copywrite_service.revise_form_questions(
        db, current_user.id, current_user.plan_tier, title=str((entry.get("payload") or {}).get("title") or "Form"),
        questions=questions, at=at, instruction=str(payload.get("instruction") or ""))
    from app.services.ai.ai_actions_catalog import ActionError, form_style
    try:
        style = form_style(current_user, out.pop("look", None), out.pop("color", None), out.pop("metal", None))
    except ActionError as e:
        out.update(note=str(e))
        style = {}
    if style:
        out["style"] = {**(picks.get("style") if isinstance(picks.get("style"), dict) else {}), **style}
    msg, entry = await ai_actions._owned_entry(db, current_user, str(payload.get("message_id") or ""), action_id)
    await ai_actions._save(db, msg, entry, draft_picks={**picks, "questions": out["questions"], **{k: out[k] for k in ("one_contact", "first_last", "dark") if isinstance(out.get(k), bool)}, **({"style": out["style"]} if out.get("style") else {})})
    return out


@router.post("/actions/{action_id}/client-result")
async def client_ai_action_result(
    action_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    """The agent's browser finished building something the helper set up (a design) - record it."""
    from app.services.ai import ai_actions
    try:
        return await ai_actions.client_result(
            db, current_user, str(payload.get("message_id") or ""), action_id, bool(payload.get("ok")),
            str(payload.get("message") or ""), payload.get("link"), payload.get("made") if isinstance(payload.get("made"), dict) else None,
        )
    except ai_actions.ActionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/actions/{action_id}/cancel")
async def cancel_ai_action(
    action_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    from app.services.ai import ai_actions
    try:
        return await ai_actions.cancel(db, current_user, str(payload.get("message_id") or ""), action_id)
    except ai_actions.ActionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/conversations", response_model=AIConversationListItem, status_code=201)
async def create_ai_conversation(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    # One chat at a time (owner, 11/01: no chat history - a clean slate after 30 minutes, and every
    # old chat resent as context costs their allowance). Starting one tidies the others away the way
    # the delete route does (archived, never destroyed - the duplicate-job guard and Admin counts
    # still read them); a chat waiting on a job or holding unread news stays until it's read.
    from app.services.ai import ai_notify
    keep = ai_notify.busy_conversations(current_user)
    tidy = update(AIConversation).where(AIConversation.owner_id == current_user.id, AIConversation.archived_at.is_(None))
    if keep:
        tidy = tidy.where(AIConversation.id.notin_(keep))
    await db.execute(tidy.values(archived_at=datetime.now(timezone.utc)))
    conversation = AIConversation(owner_id=current_user.id)
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.get("/conversations", response_model=List[AIConversationListItem])
async def list_ai_conversations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    stmt = (
        select(AIConversation)
        .where(AIConversation.owner_id == current_user.id, AIConversation.archived_at.is_(None))
        .order_by(AIConversation.last_message_at.desc().nullslast(), AIConversation.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/conversations/{conversation_id}", response_model=AIConversationOut)
async def get_ai_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    return await _get_owned_conversation(db, conversation_id, current_user.id, load_messages=True)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_ai_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    """Soft-delete only - sets archived_at. The conversation row and its messages are never
    destroyed, so the audit trail (AIUsageEvent) always resolves back to a real conversation."""
    conversation = await _get_owned_conversation(db, conversation_id, current_user.id)
    conversation.archived_at = datetime.now(timezone.utc)
    db.add(conversation)
    await db.commit()


@router.post("/conversations/{conversation_id}/messages", response_model=AISendMessageResponse)
async def send_ai_message(
    conversation_id: str,
    payload: AISendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_helper_user),
):
    conversation = await _get_owned_conversation(db, conversation_id, current_user.id)
    config = await ai_config_service.get_config(db)
    # Read once, up front: after a failed turn the DB session is broken, and reading
    # current_user.id then reloads it from the DB and crashes - which skipped the release below
    # and left the agent locked out of the chat ("You already have a request in progress") until
    # a restart (owner, 11/01).
    user_id = current_user.id

    message_text = (payload.message or "").strip()
    # Photos dropped in: the chat keeps a note with each one's address, and this turn also gets to
    # see them (a smaller copy), so "stage this" needs no more explaining.
    from app.services.ai import ai_attachments
    attached = await ai_attachments.owned(db, user_id, payload.attachments)
    if attached and not message_text:
        message_text = "Here's a photo."
    if not message_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    message_text += ai_attachments.note(attached)

    try:
        check_input_length(message_text, config)
        reservation = await check_and_reserve(db, current_user.id, config, current_user.plan_tier)
    except AIRateLimitExceeded as e:
        await record_usage_event(
            db, current_user.id, conversation.id, config.get("model"),
            None, None, None, 0, True, e.reason,
        )
        raise HTTPException(status_code=429, detail=e.user_message)

    try:
        block_reason = await maybe_compact(db, current_user, conversation, config)
        if block_reason:
            await record_usage_event(
                db, current_user.id, conversation.id, config.get("model"),
                None, None, None, 0, True, block_reason,
            )
            # The turn never ran - refund the Layer-3 reservation check_and_reserve already
            # made in full, so a conversation-length block never silently eats into the
            # platform-wide global/emergency budgets.
            await reconcile_budget(db, reservation, 0.0)
            raise HTTPException(
                status_code=429,
                detail="This conversation has gotten long enough that quality may suffer. Please start a new conversation to continue.",
            )

        history, card_note = await _build_history(db, conversation, config["max_conversation_messages"])

        user_msg = AIMessage(conversation_id=conversation.id, role="user", content=message_text)
        db.add(user_msg)
        await db.commit()
        await db.refresh(user_msg)
        user_msg_id = user_msg.id

        try:
            text, refs, usage = await run_turn(db, current_user, history, card_note + message_text, voice_mode=payload.voice_mode,
                                               images=await ai_attachments.image_blocks(attached))
        except AIOrchestratorError as e:
            # A turn cut off by the time limit can stop mid-query, leaving the session needing a
            # rollback before it can record anything.
            await db.rollback()
            # What the partial turn really cost (the orchestrator only prices a finished turn).
            # Logged as blocked, so it never counts on the agent's meter - they got no answer -
            # but the platform counters and admin view see the real spend.
            spent = e.usage.get("estimated_cost_usd")
            if spent is None:
                spent = ai_config_service.estimate_cost_usd(
                    config, e.usage.get("model", config.get("model")), e.usage.get("input_tokens") or 0, e.usage.get("output_tokens") or 0,
                    e.usage.get("cache_write_tokens") or 0, e.usage.get("cache_read_tokens") or 0,
                )
            await record_usage_event(
                db, user_id, conversation_id, e.usage.get("model", config.get("model")),
                e.usage.get("input_tokens"), e.usage.get("output_tokens"), spent,
                e.usage.get("tool_call_count", 0), True, e.reason,
                tool_names=e.usage.get("tool_names"),
            )
            # A failed/aborted turn may still have made real (partial) model calls before
            # erroring out - true up the reservation with whatever it actually cost, never a
            # blind full refund, so the global/emergency counters stay accurate either way.
            await reconcile_budget(db, reservation, spent or 0.0)
            # The question goes back in their typing box (the chat takes it off the screen), so it
            # isn't kept either - an unanswered question left in the chat was replayed into every
            # later message, and they failed too (owner, 11/08). _build_history skips any older ones.
            try:
                from sqlalchemy import delete as _delete
                await db.execute(_delete(AIMessage).where(AIMessage.id == user_msg_id))
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
            # Ran out of time = the question was too big for one turn, not something broken on our
            # side: 504 keeps its own words. A 503 is swapped for "isn't available right now - we've
            # been notified" (main.provider_failure_handler), which told the owner (11/08) the
            # assistant was down when it had only run long.
            raise HTTPException(status_code=504 if e.reason == "timeout" else 503, detail=e.user_message)

        await _replace_open_form_cards(db, conversation.id, refs)
        assistant_msg = AIMessage(conversation_id=conversation.id, role="assistant", content=text, tool_calls=refs or None)
        db.add(assistant_msg)

        conversation.last_message_at = datetime.now(timezone.utc)
        if not conversation.title:
            # whole words only - a title cut mid-word read badly in the chat header (11/01)
            t = " ".join(message_text.split())
            conversation.title = t if len(t) <= 80 else t[:79].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
        db.add(conversation)

        await db.commit()
        await db.refresh(assistant_msg)

        await record_usage_event(
            db, current_user.id, conversation.id, usage["model"], usage["input_tokens"], usage["output_tokens"],
            usage.get("estimated_cost_usd"), usage["tool_call_count"], False, None,
            tool_names=usage.get("tool_names"),
        )
        await reconcile_budget(db, reservation, usage.get("estimated_cost_usd") or 0.0)

        return AISendMessageResponse(
            conversation_id=conversation.id,
            user_message=AIMessageOut.model_validate(user_msg),
            assistant_message=AIMessageOut.model_validate(assistant_msg),
        )
    finally:
        await release(user_id)


@router.get("/insights", response_model=AIInsightsResponse)
async def get_ai_insights(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Rule-based digest for the Assistant home screen - zero LLM cost, no rate limiting
    needed since it never calls the model. Open to every user (not admin-gated like the rest
    of this router) since it never touches the LLM assistant this router otherwise gates."""
    insights = await ai_insights_service.get_insights(db, current_user)
    return AIInsightsResponse(insights=insights, generated_at=datetime.now(timezone.utc))


@router.get("/digest", response_model=DailyDigestResponse)
async def get_daily_digest_route(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Powers the Dashboard's Daily Digest panel for every user - rule-based, zero LLM cost,
    same reasoning as /insights above. Kept separate from /insights (the Assistant home
    screen's admin-only feed) because this one is richer: a "needs attention" queue with
    deep links straight to the thing to address, plus a chronological feed of what actually
    happened today."""
    return await ai_insights_service.get_daily_digest(db, current_user)


@router.get("/usage/monthly", response_model=MonthlyAIUsageResponse)
async def get_monthly_ai_usage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The "Monthly AI Usage" meter: this agent's month-to-date spend across every AI tool
    combined, as a share of their plan's monthly AI budget. Open to every user; returns
    percent=null for a tier with no configured budget (the frontend then shows no meter)."""
    config = await ai_config_service.get_config(db)
    return await monthly_ai_budget_snapshot(db, current_user.id, config, current_user.plan_tier)


@router.get("/usage/chat", response_model=ChatUsageResponse)
async def get_chat_usage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The "Chat usage" bar (owner, 11/01): the AI helper chat's own allowance for this billing
    period, by plan, separate from the Monthly AI Usage bar. Shown as a percent only."""
    config = await ai_config_service.get_config(db)
    snap = await chat_usage_snapshot(db, current_user.id, config, current_user.plan_tier)
    if not await can_use_helper(db, current_user):
        snap["percent"] = None  # no chat for them yet - the bar doesn't show
    return snap


@router.get("/copywrite/estimate")
async def estimate_ai_copywrite(
    purpose: str,
    chars: int = 1500,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Roughly what one /ai/copywrite call of this purpose will cost, for a "~X% of your monthly
    AI" note shown before the agent spends it (e.g. the open house Cheat Sheet's Recap). Uses the
    same model + pricing as the real call; `chars` is the length of the text it will read."""
    config = await ai_config_service.get_config(db)
    model = config["copywrite_model"]
    input_tokens = 700 + max(0, min(int(chars), 20000)) // 4
    output_tokens = min(int(config.get("copywrite_max_output_tokens") or 400), 160)
    usd = float(ai_config_service.estimate_cost_usd(config, model, input_tokens, output_tokens))
    from app.services.ai.ai_rate_limit_service import monthly_limit_usd
    limit = await monthly_limit_usd(db, current_user.id, config, current_user.plan_tier)
    return {"purpose": purpose, "usd": round(usd, 5), "percent": round(usd / float(limit) * 100, 3) if limit else None}


@router.post("/copywrite", response_model=CopywriteResponse)
async def ai_copywrite(
    payload: CopywriteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """AI copywriter - Design Studio template text, Blogs AI mode, Agent Hub bio polish, plus
    caption/email/text drafts. Metered against the shared Monthly AI Usage allowance."""
    return await ai_copywrite_service.generate(
        db, current_user.id, current_user.plan_tier,
        purpose=payload.purpose, context=payload.context, tone=payload.tone, instruction=payload.instruction,
    )


@router.post("/compliance-check", response_model=ComplianceCheckResponse)
async def ai_compliance_check(
    payload: ComplianceCheckRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Fair-Housing / MLS advertising review. ADVISORY: returns risky phrases with a suggested
    swap; the agent accepts or ignores each. Never rewrites the text. Metered."""
    return await ai_copywrite_service.compliance_check(db, current_user.id, current_user.plan_tier, text=payload.text)


@router.post("/digest/dismiss")
async def dismiss_daily_digest_item(
    payload: DigestDismissRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Manually dismiss one Daily Digest item without navigating away - the persistent escape
    hatch for any item that can't (or shouldn't have to) be resolved just by looking at it."""
    await ai_insights_service.dismiss_digest_item(db, current_user.id, payload.item_id)
    return {"ok": True}


@router.post("/speech")
async def get_ai_speech(
    payload: AISpeechRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Read Aloud - synthesizes exactly the text handed in (already a finished Claude reply)
    via OpenAI TTS. Never calls Claude or any reasoning model - see ai_tts_service.py."""
    config = await ai_config_service.get_config(db)

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No text to speak.")
    text = text[: config["tts_max_chars"]]

    cost_so_far = await monthly_cost_so_far(db, current_user.id, "tts")
    if cost_so_far >= config["tts_monthly_cost_ceiling_usd"]:
        await record_usage_event(
            db, current_user.id, payload.conversation_id, config.get("tts_model"),
            None, None, None, 0, True, "tts_cost_ceiling", event_type="tts",
        )
        raise HTTPException(status_code=429, detail="You've reached your Read Aloud usage limit for this month.")

    try:
        reservation = await reserve_global_and_emergency(
            db, "tts_global", config["tts_global_reservation_estimate_usd"],
            config["tts_global_daily_cost_ceiling_usd"], config["tts_global_monthly_cost_ceiling_usd"],
            config,
        )
    except AIRateLimitExceeded as e:
        await record_usage_event(
            db, current_user.id, payload.conversation_id, config.get("tts_model"),
            None, None, None, 0, True, e.reason, event_type="tts",
        )
        raise HTTPException(status_code=429, detail=e.user_message)

    try:
        audio_bytes = await synthesize_speech(text, config)
    except Exception as e:
        logger.warning("TTS synthesis failed: %s", e)
        await record_usage_event(
            db, current_user.id, payload.conversation_id, config.get("tts_model"),
            None, None, None, 0, True, "tts_error", event_type="tts",
        )
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status_code=503, detail="Read Aloud is temporarily unavailable. Please try again shortly.")

    cost = ai_config_service.estimate_tts_cost_usd(config, config["tts_model"], len(text))
    await record_usage_event(
        db, current_user.id, payload.conversation_id, config["tts_model"],
        len(text), 0, cost, 0, False, None, event_type="tts",
    )
    await reconcile_budget(db, reservation, cost)

    return Response(content=audio_bytes, media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Talk to OHP (OpenAI Realtime). The browser connects directly to OpenAI over WebRTC - see
# ai_realtime_service.py's module docstring for why these few endpoints are the only points
# left where this backend can enforce limits or tenant isolation for a voice session.
# ---------------------------------------------------------------------------

async def _get_owned_realtime_session(db: AsyncSession, session_id: str, owner_id: str) -> AIRealtimeSession:
    stmt = select(AIRealtimeSession).where(AIRealtimeSession.id == session_id, AIRealtimeSession.owner_id == owner_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session or session.ended_at:
        raise HTTPException(status_code=404, detail="Voice session not found or already ended.")
    return session


@router.post("/realtime/session", response_model=AIRealtimeSessionOut)
async def start_realtime_session(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    config = await ai_config_service.get_config(db)
    await sweep_abandoned_sessions(db, config)
    try:
        return await create_ephemeral_session(db, current_user, config)
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    except Exception as e:
        logger.warning("Failed to start realtime session: %s", e)
        raise HTTPException(status_code=503, detail="Talk to OHP is temporarily unavailable. Please try again shortly.")


@router.post("/realtime/tool-call", response_model=AIRealtimeToolCallResponse)
async def realtime_tool_call(
    payload: AIRealtimeToolCallRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """The one place a Realtime tool call actually reaches this backend - the browser is just
    the relay between OpenAI's Realtime session and this endpoint. Calls the exact same
    ai_tools.execute_tool() the text assistant uses, so a lead that doesn't belong to this
    agent is invisible here for the identical reason it already is in text - no separate
    implementation to drift or hide a tenant-isolation bug in."""
    config = await ai_config_service.get_config(db)
    session = await _get_owned_realtime_session(db, payload.session_id, current_user.id)

    if session.tool_call_count >= config["realtime_max_tool_calls_per_session"]:
        raise HTTPException(status_code=429, detail="Tool call budget for this voice session is exhausted.")

    result = await ai_tools.execute_tool(db, current_user, payload.tool_name, payload.args, config)
    refs = _extract_record_refs(payload.tool_name, payload.args) + _extract_result_refs(payload.tool_name, result)

    session.tool_call_count += 1
    session.last_heartbeat_at = datetime.now(timezone.utc)
    db.add(session)
    await db.commit()

    return AIRealtimeToolCallResponse(result=result, refs=refs)


@router.post("/realtime/usage", response_model=AIRealtimeUsageResponse)
async def realtime_usage(
    payload: AIRealtimeUsageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Forwards the real `usage` object from one Realtime response.done event - cost is
    tracked incrementally throughout the session from actual reported usage, never trusted as
    a single end-of-session self-report."""
    config = await ai_config_service.get_config(db)
    session = await _get_owned_realtime_session(db, payload.session_id, current_user.id)
    should_end = await record_realtime_usage(db, session, current_user, config, payload.usage)
    return AIRealtimeUsageResponse(should_end=should_end)


@router.post("/realtime/heartbeat")
async def realtime_heartbeat(
    payload: AIRealtimeHeartbeatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    session = await _get_owned_realtime_session(db, payload.session_id, current_user.id)
    session.last_heartbeat_at = datetime.now(timezone.utc)
    db.add(session)
    await db.commit()
    return {"ok": True}


@router.post("/realtime/end")
async def realtime_end(
    payload: AIRealtimeEndRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    stmt = select(AIRealtimeSession).where(AIRealtimeSession.id == payload.session_id, AIRealtimeSession.owner_id == current_user.id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Voice session not found.")
    config = await ai_config_service.get_config(db)
    await end_realtime_session(db, session, payload.reason, config=config)
    return {"ok": True}
