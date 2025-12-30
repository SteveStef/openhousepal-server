from app.database import AsyncSessionLocal
from sqlalchemy import select
from app.models.database import BundleCode
from datetime import datetime, timezone
import asyncio
import sys

async def list_codes():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BundleCode))
        codes = result.scalars().all()
        
        if not codes:
            print("\nNo bundle codes found in database.")
            return

        print(f"\nFound {len(codes)} bundle codes:")
        print("-" * 40)
        for c in codes:
            status = f"USED at {c.used_at}" if c.is_used else "AVAILABLE"
            print(f"{c.code:<15} | {status}")
        print("-" * 40)

async def add_code(code: str):
    async with AsyncSessionLocal() as session:
        # Check if exists
        existing = await session.get(BundleCode, code)
        if existing:
            print(f"Error: Code '{code}' already exists.")
            return

        new_code = BundleCode(code=code)
        session.add(new_code)
        await session.commit()
        print(f"Successfully added bundle code: {code}")

async def delete_code(code: str):
    async with AsyncSessionLocal() as session:
        existing = await session.get(BundleCode, code)
        if not existing:
            print(f"Error: Code '{code}' not found.")
            return
        
        await session.delete(existing)
        await session.commit()
        print(f"Successfully deleted code: {code}")

def print_usage():
    print("\nUsage:")
    print("  python manage_bundle_codes.py list")
    print("  python manage_bundle_codes.py add <CODE>")
    print("  python manage_bundle_codes.py delete <CODE>")

async def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    cmd = sys.argv[1].lower()

    if cmd == "list":
        await list_codes()
    elif cmd == "add" and len(sys.argv) == 3:
        await add_code(sys.argv[2])
    elif cmd == "delete" and len(sys.argv) == 3:
        await delete_code(sys.argv[2])
    else:
        print_usage()

if __name__ == "__main__":
    asyncio.run(main())
