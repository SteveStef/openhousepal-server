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


async def call_as_agent(user: User, method: str, path: str, *, json: Any = None, params: Optional[dict] = None) -> Any:
    """Calls one of the app's routes as `user`. Returns the JSON body; raises RouteError with the
    route's own plain message on a 4xx/5xx."""
    from app.main import app  # lazy: app.main imports every router, this module is imported by some of them
    token = create_access_token({"sub": user.id}, expires_delta=timedelta(minutes=5))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=60) as client:
        r = await client.request(method, path, json=json, params=params, headers={"Authorization": f"Bearer {token}"})
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400:
        raise RouteError(r.status_code, _detail(data))
    return data


# Fields that are noise (or heavy) for the model - dropped from lookup results.
_DROP = ("photo", "image", "snapshot", "html", "svg", "token", "secret", "password", "media", "thumbnail", "cover", "raw")


def compact(data: Any, *, max_list: int = 15, max_str: int = 400, depth: int = 0) -> Any:
    """Shrinks a route's JSON for the model: no photo/HTML/token fields, short strings, short
    lists, shallow nesting - cheap to send and nothing secret."""
    if depth > 5:
        return "..."
    if isinstance(data, dict):
        return {k: compact(v, max_list=max_list, max_str=max_str, depth=depth + 1)
                for k, v in data.items() if not any(w in k.lower() for w in _DROP)}
    if isinstance(data, list):
        out = [compact(v, max_list=max_list, max_str=max_str, depth=depth + 1) for v in data[:max_list]]
        if len(data) > max_list:
            out.append(f"... and {len(data) - max_list} more")
        return out
    if isinstance(data, str) and len(data) > max_str:
        return data[:max_str] + "..."
    return data


async def lookup(user: User, path: str, params: Optional[dict] = None, **kw) -> Tuple[bool, Any]:
    try:
        return True, compact(await call_as_agent(user, "GET", path, params=params), **kw)
    except RouteError as e:
        return False, {"error": "lookup_failed", "message": str(e)}
