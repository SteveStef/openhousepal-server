"""Things the AI helper can DO for the agent (owner, 10/30) - always as "I've set it up, tap
Confirm", never on its own.

How it works:
- The model calls an action tool (ACTION_TOOL_DEFINITIONS). Nothing changes yet: the action's
  `prepare` checks the request against the agent's OWN data (owner-scoped, same as ai_tools.py),
  and returns a plain summary of exactly what will happen.
- The prepared action rides on the assistant's reply (AIMessage.tool_calls, record_type
  "action"), and the chat shows it as a card with Confirm / Cancel.
- Only the agent's Confirm tap (POST /ai/actions/confirm) runs it - `run` calls the same service
  code the app's own buttons use, re-checking ownership, and returns a link to the result.

Never here: deletes, billing/plan changes, buying anything, passwords/login email, other agents'
data, admin data. Anything that messages a client says so on the card.
"""
import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.models.database import AIConversation, AIMessage, User

logger = get_logger(__name__)

# The card an action is running for (confirm sets it) - see ai_notify.
CARD: ContextVar[Optional[dict]] = ContextVar("ai_action_card", default=None)

PENDING_HOURS = 24          # a card left untapped this long can't be confirmed any more
MAX_ACTIONS_PER_TURN = 3


class ActionError(Exception):
    """A plain-words reason the action can't be set up or run - shown to the agent as-is."""


@dataclass
class Prepared:
    title: str                      # "Add a note to Dana Whitfield"
    details: List[str]              # bullet lines shown on the card
    payload: Dict[str, Any]         # what `run` needs - resolved, owner-checked ids
    warning: Optional[str] = None   # e.g. "This emails Dana right away."
    confirm_label: str = "Confirm"
    picker: Optional[str] = None     # the card shows this picker (e.g. "brand") - its picks come back on Confirm


@dataclass
class Action:
    definition: dict                                                   # Anthropic tool schema
    prepare: Callable[[AsyncSession, User, dict], Awaitable[Prepared]]
    run: Optional[Callable[[AsyncSession, User, dict], Awaitable[dict]]]  # -> {"message", "link"?}
    plans: Optional[set] = None                                        # None = every plan
    # Built in the agent's browser on Confirm (designs - their builders only exist in the app's
    # own code), which then reports back through client_result(). `run` is None for these.
    client: Optional[str] = None
    # Picks made on the card (a picker) folded into the payload on Confirm - checked like the
    # prepare step (10/31: Branding swatches in the chat). (user, payload, picks) -> payload.
    adjust: Optional[Callable[[User, dict, dict], dict]] = None


ACTIONS: Dict[str, Action] = {}


def register(definition: dict, prepare, run, plans: Optional[set] = None, client: Optional[str] = None, adjust=None) -> None:
    ACTIONS[definition["name"]] = Action(definition, prepare, run, plans, client, adjust)


def _allowed(user: User, a: Action) -> bool:
    return a.plans is None or user.is_admin or user.plan_tier in a.plans


def action_tools_for(user: User) -> list:
    _load()
    return [a.definition for a in ACTIONS.values() if _allowed(user, a)]


def is_action(name: str) -> bool:
    _load()
    return name in ACTIONS


def _coerce(definition: dict, args: dict) -> dict:
    """The model now and then sends a list or an object as JSON text (questions, tags...) - read it
    back so the action gets the shape its schema says (a form card crashed on this, 11/01)."""
    props = ((definition or {}).get("input_schema") or {}).get("properties") or {}
    out = dict(args)
    for k, v in args.items():
        want = (props.get(k) or {}).get("type")
        if want in ("array", "object") and isinstance(v, str) and v.strip()[:1] in "[{":
            try:
                got = json.loads(v)
            except ValueError:
                continue
            if isinstance(got, list if want == "array" else dict):
                out[k] = got
    return out


