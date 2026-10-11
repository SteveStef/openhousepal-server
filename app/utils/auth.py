from passlib.context import CryptContext
from typing import Optional
from datetime import datetime, timedelta, timezone
from app.models.database import User, PlanTier
from jose import JWTError, jwt
from fastapi import HTTPException, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
import os
from dotenv import load_dotenv

from app.database import get_db

load_dotenv()

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    if os.getenv("DEV", "false") == "true":
        JWT_SECRET_KEY = "dev-only-secret-do-not-use-in-production"
    else:
        raise RuntimeError(
            "JWT_SECRET_KEY environment variable is not set. Refusing to start with a "
            "guessable default outside of DEV mode - set JWT_SECRET_KEY before deploying."
        )
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24  # 24 hours

# Security scheme for Bearer tokens
security = HTTPBearer()


def hash_password(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[str]:
    """Verify a JWT token and return the user_id"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str | None = payload.get("sub", None)
        if user_id is None:
            return None
        return user_id
    except JWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Get the current authenticated user from Bearer token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        token = credentials.credentials
        user_id = verify_token(token)
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    except Exception:
        raise credentials_exception

    # Import here to avoid circular imports
    from app.services.user_service import UserService

    user = await UserService.get_user_by_id(db, user_id=user_id)
    if user is None:
        raise credentials_exception

    # Admin activity tracker: last seen + days active (throttled, background, never fails a request).
    from app.services.agent_activity_tracker import touch
    touch(user.id)
    return user


async def get_current_active_user(current_user=Depends(get_current_user)):
    """Get the current authenticated and active user"""
    # if not current_user.is_active:
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="Inactive user"
    #     )
    return current_user


async def require_broker_authorization(current_user=Depends(get_current_active_user)):
    """
    Require the user to have been authorized by a broker/admin.
    Admins are always authorized.
    """
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")

    if current_user.email == ADMIN_EMAIL or current_user.broker_authorized:
        return current_user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Broker authorization required to access this feature.",
    )


async def require_admin_user(current_user=Depends(get_current_active_user)):
    """
    Require the user to be the master administrator.
    """
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")

    if current_user.email != ADMIN_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required.",
        )
    return current_user


def _assert_active_subscription(current_user) -> None:
    """Tier-agnostic status check: any of the 4 plan tiers passes as long as the subscription
    itself is ACTIVE, in a valid TRIAL, or CANCELLED-but-still-in-its-paid grace period.
    Shared by every capability dependency below - none of them re-derive this logic."""
    now = datetime.now(timezone.utc)

    if current_user.subscription_status == "ACTIVE":
        return

    if current_user.subscription_status == "TRIAL":
        if current_user.trial_ends_at and current_user.trial_ends_at > now:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your free trial has expired. Please subscribe to continue using this feature.",
        )

    if current_user.subscription_status == "CANCELLED":
        if current_user.trial_ends_at and current_user.trial_ends_at > now:
            return
        if current_user.next_billing_date and current_user.next_billing_date > now:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Your subscription has ended. Please resubscribe to continue using this feature.",
    )


# Bright-ladder rank, used ONLY for genuine upgrade-tier comparisons (Open House Manager,
# Blogs, Design Studio, Showcases need PREMIUM+; the IDX site needs IDX). NO_MLS and PRO_NO_MLS
# are deliberately absent - they're a parallel track, not the bottom rung, so they can never be
# swept into a `rank >= minimum` check by accident.
PLAN_TIER_RANK = {PlanTier.BASIC.value: 1, PlanTier.PREMIUM.value: 2, PlanTier.IDX.value: 3}

# Tiers with real Bright MLS data access - an explicit membership set, not "rank >= BASIC",
# so a future tier can't silently gain or lose MLS access by falling on the wrong side of
# a rank comparison. PRO_NO_MLS, like NO_MLS, is deliberately NOT here.
MLS_ACCESS_TIERS = {PlanTier.BASIC.value, PlanTier.PREMIUM.value, PlanTier.IDX.value}

# Agent Hub is a genuinely bifurcated capability, not derivable from rank: PRO_NO_MLS gets a
# manual-entry mode, PREMIUM/IDX get the full Bright-linked mode. BASIC and plain NO_MLS get
# neither - it moved off NO_MLS's own card onto PRO_NO_MLS's, so it upsells that tier now.
AGENT_HUB_ACCESS_TIERS = {
    PlanTier.PRO_NO_MLS.value, PlanTier.PREMIUM.value, PlanTier.IDX.value,
}

# Email Signature: narrower than Agent Hub's own set - IDX only on the Bright ladder (deliberately
# NOT plain PREMIUM), plus PRO_NO_MLS ("Premium" on the No-MLS track, manual-entry mode: no Bright
# pull for recent sales/listings, same restriction Agent Hub already enforces for that tier).
EMAIL_SIGNATURE_ACCESS_TIERS = {
    PlanTier.PRO_NO_MLS.value, PlanTier.IDX.value,
}

# Design Studio + Blogs: the PREMIUM-ladder Studio features that don't depend on Bright MLS
# data, so PRO_NO_MLS gets them too (its whole pitch over NO_MLS). Still an explicit set rather
# than "rank >= PREMIUM or PRO_NO_MLS" so Showcases / Open House Manager / the IDX site (which
# DO lean on Bright data) stay out of reach for PRO_NO_MLS.
STUDIO_DESIGN_ACCESS_TIERS = {
    PlanTier.PREMIUM.value, PlanTier.IDX.value, PlanTier.PRO_NO_MLS.value,
}

# Studio AI Media tool (twilight / lights-on / virtual staging / enhancement / animate).
# IDX gets it with listing-photo access; PRO_NO_MLS gets it upload-only (no Bright data) -
# that branch is enforced in studio_media_service via tier_has_mls(), not here.
STUDIO_MEDIA_ACCESS_TIERS = {PlanTier.IDX.value, PlanTier.PRO_NO_MLS.value}

# Client Reviews (public review link + funnel + Google/Zillow nudge + import). A Premium-and-up
# differentiator: PREMIUM, IDX, and PRO_NO_MLS ("Premium" on the No-MLS track). NO_MLS Basic and
# BASIC do NOT get Reviews - same membership set as Design Studio / Blogs.
REVIEWS_ACCESS_TIERS = {
    PlanTier.PREMIUM.value, PlanTier.IDX.value, PlanTier.PRO_NO_MLS.value,
}


# Showcases (collections) - the Bright ladder's Premium and up. The No-MLS track never gets them
# (no Bright data to match homes against). Membership set, same reasoning as the ones above.
SHOWCASE_ACCESS_TIERS = {PlanTier.PREMIUM.value, PlanTier.IDX.value}

# OpenHousePal Luxe - the elevated Agent Hub theme and the Luxe article theme. IDX only; every
# other plan can preview it in the editor (an upgrade pitch) but the public page falls back to
# the standard look. The saved choice is kept, so upgrading brings it straight back.
LUXE_THEME_ACCESS_TIERS = {PlanTier.IDX.value}


def luxe_theme_allowed(user) -> bool:
    return getattr(user, "plan_tier", None) in LUXE_THEME_ACCESS_TIERS


# The Luxe ARTICLE theme is wider than the rest of Luxe: IDX on the Bright ladder plus the top of
# the No-MLS track (PRO_NO_MLS, "No-MLS Premium"). Mirrors luxeBlogAllowed in frontend navAccess.ts.
LUXE_BLOG_THEME_ACCESS_TIERS = {PlanTier.IDX.value, PlanTier.PRO_NO_MLS.value}


def luxe_blog_theme_allowed(user) -> bool:
    return getattr(user, "plan_tier", None) in LUXE_BLOG_THEME_ACCESS_TIERS


def tier_has_mls(plan_tier) -> bool:
    """True if this plan tier includes real Bright MLS data access. Used by
    studio_media_service to decide whether a 'listing' photo source is allowed."""
    return plan_tier in MLS_ACCESS_TIERS


def subscription_is_active(user) -> bool:
    """Non-raising twin of _assert_active_subscription, for the public/anonymous paths that serve
    an agent's content to someone else (Agent Hub, forms, showcases, signature links, reviews) and
    for background jobs - there's no request-scoped current_user to gate through a dependency."""
    if not user:
        return False
    now = datetime.now(timezone.utc)
    status_value = getattr(user, "subscription_status", None)
    if status_value == "ACTIVE":
        return True
    if status_value == "TRIAL":
        return bool(user.trial_ends_at and user.trial_ends_at > now)
    if status_value == "CANCELLED":
        if user.trial_ends_at and user.trial_ends_at > now:
            return True
        return bool(user.next_billing_date and user.next_billing_date > now)
    return False


def owner_has_access(user, tiers=None) -> bool:
    """True when this agent's content should still be live for the public: an active (or
    still-in-grace) subscription, and - when `tiers` is given - a plan that includes the feature.
    Once a subscription ends or the agent downgrades out of a feature, its public pages/links stop
    serving (the data itself is kept, so resubscribing/upgrading brings everything back)."""
    if not subscription_is_active(user):
        return False
    return tiers is None or getattr(user, "plan_tier", None) in tiers


async def require_active_plan(current_user=Depends(get_current_active_user)):
    """Any active tier, including NO_MLS - for features every paying agent gets: Dashboard,
    Leads, Forms, Brand Kit, and (internally branching) Open House Sign-In/Cheat Sheets."""
    _assert_active_subscription(current_user)
    return current_user


# Old name, kept as an alias so existing call sites for genuinely tier-agnostic features don't
# all need touching - identical to require_active_plan.
require_basic_plan = require_active_plan


async def require_mls_access(current_user=Depends(get_current_active_user)):
    """Real Bright MLS data access - My Listings, Discovery, base CMA. NO_MLS is explicitly
    excluded; this is a membership check, not a rank comparison."""
    _assert_active_subscription(current_user)
    if current_user.plan_tier not in MLS_ACCESS_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature requires Bright MLS access. Your plan doesn't include MLS data.",
        )
    return current_user


