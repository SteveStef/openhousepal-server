from fastapi import APIRouter, HTTPException, Depends, status, Response, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta, datetime, timezone
import os
import secrets
from app.services.email_service import EmailService

from app.database import get_db
from app.schemas.user import UserCreate, User, UserLogin, Token, ForgotPasswordRequest, ResetPasswordRequest, ChangePasswordRequest
from app.services.user_service import UserService
from app.services.paypal_service import paypal_service
from app.services.property_service import property_service
from app.utils.auth import create_access_token, get_current_active_user, hash_password, verify_password, require_broker_authorization
from app.utils.plan_ids import tier_for_plan_id, plan_id_has_trial
from app.models.database import User as UserModel, DiscoveryPreferences
from app.services.verification_service import verification_service
from app.services import login_rate_limit_service
from app.services.discord_notifier import notifier
from app.utils.subscription_sync import sync_subscription_status
from app.utils.request_ip import get_client_ip
from app.config.logging import get_logger

router = APIRouter(prefix="/auth", tags=["authentication"])
logger = get_logger(__name__)

# PayPal Plan ID Constants (from environment variables)
BASIC_PLAN_ID = os.getenv("PAYPAL_BASIC_PLAN_ID")
PREMIUM_PLAN_ID = os.getenv("PAYPAL_PREMIUM_PLAN_ID")

@router.post("/send-verification-code", status_code=status.HTTP_200_OK)
async def send_verification_code(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Send a 6-digit verification code to the user's email.
    Stores form data temporarily for registration completion.
    """
    try:
        # Check if email is already registered
        existing_user = await UserService.get_user_by_email(db, user_data.email)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        is_dev_mode = os.getenv("DEV", 'false') == 'true' # we are defaulting to prod
        if user_data.is_bright and not is_dev_mode:
            logger.info("Skipping the mls id verification")
            valid_bright_mls_id = await property_service.bright_mls_id_exists(db, user_data.mls_id)
            if not valid_bright_mls_id:
                raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="The MLS ID is not listed with BRIGHT MLS."
                        )

            existing_mls_id = await UserService.get_user_by_mls_id(db, user_data.mls_id)
            if existing_mls_id:
                raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="This MLS ID is already associated with another account."
                        )

        # Check rate limit
        can_send, error_msg = await verification_service.can_send_code(user_data.email, db)
        if not can_send:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_msg
            )

        # Generate verification code
        code = verification_service.generate_code()

        # Store code and form data
        form_data = {
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "state": user_data.state,
            "brokerage": user_data.brokerage,
            "mls_id": user_data.mls_id,
            "license_number": user_data.license_number,
            "is_bright": user_data.is_bright,
            "password": user_data.password  # Will be hashed by verification_service
        }
        await verification_service.store_code(user_data.email, code, form_data, db)

        # Log code in development mode (emails are auto-masked by logging filter)
        if EmailService().is_dev:
            logger.info(
                "DEV MODE: Verification code generated",
                extra={
                    "code": code,
                    "recipient": f"{user_data.first_name} {user_data.last_name}"
                }
            )

        # Send email with verification code
        email_service = EmailService()
        email_service.send_simple_message(
            to_email=user_data.email,
            subject="Verify Your Email - Open House Pal",
            template="verify_code",
            template_variables={
                "agent_name": user_data.first_name,
                "verify_code": code,
                "expiration_minutes": "15"
            }
        )

        return {
            "success": True,
            "message": f"Verification code sent to {user_data.email}"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("sending verification code failed", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send verification code"
        )

@router.post("/verify-code", status_code=status.HTTP_200_OK)
async def verify_code(
    request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Verify the 6-digit code and automatically create the user account.
    """
    email = (request.get("email") or "").strip().lower()
    code = request.get("code")

    if not email or not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email and code are required"
        )

    # 1. Verify the code
    is_valid, error_msg = await verification_service.verify_code(email, code, db)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_msg)

    # 2. Retrieve the stored form data (which includes hashed password)
    form_data = await verification_service.get_form_data(email, db)
    if not form_data:
        raise HTTPException(status_code=400, detail="Registration data expired. Please start over.")

    # Someone may have finished signing up with this email in another tab meanwhile.
    if await UserService.get_user_by_email(db, email):
        raise HTTPException(status_code=400, detail="Email already registered. Please log in instead.")

    # 3. Create the User (restricted status)
    try:
        new_user = UserModel(
            email=email,
            hashed_password=form_data["password"], # Already hashed by verification_service.store_code
            first_name=form_data["first_name"],
            last_name=form_data["last_name"],
            state=form_data["state"],
            brokerage=form_data["brokerage"],
            mls_id=form_data.get("mls_id") or None,
            license_number=form_data.get("license_number") or None,
            subscription_status="PENDING_PAYMENT",
            # Bright agents wait for Bright to approve OpenHousePal's data feed (an admin then
            # authorizes them). Agents outside Bright have no feed to approve - straight to checkout.
            broker_authorized=form_data.get("is_bright", True) is False,
            plan_tier=None
        )
        db.add(new_user)
        await db.flush()
        await db.refresh(new_user)

        # 4. Create Discovery Preferences
        discovery_prefs = DiscoveryPreferences(
            user_id=new_user.id,
            brokerages=[new_user.brokerage] if new_user.brokerage else [],
            state=new_user.state
        )
        db.add(discovery_prefs)

        # 5. Clear verification data
        await verification_service.clear_verification(email, db)
        await db.commit()

        # Tell the team someone is waiting - they can't pay until an admin authorizes them.
        try:
            if new_user.mls_id:
                notifier.send(f"New agent signed up: {new_user.first_name} {new_user.last_name} ({new_user.brokerage}, MLS ID {new_user.mls_id}). "
                              f"Authorize them in Admin once Bright approves their feed.")
            else:
                notifier.send(f"New agent outside Bright MLS signed up: {new_user.first_name} {new_user.last_name} ({new_user.brokerage}, {new_user.state}, "
                              f"license {new_user.license_number}). No approval needed - they can pick a No-MLS plan now.")
        except Exception:
            pass

        # 5. Return access token
        access_token = create_access_token(data={"sub": new_user.id})
        
        return {
            "success": True,
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": new_user.id,
                "email": new_user.email,
                "first_name": new_user.first_name,
                "last_name": new_user.last_name,
                "broker_authorized": new_user.broker_authorized,
                "subscription_status": new_user.subscription_status
            }
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"User creation during verification failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to finalize registration")

