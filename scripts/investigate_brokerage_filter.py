import os
import sys
import asyncio
from sqlalchemy import select, func, or_
from dotenv import load_dotenv

# Add the server directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, Brokerage, DiscoveryPreferences, User

load_dotenv()

async def investigate_compass_pa():
    print("🔍 Investigating Compass Properties in PA...")
    
    async with AsyncSessionLocal() as session:
        # 1. Check if there are ANY properties with 'Compass' in the list_office_name in PA
        print("\n--- 1. Compass Properties in Properties Table (PA) ---")
        stmt = (
            select(Property.list_office_name, func.count())
            .where(Property.state.ilike('PA'))
            .where(Property.list_office_name.ilike('%Compass%'))
            .group_by(Property.list_office_name)
        )
        result = await session.execute(stmt)
        props = result.all()
        
        if not props:
            print("No properties found in PA with 'Compass' in the office name.")
        else:
            for office, count in props:
                print(f"Office: '{office}' - {count} properties")

        # 2. Check the Brokerage reference table for 'Compass'
        print("\n--- 2. Compass entries in Brokerage Table ---")
        stmt_br = (
            select(Brokerage.name, Brokerage.parent_name)
            .where(or_(
                Brokerage.name.ilike('%Compass%'),
                Brokerage.parent_name.ilike('%Compass%')
            ))
        )
        result_br = await session.execute(stmt_br)
        brokerages = result_br.all()
        
        if not brokerages:
            print("No entries found in Brokerage table matching 'Compass'.")
        else:
            for name, parent in brokerages:
                print(f"Name: '{name}' | Parent: '{parent}'")

        # 3. Check the intersection (What the actual query does)
        # Assuming the user selected 'Compass'
        selected_brokerage = 'Compass'
        print(f"\n--- 3. Testing Discovery Logic for '{selected_brokerage}' in PA ---")
        
        # Step A: Find office names
        br_stmt = select(Brokerage.name).where(
            or_(
                Brokerage.parent_name == selected_brokerage,
                Brokerage.name == selected_brokerage
            )
        )
        br_res = await session.execute(br_stmt)
        office_names = br_res.scalars().all()
        print(f"Offices found for '{selected_brokerage}': {office_names}")
        
        if office_names:
            # Step B: Final query
            final_stmt = (
                select(func.count())
                .select_from(Property)
                .where(Property.state.ilike('PA'))
                .where(Property.list_office_name.in_(office_names))
            )
            final_res = await session.execute(final_stmt)
            print(f"Final Count: {final_res.scalar()} properties")

if __name__ == "__main__":
    asyncio.run(investigate_compass_pa())
