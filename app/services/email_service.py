import os
import re
import httpx
import json
from email.utils import formataddr
from typing import Tuple, Optional, Dict, Any
from dotenv import load_dotenv
from app.config.logging import get_logger
from datetime import datetime, timezone

load_dotenv()

logger = get_logger(__name__)

# Buyer / guest emails that carry the Showcase "Unsubscribe" (List-Unsubscribe + one-click).
# Everything else - sign-up codes, password resets, billing, and every notification to an agent -
# must NOT: it put an Unsubscribe button on password resets, and an agent who tapped it got their
# own address blacklisted (which then silently dropped lead emails queued for them).
SHOWCASE_UNSUBSCRIBE_TEMPLATES = {
    "showcase_daily_digest", "showcase_weekly_digest", "showcase_ready", "showcase_shared", "visitor_confirmation", "agent_comment_response",
}
# Spotlight alert emails have their own per-subscription unsubscribe link (unsubscribe_url).
OWN_UNSUBSCRIBE_TEMPLATES = {"spotlight_alert_confirm", "spotlight_listing_update"}

# The Oct 2026 redesign (new logo, agent-branded client emails) lives in Mailgun under "<name>_v2",
# next to the old templates, so the site that is live before this code ships keeps sending its own
# versions untouched. Once this code is live everywhere, the old un-suffixed templates can go.
REDESIGNED_TEMPLATES = {
    "account_approved", "agent_comment_response", "agent_welcome", "calendar_disconnected", "crm_push_failing",
    "direct_host_request", "domain_verification_failed", "domain_verified", "form_submission_received",
    "host_request_approved", "host_request_countered", "host_request_declined", "host_request_received", "host_request_update",
    "liked_homes_roundup", "new_lead_captured", "new_review_received", "open_house_form_request",
    "open_house_recap", "password_reset", "payment_failed", "property_comment", "refine_search_request",
    "showcase_daily_digest", "showcase_daily_digest_agent", "showcase_ready", "showcase_shared", "showcase_weekly_digest", "similar_property_visitor_message",
    "similar_property_visitor_tour_request", "spotlight_alert_confirm", "spotlight_listing_update",
    "spotlight_weekly_report", "studio_media_failed", "studio_media_ready", "subscription_cancelled",
    "subscription_plan_changed", "subscription_suspended", "support_ticket_reply", "team_broadcast_claimed",
    "team_broadcast_received", "team_invite_accepted", "team_invite_received", "team_new_listing",
    "tour_confirmed", "tour_request", "trial_ending_soon", "verify_code", "visitor_confirmation",
}
TEMPLATE_VERSION_SUFFIX = "_v2"


def mailgun_template_name(template: str) -> str:
    """The name to ask Mailgun for (the app always uses the plain name everywhere else)."""
    return f"{template}{TEMPLATE_VERSION_SUFFIX}" if template in REDESIGNED_TEMPLATES else template


# MLS photo hosts that also serve https - email clients warn about / block plain-http images.
_HTTPS_IMAGE_HOSTS = ("http://bright-media01.prd.brightmls.com/", "http://bright-media02.prd.brightmls.com/",
                      "http://bright-media.prd.brightmls.com/")


_DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
_ADDRESS_KEYS = ("address", "street", "city")
_NAME_KEYS = {"visitor_name", "recipient_name", "client_name", "respondent_name", "lead_name", "reviewer_name",
              "guest_name", "member_name", "requester_name", "owner_name", "host_name", "name"}


def _irish(text: str) -> str:
    return re.sub(r"\b([OD])'([a-z])", lambda m: f"{m.group(1)}'{m.group(2).upper()}", text)


