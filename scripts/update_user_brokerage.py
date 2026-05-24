import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import update
from app.database import AsyncSessionLocal
from app.models.database import User, OpenHouseVisitor

async def update_user_brokerage(user_id, new_brokerage):
    #user_id = "499f7670-d9d8-4570-821b-3762b53599b2"
    #new_brokerage = "KELLER WILLIAMS"
    
    print(f"Connecting to database to update user {user_id}...")
    
    async with AsyncSessionLocal() as session:
        # Construct the update statement
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(brokerage=new_brokerage)
        )
        
        # Execute the update
        result = await session.execute(stmt)
        await session.commit()
        
        if result.rowcount > 0:
            print(f"✅ Successfully updated user {user_id} brokerage to '{new_brokerage}'")
        else:
            print(f"❌ Error: User with ID {user_id} was not found in the database.")

async def update_visitor_email():
    visitor_id = "8dd218eb-a853-4638-a1ee-88f89b175a86"
    new_email = "anthonyleefernandez1995@outlook.com"
    
    print(f"Connecting to database to update visitor {visitor_id}...")
    
    async with AsyncSessionLocal() as session:
        stmt = (
            update(OpenHouseVisitor)
            .where(OpenHouseVisitor.id == visitor_id)
            .values(email=new_email)
        )
        
        result = await session.execute(stmt)
        await session.commit()
        
        if result.rowcount > 0:
            print(f"✅ Successfully updated visitor {visitor_id} email to '{new_email}'")
        else:
            print(f"❌ Error: Visitor with ID {visitor_id} was not found.")

async def run_updates():
    await update_user_brokerage("499f7670-d9d8-4570-821b-3762b53599b2", "COMPASS")
    await update_user_brokerage("9210dcf2-9538-44f7-90fd-78199b49b978", "KELLER WILLIAMS")
    await update_user_brokerage("18fb1af4-e128-4433-bb07-aea2db974bd4", "COMPASS")
    await update_visitor_email()

if __name__ == "__main__":
    try:
        asyncio.run(run_updates())
    except Exception as e:
        print(f"❌ An error occurred: {e}")
