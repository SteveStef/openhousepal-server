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

LINK_HELP = (
    "Handy pages (add the ? options to open them ready to go): /dash, /open-houses (My Opens - kits), "
    "/open-houses?kit=<street address, town> (start an Open House Kit for that home), /open-houses/my-listings (Manager), /discovery, /showcases, /showcases?new=1 (new Showcase), /showcases?showcase=<collection_id>, "
    "/leads, /leads/<lead_id>, /leads/tasks (Follow-ups), /leads/tasks?task=<task_id>, /leads/emails (Email Tracker), /activity, "
    "/studio (Tools), /studio/design?new=1 (new design), /studio/design?address=<street address> (new design for that home), "
    "/studio/property-sites?new=1 (new Spotlight), /studio/property-sites/<spotlight_id>, /studio/cma?new=1 (new CMA), /studio/cma/<report_id>, "
    "/studio/blogs?new=1 (new blog post), /studio/forms (Forms & Invites), /studio/media (Media Lab), /studio/brand-kit (Branding), "
    "/studio/email (Email settings + briefing time), /studio/email-signature, /studio/calendar, /studio/reviews, /studio/website (IDX website), "
    "/studio/site (Agent Hub editor), /settings, /settings/help (Help), /settings/help?compose=1&category=bug, /settings/subscription (Billing), /setup (Account setup)."
)

UI_TOOL_DEFINITIONS = [
    {
        "name": "offer_choices",
        "description": "Show the agent tap-to-answer buttons under your reply when you need them to pick (2-10 short options, each under 40 characters - list every real option, e.g. all 10 design kinds). Tapping one sends it as their answer. Ask the question in your reply text too.",
        "input_schema": {"type": "object", "properties": {"options": {"type": "array", "items": {"type": "string"}}}, "required": ["options"]},
    },
    {
        "name": "show_link",
        "description": "Show a 'Go there' button under your reply that opens a page in OpenHousePal - use it whenever you tell them where something is or when the page itself is the best way to finish. " + LINK_HELP,
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
    return page if any(path == p or path.startswith(p + "/") for p in _PAGES) else None


def run(name: str, args: dict) -> Tuple[dict, Optional[dict]]:
    """-> (tool result for the model, entry for the reply)"""
    if name == "offer_choices":
        opts = [str(o).strip()[:60] for o in (args.get("options") or []) if str(o).strip()][:10]
        if len(opts) < 2:
            return {"error": "need 2-10 options"}, None
        return {"shown": True}, {"tool": name, "record_type": "choices", "record_id": str(uuid.uuid4()), "options": opts}
    page = safe_page(str(args.get("page") or ""))
    if not page:
        return {"error": "not_an_app_page", "message": "Links can only go to a page inside OpenHousePal (starting with /)."}, None
    label = (str(args.get("label") or "").strip() or "Go there")[:40]
    return {"shown": True}, {"tool": name, "record_type": "link", "record_id": str(uuid.uuid4()), "label": label, "href": page}
