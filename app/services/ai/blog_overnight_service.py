"""Save usage mode - the same Research -> Write -> Fact-check run at about half the AI usage (owner, 11/14).

Agents see it as "Save usage" (the default way to write an article; "Write it now" is the faster, dearer one).
It was first built as "overnight", and the code still says overnight: the first live run took 12.5 minutes, not a
night, so the owner renamed it and made it the default ("the same product made slower so it's cheaper").

How: Anthropic's Message Batches queue takes the very same requests the writer sends now, answers them later
(usually within the hour, at most a day) and charges half price for every token. Nothing about the article
changes - same models, same instructions, same checks. Only the waiting does.

The run is ai_blog_pipeline_service.run_pipeline with one thing swapped: how a request is sent
(pipeline.TRANSPORT -> BatchTransport.send = submit a one-request batch, wait for it, hand back the answer).

It has to survive a restart, because a run can last hours and the server is redeployed often:
  - every batch id is written to the article's row the moment it is submitted (ai_review["overnight"]["calls"],
    keyed by a fingerprint of the request), so a picked-up run asks Anthropic for the answer it already paid for
    instead of asking - and paying - again;
  - the usage events each step wrote are kept there too (["events"]): a step is never charged twice, and a run
    that fails gives back exactly what it charged, however long ago;
  - the process doing the run checks in every minute (["lease_at"], ["worker"]). `watch()` runs in every copy of
    the API and picks up a run whose owner has gone quiet for four minutes.

The agent is told when it is ready by the bell AND an email, every time - they chose to walk away.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

WORKER = uuid.uuid4().hex[:12]          # this process
LEASE_SECONDS = 60                      # how often the running process checks in
ORPHAN_SECONDS = 240                    # quiet for this long = its process is gone; another one picks the run up
DEADLINE_HOURS = 20                     # a run not finished by then is stopped and refunded
MAX_PICKUPS = 12                        # a run that keeps dying is stopped, not retried forever
TYPICAL_MINUTES = 20                    # "likely ready by" until three real runs can be measured (blog_routes._slow_minutes)
# The time promise (owner 11/14: "if they take more than 30 minutes, it gets all passed to the fast version"): an
# article still waiting in the slower line after this many minutes is finished the fast way on its own. The agent pays
# what each step really used (owner, later that night: "the amount that they actually use") - half price for the steps
# the slower line answered, the normal price for the ones finished fast - so every screen states the range up front:
# the Save usage amount at best, the "Write it now" amount at worst. A capped article still costs less than one
# written fast from the start, because the steps already done stay at half price.
CAP_MINUTES = 60                        # owner 11/15: an hour in the slower line before anything is finished at full price
                                        # (30, then 25 earlier that day); config "blog_save_usage_cap_minutes" overrides
FAST_FINISH_MINUTES = 10                # what the steps still to come take the fast way, at the outside
EMAIL_TEMPLATES = {"blog_article_ready", "blog_article_failed"}

SLOW_ERROR = ("Save usage mode didn't finish this article in time, so we stopped it. Nothing was charged and your answers are "
              "saved - open it to try again.")

_RUNNING: set[str] = set()              # article ids this process is running right now
_TASKS: set = set()


class Superseded(BaseException):
    """This process no longer owns the run (given up, replaced, deleted or picked up elsewhere) - stop quietly.
    Not an Exception on purpose: the pipeline carries on past an ordinary error in its research or checking
    steps, and this must stop the whole run."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def is_overnight(review: Optional[dict]) -> bool:
    return (review or {}).get("mode") == "overnight"


def new_state(brief: dict, config: Optional[dict] = None, minutes: Optional[int] = None) -> dict:
    """What an overnight run keeps on its article row. `brief` is stored exactly as the run reads it, so a
    picked-up run builds the very same requests (and so finds its own batches again)."""
    now = datetime.now(timezone.utc)
    minutes = int(minutes or (config or {}).get("blog_overnight_typical_minutes", TYPICAL_MINUTES))
    cap = cap_minutes(config)
    return {"worker": WORKER, "lease_at": now.isoformat(), "calls": {}, "events": {}, "pickups": 0,
            "brief": json.loads(json.dumps(brief, default=str)),
            "likely_at": (now + timedelta(minutes=min(minutes, cap + FAST_FINISH_MINUTES))).isoformat(),
            "cap_at": (now + timedelta(minutes=cap)).isoformat(), "cap_minutes": cap,
            "latest_at": (now + timedelta(hours=DEADLINE_HOURS)).isoformat()}


def cap_minutes(config: Optional[dict] = None) -> int:
    return max(5, int((config or {}).get("blog_save_usage_cap_minutes", CAP_MINUTES)))


