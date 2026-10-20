"""Make the one PayPal plan every bought domain uses (domain_purchase_service) and print its id.

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

import httpx  # noqa: E402

from app.services.paypal_service import paypal_service  # noqa: E402


async def main() -> None:
    token = await paypal_service.get_token()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = paypal_service._base_url
    async with httpx.AsyncClient(timeout=30) as c:
        prod = await c.post(f"{base}/v1/catalogs/products", headers=h, json={
            "name": "OpenHousePal domain name", "type": "SERVICE", "category": "SOFTWARE",
            "description": "A website domain registered for an agent's OpenHousePal IDX website, renewed yearly.",
        })
        prod.raise_for_status()
        plan = await c.post(f"{base}/v1/billing/plans", headers=h, json={
            "product_id": prod.json()["id"], "name": "Domain name (yearly)", "status": "ACTIVE",
            "description": "Yearly domain renewal - first year charged at sign-up",
            "billing_cycles": [{
                "frequency": {"interval_unit": "YEAR", "interval_count": 1}, "tenure_type": "REGULAR",
                "sequence": 1, "total_cycles": 0,
                "pricing_scheme": {"fixed_price": {"value": "20.00", "currency_code": "USD"}},
            }],
            "payment_preferences": {
                "auto_bill_outstanding": True, "setup_fee_failure_action": "CANCEL", "payment_failure_threshold": 2,
                "setup_fee": {"value": "20.00", "currency_code": "USD"},
            },
        })
        plan.raise_for_status()
    print(f"PAYPAL_MODE={os.getenv('PAYPAL_MODE', 'sandbox')}")
    print(f"PAYPAL_DOMAIN_PLAN_ID={plan.json()['id']}")


if __name__ == "__main__":
    asyncio.run(main())
