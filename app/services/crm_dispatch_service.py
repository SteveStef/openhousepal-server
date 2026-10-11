"""Send leads OUT to the agent's real CRM.

Agents don't want to leave FollowUpBoss / Lofty / kvCORE / Sierra / BoomTown - so when a lead is
captured in OpenHousePal we forward it to whichever of these the agent connected:

  * crm_fub_api_key   - native Follow Up Boss: POST to /v1/events (creates/merges the person and
                        logs the lead event, source "OpenHousePal"). The cleanest path. On a team
                        account the new lead follows the team's lead-routing rules (often assigned
                        to the account owner), which is FUB's normal behaviour, not an error.
  * crm_webhook_url   - a JSON POST in a generic, widely-accepted shape (name/email/phone/source
                        /message/tags). Good for Zapier / Make / a custom endpoint.
  * crm_parse_email   - the CRM's "lead parsing" inbox address. We send a formatted plain-text
                        email that those systems already know how to read (they were built to
                        parse Zillow / Realtor.com lead emails, which look just like this).

Both are best-effort and never block or fail the thing that created the lead. The last result is
stamped on the user row (crm_last_push_at / crm_last_push_status) purely so Settings can show
"Last lead delivered 2m ago" vs. an error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Lead, LeadEvent, User
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(6.0, connect=4.0)

# LeadEvent.source_type -> a human phrase for the "Source" line a CRM will show its agent.
_SOURCE_LABELS = {
    "open_house_visitor": "Open House sign-in",
    "open_house_second_visit": "Open House - second visit request",
    "open_house_disclosure": "Open House - disclosure request",
    "form_submission": "Website form",
    "property_tour": "Showing request",
    "property_website_tour_request": "Showing request",
    "property_website_inquiry": "Listing website inquiry",
    "property_comment": "Listing website comment",
    "collection_visitor": "Property showcase",
    "portal_lead": "Portal lead",
    "property_page_inquiry": "Listing page message",
    "property_page_tour_request": "Listing page tour request",
    "form_partial": "Website form (unfinished)",
    "blog_inquiry": "Blog article question",
}


def _source_label(source_type: Optional[str]) -> str:
    return _SOURCE_LABELS.get(source_type or "", (source_type or "OpenHousePal").replace("_", " ").title())


# Follow Up Boss event type per touchpoint (their documented types) - so a sign-in shows as
# "Visited Open House", a home-value form as "Seller Inquiry", etc. in the agent's FUB timeline.
_FUB_TYPES = {
    "open_house_visitor": "Visited Open House",
    "open_house_second_visit": "Visited Open House",
    "open_house_disclosure": "Visited Open House",
    "property_tour": "Property Inquiry",
    "property_website_tour_request": "Property Inquiry",
    "property_page_tour_request": "Property Inquiry",
    "property_website_inquiry": "Property Inquiry",
    "property_page_inquiry": "Property Inquiry",
    "property_comment": "Property Inquiry",
    "collection_visitor": "Property Search",
    "portal_lead": "Inquiry",
    "blog_inquiry": "General Inquiry",
}
_SELLER_FORMS = {"selling", "home_value", "seller_onboarding", "make_me_sell"}


def _answers_text(event: Optional[LeadEvent]) -> str:
    """The person's own answers (form questions, portal message, preferred time...), one per line."""
    snap = (event.raw_snapshot if event and isinstance(event.raw_snapshot, dict) else {}) or {}
    lines: list[str] = []
    answers = snap.get("answers")
    if isinstance(answers, list):
        for a in answers[:25]:
            if not isinstance(a, dict):
                continue
            label, value = str(a.get("label") or "").strip(), a.get("value")
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value if v not in (None, ""))
            elif isinstance(value, dict):
                value = ", ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, ""))
            value = str(value or "").strip()
            if label and value and label.lower() not in ("name", "full name", "email", "phone", "email address", "phone number"):
                lines.append(f"{label}: {value[:300]}")
    if isinstance(snap.get("message"), str) and snap["message"].strip():
        lines.append(f"Message: {snap['message'].strip()[:1000]}")
    return "\n".join(lines)


