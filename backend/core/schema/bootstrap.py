"""Single schema-mutating startup path shared by container and tests."""

from __future__ import annotations

import hashlib
import json
import re
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
    acquire_schema_writer_lock,
)


RAW_REQUIRED_TABLES = frozenset(
    {
        "audit_actor_erasures",
        "spool_purchase_evidence",
        "cost_global_order_origins",
        "cost_global_origin_exclusions",
        "power_target_operations",
        "quiet_hours_deferred_deliveries",
        "timelapse_capture_tenancy",
        "alert_outbox",
        "production_recipes", "production_acceptances", "production_stock_additions",
        "inventory_repairs",
        "cost_identities",
        "cost_order_memberships",
        "cost_job_bindings",
        "cost_attempt_bindings",
        "cost_obligations",
        "cost_attempt_participation",
        "finance_currency_settings",

        "active_sessions",
        "ams_telemetry",
        "ams_read_generations",
        "ams_current_reads",
        "ams_report_packets",
        "ams_report_trays",
        "api_tokens",
        "biometric_tokens",
        "hms_error_history",
        "idempotency_keys",
        "education_audit_events",
        "education_cost_center_grants",
        "education_cost_center_printers",
        "education_cost_centers",
        "education_commands",
        "education_notification_outbox",
        "education_monitor_claims",
        "education_rate_counters",
        "education_storage_accounts",
        "education_submissions",
        "education_upload_operations",
        "classroom_connections",
        "classroom_oauth_states",
        "classroom_course_mappings",
        "classroom_roster_identities",
        "login_attempts",
        "mfa_enrollments",
        "model_revisions",
        "oidc_auth_codes",
        "oidc_config",
        "oidc_pending_states",
        "password_reset_tokens",
        "print_archives",
        "filament_delivery_outbox",
        "timelapse_retirements",
        "timelapse_ownership_anchors",
        "timelapse_retirement_objects",
        "timelapse_retirement_parents",
        "timelapse_retirement_quarantine",
        "timelapse_retirement_moves",
        "timelapse_retirement_unlinks",
        "timelapse_media_lifetimes",
        "timelapse_media_versions",
        "timelapse_frame_namespaces",
        "timelapse_frame_bindings",
        "timelapse_publications",
        "timelapse_publication_facts",
        "timelapse_retained_versions",
        "timelapse_retirement_lifetime_bindings",
        "timelapse_retirement_members",
        "timelapse_retirement_container_receipts",
        "print_files",
        "print_jobs",
        "printer_profiles",
        "printer_profile_revisions",
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

# Current raw inventory is literal: historical package import must not execute
# a newer migration helper before selecting its closed source contract.
POWER_OPERATION_COLUMNS = tuple("operation_nonce request_nonce owner_nonce printer_id target_key config_key destination_key purpose physical_job_id scheduled_job_id observation_key actor_user_id auth_kind authority_key state revision created_at changed_at command_started_at release_pending_at completed_at outcome cleanup_reason".split())

RAW_REQUIRED_TABLES = RAW_REQUIRED_TABLES | frozenset({'education_memberships'})

RAW_REQUIRED_COLUMNS = {
    "inventory_repairs": frozenset("event_id spool_id actor_id action before_json after_json created_at".split()),
    "production_recipes": frozenset("id owner definition".split()),
    "production_acceptances": frozenset("id owner job_uid claim_token recipe_id request_hash definition".split()),
    "production_stock_additions": frozenset("id owner receipt_id destination request_body state attempts".split()),
    "power_target_operations": frozenset(POWER_OPERATION_COLUMNS),
    "cost_global_order_origins": frozenset("order_uid resource_kind owner_scope owner_key origin_nonce proof_kind recorded_at".split()),
    "cost_global_origin_exclusions": frozenset("order_uid resource_kind owner_scope owner_key".split()),
    "spool_purchase_evidence": frozenset("evidence_nonce spool_uid birth_nonce resource_kind owner_scope owner_key revision currency price_decimal net_weight_g_decimal weight_basis provenance recorded_at".split()),
    "audit_actor_erasures": frozenset({"user_id", "erased_at"}),
    "audit_logs": frozenset({"actor_user_id", "actor_username", "actor_auth_kind"}),
    "cost_identities": frozenset("identity_uid resource_kind resource_id owner_scope owner_key".split()),
    "cost_order_memberships": frozenset("child_uid child_kind order_uid order_kind owner_scope owner_key".split()),
    "cost_job_bindings": frozenset("job_uid job_kind order_uid item_uid item_kind owner_scope owner_key".split()),
    "cost_attempt_bindings": frozenset("attempt_uid attempt_kind job_uid order_uid item_uid owner_scope owner_key".split()),
    "cost_obligations": frozenset("obligation_uid order_uid order_kind item_uid item_kind owner_scope owner_key source_uid source_kind allocation_key obligation_kind required_quantity evidence_state evidence_reason".split()),
    "cost_attempt_participation": frozenset("attempt_uid attempt_kind obligation_uid owner_scope owner_key state reason".split()),
    "finance_currency_settings": frozenset("owner_scope owner_key currency".split()),
    # Post-027 032: on its own MetaData (core/quiet_hours_deferral_schema.py), so listed here
    # for validation and the backup inventory.
    "timelapse_capture_tenancy": frozenset("operation_id captured_single_tenant created_at".split()),
    "alert_outbox": frozenset("id job_id alert_kind actor_user_id attempts created_at".split()),
    "quiet_hours_deferred_deliveries": frozenset("id user_id org_id scope_key channel alert_type severity title message printer_id job_id spool_id state outcome_reason created_at delivered_at".split()),

    "ams_read_generations": frozenset("generation_id printer_id owner_org_id configuration_key runtime_incarnation started_at state completed_at invalidated_at".split()),
    "ams_current_reads": frozenset("printer_id generation_id runtime_incarnation".split()),
    "ams_report_packets": frozenset("packet_sequence packet_id generation_id received_at receipt_provenance".split()),
    "ams_report_trays": frozenset("packet_sequence slot_number unit_id tray_id material material_state mechanically_ready".split()),
    "timelapse_ownership_anchors": frozenset("operation_id timelapse_id printer_id scheduled_job_id filename owner_state owner_org_id ownership_source print_file_id education_submission_id capture_printer_org_id capture_printer_shared created_at".split()),
    "timelapse_retirements": frozenset("operation_id timelapse_id printer_id owner_state owner_org_id filename root_device root_inode state revision reason_code created_at completed_at".split()),
    "timelapse_retirement_objects": frozenset("operation_id object_index relative_path kind device inode uid gid mode link_count byte_count mtime_ns ctime_ns state quarantine_name reason_code".split()),
    "timelapse_retirement_parents": frozenset("operation_id relative_path device inode uid gid mode".split()),
    "timelapse_retirement_quarantine": frozenset("operation_id device inode uid gid mode".split()),
    "timelapse_retirement_unlinks": frozenset("operation_id object_index phase".split()),
    "timelapse_media_lifetimes": frozenset("lifetime_nonce original_timelapse_id original_printer_id root_device root_inode root_uid root_gid root_mode created_at".split()),
    "timelapse_media_versions": frozenset("version_nonce lifetime_nonce".split()),
    "timelapse_frame_namespaces": frozenset("namespace_id lifetime_nonce relative_path phase device inode uid gid mode created_at".split()),
    "timelapse_frame_bindings": frozenset("lifetime_nonce namespace_id next_number".split()),
    "timelapse_publications": frozenset("operation_id source_nonce source_lifetime recipe_version encode_fps speed_numerator speed_denominator trim_start trim_end kind source_timelapse_id source_printer_id source_filename source_status source_preclaim_status actor_id frame_namespace_id frame_number payload_byte_count payload_digest_sha256 private_directory private_name destination_path result_nonce result_lifetime result_tid phase state revision reason_code published_ctime_ns created_at completed_at".split()),
    "timelapse_publication_facts": frozenset("operation_id role relative_path kind device inode uid gid mode link_count byte_count mtime_ns ctime_ns digest_sha256".split()),
    "timelapse_retained_versions": frozenset("version_nonce lifetime_nonce retained_by_operation filename state retire_operation_id".split()),
    "timelapse_retirement_lifetime_bindings": frozenset("operation_id version_nonce lifetime_nonce phase object_count created_at sealed_at".split()),
    "timelapse_retirement_members": frozenset("operation_id object_index role version_nonce retained_version_nonce namespace_id publication_id fact_role fact_path".split()),
    "timelapse_retirement_container_receipts": frozenset("operation_id object_index role phase completed_at".split()),
    "timelapse_retirement_moves": frozenset("operation_id object_index kind digest_sha256 phase verified_ctime_ns".split()),
    "active_sessions": frozenset("id user_id token_jti ip_address user_agent created_at last_seen_at".split()),
    "ams_telemetry": frozenset("id printer_id ams_unit humidity temperature recorded_at".split()),
    "api_tokens": frozenset("id user_id name token_hash token_prefix scopes expires_at last_used_at created_at".split()),
    "groups": frozenset("id name description owner_id created_at updated_at is_org branding_json settings_json".split()),
    "hms_error_history": frozenset("id printer_id code message severity source occurred_at".split()),
    "idempotency_keys": frozenset("key user_id method path request_hash auth_fingerprint state response_status response_body response_media_type created_at updated_at".split()),
    "mfa_enrollments": frozenset("jti user_id expires_at state encrypted_pending_secret created_at consumed_at".split()),
    "login_attempts": frozenset("id ip username attempted_at success".split()),
    "model_revisions": frozenset("id model_id revision_number file_path changelog uploaded_by created_at".split()),
    "oidc_auth_codes": frozenset("code access_token expires_at".split()),
    "oidc_config": frozenset("id display_name client_id client_secret_encrypted tenant_id discovery_url scopes auto_create_users default_role default_group_id provider_type allowed_domains is_enabled created_at updated_at".split()),
    "oidc_pending_states": frozenset("state expires_at nonce".split()),
    "password_reset_tokens": frozenset("id user_id token expires_at used".split()),
    "print_archives": frozenset("id job_id print_job_id printer_id user_id print_name status started_at completed_at actual_duration_seconds filament_used_grams cost_estimate thumbnail_b64 file_path notes created_at tags plate_count plate_thumbnails print_file_id project_id energy_kwh energy_cost consumption_json file_hash".split()),
    "print_files": frozenset("id filename original_filename project_name print_time_seconds total_weight_grams layer_count layer_height nozzle_diameter printer_model supports_used bed_type filaments_json thumbnail_b64 mesh_data model_id job_id uploaded_at stored_path filament_weight_grams bed_x_mm bed_y_mm compatible_api_types file_hash plate_count org_id created_by storage_bytes blob_state compatibility_facts_json".split()),
    "print_jobs": frozenset("id printer_id job_id filename job_name started_at ended_at status progress_percent remaining_minutes total_layers current_layer bed_temp_target nozzle_temp_target filament_slots error_code scheduled_job_id created_at model_revision_id".split()),
    "printer_profile_revisions": frozenset("id profile_id revision_number raw_content file_format slicer category changelog created_by created_at".split()),
    "printer_profiles": frozenset("id created_by printer_id org_id name description slicer category file_format filament_type raw_content is_shared is_default tags last_applied_at last_applied_printer_id created_at updated_at".split()),
    "printer_telemetry": frozenset("id printer_id bed_temp nozzle_temp bed_target nozzle_target fan_speed recorded_at".split()),
    "projects": frozenset("id created_by org_id name description color status expected_parts created_at updated_at".split()),
    "quiet_hours_digest_sends": frozenset("id user_id org_id window_ended_at sent_at delivery_status scope_key".split()),
    "quiet_hours_org_digest_sends": frozenset("id org_id window_ended_at sent_at delivery_status scope_key".split()),
    "quota_usage": frozenset("id user_id period_key grams_used hours_used jobs_used updated_at".split()),
    "report_schedules": frozenset("id name report_type frequency recipients filters is_active next_run_at last_run_at created_by created_at".split()),
    "token_blacklist": frozenset("jti expires_at".split()),
    "users": frozenset("id username email password_hash role is_active last_login created_at oidc_subject oidc_provider oidc_issuer mfa_enabled mfa_secret quota_grams quota_hours quota_jobs quota_period theme_json group_id".split()),
    "webhooks": frozenset("id name url webhook_type alert_types is_enabled created_at updated_at".split()),
    "ws_events": frozenset("id event_type data created_at".split()),
    "education_cost_centers": frozenset("id org_id name_key code_key display_name code description state revision created_by created_at updated_at".split()),
    "education_cost_center_grants": frozenset("id org_id cost_center_id user_id role state granted_by granted_at revoked_by revoked_at".split()),
    "education_cost_center_printers": frozenset("id org_id cost_center_id printer_id state granted_by granted_at revoked_by revoked_at".split()),
    "education_commands": frozenset("org_id actor_kind actor_id action command_id request_hash state result_json created_at completed_at".split()),
    "education_upload_operations": frozenset("operation_id org_id user_id state reserved_bytes accounted_bytes expected_bytes expected_hash staging_path final_path cleanup_path reservation_released_at purge_requested_at purged_at lease_owner lease_expires_at created_at updated_at".split()),
    "education_submissions": frozenset("id org_id operation_id job_id print_file_id model_id cost_center_id submitted_by approved_printer_id approved_by status lifecycle_revision compatibility_engine_version created_at updated_at".split()),
    "education_audit_events": frozenset("event_id org_id actor_kind actor_id action command_id request_hash resource_type resource_id cost_center_id lifecycle_revision details_json result_json created_at".split()),
    "education_notification_outbox": frozenset("id event_id org_id recipient_user_id state available_at lease_owner lease_expires_at attempt_count last_error delivered_at created_at".split()),
    "education_monitor_claims": frozenset("claim_id org_id submission_id job_id printer_id authority_revision token_digest state created_at updated_at".split()),
    "education_rate_counters": frozenset("org_id scope_kind scope_id user_id bucket_kind bucket_start attempt_count updated_at".split()),
    "education_storage_accounts": frozenset("org_id scope_kind scope_id user_id reserved_bytes accounted_bytes revision updated_at".split()),
    "classroom_connections": frozenset("org_id client_id client_secret_encrypted allowed_domains account_subject account_email granted_scopes refresh_token_encrypted access_token_encrypted access_token_expires_at state last_success_at last_error_code created_by created_at updated_at".split()),
    "classroom_oauth_states": frozenset("state org_id admin_id code_verifier_encrypted redirect_uri expires_at created_at".split()),
    "classroom_course_mappings": frozenset("id org_id provider_course_id cost_center_id course_name course_section course_state last_imported_at created_at updated_at".split()),
    "classroom_roster_identities": frozenset("id org_id user_id provider_user_id normalized_email state last_seen_at created_at updated_at".split()),
}

# This migration references the raw-SQL Education schema created by the
# dedicated tenant-integrity migration below. Keep its stable migration ID,
# but apply it only after that dependency exists. Existing SQLite ledgers that
# already recorded the file remain idempotent.
POST_EDUCATION_SQL_MIGRATIONS = frozenset(
    {"modules/organizations/migrations/003_google_classroom.sql"}
)


for _economic_table in ('orders', 'order_items', 'jobs'):
    RAW_REQUIRED_COLUMNS[_economic_table] = RAW_REQUIRED_COLUMNS.get(_economic_table, frozenset()) | frozenset({'economic_uid'})
for _source_table in ('spools', 'print_jobs'):
    RAW_REQUIRED_COLUMNS[_source_table] = RAW_REQUIRED_COLUMNS.get(_source_table, frozenset()) | frozenset({'economic_uid', 'economic_birth_nonce'})
RAW_REQUIRED_COLUMNS['education_memberships'] = frozenset({'user_id','org_id','retained_at','ended_at'})
RAW_REQUIRED_COLUMNS['orders'] |= frozenset({'economic_global_origin_nonce'})
RAW_REQUIRED_COLUMNS['spools'] |= frozenset({'purchase_evidence_nonce','purchase_revision'})
RAW_REQUIRED_COLUMNS['cost_source_lifetimes'] = frozenset({'birth_nonce','resource_kind','resource_id','state','identity_uid','owner_scope','owner_key','job_uid','order_uid','item_uid','attempt_uid'})


def import_all_models() -> None:
    import core.models  # noqa: F401
    import core.dispatch_claim_schema  # noqa: F401
    import core.dispatch_receipt_schema  # noqa: F401
    import core.dispatch_sender_schema  # noqa: F401
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
            migration_id = path.relative_to(backend_root).as_posix()
            if migration_id not in POST_EDUCATION_SQL_MIGRATIONS:
                files.append((migration_id, path))
    return files


def post_education_migration_files(backend_root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for migration_id in sorted(POST_EDUCATION_SQL_MIGRATIONS):
        path = backend_root / migration_id
        if not path.is_file():
            raise RuntimeError(f"Required post-Education migration is missing: {migration_id}")
        files.append((migration_id, path))
    return files


def _orm_manifest() -> dict[str, list[str]]:
    return {
        table.name: sorted(column.name for column in table.columns)
        for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name)
    }


def _orm_checksum() -> str:
    payload = json.dumps(_orm_manifest(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


CURRENT_ORM_BASELINE = "main_audit_20261012"

# Post-027 migrations, applied in this order on the current path only. Each must be
# additive and must never fail because of existing user data (data-dependent
# invariants belong in the application layer). Historical PostgreSQL archives are
# staged by core/schema/preserved029 and then upgraded through this list.
# Procedure: docs/SCHEMA_MIGRATIONS.md.
POST027_MIGRATIONS = (
    ("python:032-quiet-hours-deferred-deliveries", "core.schema.migrations.032_quiet_hours_deferred_deliveries"),
    ("python:033-timelapse-capture-tenancy", "core.schema.migrations.033_timelapse_capture_tenancy"),
    ("python:034-alert-outbox", "core.schema.migrations.034_alert_outbox"),
    ("python:035-production-acceptance", "core.schema.migrations.035_production_acceptance"),
    ("python:036-ctec-pilot", "core.schema.migrations.036_ctec_pilot"),
    ("python:037-education-memberships", "core.schema.migrations.037_education_memberships"),
)


def _recognized_orm_checksums() -> frozenset[str]:
    """Explicit build manifests, never inferred from the database being upgraded."""
    from core.schema.migrator import MigrationError

    catalog = json.loads(Path(__file__).with_name("orm_baselines.json").read_text())
    checksums = set()
    for entry in catalog.values():
        payload = json.dumps(entry["manifest"], sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if checksum != entry["checksum"]:
            raise MigrationError("Pinned ORM baseline catalog checksum mismatch")
        checksums.add(checksum)
    return frozenset(checksums)


def _validate_orm_baseline(connection) -> None:
    """Refuse unknown build/history before any schema-mutating bootstrap phase."""
    from core.schema.migrator import MigrationError

    recognized = _recognized_orm_checksums()
    catalog = json.loads(Path(__file__).with_name("orm_baselines.json").read_text())
    if _orm_checksum() != catalog[CURRENT_ORM_BASELINE]["checksum"]:
        raise MigrationError("Current ORM manifest is not pinned for this build")
    if MIGRATION_TABLE.name not in inspect(connection).get_table_names():
        return
    row = connection.execute(
        text("SELECT checksum, dialect FROM odin_schema_migrations "
             "WHERE migration_id = 'orm-baseline:1'")
    ).fetchone()
    if row and (str(row[0]) not in recognized or str(row[1]) != connection.dialect.name):
        raise MigrationError("Migration history mismatch: orm-baseline:1")


def _record_orm_baseline(connection) -> bool:
    _validate_orm_baseline(connection)
    row = connection.execute(
        text("SELECT migration_id FROM odin_schema_migrations "
             "WHERE migration_id = 'orm-baseline:1'")
    ).fetchone()
    if row:
        return False
    connection.execute(
        MIGRATION_TABLE.insert().values(
            migration_id="orm-baseline:1",
            checksum=_orm_checksum(),
            dialect=connection.dialect.name,
        )
    )
    return True


def validate_schema(connection, *, _contract=None) -> dict[str, int]:
    from core.schema.pre026_restore_contract import check_contract,inventory_for
    check_contract(_contract)
    INVENTORY = inventory_for(_contract) if _contract is not None else None
    raw_tables=RAW_REQUIRED_TABLES if _contract is None else frozenset(INVENTORY["raw_tables"])
    raw_columns=RAW_REQUIRED_COLUMNS if _contract is None else INVENTORY["raw_columns"]
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    required_tables = (
        set(Base.metadata.tables) | set(raw_tables) | {MIGRATION_TABLE.name}
    )
    missing_tables = sorted(required_tables - actual_tables)
    if missing_tables:
        raise RuntimeError("Schema validation missing tables: " + ", ".join(missing_tables))
    for table_name, required_columns in raw_columns.items():
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
    from core.schema.mfa_enrollments import validate_enrollment_shape
    validate_enrollment_shape(connection)
    return {"tables": len(actual_tables), "required_tables": len(required_tables)}


def _normalize_check_definition(value: str, dialect_name: str) -> str:
    normalized = " ".join(str(value or "").split())
    if dialect_name != "postgresql":
        return normalized
    normalized = re.sub(
        r"::character varying::text\b", "::character varying", normalized
    )
    return re.sub(r"(ARRAY\[[^]]*\])::text\[\]", r"\1", normalized)


def schema_manifest(connection) -> dict[str, object]:
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
                (
                    item.get("name") or "",
                    _normalize_check_definition(
                        item.get("sqltext") or "", connection.dialect.name
                    ),
                )
                for item in inspector.get_check_constraints(table_name)
            ),
        }
    return manifest


def schema_fingerprint(connection) -> str:
    payload = json.dumps(
        schema_manifest(connection), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_adoptable_legacy_core(connection) -> None:
    """Reject malformed lookalike schemas before creating a migration ledger."""
    inspector = inspect(connection)
    if "users" not in inspector.get_table_names():
        return
    required = {"id", "username", "password_hash", "role", "is_active"}
    actual = {item["name"] for item in inspector.get_columns("users")}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            "Schema validation missing columns on users: " + ", ".join(missing)
        )


def _bootstrap_database_contract(
    engine: Engine,
    backend_root: Path | None = None,
    *, contract=None,
) -> dict[str, object]:
    from core.schema.pre026_restore_contract import check_contract,fixed_sql_files,PRE027_RESTORE026
    check_contract(contract)
    import_all_models()
    root = backend_root or Path(__file__).resolve().parents[2]
    applied: list[str] = []
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            acquire_schema_writer_lock(connection)
        _validate_orm_baseline(connection)
        _validate_adoptable_legacy_core(connection)
        Base.metadata.create_all(bind=connection)
        ensure_ledger(connection)
        for migration_id, path in (migration_files(root) if contract is None else fixed_sql_files(root,contract=contract)):
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
            acquire_schema_writer_lock(connection)
        for migration_id, path in (post_education_migration_files(root) if contract is None else fixed_sql_files(root,post=True,contract=contract)):
            if apply_migration_file(connection, path, migration_id=migration_id):
                applied.append(migration_id)

    monitor_migration = "core.schema.migrations.003_education_monitor_claims"
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            acquire_schema_writer_lock(connection)
        if apply_python_migration(connection, monitor_migration):
            applied.append("python:003-education-monitor-claims")

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            acquire_schema_writer_lock(connection)
        if apply_python_migration(connection, "core.schema.migrations.004_quiet_hours_scoped_claims"):
            applied.append("python:004-quiet-hours-scoped-claims")
        if apply_python_migration(connection, "core.schema.migrations.005_order_production_snapshots"):
            applied.append("python:005-order-production-snapshots")
        if apply_python_migration(connection, "core.schema.migrations.006_print_job_measurements"):
            applied.append("python:006-print-job-measurements")
        if apply_python_migration(connection, "core.schema.migrations.007_attempt_accounting"):
            applied.append("python:007-attempt-accounting")
        if apply_python_migration(connection, "core.schema.migrations.008_profile_revisions"):
            applied.append("python:008-profile-revisions")
        if apply_python_migration(connection, "core.schema.migrations.009_cost_foundation"):
            applied.append("python:009-cost-foundation")
        if apply_python_migration(connection, "core.schema.migrations.010_h2d_nozzle_observations"):
            applied.append("python:010-h2d-nozzle-observations")
        if apply_python_migration(connection, "core.schema.migrations.011_dispatch_claims"):
            applied.append("python:011-dispatch-claims")
        if apply_python_migration(connection, "core.schema.migrations.012_dispatch_receipts"):
            applied.append("python:012-dispatch-receipts")
        if apply_python_migration(connection, "core.schema.migrations.013_observation_received_time"):
            applied.append("python:013-observation-received-time")
        if apply_python_migration(connection, "core.schema.migrations.014_dispatch_senders"):
            applied.append("python:014-dispatch-senders")
        if apply_python_migration(connection, "core.schema.migrations.015_timelapse_retirements"):
            applied.append("python:015-timelapse-retirements")
        if apply_python_migration(connection, "core.schema.migrations.016_timelapse_ownership"):
            applied.append("python:016-timelapse-ownership")
        if apply_python_migration(connection, "core.schema.migrations.017_timelapse_recovery"):
            applied.append("python:017-timelapse-recovery")
        if apply_python_migration(connection, "core.schema.migrations.018_timelapse_unlinks"):
            applied.append("python:018-timelapse-unlinks")
        if apply_python_migration(connection, "core.schema.migrations.019_timelapse_publications"):
            applied.append("python:019-timelapse-publications")
        if apply_python_migration(connection, "core.schema.migrations.020_ams_evidence"):
            applied.append("python:020-ams-evidence")
        if apply_python_migration(connection, "core.schema.migrations.021_economic_identity"):
            applied.append("python:021-economic-identity")
        if apply_python_migration(connection, "core.schema.migrations.022_economic_source_lifetimes"):
            applied.append("python:022-economic-source-lifetimes")
        if apply_python_migration(connection, "core.schema.migrations.023_timelapse_retirement_membership"):
            applied.append("python:023-timelapse-retirement-membership")
        if apply_python_migration(connection, "core.schema.migrations.024_audit_actor"):
            applied.append("python:024-audit-actor")
        if apply_python_migration(connection, "core.schema.migrations.025_spool_purchase_evidence"):
            applied.append("python:025-spool-purchase-evidence")
        if (contract is None or contract is PRE027_RESTORE026) and apply_python_migration(connection, "core.schema.migrations.026_global_order_origins"):
            applied.append("python:026-global-order-origins")
        if contract is None and apply_python_migration(connection, "core.schema.migrations.027_power_target_operations"):
            applied.append("python:027-power-target-operations")
        if contract is None:
            for migration_id, module_name in POST027_MIGRATIONS:
                if migration_id == "python:037-education-memberships":
                    continue
                if apply_python_migration(connection, module_name):
                    applied.append(migration_id)

    if contract is None:
        if apply_dedicated_python_migration(engine, "core.schema.migrations.037_education_memberships"):
            applied.append("python:037-education-memberships")

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            acquire_schema_writer_lock(connection)
        if _record_orm_baseline(connection):
            applied.append("orm-baseline:1")
        if contract is None:
            from importlib import import_module
            import_module('core.schema.migrations.036_ctec_pilot').retry_spool_index(connection)
            from modules.inventory.slot_material_repair import repair_slot_materials
            repair_slot_materials(connection)
        validation = validate_schema(connection, _contract=contract)
        fingerprint = schema_fingerprint(connection)
    return {
        "dialect": engine.dialect.name,
        "applied": applied,
        "schema_fingerprint": fingerprint,
        **validation,
    }


def bootstrap_database(engine: Engine, backend_root: Path | None = None) -> dict[str, object]:
    """Ordinary startup always uses the current schema contract."""
    return _bootstrap_database_contract(engine,backend_root)