# The same big job asked for again (owner, 11/01: reloaded while an AI CMA was running, and the
# helper offered a second CMA card for the same home): the slow, one-per-thing jobs, keyed by what
# makes them the same thing. A card for it that's working or done in the last 3 hours - in any of
# their chats - answers instead of a new card.
_SAME_JOB = {
    "start_cma": lambda pl: pl.get("listing_key") or pl.get("address"),
    "write_blog_post": lambda pl: " ".join(str((pl.get("body") or {}).get("topic") or "").lower().split()) or None,
    "create_spotlight": lambda pl: pl.get("listing_key"),
    "create_showcase": lambda pl: str((pl.get("body") or {}).get("visitor_email") or "").lower() or None,
}


async def _recent_same_job(db: AsyncSession, user: User, name: str, payload: dict) -> Optional[dict]:
    keyfn = _SAME_JOB.get(name)
    key = keyfn(payload or {}) if keyfn else None
    if not key:
        return None
    since = datetime.now(timezone.utc) - timedelta(hours=3)
    rows = (await db.execute(
        select(AIMessage.tool_calls, AIMessage.created_at).join(AIConversation, AIConversation.id == AIMessage.conversation_id)
        .where(AIConversation.owner_id == user.id, AIMessage.role == "assistant", AIMessage.created_at >= since)
        .order_by(AIMessage.created_at.desc()).limit(80)
    )).all()
    for calls, at in rows:
        for t in calls or []:
            if (isinstance(t, dict) and t.get("record_type") == "action" and t.get("tool") == name
                    and t.get("status") in ("working", "running", "done") and keyfn(t.get("payload") or {}) == key):
                return {**t, "_at": at}
    return None


async def propose(db: AsyncSession, user: User, name: str, args: dict) -> tuple[dict, Optional[dict]]:
    """Runs `prepare` only. Returns (tool result for the model, card entry or None)."""
    a = ACTIONS.get(name)
    if not a or not _allowed(user, a):
        return {"error": "not_available", "message": "That isn't something I can do on this plan."}, None
    args = {k: v for k, v in (args or {}).items() if k not in {"owner_id", "user_id", "agent_id", "current_user_id"}}
    args = _coerce(a.definition, args)
    try:
        p = await a.prepare(db, user, args)
    except ActionError as e:
        return {"error": "cannot_prepare", "message": str(e)}, None
    except Exception:
        logger.exception("AI action prepare failed", extra={"action": name, "user_id": user.id})
        return {"error": "cannot_prepare", "message": "Something went wrong setting that up."}, None
    try:
        same = await _recent_same_job(db, user, name, p.payload)
    except Exception:  # noqa: BLE001
        logger.warning("same-job check failed", exc_info=True)
        same = None
    if same:
        mins = max(1, int((datetime.now(timezone.utc) - same["_at"]).total_seconds() // 60))
        state = ("is still being made - it posts in that chat with a button when it's done (the chat button shows a red number)"
                 if same.get("status") in ("working", "running") else "is already done")
        return {"error": "already_started", "card_title": same.get("title"), "link": same.get("result_link"),
                "message": f"Don't set this up again: \u201c{same.get('title')}\u201d was started {mins} min ago and {state}. "
                           "Tell them that (with a show_link button to it when there's a link) instead of making a new card."}, None
    entry = {
        "tool": name, "record_type": "action", "record_id": str(uuid.uuid4()),
        "title": p.title, "details": p.details, "warning": p.warning, "confirm_label": p.confirm_label, "picker": p.picker,
        "payload": p.payload, "status": "pending", "client_action": a.client,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=PENDING_HOURS)).isoformat(),
    }
    try:
        before = await _field_snapshot(db, user, p.payload or {})
        if before:
            entry["before"] = before
    except Exception:  # noqa: BLE001 - the check is a safety net, never a reason to fail the card
        logger.warning("card snapshot failed", exc_info=True)
    return {"status": "waiting_for_agent_to_confirm", "card_title": p.title, "card_details": p.details, "card_button": p.confirm_label,
            "warning": p.warning}, entry


