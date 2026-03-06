import os
import asyncio
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL")

# Create async engine optimized for PostgreSQL
engine = create_async_engine(
    DATABASE_URL,
    echo=True if os.getenv("DEBUG") == "true" else False,
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600
)

# Create sessionmaker
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

# Dependency to get DB session
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def wait_for_db(retries: int = 5, delay: int = 5):
    """
    Waits for the database to become available before proceeding.
    Useful during startup to avoid race conditions.
    """
    for i in range(retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                logger.info("Database connection established.")
                return True
        except Exception as e:
            if i < retries - 1:
                logger.warning(f"Database connection attempt {i+1} failed. Retrying in {delay}s... Error: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Could not connect to database after {retries} attempts.")
                raise e
    return False

# async def close_db():
#     await engine.dispose()
