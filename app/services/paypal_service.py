import os
import httpx
import base64
from typing import Optional
from datetime import datetime, timedelta, timezone

class PayPalService:
    def __init__(self):
        self._client_id = os.getenv("PAYPAL_CLIENT_ID")
        self._secret = os.getenv("PAYPAL_SECRET_ID")
        self._mode = os.getenv("PAYPAL_MODE", "sandbox")

        if self._mode == "live":
            self._base_url = "https://api-m.paypal.com"
        else:
            self._base_url = "https://api-m.sandbox.paypal.com"

        self._access_token = None
        self._expired_date = None

    async def get_token(self) -> str:
        if self._access_token and self._expired_date and datetime.now(timezone.utc) < self._expired_date:
            return self._access_token
        credentials = f"{self._client_id}:{self._secret}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()
        headers = {
            "Authorization": f"Basic {encoded_creds}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = { "grant_type": "client_credentials" }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/oauth2/token",
                headers=headers,
                data=data,
                timeout=30.0
            )
            if response.status_code != 200:
                raise Exception(f"PayPal OAuth failed: {response.status_code} - {response.text}")
            token_data = response.json()
            self._access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 32400)
            self._expired_date = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

            return self._access_token

    async def get_subscription(self, subscription_id: str) -> dict:
        """
        Get details of a PayPal subscription.

        Args:
            subscription_id: The PayPal subscription ID

        Returns:
            dict: Subscription details including status, plan_id, subscriber info, etc.

        Raises:
            Exception: If the request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._base_url}/v1/billing/subscriptions/{subscription_id}",
                headers=headers,
                timeout=30.0
            )

            if response.status_code != 200:
                raise Exception(f"Failed to get subscription: {response.status_code} - {response.text}")

            return response.json()

    async def create_subscription(self, plan_id: str) -> dict:
        """
        Create a PayPal subscription.

        Note: In this implementation, subscriptions are created on the frontend
        using PayPal JavaScript SDK. This method is here for backend-initiated
        subscriptions if needed in the future.

        Args:
            plan_id: The PayPal plan ID to subscribe to

        Returns:
            dict: Subscription creation response with subscription ID and approval links

        Raises:
            Exception: If the request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        data = {
            "plan_id": plan_id,
            "application_context": {
                "brand_name": "Open House Pal",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": f"{os.getenv(CLIENT_URL)}/open-houses",
                "cancel_url": f"{os.getenv(CLIENT_URL)}/register"
            }
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/billing/subscriptions",
                headers=headers,
                json=data,
                timeout=30.0
            )

            if response.status_code not in [200, 201]:
                raise Exception(f"Failed to create subscription: {response.status_code} - {response.text}")

            return response.json()

    async def revise_subscription(self, subscription_id: str, new_plan_id: str, return_url: str, cancel_url: str) -> dict:
        """
        Revise a PayPal subscription to change the plan.
        This requires user approval via PayPal checkout.

        Args:
            subscription_id: The PayPal subscription ID to revise
            new_plan_id: The new plan ID to switch to
            return_url: URL to redirect after user approves
            cancel_url: URL to redirect if user cancels

        Returns:
            dict: Contains approval URL that user must visit to approve the change
                  Format: { "approval_url": "https://www.paypal.com/..." }

        Raises:
            Exception: If the request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        data = {
            "plan_id": new_plan_id,
            "application_context": {
                "brand_name": "Open House Pal",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "CONTINUE",
                "return_url": return_url,
                "cancel_url": cancel_url
            }
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/billing/subscriptions/{subscription_id}/revise",
                headers=headers,
                json=data,
                timeout=30.0
            )

            if response.status_code not in [200, 201]:
                raise Exception(f"Failed to revise subscription: {response.status_code} - {response.text}")

            result = response.json()

            # Debug: Print full response
            print(f"[DEBUG] PayPal revise response: {result}")

            # Extract approval URL from links
            approval_url = None
            for link in result.get("links", []):
                print(f"[DEBUG] Link: rel={link.get('rel')}, href={link.get('href')}")
                if link.get("rel") == "approve":
                    approval_url = link.get("href")
                    break

            # Check if plan change was applied immediately (no approval needed)
            if not approval_url:
                print(f"[INFO] No approval URL - plan change applied immediately")
                # Return None for approval_url to indicate immediate change
                return {
                    "approval_url": None,
                    "immediate": True,
                    "new_plan_id": result.get("plan_id"),
                    "links": result.get("links", [])
                }

            # Approval required
            return {
                "approval_url": approval_url,
                "immediate": False,
                "links": result.get("links", [])
            }

    async def suspend_subscription(self, subscription_id: str, reason: str = "Item not as described or did not match listing") -> bool:
        """
        Suspend a PayPal subscription (can be reactivated later).

        Args:
            subscription_id: The PayPal subscription ID
            reason: Reason for suspension

        Returns:
            bool: True if suspension was successful

        Raises:
            Exception: If the request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        data = {
            "reason": reason
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/billing/subscriptions/{subscription_id}/suspend",
                headers=headers,
                json=data,
                timeout=30.0
            )

            # 204 No Content means success
            if response.status_code == 204:
                return True
            else:
                raise Exception(f"Failed to suspend subscription: {response.status_code} - {response.text}")

    async def activate_subscription(self, subscription_id: str, reason: str = "Reactivating subscription") -> bool:
        """
        Activate/reactivate a suspended or cancelled subscription.

        Args:
            subscription_id: The PayPal subscription ID
            reason: Reason for activation

        Returns:
            bool: True if activation was successful

        Raises:
            Exception: If the request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        data = {
            "reason": reason
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/billing/subscriptions/{subscription_id}/activate",
                headers=headers,
                json=data,
                timeout=30.0
            )

            # 204 No Content means success
            if response.status_code == 204:
                return True
            else:
                raise Exception(f"Failed to activate subscription: {response.status_code} - {response.text}")

    async def cancel_subscription(self, subscription_id: str, reason: str = "Customer requested cancellation") -> bool:
        """
        Cancel a PayPal subscription.

        Args:
            subscription_id: The PayPal subscription ID
            reason: Reason for cancellation (optional)

        Returns:
            bool: True if cancellation was successful

        Raises:
            Exception: If the request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        data = {
            "reason": reason
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/billing/subscriptions/{subscription_id}/cancel",
                headers=headers,
                json=data,
                timeout=30.0
            )

            # 204 No Content means success
            if response.status_code == 204:
                return True
            else:
                raise Exception(f"Failed to cancel subscription: {response.status_code} - {response.text}")


paypal_service = PayPalService()

