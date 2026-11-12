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

Config (env): NAMECOM_* (namecom_service), PayPal keys (a plan per price pair is made
automatically - see plan_id), DOMAIN_MARKUP_USD (default 6),
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
    return bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_SECRET_ID"))


async def _cached(db: AsyncSession, key: str) -> Optional[str]:
    from sqlalchemy import text
    if key in _plan_ids:
        return _plan_ids[key]
    row = (await db.execute(text("SELECT value FROM app_cache WHERE key = :k"), {"k": key})).scalar_one_or_none()
    if isinstance(row, str) and row:
        _plan_ids[key] = row
        return row
    return None


async def plan_id(db: AsyncSession, price: float, renewal: float) -> str:
    """A plain PayPal plan for one price pair: year one at `price`, charged the moment the agent
    approves, then `renewal` every year - the yearly charge lands RENEW_LEAD_DAYS before the domain
    would expire. Made automatically the first time a price pair is sold and remembered in
    app_cache (per PayPal mode), so nobody has to run anything by hand.

    Why not one shared plan with each subscription's price overridden + a setup fee + a start date
    a year out (the first version): PayPal's live checkout refused that shape for every buyer
    ("PayPal isn't available at this time", 11/10) while the sandbox accepted it. A plan whose
    first cycle is billed at once is the same shape as every OpenHousePal sign-up plan."""
    from app.services import app_cache
    from app.services.paypal_service import paypal_service
    mode = os.getenv("PAYPAL_MODE", "sandbox")
    key = f"paypal_domain_plan:v2:{mode}:{price:.2f}:{renewal:.2f}"
    pid = await _cached(db, key)
    if pid:
        return pid
    product_key = f"paypal_domain_product:{mode}"
    product = await _cached(db, product_key)
    if not product:
        product = await paypal_service.create_domain_product()
        await app_cache.save(product_key, product)
        _plan_ids[product_key] = product
    pid = await paypal_service.create_domain_plan(product, price, renewal, 365 - RENEW_LEAD_DAYS)
    await app_cache.save(key, pid)
    _plan_ids[key] = pid
    _ping(f":credit_card: Made a PayPal domain plan ({mode}): ${price:.2f} then ${renewal:.2f}/yr - {pid}")
    return pid


async def renew_plan_id(db: AsyncSession, renewal: float) -> str:
    """The yearly-only plan used by "Renew with card" (a domain they already own), per price."""
    from app.services import app_cache
    from app.services.paypal_service import paypal_service
    mode = os.getenv("PAYPAL_MODE", "sandbox")
    key = f"paypal_domain_renew_plan:v1:{mode}:{renewal:.2f}"
    pid = await _cached(db, key)
    if pid:
        return pid
    product_key = f"paypal_domain_product:{mode}"
    product = await _cached(db, product_key)
    if not product:
        product = await paypal_service.create_domain_product()
        await app_cache.save(product_key, product)
        _plan_ids[product_key] = product
    pid = await paypal_service.create_domain_renew_plan(product, renewal)
    await app_cache.save(key, pid)
    _plan_ids[key] = pid
    _ping(f":credit_card: Made a PayPal domain RENEW plan ({mode}): ${renewal:.2f}/yr - {pid}")
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
        # no "mainlinehomeshomes.com" - skip a word the name already ends with
        names += [f"{sld}{s}.com" for s in SUFFIXES[1:] if len(sld + s) <= 63 and not sld.endswith(s) and not (s == "home" and sld.endswith("homes"))]
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