def fub_event_type(event: Optional[LeadEvent]) -> str:
    st = event.source_type if event else None
    if st in ("form_submission", "form_partial"):
        snap = (event.raw_snapshot if isinstance(event.raw_snapshot, dict) else {}) or {}
        return "Seller Inquiry" if snap.get("form_type") in _SELLER_FORMS else "General Inquiry"
    return _FUB_TYPES.get(st or "", "General Inquiry")


def build_payload(user: User, lead: Lead, event: Optional[LeadEvent]) -> dict:
    """The JSON body POSTed to crm_webhook_url. Deliberately flat + conventionally named so a
    no-code hook (Zapier / Make / a CRM's native inbound webhook) can map it with no transform."""
    src = _source_label(event.source_type if event else None)
    occurred = (event.occurred_at if event and event.occurred_at else datetime.now(timezone.utc))
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    answers = _answers_text(event)
    summary = event.summary if event and event.summary else src
    return {
        "system": "OpenHousePal",
        "event": "lead.captured",
        "name": lead.name or "",
        "first_name": (lead.name or "").split(" ")[0] if lead.name else "",
        "last_name": " ".join((lead.name or "").split(" ")[1:]) if lead.name and " " in lead.name else "",
        "email": lead.email or "",
        "phone": lead.phone or "",
        "source": f"OpenHousePal - {src}",
        "message": f"{summary}\n\n{answers}" if answers else summary,
        "answers": answers,
        "lead_type": fub_event_type(event),
        "property_address": (event.property_address if event else None) or "",
        "tags": ["OpenHousePal", src],
        "represented": lead.represented or "unknown",
        "occurred_at": occurred.isoformat(),
        "lead_id": lead.id,
        "agent_email": user.email,
    }


def build_email_text(user: User, lead: Lead, event: Optional[LeadEvent], payload: dict) -> str:
    """A plain-text lead email in the shape FUB / Lofty / kvCORE lead-parsers expect - a labelled
    key/value list, one per line, with a clear 'New Lead' header."""
    lines = [
        "New Lead from OpenHousePal",
        "",
        f"Name: {payload['name'] or 'Unknown'}",
        f"Email: {payload['email'] or 'N/A'}",
        f"Phone: {payload['phone'] or 'N/A'}",
        f"Source: {payload['source']}",
    ]
    if payload["property_address"]:
        lines.append(f"Property: {payload['property_address']}")
    lines.append(f"Represented: {payload['represented']}")
    lines.append("")
    lines.append(f"Notes: {payload['message']}")
    lines.append("")
    lines.append(f"Received: {payload['occurred_at']}")
    lines.append("-- ")
    lines.append("Forwarded automatically by OpenHousePal so this lead lands in your CRM.")
    return "\n".join(lines)


def _fub_event_body(payload: dict) -> dict:
    """Follow Up Boss /v1/events shape - creates/merges a person and logs the lead as an event."""
    person: dict = {
        "firstName": payload["first_name"] or payload["name"] or "OpenHousePal Lead",
        "lastName": payload["last_name"] or "",
        "tags": [t for t in payload.get("tags") or ["OpenHousePal"] if t],
    }
    if payload["email"]:
        person["emails"] = [{"value": payload["email"]}]
    if payload["phone"]:
        person["phones"] = [{"value": payload["phone"]}]
    body: dict = {
        "source": "OpenHousePal",
        "system": "OpenHousePal",
        "type": payload.get("lead_type") or ("Property Inquiry" if payload["property_address"] else "General Inquiry"),
        "message": payload["message"],
        "person": person,
    }
    if payload["property_address"]:
        body["property"] = {"street": payload["property_address"]}
    return body


