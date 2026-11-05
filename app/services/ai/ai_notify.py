"""Things the AI helper finishes later (owner, 11/01: an AI CMA started from the chat runs for a
couple of minutes). When it's done: its card turns done, a short message lands in that same chat
with a button to it, and the chat shows as unread - on the chat button, on any device - until the
agent opens that chat.

No new tables: what's being watched and which chats are unread live in users.setup_state
("ai_watch" / "ai_unread"), the same JSON the setup steps use. So does "ai_news": each finished job,
shown one time at the top of the chat the next time they open it, wherever they left off (11/05).

Several jobs can run at once (a CMA and a blog), and two can finish in the same moment. Every change
here takes the row lock first, so one job's write can never wipe out the other's.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.logging import get_logger
from app.models.database import AIConversation, AIMessage, User

logger = get_logger(__name__)

WATCH_KEY = "ai_watch"
UNREAD_KEY = "ai_unread"
NEWS_KEY = "ai_news"


def _state(user: User) -> dict:
    return dict(user.setup_state or {})


async def watch(db: AsyncSession, user: User, key: str, ctx: dict, label: str) -> None:
    """Remember which chat card is waiting on `key` (e.g. "cma:<report id>"). Commits."""
    if not ctx or not ctx.get("message_id"):
        return
    await db.refresh(user, ["setup_state"], with_for_update=True)
    st = _state(user)
    st[WATCH_KEY] = {**(st.get(WATCH_KEY) or {}), key: {**ctx, "label": label, "at": datetime.now(timezone.utc).isoformat()}}
    user.setup_state = st
    flag_modified(user, "setup_state")
    await db.commit()


async def finished(owner_id: str, key: str, ok: bool, message: str, link: Optional[str] = None, link_label: str = "Open it", made: Optional[dict] = None) -> None:
    """The watched job is done: update its card, post `message` in its chat ("{label}" = what was
    watched, e.g. the home's street), mark the chat unread.
    Its own session; never raises (the job itself already finished)."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, owner_id, with_for_update=True, populate_existing=True)
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
                        if made:
                            entry["made"] = made
                flag_modified(msg, "tool_calls")
            conv = await db.get(AIConversation, w.get("conversation_id")) if w.get("conversation_id") else None
            if conv is not None and conv.owner_id == owner_id:
                calls = [{"tool": "show_link", "record_type": "link", "record_id": str(uuid.uuid4()), "label": link_label, "href": link}] if link else None
                db.add(AIMessage(conversation_id=conv.id, role="assistant", content=message, tool_calls=calls))
                conv.last_message_at = now
                st[UNREAD_KEY] = sorted(set(st.get(UNREAD_KEY) or []) | {conv.id})
            st[NEWS_KEY] = ([*(st.get(NEWS_KEY) or []), {"id": str(uuid.uuid4()), "text": message[:300], "ok": ok, "link": link, "link_label": link_label,
                                                         "conversation_id": conv.id if conv is not None else None, "at": now.isoformat()}])[-8:]
            st[WATCH_KEY] = watching
            user.setup_state = st
            flag_modified(user, "setup_state")
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("AI helper finished-notice failed", extra={"key": key, "owner_id": owner_id})


async def working(db: AsyncSession, user: User) -> list[dict]:
    """The jobs still running for the chat's progress strip (owner, 11/01: "a nice little loading bar
    that doesn't go away" while they keep talking): kind (cma | blog), its label, when it started,
    and for a blog the pipeline stage it's on (gathering, research, write, fact_check, finishing).
    A job that died quietly (a restart mid-run) is closed here: its card turns failed and the chat
    says so with a button (11/02 - the card said "working" forever), and a blog run gives back what
    it charged, like the Blogs page does."""
    st = user.setup_state or {}
    now = datetime.now(timezone.utc)
    out, dead = [], []
    for key, w in (st.get(WATCH_KEY) or {}).items():
        if not isinstance(w, dict) or ":" not in key:
            continue
        try:
            at = datetime.fromisoformat(w.get("at") or "")
        except ValueError:
            continue
        kind, rid = key.split(":", 1)
        if now - at > timedelta(hours=2):
            dead.append((key, kind, rid, None))
            continue
        out.append({"key": key, "kind": kind, "id": rid, "label": w.get("label") or "", "at": at.isoformat(), "stage": None})
    blogs = [j["id"] for j in out if j["kind"] == "blog"]
    if blogs:
        from app.models.database import Blog
        rows = (await db.execute(select(Blog.id, Blog.ai_review).where(Blog.id.in_(blogs)))).all()
        reviews = {r.id: r.ai_review for r in rows if isinstance(r.ai_review, dict)}
        for j in list(out):
            if j["kind"] != "blog":
                continue
            rv = reviews.get(j["id"]) or {}
            j["stage"] = rv.get("stage")
            # The Blogs page's own rule: a run with no new stage for 15 minutes has died
            try:
                last = datetime.fromisoformat(rv.get("stage_at") or rv.get("started_at") or "")
            except ValueError:
                last = None
            if rv.get("status") == "failed" or (rv.get("status") == "running" and last and now - last > timedelta(minutes=15)):
                out.remove(j); dead.append((j["key"], "blog", j["id"], rv.get("started_at")))
    media = [j["id"] for j in out if j["kind"] == "media"]
    if media:
        # Backstop for a job whose own finish was lost (a restart): the project row knows.
        from app.models.database import StudioMediaAsset
        rows = {r.id: r for r in (await db.execute(select(StudioMediaAsset.id, StudioMediaAsset.status, StudioMediaAsset.kind, StudioMediaAsset.created_at).where(StudioMediaAsset.id.in_(media)))).all()}
        for j in list(out):
            if j["kind"] != "media":
                continue
            r = rows.get(j["id"])
            if r is None:
                out.remove(j); dead.append((j["key"], "media", j["id"], None))
            elif r.status != "processing":
                out.remove(j); await finish_media(user.id, j["id"])
            else:
                j["media"] = r.kind
                made_at = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
                if r.kind == "image" and now - made_at > timedelta(minutes=15):
                    out.remove(j); dead.append((j["key"], "media", j["id"], None))
    for key, kind, rid, started in dead:
        if kind == "blog":
            if started:
                from app.services.ai import ai_blog_pipeline_service
                await ai_blog_pipeline_service.refund_since(db, user.id, started)
            await finished(user.id, key, False, "The blog draft about {label} didn't finish, so nothing was charged for it. Open it on Blogs to try again.",
                           f"/studio/blogs/{rid}", "Open it")
        elif kind == "cma":
            await finished(user.id, key, False, "The CMA for {label} didn't finish. Open it to pick the comps yourself or run it again.",
                           f"/studio/cma/{rid}", "Open it")
        elif kind == "media":
            await finished(user.id, key, False, "That didn't finish - please try again. Nothing was charged for a job that failed.", "/studio/media", "Open Media Lab")
        elif kind == "spotlight":
            await finished(user.id, key, False, "Your Spotlight for {label} is made, but I couldn't finish writing it. Open it and the editor finishes the rest.",
                           f"/studio/property-sites/{rid}", "Open the Spotlight")
        else:
            await finished(user.id, key, False, "That didn't finish - please try again.")
    return sorted(out, key=lambda j: j["at"])


async def finish_media(owner_id: str, asset_id: str) -> None:
    """A Media Lab job left "processing" (studio_media_service calls this): its card shows the result.
    Does nothing unless the chat started it."""
    from app.database import AsyncSessionLocal
    from app.models.database import StudioMediaAsset
    try:
        async with AsyncSessionLocal() as db:
            a = await db.get(StudioMediaAsset, asset_id)
        if a is None or a.status == "processing":
            return
        link = f"/studio/media/{asset_id}"
        if a.status == "succeeded":
            kind = "video" if a.kind == "video" else "photo"
            await finished(owner_id, f"media:{asset_id}", True, f"Your {kind} is ready.", link, "Open it in Media Lab",
                           made={"kind": "media", "media": a.kind, "url": a.result_url, "before": a.source_photo_url})
        else:
            await finished(owner_id, f"media:{asset_id}", False, f"That didn't work: {a.error_message or 'please try again.'}", "/studio/media", "Open Media Lab")
    except Exception:  # noqa: BLE001
        logger.exception("AI helper media notice failed", extra={"asset_id": asset_id})


def busy_conversations(user: User) -> set[str]:
    """Chats a fresh start must not tidy away yet: one waiting on a background job, one with news."""
    st = user.setup_state or {}
    watched = {w.get("conversation_id") for w in (st.get(WATCH_KEY) or {}).values() if isinstance(w, dict)}
    return {c for c in watched | set(st.get(UNREAD_KEY) or []) if c}


def unread(user: User) -> list[str]:
    return list((user.setup_state or {}).get(UNREAD_KEY) or [])


def news(user: User) -> list[dict]:
    """Finished jobs they haven't been told about yet."""
    return list((user.setup_state or {}).get(NEWS_KEY) or [])


async def mark_news_seen(db: AsyncSession, user: User, ids: list[str]) -> None:
    await db.refresh(user, ["setup_state"], with_for_update=True)
    st = _state(user)
    left = [n for n in (st.get(NEWS_KEY) or []) if n.get("id") not in set(ids)]
    if len(left) != len(st.get(NEWS_KEY) or []):
        st[NEWS_KEY] = left
        user.setup_state = st
        flag_modified(user, "setup_state")
    await db.commit()


async def mark_read(db: AsyncSession, user: User, conversation_id: str) -> list[str]:
    await db.refresh(user, ["setup_state"], with_for_update=True)
    st = _state(user)
    left = [c for c in (st.get(UNREAD_KEY) or []) if c != conversation_id]
    if left != (st.get(UNREAD_KEY) or []):
        st[UNREAD_KEY] = left
        user.setup_state = st
        flag_modified(user, "setup_state")
    await db.commit()   # also lets go of the row lock when nothing changed
    return left
