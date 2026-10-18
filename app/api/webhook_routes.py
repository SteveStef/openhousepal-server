from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import time
import os
import re

from app.database import get_db
from app.models.database import User, WebhookEvent
from app.services.paypal_service import paypal_service
from app.services import portal_inbound_service
from app.utils.plan_ids import tier_for_plan_id, plan_id_has_trial
from app.config.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)

PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID")

# Mailgun (and SendGrid/Postmark/Cloudflare Email Workers, which POST the same fields) inbound
# parse -> a Lead. The address is leads-<inbound_lead_token>@<INBOUND_LEAD_DOMAIN>.
_INBOUND_RECIPIENT_RE = re.compile(r"leads-([A-Za-z0-9]+)@", re.I)


def inbound_route_key() -> str:
    """The secret on the Mailgun route's forward URL (?k=...) - derived from the Mailgun API key the
    server already has, so no extra setting is needed. Mailgun signs route forwards with its
    separate "HTTP webhook signing key", which this server may not have; the route secret lets the
    lead in either way."""
    api_key = os.getenv("MAILGUN_API_KEY") or ""
    return hmac.new(api_key.encode(), b"ohp-inbound-route", hashlib.sha256).hexdigest()[:40] if api_key else ""


def _verify_mailgun_inbound(form: dict) -> bool:
    """Mailgun signs inbound POSTs: hex HMAC-SHA256 of (timestamp + token). Newer Mailgun accounts
    sign with the separate "HTTP webhook signing key", older ones with the API key - accept a
    match on either, so a production setup with only one of them set still lets leads in. If no
    key is configured (local/dev), we accept - the token in the address is itself the secret."""
    keys = [k for k in (os.getenv("MAILGUN_WEBHOOK_SIGNING_KEY"), os.getenv("MAILGUN_API_KEY")) if k]
    ts, token, sig = form.get("timestamp"), form.get("token"), form.get("signature")
    if not keys:
        return True
    if not (ts and token and sig):
        return False  # a key is set but the post is unsigned -> reject
    try:
        if abs(time.time() - int(ts)) > 15 * 60:
            return False  # an old signed post being replayed
    except (TypeError, ValueError):
        return False
    return any(hmac.compare_digest(hmac.new(k.encode(), f"{ts}{token}".encode(), hashlib.sha256).hexdigest(), sig) for k in keys)


