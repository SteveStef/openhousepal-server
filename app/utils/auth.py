from passlib.context import CryptContext
from typing import Optional
from datetime import datetime, timedelta
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
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-here-change-in-production")
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
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

def verify_token(token: str) -> Optional[str]:
    """Verify a JWT token and return the user_id"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        return user_id
    except JWTError:
        return None

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
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
    
    return user

async def get_current_active_user(current_user = Depends(get_current_user)):
    """Get the current authenticated and active user"""
    # if not current_user.is_active:
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="Inactive user"
    #     )
    return current_user

async def require_premium_plan(current_user = Depends(get_current_active_user)):
    """
    Require user to have an active Premium plan subscription.
    Handles trial expiration, grace periods for cancelled subscriptions, and plan tier validation.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    has_access = False

    # Case 1: ACTIVE subscription
    if current_user.subscription_status == "ACTIVE":
        has_access = True

    # Case 2: TRIAL - check if not expired
    elif current_user.subscription_status == "TRIAL":
        if current_user.trial_ends_at and current_user.trial_ends_at > now:
            has_access = True
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your free trial has expired. Please subscribe to continue using Premium features."
            )

    # Case 3: CANCELLED - check grace period (they paid through a certain date)
    elif current_user.subscription_status == "CANCELLED":
        if current_user.next_billing_date and current_user.next_billing_date > now:
            has_access = True  # Still in paid period
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your subscription has ended. Please resubscribe to continue using Premium features."
            )

    # Case 4: All other statuses (SUSPENDED, EXPIRED, etc.)
    else:
        status_text = current_user.subscription_status.lower() if current_user.subscription_status else "inactive"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your subscription is {status_text}. Please renew your subscription to access this feature."
        )

    # If we got here, check plan tier is PREMIUM
    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Premium subscription required to access this feature."
        )

    if current_user.plan_tier != "PREMIUM":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature requires a Premium plan. Please upgrade to access Showcases."
        )

    return current_user

async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional:
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
