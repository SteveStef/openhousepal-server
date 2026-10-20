"""Buy a domain inside OpenHousePal (user, 10/20 - "people can buy domains in our site and it auto
sets up the DNS to their IDX website").

The whole life of a bought domain:
  1. search      - agent types a name; we check Name.com and show what's free, at our yearly price.
  2. start       - agent picks one + confirms their contact details (the domain is registered in
                   THEIR name - ICANN rules); we make a yearly PayPal subscription priced for it and
                   send them to PayPal. Row status CHECKOUT.
  3. complete    - once PayPal says year one is paid (return page polls, the webhook nudges, the
                   sweep catches stragglers): register at Name.com (idempotent), point it at the
                   website (www CNAME + bare-domain ANAME -> sites.openhousepal.com), add the usual
                   site_domains pair + Cloudflare certificates. Status ACTIVE. If Name.com refuses,
                   the payment is refunded and the subscription cancelled (FAILED).
  4. each year   - the PayPal yearly payment lands (webhook) -> we renew one year at Name.com.
                   Name.com auto-renew is OFF so nothing renews unpaid. Agent can stop renewing
                   (cancels PayPal; the domain stays until it expires) or move the domain away
                   (we unlock it and show the transfer code).

Config (env): NAMECOM_* (namecom_service), PAYPAL_DOMAIN_PLAN_ID (the shared yearly "Domain
name" plan - scripts/create_domain_paypal_plan.py makes it), DOMAIN_MARKUP_USD (default 6),
DOMAIN_MAX_PRICE_USD (default 75), DOMAIN_TLDS (endings we sell).
"""
from __future__ import annotations

import logging
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PurchasedDomain, Site, SiteDomain, User
from app.services.website import namecom_service
from app.services.website.namecom_service import NamecomError

logger = logging.getLogger(__name__)

DEFAULT_TLDS = ["com", "net", "org", "co", "homes", "realty", "house", "properties", "estate", "info"]
# Words added to a name to find free variations ("janesmith" -> janesmithhomes.com ...)
SUFFIXES = ["", "homes", "realestate", "realtor", "realty", "properties", "group", "home"]
CNAME_TARGET = os.getenv("WEBSITE_CNAME_TARGET", "sites.openhousepal.com")
RENEW_LEAD_DAYS = 14   # yearly PayPal payment this long before the domain would expire


def tlds() -> list[str]:
    raw = os.getenv("DOMAIN_TLDS")
    return [t.strip().lstrip(".").lower() for t in raw.split(",") if t.strip()] if raw else list(DEFAULT_TLDS)


_plan_ids: dict[str, str] = {}


