"""Builds a standards-compliant vCard (RFC 6350, v3.0 for the widest device support) for an
Agent Hub's "Save Contact" button. v3.0 is used instead of v4.0 because iOS/macOS Contacts and
most Android contact apps still parse it more reliably, especially for an embedded PHOTO.

The agent controls exactly what goes in via config["contactCard"]["fields"] (see
CONTACT_CARD_FIELD_DEFAULTS below) - saved through the same PUT /agent-hub/me/config the rest of
the Agent Hub page-builder payload already goes through, no separate endpoint needed. Fields that
already exist elsewhere on the Agent Hub (name, title, brokerage, phone/email, website, social
links, headshot) are just an include/exclude toggle here - the VALUE is always read live from the
existing profile/contact/socialLinks data so the agent never re-enters it and it can't go stale.
Fields with no existing home on the Agent Hub (team, office phone/address, LinkedIn, a custom
link, notes) also carry their own value under the field.
"""
import base64
import re
import logging
from typing import Any, Optional

import httpx

from app.models.database import AgentHub, User

logger = logging.getLogger(__name__)

# key -> (default include, whether this field carries its own `value` rather than being sourced
# live from existing profile/contact/socialLinks data)
CONTACT_CARD_FIELD_DEFAULTS: dict[str, dict[str, Any]] = {
    "headshot": {"include": True, "has_value": False},
    "jobTitle": {"include": True, "has_value": False},
    "company": {"include": True, "has_value": False},
    "team": {"include": False, "has_value": True},
    "mobilePhone": {"include": True, "has_value": False},
    "officePhone": {"include": False, "has_value": True},
    "email": {"include": True, "has_value": False},
    "website": {"include": True, "has_value": False},
    "agentHubUrl": {"include": True, "has_value": False},
    "officeAddress": {"include": False, "has_value": True},
    "instagram": {"include": False, "has_value": False},
    "linkedin": {"include": False, "has_value": True},
    "facebook": {"include": False, "has_value": False},
    "youtube": {"include": False, "has_value": False},
    "customUrl": {"include": False, "has_value": True, "has_label": True},
    "notes": {"include": False, "has_value": True},
}

PHOTO_FETCH_TIMEOUT_SECONDS = 5.0
PHOTO_MAX_BYTES = 3 * 1024 * 1024


