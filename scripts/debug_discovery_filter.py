import os
import sys
import asyncio
from sqlalchemy import select, func
from dotenv import load_dotenv

# Add the server directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, DiscoveryPreferences, User

load_dotenv()

async def debug_state_filter():
    print("🔍 Debugging Discovery State Filter...")
    
    async with AsyncSessionLocal() as session:
        # 1. Check a sample of properties to see how state is stored
        print("\n--- 1. Sample Property States ---")
        stmt = select(Property.state).limit(5)
        result = await session.execute(stmt)
        states = result.scalars().all()
        for s in states:
            print(f"State in DB: '{s}' (Length: {len(s) if s else 0})")

        # 2. Check current user's preference (Assuming you are testing with a specific user)
        # We'll just grab the first user with preferences for this debug script
        print("\n--- 2. User Preference Check ---")
        stmt = select(User.email, DiscoveryPreferences.state).join(DiscoveryPreferences).limit(1)
        result = await session.execute(stmt)
        user_pref = result.first()
        
        if user_pref:
            email, pref_state = user_pref
            print(f"User: {email}")
            print(f"Preference State: '{pref_state}' (Length: {len(pref_state) if pref_state else 0})")
            
            if pref_state:
                # 3. Test the query logic
                print("\n--- 3. Testing Query Logic ---")
                
                # Test literal match
                stmt_literal = select(func.count()).select_from(Property).where(Property.state == pref_state)
                res_literal = await session.execute(stmt_literal)
                count_literal = res_literal.scalar()
                print(f"Literal match (==): {count_literal} properties found")

                # Test ILIKE match
                stmt_ilike = select(func.count()).select_from(Property).where(Property.state.ilike(pref_state))
                res_ilike = await session.execute(stmt_ilike)
                count_ilike = res_ilike.scalar()
                print(f"Case-insensitive (ilike): {count_ilike} properties found")

                # Test TRIM + ILIKE match
                stmt_robust = select(func.count()).select_from(Property).where(func.trim(Property.state).ilike(pref_state.strip()))
                res_robust = await session.execute(stmt_robust)
                count_robust = res_robust.scalar()
                print(f"Robust match (trim + ilike): {count_robust} properties found")
        else:
            print("No users with DiscoveryPreferences found to test.")

if __name__ == "__main__":
    asyncio.run(debug_state_filter())