@router.post("/webhooks/paypal")
async def handle_paypal_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Receive and process PayPal webhook events.

    Handles subscription lifecycle events:
    - BILLING.SUBSCRIPTION.ACTIVATED - Trial converted to paid
    - BILLING.SUBSCRIPTION.CANCELLED - User cancelled
    - BILLING.SUBSCRIPTION.SUSPENDED - Payment failed
    - BILLING.SUBSCRIPTION.EXPIRED - Subscription ended
    - BILLING.SUBSCRIPTION.UPDATED - Plan changed
    - PAYMENT.SALE.COMPLETED - Recurring payment succeeded
    - PAYMENT.SALE.DENIED - Payment failed
    """
    try:
        # Extract webhook headers for signature verification
        transmission_id = request.headers.get("paypal-transmission-id")
        transmission_time = request.headers.get("paypal-transmission-time")
        cert_url = request.headers.get("paypal-cert-url")
        auth_algo = request.headers.get("paypal-auth-algo")
        transmission_sig = request.headers.get("paypal-transmission-sig")

        # Get the webhook body
        body = await request.json()

        # Verify webhook signature BEFORE processing anything
        if not all(
            [
                transmission_id,
                transmission_time,
                cert_url,
                auth_algo,
                transmission_sig,
                PAYPAL_WEBHOOK_ID,
            ]
        ):
            logger.error("SECURITY: Missing webhook verification headers or webhook ID")
            raise HTTPException(
                status_code=400, detail="Missing required webhook headers"
            )

        try:
            is_valid = await paypal_service.verify_webhook_signature(
                transmission_id=transmission_id,
                transmission_time=transmission_time,
                cert_url=cert_url,
                auth_algo=auth_algo,
                transmission_sig=transmission_sig,
                webhook_id=PAYPAL_WEBHOOK_ID,
                webhook_event=body,
            )

            if not is_valid:
                logger.error(
                    "SECURITY: Invalid webhook signature detected",
                    extra={"transmission_id": transmission_id},
                )
                raise HTTPException(status_code=401, detail="Invalid webhook signature")

            logger.info(
                "Webhook signature verified", extra={"transmission_id": transmission_id}
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("SECURITY: Webhook verification failed", exc_info=True)
            raise HTTPException(status_code=401, detail="Webhook verification failed")

        event_id = body.get("id")
        event_type = body.get("event_type")
        logger.info(
            "PayPal webhook received",
            extra={"event_type": event_type, "event_id": event_id},
        )

        if not event_id or not event_type:
            logger.warning("Webhook missing event_id or event_type")
            raise HTTPException(
                status_code=400, detail="Missing event ID or event_type"
            )

        existing = await db.execute(
            select(WebhookEvent).where(WebhookEvent.id == event_id)
        )
        if existing.scalar_one_or_none():
            logger.info(
                "Webhook event already processed, skipping",
                extra={"event_id": event_id},
            )
            return {"status": "already_processed", "event_id": event_id}

        # Chargeback / dispute events don't carry a subscription id (their resource points at a
        # transaction and a PP-D-* dispute id), so they can't flow through the subscription-lookup
        # path below. We don't auto-suspend on a dispute - that's a judgement call to make in the
        # PayPal dashboard - but we DO make it loud: an error-level log + a Discord ping so someone
        # goes and looks. Recorded in WebhookEvent so a re-send doesn't double-alert.
        if event_type and event_type.startswith("CUSTOMER.DISPUTE."):
            from app.services.discord_notifier import notifier
            _resource = body.get("resource", {}) or {}
            dispute_id = _resource.get("dispute_id") or _resource.get("id") or "unknown"
            reason = _resource.get("reason") or _resource.get("dispute_life_cycle_stage") or ""
            amount = (_resource.get("dispute_amount") or {}).get("value", "?")
            logger.error(
                "PayPal DISPUTE / chargeback filed - review in the PayPal dashboard",
                extra={"event_type": event_type, "dispute_id": dispute_id, "reason": reason, "amount": amount},
            )
            try:
                notifier.send(f":rotating_light: PayPal dispute {dispute_id} ({event_type.split('.')[-1]}) - amount {amount}, reason: {reason or 'n/a'}. Review it in PayPal.")
            except Exception:
                pass
            db.add(WebhookEvent(id=event_id, event_type=event_type))
            await db.commit()
            return {"received": True, "event_type": event_type, "message": "Dispute logged + alerted"}

        # Extract subscription ID from different possible locations
        resource = body.get("resource", {})
        # Prioritize billing_agreement_id because payment events use 'id' for the transaction ID
        # Subscription events don't have billing_agreement_id, so they will fall back to 'id'
        subscription_id = resource.get("billing_agreement_id") or resource.get("id")

        if not subscription_id:
            logger.warning(
                "Webhook missing subscription_id", extra={"event_type": event_type}
            )
            return {"received": True, "warning": "No subscription_id"}

        # Find user by subscription_id
        result = await db.execute(
            select(User).where(User.subscription_id == subscription_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            logger.warning(
                "User not found for subscription",
                extra={"subscription_id": subscription_id},
            )
            return {"received": True, "warning": "User not found"}

        logger.info(
            "Processing webhook for user",
            extra={"subscription_id": subscription_id, "user_id": user.id},
        )

        # Extract plan_id from resource
        plan_id = resource.get("plan_id")

        # Handle different event types
        now_utc = datetime.now(timezone.utc)
        prev_tier = user.plan_tier
        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            # Trial converted to paid, or subscription reactivated
            # Determine Tier and Status based on Plan ID
            is_trial = False
            resolved_tier = tier_for_plan_id(plan_id)
            promo_trial = False
            if resolved_tier is None:
                from app.services.promo_code_service import tier_for_promo_plan
                resolved_tier, promo_trial = await tier_for_promo_plan(db, plan_id)

            if resolved_tier is not None:
                from app.services.website.site_service import reset_idx_site_question_on_upgrade
                reset_idx_site_question_on_upgrade(user, prev_tier, resolved_tier)
                user.plan_tier = resolved_tier
                if plan_id_has_trial(plan_id) or promo_trial:
                    user.subscription_status = "TRIAL"
                    is_trial = True
                else:
                    user.subscription_status = "ACTIVE"
            else:
                # Unknown plan (fallback to active if not recognized)
                logger.warning(
                    f"Unknown plan_id {plan_id}, defaulting to ACTIVE",
                    extra={"subscription_id": subscription_id},
                )
                user.subscription_status = "ACTIVE"

            logger.info(
                f"Subscription activated: {user.plan_tier} - {user.subscription_status}",
                extra={"subscription_id": subscription_id, "user_id": user.id},
            )

            # Set Trial End Date if applicable
            if is_trial:
                billing_info = resource.get("billing_info", {})
                next_billing_time = billing_info.get("next_billing_time")

                if next_billing_time:
                    try:
                        user.trial_ends_at = datetime.fromisoformat(
                            next_billing_time.replace("Z", "+00:00")
                        )
                        user.next_billing_date = user.trial_ends_at
                    except Exception:
                        # Fallback if parsing fails
                        trial_days = int(os.getenv("TRIAL_PERIOD_DAYS", "14"))
                        user.trial_ends_at = now_utc + timedelta(days=trial_days)
                        user.next_billing_date = user.trial_ends_at
                else:
                    # Fallback if no billing time provided
                    trial_days = int(os.getenv("TRIAL_PERIOD_DAYS", "14"))
                    user.trial_ends_at = now_utc + timedelta(days=trial_days)
                    user.next_billing_date = user.trial_ends_at
            else:
                # If active (no trial), clear trial data
                user.trial_ends_at = None
                # Set next billing date (approx 1 month out for new active subs)
                user.next_billing_date = now_utc + timedelta(days=30)

            if not user.subscription_started_at:
                user.subscription_started_at = now_utc
            user.last_paypal_sync = now_utc

        elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
            # User cancelled subscription. Cancelling from Settings already set the exact end of
            # their paid time (read from PayPal before cancelling) - this event arrives after and
            # usually without next_billing_time, so never cut that access short.
            already_had_end = bool(user.next_billing_date and user.next_billing_date > now_utc)
            was_trial = user.subscription_status == "TRIAL"
            user.subscription_status = "CANCELLED"
            user.last_paypal_sync = datetime.now(timezone.utc)

            # Extract next_billing_date for grace period
            billing_info = resource.get("billing_info", {})
            next_billing_time = billing_info.get("next_billing_time")

            if already_had_end:
                pass
            elif next_billing_time:
                try:
                    # Parse ISO 8601 datetime from PayPal
                    user.next_billing_date = datetime.fromisoformat(
                        next_billing_time.replace("Z", "+00:00")
                    )
                    logger.info(
                        "Subscription cancelled with grace period",
                        extra={
                            "subscription_id": subscription_id,
                            "user_id": user.id,
                            "grace_until": next_billing_time,
                        },
                    )
                except Exception as e:
                    logger.warning("Failed to parse next_billing_time", exc_info=True)
            else:
                # Fallback: Calculate grace period manually if PayPal doesn't provide it
                # For ACTIVE users, we give a full month (30 days) from last billing
                # For TRIAL users, we give the remainder of their trial period

                trial_days = int(os.getenv("TRIAL_PERIOD_DAYS", "14"))
                is_paid_user = (not was_trial) or user.last_billing_date is not None

                grace_days = 30 if is_paid_user else trial_days

                if user.last_billing_date:
                    # User was billed recently - add 30 days from last billing
                    user.next_billing_date = user.last_billing_date + timedelta(
                        days=grace_days
                    )
                    grace_source = "last_billing_date"
                elif user.subscription_started_at:
                    # Calculate from subscription start date + grace period
                    user.next_billing_date = user.subscription_started_at + timedelta(
                        days=grace_days
                    )
                    grace_source = "subscription_started_at"
                else:
                    # Safety fallback: Give grace period from now
                    user.next_billing_date = datetime.now(timezone.utc) + timedelta(
                        days=grace_days
                    )
                    grace_source = "current_time"

                logger.info(
                    "Subscription cancelled with calculated grace period",
                    extra={
                        "subscription_id": subscription_id,
                        "user_id": user.id,
                        "grace_until": user.next_billing_date.isoformat(),
                        "grace_days": grace_days,
                        "grace_calculated_from": grace_source,
                    },
                )

        elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
            # Payment failed - subscription suspended
            user.subscription_status = "SUSPENDED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.warning(
                "Subscription suspended due to payment failure",
                extra={"subscription_id": subscription_id, "user_id": user.id},
            )

        elif event_type == "BILLING.SUBSCRIPTION.EXPIRED":
            # Subscription ended (after cancellation grace period)
            user.subscription_status = "EXPIRED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.info(
                "Subscription expired",
                extra={"subscription_id": subscription_id, "user_id": user.id},
            )

        elif event_type == "BILLING.SUBSCRIPTION.UPDATED":
            # Plan changed (upgrade/downgrade)
            new_plan_id = resource.get("plan_id")

            if new_plan_id:
                # Determine tier based on plan_id (any of the 4 tiers, trial or no-trial variant)
                resolved_tier = tier_for_plan_id(new_plan_id)
                if resolved_tier is None:
                    from app.services.promo_code_service import tier_for_promo_plan
                    resolved_tier = (await tier_for_promo_plan(db, new_plan_id))[0]
                if resolved_tier is not None:
                    from app.services.website.site_service import reset_idx_site_question_on_upgrade
                    reset_idx_site_question_on_upgrade(user, prev_tier, resolved_tier)
                    user.plan_tier = resolved_tier
                    new_tier = resolved_tier
                else:
                    new_tier = "UNKNOWN"

                logger.info(
                    "Subscription plan updated",
                    extra={
                        "subscription_id": subscription_id,
                        "user_id": user.id,
                        "new_tier": new_tier,
                        "new_plan_id": new_plan_id,
                    },
                )

                user.plan_id = new_plan_id

                # Important: Don't modify trial_ends_at here - preserve existing trial period
                # If user is switching plans during trial, they keep the same trial end date

            user.last_paypal_sync = datetime.now(timezone.utc)

        elif event_type == "PAYMENT.SALE.COMPLETED":
            # Recurring payment succeeded
            user.last_billing_date = datetime.now(timezone.utc)
            user.subscription_status = "ACTIVE"  # Ensure it's active
            # Move the next billing date forward (it's also the end of their paid time if they
            # cancel later). Payment events don't carry it, so ask PayPal; else assume a month.
            try:
                nbt = ((await paypal_service.get_subscription(subscription_id)).get("billing_info") or {}).get("next_billing_time")
                user.next_billing_date = datetime.fromisoformat(nbt.replace("Z", "+00:00")) if nbt else now_utc + timedelta(days=30)
            except Exception:
                user.next_billing_date = now_utc + timedelta(days=30)
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.info(
                "Payment completed",
                extra={"subscription_id": subscription_id, "user_id": user.id},
            )

        elif event_type == "PAYMENT.SALE.REFUNDED":
            # A refund is usually a goodwill gesture from us - it must not lock the agent out.
            # PayPal keeps billing the subscription; just make sure someone knows.
            user.last_paypal_sync = datetime.now(timezone.utc)
            try:
                from app.services.discord_notifier import notifier
                amount = (resource.get("amount") or {}).get("total", "?")
                notifier.send(f"PayPal refund of ${amount} for {user.email} (subscription {subscription_id}). Their account stays active - cancel in PayPal too if that was intended.")
            except Exception:
                pass

        elif event_type == "PAYMENT.SALE.DENIED":
            # Payment failed
            user.subscription_status = "SUSPENDED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.warning(
                "Payment issue",
                extra={
                    "subscription_id": subscription_id,
                    "user_id": user.id,
                    "issue_type": event_type,
                },
            )

        else:
            # Unknown event type - log it but don't fail
            logger.info(
                "Unhandled webhook event type", extra={"event_type": event_type}
            )
            return {
                "received": True,
                "event_type": event_type,
                "message": "Event type not handled",
            }

        # Record that we processed this event (for idempotency)
        webhook_record = WebhookEvent(id=event_id, event_type=event_type)
        db.add(webhook_record)

        # Save changes to database (both user updates and webhook event record)
        await db.commit()

        # The account notices PayPal's own emails can't give (what happens to their OpenHousePal
        # account). A new sign-up's welcome goes out from link_subscription instead.
        try:
            from app.services.agent_email_service import absolute, email_agent, nice_date, plan_name
            billing = absolute("/settings#billing")
            if event_type == "BILLING.SUBSCRIPTION.CANCELLED":
                email_agent(user, None, "subscription_cancelled", "Your OpenHousePal subscription has been cancelled",
                            {"access_end_date": nice_date(user.next_billing_date), "plan_name": plan_name(user.plan_tier), "link": billing}, always=True)
            elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
                email_agent(user, None, "subscription_suspended", "Your OpenHousePal account is paused",
                            {"plan_name": plan_name(user.plan_tier), "link": billing}, always=True)
            elif event_type == "PAYMENT.SALE.DENIED":
                amount = (resource.get("amount") or {}).get("total")
                email_agent(user, None, "payment_failed", "We couldn't process your OpenHousePal payment",
                            {"amount": f"${float(amount):,.2f}" if amount else None, "plan_name": plan_name(user.plan_tier), "link": billing}, always=True)
            elif event_type == "BILLING.SUBSCRIPTION.UPDATED" and prev_tier and user.plan_tier != prev_tier:
                email_agent(user, None, "subscription_plan_changed", f"Your plan changed to {plan_name(user.plan_tier)}",
                            {"old_plan": plan_name(prev_tier), "new_plan": plan_name(user.plan_tier),
                             "effective_date": nice_date(now_utc), "link": billing}, always=True)
        except Exception:
            logger.warning("billing notice email failed", exc_info=True)

        # Activation / plan change / payment back in good standing can all put an agent on a live
        # Showcase plan - build + send any showcases they missed while on Basic. A no-op for
        # everyone else (and idempotent, so a PayPal retry is harmless).
        if event_type in ("BILLING.SUBSCRIPTION.ACTIVATED", "BILLING.SUBSCRIPTION.UPDATED", "PAYMENT.SALE.COMPLETED"):
            from app.services.open_house_service import schedule_showcase_backfill
            schedule_showcase_backfill(user.id)

        logger.info(
            "Webhook processed successfully",
            extra={"user_id": user.id, "event_type": event_type},
        )

        return {
            "received": True,
            "event_type": event_type,
            "subscription_id": subscription_id,
            "user_email": user.email,
            "new_status": user.subscription_status,
        }

    except Exception as e:
        logger.error("Webhook processing error", exc_info=True)
        # Return 200 anyway so PayPal doesn't retry indefinitely
        return {"received": True, "error": str(e)}


@router.post("/webhooks/inbound-email")
async def handle_inbound_email(request: Request, db: AsyncSession = Depends(get_db)):
    """Inbound-parse webhook for forwarded portal leads (Zillow / Realtor.com / Homes.com /
    Facebook / anything). The email provider POSTs form fields; we pull the agent's token out of
    the recipient address, parse the message, and create a Lead. Always answers 200 (a non-2xx
    makes the provider retry the same un-parseable mail forever)."""
    try:
        form = dict(await request.form())
    except Exception:
        logger.warning("inbound-email: could not read form body")
        return {"status": "bad_request"}

    route_key = inbound_route_key()
    from_our_route = bool(route_key) and hmac.compare_digest(request.query_params.get("k", ""), route_key)
    if not from_our_route and not _verify_mailgun_inbound(form):
        logger.warning("inbound-email: signature verification failed")
        return {"status": "unauthorized"}

    recipient = (form.get("recipient") or form.get("to") or "").strip()
    m = _INBOUND_RECIPIENT_RE.search(recipient)
    if not m:
        logger.info("inbound-email: no leads-<token> recipient (%r)", recipient)
        return {"status": "no_token"}

    result = await portal_inbound_service.ingest(
        db,
        m.group(1),
        sender=(form.get("sender") or form.get("from") or "").strip(),
        subject=(form.get("subject") or "").strip(),
        # the full text - Mailgun's "stripped-text" drops quoted/forwarded parts, which is exactly
        # where a hand-forwarded lead lives
        body_plain=(form.get("body-plain") or form.get("text") or form.get("stripped-text") or ""),
        body_html=(form.get("body-html") or form.get("html") or ""),
    )
    logger.info("inbound-email: %s", result)
    return result
