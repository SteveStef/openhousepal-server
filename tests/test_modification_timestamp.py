import asyncio
import os
import sys
import json
from pathlib import Path

# Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.services.bright_mls_service import bright_mls_service
from dotenv import load_dotenv

# Load environment variables from server/.env
load_dotenv(root_dir / ".env")

async def test_modification_count(timestamp: str):
    print(f"🚀 Counting properties modified since: {timestamp}...\n")
    
    if not os.getenv("BRIGHT_MLS_CLIENT"):
        print("❌ ERROR: BRIGHT_MLS_CLIENT not found in .env file.")
        return

    try:
        # Use $top=0 and $count=true to get ONLY the count
        params = {
            "$filter": f"ModificationTimestamp gt {timestamp}",
            "$top": 0,
            "$count": "true"
        }

        data = await bright_mls_service._make_request("BrightProperties", params=params)

        count = data.get("@odata.count", 0)
        print(f"📊 Total properties modified: {count}")

    except Exception as e:
        print(f"\n❌ API Error: {e}")
    finally:
        print("\n🏁 Count complete.")

if __name__ == "__main__":
    target_timestamp = "2026-02-25T18:30:00Z"
    
    async def main():
        await test_modification_count(target_timestamp)
        await bright_mls_service.close()

    asyncio.run(main())