async def probe_fub_account(api_key: str) -> Optional[str]:
    """Ask FUB who this key belongs to. Returns a short plain-language heads-up string when the
    key is a NON-owner / non-admin user on a team account (leads we create then follow the team's
    lead-routing rules and often land with the account owner, not the connecting agent) - else
    None (solo, owner/admin, or the call didn't work). Best-effort; never raises."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.followupboss.com/v1/identity",
                auth=(api_key, ""),
                headers={"X-System": "OpenHousePal"},
            )
        if resp.status_code in (401, 403):
            return ("⚠ Follow Up Boss rejected this API key. Double-check you copied the whole "
                    "key from FUB → Admin → API → Create API Key (it's shown only once). Leads "
                    "won't reach Follow Up Boss until the key is valid.")
        if resp.status_code >= 300:
            return None
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("FUB identity probe failed: %s", e)
        return None

    acct = data.get("account") or {}
    usr = data.get("user") or {}
    if usr.get("isOwner") or usr.get("isAdmin"):
        return None  # they see every lead anyway

    owner_name = ((acct.get("owner") or {}).get("name") or "").strip()
    acct_name = (acct.get("name") or "your team").strip()
    who = f" (often {owner_name})" if owner_name else ""
    return (
        f"This key belongs to a regular user on the Follow Up Boss account “{acct_name}”. "
        f"New leads OpenHousePal sends will follow that account's lead-routing rules, so they may "
        f"be assigned to an account admin or owner{who} — or round-robined — rather than landing "
        f"directly with you. That's normal Follow Up Boss behaviour for a team key; ask your FUB "
        f"admin to route OpenHousePal leads to you if you need them in your own inbox."
    )


async def _deliver(user: User, payload: dict, email_text: str) -> dict:
    """Fire whichever channels the agent configured. Returns a per-channel result dict; never
    raises."""
    result: dict = {}
    if user.crm_webhook_url:
        result["webhook"] = None
    if user.crm_parse_email:
        result["email"] = None
    if user.crm_fub_api_key:
        result["fub"] = None

    if user.crm_fub_api_key:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    "https://api.followupboss.com/v1/events",
                    json=_fub_event_body(payload),
                    auth=(user.crm_fub_api_key, ""),
                    headers={"X-System": "OpenHousePal", "Content-Type": "application/json"},
                )
            result["fub"] = "ok" if resp.status_code < 300 else f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            logger.warning("CRM Follow Up Boss push failed: %s", e)
            result["fub"] = f"error: {type(e).__name__}"

    if user.crm_webhook_url:
        try:
            from app.utils.safe_fetch import assert_public_url
            await assert_public_url(user.crm_webhook_url)  # re-checked at send time (DNS can change)
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(user.crm_webhook_url, json=payload, follow_redirects=False)
            result["webhook"] = "ok" if resp.status_code < 300 else f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001 - best effort
            logger.warning("CRM webhook push failed: %s", e)
            result["webhook"] = f"error: {type(e).__name__}"

    if user.crm_parse_email:
        try:
            code, body = EmailService().send_raw_message(
                user.crm_parse_email,
                f"New Lead: {payload['name'] or payload['email'] or 'OpenHousePal'}",
                email_text,
                from_name=f"{user.first_name} {user.last_name}".strip() or "OpenHousePal",
                reply_to=payload["email"] or None,
            )
            result["email"] = "ok" if code < 300 else f"HTTP {code}"
        except Exception as e:  # noqa: BLE001
            logger.warning("CRM parse-email push failed: %s", e)
            result["email"] = f"error: {type(e).__name__}"

    return result


def _failing(status: Optional[str]) -> bool:
    # a failed Test click isn't "already failing" - the first real failed lead still emails the agent
    return bool(status) and not status.startswith("test - ") and ("HTTP " in status or "error:" in status)


def _alert_if_newly_failing(user: User, prev_status: Optional[str], result: dict) -> None:
    """One email when the CRM push goes from working to failing - not one per lead."""
    try:
        if _failing(prev_status):
            return
        bad = {k: v for k, v in result.items() if v and v != "ok"}
        if not bad:
            return
        names = {"fub": "Follow Up Boss", "webhook": "your CRM webhook", "email": "your CRM's lead email"}
        crm = names.get(next(iter(bad)), "your CRM")
        reason = next(iter(bad.values()))
        if reason in ("HTTP 401", "HTTP 403"):
            reason = "the key or login was rejected"
        from app.services.agent_email_service import absolute, email_agent
        email_agent(user, "CRM_PUSH_FAILING", "crm_push_failing", f"New leads aren't reaching {crm}",
                    {"crm_name": crm, "error_reason": reason, "link": absolute("/studio/integrations")})
    except Exception:  # noqa: BLE001
        logger.warning("CRM failure email failed", exc_info=True)


def _summarize(result: dict) -> str:
    parts = [f"{k}:{v}" for k, v in result.items() if v is not None]
    return ", ".join(parts) if parts else "no channels configured"


async def push_lead(db: AsyncSession, owner_id: str, lead: Lead, event: Optional[LeadEvent]) -> None:
    """Forward one just-captured lead to the owner's CRM, if they turned it on. Runs on the
    caller's session; swallows everything, only logs. The caller owns the commit."""
    try:
        user = (await db.execute(select(User).where(User.id == owner_id))).scalar_one_or_none()
        if not user or not user.crm_push_enabled:
            return
        if not user.crm_webhook_url and not user.crm_parse_email and not user.crm_fub_api_key:
            return
        payload = build_payload(user, lead, event)
        result = await _deliver(user, payload, build_email_text(user, lead, event, payload))
        _alert_if_newly_failing(user, user.crm_last_push_status, result)
        user.crm_last_push_at = datetime.now(timezone.utc)
        user.crm_last_push_status = _summarize(result)
        db.add(user)
    except Exception:
        logger.exception("push_lead failed (non-fatal)")


