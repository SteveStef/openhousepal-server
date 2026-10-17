"""Agent Hub "My Videos" that keep themselves current.

When an agent links a YouTube channel (config.videoChannelUrl) and leaves "Auto-update from
channel" on (config.videoAutoSync - on unless explicitly turned off), the hub re-reads the channel
once a day and puts new uploads first. Videos the agent added by hand (ids that don't come from a
channel load) always stay, after the channel's videos.

Runs from the 6-hourly scheduler job (each hub refreshes at most once per 24h) and, in the
background, when a public hub with stale videos is opened - never blocking the page itself.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.database import AsyncSessionLocal
from app.models.database import AgentHub

logger = logging.getLogger(__name__)

SYNC_EVERY = timedelta(hours=24)
_in_flight: set[str] = set()


def channel_url(config: Optional[dict]) -> str:
    """The agent's YouTube channel: the link in Agent Hub -> Videos, else the YouTube link they gave
    in setup / Social Links (user, 10/17: "we ask for the channel in sign-up - pull it from there")
    when it's a channel address (youtube.com/@name, /channel/..., /c/..., /user/...)."""
    cfg = config or {}
    url = str(cfg.get("videoChannelUrl") or "").strip()
    if url:
        return url
    for link in cfg.get("socialLinks") or []:
        if isinstance(link, dict) and link.get("id") == "youtube":
            u = str(link.get("url") or "").strip()
            if "youtube.com/" in u and any(k in u for k in ("/@", "/channel/", "/c/", "/user/")):
                return u if u.startswith("http") else f"https://{u.lstrip('/')}"
    return ""


def wants_sync(config: Optional[dict], force: bool = False) -> bool:
    cfg = config or {}
    if cfg.get("videoAutoSync") is False or not channel_url(cfg):
        return False
    if force:
        return True
    last = cfg.get("videoSyncedAt")
    try:
        return not last or datetime.now(timezone.utc) - datetime.fromisoformat(str(last)) >= SYNC_EVERY
    except ValueError:
        return True


async def fetch_channel_videos(channel_url: str) -> list[dict]:
    """Same two-step read the editor's "Load Recent Videos" button uses (the channel's Videos tab,
    falling back to its RSS feed)."""
    from app.api.agent_hub_routes import _fetch_channel_grid_videos, _fetch_recent_youtube_videos, _resolve_channel_id
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        videos = await _fetch_channel_grid_videos(client, channel_url)
        if not videos:
            channel_id = await _resolve_channel_id(client, channel_url)
            if channel_id:
                videos = await _fetch_recent_youtube_videos(client, f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    return videos


def merge_videos(existing: list, fetched: list[dict]) -> list[dict]:
    """The agent's starred favourites first (their order, `pinned: true`), then the channel's videos
    (newest first, as YouTube lists them), then the agent's other hand-added ones."""
    existing = [v for v in (existing or []) if isinstance(v, dict)]
    fresh = {v.get("link"): v for v in fetched}
    # A favourite that's also on the channel keeps its star but picks up the channel's latest title/thumbnail.
    pins = [{**fresh[v.get("link")], "pinned": True} if v.get("link") in fresh else v for v in existing if v.get("pinned")]
    pinned_links = {v.get("link") for v in pins}
    fetched_links = set(fresh)
    manual = [
        v for v in existing
        if not v.get("pinned") and not str(v.get("id") or "").startswith("youtube-") and v.get("link") not in fetched_links
    ]
    return pins + [v for v in fetched if v.get("link") not in pinned_links] + manual


async def sync_hub_videos(db, hub: AgentHub, force: bool = False) -> Optional[int]:
    """Refreshes one hub's videos. Returns how many new videos were added (None when skipped or
    the channel couldn't be read - the saved list is left exactly as it was)."""
    cfg = dict(hub.config or {})
    if not wants_sync(cfg, force):
        return None
    try:
        fetched = await fetch_channel_videos(channel_url(cfg))
    except Exception as exc:  # noqa: BLE001 - YouTube being unreachable must never break a hub
        logger.warning("agent hub video sync failed", extra={"hub_id": hub.id, "error": str(exc)})
        return None
    now = datetime.now(timezone.utc).isoformat()
    if not fetched:
        cfg["videoSyncedAt"] = now  # try again tomorrow, keep what's there
        hub.config = cfg
        flag_modified(hub, "config")
        await db.commit()
        return None
    before = {v.get("link") for v in (cfg.get("videoLinks") or []) if isinstance(v, dict)}
    cfg["videoLinks"] = merge_videos(cfg.get("videoLinks") or [], fetched)
    cfg["videoSyncedAt"] = now
    hub.config = cfg
    flag_modified(hub, "config")
    await db.commit()
    return sum(1 for v in fetched if v.get("link") not in before)


def refresh_in_background(hub_id: str) -> None:
    """Fire-and-forget refresh (own DB session) for a hub whose videos are stale."""
    if hub_id in _in_flight:
        return
    _in_flight.add(hub_id)

    async def run():
        try:
            async with AsyncSessionLocal() as db:
                hub = await db.get(AgentHub, hub_id)
                if hub is not None:
                    await sync_hub_videos(db, hub)
        except Exception:  # noqa: BLE001
            logger.warning("background agent hub video sync failed", exc_info=True)
        finally:
            _in_flight.discard(hub_id)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        _in_flight.discard(hub_id)


async def scheduled_hub_video_sync() -> None:
    """Every hub with a linked channel and auto-update on, at most once a day each."""
    try:
        async with AsyncSessionLocal() as db:
            hubs = (await db.execute(select(AgentHub))).scalars().all()
            refreshed = 0
            for hub in hubs:
                if not wants_sync(hub.config):
                    continue
                added = await sync_hub_videos(db, hub)
                refreshed += 1
                if added:
                    logger.info("agent hub videos updated", extra={"hub_id": hub.id, "added": added})
            if refreshed:
                logger.info("agent hub video sync completed", extra={"event": "hub_video_sync", "hubs": refreshed})
    except Exception:  # noqa: BLE001
        logger.error("agent hub video sync job failed", exc_info=True)
