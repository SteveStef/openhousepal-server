
import asyncio
from sqlalchemy import text, select, func, or_, and_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.dialects.postgresql import JSONB
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

async def test_jsonb_behavior():
    engine = create_async_engine(DATABASE_URL)
    
    async with engine.connect() as conn:
        print("Testing SQL NULL:")
        try:
            res = await conn.execute(text("SELECT * FROM jsonb_array_elements_text(NULL)"))
            print(f"  SQL NULL result: {res.all()}")
        except Exception as e:
            print(f"  SQL NULL error: {e}")

        print("\nTesting JSON null ('null'::jsonb):")
        try:
            res = await conn.execute(text("SELECT * FROM jsonb_array_elements_text('null'::jsonb)"))
            print(f"  JSON null result: {res.all()}")
        except Exception as e:
            print(f"  JSON null error: {e}")

        print("\nTesting JSON object ('{}'::jsonb):")
        try:
            res = await conn.execute(text("SELECT * FROM jsonb_array_elements_text('{}'::jsonb)"))
            print(f"  JSON object result: {res.all()}")
        except Exception as e:
            print(f"  JSON object error: {e}")

        print("\nTesting JSON string ('\"Morgantown, PA\"'::jsonb):")
        try:
            res = await conn.execute(text("SELECT * FROM jsonb_array_elements_text('\"Morgantown, PA\"'::jsonb)"))
            print(f"  JSON string result: {res.all()}")
        except Exception as e:
            print(f"  JSON string error: {e}")

        print("\nTesting jsonb_array_length with JSON null:")
        try:
            res = await conn.execute(text("SELECT jsonb_array_length('null'::jsonb)"))
            print(f"  jsonb_array_length result: {res.all()}")
        except Exception as e:
            print(f"  jsonb_array_length error: {e}")

        print("\nTesting Proposed Fix (CASE WHEN):")
        fix_sql = """
            SELECT * FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(:val) = 'array' THEN :val ELSE '[]'::jsonb END
            )
        """
        try:
            res = await conn.execute(text(fix_sql), {"val": 'null'})
            print(f"  Fix with JSON null result: {res.all()}")
            res = await conn.execute(text(fix_sql), {"val": '{}'})
            print(f"  Fix with JSON object result: {res.all()}")
            res = await conn.execute(text(fix_sql), {"val": '["A", "B"]'})
            print(f"  Fix with JSON array result: {res.all()}")
        except Exception as e:
            print(f"  Fix error: {e}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(test_jsonb_behavior())