def _paypal_ready() -> bool:
    return bool(os.getenv("PAYPAL_DOMAIN_PLAN_ID") or (os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_SECRET_ID")))


async def plan_id(db: AsyncSession) -> str:
    """The shared yearly "Domain name" PayPal plan. PAYPAL_DOMAIN_PLAN_ID if set; otherwise made
    automatically the first time anyone buys and remembered in app_cache (one per PayPal mode),
    so nobody has to run anything by hand."""
    env = os.getenv("PAYPAL_DOMAIN_PLAN_ID")
    if env:
        return env
    from sqlalchemy import text
    from app.services import app_cache
    from app.services.paypal_service import paypal_service
    key = f"paypal_domain_plan_id:{os.getenv('PAYPAL_MODE', 'sandbox')}"
    if key in _plan_ids:
        return _plan_ids[key]
    row = (await db.execute(text("SELECT value FROM app_cache WHERE key = :k"), {"k": key})).scalar_one_or_none()
    if isinstance(row, str) and row:
        _plan_ids[key] = row
        return row
    pid = await paypal_service.create_domain_plan()
    await app_cache.save(key, pid)
    _plan_ids[key] = pid
    _ping(f":credit_card: Made the PayPal 'Domain name' plan automatically ({os.getenv('PAYPAL_MODE', 'sandbox')}): {pid}")
    return pid


def available() -> dict:
    """Can agents buy right now? (the screen says "opens soon" otherwise)"""
    return {"buy": namecom_service.configured() and _paypal_ready(), "test_mode": namecom_service.is_test()}


def our_price(cost: float) -> float:
    """Name.com's price + our margin, rounded up to the next .99 (12.99 -> 18.99)."""
    markup = float(os.getenv("DOMAIN_MARKUP_USD", "6"))
    return round(math.ceil(cost + markup) - 0.01, 2)


def max_price() -> float:
    return float(os.getenv("DOMAIN_MAX_PRICE_USD", "75"))


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def clean_query(raw: str) -> tuple[str, Optional[str]]:
    """'https://www.Jane Smith Homes.com/' -> ('janesmithhomes', 'com'); 'jane smith' -> ('janesmith', None)."""
    q = (raw or "").strip().lower()
    q = re.sub(r"^[a-z]+://", "", q).split("/")[0].removeprefix("www.")
    tld = None
    if "." in q:
        sld, _, tld = q.partition(".")
        q = sld
        tld = tld.strip(".") or None
    q = re.sub(r"[^a-z0-9-]", "", q).strip("-")[:63]
    return q, tld


async def search(raw: str) -> dict:
    sld, typed_tld = clean_query(raw)
    if not sld or not _LABEL.match(sld):
        return {"query": sld, "results": [], "exact": None}
    endings = tlds()
    exact_tld = typed_tld if typed_tld in endings else "com"
    exact = f"{sld}.{exact_tld}"
    # Most wanted first: the exact name, .com ideas ("janesmithhomes.com"), then other endings.
    names = [exact, f"{sld}.com"] if "com" in endings else [exact]
    if "com" in endings:
        names += [f"{sld}{s}.com" for s in SUFFIXES[1:] if len(sld + s) <= 63]
    names += [f"{sld}.{t}" for t in endings]
    names = list(dict.fromkeys(names))[:50]
    results = await namecom_service.check_availability(names)
    by_name = {r.get("domainName", "").lower(): r for r in results}
    out = []
    for name in names:
        r = by_name.get(name)
        if not r or not r.get("purchasable") or r.get("premium") or (r.get("purchaseType") or "registration") != "registration":
            continue
        cost = float(r.get("purchasePrice") or 0)
        renew_cost = float(r.get("renewalPrice") or cost)
        if cost <= 0:
            continue
        price, renewal = our_price(cost), our_price(renew_cost)
        if price > max_price() or renewal > max_price():
            continue
        out.append({"domain": name, "price": price, "renewal_price": renewal, "exact": name == exact})
    exact_row = next((o for o in out if o["exact"]), None)
    if typed_tld and typed_tld not in endings:
        exact_state = "ending_not_sold"
    else:
        exact_state = "available" if exact_row else "taken"
    return {"query": sld, "exact": {"domain": exact, "state": exact_state}, "results": out[:12]}


# ---------- contact ----------

US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())


def clean_contact(c: dict) -> tuple[Optional[dict], Optional[str]]:
    """The agent's details -> a Name.com contact (all roles). Returns (contact, problem)."""
    get = lambda k: str((c or {}).get(k) or "").strip()  # noqa: E731
    first, last = get("firstName"), get("lastName")
    if not first or not last:
        return None, "Type your first and last name."
    address1, city, state, zip_ = get("address1"), get("city"), get("state").upper(), get("zip")
    if not address1 or not city:
        return None, "Type your street address and town."
    if state not in US_STATES:
        return None, "Pick your state."
    if not re.match(r"^\d{5}(-\d{4})?$", zip_):
        return None, "Type a 5-digit zip code."
    email = get("email")
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email):
        return None, "Type a working email - you'll get one email to confirm it."
    digits = re.sub(r"\D", "", get("phone"))
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return None, "Type a 10-digit phone number."
    contact = {"firstName": first, "lastName": last, "address1": address1, "city": city, "state": state,
               "zip": zip_, "country": "US", "email": email, "phone": f"+{digits}"}
    if get("address2"):
        contact["address2"] = get("address2")
    return contact, None


# ---------- checkout ----------

class PurchaseError(Exception):
    """Agent-safe message."""


