from typing import Optional

from fastapi import Request


def get_client_ip(request: Request) -> Optional[str]:
    """Real client IP behind the Traefik/Dokploy reverse proxy - X-Forwarded-For's first hop is
    the original client, not the proxy itself. Falls back to the raw connection IP for local/dev
    runs with no proxy in front."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
