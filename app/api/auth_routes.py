from fastapi import APIRouter, HTTPException, Depends, status, Response, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
import os

from app.database import get_db
from app.schemas.user import UserCreate, User, UserLogin, Token
from app.services.user_service import UserService
from app.utils.auth import create_access_token, get_current_active_user

router = APIRouter(prefix="/auth", tags=["authentication"])

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
                "last_name": new_user.last_name
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
        
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Inactive user account"
            )
        
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
                "last_name": user.last_name
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