@router.post("/resend-verification-code", status_code=status.HTTP_200_OK)
async def resend_verification_code(
    request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Resend a new verification code to the user's email.
    """
    email = (request.get("email") or "").strip().lower()

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is required"
        )

    # Resend code
    success, new_code, error_msg = await verification_service.resend_code(email, db)

    if not success:
        if "Too many" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_msg
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_msg
            )

    # Get form data to retrieve first name
    form_data = await verification_service.get_form_data(email, db)
    first_name = form_data.get('first_name', 'User') if form_data else 'User'

    # Print code to console in development mode
    if EmailService().is_dev:
        logger.info("Verification code generated", extra={"user_email": email, "code": new_code})

    # Send email with new code
    email_service = EmailService()
    email_service.send_simple_message(
        to_email=email,
        subject="Your New Verification Code - Open House Pal",
        template="verify_code",
        template_variables={
            "agent_name": first_name,
            "verify_code": new_code,
            "expiration_minutes": "15"
        }
    )

    return {
        "success": True,
        "message": "Verification code resent successfully"
    }

@router.post("/verify-bundle-code")
async def verify_bundle_code(
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(require_broker_authorization),
):
    """Check a checkout code. Admin-made codes (admin "Codes") come first; the old single-use
    bundle codes still work after that."""
    code_str = (request.get("code") or "").strip()
    if not code_str:
        raise HTTPException(status_code=400, detail="Enter a code first.")

    from sqlalchemy import select
    from app.models.database import BundleCode, PromoCode
    from app.services.promo_code_service import clean_code, find_usable
    from app.utils.plan_ids import tier_allowed_for, NOT_IN_BRIGHT_MESSAGE, display_name_for_tier

    if (await db.execute(select(PromoCode.id).where(PromoCode.code == clean_code(code_str)))).first():
        promo, why = await find_usable(db, code_str)
        if promo is None:
            raise HTTPException(status_code=400, detail=why)
        if not tier_allowed_for(current_user, promo.plan_tier):
            raise HTTPException(status_code=400, detail=NOT_IN_BRIGHT_MESSAGE)
        billed_by_us = promo.kind == "billed_by_us"
        return {
            "valid": True, "code": promo.code, "kind": promo.kind, "plan_tier": promo.plan_tier,
            "plan_name": display_name_for_tier(promo.plan_tier), "label": promo.label,
            "plan_id": None if billed_by_us else promo.paypal_plan_id,
            "custom_price": promo.custom_price if billed_by_us else None,
            "billing_cycle": (promo.billing_cycle or "monthly") if billed_by_us else None,
            "message": "Code applied!",
        }

    bundle_code = (await db.execute(select(BundleCode).where(BundleCode.code == code_str))).scalar_one_or_none()
    if not bundle_code:
        raise HTTPException(status_code=404, detail="That code isn't valid. Check the spelling and try again.")
    if bundle_code.is_used:
        raise HTTPException(status_code=400, detail="This code has already been used")
    bundle_plan_id = os.getenv("PAYPAL_BUNDLE_PLAN_ID")
    if not bundle_plan_id:
        logger.error("PAYPAL_BUNDLE_PLAN_ID not set in environment")
        raise HTTPException(status_code=500, detail="Special plan configuration missing")
    if not tier_allowed_for(current_user, "PREMIUM"):
        raise HTTPException(status_code=400, detail=NOT_IN_BRIGHT_MESSAGE)
    return {"valid": True, "code": code_str, "kind": "bundle", "plan_tier": "PREMIUM", "plan_name": "Premium",
            "plan_id": bundle_plan_id, "message": "Promo code applied successfully!"}


@router.post("/redeem-code")
async def redeem_billed_by_us_code(
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(require_broker_authorization),
):
    """A 'billed by us' code: switch the account on with no PayPal - OpenHousePal bills the agent
    directly (the same kind of plan as the admin's manual plan switch)."""
    from app.services.promo_code_service import claim, find_usable
    from app.utils.plan_ids import tier_allowed_for, NOT_IN_BRIGHT_MESSAGE, display_name_for_tier

    if current_user.subscription_id or (current_user.subscription_status not in (None, "PENDING_PAYMENT")):
        raise HTTPException(status_code=400, detail="You already have a plan on this account. Manage it in Settings > Billing.")
    promo, why = await find_usable(db, request.get("code") or "")
    if promo is None:
        raise HTTPException(status_code=400, detail=why)
    if promo.kind != "billed_by_us":
        raise HTTPException(status_code=400, detail="This code is used with PayPal checkout.")
    if not tier_allowed_for(current_user, promo.plan_tier):
        raise HTTPException(status_code=400, detail=NOT_IN_BRIGHT_MESSAGE)
    if not await claim(db, promo.id):
        await db.rollback()
        raise HTTPException(status_code=400, detail="That code was just used up. Please contact us.")

    from app.services.website.site_service import reset_idx_site_question_on_upgrade
    reset_idx_site_question_on_upgrade(current_user, current_user.plan_tier, promo.plan_tier)
    now = datetime.now(timezone.utc)
    current_user.plan_tier = promo.plan_tier
    current_user.subscription_status = "ACTIVE"
    current_user.subscription_id = None
    current_user.plan_id = None
    current_user.trial_ends_at = None
    current_user.next_billing_date = None
    current_user.subscription_started_at = now
    current_user.promo_code_id = promo.id
    if promo.plan_tier == "IDX":
        current_user.idx_setup_settled_at = now
    if promo.billing_group_id:
        current_user.billing_group_id = promo.billing_group_id
    await db.commit()
    await db.refresh(current_user)

    cycle = {"monthly": "month", "quarterly": "quarter", "yearly": "year"}.get(promo.billing_cycle or "monthly", "month")
    price = f"${promo.custom_price:,.2f}/{cycle}" if promo.custom_price is not None else "no price set"
    notifier.send(f"Agent {current_user.first_name} {current_user.last_name} ({current_user.email}) switched on "
                  f"{display_name_for_tier(promo.plan_tier)} with code {promo.code} - billed by us, {price}. Send them their first invoice.")
    from app.services.agent_email_service import absolute, email_agent, plan_name
    email_agent(current_user, None, "agent_welcome", f"Welcome to OpenHousePal, {current_user.first_name or 'friend'}",
                {"plan_name": plan_name(promo.plan_tier), "plan_tier": plan_name(promo.plan_tier), "trial_end_date": "",
                 "link": absolute("/dash")}, always=True)
    return {"success": True, "subscription_status": current_user.subscription_status, "plan_tier": current_user.plan_tier}


@router.post("/link-subscription")
async def link_subscription(
    subscription_id: str,
    plan_id: str,
    bundle_code: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(require_broker_authorization)
):
    """
    Link a PayPal subscription to an existing, authorized user account.
    Reuses the exact PayPal validation logic from the previous flow.
    """
    try:
        # A retry of the same link (the first answer got lost on a bad connection) is a no-op.
        if current_user.subscription_id and current_user.subscription_id == subscription_id:
            return {"success": True, "subscription_status": current_user.subscription_status, "plan_tier": current_user.plan_tier}

        # This endpoint is the first-time checkout only. Anyone who already had a subscription
        # (suspended, cancelled, expired - or still active) goes through Settings > Billing, so
        # they can't pick up a second free trial or end up paying for two subscriptions.
        if current_user.subscription_id or (current_user.subscription_status not in (None, "PENDING_PAYMENT")):
            raise HTTPException(status_code=400, detail="You already have a subscription on this account. Manage it in Settings > Billing.")

        # Step 1: Validate subscription with PayPal API
        try:
            subscription_details = await paypal_service.get_subscription(subscription_id)
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid subscription ID or PayPal service unavailable")

        # Step 2: Verify plan_id matches
        if subscription_details.get('plan_id') != plan_id:
            raise HTTPException(status_code=400, detail="Plan ID mismatch")

        # Step 3: Check subscription status - it must actually be approved on PayPal
        # (APPROVAL_PENDING means the buyer never finished approving it).
        if subscription_details.get('status') not in ['ACTIVE', 'APPROVED']:
            raise HTTPException(status_code=400, detail="PayPal hasn't confirmed this subscription yet. Please finish approving it on PayPal.")

        # Step 4: Determine plan tier - any of the 4 tiers, legacy or current plan ID
        BUNDLE_PLAN_ID = os.getenv("PAYPAL_BUNDLE_PLAN_ID")
        resolved_tier = tier_for_plan_id(plan_id)
        promo = None
        if bundle_code and not (BUNDLE_PLAN_ID and plan_id == BUNDLE_PLAN_ID):
            # An admin-made code with its own PayPal plan (e.g. IDX with no setup fee).
            from app.services.promo_code_service import find_usable
            promo, why = await find_usable(db, bundle_code)
            if promo is None:
                raise HTTPException(status_code=400, detail=why)
            if promo.kind != "paypal_plan" or promo.paypal_plan_id != plan_id:
                raise HTTPException(status_code=400, detail="This code doesn't go with that plan.")
            resolved_tier = promo.plan_tier
        if resolved_tier is not None and promo is None:
            from app.utils.plan_ids import SIGNUP_PLAN_IDS
            if plan_id not in SIGNUP_PLAN_IDS:
                # A plan-change / no-fee plan picked without the code that unlocks it.
                raise HTTPException(status_code=400, detail="That plan needs a code. Go back and pick your plan again.")
        if resolved_tier is not None:
            plan_tier = resolved_tier
        elif BUNDLE_PLAN_ID and plan_id == BUNDLE_PLAN_ID:
            if not bundle_code:
                raise HTTPException(status_code=400, detail="Promo code required for this plan")
            plan_tier = "PREMIUM"
        else:
            raise HTTPException(status_code=400, detail="Invalid plan configuration")
        from app.utils.plan_ids import tier_allowed_for, NOT_IN_BRIGHT_MESSAGE
        if not tier_allowed_for(current_user, plan_tier):
            raise HTTPException(status_code=400, detail=NOT_IN_BRIGHT_MESSAGE)

        # Step 5: Check if subscription already linked
        from sqlalchemy import select
        stmt = select(UserModel).where(UserModel.subscription_id == subscription_id)
        existing = await db.execute(stmt)
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Subscription already linked to another account")

        # Handle Bundle Code marking - only for the promo plan, and claimed atomically so two
        # people can't both redeem the same single-use code at the same moment.
        is_bundle_plan = bool(BUNDLE_PLAN_ID and plan_id == BUNDLE_PLAN_ID)
        if bundle_code and is_bundle_plan:
            from app.models.database import BundleCode
            from sqlalchemy import update
            claimed = await db.execute(
                update(BundleCode)
                .where(BundleCode.code == bundle_code, BundleCode.is_used.is_(False))
                .values(is_used=True, used_at=datetime.now(timezone.utc))
                .returning(BundleCode.code)
            )
            if claimed.first() is None:
                raise HTTPException(status_code=400, detail="This promo code is invalid or has already been used")
        if promo is not None:
            from app.services.promo_code_service import claim
            if not await claim(db, promo.id):
                raise HTTPException(status_code=400, detail="That code was just used up. Please contact us.")
            current_user.promo_code_id = promo.id
            if promo.billing_group_id:
                current_user.billing_group_id = promo.billing_group_id

        # Step 6: Update User Record
        now = datetime.now(timezone.utc)
        billing_info = subscription_details.get('billing_info', {})
        next_billing_time = billing_info.get('next_billing_time')
        
        if next_billing_time:
            trial_end = datetime.fromisoformat(next_billing_time.replace('Z', '+00:00'))
        else:
            trial_end = now + timedelta(days=int(os.getenv("TRIAL_PERIOD_DAYS", "14")))

        current_user.subscription_id = subscription_id
        current_user.plan_id = plan_id
        from app.services.website.site_service import reset_idx_site_question_on_upgrade
        reset_idx_site_question_on_upgrade(current_user, current_user.plan_tier, plan_tier)
        current_user.plan_tier = plan_tier
        # Trial only when the PayPal plan really has one (the promo plan's free period counts).
        has_trial = plan_id_has_trial(plan_id) or is_bundle_plan or bool(promo and promo.trial_days)
        current_user.subscription_status = "TRIAL" if has_trial else "ACTIVE"
        current_user.subscription_started_at = now
        current_user.trial_ends_at = trial_end if has_trial else None
        current_user.next_billing_date = trial_end
        if plan_tier == "IDX":
            current_user.idx_setup_settled_at = now  # paid with the sign-up plan, or waived by a code
        current_user.last_paypal_sync = now

        await db.commit()
        await db.refresh(current_user)

        # Welcome notification
        notifier.send(f"Agent {current_user.first_name} {current_user.last_name} has successfully linked a {plan_tier} subscription.")
        from app.services.agent_email_service import absolute, email_agent, nice_date, plan_name
        email_agent(current_user, None, "agent_welcome", f"Welcome to OpenHousePal, {current_user.first_name or 'friend'}",
                    {"plan_name": plan_name(plan_tier), "plan_tier": plan_name(plan_tier), "trial_end_date": nice_date(trial_end),
                     "link": absolute("/dash")}, always=True)

        return {
            "success": True,
            "subscription_status": current_user.subscription_status,
            "plan_tier": current_user.plan_tier
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Subscription linking failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to link subscription. Please contact support.")



@router.post("/login", response_model=Token)
async def login(
    user_credentials: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate user and return access token
    """
    try:
        email = user_credentials.email.strip().lower()
        client_ip = get_client_ip(request)

        allowed, block_message = await login_rate_limit_service.check_login_allowed(db, email, client_ip)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=block_message,
            )

        # Authenticate user
        user = await UserService.authenticate_user(
            db,
            user_credentials.email,
            user_credentials.password
        )

        if not user:
            await login_rate_limit_service.record_attempt(db, email, client_ip, success=False)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
            )

        await login_rate_limit_service.record_attempt(db, email, client_ip, success=True)

        # This is for just incase the webhook for this user misses
        if user.subscription_id:
            try:
                await sync_subscription_status(user, db)
            except Exception as e:
                logger.error(f"Failed to sync subscription on login for user {user.id}", exc_info=True)

        # Create access token
        access_token = create_access_token(
            data={"sub": user.id}
        )
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "state": user.state,
                "brokerage": user.brokerage,
                "mls_id": user.mls_id
            }
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login failed"
        )

@router.post("/logout")
async def logout():
    """
    Logout user (client-side token removal)
    """
    return {"message": "Logout successful"}

@router.get("/me", response_model=User)
async def get_current_user_profile(
    current_user: UserModel = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get current authenticated user's profile with "Smart Sync" to handle trial transitions.
    Only syncs if the user has a subscription and their trial has expired.
    """
    now = datetime.now(timezone.utc)
    
    # Check for the "Dead Zone" edge case (Trial expired but status not yet ACTIVE)
    is_trial_expired = (
        current_user.subscription_status == "TRIAL" and 
        current_user.trial_ends_at and 
        current_user.trial_ends_at < now
    )

    # Trial just ran out: check PayPal now (they've probably just been charged) so they aren't
    # locked out for even one page load.
    if current_user.subscription_id and is_trial_expired:
        try:
            # This updates the DB and the current_user object in-place
            await sync_subscription_status(current_user, db)
        except Exception as e:
            logger.error(f"Background sync failed for user {current_user.id}: {e}")
    elif current_user.subscription_id and (
        not current_user.last_paypal_sync or now - current_user.last_paypal_sync > timedelta(hours=6)
    ):
        # Safety net for a PayPal webhook that never arrived - re-check in the background at
        # most every few hours, never slowing this page load down.
        _schedule_background_sync(current_user.id)

    return current_user


_sync_in_flight: dict[str, datetime] = {}
_bg_tasks: set = set()  # strong refs so a running check isn't garbage-collected mid-flight


def _schedule_background_sync(user_id: str) -> None:
    import asyncio
    now = datetime.now(timezone.utc)
    started = _sync_in_flight.get(user_id)
    if started and now - started < timedelta(minutes=15):
        return  # already checking (or PayPal was down a moment ago) - don't pile up
    _sync_in_flight[user_id] = now

    async def run():
        from app.database import AsyncSessionLocal
        from sqlalchemy import select as _select
        try:
            async with AsyncSessionLocal() as s:
                u = (await s.execute(_select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
                if u and u.subscription_id:
                    await sync_subscription_status(u, s)
        except Exception:
            logger.warning("background PayPal sync failed", exc_info=True, extra={"user_id": user_id})

    try:
        task = asyncio.get_running_loop().create_task(run())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        pass

@router.post("/forgot-password")
async def forgot_password(
    request: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Request a password reset email with a time-limited token
    """
    email = request.email

    try:
        from sqlalchemy import select
        from app.models.database import PasswordResetToken

        user = await UserService.get_user_by_email(db, email)

        success_response = {"message": "If an account exists with this email, you will receive a password reset link shortly."}

        if not user:
            return success_response

        # At most 3 reset emails an hour per account, so nobody can flood someone's inbox.
        from sqlalchemy import func as _f
        recent = (await db.execute(
            select(_f.count(PasswordResetToken.id)).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.expires_at > datetime.now(timezone.utc),  # issued within the last hour
            )
        )).scalar() or 0
        if recent >= 3:
            return success_response

        # Generate secure token
        token = secrets.token_urlsafe(32)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=1)

        reset_token = PasswordResetToken(
            user_id=user.id,
            token=token,
            expires_at=expires_at,
            used=False
        )

        db.add(reset_token)
        await db.commit()

        reset_link = f"{os.getenv('CLIENT_URL')}/reset-password?token={token}"
        email_service = EmailService()
        status_code, response = email_service.send_simple_message(
            to_email=email,
            subject="Reset Your Password - OpenHousePal",
            template="password_reset",
            template_variables={"reset_link": reset_link}
        )

        return success_response

    except Exception as e:
        logger.error("in forgot_password failed", extra={"error": str(e)})
        # Don't reveal errors to prevent information leakage
        return {"message": "If an account exists with this email, you will receive a password reset link shortly."}

@router.post("/reset-password")
async def reset_password(
    request: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Reset user password using a valid token
    """
    try:
        from sqlalchemy import select
        from app.models.database import PasswordResetToken
        from app.utils.auth import hash_password

        # Find the token
        result = await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token == request.token)
        )
        reset_token = result.scalar_one_or_none()

        # Validate token exists
        if not reset_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )

        # Check if token has been used
        if reset_token.used:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This reset link has already been used"
            )

        # Check if token has expired
        now = datetime.now(timezone.utc)
        if now > reset_token.expires_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This reset link has expired"
            )

        # Get the user
        user_result = await db.execute(
            select(UserModel).where(UserModel.id == reset_token.user_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Update password
        user.hashed_password = hash_password(request.new_password)

        # Mark token as used - and every other outstanding link for this account
        reset_token.used = True
        from sqlalchemy import update as _update
        await db.execute(_update(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.used.is_(False)).values(used=True))

        # Commit changes
        await db.commit()

        return {"message": "Password reset successful"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("in reset_password failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password"
        )


@router.post("/change-password", status_code=status.HTTP_200_OK)
async def change_password(
    request: ChangePasswordRequest,
    current_user: UserModel = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Change your password while logged in. Proves it's you by re-checking the current password
    (there is no emailed code / 2FA in the system). The emailed forgot-password link flow is the
    path for someone who can't sign in."""
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    if verify_password(request.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current one.",
        )
    current_user.hashed_password = hash_password(request.new_password)
    await db.commit()
    return {"message": "Password changed."}

