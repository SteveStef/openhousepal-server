from fastapi import FastAPI, Request  # , Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import uvicorn
import os
import re
import time
import uuid
from dotenv import load_dotenv
from app.database import init_db, wait_for_db
from app.api import router
from app.utils.clean_cache import cleanup_expired_signup_verifications
from app.utils.property_sync_scheduler import scheduled_bright_sync
from app.utils.domain_verification_scheduler import scheduled_domain_health, scheduled_domain_verification
from app.utils.studio_media_video_poll_scheduler import scheduled_studio_media_video_poll
from app.utils.task_reminder_scheduler import scheduled_task_reminders
from app.services.calendar_sync_service import retitle_done_events, scheduled_finish_past_tours, scheduled_reconcile_deleted_followups
from app.utils.state_facts_scheduler import scheduled_state_facts_refresh
from app.services.agent_hub_video_sync import scheduled_hub_video_sync
from app.services.email_scheduler_service import EmailSchedulerService
from app.utils.create_admin import create_admin_user
from app.config.logging import (
    configure_logging,
    get_logger,
    set_request_id,
    clear_request_id,
)

load_dotenv()

CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000")

# Configure logging
configure_logging()
logger = get_logger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - handles startup and shutdown"""

    await wait_for_db()
    await init_db()
    await create_admin_user()

    # Google Maps calls stop at the free monthly allowance and wait for the 1st (owner, 10/26).
    from app.services import google_free_tier
    google_free_tier.install()

    # Emails sent on worker threads hand their Insights rows back to this loop (email_log_service).
    from app.services import email_log_service
    email_log_service.attach_loop()

    # The town / IDX market numbers saved before this restart go straight back into memory, so the
    # first visitor after a deploy never waits while they're rebuilt (app/services/app_cache.py).
    try:
        from app.services.website.content_adapters import preload_persistent_caches
        logger.info(f"Warm caches loaded from the last run: {await preload_persistent_caches()}")
    except Exception:
        logger.warning("warm cache preload failed", exc_info=True)

    # A plan with no PayPal ID can't be bought or switched to - say so loudly at boot instead
    # of an agent finding out at checkout.
    from app.utils.plan_ids import CURRENT_NO_TRIAL_PLAN_ID_BY_TIER, _PLAN_ID_ENV_PAIRS_BY_TIER
    missing = [tier for tier, pid in CURRENT_NO_TRIAL_PLAN_ID_BY_TIER.items() if not pid]
    missing += [pairs[-1][0] for tier, pairs in _PLAN_ID_ENV_PAIRS_BY_TIER.items() if not os.getenv(pairs[-1][0])]
    if missing:
        logger.error(f"PayPal plan IDs missing - these plans can't be bought or switched to: {sorted(set(missing))}")

    logger.info("Initializing APScheduler for scheduled tasks")

    cache_hour = int(os.getenv("CACHE_CLEANUP_HOUR", 2))
    cache_mins = int(os.getenv("CACHE_CLEANUP_MINUTE", 0))

    scheduler.add_job(
        cleanup_expired_signup_verifications,
        CronTrigger(hour=cache_hour, minute=cache_mins),  # Daily at 2:00 AM
        id="cleanup_signup_verifications",
        name="Clean up expired signup verifications",
        replace_existing=True,
    )

    # The Bright sync - every 5 minutes, one cycle: changed listings + deletions, open house dates,
    # sold prices (each asks Bright only for what changed since its own checkpoint).
    scheduler.add_job(
        scheduled_bright_sync,
        CronTrigger(minute="*/5"),
        id="property_sync",
        name="Bright MLS sync: listings, open houses, sold prices",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Schedule email processing every minute
    scheduler.add_job(
        EmailSchedulerService.process_due_emails,
        "interval",
        minutes=1,
        id="email_processing",
        name="Process scheduled emails",
        replace_existing=True,
    )

    # Production Bright data pulls (3 years of sales for CMAs, a one-time refresh of every listing
    # on the market, and each home's 2012+ price history) - a few pages every 10 minutes until each
    # is done. Off unless BRIGHT_DATA_PULLS is set (see bright_data_pulls_service). Price history
    # is its own job so its slow batches never hold up the sales pull.
    from app.services import bright_data_pulls_service as _pulls
    if _pulls.any_enabled():
        def _pulls_job(names: tuple, label: str):
            async def _job():
                from app.database import AsyncSessionLocal
                try:
                    async with AsyncSessionLocal() as db:
                        await _pulls.run_enabled(db, names)
                except Exception:
                    logger.error(f"{label} job failed", exc_info=True)
            return _job
        # First tick a minute after start - a deploy/restart used to idle the sales pull for 10 minutes.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        scheduler.add_job(
            _pulls_job(_pulls.SALES_PULLS, "Bright data pulls"), "interval", minutes=10, id="bright_data_pulls",
            name="Production Bright data pulls", replace_existing=True, max_instances=1,
            next_run_time=_dt.now(_tz.utc) + _td(seconds=60),
        )
        scheduler.add_job(
            _pulls_job(_pulls.HISTORY_PULLS, "Price history backfill"), "interval", minutes=10, id="bright_price_history",
            name="Production Bright price history backfill", replace_existing=True, max_instances=1,
        )
        scheduler.add_job(
            _pulls_job(_pulls.REPAIR_PULLS, "Off-market recheck"), "interval", minutes=10, id="bright_recheck_off_market",
            name="Production Bright off-market recheck", replace_existing=True, max_instances=1,
        )

    # Website Builder custom domains: real DNS/HTTPS check on a recurring poll, so a domain an
    # agent fixes doesn't require them to come back and click anything - see
    # domain_verification_scheduler.py / domain_verification_service.py.
    scheduler.add_job(
        scheduled_domain_verification,
        "interval",
        minutes=5,
        id="domain_verification",
        name="Verify custom domain DNS/HTTPS status",
        replace_existing=True,
    )
    # Every live IDX website in Google Search Console with its sitemap - new sites, new own
    # domains, retries after a failure. Does nothing until GOOGLE_SEARCH_SERVICE_ACCOUNT_JSON is set.
    from app.services.website.google_search_service import scheduled_google_search_sweep
    scheduler.add_job(
        scheduled_google_search_sweep,
        "interval",
        minutes=15,
        id="google_search_sweep",
        name="Put IDX websites in Google Search Console",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # ...and once connected, is it actually opening? Every 2 hours (new domains each time - the
    # first 4 days - after that once a month). Problems show on the agent's Dash + to the admin.
    scheduler.add_job(
        scheduled_domain_health,
        "interval",
        hours=2,
        id="domain_health",
        name="Check connected custom domains open",
        replace_existing=True,
    )

    # AI video renders take minutes and an agent is explicitly encouraged to navigate away while
    # one runs - without this, a video only ever advances out of "processing" while its own
    # project page happens to be open polling it. See studio_media_video_poll_scheduler.py.
    scheduler.add_job(
        scheduled_studio_media_video_poll,
        "interval",
        minutes=1,
        id="studio_media_video_poll",
        name="Advance in-flight AI video generations",
        replace_existing=True,
    )

    # Agent task / follow-up reminders: fire an in-app Notification when a task's remind_at passes.
    scheduler.add_job(
        scheduled_task_reminders,
        "interval",
        minutes=1,
        id="task_reminders",
        name="Fire agent task reminders",
        replace_existing=True,
    )

    # A follow-up the agent deleted from their Google/Outlook calendar counts as checked off.
    scheduler.add_job(
        scheduled_reconcile_deleted_followups,
        "interval",
        minutes=5,
        id="calendar_deleted_followups",
        name="Mark follow-ups deleted from the calendar as done",
        replace_existing=True,
    )

    # A confirmed tour whose hour has passed gets crossed out on the calendar ("✅ DONE", grey).
    scheduler.add_job(
        scheduled_finish_past_tours,
        "interval",
        minutes=15,
        id="calendar_finish_past_tours",
        name="Cross out past tours on connected calendars",
        replace_existing=True,
    )

    # Once, a few minutes after boot (live only - not the local dev server): done follow-ups and
    # tours from the last 3 weeks get the plain "DONE ·" calendar title (10/19 - the old emoji /
    # cross-out title showed bigger than the open ones on the iPhone).
    if "localhost" not in os.getenv("FRONTEND_URL", "http://localhost:3000"):
        from datetime import datetime as _dt, timedelta as _td
        scheduler.add_job(
            retitle_done_events,
            "date",
            run_date=_dt.now() + _td(minutes=3),
            id="calendar_retitle_done",
            name="Plain DONE titles on checked-off calendar events",
            replace_existing=True,
        )

    # Agent Hub "My Videos": a linked YouTube channel's new uploads appear on their own (each hub
    # refreshes at most once a day - see agent_hub_video_sync.py).
    scheduler.add_job(
        scheduled_hub_video_sync,
        "interval",
        hours=6,
        id="agent_hub_video_sync",
        name="Refresh Agent Hub YouTube videos",
        replace_existing=True,
    )

    # Shared state-wide facts for blog articles (transfer taxes, buyer programs, rules): once a month,
    # re-research any Bright state whose facts have aged out. See state_facts_scheduler.py.
    scheduler.add_job(
        scheduled_state_facts_refresh,
        CronTrigger(day=1, hour=3, minute=0, timezone="America/New_York"),
        id="state_facts_refresh",
        name="Refresh shared state facts",
        replace_existing=True,
    )

    # Shared towns stay current and accurate on their own: school districts re-checked against
    # Bright, Fair Housing wording, closed places removed, commute / flood / research refreshed
    # when due. See app/services/areas/town_upkeep_scheduler.py.
    from app.services.areas.town_upkeep_scheduler import scheduled_town_upkeep
    scheduler.add_job(
        scheduled_town_upkeep,
        CronTrigger(hour=4, minute=15, timezone="America/New_York"),
        id="town_upkeep",
        name="Keep neighborhood data current",
        replace_existing=True,
    )

    # Claude health check: a tiny real request a minute after every start (= every deploy) and every
    # 30 minutes - if the AI library or key breaks, the admin hears about it before an agent does.
    from datetime import datetime as _dt, timedelta as _td
    from apscheduler.triggers.interval import IntervalTrigger as _Interval
    # Backstop for email_log_service: any counted emails that didn't get written straight away.
    scheduler.add_job(email_log_service.flush, _Interval(minutes=1), id="email_log_flush",
                      name="Write counted emails", replace_existing=True, max_instances=1)
    from app.services.ai.ai_health_check import scheduled_ai_health_check
    scheduler.add_job(
        scheduled_ai_health_check,
        _Interval(minutes=30, start_date=_dt.now() + _td(minutes=1)),
        id="ai_health_check",
        name="Claude AI health check",
        replace_existing=True,
    )
    # Town pages: recompute every published site's market numbers right after start (the saved copy
    # above serves visitors meanwhile) - public_site_routes.warm_area_caches.
    from apscheduler.triggers.date import DateTrigger as _Once
    # Finished towns shipped with the code are put on this server once (areas/town_seeds.py).
    from app.services.areas.town_seeds import scheduled_apply_town_seeds
    scheduler.add_job(scheduled_apply_town_seeds, _Once(run_date=_dt.now() + _td(seconds=3)), id="town_seeds", name="Load finished towns", replace_existing=True)
    from app.api.public_site_routes import warm_area_caches
    scheduler.add_job(warm_area_caches, _Once(run_date=_dt.now() + _td(seconds=10)), id="warm_area_caches", name="Warm town-page caches", replace_existing=True)
    # Invites: the agent's "Tomorrow" email (who's coming) - hourly, sent from 9 AM Eastern the day
    # before each day of an invite, once per day (invites_service.send_day_before).
    from app.services.invites_service import scheduled_day_before
    scheduler.add_job(scheduled_day_before, CronTrigger(minute=7), id="invite_day_before",
                      name="Invites: tomorrow's guest list to the agent", replace_existing=True, max_instances=1, coalesce=True)

    # DISABLE_SCHEDULER=true for a second local/test copy of the API, so it never repeats the
    # real server's jobs (MLS sync, reminder emails, digests...).
    if os.getenv("DISABLE_SCHEDULER", "false").lower() == "true":
        logger.warning("Scheduler disabled (DISABLE_SCHEDULER=true) - no scheduled jobs will run in this process")
    else:
        scheduler.start()
    logger.info(
        "APScheduler started",
        extra={
            "cache_cleanup_schedule": f"Daily at {cache_hour:02d}:{cache_mins:02d}",
            "property_sync_interval": "Every 5 minutes",
            "email_processing_interval": "Every 1 minute",
        },
    )

    yield

    # Shutdown
    logger.info("Shutting down application")
    if scheduler.running:
        scheduler.shutdown()
    logger.info("APScheduler stopped")


app = FastAPI(title="Open House Pal API", lifespan=lifespan)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Log HTTP requests and responses with unique request IDs."""
    # Generate unique request ID
    request_id = str(uuid.uuid4())
    set_request_id(request_id)

    # Extract user_id from JWT token if present
    user_id = None
    try:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            from jose import jwt
            from app.auth.dependencies import SECRET_KEY, ALGORITHM

            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id = payload.get("sub")
    except Exception:
        # If token decode fails, just continue without user_id
        pass

    # Process request and measure duration
    start_time = time.time()
    try:
        response = await call_next(request)
        duration_ms = (time.time() - start_time) * 1000

        # Determine log level based on status code
        if response.status_code < 400:
            log_level = logger.info
        elif response.status_code < 500:
            log_level = logger.warning
        else:
            log_level = logger.error

        log_level(
            "Request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                # "query_params": str(request.url.query) if request.url.query else None,
                # "client_host": request.client.host if request.client else None,
                "user_id": user_id,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )

        return response
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            "Request failed with exception",
            exc_info=True,
            extra={
                "method": request.method,
                "path": request.url.path,
                "user_id": user_id,
                "duration_ms": round(duration_ms, 2),
                "error": str(e),
            },
        )
        raise
    finally:
        clear_request_id()


