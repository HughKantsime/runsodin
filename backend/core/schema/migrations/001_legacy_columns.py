"""Adopt columns historically added by the container entrypoint."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import ENUM as PostgreSQLEnum


MIGRATION_ID = "python:001-legacy-columns"


COMMON_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (
        ("mfa_enabled", "BOOLEAN DEFAULT {false}"),
        ("mfa_secret", "TEXT"),
        ("quota_grams", "REAL"),
        ("quota_hours", "REAL"),
        ("quota_jobs", "INTEGER"),
        ("quota_period", "VARCHAR(20) DEFAULT 'monthly'"),
        ("theme_json", "TEXT"),
        ("group_id", "INTEGER"),
    ),
    "groups": (
        ("is_org", "BOOLEAN DEFAULT {false}"),
        ("branding_json", "TEXT"),
        ("settings_json", "TEXT"),
    ),
    "printers": (
        ("org_id", "INTEGER"),
        ("shared", "BOOLEAN"),
        ("fan_speed", "INTEGER"),
        ("tags", "JSON"),
        ("timelapse_enabled", "BOOLEAN"),
        ("bed_x_mm", "FLOAT"),
        ("bed_y_mm", "FLOAT"),
        ("machine_type", "VARCHAR(20)"),
    ),
    "models": (("org_id", "INTEGER"),),
    "spools": (
        ("org_id", "INTEGER"),
        ("pa_profile", "VARCHAR(50)"),
        ("low_stock_threshold_g", "INTEGER"),
        ("spoolman_spool_id", "INTEGER"),
    ),
    "jobs": (
        ("charged_to_user_id", "INTEGER"),
        ("charged_to_org_id", "INTEGER"),
        ("model_revision_id", "INTEGER"),
        ("required_tags", "JSON"),
        ("queue_position", "INTEGER"),
        ("target_type", "VARCHAR(20)"),
        ("target_filter", "VARCHAR(100)"),
    ),
    "print_jobs": (
        ("job_id", "TEXT"),
        ("filename", "TEXT"),
        ("total_layers", "INTEGER"),
        ("current_layer", "INTEGER"),
        ("remaining_minutes", "REAL"),
        ("bed_temp_target", "REAL"),
        ("nozzle_temp_target", "REAL"),
        ("filament_slots", "TEXT"),
        ("error_code", "TEXT"),
        ("scheduled_job_id", "INTEGER"),
        ("created_at", "{datetime} DEFAULT {current_timestamp}"),
        ("model_revision_id", "INTEGER"),
    ),
    "print_files": (
        ("bed_x_mm", "REAL"),
        ("bed_y_mm", "REAL"),
        ("compatible_api_types", "TEXT"),
        ("file_hash", "TEXT"),
        ("plate_count", "INTEGER DEFAULT 1"),
    ),
    "print_archives": (
        ("tags", "TEXT DEFAULT ''"),
        ("plate_count", "INTEGER DEFAULT 1"),
        ("plate_thumbnails", "TEXT"),
        ("print_file_id", "INTEGER"),
        ("project_id", "INTEGER"),
        ("energy_kwh", "REAL"),
        ("energy_cost", "REAL"),
        ("consumption_json", "TEXT"),
        ("file_hash", "TEXT"),
    ),
    "vision_settings": (
        ("build_plate_empty_enabled", "INTEGER"),
        ("build_plate_empty_threshold", "FLOAT"),
    ),
}


def _table_columns(connection, table_name: str) -> set[str]:
    inspector = inspect(connection)
    if table_name not in inspector.get_table_names():
        raise RuntimeError(f"Legacy migration requires missing table: {table_name}")
    return {column["name"] for column in inspector.get_columns(table_name)}


def _column_type(connection, table_name: str, column_name: str):
    for column in inspect(connection).get_columns(table_name):
        if column["name"] == column_name:
            return column["type"]
    return None


def apply(connection) -> None:
    from core.schema.migrator import validate_column_shape

    false_literal = "FALSE" if connection.dialect.name == "postgresql" else "0"
    datetime_type = (
        "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME"
    )
    current_timestamp = (
        "CURRENT_TIMESTAMP"
        if connection.dialect.name == "postgresql"
        else "(datetime('now'))"
    )
    for table_name, definitions in COMMON_COLUMNS.items():
        columns = _table_columns(connection, table_name)
        for column_name, definition in definitions:
            column_type = definition.format(
                false=false_literal,
                datetime=datetime_type,
                current_timestamp=current_timestamp,
            )
            if column_name in columns:
                validate_column_shape(
                    connection, table_name, column_name, column_type
                )
                continue
            connection.execute(
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- identifiers and types come only from immutable COMMON_COLUMNS
                text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            )
            columns.add(column_name)
            validate_column_shape(connection, table_name, column_name, column_type)

    enum_rules = (
        ("jobs", "status", ("pending", "submitted", "rejected", "scheduled", "printing", "paused", "completed", "failed", "cancelled")),
        ("spools", "status", ("active", "empty", "archived")),
        ("orders", "status", ("pending", "in_progress", "partial", "fulfilled", "shipped", "cancelled")),
    )
    for table_name, column_name, values in enum_rules:
        if column_name not in _table_columns(connection, table_name):
            continue
        # Current PostgreSQL schemas use native SQLAlchemy enums whose labels
        # are already the canonical lowercase values. LOWER(enum) is invalid,
        # while legacy VARCHAR/TEXT columns still need normalization.
        if connection.dialect.name == "postgresql" and isinstance(
            _column_type(connection, table_name, column_name), PostgreSQLEnum
        ):
            continue
        allowed = ", ".join(f"'{value}'" for value in values)
        connection.execute(
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- identifiers and enum labels come only from immutable enum_rules
            text(
                f"UPDATE {table_name} SET {column_name} = LOWER({column_name}) "
                f"WHERE {column_name} <> LOWER({column_name}) "
                f"AND LOWER({column_name}) IN ({allowed})"
            )
        )
    for table_name in ("filament_slots", "spools", "jobs"):
        if "filament_type" in _table_columns(connection, table_name):
            if connection.dialect.name == "postgresql" and isinstance(
                _column_type(connection, table_name, "filament_type"),
                PostgreSQLEnum,
            ):
                continue
            connection.execute(
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table identifiers come only from the fixed tuple above
                text(
                    f"UPDATE {table_name} SET filament_type = 'empty' "
                    "WHERE filament_type = 'EMPTY'"
                )
            )


def validate(connection) -> None:
    from core.schema.migrator import validate_column_shape

    false_literal = "FALSE" if connection.dialect.name == "postgresql" else "0"
    datetime_type = (
        "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME"
    )
    current_timestamp = (
        "CURRENT_TIMESTAMP"
        if connection.dialect.name == "postgresql"
        else "(datetime('now'))"
    )
    missing: list[str] = []
    for table_name, definitions in COMMON_COLUMNS.items():
        columns = _table_columns(connection, table_name)
        missing.extend(
            f"{table_name}.{column_name}"
            for column_name, _ in definitions
            if column_name not in columns
        )
        for column_name, definition in definitions:
            if column_name in columns:
                validate_column_shape(
                    connection,
                    table_name,
                    column_name,
                    definition.format(
                        false=false_literal,
                        datetime=datetime_type,
                        current_timestamp=current_timestamp,
                    ),
                )
    if missing:
        raise RuntimeError("Legacy migration postcondition failed: " + ", ".join(missing))
