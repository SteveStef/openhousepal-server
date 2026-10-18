"""Google One Tap on agents' IDX websites (user, 10/18).

A visitor already signed into Google taps "Continue as Jane" once: Google hands the page an ID token
(a signed JWT naming the person). We verify it with Google, then that agent gets the visitor as a
lead ("Signed in with Google on your website", plus the home they were looking at) - and every form
on the site fills in their name and email from then on (the frontend keeps those in a first-party
cookie). No new dependency: Google's tokeninfo endpoint checks the signature and expiry for us; we
check it was minted for OUR client and that Google verified the email.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
SOURCE_TYPE = "website_google_signin"
# A visitor who signs in again (another page, another day) adds a line to their lead at most this often.
REPEAT_WINDOW = timedelta(hours=12)


def client_id() -> Optional[str]:
    """The OAuth web client One Tap runs under (the same one the Google Calendar connect uses)."""
    return (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip() or None


async def verify_credential(credential: str) -> Optional[dict]:
    """The person behind a One Tap credential - {sub, email, name, given_name, family_name, picture} -
    or None when Google doesn't vouch for it (bad / expired / someone else's client / unverified email)."""
    cid = client_id()
    if not cid or not credential or len(credential) > 4096:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(TOKENINFO, params={"id_token": credential})
    except httpx.HTTPError as exc:
        logger.warning("One Tap token check could not reach Google: %s", exc)
        return None
    if r.status_code != 200:
        return None
    info = r.json()
    try:
        exp = int(info.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    if (info.get("aud") != cid or info.get("iss") not in _ISSUERS or exp < datetime.now(timezone.utc).timestamp()
            or str(info.get("email_verified")).lower() != "true" or not info.get("email")):
        return None
    return {
        "sub": info.get("sub"), "email": str(info["email"]).strip().lower(),
        "name": (info.get("name") or "").strip() or None, "given_name": (info.get("given_name") or "").strip() or None,
        "family_name": (info.get("family_name") or "").strip() or None, "picture": info.get("picture") or None,
    }
