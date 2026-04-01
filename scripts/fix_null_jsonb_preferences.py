
import asyncio
import os
import sys
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import DATABASE_URL

async def fix_jsonb_nulls():
    """
    Finds any rows in collection_preferences where cities, townships, or school_districts
    contain a JSON literal 'null' and converts them to an empty array [].
    """
    engine = create_async_engine(DATABASE_URL)
    
    async with engine.begin() as conn:
        print("🔍 Scanning collection_preferences for JSON null values...")
        
        # 1. Check for 'null'::jsonb and convert to []::jsonb
        fix_query = """
            UPDATE collection_preferences 
            SET 
                cities = CASE 
                    WHEN cities = 'null'::jsonb OR cities IS NULL THEN '[]'::jsonb 
                    ELSE cities 
                END,
                townships = CASE 
                    WHEN townships = 'null'::jsonb OR townships IS NULL THEN '[]'::jsonb 
                    ELSE townships 
                END,
                school_districts = CASE 
                    WHEN school_districts = 'null'::jsonb OR school_districts IS NULL THEN '[]'::jsonb 
                    ELSE school_districts 
                END
            WHERE 
                cities = 'null'::jsonb OR cities IS NULL OR
                townships = 'null'::jsonb OR townships IS NULL OR
                school_districts = 'null'::jsonb OR school_districts IS NULL;
        """
        
        result = await conn.execute(text(fix_query))
        print(f"✅ Successfully updated {result.rowcount} rows.")
        print("   All NULL and JSON null fields have been converted to [].")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(fix_jsonb_nulls())