def times(config: Optional[dict] = None, minutes: Optional[int] = None) -> dict:
    """For the "Save usage" choice, before anything starts."""
    now = datetime.now(timezone.utc)
    minutes = int(minutes or (config or {}).get("blog_overnight_typical_minutes", TYPICAL_MINUTES))
    cap = cap_minutes(config)
    minutes = min(minutes, cap + FAST_FINISH_MINUTES)
    return {"typical_minutes": minutes, "likely_at": (now + timedelta(minutes=minutes)).isoformat(),
            "cap_minutes": cap, "max_minutes": cap + FAST_FINISH_MINUTES,
            "latest_at": (now + timedelta(hours=DEADLINE_HOURS)).isoformat()}


def is_stale(review: dict) -> bool:
    """Past its deadline with nobody finishing it (the watcher normally ends such a run itself)."""
    latest = _parse(((review or {}).get("overnight") or {}).get("latest_at")) or _parse((review or {}).get("started_at"))
    return latest is None or datetime.now(timezone.utc) > latest + timedelta(minutes=30)


def public_state(review: dict) -> dict:
    """What the screens may know about a waiting run."""
    ov = (review or {}).get("overnight") or {}
    return {"mode": "overnight", "likely_at": ov.get("likely_at"), "latest_at": ov.get("latest_at"),
            "steps_done": sum(1 for c in (ov.get("calls") or {}).values() if c.get("done")),
            "capped": bool(ov.get("capped")),
            "cap_minutes": ov.get("cap_minutes"), "max_minutes": (ov.get("cap_minutes") or CAP_MINUTES) + FAST_FINISH_MINUTES}


async def refund_events(db, ids: list, reason: str = "refunded_failed_run") -> None:
    """Give back exactly these usage events (they stay in the log, marked, and drop off the agent's bar)."""
    from sqlalchemy import update
    from app.models.database import AIUsageEvent
    ids = [i for i in ids if i]
    if not ids:
        return
    try:
        await db.rollback()
        await db.execute(update(AIUsageEvent).where(AIUsageEvent.id.in_(ids), AIUsageEvent.blocked.is_(False))
                         .values(blocked=True, block_reason=reason))
        await db.commit()
    except Exception:
        logger.warning("overnight article: refund failed", exc_info=True)
        await db.rollback()


class Journal:
    """The run's memory on its article row - see the module note. `save` writes the row and raises Superseded
    when the run is no longer this process's."""

    def __init__(self, state: dict, save: Callable[[], Awaitable[None]], resumed: bool = False):
        self.state, self.save, self.resumed = state, save, resumed
        state.setdefault("calls", {})
        state.setdefault("events", {})
        self._beat = time.monotonic()

    def event_ids(self) -> list:
        return [i for ids in (self.state.get("events") or {}).values() for i in ids]

    async def charged(self, db, stage: str, event) -> None:
        """A step wrote a usage event. If the same step was charged before a restart and had to be asked again
        (its request changed - new MLS numbers, a new date), the first charge goes back: one charge per step."""
        earlier = list((self.state["events"].get(stage) or []))
        if earlier:
            await refund_events(db, earlier, "replaced_after_restart")
        self.state["events"][stage] = [event.id] if event is not None and getattr(event, "id", None) else []
        await self.save()

    async def heartbeat(self) -> None:
        if time.monotonic() - self._beat >= LEASE_SECONDS:
            self._beat = time.monotonic()
            self.state["lease_at"] = now_iso()
            await self.save()

    def past_deadline(self) -> bool:
        latest = _parse(self.state.get("latest_at"))
        return latest is not None and datetime.now(timezone.utc) > latest

    def past_cap(self) -> bool:
        """The time we promised is up: what is left is done the fast way, at no extra usage to the agent."""
        cap = _parse(self.state.get("cap_at"))
        return cap is not None and datetime.now(timezone.utc) > cap


def _plain(o: Any) -> Any:
    """Request parameters as plain JSON. An earlier answer's content blocks (sent back when a turn continues) are
    turned into dicts the same way the SDK does it for a normal request."""
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json", exclude_unset=True)
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    return o


def _no_cache_marks(o: Any) -> Any:
    """A five-minute cache is no use to a request answered an hour later - and writing one costs extra."""
    if isinstance(o, dict):
        return {k: _no_cache_marks(v) for k, v in o.items() if k != "cache_control"}
    if isinstance(o, list):
        return [_no_cache_marks(v) for v in o]
    return o


def _reads_the_web(tools) -> bool:
    return any(str((t or {}).get("type") or "").startswith(("web_search", "web_fetch")) for t in tools or [] if isinstance(t, dict))


