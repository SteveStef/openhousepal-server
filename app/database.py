import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import TypeDecorator, DateTime
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./collections.db")


class TZDateTime(TypeDecorator):
    """
    A DateTime type that ensures all datetimes are timezone-aware (UTC).
    Fixes SQLite's limitation of storing datetimes as naive strings.
    """
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """When saving to database - ensure UTC"""
        if value is not None:
            if value.tzinfo is None:
                # If somehow a naive datetime gets here, assume UTC
                value = value.replace(tzinfo=timezone.utc)
            else:
                # Convert to UTC before storing
                value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        """When reading from database - force UTC timezone"""
        if value is not None and value.tzinfo is None:
            # SQLite returns naive datetimes - force UTC
            value = value.replace(tzinfo=timezone.utc)
        return value


# Create async engine
engine = create_async_engine(
    DATABASE_URL,
    echo=True if os.getenv("DEBUG") == "true" else False,
    future=True
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

# Initialize database
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Close database connections
async def close_db():
    await engine.dispose()
