"""Things the AI helper finishes later (owner, 11/01: an AI CMA started from the chat runs for a
couple of minutes). When it's done: its card turns done, a short message lands in that same chat
with a button to it, and the chat shows as unread - on the chat button, on any device - until the
agent opens that chat.

No new tables: what's being watched and which chats are unread live in users.setup_state
("ai_watch" / "ai_unread"), the same JSON the setup steps use.
"""
from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.models.database import AIConversation, AIMessage, User

logger = get_logger(__name__)

WATCH_KEY = "ai_watch"
UNREAD_KEY = "ai_unread"


def _state(user: User) -> dict:
    return dict(user.setup_state or {})


async def watch(db: AsyncSession, user: User, key: str, ctx: dict, label: str) -> None:
    """Remember which chat card is waiting on `key` (e.g. "cma:<report id>"). Commits."""
    if not ctx or not ctx.get("message_id"):
        return
    st = _state(user)
    st[WATCH_KEY] = {**(st.get(WATCH_KEY) or {}), key: {**ctx, "label": label, "at": datetime.now(timezone.utc).isoformat()}}
    user.setup_state = st
    flag_modified(user, "setup_state")
    await db.commit()


async def finished(owner_id: str, key: str, ok: bool, message: str, link: Optional[str] = None, link_label: str = "Open it") -> None:
    """The watched job is done: update its card, post `message` in its chat ("{label}" = what was
    watched, e.g. the home's street), mark the chat unread.
    Its own session; never raises (the job itself already finished)."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, owner_id)
            if user is None:
                return
            st = _state(user)
            watching = dict(st.get(WATCH_KEY) or {})
            w = watching.pop(key, None)
            if not w:
                return
            now = datetime.now(timezone.utc)
            message = message.replace("{label}", w.get("label") or "this home")
            msg = await db.get(AIMessage, w.get("message_id"))
            if msg is not None:
                for entry in (msg.tool_calls or []):
                    if entry.get("record_type") == "action" and entry.get("record_id") == w.get("action_id"):
                        entry.update(status="done" if ok else "failed", result_message=message[:300], result_link=link)
                flag_modified(msg, "tool_calls")
            conv = await db.get(AIConversation, w.get("conversation_id")) if w.get("conversation_id") else None
            if conv is not None and conv.owner_id == owner_id:
                calls = [{"tool": "show_link", "record_type": "link", "record_id": str(uuid.uuid4()), "label": link_label, "href": link}] if link else None
                db.add(AIMessage(conversation_id=conv.id, role="assistant", content=message, tool_calls=calls))
                conv.last_message_at = now
                st[UNREAD_KEY] = sorted(set(st.get(UNREAD_KEY) or []) | {conv.id})
            st[WATCH_KEY] = watching
            user.setup_state = st
            flag_modified(user, "setup_state")
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("AI helper finished-notice failed", extra={"key": key, "owner_id": owner_id})


def unread(user: User) -> list[str]:
    return list((user.setup_state or {}).get(UNREAD_KEY) or [])


async def mark_read(db: AsyncSession, user: User, conversation_id: str) -> list[str]:
    st = _state(user)
    left = [c for c in (st.get(UNREAD_KEY) or []) if c != conversation_id]
    if left != (st.get(UNREAD_KEY) or []):
        st[UNREAD_KEY] = left
        user.setup_state = st
        flag_modified(user, "setup_state")
        await db.commit()
    return left
