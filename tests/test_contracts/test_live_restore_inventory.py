"""Live-tree twins of the bundle-only qualified-source tests' current-schema assertions.

test_pre026_restore_contract.py and test_historical026_profile_extension.py run against
the preserved029 bundle. These assertions check the same facts on the live tree, so
the current schema keeps independent coverage in-process.
"""
import json

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
    assert (len(functions), len(triggers)) == (32, 45)
    assert "power_target_operation_guard" in functions and "spool_purchase_evidence_guard" in functions
    assert all(value[6] == "" for value in functions.values())


# Raw tables (and their exact columns) each registered post-027 migration adds. A new
# migration must extend this map; the test below fails until it does.
POST027_RAW_TABLES = {
    # These migrations add ORM columns only, without introducing raw tables.
    "python:041-inventory-color": {},
    "python:042-dispatch-capability": {},
    "python:040-job-foundation": {
        "job_summary_outbox": frozenset("print_attempt_uid attempt_birth job_id printer_id org_id outcome observed_at delivery_state".split()),
        "stock_snapshots": frozenset("owner_scope owner_key variation location quantity calculated_at read_at".split()),
        "job_timeline_events": frozenset("id job_id print_attempt_uid printer_id org_id kind code detail actor_user_id actor_token_id occurred_at recorded_at".split()),
        "timeline_writer_state": frozenset("process_id started_at clean_shutdown last_drained_at drop_epoch error_epoch printer_ids epoch_history".split()),
    },
    "python:039-measured-filament": {
        "terminal_filament_measurements": frozenset("observation_id attempt_uid attempt_birth owner_scope owner_key order_uid item_uid allocation_key outcome measurement_basis measured_grams reported_value reconciliation_reason snapshot_json created_at".split()),
        "filament_cost_events": frozenset("event_id observation_id revision expected_revision audit_reason spool_uid purchase_evidence_nonce currency amount_decimal reason calculation_version created_at".split()),
    },
    "python:038-dispatch-recovery": {
        "dispatch_recovery_history": frozenset("id created_at audit_id actor_id actor_identity owner_org_id charged_org_id printer_id job_id attempt_handle snapshot_handle decision reason_category facts".split()),
    },
    "python:037-education-memberships": {
        "education_memberships": frozenset("user_id org_id retained_at ended_at".split()),
    },
    "python:032-quiet-hours-deferred-deliveries": {
        "quiet_hours_deferred_deliveries": frozenset(
            "id user_id org_id scope_key channel alert_type severity title message printer_id job_id spool_id state "
            "outcome_reason created_at delivered_at".split()),
    },
    "python:033-timelapse-capture-tenancy": {
        "timelapse_capture_tenancy": frozenset("operation_id captured_single_tenant created_at".split()),
    },
    "python:034-alert-outbox": {
        "alert_outbox": frozenset("id job_id alert_kind actor_user_id attempts created_at".split()),
    },
    "python:035-production-acceptance": {
        "production_recipes": frozenset("id owner definition".split()),
        "production_acceptances": frozenset("id owner job_uid claim_token recipe_id request_hash definition".split()),
        "production_stock_additions": frozenset("id owner receipt_id destination request_body state attempts".split()),
    },
    "python:036-ctec-pilot": {
        "inventory_repairs": frozenset("event_id spool_id actor_id action before_json after_json created_at".split()),
    },
}


def test_live_inventory_is_preserved027_plus_exactly_the_registered_post027_tables():
    from core.schema.bootstrap import POST027_MIGRATIONS, RAW_REQUIRED_COLUMNS, RAW_REQUIRED_TABLES
    from tests.test_contracts.test_prepriced_restore_contract import _preserved027_inventory
    accepted = _preserved027_inventory()
    assert set(POST027_RAW_TABLES) == {migration_id for migration_id, _ in POST027_MIGRATIONS}
    added = {table: columns for tables in POST027_RAW_TABLES.values() for table, columns in tables.items()}
    assert sorted(RAW_REQUIRED_TABLES) == sorted(set(accepted["raw_tables"]) | set(added))
    live_columns = {table: sorted(columns) for table, columns in RAW_REQUIRED_COLUMNS.items()}
    expected_columns = {**accepted["raw_columns"], **{table: sorted(columns) for table, columns in added.items()}}
    assert live_columns == expected_columns
    # 037, 038 and 039 add exactly their source-pinned functions and triggers.
    import importlib
    retained=importlib.import_module('core.schema.migrations.037_education_memberships')
    recovery=importlib.import_module('core.schema.migrations.038_dispatch_recovery')
    added_functions = {**retained.pg_functions(), **recovery.pg_functions()}
    added_triggers = {**retained.pg_triggers(), **recovery.pg_triggers()}
    functions=canonical_functions();triggers=canonical_triggers()
    measured=importlib.import_module('core.schema.migrations.039_measured_filament')
    added_functions.update(measured.pg_functions())
    added_triggers.update(measured.pg_triggers())
    timeline=importlib.import_module("core.schema.migrations.040_job_foundation")
    added_functions.update(timeline.pg_functions())
    added_triggers.update(timeline.pg_triggers())
    assert set(functions)-set(accepted['functions']) == set(added_functions)
    assert json.loads(json.dumps({k:v for k,v in functions.items() if k not in added_functions})) == accepted["functions"]
    assert json.loads(json.dumps([[list(k), list(v)] for k, v in sorted(triggers.items()) if k not in added_triggers])) == accepted["triggers"]
    for key,ddl in retained.pg_triggers().items():assert triggers[key] == (ddl,key[1],'public','O')

    for key,ddl in recovery.pg_triggers().items():assert triggers[key] == (ddl,'odin_recovery_immutable','public','O')