def tidy_caps(text: str) -> str:
    """MLS ALL-CAPS -> "1650 Skinners Turn Rd, Wayne, PA 19087" (same idea as the app's tidyAllCaps):
    keeps state codes after a comma, street directions (N, NE...) and unit letters, "5TH" -> "5th".
    Anything that already has lowercase letters is returned untouched."""
    if not isinstance(text, str) or not text or re.search(r"[a-z]", text) or not re.search(r"[A-Z]{2}", text):
        return text
    out, after_comma = [], False
    for w in re.split(r"(\s+|,)", text):
        if w == ",":
            after_comma = True
        elif not w or w.isspace():
            pass
        elif (re.fullmatch(r"[A-Z]{2}", w) and after_comma) or w in _DIRECTIONS or re.fullmatch(r"\d+[A-Z]?", w):
            pass
        elif re.fullmatch(r"\d+(ST|ND|RD|TH)", w):
            w = w.lower()
        else:
            w = re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), w)
        out.append(w)
    return _irish("".join(out))


def tidy_person_name(name: str) -> str:
    """"casey morgan" / "CASEY MORGAN" -> "Casey Morgan". Mixed case is the person's own choice
    (McDonald, DeAngelo) and stays as typed."""
    if not isinstance(name, str) or not name.strip():
        return name
    if name != name.lower() and name != name.upper():
        return name
    return _irish(re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), name))