async def require_agent_hub_access(current_user=Depends(get_current_active_user)):
    """Agent Hub: PRO_NO_MLS (manual-entry mode), PREMIUM, or IDX (full Bright-linked mode).
    BASIC and plain NO_MLS do not get Agent Hub at all - see AGENT_HUB_ACCESS_TIERS."""
    _assert_active_subscription(current_user)
    if current_user.plan_tier not in AGENT_HUB_ACCESS_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent Hub requires a Premium plan (or the No-MLS plan's manual mode). Please upgrade to access it.",
        )
    return current_user


async def require_email_signature_access(current_user=Depends(get_current_active_user)):
    """Email Signature: IDX, or PRO_NO_MLS (manual-entry mode) - narrower than Agent Hub's own
    access set. Plain PREMIUM does NOT get this. See EMAIL_SIGNATURE_ACCESS_TIERS."""
    _assert_active_subscription(current_user)
    if current_user.plan_tier not in EMAIL_SIGNATURE_ACCESS_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email Signature requires the IDX plan (or the No-MLS plan's Pro tier). Please upgrade to access it.",
        )
    return current_user


async def require_studio_design_access(current_user=Depends(get_current_active_user)):
    """Design Studio + Blogs: PREMIUM, IDX, or PRO_NO_MLS. Membership check, not a rank
    comparison - see STUDIO_DESIGN_ACCESS_TIERS."""
    _assert_active_subscription(current_user)
    if current_user.plan_tier not in STUDIO_DESIGN_ACCESS_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature requires the Premium plan or higher. Please upgrade to access it.",
        )
    return current_user


