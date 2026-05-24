import os
import sys
import asyncio
from sqlalchemy import text
from dotenv import load_dotenv

# Add the server directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import engine

load_dotenv()

async def clear_discovery_preferences():
    print("🧹 Clearing all discovery_preferences...")
    
    async with engine.begin() as conn:
        try:
            # Using TRUNCATE is faster and resets identity if any
            await conn.execute(text("TRUNCATE discovery_preferences RESTART IDENTITY CASCADE;"))
            print("✅ Successfully cleared discovery_preferences table.")
        except Exception as e:
            print(f"❌ Error clearing table: {e}")

if __name__ == "__main__":
    asyncio.run(clear_discovery_preferences())
