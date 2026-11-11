"""Custom payment requests (admin, 11/11): "structure any deal and send a payment request right
away". The admin picks who it's for (a brokerage group, an agent, or just a name + email), a price,
one time or recurring and how often, the plan it gives and how many people it covers. The payer
gets a link (/pay/<token>, emailed or copied), enters any debit or credit card once on PayPal's
secure card form, and it then charges by itself.

Every request is its own PayPal plan (amount + cycle) and, once paid, its own subscription with
custom_id "payreq:<id>" - the same shape as every sign-up and domain payment, so the card button
(CardPayButtons) works unchanged. PayPal's webhook keeps the request in step: each payment, a
failed card, a cancellation, the last payment of a fixed run.

When it's paid: the group is marked active with its PayPal reference and next charge date, and the
plan (if one was picked) goes to the group's agents or the one agent, brokerage-paid. A failed or
cancelled payment never takes anyone's plan away by itself - the admin gets a Discord ping and the
request shows it, so they decide."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import BillingGroup, PaymentRequest, User

logger = get_logger(__name__)

CYCLES = ("once", "monthly", "quarterly", "yearly")
CYCLE_WORDS = {"once": "one time", "monthly": "every month", "quarterly": "every 3 months", "yearly": "every year"}
PLAN_NAMES = {"NO_MLS": "No-MLS Basic", "PRO_NO_MLS": "No-MLS Premium", "BASIC": "Basic", "PREMIUM": "Premium", "IDX": "IDX"}
CUSTOM_PREFIX = "payreq:"


class PaymentRequestError(Exception):
    pass


def _ping(msg: str) -> None:
    try:
        from app.services.discord_notifier import notifier
        notifier.send(msg)
    except Exception:
        pass


def money(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def price_line(req: PaymentRequest) -> str:
    """"$500 every month" / "$1,200 one time" / "$300 every 3 months, 12 payments"."""
    out = f"{money(req.amount)} {CYCLE_WORDS.get(req.cycle, req.cycle)}"
    if req.cycle != "once" and req.total_payments:
        out += f", {req.total_payments} payments"
    return out


def link(req: PaymentRequest) -> str:
    from app.services.agent_email_service import app_url
    return f"{app_url()}/pay/{req.token}"


def new_token() -> str:
    return secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")


# ------------------------------------------------------------------ PayPal plan

async def make_plan(db: AsyncSession, req: PaymentRequest) -> str:
    """A PayPal plan for this request's price + cycle (the product is made once and remembered)."""
    from app.services import app_cache
    from app.services.domain_purchase_service import _cached, _plan_ids
    from app.services.paypal_service import paypal_service
    mode = os.getenv("PAYPAL_MODE", "sandbox")
    product_key = f"paypal_custom_product:{mode}"
    product = await _cached(db, product_key)
    if not product:
        product = await paypal_service.create_custom_product()
        await app_cache.save(product_key, product)
        _plan_ids[product_key] = product
    return await paypal_service.create_custom_plan(product, f"OpenHousePal - {req.title}", float(req.amount), req.cycle, req.total_payments)


# ------------------------------------------------------------------ what the pay page shows

async def _names(db: AsyncSession, req: PaymentRequest) -> tuple[Optional[str], Optional[str]]:
    group = await db.get(BillingGroup, req.billing_group_id) if req.billing_group_id else None
    user = await db.get(User, req.user_id) if req.user_id else None
    uname = (f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email) if user else None
    return (group.name if group else None), uname


def can_pay(req: PaymentRequest) -> bool:
    return req.status in ("open", "past_due") and bool(req.paypal_plan_id)


async def public_view(db: AsyncSession, req: PaymentRequest) -> dict:
    group_name, user_name = await _names(db, req)
    return {
        "title": req.title, "note": req.note, "amount": req.amount, "cycle": req.cycle,
        "total_payments": req.total_payments, "price_line": price_line(req),
        "payer_name": req.payer_name or user_name, "group_name": group_name,
        "plan_name": PLAN_NAMES.get(req.plan_tier or ""), "seats": req.seats,
        "status": req.status, "payments_count": req.payments_count,
        "last_paid_at": req.last_paid_at, "next_charge_at": req.next_charge_at,
        "start": {"plan_id": req.paypal_plan_id, "custom_id": f"{CUSTOM_PREFIX}{req.id}"} if can_pay(req) else None,
    }


