"""Pull portal leads IN (Zillow / Realtor.com / Homes.com / Facebook / anything forwarded).

The agent gets one unique address - leads-<inbound_lead_token>@<INBOUND_LEAD_DOMAIN> - and points
each portal's "forward my leads to..." setting at it (or sets up a filter in their own inbox that
forwards matching mail there). An inbound-email webhook (Mailgun Routes, SendGrid Inbound Parse,
Postmark, Cloudflare Email Workers - all POST the same handful of fields) hits
POST /webhooks/inbound-email, and this module turns the forwarded message into a real Lead.

Parsing is intentionally forgiving: try the portal's known labelled format first, then fall back
to "first email + first phone + a best-guess name" so a lead is never dropped just because a
portal tweaked their template. Everything lands as source_type "portal_lead"; the specific portal
lives in the event summary + raw_snapshot.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Notification, User
from app.services.leads_service import upsert_lead

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# US-style phone: 555-123-4567 / (555) 123 4567 / +1 555.123.4567 / 5551234567
_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}")
_LABEL_RE = lambda label: re.compile(  # noqa: E731
    rf"(?im)^[\s>*\-]*{label}\s*[:\-]\s*(.+?)\s*$"
)

# Portal-owned "do not treat as the lead's own address" senders / domains.
_PORTAL_SIGNATURES = [
    ("zillow", ("zillow.com", "convo.zillow.com", "zillowgroup.com")),
    ("realtor", ("realtor.com", "leads.realtor.com", "move.com")),
    ("homes", ("homes.com", "leads.homes.com")),
    ("redfin", ("redfin.com",)),
    ("facebook", ("facebook.com", "fb.com", "meta.com")),
]


def detect_portal(sender: str, subject: str, body: str) -> str:
    hay = f"{sender}\n{subject}\n{body[:400]}".lower()
    for name, domains in _PORTAL_SIGNATURES:
        if any(d in hay for d in domains) or name in hay:
            return name
    return "other"


_PORTAL_DOMAINS = tuple(d for _n, ds in _PORTAL_SIGNATURES for d in ds) + ("trulia.com", "zillowmail.com")
# Sender-style mailboxes that are never the lead (gmail.com / hotmail.com etc. ARE leads).
_ROBOT_LOCAL = re.compile(r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|mailer-daemon|postmaster|bounce[s]?|leads?|support|info|alerts?)\b", re.I)
_FWD_MARKER = re.compile(r"^[\s>]*-{2,}\s*(?:forwarded message|original message)\s*-*\s*$", re.I)
_HEADER_LINE = re.compile(r"^[\s>*]*(from|sent|to|cc|bcc|date|subject|reply-to)\s*:\s*(.*)$", re.I)


def _digits(v: Optional[str]) -> str:
    d = re.sub(r"\D", "", v or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def _strip_forward_headers(body: str, agent_emails: set[str]) -> tuple[str, Optional[str]]:
    """Remove the header block an agent's mail app adds when forwarding (From/To/Sent/Subject of
    the forward itself - they carry the agent's and the portal's addresses, never the lead's).
    Returns (clean body, the forwarded email's own subject if one was found)."""
    out: list[str] = []
    inner_subject: Optional[str] = None
    for line in body.splitlines():
        if _FWD_MARKER.match(line):
            continue
        m = _HEADER_LINE.match(line)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            low = val.lower()
            if key == "subject":
                inner_subject = inner_subject or val
                continue
            if key in ("sent", "date", "to", "cc", "bcc", "reply-to"):
                continue
            if key == "from" and ("<" in val or "@" in val or any(d in low for d in _PORTAL_DOMAINS) or any(a in low for a in agent_emails)):
                continue
        out.append(line)
    return "\n".join(out), inner_subject


def _usable_email(addr: str, agent_emails: set[str]) -> bool:
    low = addr.lower()
    local, _, domain = low.partition("@")
    if low in agent_emails or "openhousepal" in domain:
        return False
    if any(domain == d or domain.endswith("." + d) for d in _PORTAL_DOMAINS):
        return False
    return not _ROBOT_LOCAL.match(local)


def _first(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v and v.strip():
            return v.strip()
    return None


def _label(body: str, *labels: str) -> Optional[str]:
    for label in labels:
        m = _LABEL_RE(label).search(body)
        if m:
            val = m.group(1).strip()
            # strip a trailing "(via Zillow)" etc.
            val = re.sub(r"\s*\((?:via|from)\b.*?\)\s*$", "", val, flags=re.I).strip()
            if val and val.lower() not in ("n/a", "none", "-"):
                return val
    return None


def _clean_name(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip().strip('"').strip()
    # "New Contact: Jane Doe" / "New Zillow lead - Jane Doe" -> "Jane Doe"
    raw = re.sub(r"^(?:new\s+)?(?:[\w.]+\s+)?(?:contact|lead|inquiry|message)(?:\s+from)?\s*[:\-]\s*", "", raw, flags=re.I).strip()
    raw = re.sub(r"^(?:zillow|trulia|realtor\.com|homes\.com|redfin|facebook)\s*[:\-]\s*", "", raw, flags=re.I).strip()
    # drop an email or phone that snuck into the name line
    raw = _EMAIL_RE.sub("", raw)
    raw = _PHONE_RE.sub("", raw)
    raw = re.sub(r"\s{2,}", " ", raw).strip(" -,|")
    if not raw or "@" in raw or len(raw) > 80:
        return None
    return raw


def _name_from_subject(subject: str) -> Optional[str]:
    s = subject.strip()
    # "New Zillow lead: Jane Doe" / "Jane Doe is interested in 12 Oak St" / "Lead from Jane Doe"
    m = re.search(r"(?:lead(?: from| for)?|contact(?: from)?|inquiry from|message from|request from)\s*[:\-]\s*(.+)$", s, re.I) \
        or re.search(r"(?:inquiry|message|request) from\s+(.+)$", s, re.I)
    if m:
        return _clean_name(m.group(1))
    m = re.match(r"\s*(.+?)\s+is interested in\b", s, re.I)
    if m:
        return _clean_name(m.group(1))
    return None


def _address_from(subject: str, body: str) -> Optional[str]:
    lab = _label(body, "Property", "Address", "Listing", "Regarding", "Home", "Interested in", "Property Address")
    if lab:
        return lab
    for text in (subject, body):
        m = re.search(r"interested in\s+(?:the\s+(?:property|home|listing)\s+at\s+)?(\d[^\n?!]*?)(?:\.\s|[?!]|\.$|$|\n)", text, re.I | re.M)
        if m:
            cand = m.group(1).strip(" .")
            if any(ch.isdigit() for ch in cand) and len(cand) <= 160:
                return cand
    return None


_NAME_LINE = re.compile(r"^[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){1,3}$")
_BOILER = re.compile(r"(?i)^(?:you have a new|new (?:contact|lead|inquiry)|reply to this|view (?:this|the) lead|unsubscribe|this (?:email|message) was sent)")


def _name_near(body: str, email: Optional[str], phone: Optional[str]) -> Optional[str]:
    """Unlabelled emails (Zillow's) put the name on its own line right above the email/phone."""
    lines = [l.strip() for l in body.splitlines()]
    for i, line in enumerate(lines):
        if (email and email in line) or (phone and phone in line):
            for j in range(i - 1, max(-1, i - 3), -1):
                cand = lines[j]
                if cand and _NAME_LINE.match(cand) and not _BOILER.match(cand):
                    return cand
            break
    return None


def _message_from(body: str, agent_emails: set[str], agent_phones: set[str]) -> Optional[str]:
    """The person's own words: a line/paragraph that reads like a question or request, never the
    portal's boilerplate or the agent's own note/signature."""
    best = None
    for para in re.split(r"\n\s*\n|\n", body):
        p = para.strip()
        if not (12 <= len(p) <= 600) or _BOILER.match(p) or _EMAIL_RE.search(p) or ":" in p[:20]:
            continue
        if any(_digits(m.group(0)) in agent_phones for m in _PHONE_RE.finditer(p)) or _PHONE_RE.fullmatch(p):
            continue
        if "?" in p or re.search(r"(?i)\b(interested|tour|showing|schedule|available|call|info|see)\b", p):
            return p
        best = best or p
    return best


def parse(sender: str, subject: str, body_plain: str, body_html: str = "", *,
          agent_emails: Optional[set[str]] = None, agent_phones: Optional[set[str]] = None) -> Optional[dict]:
    """Extract {name, email, phone, message, portal, property_address} from a forwarded portal
    email. Returns None only if there's nothing usable (no email AND no phone). The agent's own
    addresses/phones (they're in every forward) are never taken as the lead's."""
    agent_emails = {e.lower() for e in (agent_emails or set()) if e}
    agent_phones = {_digits(p) for p in (agent_phones or set()) if _digits(p)}
    subject = (subject or "").strip()
    body = (body_plain or "").strip()
    if not body and body_html:
        body = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h\d)>", "\n", body_html)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"&nbsp;?", " ", body)
        body = re.sub(r"&amp;", "&", body)
        body = re.sub(r"[ \t]{2,}", " ", body)

    portal = detect_portal(sender or "", subject, body)
    body, inner_subject = _strip_forward_headers(body, agent_emails)
    # "Fwd: New Zillow lead: Jane" -> read the forwarded email's own subject
    if inner_subject and re.match(r"^\s*(?:fwd?|fw)\s*:", subject, re.I):
        subject = inner_subject
    subject = re.sub(r"^\s*(?:(?:fwd?|fw|re)\s*:\s*)+", "", subject, flags=re.I)

    name = _clean_name(_label(body, "Name", "Lead Name", "Contact", "From", "Full Name")) or _name_from_subject(subject)

    labelled_email = _label(body, "Email", "E-mail", "Email Address")
    lm = _EMAIL_RE.search(labelled_email or "")
    email = lm.group(0) if (lm and _usable_email(lm.group(0), agent_emails)) else None
    if not email:
        for cand in _EMAIL_RE.findall(body):
            if _usable_email(cand, agent_emails):
                email = cand
                break

    def _phone_ok(p: str) -> bool:
        return _digits(p) not in agent_phones and len(_digits(p)) == 10

    labelled_phone = _label(body, "Phone", "Phone Number", "Mobile", "Cell", "Tel")
    phone = None
    if labelled_phone:
        pm = _PHONE_RE.search(labelled_phone)
        phone = pm.group(0) if pm and _phone_ok(pm.group(0)) else None
    if not phone:
        for pm in _PHONE_RE.finditer(body):
            if _phone_ok(pm.group(0)):
                phone = pm.group(0)
                break

    message = _label(body, "Message", "Comments", "Note", "Notes", "Inquiry", "Question") or _message_from(body, agent_emails, agent_phones)
    if not name:
        name = _name_near(body, email, phone)
    if not name:
        m = re.match(r"\s*([A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){1,2})\s+(?:would like|wants|is interested|requested|asked)\b", body)
        name = m.group(1) if m else None

    if not email and not phone:
        return None

    return {
        "portal": portal,
        "name": name,
        "email": email,
        "phone": phone,
        "message": message,
        "property_address": _address_from(subject, body),
    }


_PORTAL_TITLES = {
    "zillow": "Zillow",
    "realtor": "Realtor.com",
    "homes": "Homes.com",
    "redfin": "Redfin",
    "facebook": "Facebook",
    "other": "Portal",
}


async def resolve_user_by_token(db: AsyncSession, token: str) -> Optional[User]:
    if not token:
        return None
    return (await db.execute(select(User).where(User.inbound_lead_token == token))).scalar_one_or_none()


async def _agent_identity(db: AsyncSession, user: User) -> tuple[set[str], set[str]]:
    """The agent's own addresses and numbers - they appear in every forwarded email."""
    from app.models.database import AgentHub
    emails = {user.email.lower()} if user.email else set()
    phones: set[str] = set()
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    cfg = (hub.config if hub else None) or {}
    contact = cfg.get("contact") or {}
    if isinstance(contact.get("email"), str) and contact["email"].strip():
        emails.add(contact["email"].strip().lower())
    for k in ("call", "text"):
        if isinstance(contact.get(k), str) and contact[k].strip():
            phones.add(contact[k])
    office = (((cfg.get("contactCard") or {}).get("fields") or {}).get("officePhone") or {}).get("value")
    if isinstance(office, str) and office.strip():
        phones.add(office)
    return emails, phones


async def ingest(db: AsyncSession, token: str, *, sender: str, subject: str,
                 body_plain: str, body_html: str = "") -> dict:
    """Resolve the agent from the inbound token, parse the forwarded email, create the lead +
    a bell notification. Returns a small status dict (the webhook always answers 200 anyway)."""
    user = await resolve_user_by_token(db, token)
    if not user:
        return {"status": "unknown_token"}

    agent_emails, agent_phones = await _agent_identity(db, user)
    parsed = parse(sender or "", subject or "", body_plain or "", body_html or "",
                   agent_emails=agent_emails, agent_phones=agent_phones)
    if not parsed:
        logger.info("portal inbound: unparseable message for user %s (subject=%r)", user.id, subject)
        return {"status": "unparseable"}

    portal_title = _PORTAL_TITLES.get(parsed["portal"], "Portal")
    summary = f"{portal_title} lead"
    if parsed["property_address"]:
        summary += f" - interested in {parsed['property_address']}"

    lead = await upsert_lead(
        db,
        owner_id=user.id,
        name=parsed["name"],
        email=parsed["email"],
        phone=parsed["phone"],
        source_type="portal_lead",
        source_id=None,
        summary=summary,
        occurred_at=datetime.now(timezone.utc),
        property_address=parsed["property_address"],
        raw_snapshot={
            "portal": parsed["portal"],
            "portal_title": portal_title,
            "subject": subject,
            "sender": sender,
            "message": parsed["message"],
        },
        commit=True,
    )
    if not lead:
        return {"status": "no_contact"}

    try:
        db.add(Notification(
            agent_id=user.id,
            type="PORTAL_LEAD",
            reference_type="LEAD",
            reference_id=lead.id,
            title=f"New {portal_title} lead",
            message=f"{parsed['name'] or parsed['email'] or 'A new lead'} came in from {portal_title}.",
            visitor_name=parsed["name"],
            property_address=parsed["property_address"],
            link=f"/leads/{lead.id}",
            is_read=False,
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        from app.services.agent_email_service import absolute, email_agent
        who = parsed["name"] or parsed["email"] or "New lead"
        email_agent(user, "PORTAL_LEAD", "new_lead_captured", f"New {portal_title} lead: {who}",
                    {"lead_name": who, "lead_source": portal_title,
                     "lead_email": parsed.get("email"), "lead_phone": parsed.get("phone"), "lead_message": parsed.get("message"),
                     "property_address": parsed.get("property_address"), "link": absolute(f"/leads/{lead.id}")})
    except Exception:
        logger.exception("portal inbound: notification failed (lead still created)")
        await db.rollback()

    return {"status": "ok", "lead_id": lead.id, "portal": parsed["portal"]}