async def require_reviews_access(current_user=Depends(get_current_active_user)):
    """Client Reviews: PREMIUM, IDX, or PRO_NO_MLS only - see REVIEWS_ACCESS_TIERS. Membership
    check, not a rank comparison (NO_MLS Basic is a parallel track, not the ladder's bottom)."""
    _assert_active_subscription(current_user)
    if current_user.plan_tier not in REVIEWS_ACCESS_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client Reviews requires the Premium plan or higher. Please upgrade to access it.",
        )
    return current_user


async def require_studio_media_access(current_user=Depends(get_current_active_user)):
    """Studio AI Media tool: IDX or PRO_NO_MLS only - see STUDIO_MEDIA_ACCESS_TIERS. The
    listing-vs-upload photo-source branch for PRO_NO_MLS is enforced downstream in
    studio_media_service, not here."""
    _assert_active_subscription(current_user)
    if current_user.plan_tier not in STUDIO_MEDIA_ACCESS_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The AI Media tool requires the IDX or Pro No-MLS plan. Please upgrade to access it.",
        )
    return current_user


def require_plan_tier(minimum: PlanTier):
    """Rank-based gate for genuine Basic->Premium->IDX upgrade-ladder features (Open House
    Manager, Blogs, Design Studio, Showcases need PREMIUM+; the IDX site needs IDX). NO_MLS
    always fails this - it has no rank - which is correct, since none of these ladder features
    are part of the No-MLS bundle."""

    async def _check(current_user=Depends(get_current_active_user)):
        _assert_active_subscription(current_user)
        user_rank = PLAN_TIER_RANK.get(current_user.plan_tier, 0)
        if user_rank < PLAN_TIER_RANK[minimum.value]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires the {minimum.value.title()} plan or higher. Please upgrade to access it.",
            )
        return current_user

    return _check


# Back-compat alias for the 3 existing call sites (collections_routes.py,
# collection_preferences_routes.py) that gate Showcases at Premium - IDX also passes since it
# outranks Premium. site_routes.py (the actual IDX website) has been moved to
# require_plan_tier(PlanTier.IDX) directly, since IDX-only content wrongly used to check
# "Premium" identically to Showcases.
require_premium_plan = require_plan_tier(PlanTier.PREMIUM)


async def get_current_user_optional(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """Get the current user if authenticated, otherwise return None"""
    try:
        # Check if Authorization header exists
        auth_header = request.headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None

        token = auth_header.replace("Bearer ", "")
        user_id = verify_token(token)
        if user_id is None:
            return None

        # Import here to avoid circular imports
        from app.services.user_service import UserService

        user = await UserService.get_user_by_id(db, user_id=user_id)
        # if user is None or not user.is_active:
        #     return None

        return user

    except Exception:
        # If anything fails, just return None (anonymous user)
        return None
