import asyncio
import os
import sys
from sqlalchemy import text

# Add parent directory to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import engine
from app.utils.normalization import MAJOR_BRANDS, normalize_township

async def seed_categorical_data():
    print("🚀 Starting cleaned categorical data seeding...")
    
    # Strategy for duplicates: TRUNCATE and rebuild.
    truncate_query = "TRUNCATE cities, townships, brokerages RESTART IDENTITY CASCADE;"
    
    # Build the SQL VALUES string for the Master Brands array from the shared utility
    brands_values = ", ".join(["('" + b.replace("'", "''") + "')" for b in MAJOR_BRANDS])

    queries = [
        # Seed Cities
        {
            "name": "Cities",
            "query": """
                INSERT INTO cities (id, name, state, created_at)
                SELECT DISTINCT ON (UPPER(TRIM(city)), UPPER(TRIM(state))) 
                    gen_random_uuid()::text, UPPER(TRIM(city)), UPPER(TRIM(state)), NOW()
                FROM properties
                WHERE city IS NOT NULL AND city != '' AND state IS NOT NULL
                ON CONFLICT (name, state) DO NOTHING;
            """
        },
        
        # Seed Townships
        {
            "name": "Townships",
            "query": """
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
                    -- Filter out junk data like '.', '0', '1-1'
                    AND TRIM(township) !~ '^[0-9.-]+$'
                    AND LENGTH(TRIM(township)) > 1
                    AND UPPER(TRIM(township)) NOT IN ('NA', 'N/A', 'NO', 'NT')
                ) sub
                WHERE clean_township != ''
                ON CONFLICT (name, state) DO NOTHING;
            """
        },
        
        # Seed Brokerages (with Master Array + Smart Normalization)
        {
            "name": "Brokerages",
            "query": f"""
                WITH raw_brands (raw_brand_name) AS (
                    VALUES {brands_values}
                ),
                brands AS (
                    SELECT 
                        raw_brand_name,
                        UPPER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(raw_brand_name, '[.,/\\\\-]', ' ', 'g'), '\\\\s+', ' ', 'g'))) as match_brand_name
                    FROM raw_brands
                )
                INSERT INTO brokerages (id, name, parent_name, state, created_at)
                SELECT DISTINCT ON (clean_name, state)
                    gen_random_uuid()::text, clean_name, parent_name, UPPER(state), NOW()
                FROM (
                    SELECT 
                        clean_name,
                        COALESCE(
                            -- TIER 1: Special Abbreviation Mappings
                            CASE 
                                WHEN clean_name LIKE 'KW %' OR clean_name = 'KW' THEN 'KELLER WILLIAMS'
                                WHEN clean_name LIKE 'RE MAX %' OR clean_name LIKE 'REMAX %' THEN 'RE/MAX'
                                WHEN clean_name LIKE 'BHHS %' THEN 'BERKSHIRE HATHAWAY'
                                WHEN clean_name LIKE 'C 21 %' THEN 'CENTURY 21'
                                ELSE NULL
                            END,
                            
                            -- TIER 2: Master Array Prefix Match
                            (SELECT raw_brand_name FROM brands WHERE clean_name LIKE match_brand_name || '%' LIMIT 1),
                            
                            -- TIER 3: Smart Stemming (First 3 words then strip fluff)
                            TRIM(REGEXP_REPLACE(
                                REGEXP_REPLACE(
                                    -- Extract first 3 significant non-whitespace chunks
                                    TRIM(REGEXP_REPLACE(clean_name, '^(\\\\S+(?:\\\\s+\\\\S+){{0,2}}).*$', '\\\\1')),
                                    -- Strip industry fluff
                                    '\\\\b(REALTY|PROPERTIES|REAL ESTATE|GROUP|SERVICES|ASSOCIATES|ASSOC|RE|LLC|INC|LTD|COMPANY|CO|CORP|CORPORATION|REALTORS|REALTOR)\\\\b', '', 'gi'
                                ),
                                '[&\\\\s]+$', ''
                            ))
                        ) as parent_name,
                        state
                    FROM (
                        SELECT 
                            UPPER(TRIM(
                                REGEXP_REPLACE(
                                    REGEXP_REPLACE(list_office_name, '(,?\\\\s*(LLC|INC|LTD)\\\\.?)$', '', 'i'),
                                    '[.,/\\\\\\\\-]', ' ', 'g'
                                )
                            )) as clean_name,
                            state
                        FROM properties
                        WHERE list_office_name IS NOT NULL AND list_office_name != '' AND state IS NOT NULL
                    ) raw_sub
                ) sub
                WHERE clean_name != ''
                ON CONFLICT (name, state) DO NOTHING;
            """
        }
    ]

    async with engine.begin() as conn:
        print("🧹 Clearing existing duplicates...")
        await conn.execute(text(truncate_query))
        
        for q in queries:
            print(f"⌛ Seeding {q['name']}...")
            result = await conn.execute(text(q['query']))
            print(f"✅ {q['name']} seeded. Rows affected: {result.rowcount}")

    print("🎉 Seeding complete with shared normalization rules!")

if __name__ == "__main__":
    asyncio.run(seed_categorical_data())
