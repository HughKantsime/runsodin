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

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from core.config import settings
from core.base import Base  # noqa: F401 — Single Base instance shared across all models

# Detect database type from URL
IS_SQLITE = settings.database_url.startswith("sqlite")
IS_POSTGRES = settings.database_url.startswith("postgresql")

# Configure engine based on database type
if IS_SQLITE:
    engine = create_engine(
        settings.database_url,
        echo=settings.debug,
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
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

elif IS_POSTGRES:
    engine = create_engine(
        settings.database_url,
        echo=settings.debug,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
    )
else:
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


def _db_path_from_url(database_url: str) -> str:
    """Extract the filesystem path from a sqlite:/// URL."""
    if database_url.startswith("sqlite:////"):
        return database_url[len("sqlite:///"):]
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    raise ValueError(f"Unsupported database URL for SQLite migration runner: {database_url}")


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
    out_lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        # Naive check — doesn't account for `--` inside a string
        # literal, but none of the ODIN migrations use that pattern.
        # If that ever becomes an issue, swap in sqlparse or
        # equivalent.
        if idx >= 0:
            line = line[:idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def _run_sql_file(db_path: str, sql_file: Path) -> None:
    """Execute a single SQL migration file against the SQLite database."""
    sql = sql_file.read_text(encoding="utf-8")

    non_comment_lines = [
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    if not non_comment_lines:
        return

    conn = sqlite3.connect(db_path)
    try:
        if "ALTER TABLE" in sql.upper():
            # Strip comments FIRST so an inline `;` inside a comment
            # (see `_strip_sql_comments` docstring for the prod
            # incident) can't split a statement in half.
            stripped = _strip_sql_comments(sql)
            for stmt in stripped.split(";"):
                stmt = stmt.strip()
                if not stmt:
                    continue
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" in str(exc):
                        pass
                    else:
                        raise
            conn.commit()
        else:
            conn.executescript(sql)
            conn.commit()
    finally:
        conn.close()


def _run_pg_migration(sql_file: Path) -> None:
    """Execute a SQL migration file against PostgreSQL.

    Converts common SQLite syntax to PostgreSQL on the fly.
    """
    sql = sql_file.read_text(encoding="utf-8")

    non_comment_lines = [
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    if not non_comment_lines:
        return

    # SQLite → PostgreSQL syntax conversion
    sql = sql.replace("AUTOINCREMENT", "")
    sql = sql.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
    sql = sql.replace("datetime('now')", "NOW()")
    sql = sql.replace("datetime('now', 'localtime')", "NOW()")
    sql = sql.replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
    sql = sql.replace("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")
    sql = sql.replace("TEXT NOT NULL DEFAULT ''", "TEXT NOT NULL DEFAULT ''")

    with engine.begin() as conn:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if not stmt or stmt.startswith("--"):
                continue
            real_lines = [l for l in stmt.splitlines()
                          if l.strip() and not l.strip().startswith("--")]
            if not real_lines:
                continue
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                err_str = str(exc).lower()
                if "already exists" in err_str or "duplicate" in err_str:
                    pass
                else:
                    raise


def run_core_migrations(database_url: str | None = None) -> None:
    """Run core platform migration SQL files."""
    if database_url is None:
        database_url = settings.database_url

    core_migrations_dir = Path(__file__).parent / "migrations"
    if not core_migrations_dir.exists():
        print("  - No core migrations directory found, skipping")
        return

    sql_files = sorted(core_migrations_dir.glob("*.sql"))

    if IS_SQLITE:
        db_path = _db_path_from_url(database_url)
        for sql_file in sql_files:
            _run_sql_file(db_path, sql_file)
            print(f"  ✓ Applied core migration: {sql_file.name}")
    elif IS_POSTGRES:
        for sql_file in sql_files:
            _run_pg_migration(sql_file)
            print(f"  ✓ Applied core migration (pg): {sql_file.name}")


def run_module_migrations(modules_dir: Path, database_url: str | None = None) -> None:
    """Run all per-module migration SQL files."""
    if database_url is None:
        database_url = settings.database_url

    if not modules_dir.exists():
        print(f"  - Modules directory not found: {modules_dir}, skipping")
        return

    if IS_SQLITE:
        db_path = _db_path_from_url(database_url)
    else:
        db_path = None

    for module_dir in sorted(modules_dir.iterdir()):
        if not module_dir.is_dir():
            continue
        migrations_dir = module_dir / "migrations"
        if not migrations_dir.exists():
            continue
        sql_files = sorted(migrations_dir.glob("*.sql"))
        for sql_file in sql_files:
            if IS_SQLITE:
                _run_sql_file(db_path, sql_file)
                print(f"  ✓ Applied {module_dir.name} migration: {sql_file.name}")
            elif IS_POSTGRES:
                _run_pg_migration(sql_file)
                print(f"  ✓ Applied {module_dir.name} migration (pg): {sql_file.name}")
