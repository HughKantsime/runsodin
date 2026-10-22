"""Migration 033: capture-time tenancy of timelapse media (additive; no data rewrite).

Creates timelapse_capture_tenancy: one row per ownership anchor, written at capture
and copied to derived anchors (edit, encode, inherit), recording whether the
capture was made while the install had no organizations. The audience rule
(core.timelapse_access) reads this fact instead of anchor timestamps.

The DDL and its shape validation live in this file, so the ledger checksum of
this migration covers them; core/timelapse_capture_tenancy_schema.py only reuses
the table object for runtime reads and writes.

It cannot fail because of existing user data: the table starts empty and only its
exact shape is validated. Existing anchors get no row and use the documented
fallback (install-wide while no organization exists, admin-only once one does).
Rows hold only an anchor id, a boolean and a timestamp: no user data, so GDPR
export/erasure has nothing to scrub; rows follow their anchor (ON DELETE CASCADE),
and ODIN never deletes anchors.
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, MetaData, String, Table, inspect, text

MIGRATION_ID = 'python:033-timelapse-capture-tenancy'
TABLE = "timelapse_capture_tenancy"

metadata = MetaData()
_anchors_ref = Table("timelapse_ownership_anchors", metadata, Column("operation_id", String(64), primary_key=True))
capture_tenancy = Table(
    TABLE, metadata,
    Column("operation_id", String(64), ForeignKey("timelapse_ownership_anchors.operation_id", ondelete="CASCADE"),
           primary_key=True),
    Column("captured_single_tenant", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)
EXPECTED_COLUMNS = frozenset(c.name for c in capture_tenancy.columns)


# Spellings of "the current time" a reflected default may take: SQLite keeps the
# declared text; PostgreSQL reports CURRENT_TIMESTAMP or now(), possibly wrapped in
# parentheses or cast to timestamptz.
_NOW = frozenset({"current_timestamp", "now()", "transaction_timestamp()"})
_CASTS = ("::timestamp with time zone", "::timestamptz", "::timestamp without time zone", "::timestamp")


def _current_timestamp_default(reflected) -> bool:
    text_value = " ".join(str(reflected or "").lower().split())
    for _ in range(4):  # unwrap balanced outer parentheses and a trailing cast
        if text_value.startswith("(") and text_value.endswith(")") and text_value.count("(") == text_value.count(")"):
            inner = text_value[1:-1].strip()
            if inner.count("(") == inner.count(")"):
                text_value = inner
                continue
        stripped = next((text_value[:-len(cast)].strip() for cast in _CASTS if text_value.endswith(cast)), None)
        if stripped is None:
            break
        text_value = stripped
    return text_value in _NOW


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    metadata.create_all(connection, tables=[capture_tenancy], checkfirst=True)
    validate(connection)


def validate(connection):
    from core.schema.migrator import MigrationError
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        raise MigrationError(f"Missing table {TABLE}")
    columns = {item["name"]: item for item in inspector.get_columns(TABLE)}
    if set(columns) != EXPECTED_COLUMNS:
        raise MigrationError("Capture tenancy column shape mismatch")
    for name in EXPECTED_COLUMNS:
        if columns[name]["nullable"]:
            raise MigrationError(f"Capture tenancy column must be NOT NULL: {name}")
    from sqlalchemy import Boolean as _Boolean, DateTime as _DateTime, String as _String
    operation_type = columns["operation_id"]["type"]
    if not isinstance(operation_type, _String) or getattr(operation_type, "length", None) != 64:
        raise MigrationError("Capture tenancy operation_id must be VARCHAR(64)")
    if not isinstance(columns["captured_single_tenant"]["type"], _Boolean):
        raise MigrationError("Capture tenancy captured_single_tenant must be BOOLEAN")
    created_type = columns["created_at"]["type"]
    # SQLite does not record time zones; PostgreSQL must store timestamptz.
    if not isinstance(created_type, _DateTime) or (connection.dialect.name == "postgresql" and not created_type.timezone):
        raise MigrationError("Capture tenancy created_at must be a timezone-aware timestamp")
    if not _current_timestamp_default(columns["created_at"].get("default")):
        raise MigrationError("Capture tenancy created_at must default to CURRENT_TIMESTAMP")
    if inspector.get_pk_constraint(TABLE).get("constrained_columns") != ["operation_id"]:
        raise MigrationError("Capture tenancy primary key missing")
    foreign = [(fk.get("referred_table"), tuple(fk.get("constrained_columns") or ()), tuple(fk.get("referred_columns") or ()))
               for fk in inspector.get_foreign_keys(TABLE)]
    if foreign != [("timelapse_ownership_anchors", ("operation_id",), ("operation_id",))]:
        raise MigrationError("Capture tenancy foreign key mismatch")
    if connection.dialect.name == "sqlite":
        # The engine's own record (reflection misses column-level ON DELETE clauses).
        ondelete = [str(row[6]).upper() for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list({TABLE})")]
    else:
        ondelete = [str((fk.get("options") or {}).get("ondelete") or "").upper() for fk in inspector.get_foreign_keys(TABLE)]
    if ondelete != ["CASCADE"]:
        raise MigrationError("Capture tenancy foreign key must cascade on anchor delete")
