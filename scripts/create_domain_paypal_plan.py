"""Make the one PayPal plan every bought domain uses (domain_purchase_service) and print its id.
Optional - the app makes it by itself on the first purchase when PAYPAL_DOMAIN_PLAN_ID is unset.

    cd backend && .venv/Scripts/python.exe scripts/create_domain_paypal_plan.py

Uses PAYPAL_CLIENT_ID / PAYPAL_SECRET_ID / PAYPAL_MODE from .env - run it once in sandbox (dev)
and once with the live keys, then set PAYPAL_DOMAIN_PLAN_ID to the printed id in that environment.
The $20 prices are placeholders: every domain's subscription overrides them with its own price.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from app.services.paypal_service import paypal_service  # noqa: E402


async def main() -> None:
    pid = await paypal_service.create_domain_plan()
    print(f"PAYPAL_MODE={os.getenv('PAYPAL_MODE', 'sandbox')}")
    print(f"PAYPAL_DOMAIN_PLAN_ID={pid}")


if __name__ == "__main__":
    asyncio.run(main())