async def start(db: AsyncSession, user: User, domain: str, contact_in: dict, back: str) -> dict:
    from app.services.agent_email_service import app_url
    from app.services.paypal_service import paypal_service
    if not available()["buy"]:
        raise PurchaseError("Buying a domain opens soon.")
    domain = (domain or "").strip().lower()
    sld, tld = clean_query(domain)
    if not sld or not tld or tld not in tlds() or f"{sld}.{tld}" != domain:
        raise PurchaseError("Pick one of the names from the list.")
    contact, problem = clean_contact(contact_in)
    if problem:
        raise PurchaseError(problem)
    site = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalar_one_or_none()
    if not site:
        raise PurchaseError("Set up your IDX website first.")
    taken = (await db.execute(select(SiteDomain.id).where(SiteDomain.hostname.in_([domain, f"www.{domain}"])))).first()
    if taken:
        raise PurchaseError("That domain is already connected to a website.")
    open_buy = (await db.execute(select(PurchasedDomain).where(
        PurchasedDomain.domain_name == domain, PurchasedDomain.status.in_(["REGISTERING", "ACTIVE"])))).scalar_one_or_none()
    if open_buy:
        raise PurchaseError("That domain was just bought.")

    # fresh price - it can change between the search and the click
    rows = await namecom_service.check_availability([domain])
    r = next((x for x in rows if (x.get("domainName") or "").lower() == domain), None)
    if not r or not r.get("purchasable") or r.get("premium"):
        raise PurchaseError("Sorry - someone just took that one. Pick another.")
    cost = float(r.get("purchasePrice") or 0)
    price, renewal = our_price(cost), our_price(float(r.get("renewalPrice") or cost))
    if cost <= 0 or price > max_price():
        raise PurchaseError("That one isn't available here - pick another.")

    purchase = PurchasedDomain(owner_id=user.id, domain_name=domain, status="CHECKOUT", price_usd=price,
                               renewal_price_usd=renewal, cost_usd=cost, registrant=contact)
    db.add(purchase)
    await db.flush()
    back = back if back in ("setup", "settings", "website") else "website"
    ret = f"{app_url()}/studio/domains/return?purchase={purchase.id}&back={back}"
    first_renewal = datetime.now(timezone.utc) + timedelta(days=365 - RENEW_LEAD_DAYS)
    try:
        sub = await paypal_service.create_domain_subscription(
            await plan_id(db), price, renewal, first_renewal.strftime("%Y-%m-%dT%H:%M:%SZ"), purchase.id,
            f"{domain} (yearly)", ret, f"{ret}&cancelled=1",
        )
    except Exception:
        logger.exception("PayPal domain subscription create failed", extra={"domain": domain})
        await db.rollback()
        raise PurchaseError("PayPal didn't answer - try again in a minute.")
    purchase.paypal_subscription_id = sub.get("id")
    await db.commit()
    approve = next((l.get("href") for l in sub.get("links") or [] if l.get("rel") in ("approve", "payer-action")), None)
    return {"purchase_id": purchase.id, "approve_url": approve}