def resolve_contact_card_fields(config: dict) -> dict[str, dict[str, Any]]:
    """Merges the agent's saved toggles/values over the defaults so every field key is always
    present, regardless of what's actually been saved yet."""
    saved = ((config or {}).get("contactCard") or {}).get("fields") or {}
    resolved: dict[str, dict[str, Any]] = {}
    for key, defaults in CONTACT_CARD_FIELD_DEFAULTS.items():
        saved_field = saved.get(key) if isinstance(saved.get(key), dict) else {}
        resolved[key] = {
            "include": bool(saved_field.get("include", defaults["include"])),
            "value": str(saved_field.get("value") or "").strip() if defaults.get("has_value") else "",
        }
        if defaults.get("has_label"):
            resolved[key]["label"] = str(saved_field.get("label") or "").strip()
    return resolved


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 6350 line folding: no physical line may exceed 75 octets, continuations start with a
    single space. Long PHOTO/NOTE lines routinely blow past this without it."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    start = 0
    limit = 75
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        # Never split a multi-byte UTF-8 sequence in half.
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(encoded[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines lose one column to the leading space
    return "\r\n ".join(parts)


async def fetch_photo_base64(photo_url: Optional[str]) -> Optional[str]:
    """Best-effort headshot fetch for PHOTO embedding - any failure (network, oversized, non-image
    response) just means the vCard ships without a photo rather than failing the whole download."""
    if not photo_url or not photo_url.startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(timeout=PHOTO_FETCH_TIMEOUT_SECONDS) as client:
            response = await client.get(photo_url)
        if response.status_code != 200:
            return None
        content_type = response.headers.get("content-type", "")
        if content_type and not content_type.startswith("image/"):
            return None
        if len(response.content) > PHOTO_MAX_BYTES:
            return None
        return base64.b64encode(response.content).decode("ascii")
    except Exception as exc:
        logger.warning("vCard photo fetch failed", extra={"photo_url": photo_url, "error": str(exc)})
        return None


def build_vcard_text(
    hub: AgentHub,
    owner: User,
    agent_hub_url: str,
    photo_base64: Optional[str] = None,
    config: Optional[dict] = None,
) -> str:
    # `config`: the hub config as read live (the Website link filled in) - the saved one if not given.
    config = config if config is not None else (hub.config or {})
    profile = config.get("profile") or {}
    contact = config.get("contact") or {}
    social_links = config.get("socialLinks") or []
    fields = resolve_contact_card_fields(config)

    def social(social_id: str) -> str:
        for link in social_links:
            if isinstance(link, dict) and link.get("id") == social_id and link.get("enabled") and str(link.get("url") or "").strip():
                url = str(link["url"]).strip()
                return url if url.startswith("http") else f"https://{url}"
        return ""

    display_name = (profile.get("name") or "").strip() or f"{owner.first_name} {owner.last_name}".strip() or "Agent"
    first_name = (owner.first_name or "").strip()
    last_name = (owner.last_name or "").strip()

    lines = ["BEGIN:VCARD", "VERSION:3.0"]
    lines.append(f"N:{_escape(last_name)};{_escape(first_name)};;;")
    lines.append(f"FN:{_escape(display_name)}")

    org_parts = []
    if fields["company"]["include"] and (profile.get("brokerage") or "").strip():
        org_parts.append(profile["brokerage"].strip())
    team = (profile.get("team") or "").strip() or fields["team"]["value"]
    if fields["team"]["include"] and team:
        org_parts.append(team)
    if org_parts:
        lines.append(f"ORG:{';'.join(_escape(p) for p in org_parts)}")

    if fields["jobTitle"]["include"] and (profile.get("title") or "").strip():
        lines.append(f"TITLE:{_escape(profile['title'].strip())}")

    if fields["mobilePhone"]["include"] and (contact.get("call") or "").strip():
        lines.append(f"TEL;TYPE=CELL:{_escape(contact['call'].strip())}")
        text = (contact.get("text") or "").strip()
        if text and re.sub(r"\D", "", text) != re.sub(r"\D", "", contact["call"]):
            # A separate texting number - labelled "Text" on iPhone (other phones show it as a 2nd mobile).
            lines.append(f"item9.TEL;TYPE=CELL:{_escape(text)}")
            lines.append("item9.X-ABLabel:Text")
    # The agent's OWN office line only - never the brokerage's main number (see hub_phones).
    from app.services.hub_phones import office_phone
    office = office_phone(config)
    if fields["officePhone"]["include"] and office:
        lines.append(f"TEL;TYPE=WORK:{_escape(office)}")

    if fields["email"]["include"] and (contact.get("email") or "").strip():
        lines.append(f"EMAIL;TYPE=INTERNET:{_escape(contact['email'].strip())}")

    if fields["officeAddress"]["include"] and fields["officeAddress"]["value"]:
        lines.append(f"ADR;TYPE=WORK:;;{_escape(fields['officeAddress']['value'])};;;;")

    if fields["notes"]["include"] and fields["notes"]["value"]:
        lines.append(f"NOTE:{_escape(fields['notes']['value'])}")

    # Labeled links (website, Agent Hub, socials, custom) as Apple-style grouped item.URL +
    # item.X-ABLabel pairs - RFC 6350 property grouping, so a parser that doesn't understand the
    # label just sees an ordinary URL and ignores the group prefix.
    item_index = 0
    def add_labeled_url(url: str, label: str):
        nonlocal item_index
        if not url:
            return
        item_index += 1
        lines.append(f"item{item_index}.URL:{_escape(url)}")
        lines.append(f"item{item_index}.X-ABLabel:{_escape(label)}")

    if fields["website"]["include"]:
        add_labeled_url(social("website"), "Website")
    if fields["agentHubUrl"]["include"] and agent_hub_url:
        add_labeled_url(agent_hub_url, "Agent Hub")
    if fields["instagram"]["include"]:
        add_labeled_url(social("instagram"), "Instagram")
    if fields["linkedin"]["include"] and fields["linkedin"]["value"]:
        linkedin_url = fields["linkedin"]["value"]
        add_labeled_url(linkedin_url if linkedin_url.startswith("http") else f"https://{linkedin_url}", "LinkedIn")
    if fields["facebook"]["include"]:
        add_labeled_url(social("facebook"), "Facebook")
    if fields["youtube"]["include"]:
        add_labeled_url(social("youtube"), "YouTube")
    if fields["customUrl"]["include"] and fields["customUrl"]["value"]:
        custom_url = fields["customUrl"]["value"]
        add_labeled_url(
            custom_url if custom_url.startswith("http") else f"https://{custom_url}",
            fields["customUrl"].get("label") or "Link",
        )

    if fields["headshot"]["include"] and photo_base64:
        lines.append(f"PHOTO;ENCODING=b;TYPE=JPEG:{photo_base64}")

    lines.append("END:VCARD")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
