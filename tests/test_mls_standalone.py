import asyncio
import os
import sys
from pathlib import Path

# Fix ModuleNotFoundError: Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.services.bright_mls_service import bright_mls_service
from dotenv import load_dotenv

# Load environment variables from server/.env
load_dotenv(root_dir / ".env")

async def test_member_id(mls_id: str = "3235372"):
    print(f"🚀 Testing Bright MLS Member ID: {mls_id}...\n")
    if not os.getenv("BRIGHT_MLS_CLIENT"):
        print("❌ ERROR: BRIGHT_MLS_CLIENT not found in .env file.")
        return

    try:
        print("--- Calling BrightMembers API ---")
        exists = await bright_mls_service.bright_mls_id_exists(mls_id)
        print(exists)

    except Exception as e:
        print(f"\n❌ API Error: {e}")
    finally:
        print("\n🏁 Check complete.")

if __name__ == "__main__":
    asyncio.run(test_member_id("3372275"))