def _money(v) -> float:
    try:
        return float((v or {}).get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _year_one_paid(sub: dict, purchase: PurchasedDomain) -> Optional[str]:
    """The PayPal sale id of the year-one payment, '' when paid but the id isn't listed yet, None = not paid."""
    from app.services.paypal_service import paypal_service
    last = (sub.get("billing_info") or {}).get("last_payment") or {}
    paid = _money(last.get("amount")) >= purchase.price_usd - 0.01
    try:
        txs = await paypal_service.list_subscription_transactions(purchase.paypal_subscription_id, days=30)
    except Exception:
        txs = []
    sale = next((t for t in txs if (t.get("status") or "").upper() == "COMPLETED"
                 and _money((t.get("amount_with_breakdown") or {}).get("gross_amount")) >= purchase.price_usd - 0.01), None)
    if sale:
        return sale.get("id") or ""
    return "" if paid else None


async def complete(db: AsyncSession, purchase: PurchasedDomain) -> PurchasedDomain:
    """Idempotent - safe to call from the return page, the webhook and the sweep at once."""
    from app.services.paypal_service import paypal_service
    if purchase.status != "CHECKOUT" or not purchase.paypal_subscription_id:
        return purchase
    try:
        sub = await paypal_service.get_subscription(purchase.paypal_subscription_id)
    except Exception:
        logger.warning("PayPal lookup failed for domain checkout", exc_info=True, extra={"purchase": purchase.id})
        return purchase
    if (sub.get("custom_id") or purchase.id) != purchase.id:
        logger.error("Domain checkout subscription mismatch", extra={"purchase": purchase.id})
        return purchase
    status = (sub.get("status") or "").upper()
    if status in ("CANCELLED", "EXPIRED"):
        purchase.status = "ABANDONED"
        await db.commit()
        return purchase
    if status != "ACTIVE":
        return purchase  # APPROVAL_PENDING / APPROVED - not paid yet
    sale_id = await _year_one_paid(sub, purchase)
    if sale_id is None:
        return purchase

    # claim it - only one caller registers
    claimed = await db.execute(update(PurchasedDomain).where(PurchasedDomain.id == purchase.id, PurchasedDomain.status == "CHECKOUT")
                               .values(status="REGISTERING"))
    await db.commit()
    if claimed.rowcount != 1:
        await db.refresh(purchase)
        return purchase
    await db.refresh(purchase)
    return await _register(db, purchase, sale_id)


async def _register(db: AsyncSession, purchase: PurchasedDomain, sale_id: str) -> PurchasedDomain:
    from app.services.paypal_service import paypal_service
    try:
        res = await namecom_service.create_domain(purchase.domain_name, purchase.registrant or {}, purchase.id)
    except NamecomError as e:
        if e.code == "unreachable":
            # might have gone through - the idempotency key makes the retry safe; the sweep retries
            purchase.status = "CHECKOUT"
            purchase.last_error = "registry_busy"
            await db.commit()
            return purchase
        logger.error("Name.com registration failed", extra={"domain": purchase.domain_name, "error": e.message})
        purchase.status = "FAILED"
        purchase.last_error = "registration_failed"
        refunded = False
        try:
            if not sale_id:
                txs = await paypal_service.list_subscription_transactions(purchase.paypal_subscription_id, days=30)
                sale_id = next((t.get("id") for t in txs if (t.get("status") or "").upper() == "COMPLETED"), "") or ""
            refunded = bool(sale_id) and await paypal_service.refund_sale(sale_id)
            await paypal_service.cancel_subscription(purchase.paypal_subscription_id, "Domain could not be registered - refunded")
        except Exception:
            logger.exception("Refund/cancel after failed registration", extra={"purchase": purchase.id})
        await db.commit()
        _ping(f":warning: Domain registration FAILED for {purchase.domain_name} ({e.message}). Refund {'sent' if refunded else 'NOT sent - refund it in PayPal'}.")
        await _email(db, purchase, "failed")
        return purchase

    dom = res.get("domain") or {}
    now = datetime.now(timezone.utc)
    purchase.registered_at = now
    purchase.expires_at = _parse(dom.get("expireDate")) or now + timedelta(days=365)
    purchase.namecom_order_id = str(res.get("order") or res.get("orderId") or "") or None
    if res.get("totalPaid") is not None:
        try:
            purchase.cost_usd = float(res["totalPaid"])
        except (TypeError, ValueError):
            pass
    purchase.status = "ACTIVE"
    purchase.last_error = None
    await db.commit()
    try:
        await namecom_service.disable_autorenew(purchase.domain_name)
    except NamecomError:
        logger.warning("Couldn't switch off Name.com auto-renew", extra={"domain": purchase.domain_name})
    await hook_up(db, purchase)
    _ping(f":globe_with_meridians: Domain bought: {purchase.domain_name} for ${purchase.price_usd:.2f}/yr (cost ${purchase.cost_usd or 0:.2f}).")
    await _email(db, purchase, "bought")
    return purchase


def _parse(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


async def set_dns(domain: str) -> None:
    """www -> CNAME, bare domain -> ANAME (Name.com's root alias), both at sites.openhousepal.com.
    Clears Name.com's parking records on those two names first."""
    records = await namecom_service.list_records(domain)
    for r in records:
        host = (r.get("host") or "").lower()
        if host in ("", "@", "www") and (r.get("type") or "").upper() in ("A", "AAAA", "CNAME", "ANAME"):
            if host == "www" and r.get("type") == "CNAME" and (r.get("answer") or "").rstrip(".").lower() == CNAME_TARGET:
                continue
            if host in ("", "@") and r.get("type") == "ANAME" and (r.get("answer") or "").rstrip(".").lower() == CNAME_TARGET:
                continue
            if r.get("id") is not None:
                await namecom_service.delete_record(domain, r["id"])
    have = {((r.get("host") or "").lower(), (r.get("type") or "").upper()) for r in await namecom_service.list_records(domain)}
    if ("www", "CNAME") not in have:
        await namecom_service.create_record(domain, "www", "CNAME", CNAME_TARGET)
    if ("", "ANAME") not in have and ("@", "ANAME") not in have:
        await namecom_service.create_record(domain, "", "ANAME", CNAME_TARGET)


async def hook_up(db: AsyncSession, purchase: PurchasedDomain) -> None:
    """DNS at Name.com + the site_domains pair + Cloudflare certificates. Safe to re-run."""
    from app.services.website import cloudflare_saas_service, domain_resolution_service
    domain = purchase.domain_name
    try:
        await set_dns(domain)
        if purchase.last_error == "dns_setup":
            purchase.last_error = None
    except NamecomError:
        logger.warning("Name.com DNS setup failed - the sweep retries", extra={"domain": domain})
        purchase.last_error = "dns_setup"
    site = (await db.execute(select(Site).where(Site.owner_id == purchase.owner_id))).scalar_one_or_none()
    if site:
        www = f"www.{domain}"
        main = (await db.execute(select(SiteDomain).where(SiteDomain.hostname == www))).scalar_one_or_none()
        if not main:
            main = SiteDomain(site_id=site.id, owner_id=purchase.owner_id, hostname=www, verification_token=secrets.token_hex(16))
            db.add(main)
            await db.flush()
        if main.owner_id == purchase.owner_id:
            bare = (await db.execute(select(SiteDomain).where(SiteDomain.hostname == domain))).scalar_one_or_none()
            if not bare:
                db.add(SiteDomain(site_id=site.id, owner_id=purchase.owner_id, hostname=domain,
                                  verification_token=secrets.token_hex(16), redirect_target_domain_id=main.id))
        domain_resolution_service.invalidate_site(site.id)
    await db.commit()
    for host in (f"www.{domain}", domain):
        await cloudflare_saas_service.register(host)


async def make_primary_when_live(db: AsyncSession, purchase: PurchasedDomain) -> None:
    """A bought domain becomes the website's main address as soon as it works - unless the agent
    already has another one as main."""
    from app.services.website import site_service
    main = (await db.execute(select(SiteDomain).where(SiteDomain.hostname == f"www.{purchase.domain_name}",
                                                     SiteDomain.owner_id == purchase.owner_id))).scalar_one_or_none()
    if not main or main.status != "ACTIVE" or main.is_primary:
        return
    other = (await db.execute(select(SiteDomain.id).where(SiteDomain.site_id == main.site_id, SiteDomain.is_primary.is_(True)))).first()
    if other:
        return
    site = await db.get(Site, main.site_id)
    owner = await db.get(User, purchase.owner_id)
    if site and owner:
        await site_service.set_primary_domain(db, site, main, owner)


# ---------- renewals + the agent's controls ----------

async def on_paypal_event(db: AsyncSession, purchase: PurchasedDomain, event_type: str, resource: dict) -> None:
    """Webhook events for a domain's subscription (webhook_routes sends them here)."""
    if purchase.status == "CHECKOUT":
        await complete(db, purchase)
        return
    if event_type == "PAYMENT.SALE.COMPLETED" and purchase.status == "ACTIVE":
        await renew_if_due(db, purchase, paid=True)
    elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED"):
        if purchase.auto_renew:
            purchase.auto_renew = False
            await db.commit()
            if event_type != "BILLING.SUBSCRIPTION.CANCELLED":
                await _email(db, purchase, "payment_failed")


async def renew_if_due(db: AsyncSession, purchase: PurchasedDomain, paid: bool) -> None:
    """A yearly payment came in: add a year at Name.com (only if it's within ~2 months of expiry -
    the year-one payment also arrives as a sale and must not renew)."""
    now = datetime.now(timezone.utc)
    exp = purchase.expires_at if not purchase.expires_at or purchase.expires_at.tzinfo else purchase.expires_at.replace(tzinfo=timezone.utc)
    if not paid or not exp or exp - now > timedelta(days=60):
        return
    if purchase.last_renewed_at and now - (purchase.last_renewed_at if purchase.last_renewed_at.tzinfo else purchase.last_renewed_at.replace(tzinfo=timezone.utc)) < timedelta(days=300):
        return
    try:
        res = await namecom_service.renew(purchase.domain_name, 1)
        dom = res.get("domain") or {}
        purchase.expires_at = _parse(dom.get("expireDate")) or exp + timedelta(days=365)
        purchase.last_renewed_at = now
        purchase.last_error = None
        _ping(f":repeat: Domain renewed: {purchase.domain_name} (paid ${purchase.renewal_price_usd:.2f}).")
    except NamecomError as e:
        purchase.last_error = "renew_failed"
        _ping(f":rotating_light: Domain RENEWAL failed for {purchase.domain_name} ({e.message}) - the agent paid; renew it at Name.com by hand.")
    await db.commit()


async def stop_renewing(db: AsyncSession, purchase: PurchasedDomain) -> None:
    from app.services.paypal_service import paypal_service
    if purchase.paypal_subscription_id and purchase.auto_renew:
        try:
            await paypal_service.cancel_subscription(purchase.paypal_subscription_id, "Agent turned off domain renewal")
        except Exception:
            logger.warning("Cancel domain subscription failed", exc_info=True, extra={"purchase": purchase.id})
            raise PurchaseError("PayPal didn't answer - try again in a minute.")
    purchase.auto_renew = False
    await db.commit()


async def transfer_code(db: AsyncSession, purchase: PurchasedDomain) -> str:
    """Unlock + the code another company asks for. ICANN blocks moves for 60 days after buying."""
    reg = purchase.registered_at if not purchase.registered_at or purchase.registered_at.tzinfo else purchase.registered_at.replace(tzinfo=timezone.utc)
    if reg and datetime.now(timezone.utc) - reg < timedelta(days=60):
        ok = reg + timedelta(days=60)
        raise PurchaseError(f"A new domain can be moved 60 days after it's bought - from {ok:%B} {ok.day}, {ok.year}.")
    try:
        await namecom_service.unlock(purchase.domain_name)
        return await namecom_service.get_auth_code(purchase.domain_name)
    except NamecomError:
        raise PurchaseError("We couldn't get the code just now - try again, or contact support.")


async def resend_email(purchase: PurchasedDomain) -> bool:
    try:
        dom = await namecom_service.get_domain(purchase.domain_name)
        vid = (((dom.get("contacts") or {}).get("registrant")) or {}).get("verificationId")
        if not vid:
            return False
        await namecom_service.resend_verification(int(vid))
        return True
    except (NamecomError, ValueError):
        return False


async def email_confirmed(purchase: PurchasedDomain) -> Optional[bool]:
    """Has the agent clicked the one-time "confirm your email" link? (None = can't tell)"""
    try:
        dom = await namecom_service.get_domain(purchase.domain_name)
    except NamecomError:
        return None
    reg = (dom.get("contacts") or {}).get("registrant") or {}
    return reg.get("isVerified") if isinstance(reg.get("isVerified"), bool) else None


def as_dict(p: PurchasedDomain) -> dict:
    return {
        "id": p.id, "domain": p.domain_name, "status": p.status, "price": p.price_usd, "renewal_price": p.renewal_price_usd,
        "auto_renew": p.auto_renew, "registered_at": p.registered_at.isoformat() if p.registered_at else None,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None, "last_error": p.last_error,
        "email": (p.registrant or {}).get("email"),
    }


# ---------- the sweep (runs with the domain health job) ----------

async def sweep(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(PurchasedDomain).where(PurchasedDomain.status.in_(["CHECKOUT", "REGISTERING", "ACTIVE"])))).scalars().all()
    for p in rows:
        try:
            created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc)
            if p.status == "CHECKOUT":
                if now - created > timedelta(days=2):
                    p.status = "ABANDONED"   # never approved at PayPal
                    await db.commit()
                else:
                    await complete(db, p)
            elif p.status == "REGISTERING" and now - (p.updated_at or created).replace(tzinfo=timezone.utc) > timedelta(minutes=30):
                _ping(f":hourglass: Domain {p.domain_name} stuck registering for 30+ min - check Name.com.")
            elif p.status == "ACTIVE":
                if p.last_error == "dns_setup":
                    await hook_up(db, p)
                await make_primary_when_live(db, p)
                exp = p.expires_at if not p.expires_at or p.expires_at.tzinfo else p.expires_at.replace(tzinfo=timezone.utc)
                if exp and exp < now:
                    p.status = "EXPIRED"
                    await db.commit()
                elif exp and p.auto_renew and exp - now < timedelta(days=10) and p.paypal_subscription_id:
                    # the yearly-payment webhook never arrived? ask PayPal directly before it lapses
                    from app.services.paypal_service import paypal_service
                    last = ((await paypal_service.get_subscription(p.paypal_subscription_id)).get("billing_info") or {}).get("last_payment") or {}
                    when = _parse(last.get("time"))
                    if when and now - when < timedelta(days=40) and _money(last.get("amount")) >= p.renewal_price_usd - 0.01:
                        await renew_if_due(db, p, paid=True)
                elif exp and not p.auto_renew:
                    days = (exp - now).days
                    flags = dict(p.registrant or {})
                    for mark in (30, 7):
                        if days <= mark and not flags.get(f"_warned{mark}"):
                            flags[f"_warned{mark}"] = True
                            p.registrant = flags
                            await db.commit()
                            await _email(db, p, "expiring")
                            break
        except Exception:
            logger.exception("Domain purchase sweep failed", extra={"purchase": p.id})
            await db.rollback()