async def admin_view(db: AsyncSession, req: PaymentRequest) -> dict:
    group_name, user_name = await _names(db, req)
    return {
        "id": req.id, "token": req.token, "link": link(req), "title": req.title, "note": req.note,
        "amount": req.amount, "cycle": req.cycle, "total_payments": req.total_payments, "price_line": price_line(req),
        "billing_group_id": req.billing_group_id, "group_name": group_name,
        "user_id": req.user_id, "user_name": user_name,
        "payer_name": req.payer_name, "payer_email": req.payer_email,
        "plan_tier": req.plan_tier, "seats": req.seats, "status": req.status,
        "paypal_subscription_id": req.paypal_subscription_id,
        "payments_count": req.payments_count, "amount_paid_total": round(req.amount_paid_total or 0, 2),
        "last_paid_at": req.last_paid_at, "next_charge_at": req.next_charge_at,
        "sent_at": req.sent_at, "sent_count": req.sent_count, "cancelled_at": req.cancelled_at,
        "replaces_request_id": req.replaces_request_id,
        "created_at": req.created_at,
    }


# ------------------------------------------------------------------ paid: keep in step with PayPal

def _when(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


async def sync(db: AsyncSession, req: PaymentRequest) -> None:
    """Read the subscription and its payments back from PayPal - the one source for how many
    payments came in, the last one and the next charge (safe to run again and again)."""
    from app.services.paypal_service import paypal_service
    if not req.paypal_subscription_id:
        return
    sub = await paypal_service.get_subscription(req.paypal_subscription_id)
    status = (sub.get("status") or "").upper()
    info = sub.get("billing_info") or {}
    try:
        txns = await paypal_service.list_subscription_transactions(req.paypal_subscription_id, days=3700)
    except Exception:
        logger.warning("Couldn't list payment request transactions", exc_info=True, extra={"request": req.id})
        txns = None
    if txns is not None:
        done = [t for t in txns if (t.get("status") or "").upper() in ("COMPLETED", "PARTIALLY_REFUNDED")]
        req.payments_count = len(done)
        req.amount_paid_total = round(sum(float(((t.get("amount_with_breakdown") or {}).get("gross_amount") or {}).get("value") or 0) for t in done), 2)
        if done:
            req.last_paid_at = max((_when(t.get("time")) for t in done if _when(t.get("time"))), default=req.last_paid_at)
    last = info.get("last_payment") or {}
    if not req.payments_count and last.get("amount"):
        # The payments list can lag a minute behind the first charge.
        req.payments_count = 1
        req.amount_paid_total = float((last.get("amount") or {}).get("value") or req.amount)
        req.last_paid_at = _when(last.get("time")) or datetime.now(timezone.utc)
    req.next_charge_at = _when(info.get("next_billing_time")) if req.cycle != "once" else None
    if req.status == "cancelled":
        return
    if status in ("CANCELLED",):
        req.status = "cancelled"
    elif status == "EXPIRED":
        req.status = "paid" if req.cycle == "once" else "ended"
    elif status == "SUSPENDED" or float((info.get("outstanding_balance") or {}).get("value") or 0) > 0.009:
        req.status = "past_due"
    elif status in ("ACTIVE", "APPROVED"):
        req.status = "paid" if req.cycle == "once" else "active"
        if req.cycle == "once" and req.payments_count >= 1:
            req.next_charge_at = None


async def apply_benefits(db: AsyncSession, req: PaymentRequest, *, give_plan: bool = True) -> list[dict]:
    """Paid: the group / agent gets the deal. give_plan only on the first payment - every later
    payment just keeps the group's dates current, so an agent the admin moved to another plan
    (or added to the group later) is never switched back by a monthly charge.
    Returns agents who still had their own PayPal subscription (that keeps charging until it's
    cancelled in PayPal - same warning as Groups)."""
    from app.api.admin_groups_routes import _give_managed_plan
    from app.services.open_house_service import schedule_showcase_backfill
    had_paypal: list[dict] = []
    people: list[User] = []
    if req.billing_group_id:
        g = await db.get(BillingGroup, req.billing_group_id)
        if g:
            g.status = "active"
            g.paypal_reference = req.paypal_subscription_id
            g.amount = req.amount
            if req.cycle in ("monthly", "quarterly", "yearly"):
                g.billing_cycle = req.cycle
            if req.seats:
                g.seats = req.seats
            if req.plan_tier and give_plan:
                g.plan_tier = req.plan_tier
            if req.next_charge_at:
                g.next_invoice_on = req.next_charge_at.date()
            if not g.started_on:
                g.started_on = datetime.now(timezone.utc).date()
            if req.plan_tier and give_plan:
                people = list((await db.execute(select(User).where(User.billing_group_id == g.id))).scalars().all())
    if req.user_id and req.plan_tier and give_plan:
        u = await db.get(User, req.user_id)
        if u and u not in people:
            people.append(u)
    changed = []
    for u in people:
        if u.is_admin:
            continue
        if u.plan_tier == req.plan_tier and (u.subscription_status or "").upper() == "ACTIVE" and not u.subscription_id:
            continue
        if u.subscription_id:
            had_paypal.append({"id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email,
                               "subscription_id": u.subscription_id})
        _give_managed_plan(u, req.plan_tier)
        changed.append(u.id)
    await db.commit()
    for uid in changed:
        schedule_showcase_backfill(uid)
    if had_paypal:
        _ping(":warning: Payment request '" + req.title + "' is paid - these agents still had their own PayPal subscription "
              "(cancel it in PayPal or they pay twice): " + ", ".join(f"{p['name']} {p['subscription_id']}" for p in had_paypal))
    return had_paypal


async def attach(db: AsyncSession, req: PaymentRequest, subscription_id: str) -> PaymentRequest:
    """The card form approved: check PayPal says it's this request's payment, then it's paid."""
    from app.services.paypal_service import paypal_service
    sid = (subscription_id or "").strip()
    if not sid:
        raise PaymentRequestError("PayPal didn't send the payment back - try again.")
    if req.paypal_subscription_id == sid and req.status in ("active", "paid"):
        return req
    if req.status not in ("open", "past_due"):
        raise PaymentRequestError("This payment request is closed.")
    used = (await db.execute(select(PaymentRequest.id).where(PaymentRequest.paypal_subscription_id == sid, PaymentRequest.id != req.id))).first()
    if used:
        raise PaymentRequestError("That payment is already used for another request.")
    try:
        sub = await paypal_service.get_subscription(sid)
    except Exception:
        logger.warning("PayPal lookup failed attaching a payment request", exc_info=True, extra={"request": req.id})
        raise PaymentRequestError("PayPal didn't answer - tap Finish to try again.")
    if sub.get("custom_id") != f"{CUSTOM_PREFIX}{req.id}" or sub.get("plan_id") != req.paypal_plan_id:
        logger.error("Payment request mismatch", extra={"request": req.id, "subscription": sid})
        raise PaymentRequestError("That payment doesn't match this request.")
    if (sub.get("status") or "").upper() not in ("ACTIVE", "APPROVED"):
        raise PaymentRequestError("PayPal hasn't confirmed this payment yet.")
    old = req.paypal_subscription_id
    if old and old != sid:
        # A card that failed before: the old subscription must never charge alongside the new one.
        try:
            if ((await paypal_service.get_subscription(old)).get("status") or "").upper() in ("ACTIVE", "SUSPENDED", "APPROVED"):
                await paypal_service.cancel_subscription(old, "Replaced by a new card")
        except Exception:
            logger.error("Couldn't cancel the old payment request subscription", exc_info=True, extra={"request": req.id, "old": old})
            _ping(f":warning: Payment request '{req.title}': couldn't cancel old PayPal subscription {old} - cancel it by hand.")
    was_past_due = req.status == "past_due"
    req.paypal_subscription_id = sid
    req.status = "paid" if req.cycle == "once" else "active"
    try:
        await sync(db, req)
    except Exception:
        logger.warning("Payment request sync after attach failed", exc_info=True, extra={"request": req.id})
    await db.commit()
    await db.refresh(req)
    await apply_benefits(db, req, give_plan=not was_past_due)
    if req.replaces_request_id and not was_past_due:
        old_req = await db.get(PaymentRequest, req.replaces_request_id)
        if old_req is not None and old_req.id != req.id and old_req.status in ("open", "active", "past_due"):
            try:
                await cancel(db, old_req)
                _ping(f":repeat: '{old_req.title}' stopped - replaced by '{req.title}'.")
            except PaymentRequestError:
                _ping(f":warning: '{req.title}' is paid but the old '{old_req.title}' couldn't be stopped - press Stop charging on it.")
    who = req.payer_name or req.payer_email or "someone"
    _ping(f":moneybag: Payment request {'card updated' if was_past_due else 'PAID'}: '{req.title}' - {price_line(req)} by {who}.")
    return req


async def on_paypal_event(db: AsyncSession, req: PaymentRequest, event_type: str, resource: dict) -> None:
    """Every later PayPal event for this request's subscription."""
    before = req.status
    try:
        await sync(db, req)
    except Exception:
        logger.warning("Payment request webhook sync failed", exc_info=True, extra={"request": req.id, "event": event_type})
    if event_type in ("BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.PAYMENT.FAILED", "PAYMENT.SALE.DENIED") and req.status != "cancelled":
        req.status = "past_due"
    elif event_type == "BILLING.SUBSCRIPTION.CANCELLED" and req.status != "cancelled":
        req.status = "cancelled"
        req.cancelled_at = req.cancelled_at or datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(req)
    who = req.payer_name or req.payer_email or "the payer"
    if event_type == "PAYMENT.SALE.COMPLETED":
        await apply_benefits(db, req, give_plan=False)   # keeps the group's next charge date current
        _ping(f":moneybag: Payment received: '{req.title}' - {money(req.amount)} from {who} (payment {req.payments_count}).")
    elif req.status == "past_due" and before != "past_due":
        _ping(f":rotating_light: Payment FAILED: '{req.title}' ({price_line(req)}) - {who}'s card didn't go through. "
              "They can put in a new card at the same link; their plan is unchanged until you decide.")
        import asyncio
        to, vars_ = req.payer_email, email_vars(req)
        asyncio.get_running_loop().run_in_executor(None, lambda: _send(to, "failed", vars_, None))
    elif req.status == "cancelled" and before != "cancelled":
        _ping(f":x: Payment request cancelled in PayPal: '{req.title}' ({who}). No more charges - plans are unchanged; switch them on the Agents tab if the deal ended.")
    elif req.status == "ended" and before != "ended":
        _ping(f":checkered_flag: Payment request finished its {req.total_payments or ''} payments: '{req.title}' ({who}).")


async def cancel(db: AsyncSession, req: PaymentRequest) -> PaymentRequest:
    """Admin: stop it. Cancels the PayPal subscription (no more charges) - plans stay as they are."""
    from app.services.paypal_service import paypal_service
    if req.paypal_subscription_id and req.status in ("active", "past_due", "open"):
        try:
            st = ((await paypal_service.get_subscription(req.paypal_subscription_id)).get("status") or "").upper()
            if st in ("ACTIVE", "SUSPENDED", "APPROVED", "APPROVAL_PENDING"):
                await paypal_service.cancel_subscription(req.paypal_subscription_id, "Cancelled by OpenHousePal")
        except Exception:
            logger.error("Couldn't cancel a payment request subscription", exc_info=True, extra={"request": req.id})
            raise PaymentRequestError("PayPal didn't answer - nothing was cancelled. Try again in a minute.")
    req.status = "cancelled"
    req.cancelled_at = datetime.now(timezone.utc)
    req.next_charge_at = None
    await db.commit()
    await db.refresh(req)
    return req


# ------------------------------------------------------------------ the email

EMAIL_TEMPLATES = {"payment_request", "payment_request_failed", "group_invite"}


def render_invite(subject: str, v: dict) -> tuple[str, str, str]:
    """A brokerage group's invite: "<brokerage> set you up on OpenHousePal" + Create my account."""
    from app.services.invite_email_service import button, card, e, eyebrow, field, h1, ohp_header, para, shell
    who = v.get("group_name") or "Your brokerage"
    head = f"{who} set you up on OpenHousePal"
    rows = []
    if v.get("plan_name"):
        rows.append(field("Your plan", f'<strong style="color:#17140f;" class="ohp-ink">OpenHousePal {e(v["plan_name"])}</strong> - paid by {e(who)}', first=True))
    rows.append(field("What to do", "Create your account with this email address. Your plan switches on by itself - no card needed.", first=not rows))
    body = [eyebrow("You're invited"), h1(head),
            para(f"{e(who)} is covering your OpenHousePal account - open houses, sign-ins, follow-ups and more, all in one place."),
            card("".join(rows)), button("Create my account", v.get("link") or ""),
            para("Use this same email when you sign up so we know you're with them.", muted=True, center=True, top=14, size=13)]
    html = shell(title=head, preheader=f"{who} is covering your OpenHousePal plan", header_cells=ohp_header("Invite"),
                 body="".join(body), footer="Questions? Just reply to this email.")
    text = f"{head}\n{who} is covering your OpenHousePal account.\nCreate your account with this email: {v.get('link') or ''}"
    return subject, html, text


def send_invite(to: str, v: dict, reply_to: Optional[str]) -> bool:
    from app.services.email_service import EmailService
    try:
        code, _ = EmailService().send_simple_message(
            to_email=to, subject=f"{v.get('group_name') or 'Your brokerage'} set you up on OpenHousePal", template="group_invite",
            template_variables={**v, "_log_audience": "account"}, reply_to=reply_to, from_name="OpenHousePal")
        return code < 300
    except Exception:
        logger.warning("Group invite email failed", exc_info=True)
        return False


def render(template: str, subject: str, v: dict) -> tuple[str, str, str]:
    if template == "group_invite":
        return render_invite(subject, v)
    from app.services.invite_email_service import button, card, e, eyebrow, field, h1, ohp_header, para, shell
    failed = template == "payment_request_failed"
    first = (v.get("first_name") or "").strip()
    hi = f"Hi {e(first)}, " if first else ""
    title = v.get("title") or "Your OpenHousePal plan"
    if failed:
        head = "Your card didn't go through"
        lead = (f"{hi}the latest payment for {e(title)} didn&rsquo;t go through. "
                "Please put in a card again at the secure link below - it only takes a minute.")
        btn = "Update my card"
        tag = "Payment issue"
    else:
        head = title
        lead = f"{hi}here is your payment request from OpenHousePal."
        btn = "Review and pay"
        tag = "Payment request"
    rows = [field("Amount", f'<strong style="color:#17140f;" class="ohp-ink">{e(v.get("price_line") or "")}</strong>', first=True)]
    if v.get("covers"):
        rows.append(field("Covers", e(v["covers"])))
    if v.get("recurring"):
        rows.append(field("How it works", "Charged when you pay, then automatically on the same day each time. You can ask us to stop it any time."))
    body = [eyebrow(tag), h1(head), para(lead)]
    if v.get("note") and not failed:
        body.append(para(e(v["note"]).replace("\n", "<br>"), top=12))
    body.append(card("".join(rows)))
    body.append(button(btn, v.get("link") or ""))
    body.append(para("Pay with any debit or credit card - no PayPal account needed. Secure checkout by PayPal.", muted=True, center=True, top=14, size=13))
    footer = "Questions? Just reply to this email."
    html = shell(title=head, preheader=f"{title} - {v.get('price_line') or ''}", header_cells=ohp_header(tag), body="".join(body), footer=footer)
    text = [head, (f"Hi {first}," if first else ""), f"{title}: {v.get('price_line') or ''}"]
    if v.get("covers"):
        text.append(f"Covers: {v['covers']}")
    if v.get("note") and not failed:
        text.append(v["note"])
    text.append(f"{btn}: {v.get('link') or ''}")
    text.append("Pay with any debit or credit card - no PayPal account needed.")
    return subject, html, "\n".join(t for t in text if t)


def email_vars(req: PaymentRequest, covers: Optional[str] = None) -> dict:
    first = (req.payer_name or "").strip().split(" ")[0] if req.payer_name else ""
    return {"first_name": first, "title": req.title, "price_line": price_line(req), "note": req.note,
            "link": link(req), "covers": covers, "recurring": req.cycle != "once"}


def covers_line(req: PaymentRequest, group_name: Optional[str], user_name: Optional[str]) -> Optional[str]:
    bits = []
    plan = PLAN_NAMES.get(req.plan_tier or "")
    if plan:
        bits.append(f"OpenHousePal {plan}")
    if req.seats:
        bits.append(f"{req.seats} {'person' if req.seats == 1 else 'people'}")
    who = group_name or user_name
    if who:
        bits.append(who)
    return " · ".join(bits) or None


def send_email(req: PaymentRequest, *, kind: str = "request", covers: Optional[str] = None, reply_to: Optional[str] = None) -> bool:
    """Email the payer their link (or, kind="failed", the "card didn't go through" note)."""
    return _send(req.payer_email, kind, email_vars(req, covers), reply_to)


def _send(to: Optional[str], kind: str, v: dict, reply_to: Optional[str]) -> bool:
    if not to:
        return False
    from app.services.email_service import EmailService
    template = "payment_request_failed" if kind == "failed" else "payment_request"
    subject = (f"Action needed: your payment for {v.get('title')}" if kind == "failed" else f"Payment request: {v.get('title')}")
    try:
        code, _ = EmailService().send_simple_message(
            to_email=to, subject=subject, template=template,
            template_variables={**v, "_log_audience": "account"}, reply_to=reply_to, from_name="OpenHousePal",
        )
        return code < 300
    except Exception:
        logger.warning("Payment request email failed", exc_info=True, extra={"template": template})
        return False
