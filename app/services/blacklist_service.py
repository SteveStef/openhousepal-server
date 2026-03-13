from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import BlacklistedEmail
from app.config.logging import get_logger

logger = get_logger(__name__)

class BlacklistService:
    @staticmethod
    async def is_blacklisted(db: AsyncSession, email: str) -> bool:
        """
        Check if an email is blacklisted.
        """
        if not email:
            return False
            
        try:
            email_lower = email.lower().strip()
            result = await db.execute(
                select(BlacklistedEmail).where(BlacklistedEmail.email == email_lower)
            )
            return result.scalars().first() is not None
        except Exception as e:
            logger.error(f"Error checking blacklist for {email}: {e}")
            return False

    @staticmethod
    async def blacklist_email(db: AsyncSession, email: str) -> bool:
        """
        Add an email to the blacklist.
        Returns True if newly added, False if already present.
        """
        if not email:
            return False
            
        try:
            email_lower = email.lower().strip()
            
            # Check if already blacklisted
            if await BlacklistService.is_blacklisted(db, email_lower):
                return False
                
            new_blacklisted = BlacklistedEmail(email=email_lower)
            db.add(new_blacklisted)
            await db.commit()
            return True
        except Exception as e:
            logger.error(f"Error adding {email} to blacklist: {e}")
            await db.rollback()
            return False
