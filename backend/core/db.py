"""
O.D.I.N. — Core database layer.

Provides the SQLAlchemy engine, session factory, declarative base,
and the FastAPI get_db dependency.

Also provides the module migration runner used by docker/entrypoint.sh to
apply per-module SQL migration files idempotently.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import get_db) continues to work via re-exports in deps.py.
"""

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.config import settings
from core.base import Base  # Single Base instance shared across all models

engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    poolclass=NullPool,
    connect_args={"check_same_thread": False},
)
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA busy_timeout=5000"))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _db_path_from_url(database_url: str) -> str:
    """Extract the filesystem path from a sqlite:/// URL."""
    # Handles sqlite:////abs/path and sqlite:///rel/path
    if database_url.startswith("sqlite:////"):
        return database_url[len("sqlite:///"):]
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    raise ValueError(f"Unsupported database URL for migration runner: {database_url}")


def _run_sql_file(db_path: str, sql_file: Path) -> None:
    """Execute a single SQL migration file against the SQLite database.

    Uses executescript() which runs all statements in the file as a batch.
    CREATE TABLE IF NOT EXISTS ensures idempotency — safe to re-run.
    Skips files that contain only comments and whitespace.
    """
    sql = sql_file.read_text(encoding="utf-8")

    # Strip comment lines and check if there's any real SQL to execute
    non_comment_lines = [
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    if not non_comment_lines:
        return

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def run_core_migrations(database_url: str | None = None) -> None:
    """Run core platform migration SQL files.

    Executes backend/core/migrations/*.sql in sorted filename order.
    Called from docker/entrypoint.sh before module migrations, because
    the users table (defined here) is a FK target for most modules.
    """
    if database_url is None:
        database_url = settings.database_url
    db_path = _db_path_from_url(database_url)

    core_migrations_dir = Path(__file__).parent / "migrations"
    if not core_migrations_dir.exists():
        print("  - No core migrations directory found, skipping")
        return

    sql_files = sorted(core_migrations_dir.glob("*.sql"))
    for sql_file in sql_files:
        _run_sql_file(db_path, sql_file)
        print(f"  ✓ Applied core migration: {sql_file.name}")


def run_module_migrations(modules_dir: Path, database_url: str | None = None) -> None:
    """Run all per-module migration SQL files.

    Discovers modules by iterating modules_dir/*/migrations/*.sql
    in sorted order (module name alphabetical, then filename).

    Migration order for FK safety:
      organizations depends on core (users)
      printers, jobs, inventory, models_library, notifications,
      archives, reporting, system, vision, orders depend on printers/core/groups.

    The sorted alphabetical order works for this codebase because:
      - core/ runs first via run_core_migrations()
      - organizations/ runs before printers/ alphabetically (o < p)
      - All other modules depend only on core/printers/organizations tables,
        which are created before them alphabetically or by create_all()

    Args:
        modules_dir: Path to the backend/modules/ directory.
        database_url: SQLite URL. Defaults to settings.database_url.
    """
    if database_url is None:
        database_url = settings.database_url
    db_path = _db_path_from_url(database_url)

    if not modules_dir.exists():
        print(f"  - Modules directory not found: {modules_dir}, skipping")
        return

    # Iterate modules in sorted order for deterministic execution
    for module_dir in sorted(modules_dir.iterdir()):
        if not module_dir.is_dir():
            continue
        migrations_dir = module_dir / "migrations"
        if not migrations_dir.exists():
            continue
        sql_files = sorted(migrations_dir.glob("*.sql"))
        for sql_file in sql_files:
            _run_sql_file(db_path, sql_file)
            print(f"  ✓ Applied {module_dir.name} migration: {sql_file.name}")