class CrashToJsonMiddleware:
    """Turns an unexpected crash into a normal 500 reply INSIDE the CORS layer (added just before it,
    so it sits inside it). Starlette runs @app.exception_handler(Exception) outside every middleware,
    so a crash used to reach the browser with no Access-Control-Allow-Origin - fetch() then reported
    "Network error" and the real reason was lost (Spotlight's AI steps all showed "Skipped", 10/16).
    The exact error also goes to the admin's bell + Discord (throttled)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001
            path = scope.get("path", "")
            logger.error("Unhandled exception", exc_info=True, extra={"path": path})
            try:
                from app.services.admin_alerts import alert_admin
                area = (path.strip("/").split("/") + [""])[0] or "api"
                await alert_admin(area, f"Server error on {path}: {type(exc).__name__}"[:160],
                                  f"{scope.get('method')} {path} crashed: {type(exc).__name__}: {str(exc)[:500]}", path=path)
            except Exception:  # noqa: BLE001
                pass
            if started:
                raise
            await JSONResponse(status_code=500, content={"detail": "Something went wrong on our side - please try again."})(scope, receive, send)


app.add_middleware(CrashToJsonMiddleware)

app.add_middleware(
    CORSMiddleware,
    # Local dev browsers may load the app via either "localhost" or "127.0.0.1" -
    # allow both so a CORS-origin mismatch never masks itself as a generic
    # "Failed to fetch" error in the browser console.
    allow_origins=[CLIENT_URL, CLIENT_URL.replace("localhost", "127.0.0.1")] if "localhost" in CLIENT_URL else [CLIENT_URL],
    # Every agent's own <slug>.openhousepal.com website (IDX site, Spotlights) talks to this API
    # from the browser too (signature card, "data last updated", Showcase actions) - without
    # this those calls were blocked as soon as a site ran on its own address.
    allow_origin_regex=rf"https://([a-z0-9-]+\.)*{re.escape(os.getenv('OHP_BASE_DOMAIN', 'openhousepal.com'))}",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# Agents' own custom domains (theirrealty.com) can't be listed ahead of time. Visitor-facing,
# no-login endpoints answer browsers on ANY site - safe because sign-in is a Bearer token, never a
# cookie, so a foreign page can't act as anyone. Added after CORSMiddleware = runs first.
_PUBLIC_CORS_PREFIXES = (
    "/public/", "/agent-hub/public/", "/agent-hub/forms/public/", "/studio/blogs/public/",
    "/email-signature/public/", "/api/cma/public/", "/invites/public/",
)


def _is_public_path(path: str) -> bool:
    # + the no-login property lookups visitors' pages use on any site (home map lines, town /
    # school district / address pickers): /api/properties/<thing>/public.
    return path.startswith(_PUBLIC_CORS_PREFIXES) or (path.startswith("/api/properties/") and path.rstrip("/").endswith("/public"))


class PublicCORSMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_public_path(scope["path"]):
            return await self.app(scope, receive, send)
        origin = next((v.decode() for k, v in scope["headers"] if k == b"origin"), None)
        if not origin:
            return await self.app(scope, receive, send)
        cors = [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin")]
        if scope["method"] == "OPTIONS":
            req_headers = next((v for k, v in scope["headers"] if k == b"access-control-request-headers"), b"content-type")
            await send({"type": "http.response.start", "status": 200, "headers": cors + [
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                (b"access-control-allow-headers", req_headers), (b"access-control-max-age", b"600"),
                (b"content-length", b"0")]})
            return await send({"type": "http.response.body", "body": b""})

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"access-control-allow-origin", b"access-control-allow-credentials")]
                message = {**message, "headers": headers + cors}
            await send(message)
        return await self.app(scope, receive, send_with_cors)


app.add_middleware(PublicCORSMiddleware)


from fastapi import HTTPException as _HTTPException
from fastapi.exception_handlers import http_exception_handler as _default_http_handler


@app.exception_handler(_HTTPException)
async def provider_failure_handler(request: Request, exc: _HTTPException):
    """502 / 503 = something only OpenHousePal can fix (an AI provider out of credit or down, a key or
    setting missing). Agents never see why (user, 10/15) - they get one calm message, and the real
    reason goes to the admin's bell + Discord (admin_alerts). Every other status passes through."""
    if exc.status_code in (502, 503):
        from app.services.admin_alerts import AGENT_MESSAGE, alert_admin
        owner_id = None
        try:
            from app.utils.auth import verify_token
            auth = request.headers.get("authorization") or ""
            owner_id = verify_token(auth.split(" ", 1)[1]) if auth.lower().startswith("bearer ") else None
        except Exception:  # noqa: BLE001
            owner_id = None
        area = (request.url.path.strip("/").split("/") + [""])[0] or "api"
        await alert_admin(area, str(exc.detail)[:160], f"{request.method} {request.url.path} returned {exc.status_code}: {exc.detail}",
                          owner_id=owner_id, path=request.url.path)
        return JSONResponse(status_code=exc.status_code, content={"detail": AGENT_MESSAGE}, headers=getattr(exc, "headers", None))
    return await _default_http_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Without this, an unhandled exception is caught by Starlette's default
    # error handler outside the CORS middleware layer, so the resulting 500
    # has no Access-Control-Allow-Origin header. Browsers then block the
    # response as a CORS failure, which surfaces to fetch() callers as a
    # generic "Failed to fetch" indistinguishable from the server being down.
    logger.error("Unhandled exception", exc_info=True, extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Include API routes
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port: int = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
