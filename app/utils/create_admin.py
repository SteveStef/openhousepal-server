"""
Script to create an admin user on server startup.
Checks if admin exists, creates if not.
"""
import os
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from passlib.context import CryptContext

from app.database import AsyncSessionLocal
from app.models.database import User

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Admin user configuration from environment variables
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_FIRST_NAME = os.getenv("ADMIN_FIRST_NAME", "Admin")
ADMIN_LAST_NAME = os.getenv("ADMIN_LAST_NAME", "User")
ADMIN_STATE = os.getenv("ADMIN_STATE", "CA")
ADMIN_BROKERAGE = os.getenv("ADMIN_BROKERAGE", "Open House Pal")


async def create_admin_user():
    """
    Create admin user if it doesn't exist.
    Admin user gets PREMIUM tier with ACTIVE status (no trial needed).
    """
    try:
        async with AsyncSessionLocal() as db:
            # Check if admin user already exists
            result = await db.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )
            existing_admin = result.scalar_one_or_none()

            if existing_admin:
                print(f"✓ Admin user already exists: {ADMIN_EMAIL}")
                return

            # Create admin user
            now = datetime.now(timezone.utc)
            admin_user = User(
                email=ADMIN_EMAIL,
                hashed_password=pwd_context.hash(ADMIN_PASSWORD),
                first_name=ADMIN_FIRST_NAME,
                last_name=ADMIN_LAST_NAME,
                state=ADMIN_STATE,
                brokerage=ADMIN_BROKERAGE,
                # Premium subscription with no expiration
                subscription_id=None,  # No PayPal subscription needed
                subscription_status="ACTIVE",  # Active, not trial
                plan_id=None,  # No PayPal plan
                plan_tier="PREMIUM",  # Premium tier
                trial_ends_at=None,  # No trial
                subscription_started_at=now,
                last_billing_date=now,
                next_billing_date=now + timedelta(days=365),  # 1 year from now
            )

            db.add(admin_user)
            await db.commit()
            await db.refresh(admin_user)

            print(f"\n{'='*60}")
            print(f"✓ Admin user created successfully!")
            print(f"{'='*60}")
            print(f"Email:     {ADMIN_EMAIL}")
            print(f"Password:  {ADMIN_PASSWORD}")
            print(f"Plan Tier: PREMIUM")
            print(f"Status:    ACTIVE")
            print(f"{'='*60}")
            print(f"⚠️  IMPORTANT: Change the admin password after first login!")
            print(f"{'='*60}\n")

    except Exception as e:
        print(f"✗ Error creating admin user: {e}")
        raise


def run_create_admin():
    """Synchronous wrapper to run the async function"""
    asyncio.run(create_admin_user())


if __name__ == "__main__":
    run_create_admin()
