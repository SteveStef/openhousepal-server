from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.database import Base
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    # Get database URL from config or environment
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        # Convert async URL to sync URL for migrations
        async_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./collections.db")
        if "sqlite+aiosqlite" in async_url:
            url = async_url.replace("sqlite+aiosqlite", "sqlite")
        else:
            url = "sqlite:///./collections.db"
    
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Get configuration and set database URL if not present
    configuration = config.get_section(config.config_ini_section, {})
    if "sqlalchemy.url" not in configuration:
        # Convert async URL to sync URL for migrations
        async_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./collections.db")
        if "sqlite+aiosqlite" in async_url:
            sync_url = async_url.replace("sqlite+aiosqlite", "sqlite")
        else:
            sync_url = "sqlite:///./collections.db"
        configuration["sqlalchemy.url"] = sync_url
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
