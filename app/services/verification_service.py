import random
import string
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
from passlib.context import CryptContext
from app.config.logging import get_logger

# Get logger from centralized config
logger = get_logger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class VerificationService:
    def __init__(self):
        # In-memory cache: {email: {code, expires_at, verified, form_data, attempts, last_sent}}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.code_expiration_minutes = 15
        self.rate_limit_window_minutes = 15
        self.max_attempts_per_window = 3

    def generate_code(self) -> str:
        """Generate a random 6-digit verification code"""
        return ''.join(random.choices(string.digits, k=6))

    def store_code(self, email: str, code: str, form_data: Dict[str, Any]) -> None:
        """
        Store verification code with form data in cache
        Form data should include: first_name, last_name, state, brokerage, password
        """
        # Hash password before storing
        if 'password' in form_data:
            form_data['password'] = pwd_context.hash(form_data['password'])

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self.code_expiration_minutes)

        # Check if entry exists to preserve attempts counter
        existing = self._cache.get(email, {})
        attempts = existing.get('attempts', 0) + 1

        # Check if this is within the same rate limit window
        last_sent = existing.get('last_sent')
        if last_sent:
            time_since_last = (now - last_sent).total_seconds() / 60
            if time_since_last >= self.rate_limit_window_minutes:
                # Reset attempts if we're in a new window
                attempts = 1

        self._cache[email] = {
            'code': code,
            'expires_at': expires_at,
            'verified': False,
            'form_data': form_data,
            'attempts': attempts,
            'last_sent': now
        }

        logger.info(f"Stored verification code for {email} (attempt {attempts})")

    def can_send_code(self, email: str) -> tuple[bool, Optional[str]]:
        """
        Check if email can receive a new verification code
        Returns (can_send, error_message)
        """
        entry = self._cache.get(email)

        if not entry:
            return True, None

        now = datetime.now(timezone.utc)
        last_sent = entry.get('last_sent')
        attempts = entry.get('attempts', 0)

        # Check if we're in the same rate limit window
        if last_sent:
            time_since_last = (now - last_sent).total_seconds() / 60

            if time_since_last < self.rate_limit_window_minutes:
                # Still in the same window - check attempt count
                if attempts >= self.max_attempts_per_window:
                    return False, f"Too many verification emails sent. Please try again in {int(self.rate_limit_window_minutes - time_since_last)} minutes."

        return True, None

    def verify_code(self, email: str, code: str) -> tuple[bool, Optional[str]]:
        """
        Verify the code for an email
        Returns (is_valid, error_message)
        """
        entry = self._cache.get(email)

        if not entry:
            return False, "No verification code found for this email"

        # Check if already verified
        if entry.get('verified'):
            return False, "Email already verified"

        # Check if expired
        now = datetime.now(timezone.utc)
        if now > entry['expires_at']:
            return False, "Verification code has expired. Please request a new one."

        # Check if code matches
        if entry['code'] != code:
            return False, "Invalid verification code"

        # Mark as verified
        entry['verified'] = True
        logger.info(f"Email verified successfully: {email}")

        return True, None

    def is_verified(self, email: str) -> bool:
        """Check if email is verified"""
        entry = self._cache.get(email)
        if not entry:
            return False
        return entry.get('verified', False)

    def get_form_data(self, email: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored form data for verified email"""
        entry = self._cache.get(email)
        if not entry or not entry.get('verified'):
            return None
        return entry.get('form_data')

    def resend_code(self, email: str) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Generate and store a new code for existing verification entry
        Returns (success, new_code, error_message)
        """
        entry = self._cache.get(email)

        if not entry:
            return False, None, "No verification pending for this email"

        # Check rate limit
        can_send, error = self.can_send_code(email)
        if not can_send:
            return False, None, error

        # Generate new code
        new_code = self.generate_code()

        # Update entry with new code and expiration
        now = datetime.now(timezone.utc)
        entry['code'] = new_code
        entry['expires_at'] = now + timedelta(minutes=self.code_expiration_minutes)
        entry['verified'] = False
        entry['attempts'] = entry.get('attempts', 0) + 1
        entry['last_sent'] = now

        logger.info(f"Resent verification code for {email}")

        return True, new_code, None

    def clear_verification(self, email: str) -> None:
        """Clear verification data for an email (after successful signup)"""
        if email in self._cache:
            del self._cache[email]
            logger.info(f"Cleared verification data for {email}")

    def cleanup_expired(self) -> int:
        """
        Remove expired verification entries from cache
        Returns number of entries removed
        """
        now = datetime.now(timezone.utc)
        expired_emails = []

        for email, entry in self._cache.items():
            if now > entry['expires_at']:
                expired_emails.append(email)

        for email in expired_emails:
            del self._cache[email]

        if expired_emails:
            logger.info(f"Cleaned up {len(expired_emails)} expired verification entries")

        return len(expired_emails)


# Global instance
verification_service = VerificationService()
