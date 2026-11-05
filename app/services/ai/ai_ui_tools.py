"""Chat-only tools that SHOW something instead of looking anything up (owner, 10/30): tap-to-answer
choices when the helper needs the agent to pick, and "Go there" buttons to the exact page.
They ride on the reply like action cards (AIMessage.tool_calls), never touch data, and a link
can only point inside the app (checked against the app's own sections - never another site)."""
import re
import uuid
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode

_PAGES = ("/dash", "/discovery", "/open-houses", "/showcases", "/leads", "/activity", "/studio", "/settings",
          "/agent-hub", "/setup", "/assistant", "/broker-authorization", "/checkout")
_SAFE = re.compile(r"^/[A-Za-z0-9/_\-?=&%.,+]*$")
# The real pages one level down (frontend app/ folders) - a guessed "/studio/crm" was let through
# and opened a dead page (11/01 capability review). Anything deeper under these is the page's own.
_SUBPAGES = {
    "/studio": {"blogs", "brand-kit", "calendar", "cma", "design", "domains", "email-signature", "email", "forms", "integrations",
                "media", "property-sites", "reviews", "site", "tools", "website"},
    "/settings": {"connected", "help", "subscription"},
    "/open-houses": {"my-listings", "visitors"},
}

LINK_HELP = (
    "Handy pages (add the ? options to open them ready to go): /dash, /open-houses (My Opens - kits), "
    "/open-houses?kit=<street address, town> (start an Open House Kit for that home), /open-houses/my-listings (Manager), /discovery, /showcases, /showcases?new=1 (new Showcase), /showcases?showcase=<collection_id>, "
    "/leads, /leads/<lead_id>, /leads/tasks (Follow-ups), /leads/tasks?task=<task_id>, /leads/emails (Email Tracker), /activity, "
    "/studio (Tools), /studio/design?new=1 (new design), /studio/design?address=<street address> (new design for that home), "
    "/studio/property-sites?new=1 (new Spotlight), /studio/property-sites/<spotlight_id>, /studio/cma?new=1 (new CMA), /studio/cma/<report_id>, "
    "/studio/blogs?new=1 (new blog post), /studio/forms (Forms & Invites), /studio/forms?new=1 (new custom form), /studio/media (Media Lab), /studio/media/new (new AI media), /studio/brand-kit (Branding), "
    "/studio/email (Email settings + briefing time), /studio/email-signature, /studio/calendar, /studio/reviews, /studio/website (IDX website), "
    "/studio/site (Agent Hub editor), /studio/integrations (CRM - Follow Up Boss, webhooks, lead email address), /studio/domains (Domains), /studio/tools (seller net sheet, buyer cash needed), "
    "/open-houses/visitors/<open_house_id> (that open house's Visitors), /settings, /settings/connected (Send from your email - Gmail/Outlook), "
    "/settings/help (Help), /settings/help?compose=1&category=bug, /settings/subscription (Billing), /setup (Account setup), and the public pages /tools, /pricing, /idx-websites, /faq, /about, /resources, /brokerages."
)

UI_TOOL_DEFINITIONS = [
    {
        "name": "offer_choices",
        "description": "Show the agent tap-to-answer buttons under your reply when you need them to pick (2-10 short options, each under 40 characters - list every real option, e.g. all 10 design kinds), or ONE do-it-here button after a how-to answer for something you can do in this chat ('Make a Showcase here'). Tapping one sends it as their answer. Ask the question in your reply text too.",
        "input_schema": {"type": "object", "properties": {"options": {"type": "array", "items": {"type": "string"}}}, "required": ["options"]},
    },
    {
        "name": "show_link",
        "description": "Show a 'Go there' button under your reply that opens a page in OpenHousePal - use it whenever you tell them where something is or when the page itself is the best way to finish. "
                       "ALWAYS for a 'how do I make / create / start X' question (owner, 10/31): one button that opens X's create screen itself, as if they'd clicked its Create button - "
                       "label it 'Create a Showcase', 'Make a design', 'Start an Open House Kit'... (e.g. /showcases?new=1, /studio/design?new=1, /studio/forms?new=1, /studio/cma?new=1, /studio/blogs?new=1, /studio/property-sites?new=1, /open-houses?kit=<address>). "
                       "Keep the steps short; the button is the main answer. " + LINK_HELP,
        "input_schema": {"type": "object", "properties": {"label": {"type": "string"}, "page": {"type": "string"}}, "required": ["label", "page"]},
    },
]
UI_TOOL_NAMES = {t["name"] for t in UI_TOOL_DEFINITIONS}


def safe_page(page: str) -> Optional[str]:
    page = (page or "").strip()
    if "?" in page:  # "?kit=12 Oak St, Malvern" - encode the values the model wrote in plain words
        path, query = page.split("?", 1)
        page = path + "?" + urlencode(parse_qsl(query, keep_blank_values=True))
    if not page.startswith("/") or "//" in page or ".." in page or not _SAFE.match(page):
        return None
    path = page.split("?", 1)[0]
    if not any(path == p or path.startswith(p + "/") for p in _PAGES):
        return None
    for area, subs in _SUBPAGES.items():
        if path.startswith(area + "/") and path[len(area) + 1:].split("/")[0] not in subs:
            return None
    return page


def run(name: str, args: dict) -> Tuple[dict, Optional[dict]]:
    """-> (tool result for the model, entry for the reply)"""
    if name == "offer_choices":
        opts = [str(o).strip()[:60] for o in (args.get("options") or []) if str(o).strip()][:10]
        if not opts:  # one is fine: the do-it-here button after a how-to answer (owner, 11/01)
            return {"error": "need 1-10 options"}, None
        return {"shown": True}, {"tool": name, "record_type": "choices", "record_id": str(uuid.uuid4()), "options": opts}
    page = safe_page(str(args.get("page") or ""))
    if not page:
        return {"error": "not_an_app_page", "message": "Links can only go to a page inside OpenHousePal (starting with /)."}, None
    # Whole words, up to 60 characters ("Start an Open House Kit for 719 Beechwoo" was cut mid-word, 11/01).
    label = " ".join(str(args.get("label") or "").split()) or "Go there"
    if len(label) > 60:
        label = label[:59].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    return {"shown": True}, {"tool": name, "record_type": "link", "record_id": str(uuid.uuid4()), "label": label, "href": page}