def batch_params(kwargs: dict) -> dict:
    """The request as the batch queue takes it: the same request, `extra_body` folded in. Cache marks come off -
    except on the research request. That one request reads the web in many rounds, re-reading everything it has
    found so far each round; with its cache mark those re-reads cost a twentieth, and that stacks with the queue's
    half price (measured 11/14: without it a heavy research step would cost MORE in the queue than written now)."""
    kw = {k: v for k, v in kwargs.items() if k != "extra_body"}
    kw.update(kwargs.get("extra_body") or {})
    kw = _plain(kw)
    if not _reads_the_web(kw.get("tools")):
        kw["system"] = _no_cache_marks(kw.get("system"))
        kw["tools"] = _no_cache_marks(kw.get("tools"))
    return kw


def request_key(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:40]


def _wait_seconds(elapsed: float) -> float:
    return 15 if elapsed < 180 else (30 if elapsed < 900 else 60)


class BatchTransport:
    """pipeline.TRANSPORT for one overnight run."""

    def __init__(self, journal: Journal, db=None, sleep=asyncio.sleep):
        self.journal, self.db, self.sleep = journal, db, sleep

    def _capped(self) -> bool:
        """The time we promised is up: every step still to come is sent the normal way."""
        if not self.journal.past_cap():
            return False
        if not self.journal.state.get("capped"):
            self.journal.state["capped"] = True
            logger.info("save usage article: time cap reached - finishing the fast way")
        return True

    @staticmethod
    async def _now(client, kwargs: dict):
        """Send this step the normal way (after the time cap). It is charged as what it is: the normal amount."""
        async with client.messages.stream(**kwargs) as stream:
            return await stream.get_final_message()

    async def send(self, client, kwargs: dict, usage: dict):
        from app.services.ai.ai_blog_pipeline_service import _add_usage, _new_usage
        params = batch_params(kwargs)
        key = request_key(params)
        calls = self.journal.state["calls"]
        entry = calls.get(key)
        answered_before = bool(entry and entry.get("done"))
        if entry is None and self._capped():
            return await self._now(client, kwargs)
        if entry is None:
            batch = await client.messages.batches.create(requests=[{"custom_id": key, "params": params}])
            entry = calls[key] = {"batch": batch.id, "at": now_iso()}
            await self.journal.save()
            logger.info("overnight article: batch submitted", extra={"batch_id": batch.id, "model": params.get("model")})
        if self.db is not None:
            await self.db.commit()   # nothing sits on a database connection while the queue works
        message = await self._wait(client, entry["batch"])
        if message is None:
            # The time cap passed while this step waited in the slower line: it is sent the normal way instead.
            calls.pop(key, None)
            await self.journal.save()
            return await self._now(client, kwargs)
        if not entry.get("done"):
            entry["done"] = True
            entry["done_at"] = now_iso()
            await self.journal.save()
        # THIS answer came through the slower line: half price. Kept as its own tally, so a step that was partly
        # answered there and partly finished fast is charged exactly - each answer at the price it really had.
        _add_usage(usage.setdefault("batched", _new_usage()), message)
        if answered_before:
            # Charged when it was first answered (before a restart) - counted for the receipt, not charged again.
            _add_usage(usage.setdefault("replayed", _new_usage()), message)
        return message

    async def _wait(self, client, batch_id: str):
        import anthropic
        began = time.monotonic()
        while True:
            try:
                batch = await client.messages.batches.retrieve(batch_id)
            except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError):
                logger.warning("overnight article: could not ask about batch %s - trying again", batch_id, exc_info=True)
                batch = None
            if batch is not None and batch.processing_status == "ended":
                break
            if self.journal.past_deadline():
                try:
                    await client.messages.batches.cancel(batch_id)
                except Exception:  # noqa: BLE001 - it expires on its own
                    pass
                raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, SLOW_ERROR)
            if self._capped():
                try:
                    await client.messages.batches.cancel(batch_id)
                except Exception:  # noqa: BLE001 - an answer that still arrives is simply never read
                    pass
                return None
            await self.journal.heartbeat()
            await self.sleep(_wait_seconds(time.monotonic() - began))
        async for row in await client.messages.batches.results(batch_id):
            result = row.result
            if result.type == "succeeded":
                return result.message
            if result.type == "errored":
                err = getattr(getattr(result, "error", None), "error", None)
                kind, text = getattr(err, "type", "") or "", getattr(err, "message", "") or "the request failed"
                if kind == "invalid_request_error":
                    # The research step falls back to the other web tools on exactly this error (see _research_call).
                    import httpx
                    raise anthropic.BadRequestError(text, response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")), body=None)
                logger.warning("overnight article: batch %s errored: %s %s", batch_id, kind, text)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer hit a problem - please try again. Nothing was charged for this try.")
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, SLOW_ERROR)   # expired or canceled - not billed
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer returned nothing - please try again. Nothing was charged for this try.")


