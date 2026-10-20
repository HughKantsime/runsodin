"""Migration 034: alert outbox for job create/approval alerts (additive; no data rewrite).

Creates alert_outbox. A job create or approval writes one row inside its own
transaction (job id, alert kind, actor); after the commit the dispatcher claims the
row, stages the alert fan-out in a transaction of its own and deletes the row in that
same transaction, so a claimed row yields exactly one alert. Rows a crash or a
dispatch failure leaves behind are replayed at startup and by a periodic sweep
(modules.notifications.alert_outbox).

The DDL and its shape validation live in this file, so the ledger checksum of this
migration covers them; modules/notifications/alert_outbox.py only reuses the table.

It cannot fail because of existing user data: the table starts empty and only its
exact shape is validated. Rows are transient (deleted when dispatched) and hold a job
id, an alert kind, an actor user id and counters: rows follow their job (ON DELETE
CASCADE) and an erased actor becomes NULL (ON DELETE SET NULL), so GDPR erasure needs
no separate scrub and the privacy export has no lasting user data to report.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, MetaData, String, Table, inspect, text

MIGRATION_ID = 'python:034-alert-outbox'
TABLE = "alert_outbox"
KINDS = ("job_submitted", "queue_added", "job_approved")

metadata = MetaData()
_jobs_ref = Table("jobs", metadata, Column("id", Integer, primary_key=True))
_users_ref = Table("users", metadata, Column("id", Integer, primary_key=True))
alert_outbox = Table(
    TABLE, metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("alert_kind", String(32), nullable=False),
    Column("actor_user_id", Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)
EXPECTED_COLUMNS = frozenset(c.name for c in alert_outbox.columns)
_NULLABLE = frozenset({"actor_user_id"})

_NOW = frozenset({"current_timestamp", "now()", "transaction_timestamp()"})
_CASTS = ("::timestamp with time zone", "::timestamptz", "::timestamp without time zone", "::timestamp", "::integer")


def _normalized_default(reflected) -> str:
    value = " ".join(str(reflected or "").lower().split())
    for _ in range(4):  # unwrap balanced outer parentheses, quotes and a trailing cast
        if value.startswith("(") and value.endswith(")") and value.count("(") == value.count(")"):
            inner = value[1:-1].strip()
            if inner.count("(") == inner.count(")"):
                value = inner
                continue
        if len(value) > 1 and value[0] == value[-1] == "'":
            value = value[1:-1]
            continue
        stripped = next((value[:-len(cast)].strip() for cast in _CASTS if value.endswith(cast)), None)
        if stripped is None:
            break
        value = stripped
    return value


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    metadata.create_all(connection, tables=[alert_outbox], checkfirst=True)
    validate(connection)


def validate(connection):
    from core.schema.migrator import MigrationError
    from sqlalchemy import DateTime as _DateTime, Integer as _Integer, String as _String
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        raise MigrationError(f"Missing table {TABLE}")
    columns = {item["name"]: item for item in inspector.get_columns(TABLE)}
    if set(columns) != EXPECTED_COLUMNS:
        raise MigrationError("Alert outbox column shape mismatch")
    for name in EXPECTED_COLUMNS:
        if bool(columns[name]["nullable"]) != (name in _NULLABLE):
            raise MigrationError(f"Alert outbox column nullability mismatch: {name}")
    for name in ("id", "job_id", "actor_user_id", "attempts"):
        if not isinstance(columns[name]["type"], _Integer):
            raise MigrationError(f"Alert outbox {name} must be an integer")
    kind_type = columns["alert_kind"]["type"]
    if not isinstance(kind_type, _String) or getattr(kind_type, "length", None) != 32:
        raise MigrationError("Alert outbox alert_kind must be VARCHAR(32)")
    created_type = columns["created_at"]["type"]
    if not isinstance(created_type, _DateTime) or (connection.dialect.name == "postgresql" and not created_type.timezone):
        raise MigrationError("Alert outbox created_at must be a timezone-aware timestamp")
    if _normalized_default(columns["created_at"].get("default")) not in _NOW:
        raise MigrationError("Alert outbox created_at must default to CURRENT_TIMESTAMP")
    if _normalized_default(columns["attempts"].get("default")) != "0":
        raise MigrationError("Alert outbox attempts must default to 0")
    if inspector.get_pk_constraint(TABLE).get("constrained_columns") != ["id"]:
        raise MigrationError("Alert outbox primary key missing")
    foreign = sorted((fk.get("referred_table"), tuple(fk.get("constrained_columns") or ()), tuple(fk.get("referred_columns") or ()))
                     for fk in inspector.get_foreign_keys(TABLE))
    if foreign != [("jobs", ("job_id",), ("id",)), ("users", ("actor_user_id",), ("id",))]:
        raise MigrationError("Alert outbox foreign keys mismatch")
    if connection.dialect.name == "sqlite":
        # The engine's own record (reflection misses column-level ON DELETE clauses).
        ondelete = sorted((str(row[2]), str(row[6]).upper())
                          for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list({TABLE})"))
    else:
        ondelete = sorted((fk.get("referred_table"), str((fk.get("options") or {}).get("ondelete") or "").upper())
                          for fk in inspector.get_foreign_keys(TABLE))
    if ondelete != [("jobs", "CASCADE"), ("users", "SET NULL")]:
        raise MigrationError("Alert outbox foreign keys must cascade with the job and clear an erased actor")
