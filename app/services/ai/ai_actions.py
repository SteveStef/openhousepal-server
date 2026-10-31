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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.models.database import AIConversation, AIMessage, User

logger = get_logger(__name__)

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


@dataclass
class Action:
    definition: dict                                                   # Anthropic tool schema
    prepare: Callable[[AsyncSession, User, dict], Awaitable[Prepared]]
    run: Optional[Callable[[AsyncSession, User, dict], Awaitable[dict]]]  # -> {"message", "link"?}
    plans: Optional[set] = None                                        # None = every plan
    # Built in the agent's browser on Confirm (designs - their builders only exist in the app's
    # own code), which then reports back through client_result(). `run` is None for these.
    client: Optional[str] = None


ACTIONS: Dict[str, Action] = {}


def register(definition: dict, prepare, run, plans: Optional[set] = None, client: Optional[str] = None) -> None:
    ACTIONS[definition["name"]] = Action(definition, prepare, run, plans, client)


def _allowed(user: User, a: Action) -> bool:
    return a.plans is None or user.is_admin or user.plan_tier in a.plans


def action_tools_for(user: User) -> list:
    _load()
    return [a.definition for a in ACTIONS.values() if _allowed(user, a)]


def is_action(name: str) -> bool:
    _load()
    return name in ACTIONS


async def propose(db: AsyncSession, user: User, name: str, args: dict) -> tuple[dict, Optional[dict]]:
    """Runs `prepare` only. Returns (tool result for the model, card entry or None)."""
    a = ACTIONS.get(name)
    if not a or not _allowed(user, a):
        return {"error": "not_available", "message": "That isn't something I can do on this plan."}, None
    args = {k: v for k, v in (args or {}).items() if k not in {"owner_id", "user_id", "agent_id", "current_user_id"}}
    try:
        p = await a.prepare(db, user, args)
    except ActionError as e:
        return {"error": "cannot_prepare", "message": str(e)}, None
    except Exception:
        logger.exception("AI action prepare failed", extra={"action": name, "user_id": user.id})
        return {"error": "cannot_prepare", "message": "Something went wrong setting that up."}, None
    entry = {
        "tool": name, "record_type": "action", "record_id": str(uuid.uuid4()),
        "title": p.title, "details": p.details, "warning": p.warning, "confirm_label": p.confirm_label,
        "payload": p.payload, "status": "pending", "client_action": a.client,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=PENDING_HOURS)).isoformat(),
    }
    return {"status": "waiting_for_agent_to_confirm", "card_title": p.title, "card_details": p.details,
            "warning": p.warning}, entry


async def _owned_entry(db: AsyncSession, user: User, message_id: str, action_id: str):
    msg = (await db.execute(
        select(AIMessage).join(AIConversation, AIConversation.id == AIMessage.conversation_id)
        .where(AIMessage.id == message_id, AIConversation.owner_id == user.id)
    )).scalar_one_or_none()
    entry = next((t for t in (msg.tool_calls or []) if t.get("record_type") == "action" and t.get("record_id") == action_id), None) if msg else None
    if not entry:
        raise ActionError("I couldn't find that request any more.")
    return msg, entry


async def _save(db: AsyncSession, msg: AIMessage, entry: dict, **changes) -> dict:
    entry.update(changes)
    flag_modified(msg, "tool_calls")
    await db.commit()
    return {k: entry.get(k) for k in ("record_id", "status", "result_message", "result_link", "made")}


async def confirm(db: AsyncSession, user: User, message_id: str, action_id: str) -> dict:
    msg, entry = await _owned_entry(db, user, message_id, action_id)
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
    if a.client:
        # The browser builds it now and reports back (client_result) - claimed so a double tap
        # can't build two.
        await _save(db, msg, entry, status="running", started_at=datetime.now(timezone.utc).isoformat())
        return {"record_id": action_id, "status": "running", "client_action": a.client, "payload": entry.get("payload")}
    # Claim it first so a double tap can't run it twice.
    await _save(db, msg, entry, status="running")
    try:
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
    return await _save(db, msg, entry, status="done", result_message=out.get("message") or "Done.", result_link=out.get("link"))


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