# A card that changes fields on one of their things notes those fields' values when it's made. If
# they change one of them somewhere else (its own page) before tapping, the card stops instead of
# putting back what it planned (owner, 11/03: "don't ruin a finished version" after seeing the
# chat's older card). Only the fields the card changes are compared - a new guest or a like on the
# same lead/Showcase doesn't count.
def _target_models():
    from app.models.database import AgentForm, AgentTask, Collection, Lead, OpenHouseEvent
    return {"lead_id": Lead, "task_id": AgentTask, "collection_id": Collection, "open_house_id": OpenHouseEvent, "form_id": AgentForm}


async def _field_snapshot(db: AsyncSession, user: User, payload: dict, fields: Optional[List[str]] = None) -> Optional[dict]:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else None
    if not body:
        return None
    key, model = next(((k, m) for k, m in _target_models().items() if payload.get(k)), (None, None))
    row = await db.get(model, payload[key]) if model else user
    if row is None:
        return None
    cols = set(row.__table__.columns.keys())
    names = fields if fields is not None else [f for f in body if f in cols and f not in {"id", "owner_id", "agent_id", "user_id"}]
    snap = {}
    for f in names:
        v = getattr(row, f, None)
        snap[f] = v if v is None or isinstance(v, (str, int, float, bool, list, dict)) else str(v)
    return {"key": key, "fields": snap} if snap else None


async def _owned_entry(db: AsyncSession, user: User, message_id: str, action_id: str, lock: bool = False):
    stmt = (select(AIMessage).join(AIConversation, AIConversation.id == AIMessage.conversation_id)
            .where(AIMessage.id == message_id, AIConversation.owner_id == user.id))
    if lock:
        # Confirm reads + claims the card under a row lock: two taps (or a retry) at the same moment
        # wait for each other, so the second sees "running" - one card can never run twice (11/01).
        stmt = stmt.with_for_update(of=AIMessage)
    msg = (await db.execute(stmt)).scalar_one_or_none()
    entry = next((t for t in (msg.tool_calls or []) if t.get("record_type") == "action" and t.get("record_id") == action_id), None) if msg else None
    if not entry:
        raise ActionError("I couldn't find that request any more.")
    return msg, entry


async def _save(db: AsyncSession, msg: AIMessage, entry: dict, **changes) -> dict:
    entry.update(changes)
    flag_modified(msg, "tool_calls")
    await db.commit()
    return {k: entry.get(k) for k in ("record_id", "status", "result_message", "result_link", "made", "share")}


