import asyncio
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.services.paypal_service import paypal_service as paypal_serv


async def test_get_token():
    """Test getting PayPal OAuth token"""
    print("Testing PayPal get_token()...\n")

    try:
        print(f"Client ID: {paypal_serv._client_id[:20]}..." if paypal_serv._client_id else "None")
        print(f"Mode: {paypal_serv._mode}")
        print(f"Base URL: {paypal_serv._base_url}\n")

        # Get token
        print("Requesting access token...")
        token = await paypal_serv.get_token()

        print(f"\n✓ Successfully retrieved access token!")
        print(f"Token (first 50 chars): {token[:50]}...")
        print(f"Token length: {len(token)} characters")
        print(f"Token expires at: {paypal_serv._expired_date}")

        # Test token caching - second call should return cached token
        print("\n\nTesting token caching...")
        token2 = await paypal_serv.get_token()

        if token == token2:
            print("✓ Token caching works! Same token returned.")
        else:
            print("✗ Warning: Different token returned (unexpected)")

        return True

    except Exception as e:
        print(f"\n✗ Error: {str(e)}")
        return False


async def main():
    """Main test runner"""
    print("=" * 60)
    print("PayPal Service Test")
    print("=" * 60 + "\n")

    success = await test_get_token()

    print("\n" + "=" * 60)
    if success:
        print("✓ All tests passed!")
    else:
        print("✗ Tests failed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
