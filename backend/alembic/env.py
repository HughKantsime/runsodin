"""
Alembic environment configuration for O.D.I.N.

Reads DATABASE_URL from environment (falling back to config.py settings),
imports SQLAlchemy metadata from models.py, and enables batch mode for
SQLite compatibility.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from models import Base

# Alembic Config object (provides access to alembic.ini values)
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLAlchemy metadata for autogenerate support
target_metadata = Base.metadata

# Resolve database URL: env var > config.py default
def get_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from config import settings
        return settings.database_url
    except Exception:
        return "sqlite:////data/odin.db"


def run_migrations_offline():
    """Run migrations in 'offline' mode (generates SQL script without DB connection)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite compatibility
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations against a live database connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite compatibility
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