async def confirm(db: AsyncSession, user: User, message_id: str, action_id: str, picks: Optional[dict] = None) -> dict:
    msg, entry = await _owned_entry(db, user, message_id, action_id, lock=True)
    if entry.get("client_action") and entry.get("status") == "running" and entry.get("started_at") and \
            datetime.fromisoformat(entry["started_at"]) < datetime.now(timezone.utc) - timedelta(minutes=10):
        entry["status"] = "pending"   # the browser that was building it went away - let them try again
    if entry.get("status") != "pending":
        return {k: entry.get(k) for k in ("record_id", "status", "result_message", "result_link")}
    if datetime.fromisoformat(entry["expires_at"]) < datetime.now(timezone.utc):
        return await _save(db, msg, entry, status="expired", result_message="This request is over a day old - ask me again.")
    _load()
    a = ACTIONS.get(entry["tool"])
    if not a or not _allowed(user, a):
        return await _save(db, msg, entry, status="failed", result_message="That isn't something I can do on this plan.")
    before = entry.get("before")
    if before and isinstance(before.get("fields"), dict):
        try:
            now_snap = await _field_snapshot(db, user, dict(entry.get("payload") or {}), list(before["fields"]))
        except Exception:  # noqa: BLE001
            now_snap = None
        if now_snap and now_snap["fields"] != before["fields"]:
            return await _save(db, msg, entry, status="stale",
                               result_message="This was changed on its own page after I made this card, so I left it as it is. Ask me again if you still want this change.")
    if picks and a.adjust:
        try:
            entry["payload"] = a.adjust(user, dict(entry.get("payload") or {}), picks)
        except ActionError as e:
            return {"record_id": action_id, "status": "pending", "result_message": str(e), "result_link": None}
    if a.client:
        # The browser builds it now and reports back (client_result) - claimed so a double tap
        # can't build two.
        await _save(db, msg, entry, status="running", started_at=datetime.now(timezone.utc).isoformat())
        return {"record_id": action_id, "status": "running", "client_action": a.client, "payload": entry.get("payload")}
    # Claim it first so a double tap can't run it twice.
    await _save(db, msg, entry, status="running")
    try:
        # Which card this is - an action that finishes later reports back to it (ai_notify). Kept
        # out of the payload (some actions send theirs straight on to a route).
        CARD.set({"message_id": message_id, "action_id": action_id, "conversation_id": msg.conversation_id})
        out = await a.run(db, user, dict(entry.get("payload") or {}))
    except ActionError as e:
        await db.rollback()
        msg, entry = await _owned_entry(db, user, message_id, action_id)
        return await _save(db, msg, entry, status="failed", result_message=str(e))
    except Exception:
        logger.exception("AI action run failed", extra={"action": entry["tool"], "user_id": user.id})
        await db.rollback()
        msg, entry = await _owned_entry(db, user, message_id, action_id)
        return await _save(db, msg, entry, status="failed", result_message="Something went wrong - nothing was changed.")
    msg, entry = await _owned_entry(db, user, message_id, action_id)
    logger.info("AI action done", extra={"event": "ai_action", "action": entry["tool"], "user_id": user.id})
    # share: the link a client would get by text ({url, preview}) - the chat shows its Messages-style card.
    # working: started and still going (an AI CMA) - ai_notify turns the card done when it finishes.
    # made: something the card can show or finish in the browser (a Spotlight's automatic setup).
    return await _save(db, msg, entry, status="working" if out.get("working") else "done", result_message=out.get("message") or "Done.",
                       result_link=out.get("link"), share=out.get("share") if isinstance(out.get("share"), dict) else None,
                       made=out.get("made") if isinstance(out.get("made"), dict) else None)


async def client_result(db: AsyncSession, user: User, message_id: str, action_id: str, ok: bool, message: str,
                        link: Optional[str], made: Optional[dict]) -> dict:
    """The browser finished (or failed) a client-built action - record it on the card."""
    from app.services.ai.ai_ui_tools import safe_page
    msg, entry = await _owned_entry(db, user, message_id, action_id)
    if not entry.get("client_action") or entry.get("status") != "running":
        return {k: entry.get(k) for k in ("record_id", "status", "result_message", "result_link")}
    made = {k: str(v)[:200] for k, v in (made or {}).items() if k in ("kind", "design_id", "title", "is_print") and v is not None} or None
    logger.info("AI action done", extra={"event": "ai_action", "action": entry["tool"], "user_id": user.id, "ok": ok})
    return await _save(db, msg, entry, status="done" if ok else "failed", result_message=(message or ("Done." if ok else "That didn't work - nothing was made."))[:300],
                       result_link=safe_page(link or "") or None, made=made)


async def cancel(db: AsyncSession, user: User, message_id: str, action_id: str) -> dict:
    msg, entry = await _owned_entry(db, user, message_id, action_id)
    if entry.get("status") != "pending":
        return {k: entry.get(k) for k in ("record_id", "status", "result_message", "result_link")}
    return await _save(db, msg, entry, status="cancelled", result_message="Cancelled - nothing was changed.")


_loaded = False


def _load() -> None:
    """The action modules register themselves on import (kept out of import time here so this
    module never pulls in every route/service just to be imported)."""
    global _loaded
    if not _loaded:
        _loaded = True
        from app.services.ai import ai_actions_catalog  # noqa: F401
