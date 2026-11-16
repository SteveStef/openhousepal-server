from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import BlacklistedEmail
from app.config.logging import get_logger

logger = get_logger(__name__)

class BlacklistService:
    @staticmethod
    async def is_blacklisted(db: AsyncSession, email: str, owner_id: Optional[str] = None, manual: bool = False) -> bool:
        """
        Should this agent's email to this person be held back?
        - The platform-wide list (blacklisted_emails): unsubscribes from before 11/16, when one link
          stopped every agent. Still honoured for everything.
        - owner_id given: they unsubscribed from THIS agent (owner, 11/16 - every unsubscribe is per
          agent now: client_email_service.AGENT_UNSUB). That holds the agent's automatic emails;
          manual=True (the agent pressed a button to send this one) still goes.
        """
        if not email:
            return False
            
        try:
            email_lower = email.lower().strip()
            result = await db.execute(
                select(BlacklistedEmail).where(BlacklistedEmail.email == email_lower)
            )
            if result.scalars().first() is not None:
                return True
            if owner_id and not manual:
                from app.services.client_email_service import unsubscribed_from_agent
                return await unsubscribed_from_agent(db, owner_id, email_lower)
            return False
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