# ---------------------------------------------------------------------------
# Picking a run up again after a restart
# ---------------------------------------------------------------------------

def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def pick_up_orphans() -> int:
    """Overnight runs whose process has gone quiet: claim each (one process wins) and carry on. Returns how many
    this process took."""
    from sqlalchemy import select, update
    from app.database import AsyncSessionLocal
    from app.models.database import Blog
    took = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Blog.id, Blog.owner_id, Blog.ai_review).where(
            Blog.ai_review["status"].astext == "running", Blog.ai_review["mode"].astext == "overnight"))).all()
        for blog_id, owner_id, review in rows:
            ov = dict((review or {}).get("overnight") or {})
            if blog_id in _RUNNING:
                continue
            lease = _parse(ov.get("lease_at"))
            if lease is not None and (datetime.now(timezone.utc) - lease).total_seconds() < ORPHAN_SECONDS:
                continue
            latest = _parse(ov.get("latest_at"))
            give_up = (latest is not None and datetime.now(timezone.utc) > latest) or int(ov.get("pickups") or 0) >= MAX_PICKUPS
            claim = {**review, "overnight": {**ov, "worker": WORKER, "lease_at": now_iso(), "pickups": int(ov.get("pickups") or 0) + 1}}
            won = await db.execute(update(Blog).where(
                Blog.id == blog_id, Blog.ai_review["status"].astext == "running",
                Blog.ai_review["started_at"].astext == str(review.get("started_at")),
                Blog.ai_review["overnight"]["lease_at"].astext == str(ov.get("lease_at"))).values(ai_review=claim))
            await db.commit()
            if not won.rowcount:
                continue
            took += 1
            from app.api.blog_routes import _finish_overnight_failed, _run_blog_ai_job
            if give_up:
                logger.warning("overnight article given up", extra={"blog_id": blog_id, "pickups": ov.get("pickups")})
                spawn(_finish_overnight_failed(blog_id, owner_id, SLOW_ERROR))
            else:
                logger.info("overnight article picked up after a restart", extra={"blog_id": blog_id, "pickups": claim["overnight"]["pickups"]})
                spawn(_run_blog_ai_job(blog_id, owner_id, ov.get("brief") or review.get("brief") or {}, review.get("engine") or "max",
                                       overnight=True, resume=True))
    return took


async def watch() -> None:
    """Started once per API process (main.py lifespan)."""
    await asyncio.sleep(25)
    while True:
        try:
            await pick_up_orphans()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next minute tries again
            logger.warning("overnight article watcher failed", exc_info=True)
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# The "it's ready" email (rendered here - the Mailgun domain is at its template cap)
# ---------------------------------------------------------------------------

def render_email(template: str, subject: str, v: dict) -> tuple[str, str, str]:
    from app.services.invite_email_service import _settings_footer, button, eyebrow, h1, ohp_header, para, shell, e
    title = v.get("article_title") or "your article"
    link = v.get("link") or ""
    if template == "blog_article_failed":
        body = [eyebrow("AI Blog Writer"), h1("We couldn't finish your article"),
                para(f"Hi {e(v.get('first_name') or 'there')}, the article you left us to write, &ldquo;{e(title)}&rdquo;, didn't finish."),
                para(e(v.get("reason") or "Nothing was charged and your answers are saved."), muted=True),
                button("Open it and try again", link)]
        text = [f"We couldn't finish \"{title}\".", v.get("reason") or "Nothing was charged and your answers are saved.", f"Open it: {link}"]
        pre = "Nothing was charged - your answers are saved."
    else:
        used = v.get("used_line") or ""
        body = [eyebrow("AI Blog Writer"), h1("Your article is ready"),
                para(f"Hi {e(v.get('first_name') or 'there')}, &ldquo;{e(title)}&rdquo; was researched, written and fact-checked while you were away."),
                para("It is a draft - nothing is public until you publish it. Give it a read, pick its look and publish when you're happy.")]
        if used:
            body.append(para(e(used), muted=True))
        body.append(button("Read your article", link))
        text = [f"Your article is ready: \"{title}\".", "It is a draft - nothing is public until you publish it.", used, f"Read it: {link}"]
        pre = "Researched, written and fact-checked while you were away."
    html = shell(title=subject, preheader=pre, header_cells=ohp_header("Blog"), body="".join(body), footer=_settings_footer(v.get("app_url") or ""))
    return subject, html, "\n".join(t for t in text if t)
