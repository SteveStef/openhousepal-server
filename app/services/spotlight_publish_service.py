"""Listing Spotlight: "publish as soon as the setup is done" (user, 10/24).

The editor's first-open setup (highlights, polished story, "About the town") runs in the agent's
browser. If the agent presses Publish while some of it is still working, the page is marked
`publish_when_ready` on its draft and the server takes over:

1. For up to WAIT_FOR_EDITOR seconds it waits for the editor to finish and save
   (draft auto_setup_done) - then publishes exactly what the agent saw.
2. If the editor never finishes (the agent closed the tab / left the page), the server writes the
   missing pieces itself - the same copywriter purposes the editor uses, on the agent's own monthly
   AI allowance (ai_copywrite_service.generate does the guardrails) - and publishes.

A server restart mid-wait is covered by sweep_pending (task_reminder_scheduler, every minute).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.database import PropertyWebsite, User

logger = get_logger(__name__)

PENDING_KEY = "publish_when_ready"
WAIT_FOR_EDITOR = 90  # seconds
_tasks: set = set()


def _tidy(value: Optional[str]) -> str:
    v = (value or "").strip()
    if not v or any(c.islower() for c in v):
        return v
    return " ".join(w.capitalize() for w in v.lower().split())


def pending_since(site: PropertyWebsite) -> Optional[datetime]:
    raw = ((site.draft_data or {}).get(PENDING_KEY) or {}).get("at")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


async def request_publish(db, site: PropertyWebsite) -> PropertyWebsite:
    """Mark the page "publish when ready" and start watching it. Already set up = publish now."""
    await db.refresh(site, with_for_update=True)  # the editor's autosave may have just landed
    draft = dict(site.draft_data or {})
    if draft.get("auto_setup_done"):
        return await _publish(db, site)
    draft[PENDING_KEY] = {"at": datetime.now(timezone.utc).isoformat()}
    site.draft_data = draft
    await db.commit()
    await db.refresh(site)
    task = asyncio.create_task(_watch(site.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return site


async def cancel(db, site: PropertyWebsite) -> PropertyWebsite:
    await db.refresh(site, with_for_update=True)
    draft = dict(site.draft_data or {})
    if draft.pop(PENDING_KEY, None) is not None:
        site.draft_data = draft
        await db.commit()
        await db.refresh(site)
    return site


async def _publish(db, site: PropertyWebsite) -> PropertyWebsite:
    from app.services import property_website_service as pws
    await db.refresh(site, with_for_update=True)
    draft = dict(site.draft_data or {})
    draft.pop(PENDING_KEY, None)
    site.draft_data = draft
    return await pws.publish(db, site)


async def _watch(site_id: str) -> None:
    """Wait for the editor to finish the setup, else finish it here - then publish."""
    try:
        waited = 0
        while waited < WAIT_FOR_EDITOR:
            await asyncio.sleep(3)
            waited += 3
            async with AsyncSessionLocal() as db:
                site = await db.get(PropertyWebsite, site_id)
                if site is None or pending_since(site) is None:
                    return  # deleted, cancelled or already published
                if (site.draft_data or {}).get("auto_setup_done"):
                    await _publish(db, site)
                    logger.info("Spotlight published after the editor's setup", extra={"site_id": site_id})
                    return
        async with AsyncSessionLocal() as db:
            site = await db.get(PropertyWebsite, site_id)
            if site is not None and pending_since(site) is not None:
                await finish_on_server(db, site)
    except Exception:  # noqa: BLE001 - the scheduler sweep tries again
        logger.warning("Spotlight publish-when-ready watch failed", exc_info=True, extra={"site_id": site_id})


async def finish_on_server(db, site: PropertyWebsite) -> PropertyWebsite:
    """Write whatever the editor's setup didn't get to, then publish. Each AI piece is optional -
    one that fails (allowance used up, provider down) just stays empty, the page still goes live."""
    from app.services import property_website_service as pws
    from app.services.ai import ai_copywrite_service
    owner = await db.get(User, site.owner_id)
    draft = dict(site.draft_data or {})
    snap = site.property_snapshot or {}
    patch: dict = {}
    description = str(draft.get("description_override") or snap.get("description") or "").strip()

    async def write(purpose: str, context: str) -> str:
        if owner is None or not context.strip():
            return ""
        try:
            out = await ai_copywrite_service.generate(db, owner.id, owner.plan_tier, purpose=purpose, context=context)
            return str(out.get("text") or "").strip()
        except Exception:  # noqa: BLE001
            logger.info("Spotlight server setup: %s skipped", purpose, extra={"site_id": site.id})
            return ""

    if not draft.get("highlights") and description:
        text = await write("property_site_highlights", description)
        lines = [ln.lstrip(" -*•\t").strip() for ln in text.splitlines()]
        if lines := [ln for ln in lines if ln][:8]:
            patch["highlights"] = lines

    town = draft.get("town_story") or {}
    city = _tidy(snap.get("city"))
    if not (town.get("body") or town.get("facts")) and city:
        parts = [f"Town: {city}{', ' + snap['state'] if snap.get('state') else ''}"]
        if snap.get("county"):
            parts.append(f"County: {_tidy(str(snap['county']).split('-')[0])}")
        if snap.get("township"):
            parts.append(f"Township: {_tidy(snap['township'])}")
        text = await write("property_site_town_facts", "\n".join(parts))
        facts = None
        try:
            facts = json.loads(text[text.index("{"): text.rindex("}") + 1]) if "{" in text else None
        except (ValueError, json.JSONDecodeError):
            facts = None
        if isinstance(facts, dict):
            patch["town_story"] = {"name": town.get("name") or city, "body": "", "hidden": bool(town.get("hidden")),
                                   "facts": facts, "photo": town.get("photo"), "photo_off": bool(town.get("photo_off"))}

    patch["auto_setup_done"] = True
    site = await pws.update_draft(db, site, patch)
    site = await _publish(db, site)
    logger.info("Spotlight setup finished on the server and published", extra={"site_id": site.id, "filled": [k for k in patch if k != "auto_setup_done"]})
    return site


async def sweep_pending(db) -> int:
    """Pages still waiting a few minutes after Publish (the server restarted while it waited):
    finish them now. Runs every minute from task_reminder_scheduler."""
    rows = (await db.execute(
        select(PropertyWebsite).where(PropertyWebsite.draft_data.has_key(PENDING_KEY))
    )).scalars().all()
    done = 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WAIT_FOR_EDITOR + 60)
    for site in rows:
        since = pending_since(site)
        if since is None or since > cutoff:
            continue
        try:
            if (site.draft_data or {}).get("auto_setup_done"):
                await _publish(db, site)
            else:
                await finish_on_server(db, site)
            done += 1
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.warning("Spotlight pending publish failed", exc_info=True, extra={"site_id": site.id})
    return done
