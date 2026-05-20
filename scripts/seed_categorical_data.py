import asyncio
import os
import sys
import uuid
from sqlalchemy import text, select

# Add parent directory to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import engine, AsyncSessionLocal
from app.utils.normalization import normalize_brokerage
from app.models.database import Property, Brokerage

async def seed_categorical_data():
    print("🚀 Starting centralized categorical data seeding...")
    
    # 1. Cities and Townships (Remain as efficient SQL queries)
    city_query = """
        INSERT INTO cities (id, name, state, created_at)
        SELECT DISTINCT ON (UPPER(TRIM(city)), UPPER(TRIM(state))) 
            gen_random_uuid()::text, UPPER(TRIM(city)), UPPER(TRIM(state)), NOW()
        FROM properties
        WHERE city IS NOT NULL AND city != '' AND state IS NOT NULL
        ON CONFLICT (name, state) DO NOTHING;
    """
    
    township_query = """
        INSERT INTO townships (id, name, state, created_at)
        SELECT DISTINCT ON (clean_township, state)
            gen_random_uuid()::text, clean_township, state, NOW()
        FROM (
            SELECT 
                CASE 
                    WHEN UPPER(TRIM(township)) IN ('AA', 'AA COUNTY') THEN 'ANNE ARUNDEL COUNTY'
                    WHEN UPPER(TRIM(township)) IN ('BA', 'BA COUNTY') THEN 'BALTIMORE CITY'
                    WHEN UPPER(TRIM(township)) IN ('BC', 'BC COUNTY') THEN 'BALTIMORE COUNTY'
                    ELSE UPPER(TRIM(township))
                END as clean_township,
                UPPER(TRIM(state)) as state
            FROM properties
            WHERE township IS NOT NULL 
            AND state IS NOT NULL
            AND TRIM(township) !~ '^[0-9.-]+$'
            AND LENGTH(TRIM(township)) > 1
            AND UPPER(TRIM(township)) NOT IN ('NA', 'N/A', 'NO', 'NT')
        ) sub
        WHERE clean_township != ''
        ON CONFLICT (name, state) DO NOTHING;
    """

    async with engine.begin() as conn:
        print("🧹 Clearing existing data...")
        await conn.execute(text("TRUNCATE cities, townships, brokerages RESTART IDENTITY CASCADE;"))
        
        print("⌛ Seeding Cities...")
        await conn.execute(text(city_query))
        
        print("⌛ Seeding Townships...")
        await conn.execute(text(township_query))

    # 2. Brokerages (Refactored to use Python normalization utility)
    print("⌛ Processing Brokerages using Python normalization rules...")
    
    async with AsyncSessionLocal() as session:
        # Fetch all distinct office names and states from properties
        stmt = select(Property.list_office_name, Property.state).where(
            Property.list_office_name != None,
            Property.list_office_name != ""
        ).distinct()
        
        result = await session.execute(stmt)
        office_state_pairs = result.all()
        
        print(f"🔍 Found {len(office_state_pairs)} unique office/state combinations. Normalizing...")
        
        processed_brokerages = []
        seen_pairs = set() # To prevent duplicates after normalization
        
        for raw_name, state in office_state_pairs:
            if not raw_name or not state:
                continue
                
            # Use the shared utility function!
            name, parent_name = normalize_brokerage(raw_name)
            state = state.upper().strip()
            
            # Key for deduplication
            pair_key = (name, state)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            
            brokerage = Brokerage(
                id=str(uuid.uuid4()),
                name=name,
                parent_name=parent_name,
                state=state
            )
            processed_brokerages.append(brokerage)
            
        print(f"📦 Bulk inserting {len(processed_brokerages)} brokerages...")
        
        # Insert in chunks to be safe
        chunk_size = 1000
        for i in range(0, len(processed_brokerages), chunk_size):
            chunk = processed_brokerages[i : i + chunk_size]
            session.add_all(chunk)
            await session.flush()
            
        await session.commit()
        print(f"✅ Brokerages seeded. Total: {len(processed_brokerages)}")

    print("\n🎉 Seeding complete! All data is now perfectly synchronized with normalization utilities.")

if __name__ == "__main__":
    asyncio.run(seed_categorical_data())
