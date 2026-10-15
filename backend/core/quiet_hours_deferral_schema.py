"""Deferred quiet-hours deliveries (migration 032) — table model and shape validation.

One row per (alert, recipient, external channel) that quiet hours suppressed for a
recipient who has no in-app alert row (in-app alerts are already digested from the
alerts table). Rows are org-scoped (scope_key 'system' | 'org:<id>'), delivered by
the quiet-hours digest exactly once, then purged after a retention period.

The table lives on its own MetaData, not core.base.Base: until migration 032 is
registered (see .claude/specs/post027-migrations.md) the pinned ORM manifest and
baselines must not change. Runtime code checks table presence and degrades to the
pre-032 behaviour when it is absent.
"""
from sqlalchemy import (CheckConstraint, Column, DateTime, Index, Integer, MetaData, String, Table, Text,
                        inspect, text)

TABLE = "quiet_hours_deferred_deliveries"
CHANNELS = ("email", "push")
STATES = ("pending", "sent", "failed")

metadata = MetaData()
deferred_deliveries = Table(
    TABLE, metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", Integer, nullable=False),
    Column("org_id", Integer),
    Column("scope_key", String(40), nullable=False),
    Column("channel", String(10), nullable=False),
    Column("alert_type", String(64), nullable=False),
    Column("severity", String(20), nullable=False),
    Column("title", Text, nullable=False),
    Column("message", Text),
    Column("printer_id", Integer),
    Column("job_id", Integer),
    Column("spool_id", Integer),
    Column("state", String(10), nullable=False, server_default=text("'pending'")),
    Column("outcome_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("delivered_at", DateTime(timezone=True)),
    CheckConstraint("channel IN ('email','push')", name="ck_qhdd_channel"),
    CheckConstraint("state IN ('pending','sent','failed')", name="ck_qhdd_state"),
    CheckConstraint("scope_key = 'system' OR scope_key LIKE 'org:%'", name="ck_qhdd_scope"),
)
Index("idx_qhdd_scope_state_created", deferred_deliveries.c.scope_key, deferred_deliveries.c.state,
      deferred_deliveries.c.created_at)
Index("idx_qhdd_user", deferred_deliveries.c.user_id)

EXPECTED_COLUMNS = {c.name for c in deferred_deliveries.columns}
EXPECTED_INDEXES = {"idx_qhdd_scope_state_created": ("scope_key", "state", "created_at"),
                    "idx_qhdd_user": ("user_id",)}
EXPECTED_CHECKS = {"ck_qhdd_channel", "ck_qhdd_state", "ck_qhdd_scope"}


def table_present(connection) -> bool:
    return inspect(connection).has_table(TABLE)


def create(connection) -> None:
    metadata.create_all(connection, tables=[deferred_deliveries], checkfirst=True)


def validate(connection) -> None:
    from core.schema.migrator import MigrationError
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        raise MigrationError(f"Missing table {TABLE}")
    columns = {item["name"]: item for item in inspector.get_columns(TABLE)}
    if set(columns) != EXPECTED_COLUMNS:
        raise MigrationError("Deferred delivery column shape mismatch")
    for name in ("id", "user_id", "scope_key", "channel", "alert_type", "severity", "title", "state", "created_at"):
        if columns[name]["nullable"]:
            raise MigrationError(f"Deferred delivery column must be NOT NULL: {name}")
    if inspector.get_pk_constraint(TABLE).get("constrained_columns") != ["id"]:
        raise MigrationError("Deferred delivery primary key missing")
    indexes = {item["name"]: tuple(item["column_names"]) for item in inspector.get_indexes(TABLE)}
    for name, cols in EXPECTED_INDEXES.items():
        if indexes.get(name) != cols:
            raise MigrationError(f"Deferred delivery index shape mismatch: {name}")
    checks = {item["name"] for item in inspector.get_check_constraints(TABLE)}
    if not EXPECTED_CHECKS <= checks:
        raise MigrationError("Deferred delivery check constraints missing")
