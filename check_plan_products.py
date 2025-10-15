#!/usr/bin/env python3
"""Check if PayPal plans share the same product"""

import asyncio
import sys
from dotenv import load_dotenv

# Load environment variables first
load_dotenv()

from app.services.paypal_service import paypal_service

async def check_plans():
    basic_plan_id = "P-00K26568FD6086816NDT7SXA"
    premium_plan_id = "P-50796747YK1489053NDT7TXQ"

    print("Fetching Basic Plan details...")
    try:
        # Get token first
        token = await paypal_service.get_token()

        # Fetch both plans using httpx directly
        import httpx
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            # Get Basic plan
            basic_response = await client.get(
                f"{paypal_service._base_url}/v1/billing/plans/{basic_plan_id}",
                headers=headers
            )
            basic_data = basic_response.json()

            # Get Premium plan
            premium_response = await client.get(
                f"{paypal_service._base_url}/v1/billing/plans/{premium_plan_id}",
                headers=headers
            )
            premium_data = premium_response.json()

        # Extract product IDs
        basic_product = basic_data.get("product_id")
        premium_product = premium_data.get("product_id")

        print(f"\n{'='*60}")
        print(f"BASIC Plan ID:    {basic_plan_id}")
        print(f"BASIC Product ID: {basic_product}")
        print(f"\n{'='*60}")
        print(f"PREMIUM Plan ID:    {premium_plan_id}")
        print(f"PREMIUM Product ID: {premium_product}")
        print(f"{'='*60}\n")

        if basic_product == premium_product:
            print("✅ SUCCESS: Both plans use the SAME product!")
            print(f"   Product ID: {basic_product}")
            print("   You can upgrade/downgrade between these plans.")
        else:
            print("❌ PROBLEM: Plans use DIFFERENT products!")
            print(f"   Basic uses:   {basic_product}")
            print(f"   Premium uses: {premium_product}")
            print("\n   PayPal requires both plans to be under the same product")
            print("   to allow upgrades/downgrades.")
            print("\n   Solution: Create new plans under the same product in PayPal Dashboard")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(check_plans())
