"""Every paid provider's month so far, against its free monthly allowance or our own monthly cap
(owner 11/13: "add to the admin dash tracking ... how much is gone ... admin account should get
alerts about these limits getting close ... do that with all providers we use").

Rows (all month = calendar month, UTC - how AWS and Google count):
  - AWS Lambda (Cinematic Video renders): EXACT compute and requests from CloudWatch, against AWS's
    free 400,000 GB-seconds + 1,000,000 requests a month; money past that, and what agents were charged.
  - Google Maps: every kind of call against its free calls (google_free_tier counts them and stops
    them at 95%, so these never bill).
  - AI providers (Anthropic, OpenAI, fal, Runway/MiniMax, AWS, Google charged to agents): spend this
    month from AIUsageEvent, and all of it against the platform's monthly emergency cap.
  - Mailgun: emails sent this month (EmailLog), against the plan's included emails when that's set
    (config mailgun_monthly_included_emails).

check_and_alert() (scheduler, every 30 minutes) sends the admin ONE alert per row per month at 80%
and again at 95% (deduped in usage_budget counters, so restarts don't repeat it).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

LAMBDA_FREE_GB_SECONDS = 400_000
LAMBDA_FREE_REQUESTS = 1_000_000
LAMBDA_ARM_USD_PER_GB_SECOND = 0.0000133334
LAMBDA_USD_PER_REQUEST = 0.0000002
ALERT_AT = (80, 95)
_CACHE: dict[str, Any] = {"at": 0.0, "aws": None}


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _aws_lambda_month() -> Optional[dict]:
    """Month-to-date Lambda use for the whole AWS account in the render region (CloudWatch's
    account-wide AWS/Lambda metrics). Blocking - run in a thread. None when AWS isn't set up."""
    if not os.environ.get("REMOTION_FUNCTION_NAME"):
        return None
    import boto3
    region = os.environ.get("REMOTION_REGION") or "us-east-1"
    cw = boto3.client("cloudwatch", region_name=region)
    start, end = _month_start(), datetime.now(timezone.utc)

    def total(metric: str) -> float:
        r = cw.get_metric_statistics(Namespace="AWS/Lambda", MetricName=metric, StartTime=start, EndTime=end,
                                     Period=86400, Statistics=["Sum"])
        return float(sum(p.get("Sum", 0) for p in r.get("Datapoints") or []))
    duration_ms = total("Duration")
    requests = total("Invocations")
    mem_gb = float(os.environ.get("REMOTION_MEMORY_MB") or 3008) / 1024.0
    gbs = duration_ms / 1000.0 * mem_gb
    over = max(0.0, gbs - LAMBDA_FREE_GB_SECONDS) * LAMBDA_ARM_USD_PER_GB_SECOND + max(0.0, requests - LAMBDA_FREE_REQUESTS) * LAMBDA_USD_PER_REQUEST
    full = gbs * LAMBDA_ARM_USD_PER_GB_SECOND + requests * LAMBDA_USD_PER_REQUEST
    return {"gb_seconds": gbs, "requests": requests, "billed_usd": over, "value_usd": full}


async def _aws_cached() -> Optional[dict]:
    if time.time() - _CACHE["at"] < 600 and _CACHE["aws"] is not None:
        return _CACHE["aws"]
    try:
        aws = await asyncio.to_thread(_aws_lambda_month)
    except Exception as exc:  # noqa: BLE001 - shown as "couldn't read" on the card
        logger.warning("provider limits: CloudWatch read failed: %s", str(exc)[:200])
        return {"error": str(exc)[:160]}
    _CACHE.update(at=time.time(), aws=aws)
    return aws


def _provider_of(model: Optional[str], event_type: Optional[str]) -> str:
    m = (model or "").lower()
    if m.startswith("aws:"):
        return "AWS"
    if m.startswith("google:") or event_type == "external_api":
        return "Google"
    if m.startswith("fal:") or "fal-ai" in m or m.startswith("fal"):
        return "fal"
    if "runway" in m or "gen4" in m:
        return "Runway"
    if "minimax" in m or "hailuo" in m:
        return "MiniMax"
    if m.startswith("claude") or "anthropic" in m:
        return "Anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "tts", "whisper", "dall")) or event_type in ("tts", "realtime", "studio_media_image", "cma_reasoning"):
        return "OpenAI"
    return "Other"


