from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from typing import Optional

from app.models.database import User, DiscoveryPreferences
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
            brokerage=user_data.brokerage,
            mls_id=user_data.mls_id
        )
        
        try:
            db.add(db_user)
            await db.flush()
            
            # Create discovery preferences
            discovery_prefs = DiscoveryPreferences(
                user_id=db_user.id,
                brokerages=[db_user.brokerage] if db_user.brokerage else [],
                state=db_user.state
            )
            db.add(discovery_prefs)
            
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
        # Case-insensitive: older rows may hold mixed case, new ones are stored lowercase.
        result = await db.execute(select(User).where(func.lower(User.email) == (email or "").strip().lower()))
        return result.scalars().first()
    
    @staticmethod
    async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
        """Get user by ID"""
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_user_by_mls_id(db: AsyncSession, mls_id: str) -> Optional[User]:
        # The real agent with this MLS ID, never an admin-made test copy that shares it.
        result = await db.execute(select(User).where(User.mls_id == mls_id).order_by(User.test_copy_of_user_id.isnot(None), User.created_at))
        return result.scalars().first()

