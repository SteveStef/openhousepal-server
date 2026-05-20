import asyncio
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import select, func
from app.database import AsyncSessionLocal
from app.models.database import Property, Brokerage

async def check_naming():
    async with AsyncSessionLocal() as session:
        print("--- BROKERAGE TABLE NAMES (containing RE) ---")
        stmt = select(Brokerage.name).where(Brokerage.name.ilike('%RE%MAX%')).limit(10)
        result = await session.execute(stmt)
        for name in result.scalars().all():
            print(f"Brokerage Table: '{name}'")

        print("\n--- PROPERTY TABLE OFFICE NAMES (containing RE) ---")
        stmt = select(Property.list_office_name).where(Property.list_office_name.ilike('%RE%MAX%')).distinct().limit(10)
        result = await session.execute(stmt)
        for name in result.scalars().all():
            print(f"Property Office: '{name}'")

if __name__ == "__main__":
    asyncio.run(check_naming())
