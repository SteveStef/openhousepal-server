"""Admin "Recent failures": every feature failure we already record, in one newest-first list -
who, which feature, when, and the error (user, 10/20: a CMA generation failed and nothing showed
in admin). Read-only; it gathers what each feature already stores, no new tracking:

  * AI calls        - AIUsageEvent rows with blocked=True (block_reason is the code: a provider
                      error from classify_provider_error, or one of our own spending limits)
  * CMA generations - CmaReport.generation_status='failed' + generation_error (the full message)
  * AI Media        - StudioMediaAsset.status='failed' + error_message
  * Emails          - ClientEmail.status='failed' + error, ScheduledEmail.status='FAILED' + error_message
  * Connections     - MailConnection / CalendarConnection.last_error (a broken Gmail/Outlook/Calendar link)

Like the rest of the admin AI page it never reads message content (AIMessage) - only the
metadata and the stored error text.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    AIUsageEvent, CalendarConnection, ClientEmail, CmaReport, MailConnection, ScheduledEmail,
    StudioMediaAsset, User,
)

# AIUsageEvent.event_type -> the feature an admin recognises.
_FEATURE = {
    "chat": "Agent Chat", "tts": "Read Aloud", "realtime": "Talk to OHP",
    "cma_ai": "CMA - AI comp analysis", "cma_reasoning": "CMA - final valuation",
    "area_narrative": "Area narrative", "area_research": "Area research",
    "copywrite": "AI Copywriter", "blog_writer": "AI Blog Writer",
    "studio_media_image": "AI Media - image", "studio_media_video": "AI Media - video",
}

# block_reason -> what it means, in plain words.
_REASON = {
    "insufficient_credit": "The AI provider account is out of credit.",
    "provider_rate_limited": "The AI provider rate-limited us.",
    "provider_overloaded": "The AI provider was overloaded or unavailable.",
    "provider_timeout": "The AI provider timed out.",
    "provider_auth_error": "The AI provider rejected our API key.",
    "provider_error": "The AI provider returned an error.",
    "burst_limit": "Blocked by the per-agent burst limit.",
    "daily_limit": "Blocked by the agent's daily limit.",
    "monthly_limit": "Blocked by the agent's monthly AI allowance.",
    "cost_ceiling": "Blocked by a platform-wide spending ceiling.",
    "input_too_long": "The request was too long to send.",
    "timeout": "The request timed out.",
    "iteration_limit": "The AI hit its step limit before finishing.",
    "refunded_failed_run": "The run failed part-way and its cost was refunded to the agent.",
}
# Spending/abuse limits doing their job - shown, but not as an error.
_LIMIT_REASONS = {"burst_limit", "daily_limit", "monthly_limit", "cost_ceiling", "input_too_long"}


def _row(at, source: str, feature: str, owner_id: Optional[str], code: Optional[str], message: Optional[str],
         kind: str = "error", ref: Optional[str] = None) -> dict:
    return {"at": at.isoformat() if at else None, "source": source, "feature": feature, "owner_id": owner_id,
            "code": code, "message": (message or "")[:600] or None, "kind": kind, "ref": ref}


async def recent_failures(db: AsyncSession, days: int = 14, limit: int = 200, include_limits: bool = False) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []

    ai_q = select(AIUsageEvent).where(AIUsageEvent.blocked.is_(True), AIUsageEvent.created_at >= since)
    if not include_limits:
        ai_q = ai_q.where(or_(AIUsageEvent.block_reason.is_(None), AIUsageEvent.block_reason.notin_(list(_LIMIT_REASONS))))
    for e in (await db.execute(ai_q.order_by(AIUsageEvent.created_at.desc()).limit(limit))).scalars():
        reason = e.block_reason or "unknown"
        rows.append(_row(e.created_at, "ai", _FEATURE.get(e.event_type, e.event_type or "AI"), e.owner_id, reason,
                         " ".join(p for p in (_REASON.get(reason), f"Model: {e.model}." if e.model else None) if p),
                         kind="limit" if reason in _LIMIT_REASONS else "error"))

    for r in (await db.execute(
        select(CmaReport).where(CmaReport.generation_status == "failed",
                                func.coalesce(CmaReport.updated_at, CmaReport.created_at) >= since)
        .order_by(func.coalesce(CmaReport.updated_at, CmaReport.created_at).desc()).limit(limit)
    )).scalars():
        address = ((r.subject or {}).get("street_address") or "") if isinstance(r.subject, dict) else ""
        rows.append(_row(r.updated_at or r.created_at, "cma", "CMA generation", r.owner_id, "generation_failed",
                         r.generation_error or "The AI analysis didn't finish.", ref=f"{address} · report {r.id[:8]}" if address else f"report {r.id[:8]}"))

    for a in (await db.execute(
        select(StudioMediaAsset).where(StudioMediaAsset.status == "failed", StudioMediaAsset.created_at >= since)
        .order_by(StudioMediaAsset.created_at.desc()).limit(limit)
    )).scalars():
        rows.append(_row(a.updated_at or a.created_at, "media", f"AI Media - {a.kind} ({a.action})", a.owner_id,
                         a.provider or "failed", a.error_message))

    for m in (await db.execute(
        select(ClientEmail).where(ClientEmail.status == "failed", ClientEmail.created_at >= since)
        .order_by(ClientEmail.created_at.desc()).limit(limit)
    )).scalars():
        rows.append(_row(m.created_at, "email", f"Email to a client ({m.template})", m.owner_id, m.via or "send_failed", m.error))

    for s in (await db.execute(
        select(ScheduledEmail).where(func.upper(ScheduledEmail.status) == "FAILED", ScheduledEmail.created_at >= since)
        .order_by(ScheduledEmail.created_at.desc()).limit(limit)
    )).scalars():
        rows.append(_row(s.sent_at or s.scheduled_for or s.created_at, "email", f"Scheduled email ({s.template_name})", s.owner_id,
                         "send_failed", s.error_message))

    for model, label in ((MailConnection, "Send as me (mailbox)"), (CalendarConnection, "Calendar sync")):
        for c in (await db.execute(
            select(model).where(model.last_error.isnot(None), model.last_error != "",
                                func.coalesce(model.updated_at, model.created_at) >= since)
        )).scalars():
            rows.append(_row(c.updated_at or c.created_at, "connection", f"{label} - {c.provider}", c.owner_id,
                             "connection_error", c.last_error))

    rows.sort(key=lambda x: x["at"] or "", reverse=True)
    rows = rows[:limit]

    owner_ids = {r["owner_id"] for r in rows if r["owner_id"]}
    people = {}
    if owner_ids:
        for u in (await db.execute(select(User).where(User.id.in_(owner_ids)))).scalars():
            people[u.id] = {"name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email, "email": u.email}
    for r in rows:
        p = people.get(r["owner_id"] or "", {})
        r["owner_name"] = p.get("name")
        r["owner_email"] = p.get("email")

    by_feature: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "error":
            by_feature[r["feature"]] = by_feature.get(r["feature"], 0) + 1
    return {"days": days, "count": len(rows), "errors": sum(1 for r in rows if r["kind"] == "error"),
            "by_feature": by_feature, "failures": rows}
