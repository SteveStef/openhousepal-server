import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import update
from app.database import AsyncSessionLocal
from app.models.database import User

async def update_user_brokerage():
    user_id = "499f7670-d9d8-4570-821b-3762b53599b2"
    new_brokerage = "KELLER WILLIAMS"
    
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

if __name__ == "__main__":
    try:
        asyncio.run(update_user_brokerage())
    except Exception as e:
        print(f"❌ An error occurred: {e}")
