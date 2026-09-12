"""
O.D.I.N. — Core database layer.

Supports both SQLite (default, self-hosted simplicity) and PostgreSQL (enterprise scale).
Database type is auto-detected from the DATABASE_URL environment variable:
  - sqlite:///./odin.db  → SQLite with WAL mode
  - postgresql://user:pass@host/db → PostgreSQL with connection pooling

Provides the SQLAlchemy engine, session factory, declarative base,
and the FastAPI get_db dependency.

Also provides the module migration runner used by docker/entrypoint.sh to
apply per-module SQL migration files idempotently.
"""

import os
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.base import Base  # noqa: F401 — Single Base instance shared across all models
from core.database_config import create_database_engine
from core.schema.migrator import run_migration_files, strip_sql_comments

# Detect database type from URL
IS_SQLITE = settings.database_url.startswith("sqlite")
IS_POSTGRES = settings.database_url.startswith("postgresql")

engine = create_database_engine(
    settings.database_url,
    role=os.getenv("ODIN_DB_ROLE", "api"),
    password_file=os.getenv("DATABASE_PASSWORD_FILE"),
    echo=settings.debug,
)

# Configure connection behavior based on database type
if IS_SQLITE:
    # SQLite-specific pragmas for performance and safety
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
        conn.execute(text("PRAGMA foreign_keys=ON"))

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        """Enable foreign key enforcement on every new SQLite connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

elif not IS_POSTGRES:
    raise ValueError(
        f"Unsupported database URL: {settings.database_url}. "
        "Use sqlite:/// for SQLite or postgresql:// for PostgreSQL."
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_type() -> str:
    """Return 'sqlite' or 'postgresql' based on the configured database."""
    if IS_SQLITE:
        return "sqlite"
    if IS_POSTGRES:
        return "postgresql"
    return "unknown"


def _strip_sql_comments(sql: str) -> str:
    """Strip SQL line comments (`-- ...`) from a SQL blob.

    The naive prior approach of `sql.split(";")` broke on comments
    containing inline semicolons (v1.9.1 prod incident 2026-04-16:
    a header comment in migration 004 read "would be dead state; one
    DELETE is the correct cleanup." The `;` inside the comment split
    the blob, leaving "one DELETE..." as the start of the next chunk
    and sqlite3 choked with `near "one": syntax error` on boot).

    We strip line comments BEFORE splitting so inline `;` inside
    comments cannot leak into SQL. Block comments (`/* ... */`) are
    rare in ODIN migrations but are left intact — SQLite parses them
    correctly; we only need to neutralize line comments, which are
    the ones that can contaminate a split when they carry a `;`.
    """
    return strip_sql_comments(sql)


def _migration_engine(database_url: str):
    if database_url == settings.database_url:
        return engine, False
    return (
        create_database_engine(
            database_url,
            role="bootstrap",
            password_file=os.getenv("DATABASE_PASSWORD_FILE"),
        ),
        True,
    )


def run_core_migrations(database_url: str | None = None) -> None:
    """Run core platform migration SQL files."""
    if database_url is None:
        database_url = settings.database_url

    core_migrations_dir = Path(__file__).parent / "migrations"
    if not core_migrations_dir.exists():
        print("  - No core migrations directory found, skipping")
        return

    sql_files = sorted(core_migrations_dir.glob("*.sql"))

    migration_engine, dispose = _migration_engine(database_url)
    try:
        applied = set(
            run_migration_files(
                migration_engine,
                [(f"core/migrations/{path.name}", path) for path in sql_files],
            )
        )
        for sql_file in sql_files:
            migration_id = f"core/migrations/{sql_file.name}"
            state = "Applied" if migration_id in applied else "Verified"
            print(f"  ✓ {state} core migration: {sql_file.name}")
    finally:
        if dispose:
            migration_engine.dispose()


def run_module_migrations(modules_dir: Path, database_url: str | None = None) -> None:
    """Run all per-module migration SQL files."""
    if database_url is None:
        database_url = settings.database_url

    if not modules_dir.exists():
        print(f"  - Modules directory not found: {modules_dir}, skipping")
        return

    files: list[tuple[str, Path]] = []
    for module_dir in sorted(modules_dir.iterdir()):
        if not module_dir.is_dir():
            continue
        migrations_dir = module_dir / "migrations"
        if not migrations_dir.exists():
            continue
        sql_files = sorted(migrations_dir.glob("*.sql"))
        for sql_file in sql_files:
            files.append((f"modules/{module_dir.name}/migrations/{sql_file.name}", sql_file))

    migration_engine, dispose = _migration_engine(database_url)
    try:
        applied = set(run_migration_files(migration_engine, files))
        for migration_id, sql_file in files:
            state = "Applied" if migration_id in applied else "Verified"
            print(f"  ✓ {state} {migration_id}: {sql_file.name}")
    finally:
        if dispose:
            migration_engine.dispose()
