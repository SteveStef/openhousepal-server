import os
import httpx
import base64
from typing import Optional
from datetime import datetime, timedelta, timezone
from app.config.logging import get_logger

logger = get_logger(__name__)

class PayPalService:
    def __init__(self):
        self._client_id = os.getenv("PAYPAL_CLIENT_ID")
        self._secret = os.getenv("PAYPAL_SECRET_ID")
        self._mode = os.getenv("PAYPAL_MODE", "sandbox")

        if self._mode == "live":
            self._base_url = "https://api-m.paypal.com"
        else:
            self._base_url = "https://api-m.sandbox.paypal.com"

    # This needs to be in try catch
    async def get_token(self) -> str:
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
            access_token = token_data["access_token"]

            return access_token

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

    async def get_plan(self, plan_id: str) -> dict:
        """Raw PayPal billing plan (billing cycles, setup fee) - what PayPal will really charge."""
        token = await self.get_token()
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._base_url}/v1/billing/plans/{plan_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=30.0,
            )
            if response.status_code != 200:
                raise Exception(f"Failed to get plan: {response.status_code} - {response.text}")
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
                "return_url": f"{os.getenv('CLIENT_URL')}/open-houses",
                "cancel_url": f"{os.getenv('CLIENT_URL')}/register"
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

    async def create_subscription_with_urls(self, plan_id: str, return_url: str, cancel_url: str, start_time: str = None) -> dict:
        """
        Create a PayPal subscription with custom return URLs.

        This is used when users need to create a new subscription (e.g., after cancelling).

        Args:
            plan_id: The PayPal plan ID to subscribe to
            return_url: URL to redirect after user approves subscription
            cancel_url: URL to redirect if user cancels

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
                "return_url": return_url,
                "cancel_url": cancel_url
            }
        }
        if start_time:
            # First monthly charge on this date (a one-time setup fee is still charged right away).
            data["start_time"] = start_time

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

            # Extract approval URL from links
            approval_url = None
            for link in result.get("links", []):
                if link.get("rel") == "approve":
                    approval_url = link.get("href")
                    break

            # Check if plan change was applied immediately (no approval needed)
            if not approval_url:
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

    async def verify_webhook_signature(
        self,
        transmission_id: str,
        transmission_time: str,
        cert_url: str,
        auth_algo: str,
        transmission_sig: str,
        webhook_id: str,
        webhook_event: dict
    ) -> bool:
        """
        Verify PayPal webhook signature to ensure the webhook came from PayPal.

        This prevents attackers from sending fake webhook events to manipulate subscriptions.

        Args:
            transmission_id: PAYPAL-TRANSMISSION-ID header
            transmission_time: PAYPAL-TRANSMISSION-TIME header
            cert_url: PAYPAL-CERT-URL header
            auth_algo: PAYPAL-AUTH-ALGO header
            transmission_sig: PAYPAL-TRANSMISSION-SIG header
            webhook_id: Your webhook ID from PayPal dashboard
            webhook_event: The full webhook event body (as dict)

        Returns:
            bool: True if signature is valid, False otherwise

        Raises:
            Exception: If the verification request fails
        """
        token = await self.get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        data = {
            "transmission_id": transmission_id,
            "transmission_time": transmission_time,
            "cert_url": cert_url,
            "auth_algo": auth_algo,
            "transmission_sig": transmission_sig,
            "webhook_id": webhook_id,
            "webhook_event": webhook_event
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/v1/notifications/verify-webhook-signature",
                headers=headers,
                json=data,
                timeout=30.0
            )

            if response.status_code != 200:
                raise Exception(f"Failed to verify webhook signature: {response.status_code} - {response.text}")

            result = response.json()
            verification_status = result.get("verification_status")

            return verification_status == "SUCCESS"

    # ---- Domains bought in OpenHousePal (domain_purchase_service) ----

    async def _post(self, path: str, data: dict) -> httpx.Response:
        token = await self.get_token()
        async with httpx.AsyncClient() as client:
            return await client.post(f"{self._base_url}{path}", json=data, timeout=30.0,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    async def create_domain_plan(self) -> str:
        """The one shared yearly "Domain name" plan (product + plan) - its $20 prices are
        placeholders; every domain's subscription overrides them. Returns the plan id."""
        prod = await self._post("/v1/catalogs/products", {
            "name": "OpenHousePal domain name", "type": "SERVICE", "category": "SOFTWARE",
            "description": "A website domain registered for an agent's OpenHousePal IDX website, renewed yearly.",
        })
        if prod.status_code not in (200, 201):
            raise Exception(f"Failed to create domain product: {prod.status_code} - {prod.text}")
        plan = await self._post("/v1/billing/plans", {
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
        if plan.status_code not in (200, 201):
            raise Exception(f"Failed to create domain plan: {plan.status_code} - {plan.text}")
        return plan.json()["id"]

    async def create_domain_subscription(self, plan_id: str, first_year: float, renewal: float, start_time: str,
                                         custom_id: str, description: str, return_url: str, cancel_url: str) -> dict:
        """One yearly subscription per domain on the shared "Domain name" plan, priced for this
        domain: year one is the setup fee (charged at approval - CANCEL if it fails, so ACTIVE means
        paid), the yearly cycle starts at `start_time` (just before it would expire)."""
        resp = await self._post("/v1/billing/subscriptions", {
            "plan_id": plan_id,
            "start_time": start_time,
            "custom_id": custom_id,
            "plan": {
                "billing_cycles": [{"sequence": 1, "total_cycles": 0,
                                    "pricing_scheme": {"fixed_price": {"value": f"{renewal:.2f}", "currency_code": "USD"}}}],
                "payment_preferences": {"auto_bill_outstanding": True, "setup_fee_failure_action": "CANCEL",
                                        "payment_failure_threshold": 2,
                                        "setup_fee": {"value": f"{first_year:.2f}", "currency_code": "USD"}},
            },
            "application_context": {
                "brand_name": "OpenHousePal", "shipping_preference": "NO_SHIPPING", "user_action": "SUBSCRIBE_NOW",
                "return_url": return_url, "cancel_url": cancel_url,
            },
        })
        if resp.status_code not in (200, 201):
            raise Exception(f"Failed to create domain subscription: {resp.status_code} - {resp.text}")
        return resp.json()

    async def list_subscription_transactions(self, subscription_id: str, days: int = 400) -> list:
        token = await self.get_token()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/v1/billing/subscriptions/{subscription_id}/transactions",
                params={"start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
                headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
            )
        if resp.status_code != 200:
            raise Exception(f"Failed to list transactions: {resp.status_code} - {resp.text}")
        return resp.json().get("transactions") or []

    async def refund_sale(self, sale_id: str) -> bool:
        """Full refund of one subscription payment (a domain we couldn't register)."""
        resp = await self._post(f"/v1/payments/sale/{sale_id}/refund", {})
        return resp.status_code in (200, 201)


paypal_service = PayPalService()