async def start(db: AsyncSession, user: User, domain: str, contact_in: dict, back: str, buttons: bool = False) -> dict:
    """buttons=True: PayPal's own on-page buttons make the subscription in the browser (they offer
    'Debit or Credit Card' without a PayPal login - the redirect page on our live account only
    offers PayPal login, user 11/10), so return the plan for them and wait for attach()."""
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
    # Name.com takes the cost from our account credit only - if it can't cover this one, stop
    # before the agent pays (rather than charging them and refunding a minute later).
    try:
        balance = await namecom_service.get_balance()
    except NamecomError:
        balance = None
    if balance is not None and balance < cost:
        _ping(f":rotating_light: Domain purchase BLOCKED - Name.com credit is ${balance:.2f}, {domain} costs ${cost:.2f}. "
              "Top up at name.com > Account > Account Credit.")
        raise PurchaseError("Buying a domain is paused for a moment - please try again later.")

    purchase = PurchasedDomain(owner_id=user.id, domain_name=domain, status="CHECKOUT", price_usd=price,
                               renewal_price_usd=renewal, cost_usd=cost, registrant=contact)
    db.add(purchase)
    await db.flush()
    if buttons:
        try:
            plan = await plan_id(db, price, renewal)
        except Exception:
            logger.exception("PayPal domain plan failed", extra={"domain": domain})
            await db.rollback()
            raise PurchaseError("PayPal didn't answer - try again in a minute.")
        purchase_id = purchase.id
        await db.commit()
        return {"purchase_id": purchase_id, "plan_id": plan, "approve_url": None}
    back = back if back in ("setup", "settings", "website") else "website"
    ret = f"{app_url()}/studio/domains/return?purchase={purchase.id}&back={back}"
    try:
        sub = await paypal_service.create_domain_subscription(
            await plan_id(db, price, renewal), purchase.id, ret, f"{ret}&cancelled=1",
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


async def attach(db: AsyncSession, purchase: PurchasedDomain, subscription_id: str) -> PurchasedDomain:
    """Buttons checkout: tie the subscription PayPal's buttons made to this purchase - only when
    PayPal says it's ours (our custom_id + this price pair's plan) - then finish as usual.
    Called by the page right after approval, and by the webhook if the page never got to."""
    from app.services.paypal_service import paypal_service
    sid = (subscription_id or "").strip()
    if not sid:
        raise PurchaseError("PayPal didn't send the payment back - try again.")
    if purchase.paypal_subscription_id == sid:
        return await complete(db, purchase)
    if purchase.status != "CHECKOUT":
        raise PurchaseError("This checkout has closed - start again.")
    used = (await db.execute(select(PurchasedDomain.id).where(PurchasedDomain.paypal_subscription_id == sid))).first()
    if used:
        raise PurchaseError("That payment is already used for another domain.")
    try:
        sub = await paypal_service.get_subscription(sid)
        want = await plan_id(db, purchase.price_usd, purchase.renewal_price_usd)
    except Exception:
        logger.warning("PayPal lookup failed attaching a domain payment", exc_info=True, extra={"purchase": purchase.id})
        raise PurchaseError("PayPal didn't answer - tap Finish to try again.")
    if sub.get("custom_id") != purchase.id or sub.get("plan_id") != want:
        logger.error("Domain payment mismatch", extra={"purchase": purchase.id, "subscription": sid})
        raise PurchaseError("That payment doesn't match this domain.")
    # Only an APPROVED payment is tied on (11/10: a first try PayPal only CREATED got tied on by its
    # webhook, then the real paid second try was refused).
    if (sub.get("status") or "").upper() not in ("ACTIVE", "APPROVED"):
        raise PurchaseError("PayPal hasn't confirmed this payment yet.")
    old = purchase.paypal_subscription_id
    if old:
        # An earlier try is tied on: replace it only if that one was never paid/approved.
        try:
            old_status = ((await paypal_service.get_subscription(old)).get("status") or "").upper()
        except Exception:
            raise PurchaseError("PayPal didn't answer - tap Finish to try again.")
        if old_status in ("ACTIVE", "APPROVED", "SUSPENDED"):
            logger.error("Second approved payment for one domain", extra={"purchase": purchase.id, "subscription": sid, "kept": old})
            _ping(f":warning: Domain {purchase.domain_name}: a 2nd approved PayPal subscription {sid} came in for one checkout (kept {old}) - refund/cancel {sid} by hand.")
            raise PurchaseError("This domain is already paid for.")
    claimed = await db.execute(update(PurchasedDomain).where(
        PurchasedDomain.id == purchase.id, PurchasedDomain.status == "CHECKOUT",
        (PurchasedDomain.paypal_subscription_id.is_(None) if not old else PurchasedDomain.paypal_subscription_id == old),
    ).values(paypal_subscription_id=sid))
    await db.commit()
    await db.refresh(purchase)
    if claimed.rowcount != 1 and purchase.paypal_subscription_id != sid:
        raise PurchaseError("That payment doesn't match this domain.")
    return await complete(db, purchase)


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
    """www -> CNAME at sites.openhousepal.com. Bare domain -> A record at the bare-domain redirect
    server (WEBSITE_APEX_IP, user 11/10: every page without www goes to the same page on www), or an
    ANAME at sites.openhousepal.com when that server isn't set. Clears Name.com's parking records first."""
    from app.services.website.domain_verification_service import WEBSITE_APEX_IP
    want_root = ("A", WEBSITE_APEX_IP) if WEBSITE_APEX_IP else ("ANAME", CNAME_TARGET)
    records = await namecom_service.list_records(domain)
    for r in records:
        host = (r.get("host") or "").lower()
        rtype = (r.get("type") or "").upper()
        if host in ("", "@", "www") and rtype in ("A", "AAAA", "CNAME", "ANAME"):
            answer = (r.get("answer") or "").rstrip(".").lower()
            if host == "www" and rtype == "CNAME" and answer == CNAME_TARGET:
                continue
            if host in ("", "@") and (rtype, answer) == (want_root[0], want_root[1].lower()):
                continue
            if r.get("id") is not None:
                await namecom_service.delete_record(domain, r["id"])
    have = {((r.get("host") or "").lower(), (r.get("type") or "").upper()) for r in await namecom_service.list_records(domain)}
    if ("www", "CNAME") not in have:
        await namecom_service.create_record(domain, "www", "CNAME", CNAME_TARGET)
    if ("", want_root[0]) not in have and ("@", want_root[0]) not in have:
        await namecom_service.create_record(domain, "", want_root[0], want_root[1])


async def hook_up(db: AsyncSession, purchase: PurchasedDomain) -> None:
    """DNS at Name.com + the site_domains pair + Cloudflare certificates. Safe to re-run."""
    from app.services.website import cloudflare_saas_service, domain_resolution_service
    domain = purchase.domain_name
    try:
        await set_dns(domain)
        from app.services.website.domain_verification_service import WEBSITE_APEX_IP
        if WEBSITE_APEX_IP:
            purchase.registrant = {**(purchase.registrant or {}), "_apex_ip": WEBSITE_APEX_IP}
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
            if event_type != "BILLING.SUBSCRIPTION.CANCELLED":
                purchase.last_error = "payment_failed"  # the domain card says so + offers Renew with card
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


def _renew_start(purchase: PurchasedDomain) -> Optional[str]:
    """When the first "Renew with card" charge lands: RENEW_LEAD_DAYS before expiry (when the
    yearly charge would have come anyway); None = now, when that's already past/close."""
    exp = purchase.expires_at if not purchase.expires_at or purchase.expires_at.tzinfo else purchase.expires_at.replace(tzinfo=timezone.utc)
    if not exp:
        return None
    when = exp - timedelta(days=RENEW_LEAD_DAYS)
    if when <= datetime.now(timezone.utc) + timedelta(hours=1):
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


async def renew_checkout(db: AsyncSession, purchase: PurchasedDomain) -> dict:
    """"Renew with card": renewing was stopped or the yearly card payment failed - start a new
    yearly card subscription for a domain they still own (before it ends)."""
    if purchase.status != "ACTIVE":
        raise PurchaseError("This domain can't be renewed here - contact support.")
    if purchase.auto_renew:
        raise PurchaseError("This domain already renews automatically.")
    exp = purchase.expires_at if not purchase.expires_at or purchase.expires_at.tzinfo else purchase.expires_at.replace(tzinfo=timezone.utc)
    if exp and exp < datetime.now(timezone.utc):
        raise PurchaseError("This domain has already ended - contact support to get it back.")
    try:
        plan = await renew_plan_id(db, purchase.renewal_price_usd)
    except Exception:
        logger.exception("PayPal domain renew plan failed", extra={"purchase": purchase.id})
        raise PurchaseError("PayPal didn't answer - try again in a minute.")
    return {"plan_id": plan, "custom_id": f"renew:{purchase.id}", "start_time": _renew_start(purchase)}


async def attach_renewal(db: AsyncSession, purchase: PurchasedDomain, subscription_id: str) -> PurchasedDomain:
    """The card button approved a "Renew with card" subscription - it becomes this domain's yearly
    payment (the PAYMENT.SALE.COMPLETED webhook then renews at Name.com via renew_if_due)."""
    from app.services.paypal_service import paypal_service
    sid = (subscription_id or "").strip()
    if not sid:
        raise PurchaseError("PayPal didn't send the payment back - try again.")
    if purchase.paypal_subscription_id == sid and purchase.auto_renew:
        return purchase
    if purchase.status != "ACTIVE":
        raise PurchaseError("This domain can't be renewed here - contact support.")
    used = (await db.execute(select(PurchasedDomain.id).where(PurchasedDomain.paypal_subscription_id == sid))).first()
    if used:
        raise PurchaseError("That payment is already used for another domain.")
    try:
        sub = await paypal_service.get_subscription(sid)
        want = await renew_plan_id(db, purchase.renewal_price_usd)
    except Exception:
        logger.warning("PayPal lookup failed attaching a domain renewal", exc_info=True, extra={"purchase": purchase.id})
        raise PurchaseError("PayPal didn't answer - tap Finish to try again.")
    if sub.get("custom_id") != f"renew:{purchase.id}" or sub.get("plan_id") != want:
        logger.error("Domain renewal payment mismatch", extra={"purchase": purchase.id, "subscription": sid})
        raise PurchaseError("That payment doesn't match this domain.")
    if (sub.get("status") or "").upper() not in ("ACTIVE", "APPROVED"):
        raise PurchaseError("PayPal hasn't confirmed this payment yet.")
    old = purchase.paypal_subscription_id
    if old and old != sid:
        # the stopped / failed one must never charge alongside the new one
        try:
            if ((await paypal_service.get_subscription(old)).get("status") or "").upper() in ("ACTIVE", "SUSPENDED", "APPROVED"):
                await paypal_service.cancel_subscription(old, "Replaced by Renew with card")
        except Exception:
            logger.error("Couldn't cancel the old domain subscription", exc_info=True, extra={"purchase": purchase.id, "old": old})
            _ping(f":warning: Domain {purchase.domain_name}: couldn't cancel old PayPal subscription {old} after Renew with card - cancel it by hand.")
    purchase.paypal_subscription_id = sid
    purchase.auto_renew = True
    if purchase.last_error == "payment_failed":
        purchase.last_error = None
    flags = {k: v for k, v in (purchase.registrant or {}).items() if not str(k).startswith("_warned")}
    purchase.registrant = flags
    await db.commit()
    await db.refresh(purchase)
    _ping(f":repeat: Domain renewing again: {purchase.domain_name} (${purchase.renewal_price_usd:.2f}/yr, card).")
    # Charged now (expiry is close)? PayPal's sale webhook renews it; this covers a fast/missed one.
    try:
        last = ((sub.get("billing_info") or {}).get("last_payment") or {})
        if _money(last.get("amount")) >= purchase.renewal_price_usd - 0.01:
            await renew_if_due(db, purchase, paid=True)
    except Exception:
        logger.warning("Immediate renew check failed", exc_info=True, extra={"purchase": purchase.id})
    return purchase


async def release_ended(db: AsyncSession, purchase: PurchasedDomain) -> None:
    """A bought domain ended (renewing off / card failed): the website and every new link go back
    to the free OpenHousePal address - never forward visitors or print QR codes onto a dead domain."""
    from app.services.website import cloudflare_saas_service, domain_resolution_service, site_service
    rows = (await db.execute(select(SiteDomain).where(
        SiteDomain.hostname.in_([purchase.domain_name, f"www.{purchase.domain_name}"]),
        SiteDomain.owner_id == purchase.owner_id,
    ))).scalars().all()
    if not rows:
        return
    was_primary = any(r.is_primary for r in rows)
    site_ids = {r.site_id for r in rows}
    for r in sorted(rows, key=lambda r: r.redirect_target_domain_id is None):  # partners first
        try:
            await cloudflare_saas_service.remove(r.hostname)
        except Exception:
            logger.warning("Cloudflare remove failed for an ended domain", exc_info=True, extra={"host": r.hostname})
        await db.delete(r)
    await db.commit()
    for sid_ in site_ids:
        domain_resolution_service.invalidate_site(sid_)
    if was_primary:
        owner = await db.get(User, purchase.owner_id)
        if owner:
            await site_service.regenerate_owner_idx_links(db, owner)
    _ping(f":hourglass_flowing_sand: Domain ended: {purchase.domain_name} - website back on its OpenHousePal address.")


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
    def _locked_until(when: Optional[datetime]) -> PurchaseError:
        return PurchaseError(f"This domain can be moved to another company from {when:%B} {when.day}, {when.year} "
                             "(every new domain has to wait 60 days).")
    try:
        # Name.com knows the exact end of the registry's 60-day lock - ours is only an estimate
        lock = _parse((await namecom_service.get_domain(purchase.domain_name)).get("transferLockExpiresAt"))
        if lock and lock > datetime.now(timezone.utc):
            raise _locked_until(lock)
        await namecom_service.unlock(purchase.domain_name)
        return await namecom_service.get_auth_code(purchase.domain_name)
    except NamecomError as e:
        m = re.search(r"until (\d{4}-\d{2}-\d{2}T[\d:]+Z)", e.message or "")
        if m and _parse(m.group(1)):
            raise _locked_until(_parse(m.group(1)))
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


CONFIRM_DAYS = 15   # ICANN: the registrant's email must be confirmed within 15 days or the domain pauses


async def confirm_reminders(db: AsyncSession, owner_id: str) -> list[dict]:
    """Owner, 11/11: every domain this agent bought whose Name.com "verify your email" link hasn't
    been clicked yet - shown on the Dash and the Domains screen until it's done. Once Name.com says
    confirmed we remember it and stop asking. confirmed None = Name.com couldn't tell us (then the
    agent may dismiss it)."""
    rows = (await db.execute(select(PurchasedDomain).where(
        PurchasedDomain.owner_id == owner_id, PurchasedDomain.status == "ACTIVE"))).scalars().all()
    out, changed = [], False
    now = datetime.now(timezone.utc)
    for p in rows:
        flags = dict(p.registrant or {})
        if flags.get("_email_ok") or flags.get("_confirm_dismissed"):
            continue
        ok = await email_confirmed(p)
        if ok is True:
            p.registrant = {**flags, "_email_ok": True}
            changed = True
            continue
        start = p.registered_at or p.created_at
        left = None
        if start is not None:
            start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            left = max(0, CONFIRM_DAYS - (now - start).days)
        out.append({"id": p.id, "domain": p.domain_name, "email": flags.get("email"), "confirmed": ok, "days_left": left})
    if changed:
        await db.commit()
    return out


async def dismiss_confirm_reminder(db: AsyncSession, p: PurchasedDomain) -> bool:
    """Only when we can't check with Name.com - a domain we know isn't confirmed keeps its reminder."""
    if await email_confirmed(p) is False:
        return False
    p.registrant = {**(p.registrant or {}), "_confirm_dismissed": True}
    await db.commit()
    return True


def as_dict(p: PurchasedDomain) -> dict:
    return {
        "id": p.id, "domain": p.domain_name, "status": p.status, "price": p.price_usd, "renewal_price": p.renewal_price_usd,
        "auto_renew": p.auto_renew, "registered_at": p.registered_at.isoformat() if p.registered_at else None,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None, "last_error": p.last_error,
        "email": (p.registrant or {}).get("email"),
    }


# ---------- the sweep (runs with the domain health job) ----------

def credit_alert_usd() -> float:
    return float(os.getenv("DOMAIN_CREDIT_ALERT_USD", "50"))


async def check_credit(db: AsyncSession) -> Optional[float]:
    """Once a day: Name.com account credit (pays every purchase + renewal) below $50 -> one
    Discord ping that day (user, 10/20). The day's check is remembered in app_cache so a deploy
    or restart doesn't ping again."""
    if not namecom_service.configured():
        return None
    from sqlalchemy import text
    from app.services import app_cache
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = "namecom_credit_checked"
    last = (await db.execute(text("SELECT value FROM app_cache WHERE key = :k"), {"k": key})).scalar_one_or_none()
    if last == today:
        return None
    try:
        balance = await namecom_service.get_balance()
    except NamecomError:
        return None  # try again on the next sweep
    await app_cache.save(key, today)
    if balance < credit_alert_usd():
        _ping(f":moneybag: Name.com credit is low: ${balance:.2f} left (alert below ${credit_alert_usd():.0f}). "
              "Top it up at name.com > Account > Account Credit - new domain purchases and yearly renewals "
              "are paid from it and stop when it runs out.")
    return balance


async def sweep(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    try:
        await check_credit(db)
    except Exception:
        logger.warning("Name.com credit check failed", exc_info=True)
    rows = (await db.execute(select(PurchasedDomain).where(PurchasedDomain.status.in_(["CHECKOUT", "REGISTERING", "ACTIVE"])))).scalars().all()
    for p in rows:
        try:
            created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc)
            if p.status == "CHECKOUT":
                if now - created > timedelta(days=2):
                    # Never approved - or approved but year one never got paid (card declined). Stop
                    # the subscription too, so PayPal doesn't keep retrying a domain we never bought.
                    if p.paypal_subscription_id:
                        try:
                            from app.services.paypal_service import paypal_service
                            sub = await paypal_service.get_subscription(p.paypal_subscription_id)
                            if (sub.get("status") or "").upper() in ("ACTIVE", "SUSPENDED"):
                                await paypal_service.cancel_subscription(p.paypal_subscription_id, "Domain checkout never paid")
                        except Exception:
                            logger.warning("Couldn't cancel an unpaid domain subscription", exc_info=True, extra={"purchase": p.id})
                    p.status = "ABANDONED"
                    await db.commit()
                else:
                    await complete(db, p)
            elif p.status == "REGISTERING" and now - (p.updated_at or created).replace(tzinfo=timezone.utc) > timedelta(minutes=30):
                _ping(f":hourglass: Domain {p.domain_name} stuck registering for 30+ min - check Name.com.")
            elif p.status == "ACTIVE":
                if p.last_error == "dns_setup":
                    await hook_up(db, p)
                # Bare-domain redirect server set (or changed): point this domain's root at it once.
                from app.services.website.domain_verification_service import WEBSITE_APEX_IP
                if WEBSITE_APEX_IP and (p.registrant or {}).get("_apex_ip") != WEBSITE_APEX_IP:
                    try:
                        await set_dns(p.domain_name)
                        p.registrant = {**(p.registrant or {}), "_apex_ip": WEBSITE_APEX_IP}
                        await db.commit()
                    except NamecomError:
                        logger.warning("Couldn't point the bare domain at the redirect server - next sweep retries",
                                       extra={"domain": p.domain_name})
                await make_primary_when_live(db, p)
                exp = p.expires_at if not p.expires_at or p.expires_at.tzinfo else p.expires_at.replace(tzinfo=timezone.utc)
                if exp and exp < now:
                    p.status = "EXPIRED"
                    await db.commit()
                    await release_ended(db, p)
                    await _email(db, p, "ended")
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
                note="", p2=f"To keep it, open your Domain screen and tap Renew with card before {exp} - it only takes "
                             "a minute, and you won't be charged twice."),
            "expiring": dict(
                subject=f"{d} ends on {exp}", eyebrow="Reminder", heading=f"{d} ends on {exp}", button="See my domain",
                preheader=f"{d} ends on {exp}.",
                p1=f"renewing is switched off for {d}, so it ends on {exp}. After that your website goes back to its free "
                   f"OpenHousePal address, and links that used {d} stop opening.",
                note="", p2="Want to keep it? Tap Renew with card on your Domain screen. Moving it to another company? "
                            "Get its transfer code from the Domain screen before it ends."),
            "ended": dict(
                subject=f"{d} has ended", eyebrow="Domain ended", heading=f"{d} has ended", button="See my domain",
                preheader=f"Your website is back on its OpenHousePal address.",
                p1=f"{d} wasn't renewed, so it has ended. Your website is back on its free OpenHousePal address, and every "
                   "new QR code and link you share uses that address now.",
                note="", p2="Want the name back? Contact OpenHousePal support - it can often be recovered for a short time."),
        }
        m = msgs[kind]
        subject = m.pop("subject")
        email_agent(owner, None, "domain_update", subject, {**m, "domain": d, "link": absolute("/studio/website?tab=domains")})
    except Exception:
        logger.warning("domain email failed", exc_info=True)