def _pretty_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if re.fullmatch(r"[\d\s().+-]+", value or "") and (len(digits) == 10 or (len(digits) == 11 and digits[0] == "1")):
        d = digits[-10:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return value


def _tidy_values(value, key: str = ""):
    """Reader-facing polish for every email: MLS caps addresses, lowercase names, bare phone digits."""
    if isinstance(value, dict):
        return {k: _tidy_values(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_tidy_values(v, key) for v in value]
    if isinstance(value, str) and key:
        k = key.lower()
        if any(t in k for t in _ADDRESS_KEYS) and not value.startswith("http"):
            return tidy_caps(value)
        if k in _NAME_KEYS:
            return tidy_person_name(value)
        if k.endswith("phone"):
            return _pretty_phone(value)
    return value


def _tidy_subject(subject: str) -> str:
    # an all-caps street address inside a sentence ("... wants to host 1650 SKINNERS TURN RD"),
    # with the town/state after it ("..., WAYNE, PA 19087") and numbered streets ("N 5TH ST")
    word = r"(?:\d+(?:ST|ND|RD|TH)\b|[A-Z][A-Z0-9'.#-]*|#?\d+[A-Z]?)"
    pattern = rf"\b\d+[A-Z]{{0,2}}(?:\s+{word})+(?:,\s*[A-Z][A-Z'.-]*(?:\s+[A-Z][A-Z'.-]*)*)*(?:\s+\d{{5}})?"
    out = re.sub(pattern, lambda m: tidy_caps(m.group(0)), subject or "")
    # an empty unit number from the MLS: "1330 N Avignon Dr #, Gladwyne" -> "1330 N Avignon Dr, Gladwyne"
    return re.sub(r"\s+#(?=\s*(?:,|$|\s-\s|\)))", "", out)


def _secure_urls(value):
    if isinstance(value, str):
        return "https://" + value[len("http://"):] if value.startswith(_HTTPS_IMAGE_HOSTS) else value
    if isinstance(value, list):
        return [_secure_urls(v) for v in value]
    if isinstance(value, dict):
        return {k: _secure_urls(v) for k, v in value.items()}
    return value

def prepare_template_email(subject: str, template_variables: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """The same clean-up every template email gets, whoever sends it (Mailgun or an agent's mailbox)."""
    variables = _tidy_values(_secure_urls(dict(template_variables or {})))
    if "today_date" not in variables:
        variables["today_date"] = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    return _tidy_subject(subject), variables


def unsubscribe_headers(template: str, to_email: str, template_variables: Dict[str, Any]) -> Dict[str, str]:
    """List-Unsubscribe headers for the guest emails that carry them (RFC 8058 one-click)."""
    from urllib.parse import quote
    client_url = os.getenv('CLIENT_URL', 'https://openhousepal.com')
    api_url = os.getenv('API_URL', 'https://api.openhousepal.com')
    if template in SHOWCASE_UNSUBSCRIBE_TEMPLATES:
        e = quote(to_email or "", safe="")
        return {
            "List-Unsubscribe": f"<{api_url.rstrip('/')}/collections/unsubscribe/one-click?email={e}>, <{client_url.rstrip('/')}/unsubscribe?email={e}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    if template in OWN_UNSUBSCRIBE_TEMPLATES and template_variables.get("unsubscribe_url"):
        return {"List-Unsubscribe": f"<{template_variables['unsubscribe_url']}>"}
    return {}


def sender_address(configured: Optional[str]) -> str:
    """MAILGUN_FROM may be a bare address or "OpenHousePal <noreply@mg...>" - always the bare address,
    so "{name} <{MAILGUN_FROM}>" can never become the invalid "Name <OpenHousePal <noreply@...>>"."""
    from email.utils import parseaddr
    return parseaddr(configured or "")[1] or (configured or "").strip()


def agent_sender_address(agent_name: str, fallback: str) -> str:
    """"Peter Stefanatos" + "noreply@mg.openhousepal.com" -> "peter.stefanatos@mg.openhousepal.com"."""
    import re as _re
    domain = fallback.split("@")[-1] if "@" in fallback else ""
    local = _re.sub(r"[^a-z0-9]+", ".", (agent_name or "").lower()).strip(".")[:48]
    return f"{local}@{domain}" if local and domain else fallback


class EmailService:
    def __init__(self):
        self.mailgun_url = os.getenv("MAILGUN_URL")
        self.mailgun_api_key = os.getenv('MAILGUN_API_KEY')
        self.mailgun_from = os.getenv("MAILGUN_FROM")
        # Unset MAILGUN_DEV follows DEV: dev never sends, production always does (a missing
        # variable must not silently switch off every email in production).
        self.is_dev = os.getenv("MAILGUN_DEV", "yes" if os.getenv("DEV", "false") == "true" else "no") == "yes"

    def send_simple_message(
        self,
        to_email: str,
        subject: str,
        template: str,
        template_variables: Dict[str, Any],
        reply_to: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> Tuple[int, str]:
        # Counted for the admin Insights Emails page (email_log_service) - who it's for and whose
        # it is ride along as private variables and never reach the template.
        template_variables = dict(template_variables or {})
        owner = template_variables.pop("_log_owner", None)
        audience = template_variables.pop("_log_audience", None)
        track_id = template_variables.get("_track_id")
        code, body = self._send_simple_message(to_email, subject, template, template_variables, reply_to, from_name)
        from app.services import email_log_service
        email_log_service.record(
            template, "client" if track_id else (audience or "account"), ok=code < 300, held=self.is_dev,
            owner_id=owner, client_email_id=track_id,
            provider_id=None if self.is_dev else email_log_service.provider_id_from(body),
        )
        return code, body

    def _send_simple_message(
        self,
        to_email: str,
        subject: str,
        template: str,
        template_variables: Dict[str, Any],
        reply_to: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> Tuple[int, str]:
        # Inject today's date for all templates
        template_variables = dict(template_variables or {})
        track_id = template_variables.pop("_track_id", None)   # client_email_service tracking id
        ics = template_variables.pop("_ics", None)              # calendar_invite_service - attached, not a template var
        template_variables = _tidy_values(_secure_urls(template_variables))
        subject = _tidy_subject(subject)
        if "today_date" not in template_variables:
            template_variables["today_date"] = datetime.now(timezone.utc).strftime("%m/%d/%Y")

        if self.is_dev:
            logger.info(
                "DEV MODE: Email suppressed", 
                extra={
                    "to": to_email, 
                    "subject": subject, 
                    "template": template, 
                    "variables": template_variables,
                    "reply_to": reply_to
                }
            )
            return 200, "Email suppressed in dev mode"

        try:
            client_url = os.getenv('CLIENT_URL', 'https://openhousepal.com')
            api_url = os.getenv('API_URL', 'https://api.openhousepal.com')
            
            # 1. Professional "From" name - e.g., "Sarah from OpenHousePal"
            # An explicit sender name wins (notification emails to the agent come from
            # "OpenHousePal", not from the agent's own name).
            agent_name = template_variables.get("agent_name")
            from_name = from_name or (f"{agent_name}" if agent_name else "OpenHousePal")
            
            from_addr = sender_address(self.mailgun_from)
            from app.services.personal_email_service import PERSONAL_TEMPLATES
            if template in PERSONAL_TEMPLATES and template_variables.get("agent_name"):
                # A personal note reads as one: "Peter Stefanatos <peter.stefanatos@mg...>", not "noreply@".
                from_addr = agent_sender_address(template_variables["agent_name"], from_addr)
            data = {
                "from": formataddr((from_name, from_addr)),
                "to": to_email,
                "subject": subject,
                "o:tag": template,
                "o:tracking": "no",
            }
            if template in PERSONAL_TEMPLATES:
                from app.services.personal_email_service import render as render_personal
                link = template_variables.get("showcase_link") or template_variables.get("collection_link") or ""
                data["subject"], data["html"], data["text"] = render_personal(template, subject, template_variables, link=link)
            else:
                data.update({
                    "template": mailgun_template_name(template),
                    # Plain-text part rendered from the same template by Mailgun. It used to be one
                    # generic "You have a new update regarding your showcase" text for EVERY email -
                    # including sign-up codes (no code in it) and password resets.
                    "t:text": "yes",
                    "t:variables": json.dumps(template_variables),
                })
            if track_id:
                data["v:ce"] = track_id   # comes back on Mailgun's bounce / complaint webhooks
            from urllib.parse import quote
            if template in SHOWCASE_UNSUBSCRIBE_TEMPLATES:
                # RFC 8058 one-click (Gmail/Yahoo bulk-sender rule). The API serves /collections/...
                # with no /api prefix - the old /api/collections/... address answered 404.
                e = quote(to_email or "", safe="")
                data["h:List-Unsubscribe"] = f"<{api_url.rstrip('/')}/collections/unsubscribe/one-click?email={e}>, <{client_url.rstrip('/')}/unsubscribe?email={e}>"
                data["h:List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
            elif template in OWN_UNSUBSCRIBE_TEMPLATES and template_variables.get("unsubscribe_url"):
                data["h:List-Unsubscribe"] = f"<{template_variables['unsubscribe_url']}>"

            if reply_to:
                data["h:Reply-To"] = reply_to

            files = [("attachment", ("invite.ics", ics.encode("utf-8"), "text/calendar; method=REQUEST; charset=UTF-8"))] if ics else None
            response = httpx.post(
                self.mailgun_url,
                auth=("api", self.mailgun_api_key),
                data=data,
                files=files,
                timeout=10.0
            )
            return response.status_code, response.text
        except Exception as e:
            logger.error("Error sending email", exc_info=True, extra={"template": template})
            return 500, str(e)

    def send_raw_message(
        self,
        to_email: str,
        subject: str,
        text: str,
        *,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> Tuple[int, str]:
        """Send a plain-text email with NO Mailgun template - the body is exactly `text`.

        Used where the recipient is a machine that parses the body itself (a CRM's
        lead-parsing inbox), so template HTML would only get in the way. Dev mode logs and
        suppresses, same as send_simple_message.
        """
        if self.is_dev:
            logger.info(
                "DEV MODE: Raw email suppressed",
                extra={"to": to_email, "subject": subject, "text": text, "reply_to": reply_to},
            )
            return 200, "Email suppressed in dev mode"

        if not (self.mailgun_url and self.mailgun_api_key and self.mailgun_from):
            logger.error("send_raw_message: Mailgun is not configured")
            return 500, "Email is not configured"

        try:
            data = {
                "from": formataddr((from_name or "OpenHousePal", sender_address(self.mailgun_from))),
                "to": to_email,
                "subject": subject,
                "text": text,
                "o:tracking": "no",
            }
            if reply_to:
                data["h:Reply-To"] = reply_to
            response = httpx.post(
                self.mailgun_url,
                auth=("api", self.mailgun_api_key),
                data=data,
                timeout=10.0,
            )
            return response.status_code, response.text
        except Exception as e:
            logger.error("Error sending raw email", exc_info=True)
            return 500, str(e)
