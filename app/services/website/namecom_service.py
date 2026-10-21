"""Name.com reseller API (Core API v1) - the company we register agents' new domains with.

Configured by env vars:
  NAMECOM_USERNAME   - the Name.com account username
  NAMECOM_API_TOKEN  - an API token for it (Account > Settings > API). Test tokens only work
                       against the test server and never charge anything.
  NAMECOM_API_URL    - https://api.dev.name.com (test, the default) or https://api.name.com (real)
Without the username/token every call raises NamecomError("not_configured"), and the buy screen
shows "opens soon" instead (domain_purchase_service.available()).

Docs: https://docs.name.com (OpenAPI: namedotcom-cdn.name.tools/api-info/namecom.api.yaml).
Rate limit 20 requests/second. Every POST/PUT needs Content-Type: application/json.
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class NamecomError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status = status


def configured() -> bool:
    return bool(os.getenv("NAMECOM_USERNAME") and os.getenv("NAMECOM_API_TOKEN"))


def is_test() -> bool:
    return "api.dev.name.com" in _base() or "localhost" in _base() or "127.0.0.1" in _base()


def _username() -> str:
    """Name.com's test server signs in as "<username>-test" - added here so the same username works for both."""
    u = (os.getenv("NAMECOM_USERNAME") or "").strip()
    return f"{u}-test" if is_test() and u and not u.endswith("-test") and "127.0.0.1" not in _base() else u


def _base() -> str:
    return (os.getenv("NAMECOM_API_URL") or "https://api.dev.name.com").rstrip("/")


async def _call(method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None,
                headers: Optional[dict] = None, timeout: float = 20.0) -> dict:
    if not configured():
        raise NamecomError("not_configured", "Name.com isn't set up")
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=(_username(), os.getenv("NAMECOM_API_TOKEN"))) as client:
            resp = await client.request(method, f"{_base()}{path}", json=json, params=params,
                                        headers={"Content-Type": "application/json", **(headers or {})})
    except httpx.HTTPError as e:
        logger.warning("Name.com request failed", extra={"path": path, "error": str(e)})
        raise NamecomError("unreachable", "Name.com didn't answer")
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        msg = str(body.get("message") or body.get("details") or resp.text[:200])
        logger.warning("Name.com error", extra={"path": path, "status": resp.status_code, "detail": msg})
        raise NamecomError("http_error", msg, resp.status_code)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


# ---------- discovery ----------

async def check_availability(domain_names: list[str]) -> list[dict]:
    """Up to 50 names -> [{domainName, purchasable, premium, purchasePrice, renewalPrice, purchaseType, ...}]."""
    data = await _call("POST", "/core/v1/domains:checkAvailability",
                       json={"domainNames": domain_names[:50], "purchaseType": "registration"})
    return data.get("results") or []


async def search(keyword: str, tlds: list[str]) -> list[dict]:
    """Suggestions for a keyword (same result shape as check_availability)."""
    data = await _call("POST", "/core/v1/domains:search",
                       json={"keyword": keyword, "tldFilter": tlds, "purchaseType": "registration", "timeout": 3000})
    return data.get("results") or []


async def get_pricing(domain_name: str, years: int = 1) -> dict:
    return await _call("GET", f"/core/v1/domains/{domain_name}:getPricing", params={"years": years})


async def get_balance() -> float:
    """Account credit left in USD - every API purchase and renewal is paid from it (never from
    the card or PayPal on the Name.com account; too little = 402 "Insufficient Funds")."""
    data = await _call("GET", "/core/v1/accountinfo/balance")
    return float(data.get("balance") or 0)


# ---------- registering + managing ----------

async def create_domain(domain_name: str, contact: dict, idempotency_key: str, years: int = 1,
                        purchase_price: Optional[float] = None) -> dict:
    """Registers it. `contact` = Name.com ContactRequest (firstName, lastName, address1, city,
    state, zip, country, email, phone) - used for every role. Whois privacy on (it's free).
    The idempotency key makes a retry after a timeout safe (no second charge)."""
    body: dict = {
        "domain": {
            "domainName": domain_name,
            "privacyEnabled": True,
            "contacts": {"registrant": contact, "admin": contact, "tech": contact, "billing": contact},
        },
        "years": years,
        "purchaseType": "registration",
    }
    if purchase_price is not None:
        body["purchasePrice"] = purchase_price
    return await _call("POST", "/core/v1/domains", json=body, headers={"X-Idempotency-Key": idempotency_key}, timeout=60.0)


async def get_domain(domain_name: str) -> dict:
    return await _call("GET", f"/core/v1/domains/{domain_name}")


async def list_records(domain_name: str) -> list[dict]:
    data = await _call("GET", f"/core/v1/domains/{domain_name}/records", params={"perPage": 1000})
    return data.get("records") or []


async def create_record(domain_name: str, host: str, type_: str, answer: str, ttl: int = 300) -> dict:
    return await _call("POST", f"/core/v1/domains/{domain_name}/records",
                       json={"host": host, "type": type_, "answer": answer, "ttl": ttl})


async def delete_record(domain_name: str, record_id: int) -> None:
    await _call("DELETE", f"/core/v1/domains/{domain_name}/records/{record_id}")


async def disable_autorenew(domain_name: str) -> None:
    # We renew ourselves once the agent's yearly PayPal payment lands (renewal_due), so Name.com
    # never charges our account for a domain nobody paid for.
    await _call("POST", f"/core/v1/domains/{domain_name}:disableAutorenew", json={})


async def renew(domain_name: str, years: int = 1) -> dict:
    return await _call("POST", f"/core/v1/domains/{domain_name}:renew", json={"years": years}, timeout=60.0)


async def unlock(domain_name: str) -> dict:
    return await _call("POST", f"/core/v1/domains/{domain_name}:unlock", json={})


async def get_auth_code(domain_name: str) -> str:
    data = await _call("GET", f"/core/v1/domains/{domain_name}:getAuthCode")
    return data.get("authCode") or ""


async def resend_verification(verification_id: int) -> None:
    await _call("POST", f"/core/v1/contacts/verify/{verification_id}:resend", json={})