# ---------- messages ----------

def _ping(msg: str) -> None:
    try:
        from app.services.discord_notifier import notifier
        notifier.send(msg)
    except Exception:
        pass


async def _email(db: AsyncSession, p: PurchasedDomain, kind: str) -> None:
    """One Mailgun template (domain_update) for every case - prod Mailgun is at its template cap."""
    try:
        from app.services.agent_email_service import absolute, email_agent
        owner = await db.get(User, p.owner_id)
        if not owner:
            return
        d = p.domain_name
        exp = f"{p.expires_at:%B} {p.expires_at.day}, {p.expires_at.year}" if p.expires_at else ""
        price = f"${p.renewal_price_usd:.2f}"
        email = (p.registrant or {}).get("email") or owner.email
        msgs = {
            "bought": dict(
                subject=f"{d} is yours", eyebrow="New domain", heading=f"{d} is yours", button="See my domain",
                preheader=f"{d} is yours - we're setting it up now.",
                p1=f"you now own {d}. It's registered in your name, and we've already pointed it at your OpenHousePal "
                   "website - there's nothing to set up. New domains take a little while to reach every computer; your "
                   "site usually opens on it within an hour, and we'll email you when it does.",
                note=f"One thing to do: look for an email from Name.com (our domain partner) sent to {email} and click the "
                     "confirm link inside. Every domain company has to ask this. If it isn't clicked within 15 days, the domain pauses.",
                p2=f"It renews automatically each year for {price} through PayPal. You can stop renewing or move it any time."),
            "failed": dict(
                subject=f"We couldn't get {d} - you've been refunded", eyebrow="Refunded", heading=f"We couldn't get {d}",
                button="Pick another name", preheader=f"Your payment for {d} is being refunded in full.",
                p1=f"the domain company wouldn't register {d} - most often someone took it a moment before you did. Your "
                   "PayPal payment is being refunded in full; you'll see it within a few days. Nothing renews.",
                note="", p2="Pick another name any time - it only takes a minute."),
            "payment_failed": dict(
                subject=f"{d} won't renew - PayPal payment didn't go through", eyebrow="Renewal", heading=f"{d} won't renew",
                button="See my domain", preheader=f"PayPal couldn't take the renewal for {d}.",
                p1=f"PayPal couldn't take this year's payment for {d}, so it won't renew. It keeps working until {exp}.",
                note="", p2="To keep it, check the card on your PayPal account, then reply to this email or contact "
                             "OpenHousePal support and we'll sort it out with you."),
            "expiring": dict(
                subject=f"{d} ends on {exp}", eyebrow="Reminder", heading=f"{d} ends on {exp}", button="See my domain",
                preheader=f"{d} ends on {exp}.",
                p1=f"renewing is switched off for {d}, so it ends on {exp}. After that your website goes back to its free "
                   f"OpenHousePal address, and links that used {d} stop opening.",
                note="", p2="Moving it to another company? Get its transfer code from the Domain screen before it ends."),
        }
        m = msgs[kind]
        subject = m.pop("subject")
        email_agent(owner, None, "domain_update", subject, {**m, "domain": d, "link": absolute("/studio/website?tab=domains")})
    except Exception:
        logger.warning("domain email failed", exc_info=True)
