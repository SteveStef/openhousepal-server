"""Save usage for CMA reports (owner, 11/17): "I want CMAs to have the save usage mode and the make it now like our
other features with how much AI usage each uses and how long it takes to make".

The very same report - same comps, same models, same instructions, same checks - with its AI requests sent through
Anthropic's half-price batch queue instead of the normal way (cma_claude.save_usage). Only the waiting changes:
  - the run happens in the background; the agent can leave the page and is told by the bell AND an email;
  - a request still waiting when the report has been going CAP_MINUTES is sent the normal way, and every request
    after it too, so a report is never much later than the limit. What it costs is what it really used - half for
    the answers the queue gave, full price for any finished the fast way - so the screen shows a range up front
    (CAP_MINUTES = 20, so a Save usage report is ready within about 25 minutes);
  - live 11/17: the last 7 reports cost $0.64-$0.85 made the normal way (about $0.73).

A run cut off by a server restart (a push) starts again by itself within a few minutes (cma_routes.resume_stalled),
with only what is left of its 20 minutes in the slower line - so it still ends within about 25 minutes of the press.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CAP_MINUTES = 20            # owner 11/17: "for cma it should be like 20 min, as after that we know it's probably not
                            # happening" (blogs keep their hour) - then whatever is left is done the fast way
FAST_FINISH_MINUTES = 5     # what the remaining steps take the fast way, at the outside (a whole report: ~3 min)
TYPICAL_MINUTES = 15        # "likely ready by" until real Save usage runs can be measured (local tests: 9-13 minutes)
NOW_MINUTES = 3             # a report made the normal way
EMAIL_TEMPLATES = {"cma_report_ready", "cma_report_failed"}


def cap_minutes(config: Optional[dict] = None) -> int:
    try:
        return max(10, int((config or {}).get("cma_save_usage_cap_minutes") or CAP_MINUTES))
    except (TypeError, ValueError):
        return CAP_MINUTES


def stale_after_seconds(config: Optional[dict] = None) -> int:
    """A Save usage run still marked "generating" past this has died (a restart) - the page stops waiting for it."""
    return (cap_minutes(config) + FAST_FINISH_MINUTES + 15) * 60


def times(config: Optional[dict] = None, minutes: Optional[float] = None) -> dict:
    """For the Save usage choice, before anything starts."""
    now = datetime.now(timezone.utc)
    cap = cap_minutes(config)
    typical = int(round(min(float(minutes or TYPICAL_MINUTES), cap + FAST_FINISH_MINUTES)))
    return {"typical_minutes": typical, "likely_at": (now + timedelta(minutes=typical)).isoformat(),
            "cap_minutes": cap, "max_minutes": cap + FAST_FINISH_MINUTES,
            "latest_at": (now + timedelta(minutes=cap + FAST_FINISH_MINUTES)).isoformat()}


def run_record(state: dict, started: datetime, finished: Optional[datetime] = None) -> dict:
    """Kept on the report (ai_analysis["save_usage"]) - how long it took and how much went through the slower line.
    The "likely ready by" of later reports is measured from these."""
    finished = finished or datetime.now(timezone.utc)
    return {"minutes": round((finished - started).total_seconds() / 60, 1),
            "queued_calls": int(state.get("queued") or 0), "fast_calls": int(state.get("fast") or 0),
            "capped": bool(state.get("fast"))}


def render_email(template: str, subject: str, v: dict) -> tuple[str, str, str]:
    """The ready / couldn't-finish email, rendered here (the Mailgun domain is at its template cap)."""
    from app.services.invite_email_service import _settings_footer, button, eyebrow, h1, ohp_header, para, shell, e
    home = v.get("address") or "your home"
    link = v.get("link") or ""
    if template == "cma_report_failed":
        body = [eyebrow("CMA"), h1("We couldn't finish your CMA"),
                para(f"Hi {e(v.get('first_name') or 'there')}, the CMA for {e(home)} that you left us to make didn't finish."),
                para(e(v.get("reason") or "Open it to try again."), muted=True),
                button("Open the CMA", link)]
        text = [f"We couldn't finish the CMA for {home}.", v.get("reason") or "Open it to try again.", f"Open it: {link}"]
        pre = "Open it to try again."
    else:
        used = v.get("used_line") or ""
        body = [eyebrow("CMA"), h1("Your CMA is ready"),
                para(f"Hi {e(v.get('first_name') or 'there')}, the CMA for {e(home)} is finished - comps analyzed, value worked out and the report written."),
                para("Nothing is sent to your client until you share it. Give it a read first.")]
        if used:
            body.append(para(e(used), muted=True))
        body.append(button("Open the CMA", link))
        text = [f"Your CMA for {home} is ready.", "Nothing is sent to your client until you share it.", used, f"Open it: {link}"]
        pre = "Comps analyzed, value worked out and the report written."
    html = shell(title=subject, preheader=pre, header_cells=ohp_header("CMA"), body="".join(body), footer=_settings_footer(v.get("app_url") or ""))
    return subject, html, "\n".join(t for t in text if t)


async def tell_agent(owner_id: str, report_id: str, address: str, error: Optional[str], cost_usd: Optional[float]) -> None:
    """The bell and an email, every time - the agent chose to walk away. Never sinks a finished report."""
    try:
        from app.database import AsyncSessionLocal
        from app.models.database import Notification, User
        from app.services.agent_email_service import absolute, email_agent
        from app.services.ai.ai_config_service import ai_config_service
        from app.services.ai.ai_rate_limit_service import monthly_limit_usd
        link = f"/studio/cma/{report_id}"
        async with AsyncSessionLocal() as db:
            db.add(Notification(
                agent_id=owner_id, type="CMA_AI_FAILED" if error else "CMA_AI_READY", reference_type="CMA", reference_id=report_id,
                title=(f"Couldn't finish the CMA for {address}" if error else f"Your CMA is ready: {address}")[:200],
                message=(f"{error} Open it to try again." if error else "Give it a read before you share it with your client.")[:500],
                link=link, is_read=False, created_at=datetime.now(timezone.utc)))
            await db.commit()
            owner = await db.get(User, owner_id)
            if owner is None:
                return
            used_line = None
            if cost_usd and not error:
                limit = await monthly_limit_usd(db, owner_id, await ai_config_service.get_config(db), owner.plan_tier)
                if limit:
                    used_line = f"It used about {max(0.1, round(cost_usd / float(limit) * 100, 1)):g}% of this month's AI usage."
        email_agent(owner, None, "cma_report_failed" if error else "cma_report_ready",
                    (f"We couldn't finish your CMA: {address}" if error else f"Your CMA is ready: {address}")[:150],
                    {"address": address, "link": absolute(link), "reason": error, "used_line": used_line}, always=True)
    except Exception:  # noqa: BLE001
        logger.warning("CMA Save usage notice failed", exc_info=True, extra={"report_id": report_id})
