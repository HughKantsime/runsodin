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
    if inspector.get_pk_constraint(TABLE).get("constrained_columns") != ["operation_id"]:
        raise MigrationError("Capture tenancy primary key missing")
    foreign = [(fk.get("referred_table"), tuple(fk.get("constrained_columns") or ()), tuple(fk.get("referred_columns") or ()))
               for fk in inspector.get_foreign_keys(TABLE)]
    if foreign != [("timelapse_ownership_anchors", ("operation_id",), ("operation_id",))]:
        raise MigrationError("Capture tenancy foreign key mismatch")
