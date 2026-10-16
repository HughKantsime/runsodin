"""Runtime access to timelapse_capture_tenancy (migration 033).

The table object, DDL and shape validation are defined in the migration file
(core/schema/migrations/033_timelapse_capture_tenancy.py), covered by its ledger
checksum; this module only reads and writes rows. See the migration docstring for
the audience fallback of anchors without a row and for data-protection notes
(no user data: anchor id, boolean, timestamp).
"""
import importlib

from sqlalchemy import inspect

_migration = importlib.import_module("core.schema.migrations.033_timelapse_capture_tenancy")
TABLE = _migration.TABLE
EXPECTED_COLUMNS = set(_migration.EXPECTED_COLUMNS)
capture_tenancy = _migration.capture_tenancy
validate = _migration.validate


def table_present(connection) -> bool:
    return inspect(connection).has_table(TABLE)


def record(connection, operation_id: str, captured_single_tenant: bool) -> None:
    """Write the capture-time fact for a new anchor (no-op before 033 is applied)."""
    if not table_present(connection):
        return
    connection.execute(capture_tenancy.insert().values(operation_id=operation_id,
                                                       captured_single_tenant=bool(captured_single_tenant)))


def copy(connection, source_operation_id: str, derived_operation_id: str) -> None:
    """Carry the source anchor's fact to a derived anchor (edit, encode, inherit)."""
    if not table_present(connection):
        return
    row = connection.execute(capture_tenancy.select().where(
        capture_tenancy.c.operation_id == source_operation_id)).mappings().first()
    if row is not None:
        connection.execute(capture_tenancy.insert().values(operation_id=derived_operation_id,
                                                           captured_single_tenant=row["captured_single_tenant"]))
