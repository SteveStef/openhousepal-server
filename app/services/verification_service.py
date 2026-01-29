import random
import string
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, and_
from passlib.context import CryptContext
from app.config.logging import get_logger
from app.models.database import SignupVerification

# Get logger from centralized config
logger = get_logger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class VerificationService:
    def __init__(self):
        # Configuration
        self.code_expiration_minutes = 15
        self.rate_limit_window_minutes = 15
        self.max_attempts_per_window = 3

    def generate_code(self) -> str:
        """Generate a random 6-digit verification code"""
        return ''.join(random.choices(string.digits, k=6))

    async def store_code(self, email: str, code: str, form_data: Dict[str, Any], db: AsyncSession) -> None:
        """
        Store verification code with form data in database
        """
        # Hash password before storing
        if 'password' in form_data:
            form_data['password'] = pwd_context.hash(form_data['password'])

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self.code_expiration_minutes)

        # Check for existing entry
        stmt = select(SignupVerification).where(SignupVerification.email == email)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Check rate limit window
            time_since_last = (now - existing.last_sent_at).total_seconds() / 60
            if time_since_last >= self.rate_limit_window_minutes:
                # New window, reset attempts
                existing.attempts = 1
            else:
                existing.attempts += 1
            
            # Update existing
            existing.code = code
            existing.form_data = form_data
            existing.expires_at = expires_at
            existing.last_sent_at = now
            existing.verified = False  # Reset verified status on new code
        else:
            # Create new
            new_entry = SignupVerification(
                email=email,
                code=code,
                form_data=form_data,
                verified=False,
                attempts=1,
                last_sent_at=now,
                expires_at=expires_at
            )
            db.add(new_entry)

        await db.commit()
        logger.info(f"Stored verification code for {email}")

    async def can_send_code(self, email: str, db: AsyncSession) -> Tuple[bool, Optional[str]]:
        """
        Check if email can receive a new verification code
        """
        stmt = select(SignupVerification).where(SignupVerification.email == email)
        result = await db.execute(stmt)
        entry = result.scalar_one_or_none()

        if not entry:
            return True, None

        now = datetime.now(timezone.utc)
        time_since_last = (now - entry.last_sent_at).total_seconds() / 60

        # Check if we're in the same rate limit window
        if time_since_last < self.rate_limit_window_minutes:
            if entry.attempts >= self.max_attempts_per_window:
                wait_time = int(self.rate_limit_window_minutes - time_since_last)
                return False, f"Too many verification emails sent. Please try again in {wait_time} minutes."

        return True, None

    async def verify_code(self, email: str, code: str, db: AsyncSession) -> Tuple[bool, Optional[str]]:
        """
        Verify the code for an email
        """
        stmt = select(SignupVerification).where(SignupVerification.email == email)
        result = await db.execute(stmt)
        entry = result.scalar_one_or_none()

        if not entry:
            return False, "No verification code found for this email"

        # Check if already verified
        if entry.verified:
            return False, "Email already verified"

        # Check if expired
        now = datetime.now(timezone.utc)
        if now > entry.expires_at:
            return False, "Verification code has expired. Please request a new one."

        # Check if code matches
        if entry.code != code:
            return False, "Invalid verification code"

        # Mark as verified
        entry.verified = True
        await db.commit()
        
        logger.info(f"Email verified successfully: {email}")
        return True, None

    async def is_verified(self, email: str, db: AsyncSession) -> bool:
        """Check if email is verified"""
        stmt = select(SignupVerification).where(SignupVerification.email == email)
        result = await db.execute(stmt)
        entry = result.scalar_one_or_none()
        
        if not entry:
            return False
        return entry.verified

    async def get_form_data(self, email: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
        """Retrieve stored form data for verified email"""
        stmt = select(SignupVerification).where(SignupVerification.email == email)
        result = await db.execute(stmt)
        entry = result.scalar_one_or_none()
        
        if not entry or not entry.verified:
            return None
        return entry.form_data

    async def resend_code(self, email: str, db: AsyncSession) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Generate and store a new code for existing verification entry
        Returns (success, new_code, error_message)
        """
        stmt = select(SignupVerification).where(SignupVerification.email == email)
        result = await db.execute(stmt)
        entry = result.scalar_one_or_none()

        if not entry:
            return False, None, "No verification pending for this email"

        # Check rate limit
        can_send, error = await self.can_send_code(email, db)
        if not can_send:
            return False, None, error

        # Generate new code
        new_code = self.generate_code()
        now = datetime.now(timezone.utc)

        # Update entry
        entry.code = new_code
        entry.expires_at = now + timedelta(minutes=self.code_expiration_minutes)
        entry.verified = False
        
        # Increment attempts (logic duplicated slightly from store_code but needed here)
        time_since_last = (now - entry.last_sent_at).total_seconds() / 60
        if time_since_last >= self.rate_limit_window_minutes:
            entry.attempts = 1
        else:
            entry.attempts += 1
            
        entry.last_sent_at = now

        await db.commit()
        logger.info(f"Resent verification code for {email}")

        return True, new_code, None

    async def clear_verification(self, email: str, db: AsyncSession) -> None:
        """Clear verification data for an email (after successful signup)"""
        stmt = delete(SignupVerification).where(SignupVerification.email == email)
        await db.execute(stmt)
        # Caller handles commit to ensure atomicity with user creation
        logger.info(f"Cleared verification data for {email}")

    async def cleanup_expired(self, db: AsyncSession) -> int:
        """
        Remove expired verification entries
        """
        now = datetime.now(timezone.utc)
        stmt = delete(SignupVerification).where(SignupVerification.expires_at < now)
        result = await db.execute(stmt)
        await db.commit()
        
        count = result.rowcount
        if count > 0:
            logger.info(f"Cleaned up {count} expired verification entries")
        
        return count


# Global instance
verification_service = VerificationService()
