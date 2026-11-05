"""Lets the AI helper use the app's OWN routes as the logged-in agent (10/30).

An action or lookup calls the same URL the page's button calls, in-process, with a 5-minute
token for that agent - so every plan check, broker-authorization check, ownership check and
body validation runs exactly as if the agent had clicked it. Nothing here can reach a route the
agent couldn't; and callers only ever pass fixed paths from ai_actions_catalog / ai_tools, never
a path the model wrote.
"""
from datetime import timedelta
from typing import Any, Optional, Tuple

import httpx

from app.models.database import User
from app.utils.auth import create_access_token


class RouteError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _detail(data: Any) -> str:
    d = data.get("detail") if isinstance(data, dict) else None
    if isinstance(d, str):
        return d
    if isinstance(d, list) and d and isinstance(d[0], dict):  # pydantic validation errors
        return "; ".join(str(e.get("msg", "")) for e in d[:3])
    if isinstance(d, dict) and d.get("message"):
        return str(d["message"])
    return "That didn't work."


async def call_as_agent(user: User, method: str, path: str, *, json: Any = None, params: Optional[dict] = None, files: Optional[dict] = None) -> Any:
    """Calls one of the app's routes as `user`. Returns the JSON body; raises RouteError with the
    route's own plain message on a 4xx/5xx."""
    from app.main import app  # lazy: app.main imports every router, this module is imported by some of them
    token = create_access_token({"sub": user.id}, expires_delta=timedelta(minutes=5))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=60) as client:
        r = await client.request(method, path, json=json, params=params, files=files, headers={"Authorization": f"Bearer {token}"})
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400:
        raise RouteError(r.status_code, _detail(data))
    return data


# Fields that are noise (or heavy) for the model - dropped from lookup results.
_DROP = ("photo", "image", "snapshot", "html", "svg", "token", "secret", "password", "media", "thumbnail", "cover", "raw")


def _small_dict(d: Any) -> bool:
    """A short block of plain values (a Showcase's counts) - kept even when nested details are flattened."""
    return isinstance(d, dict) and len(d) <= 12 and all(not isinstance(v, list) and (not isinstance(v, dict) or _small_dict(v)) for v in d.values())


def compact(data: Any, *, max_list: int = 15, max_str: int = 400, depth: int = 0,
            inner_list: Optional[int] = None, flat: bool = False, _in_list: bool = False, _item_depth: int = 0) -> Any:
    """Shrinks a route's JSON for the model: no photo/HTML/token fields, short strings, short
    lists, shallow nesting - cheap to send and nothing secret. `inner_list` caps lists inside the
    items of the main list (a Showcase's homes, a form's questions); `flat` turns anything nested
    inside a main-list item into "..." (the item's own plain fields stay)."""
    if depth > 5:
        return "..."
    if flat and _in_list and _item_depth >= 2 and isinstance(data, (dict, list)) and not _small_dict(data):
        return "..."
    kw = dict(max_list=max_list, max_str=max_str, inner_list=inner_list, flat=flat)
    if isinstance(data, dict):
        return {k: compact(v, depth=depth + 1, _in_list=_in_list, _item_depth=_item_depth + 1 if _in_list else 0, **kw)
                for k, v in data.items() if not any(w in k.lower() for w in _DROP)}
    if isinstance(data, list):
        cap = inner_list if (_in_list and inner_list is not None) else max_list
        out = [compact(v, depth=depth + 1, _in_list=True, _item_depth=_item_depth + 1 if _in_list else 1, **kw) for v in data[:cap]]
        if len(data) > cap:
            out.append(f"... and {len(data) - cap} more")
        return out
    if isinstance(data, str) and len(data) > max_str:
        return data[:max_str] + "..."
    return data


# One lookup's result for the model stays under ~9K tokens (owner, 11/04: chat usage climbing - a
# Showcases list was ~40K tokens, every Showcase with its homes, and it went out again on each step
# of the answer). Inner lists shrink first, then nested details, then long text, then the main list.
RESULT_BUDGET_CHARS = 36_000


def compact_to_fit(data: Any, *, max_list: int = 15, max_str: int = 400, budget: int = RESULT_BUDGET_CHARS) -> Any:
    import json as _json
    inner, flat, ms, ml = None, False, max_str, max_list
    out = compact(data, max_list=ml, max_str=ms)
    for _ in range(12):
        if len(_json.dumps(out, default=str)) <= budget:
            return out
        if inner is None or inner > 0:
            inner = 8 if inner is None else (3 if inner > 3 else 0)  # down to just "... and N more"
        elif not flat:
            flat = True
        elif ms > 120:
            ms = max(120, ms // 2)
        elif ml > 5:
            ml = max(5, ml // 2)
        else:
            break
        out = compact(data, max_list=ml, max_str=ms, inner_list=inner, flat=flat)
    return {**out, "_note": "Shortened to fit - ask about one item for its full details."} if isinstance(out, dict) else out


async def lookup(user: User, path: str, params: Optional[dict] = None, **kw) -> Tuple[bool, Any]:
    try:
        return True, compact_to_fit(await call_as_agent(user, "GET", path, params=params), **kw)
    except RouteError as e:
        return False, {"error": "lookup_failed", "message": str(e)}
