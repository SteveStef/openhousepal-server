from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typing import Optional

from app.models.database import User
from app.schemas.user import UserCreate
from app.utils.auth import hash_password, verify_password

class UserService:
    @staticmethod
    async def create_user(db: AsyncSession, user_data: UserCreate) -> User:
        """Create a new user"""
        # Hash the password
        hashed_password = hash_password(user_data.password)
        
        # Create user object
        db_user = User(
            email=user_data.email,
            hashed_password=hashed_password,
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            state=user_data.state,
            brokerage=user_data.brokerage
        )
        
        try:
            db.add(db_user)
            await db.commit()
            await db.refresh(db_user)
            return db_user
        except IntegrityError:
            await db.rollback()
            raise ValueError("Email already exists")
    
    @staticmethod
    async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[User]:
        """Authenticate a user with email and password"""
        user = await UserService.get_user_by_email(db, email)
        
        if not user:
            return None
            
        if not verify_password(password, user.hashed_password):
            return None
            
        return user
    
    @staticmethod
    async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
        """Get user by email"""
        result = await db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
        """Get user by ID"""
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()