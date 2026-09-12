"""
Alembic environment configuration for O.D.I.N.

Reads DATABASE_URL from environment (falling back to config.py settings),
imports SQLAlchemy metadata from models.py, and enables batch mode for
SQLite compatibility.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import make_url
from core.base import Base
from core.database_config import create_database_engine
from core.schema.bootstrap import import_all_models

# Alembic Config object (provides access to alembic.ini values)
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLAlchemy metadata for autogenerate support
import_all_models()
target_metadata = Base.metadata

# Resolve database URL: env var > config.py default
def get_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from core.config import settings
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
        render_as_batch=make_url(url).get_backend_name() == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations against a live database connection."""
    connectable = create_database_engine(
        get_url(),
        role="bootstrap",
        password_file=os.environ.get("DATABASE_PASSWORD_FILE"),
    )

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=connection.dialect.name == "sqlite",
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
