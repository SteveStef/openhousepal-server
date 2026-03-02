import asyncio
import os
import sys
from sqlalchemy import select, func
from dotenv import load_dotenv

# Add the current directory to sys.path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

load_dotenv()

from app.database import AsyncSessionLocal
from app.models.database import SchoolDistrict

async def check():
    async with AsyncSessionLocal() as db:
        count = await db.execute(select(func.count(SchoolDistrict.id)))
        print(f'COUNT: {count.scalar()}')

if __name__ == "__main__":
    asyncio.run(check())