async def snapshot(db) -> dict:
    from app.models.database import AIUsageEvent, EmailLog
    from app.services import google_free_tier
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    start = _month_start()
    rows: list[dict] = []

    def row(key: str, provider: str, label: str, used: float, limit: Optional[float], unit: str, note: str = "", spend_usd: Optional[float] = None, kind: str = "free"):
        pct = round(used / limit * 100, 1) if limit else None
        rows.append({"key": key, "provider": provider, "label": label, "used": round(used, 2), "limit": limit, "unit": unit,
                     "percent": pct, "note": note, "spend_usd": None if spend_usd is None else round(spend_usd, 4), "kind": kind})

    # --- spend by provider this month (what our own records say each provider cost)
    # same money rule and Eastern month as the rest of Admin (admin_ai_routes._spent: refunded runs
    # we still paid for count; pre-call guesses don't), so this card and the provider cards agree
    from app.api.admin_ai_routes import _month_start as _admin_month_start, _spent
    spend: dict[str, float] = {}
    q = await db.execute(select(AIUsageEvent.model, AIUsageEvent.event_type, _spent(config))
                         .where(AIUsageEvent.created_at >= _admin_month_start(datetime.now(timezone.utc)))
                         .group_by(AIUsageEvent.model, AIUsageEvent.event_type))
    for model, et, cost in q.all():
        p = _provider_of(model, et)
        spend[p] = spend.get(p, 0.0) + float(cost or 0)

    # --- AWS Lambda (exact, from AWS)
    aws = await _aws_cached()
    if aws and not aws.get("error"):
        agents = spend.get("AWS", 0.0)
        row("aws_lambda_compute", "AWS", "AWS Lambda - video making (compute)", aws["gb_seconds"], LAMBDA_FREE_GB_SECONDS, "GB-seconds",
            f"AWS gives 400,000 free a month (about 40 thirty-second videos). Billed past that: ${aws['billed_usd']:.2f} so far. "
            f"Charged to agents' AI usage: ${agents:.2f}.", spend_usd=aws["billed_usd"])
        row("aws_lambda_requests", "AWS", "AWS Lambda - requests", aws["requests"], LAMBDA_FREE_REQUESTS, "requests",
            "AWS gives 1,000,000 free a month.")
    elif aws and aws.get("error"):
        row("aws_lambda_compute", "AWS", "AWS Lambda - video making", 0, None, "", f"Couldn't read AWS just now: {aws['error']}")

    # --- Google Maps free calls (never billed: calls stop at 95% and wait for the 1st)
    try:
        for g in await google_free_tier.month_usage(db):
            row(f"google:{g['sku']}", "Google", f"Google - {g['sku'].replace('_', ' ')}", g["used"], g["free"], "calls",
                f"{g['free']:,} free a month. Calls stop at {g['stops_at']:,} and wait for the 1st - never billed.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider limits: google usage failed: %s", str(exc)[:200])

    # --- AI providers: spend this month vs the platform's monthly emergency cap
    cap = float(config.get("platform_emergency_monthly_cost_ceiling_usd") or 0) or None
    total = sum(spend.values())
    row("platform_ai_total", "All AI", "All AI + per-use providers together", total, cap, "usd",
        "Our own monthly emergency cap (AI settings): past it, AI tools stop for everyone until the 1st.", spend_usd=total, kind="cap")
    for p in ("Anthropic", "OpenAI", "fal", "Runway", "MiniMax", "AWS", "Google", "Other"):
        if spend.get(p):
            row(f"spend:{p}", p, f"{p} - spent this month", spend[p], None, "usd", "From our usage records (what agents' tools cost).",
                spend_usd=spend[p], kind="spend")

    # --- Mailgun
    try:
        sent = (await db.execute(select(func.count(EmailLog.id)).where(EmailLog.created_at >= start, EmailLog.via == "openhousepal",
                                                                        EmailLog.status == "sent"))).scalar_one() or 0
        inc = config.get("mailgun_monthly_included_emails")
        row("mailgun_emails", "Mailgun", "Mailgun - emails sent", float(sent), float(inc) if inc else None, "emails",
            "Included in the Mailgun plan each month." if inc else "Set mailgun_monthly_included_emails in AI settings to get alerts.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider limits: email count failed: %s", str(exc)[:200])

    return {"month": start.strftime("%Y-%m"), "updated_at": datetime.now(timezone.utc).isoformat(), "rows": rows}


async def check_and_alert() -> int:
    """Scheduler job: one admin alert per row per month at 80% and at 95%."""
    from app.database import AsyncSessionLocal
    from app.services.admin_alerts import alert_admin
    from app.services.usage_budget_service import _period_keys, reserve_bucket
    sent = 0
    try:
        async with AsyncSessionLocal() as db:
            snap = await snapshot(db)
            _day, month = _period_keys()
            for r in snap["rows"]:
                pct = r.get("percent")
                if pct is None:
                    continue
                for thr in sorted(ALERT_AT, reverse=True):
                    if pct < thr:
                        continue
                    first = await reserve_bucket(db, f"limit_alert:{r['key']}:{thr}", "month", month, 1, 0, 1, None)
                    await db.commit()
                    if first:
                        used = f"{r['used']:,.0f} of {r['limit']:,.0f} {r['unit']}" if r["unit"] != "usd" else f"${r['used']:,.2f} of ${r['limit']:,.2f}"
                        await alert_admin("limits", f"{r['label']}: {pct:.0f}% used this month", f"{used}. {r['note']}", path="/admin/costs/providers")
                        sent += 1
                    break  # only the highest line crossed
    except Exception:  # noqa: BLE001
        logger.warning("provider limit check failed", exc_info=True)
    return sent
