"""Live-tree twins of the bundle-only qualified-source tests' current-schema assertions.

test_pre026_restore_contract.py and test_historical026_profile_extension.py run against
the preserved029 bundle. These assertions check the same facts on the live tree, so
the current schema keeps independent coverage in-process.
"""
import pytest

from core.schema.pre026_restore_contract import INVENTORY, INVENTORY026
from modules.system import postgres_backup_service as backup
from modules.system.postgres_canonical_executables import canonical_functions, canonical_triggers


def _toc(tables):
    return "\n".join(f"{i+1}; 1 1 TABLE public {table} postgres" for i, table in enumerate(sorted(tables)))


def test_live_toc_accepts_the_current_tables_and_refuses_historical_inventories():
    current = backup.canonical_tables()
    assert backup.validate_archive_toc(_toc(current), allow_unresolved_structure=True)["table_count"] == len(current)
    for historical in (INVENTORY["tables"], INVENTORY026["tables"]):
        with pytest.raises(backup.BackupValidationError):
            backup.validate_archive_toc(_toc(historical))


def test_live_current_trigger_union_uses_table_name_keys_and_native_power_shape():
    from core import power_target_operation_schema as power
    triggers = canonical_triggers()
    assert all(type(key) is tuple and len(key) == 2 for key in triggers)
    for name, definition in power.pg_triggers().items():
        expected = definition.replace("INSERT OR UPDATE OR DELETE", "INSERT OR DELETE OR UPDATE").replace("EXECUTE FUNCTION public.", "EXECUTE FUNCTION ")
        assert triggers[(power.TABLE, name)] == (expected, power.FUNCTION, "public", "O")


def test_live_executable_counts():
    # Update together with the migration that adds a PostgreSQL function or trigger.
    functions, triggers = canonical_functions(), canonical_triggers()
    assert (len(functions), len(triggers)) == (21, 31)
    assert "power_target_operation_guard" in functions and "spool_purchase_evidence_guard" in functions
    assert all(value[6] == "" for value in functions.values())
