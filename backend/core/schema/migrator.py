"""Transactional, checksummed migration runner for SQLite and PostgreSQL."""

from __future__ import annotations

import hashlib
import importlib
import inspect as python_inspect
import re
from pathlib import Path
from typing import Iterable

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, text
from sqlalchemy.sql.sqltypes import DateTime as SQLAlchemyDateTime
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql import func


class MigrationError(RuntimeError):
    """Raised when migration history or execution is unsafe."""


MIGRATION_TABLE = Table(
    "odin_schema_migrations",
    MetaData(),
    Column("migration_id", String(255), primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column("dialect", String(32), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

_ALTER_ADD_RE = re.compile(
    r"^ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_INDEX_RE = re.compile(
    r"^CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_ALLOWED_PYTHON_MIGRATIONS = frozenset(
    {"core.schema.migrations.001_legacy_columns"}
)


def strip_sql_comments(sql: str) -> str:
    """Strip SQL `--` comments; repository migrations contain no quoted `--`."""
    output: list[str] = []
    for line in sql.splitlines():
        marker = line.find("--")
        output.append(line[:marker] if marker >= 0 else line)
    return "\n".join(output)


def split_sql_statements(sql: str) -> list[str]:
    """Split the repository's simple migration SQL after comments are removed."""
    return [
        statement.strip()
        for statement in strip_sql_comments(sql).split(";")
        if statement.strip()
    ]


def translate_statement(statement: str, dialect: str) -> str:
    if dialect != "postgresql":
        return statement
    translated = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        statement,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"DEFAULT\s*\(\s*datetime\s*\(\s*'now'\s*\)\s*\)",
        "DEFAULT CURRENT_TIMESTAMP",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bDATETIME\b",
        "TIMESTAMP WITH TIME ZONE",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bBOOLEAN\s+DEFAULT\s+0\b",
        "BOOLEAN DEFAULT FALSE",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bBOOLEAN\s+DEFAULT\s+1\b",
        "BOOLEAN DEFAULT TRUE",
        translated,
        flags=re.IGNORECASE,
    )
    return translated


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_ledger(connection: Connection) -> None:
    MIGRATION_TABLE.create(bind=connection, checkfirst=True)


def _recorded(connection: Connection, migration_id: str) -> tuple[str, str] | None:
    row = connection.execute(
        text(
            "SELECT checksum, dialect FROM odin_schema_migrations "
            "WHERE migration_id = :migration_id"
        ),
        {"migration_id": migration_id},
    ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _column_exists(connection: Connection, table_name: str, column_name: str) -> bool:
    inspector = inspect(connection)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _normalized_type(value: object) -> str:
    if isinstance(value, SQLAlchemyDateTime):
        return "TIMESTAMP WITH TIME ZONE" if value.timezone else "DATETIME"
    normalized = re.sub(r"\s+", " ", str(value).upper().strip())
    return normalized.replace("CHARACTER VARYING", "VARCHAR").replace(
        "DOUBLE PRECISION", "FLOAT"
    )


def _expected_type(definition: str) -> str:
    match = re.match(
        r"(TIMESTAMP(?:\s+WITH(?:OUT)?\s+TIME\s+ZONE)?|DATETIME|"
        r"VARCHAR(?:\s*\(\s*\d+\s*\))?|BOOLEAN|INTEGER|BIGINT|FLOAT|REAL|TEXT|JSON)"
        r"(?![A-Za-z0-9_])",
        definition.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise MigrationError(f"Unsupported migration column definition: {definition}")
    return re.sub(r"\s*\(\s*", "(", re.sub(r"\s*\)\s*", ")", _normalized_type(match.group(1))))


def _normalized_default(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    normalized = re.sub(r"::[A-Za-z_][A-Za-z0-9_ ]*(?:\[\])?$", "", normalized)
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == "'":
        normalized = normalized[1:-1].replace("''", "'")
    normalized = normalized.lower()
    if normalized in {"0", "false"}:
        return "false"
    if normalized in {"1", "true"}:
        return "true"
    return normalized


def _reflected_default(
    connection: Connection,
    table_name: str,
    column_name: str,
    fallback: object | None,
) -> object | None:
    if connection.dialect.name != "sqlite":
        return fallback
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise MigrationError(f"Unsafe migration table identifier: {table_name}")
    for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")'):
        if row[1] == column_name:
            return row[4]
    return fallback


def validate_column_shape(
    connection: Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    """Fail closed when an adopted column differs from its migration contract."""
    columns = {
        column["name"]: column
        for column in inspect(connection).get_columns(table_name)
    }
    column = columns.get(column_name)
    if column is None:
        raise MigrationError(f"Migration column is missing: {table_name}.{column_name}")
    if _normalized_type(column["type"]) != _expected_type(definition):
        raise MigrationError(
            f"Migration column shape mismatch: {table_name}.{column_name}"
        )

    expected_nullable = "NOT NULL" not in definition.upper()
    if bool(column.get("nullable", True)) != expected_nullable:
        raise MigrationError(
            f"Migration column shape mismatch: {table_name}.{column_name}"
        )

    default_match = re.search(
        r"\bDEFAULT\s+('(?:''|[^'])*'|[^\s,]+)", definition, re.IGNORECASE
    )
    expected_default = _normalized_default(default_match.group(1)) if default_match else None
    actual_default = _reflected_default(
        connection, table_name, column_name, column.get("default")
    )
    if _normalized_default(actual_default) != expected_default:
        raise MigrationError(
            f"Migration column shape mismatch: {table_name}.{column_name}"
        )

    reference = re.search(
        r"\bREFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        definition,
        re.IGNORECASE,
    )
    if reference:
        expected_target = f"{reference.group(1)}.{reference.group(2)}"
        targets = {
            f"{foreign_key['referred_table']}.{remote}"
            for foreign_key in inspect(connection).get_foreign_keys(table_name)
            if column_name in foreign_key.get("constrained_columns", [])
            for remote in foreign_key.get("referred_columns", [])
        }
        if targets != {expected_target}:
            raise MigrationError(
                f"Migration column shape mismatch: {table_name}.{column_name}"
            )


def _validate_index_shape(
    connection: Connection,
    table_name: str,
    index_name: str,
    columns: list[str],
    unique: bool,
) -> bool:
    matches = [
        item
        for item in inspect(connection).get_indexes(table_name)
        if item.get("name") == index_name
    ]
    if not matches:
        return False
    item = matches[0]
    if list(item.get("column_names") or []) != columns or bool(item.get("unique")) != unique:
        raise MigrationError(f"Migration index shape mismatch: {index_name}")
    return True


def apply_migration_file(
    connection: Connection,
    path: Path,
    *,
    migration_id: str,
) -> bool:
    """Apply and record one migration inside the caller's transaction."""
    ensure_ledger(connection)
    dialect = connection.dialect.name
    checksum = _checksum(path)
    prior = _recorded(connection, migration_id)
    if prior:
        if prior != (checksum, dialect):
            raise MigrationError(f"Migration history mismatch: {migration_id}")
        return False

    for original in split_sql_statements(path.read_text(encoding="utf-8")):
        statement = translate_statement(original, dialect)
        alter = _ALTER_ADD_RE.match(statement)
        if alter:
            table_name, column_name, definition = alter.groups()
            if _column_exists(connection, table_name, column_name):
                validate_column_shape(connection, table_name, column_name, definition)
                continue
        create_index = _CREATE_INDEX_RE.match(statement)
        if create_index:
            unique_marker, index_name, table_name, column_list = create_index.groups()
            indexed_columns = [
                item.strip().split()[0].strip('"')
                for item in column_list.split(",")
            ]
            if not set(indexed_columns).issubset(
                {
                    column["name"]
                    for column in inspect(connection).get_columns(table_name)
                }
            ):
                # A later migration may add these columns and owns creation of
                # the corresponding index. This is required when adopting an
                # unledgered intermediate schema.
                continue
            if _validate_index_shape(
                connection,
                table_name,
                index_name,
                indexed_columns,
                bool(unique_marker),
            ):
                continue
        connection.execute(text(statement))
        if alter:
            validate_column_shape(connection, table_name, column_name, definition)
        elif create_index and not _validate_index_shape(
            connection,
            table_name,
            index_name,
            indexed_columns,
            bool(unique_marker),
        ):
            raise MigrationError(f"Migration index is missing: {index_name}")

    connection.execute(
        MIGRATION_TABLE.insert().values(
            migration_id=migration_id,
            checksum=checksum,
            dialect=dialect,
        )
    )
    return True


def apply_python_migration(connection: Connection, module_name: str) -> bool:
    """Apply one source-checksummed Python migration module."""
    ensure_ledger(connection)
    if module_name not in _ALLOWED_PYTHON_MIGRATIONS:
        raise MigrationError(f"Unsupported Python migration: {module_name}")
    module = importlib.import_module(module_name)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- exact module name is checked against a closed allowlist
    migration_id = str(module.MIGRATION_ID)
    source_path = Path(python_inspect.getsourcefile(module) or "")
    if not source_path.is_file():
        raise MigrationError(f"Cannot checksum Python migration: {migration_id}")
    checksum = _checksum(source_path)
    dialect = connection.dialect.name
    prior = _recorded(connection, migration_id)
    if prior:
        if prior != (checksum, dialect):
            raise MigrationError(f"Migration history mismatch: {migration_id}")
        return False
    module.apply(connection)
    module.validate(connection)
    connection.execute(
        MIGRATION_TABLE.insert().values(
            migration_id=migration_id,
            checksum=checksum,
            dialect=dialect,
        )
    )
    return True


def run_migration_files(
    engine: Engine,
    files: Iterable[tuple[str, Path]],
) -> list[str]:
    applied: list[str] = []
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(71403114720480978)"))
        for migration_id, path in files:
            if apply_migration_file(connection, path, migration_id=migration_id):
                applied.append(migration_id)
    return applied
