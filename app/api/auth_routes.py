from fastapi import APIRouter, HTTPException, Depends, status, Response, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta, datetime, timezone
import os

from app.database import get_db
from app.schemas.user import UserCreate, User, UserLogin, Token
from app.services.user_service import UserService
from app.services.paypal_service import paypal_service
from app.utils.auth import create_access_token, get_current_active_user, hash_password
from app.models.database import User as UserModel

router = APIRouter(prefix="/auth", tags=["authentication"])

# PayPal Plan ID Constants (from environment variables)
BASIC_PLAN_ID = os.getenv("PAYPAL_BASIC_PLAN_ID")
PREMIUM_PLAN_ID = os.getenv("PAYPAL_PREMIUM_PLAN_ID")

@router.post("/validate-signup-form", status_code=status.HTTP_200_OK)
async def validate_signup(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Validate signup form data before proceeding to payment.
    Checks if email is already registered.
    """
    try:
        existing_user = await UserService.get_user_by_email(db, user_data.email)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        return {
            "valid": True,
            "message": "Email available"
        }
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        print(f"Validation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Validation failed"
        )

@router.post("/signup-with-subscription", response_model=Token, status_code=status.HTTP_201_CREATED)
async def signup_with_subscription(
    subscription_id: str,
    plan_id: str,
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Atomically create user account with PayPal subscription.
    All-or-nothing: account and subscription are linked together or neither is created.
    """
    try:
        # Step 1: Validate subscription with PayPal API
        try:
            subscription_details = await paypal_service.get_subscription(subscription_id)
        except Exception as e:
            print(f"PayPal API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid subscription ID or PayPal service unavailable"
            )

        # Step 2: Verify plan_id matches what PayPal says
        paypal_plan_id = subscription_details.get('plan_id')
        if paypal_plan_id != plan_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Plan mismatch: expected {plan_id}, got {paypal_plan_id}"
            )

        # Step 3: Check subscription status is valid
        subscription_status = subscription_details.get('status')
        if subscription_status not in ['ACTIVE', 'APPROVAL_PENDING', 'APPROVED']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid subscription status: {subscription_status}"
            )

        # Step 4: Determine plan tier from plan_id
        if plan_id == BASIC_PLAN_ID:
            plan_tier = "BASIC"
        elif plan_id == PREMIUM_PLAN_ID:
            plan_tier = "PREMIUM"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan ID"
            )

        # Step 5: Start atomic transaction
        async with db.begin():
            # Check if email already exists (database will lock this row)
            existing_user = await UserService.get_user_by_email(db, user_data.email)
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered"
                )

            # Check if subscription already linked to another user
            from sqlalchemy import select
            result = await db.execute(
                select(UserModel).where(UserModel.subscription_id == subscription_id)
            )
            existing_subscription = result.scalar_one_or_none()
            if existing_subscription:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Subscription already linked to another account"
                )

            # Create user with subscription data
            now = datetime.now(timezone.utc)
            trial_end = now + timedelta(days=14)

            new_user = UserModel(
                email=user_data.email,
                hashed_password=hash_password(user_data.password),
                first_name=user_data.first_name,
                last_name=user_data.last_name,
                state=user_data.state,
                brokerage=user_data.brokerage,
                # Subscription fields
                subscription_id=subscription_id,
                plan_id=plan_id,
                plan_tier=plan_tier,
                subscription_status="TRIAL",
                subscription_started_at=now,
                trial_ends_at=trial_end
            )

            db.add(new_user)
            await db.flush()  # Get the ID before commit
            await db.refresh(new_user)

        # Transaction committed successfully - account and subscription linked atomically!

        # Create access token
        access_token = create_access_token(data={"sub": new_user.id})

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": new_user.id,
                "email": new_user.email,
                "first_name": new_user.first_name,
                "last_name": new_user.last_name,
                "state": new_user.state,
                "brokerage": new_user.brokerage,
                "plan_tier": new_user.plan_tier,
                "subscription_status": new_user.subscription_status
            }
        }

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        print(f"Signup with subscription error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create account with subscription"
        )


@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
async def signup(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):

    """
    Create a new user account and return access token
    """
    try:
        # Check if user already exists
        existing_user = await UserService.get_user_by_email(db, user_data.email)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Create new user
        new_user = await UserService.create_user(db, user_data)
        
        # Create access token for the new user
        access_token = create_access_token(
            data={"sub": new_user.id}
        )
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": new_user.id,
                "email": new_user.email,
                "first_name": new_user.first_name,
                "last_name": new_user.last_name,
                "state": new_user.state,
                "brokerage": new_user.brokerage
            }
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions (like the 400 above)
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        # Log the actual error for debugging
        print(f"Signup error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user"
        )

@router.get("/users/{user_id}", response_model=User)
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get user by ID (for testing)
    """
    user = await UserService.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.post("/login", response_model=Token)
async def login(
    user_credentials: UserLogin,
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate user and return access token
    """
    try:
        # Authenticate user
        user = await UserService.authenticate_user(
            db, 
            user_credentials.email, 
            user_credentials.password
        )
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
            )
        
        # if not user.is_active:
        #     raise HTTPException(
        #         status_code=status.HTTP_400_BAD_REQUEST,
        #         detail="Inactive user account"
        #     )
        
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
                "brokerage": user.brokerage
            }
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        print(f"Login error: {e}")
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
    current_user: User = Depends(get_current_active_user)
):
    """
    Get current authenticated user's profile
    """
    return current_user

@router.get("/debug")
async def debug_auth(request: Request):
    """
    Debug endpoint to check authentication headers
    """
    headers = dict(request.headers)
    
    auth_header = headers.get('authorization', 'No Authorization header')
    
    return {
        "message": "Debug endpoint reached",
        "authorization_header": auth_header,
        "all_headers": headers,
        "jwt_secret_key_env": os.getenv("JWT_SECRET_KEY", "NOT_SET")[:10] + "..." if os.getenv("JWT_SECRET_KEY") else "NOT_SET"
    }
