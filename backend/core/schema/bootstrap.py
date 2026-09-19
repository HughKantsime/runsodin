"""Single schema-mutating startup path shared by container and tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from core.base import Base
from core.schema.migrator import (
    MIGRATION_TABLE,
    apply_dedicated_python_migration,
    apply_migration_file,
    apply_python_migration,
    ensure_ledger,
)


RAW_REQUIRED_TABLES = frozenset(
    {
        "active_sessions",
        "ams_telemetry",
        "api_tokens",
        "biometric_tokens",
        "hms_error_history",
        "idempotency_keys",
        "education_audit_events",
        "education_cost_center_grants",
        "education_cost_center_printers",
        "education_cost_centers",
        "education_notification_outbox",
        "education_rate_counters",
        "education_storage_accounts",
        "education_submissions",
        "education_upload_operations",
        "login_attempts",
        "model_revisions",
        "oidc_auth_codes",
        "oidc_config",
        "oidc_pending_states",
        "password_reset_tokens",
        "print_archives",
        "print_files",
        "print_jobs",
        "printer_profiles",
        "printer_telemetry",
        "projects",
        "push_devices",
        "quiet_hours_digest_sends",
        "quiet_hours_org_digest_sends",
        "quota_usage",
        "report_schedules",
        "token_blacklist",
        "users",
        "webhooks",
        "ws_events",
    }
)

RAW_REQUIRED_COLUMNS = {
    "active_sessions": frozenset("id user_id token_jti ip_address user_agent created_at last_seen_at".split()),
    "ams_telemetry": frozenset("id printer_id ams_unit humidity temperature recorded_at".split()),
    "api_tokens": frozenset("id user_id name token_hash token_prefix scopes expires_at last_used_at created_at".split()),
    "groups": frozenset("id name description owner_id created_at updated_at is_org branding_json settings_json".split()),
    "hms_error_history": frozenset("id printer_id code message severity source occurred_at".split()),
    "idempotency_keys": frozenset("key user_id method path request_hash auth_fingerprint state response_status response_body response_media_type created_at updated_at".split()),
    "login_attempts": frozenset("id ip username attempted_at success".split()),
    "model_revisions": frozenset("id model_id revision_number file_path changelog uploaded_by created_at".split()),
    "oidc_auth_codes": frozenset("code access_token expires_at".split()),
    "oidc_config": frozenset("id display_name client_id client_secret_encrypted tenant_id discovery_url scopes auto_create_users default_role default_group_id is_enabled created_at updated_at".split()),
    "oidc_pending_states": frozenset("state expires_at".split()),
    "password_reset_tokens": frozenset("id user_id token expires_at used".split()),
    "print_archives": frozenset("id job_id print_job_id printer_id user_id print_name status started_at completed_at actual_duration_seconds filament_used_grams cost_estimate thumbnail_b64 file_path notes created_at tags plate_count plate_thumbnails print_file_id project_id energy_kwh energy_cost consumption_json file_hash".split()),
    "print_files": frozenset("id filename original_filename project_name print_time_seconds total_weight_grams layer_count layer_height nozzle_diameter printer_model supports_used bed_type filaments_json thumbnail_b64 mesh_data model_id job_id uploaded_at stored_path filament_weight_grams bed_x_mm bed_y_mm compatible_api_types file_hash plate_count org_id created_by storage_bytes blob_state compatibility_facts_json".split()),
    "print_jobs": frozenset("id printer_id job_id filename job_name started_at ended_at status progress_percent remaining_minutes total_layers current_layer bed_temp_target nozzle_temp_target filament_slots error_code scheduled_job_id created_at model_revision_id".split()),
    "printer_profiles": frozenset("id created_by printer_id org_id name description slicer category file_format filament_type raw_content is_shared is_default tags last_applied_at last_applied_printer_id created_at updated_at".split()),
    "printer_telemetry": frozenset("id printer_id bed_temp nozzle_temp bed_target nozzle_target fan_speed recorded_at".split()),
    "projects": frozenset("id created_by org_id name description color status expected_parts created_at updated_at".split()),
    "quiet_hours_digest_sends": frozenset("id user_id org_id window_ended_at sent_at delivery_status".split()),
    "quiet_hours_org_digest_sends": frozenset("id org_id window_ended_at sent_at delivery_status".split()),
    "quota_usage": frozenset("id user_id period_key grams_used hours_used jobs_used updated_at".split()),
    "report_schedules": frozenset("id name report_type frequency recipients filters is_active next_run_at last_run_at created_by created_at".split()),
    "token_blacklist": frozenset("jti expires_at".split()),
    "users": frozenset("id username email password_hash role is_active last_login created_at oidc_subject oidc_provider oidc_issuer mfa_enabled mfa_secret quota_grams quota_hours quota_jobs quota_period theme_json group_id".split()),
    "webhooks": frozenset("id name url webhook_type alert_types is_enabled created_at updated_at".split()),
    "ws_events": frozenset("id event_type data created_at".split()),
    "education_cost_centers": frozenset("id org_id name_key code_key display_name code description state revision created_by created_at updated_at".split()),
    "education_cost_center_grants": frozenset("id org_id cost_center_id user_id role state granted_by granted_at revoked_by revoked_at".split()),
    "education_cost_center_printers": frozenset("id org_id cost_center_id printer_id state granted_by granted_at revoked_by revoked_at".split()),
    "education_upload_operations": frozenset("operation_id org_id user_id state reserved_bytes accounted_bytes expected_bytes expected_hash staging_path final_path cleanup_path reservation_released_at purge_requested_at purged_at lease_owner lease_expires_at created_at updated_at".split()),
    "education_submissions": frozenset("id org_id operation_id job_id print_file_id model_id cost_center_id submitted_by approved_printer_id approved_by status lifecycle_revision compatibility_engine_version created_at updated_at".split()),
    "education_audit_events": frozenset("event_id org_id actor_kind actor_id action command_id request_hash resource_type resource_id cost_center_id lifecycle_revision details_json result_json created_at".split()),
    "education_notification_outbox": frozenset("id event_id org_id recipient_user_id state available_at lease_owner lease_expires_at attempt_count last_error delivered_at created_at".split()),
    "education_rate_counters": frozenset("org_id scope_kind scope_id bucket_kind bucket_start attempt_count updated_at".split()),
    "education_storage_accounts": frozenset("org_id scope_kind scope_id reserved_bytes accounted_bytes revision updated_at".split()),
}


def import_all_models() -> None:
    import core.models  # noqa: F401
    import modules.archives.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.jobs.models  # noqa: F401
    import modules.models_library.models  # noqa: F401
    import modules.notifications.models  # noqa: F401
    import modules.orders.models  # noqa: F401
    import modules.organizations.branding  # noqa: F401
    import modules.printers.models  # noqa: F401
    import modules.push.models  # noqa: F401
    import modules.system.models  # noqa: F401
    import modules.vision.models  # noqa: F401


def migration_files(backend_root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    core_dir = backend_root / "core" / "migrations"
    for path in sorted(core_dir.glob("*.sql")):
        files.append((path.relative_to(backend_root).as_posix(), path))
    modules_dir = backend_root / "modules"
    for module_dir in sorted(modules_dir.iterdir()):
        migration_dir = module_dir / "migrations"
        if not migration_dir.is_dir():
            continue
        for path in sorted(migration_dir.glob("*.sql")):
            files.append((path.relative_to(backend_root).as_posix(), path))
    return files


def _orm_manifest() -> dict[str, list[str]]:
    return {
        table.name: sorted(column.name for column in table.columns)
        for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name)
    }


def _orm_checksum() -> str:
    payload = json.dumps(_orm_manifest(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_orm_baseline(connection) -> bool:
    migration_id = "orm-baseline:1"
    checksum = _orm_checksum()
    row = connection.execute(
        text(
            "SELECT checksum, dialect FROM odin_schema_migrations "
            "WHERE migration_id = :migration_id"
        ),
        {"migration_id": migration_id},
    ).fetchone()
    expected = (checksum, connection.dialect.name)
    if row:
        if (str(row[0]), str(row[1])) != expected:
            from core.schema.migrator import MigrationError

            raise MigrationError(f"Migration history mismatch: {migration_id}")
        return False
    connection.execute(
        MIGRATION_TABLE.insert().values(
            migration_id=migration_id,
            checksum=checksum,
            dialect=connection.dialect.name,
        )
    )
    return True


def validate_schema(connection) -> dict[str, int]:
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    required_tables = (
        set(Base.metadata.tables) | set(RAW_REQUIRED_TABLES) | {MIGRATION_TABLE.name}
    )
    missing_tables = sorted(required_tables - actual_tables)
    if missing_tables:
        raise RuntimeError("Schema validation missing tables: " + ", ".join(missing_tables))
    for table_name, required_columns in RAW_REQUIRED_COLUMNS.items():
        actual_columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            raise RuntimeError(
                f"Schema validation missing columns on {table_name}: "
                + ", ".join(missing_columns)
            )
    for table_name, columns in _orm_manifest().items():
        actual_columns = {
            item["name"] for item in inspect(connection).get_columns(table_name)
        }
        missing_columns = sorted(set(columns) - actual_columns)
        if missing_columns:
            raise RuntimeError(
                f"Schema validation missing columns on {table_name}: "
                + ", ".join(missing_columns)
            )
    return {"tables": len(actual_tables), "required_tables": len(required_tables)}


def schema_fingerprint(connection) -> str:
    inspector = inspect(connection)
    manifest: dict[str, object] = {}
    for table_name in sorted(inspector.get_table_names()):
        manifest[table_name] = {
            "columns": sorted(
                (
                    column["name"],
                    str(column["type"]),
                    bool(column.get("nullable", True)),
                    str(column.get("default")),
                )
                for column in inspector.get_columns(table_name)
            ),
            "indexes": sorted(
                (
                    index["name"] or "",
                    tuple(index.get("column_names") or ()),
                    bool(index.get("unique")),
                )
                for index in inspector.get_indexes(table_name)
            ),
            "foreign_keys": sorted(
                (
                    tuple(item.get("constrained_columns") or ()),
                    item.get("referred_table") or "",
                    tuple(item.get("referred_columns") or ()),
                )
                for item in inspector.get_foreign_keys(table_name)
            ),
            "checks": sorted(
                (item.get("name") or "", item.get("sqltext") or "")
                for item in inspector.get_check_constraints(table_name)
            ),
        }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bootstrap_database(
    engine: Engine,
    backend_root: Path | None = None,
) -> dict[str, object]:
    import_all_models()
    root = backend_root or Path(__file__).resolve().parents[2]
    applied: list[str] = []
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(71403114720480978)"))
        Base.metadata.create_all(bind=connection)
        ensure_ledger(connection)
        for migration_id, path in migration_files(root):
            if apply_migration_file(connection, path, migration_id=migration_id):
                applied.append(migration_id)
        python_migration = "core.schema.migrations.001_legacy_columns"
        if apply_python_migration(connection, python_migration):
            applied.append("python:001-legacy-columns")

    education_migration = "core.schema.migrations.002_education_tenant_integrity"
    if apply_dedicated_python_migration(engine, education_migration):
        applied.append("python:002-education-tenant-integrity")

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(71403114720480978)"))
        if _record_orm_baseline(connection):
            applied.append("orm-baseline:1")
        validation = validate_schema(connection)
        fingerprint = schema_fingerprint(connection)
    return {
        "dialect": engine.dialect.name,
        "applied": applied,
        "schema_fingerprint": fingerprint,
        **validation,
    }