_PENDING: set = set()


def schedule_push(owner_id: str, lead_id: str, event_id: Optional[str]) -> None:
    import asyncio
    task = asyncio.create_task(push_lead_background(owner_id, lead_id, event_id))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def push_lead_background(owner_id: str, lead_id: str, event_id: Optional[str]) -> None:
    """Fire-and-forget entrypoint: opens its OWN db session so a slow CRM endpoint never adds
    latency to (or rolls back) the request that captured the lead. Scheduled via
    asyncio.create_task from upsert_lead once the lead + event are committed."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.id == owner_id))).scalar_one_or_none()
            if not user or not user.crm_push_enabled:
                return
            if not user.crm_webhook_url and not user.crm_parse_email and not user.crm_fub_api_key:
                return
            lead = (await db.execute(select(Lead).where(Lead.id == lead_id))).scalar_one_or_none()
            if not lead:
                return
            event = None
            if event_id:
                event = (await db.execute(select(LeadEvent).where(LeadEvent.id == event_id))).scalar_one_or_none()
            payload = build_payload(user, lead, event)
            result = await _deliver(user, payload, build_email_text(user, lead, event, payload))
            _alert_if_newly_failing(user, user.crm_last_push_status, result)
            user.crm_last_push_at = datetime.now(timezone.utc)
            user.crm_last_push_status = _summarize(result)
            db.add(user)
            await db.commit()
    except Exception:
        logger.exception("push_lead_background failed (non-fatal)")


async def send_test(db: AsyncSession, user: User) -> dict:
    """Fire a synthetic lead down whatever channels are configured, ignoring crm_push_enabled, so
    the agent can verify the connection from Settings before switching it on."""
    fake_lead = Lead(
        id="test-lead",
        owner_id=user.id,
        name="Test Lead (OpenHousePal)",
        email="test-lead@openhousepal.com",
        phone="(555) 010-0000",
        represented="unknown",
        status="new",
        tags=[],
    )
    fake_event = LeadEvent(
        lead_id="test-lead",
        source_type="open_house_visitor",
        summary="This is a test lead sent from OpenHousePal Settings to confirm your CRM connection works.",
        property_address="123 Test Street",
        occurred_at=datetime.now(timezone.utc),
    )
    payload = build_payload(user, fake_lead, fake_event)
    result = await _deliver(user, payload, build_email_text(user, fake_lead, fake_event, payload))
    user.crm_last_push_at = datetime.now(timezone.utc)
    user.crm_last_push_status = "test - " + _summarize(result)
    db.add(user)
    await db.commit()
    return result
