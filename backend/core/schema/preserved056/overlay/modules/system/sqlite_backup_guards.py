"""Source-owned SQLite restore schema and trigger guards."""
from core.schema.audit_integrity_version import MODULE as AUDIT_MIGRATION_MODULE, MIGRATION_ID as AUDIT_MIGRATION_ID
import importlib
import re

def _normalized_schema_sql(value: str | None) -> str:
    # SQL keywords may change case; literal and quoted-identifier contents may not.
    parts = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", str(value or ""))
    return "".join(part if index % 2 else re.sub(r"\s+", " ", part.lower())
                   for index, part in enumerate(parts)).strip()


_ALLOWED_SQLITE_TRIGGERS = {
    "trg_education_audit_no_update": _normalized_schema_sql(
        "CREATE TRIGGER trg_education_audit_no_update BEFORE UPDATE ON "
        "education_audit_events BEGIN SELECT RAISE(ABORT, "
        "'education audit events are immutable'); END"
    ),
    "trg_education_audit_no_delete": _normalized_schema_sql(
        "CREATE TRIGGER trg_education_audit_no_delete BEFORE DELETE ON "
        "education_audit_events BEGIN SELECT RAISE(ABORT, "
        "'education audit events are immutable'); END"
    ),
}


# Closed packaged producers; archive SQL is never a source of authority.
_LATE_TRIGGER_MODULES = (
    '021_economic_identity', '022_economic_source_lifetimes',
    '023_timelapse_retirement_membership', '024_audit_actor',
    '025_spool_purchase_evidence', '026_global_order_origins',
    '027_power_target_operations', '037_education_memberships', '038_dispatch_recovery', '039_measured_filament', '040_job_foundation', '049_education_classes', '055_controlled_history', '056_dispatch_recovery_global',
)


def _canonical_sqlite_triggers():
    from modules.system.backup_service import BackupValidationError
    allowed = {name: ("education_audit_events", definition)
               for name, definition in _ALLOWED_SQLITE_TRIGGERS.items()}
    def add(name, table, definition):
        if name in allowed:
            raise ValueError('Duplicate packaged trigger')
        allowed[name] = (table, _normalized_schema_sql(definition))
    try:
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- AUDIT_MIGRATION_MODULE is derived solely from the literal MIGRATION_ID in core.schema.audit_integrity_version
        integrity = importlib.import_module(AUDIT_MIGRATION_MODULE)
        add("odin_audit_append", "audit_logs", integrity.sqlite_trigger())
        contract = importlib.import_module("core.schema.migrations.009_cost_foundation")
        contract._helper_integrity()
        for kind, name, table, definition in contract.EXPECTED_SQLITE:
            if kind == "trigger":
                add(name, table, definition)
        for suffix in _LATE_TRIGGER_MODULES:
            module = importlib.import_module('core.schema.migrations.' + suffix)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- verified safe — suffix iterates the fixed _LATE_TRIGGER_MODULES tuple, not input
            if suffix.startswith('021_'):
                definitions = {}
                for table in module.TABLES:
                    for name, sql in module._sqlite_triggers(table).items():
                        if name in definitions:
                            raise ValueError('Duplicate packaged trigger')
                        definitions[name] = sql
            elif suffix.startswith('055_'):
                definitions = module.job_id_triggers()
            elif suffix.startswith(('037_', '038_', '039_', '040_', '049_', '056_')):
                definitions = module.sqlite_triggers()
            elif suffix.startswith('022_'):
                definitions = module._sqlite_triggers()
            else:
                module._source()
                if suffix.startswith('023_'):
                    definitions = module.sqlite_triggers()
                elif suffix.startswith('024_'):
                    definitions = module.schema.trigger_sql(False)
                else:
                    definitions = module.schema.sqlite_triggers()
            for name, definition in definitions.items():
                # Parse only the closed trusted producer's simple header.
                header = re.match(r'CREATE\s+TRIGGER\s+([a-z_][a-z0-9_]*)\s+BEFORE\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+([a-z_][a-z0-9_]*)\b', definition, re.I)
                after_header = re.match(r'CREATE\s+TRIGGER\s+([a-z_][a-z0-9_]*)\s+AFTER\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+([a-z_][a-z0-9_]*)\b', definition, re.I)
                header = header or after_header
                if not header or header[1] != name:
                    raise ValueError('Packaged trigger header differs')
                add(name, header[2], definition)
    except Exception:
        raise BackupValidationError("Packaged backup trigger contract is unavailable") from None
    return allowed


# Minimum columns and recursive dependencies are literal reviewed contracts.
_COST_COLUMNS = {
    'cost_identities': 'identity_uid resource_kind resource_id owner_scope owner_key',
    'cost_order_memberships': 'child_uid child_kind order_uid order_kind owner_scope owner_key',
    'cost_job_bindings': 'job_uid job_kind order_uid item_uid item_kind owner_scope owner_key',
    'cost_attempt_bindings': 'attempt_uid attempt_kind job_uid order_uid item_uid owner_scope owner_key',
    'cost_obligations': 'obligation_uid order_uid order_kind item_uid item_kind owner_scope owner_key source_uid source_kind allocation_key obligation_kind required_quantity evidence_state evidence_reason',
    'cost_attempt_participation': 'attempt_uid attempt_kind obligation_uid owner_scope owner_key state reason',
    'finance_currency_settings': 'owner_scope owner_key currency',
}
_COST_DEPENDENCIES = {
    'cost_order_memberships': ('cost_identities',),
    'cost_job_bindings': ('cost_order_memberships',),
    'cost_attempt_bindings': ('cost_job_bindings','cost_identities'),
    'cost_obligations': ('cost_order_memberships',),
    'cost_attempt_participation': ('cost_attempt_bindings','cost_obligations'),
}
_SOURCE_COLUMNS = {
    'orders': 'id economic_uid org_id',
    'order_items': 'id economic_uid order_id',
    'jobs': 'id economic_uid order_item_id charged_to_org_id',
    'spools': 'id economic_uid economic_birth_nonce org_id',
    'print_jobs': 'id economic_uid economic_birth_nonce scheduled_job_id printer_id spool_snapshot',
    'cost_source_lifetimes': 'birth_nonce resource_kind resource_id state identity_uid owner_scope owner_key job_uid order_uid item_uid attempt_uid',
}

_MEMBERSHIP_COLUMNS = {'timelapse_retirement_lifetime_bindings': 'operation_id version_nonce lifetime_nonce phase object_count '
                                           'created_at sealed_at',
 'timelapse_retirement_members': 'operation_id object_index role version_nonce retained_version_nonce '
                                 'namespace_id publication_id fact_role fact_path',
 'timelapse_retirement_container_receipts': 'operation_id object_index role phase completed_at',
 'timelapse_retirements': 'operation_id timelapse_id printer_id owner_state owner_org_id filename '
                          'root_device root_inode state created_at',
 'timelapse_retirement_objects': 'operation_id object_index relative_path kind device inode uid gid mode '
                                 'link_count byte_count mtime_ns ctime_ns state quarantine_name reason_code',
 'timelapse_media_versions': 'version_nonce lifetime_nonce',
 'timelapse_media_lifetimes': 'lifetime_nonce root_device root_inode',
 'timelapse_ownership_anchors': 'operation_id timelapse_id printer_id filename owner_state owner_org_id',
 'timelapses': 'id ownership_operation_id retirement_operation_id status printer_id filename frame_count '
               'completed_at duration_seconds file_size_mb',
 'timelapse_frame_namespaces': 'namespace_id lifetime_nonce relative_path phase device inode uid gid mode',
 'timelapse_publications': 'operation_id kind frame_namespace_id source_lifetime result_nonce '
                           'result_lifetime phase state destination_path published_ctime_ns source_nonce '
                           'private_directory private_name',
 'timelapse_publication_facts': 'operation_id role relative_path device inode uid gid mode link_count '
                                'byte_count mtime_ns ctime_ns',
 'timelapse_retained_versions': 'version_nonce lifetime_nonce state retire_operation_id '
                                'retained_by_operation filename',
 'timelapse_retirement_moves': 'operation_id object_index kind phase verified_ctime_ns',
 'timelapse_retirement_unlinks': 'operation_id object_index phase',
 'audit_logs': 'action entity_type entity_id details'}

_PURCHASE_COLUMNS = {
 'spool_purchase_evidence': 'evidence_nonce spool_uid birth_nonce resource_kind owner_scope owner_key revision currency price_decimal net_weight_g_decimal weight_basis provenance recorded_at',
 'spools': _SOURCE_COLUMNS['spools'] + ' price initial_weight_g purchase_evidence_nonce purchase_revision',
}
_ORIGIN_COLUMNS = {
 'orders': _SOURCE_COLUMNS['orders'] + ' economic_global_origin_nonce',
 'cost_global_order_origins': 'order_uid resource_kind owner_scope owner_key origin_nonce proof_kind recorded_at',
 'cost_global_origin_exclusions': 'order_uid resource_kind owner_scope owner_key',
}
_POWER_COLUMNS = 'operation_nonce request_nonce owner_nonce printer_id target_key config_key destination_key purpose physical_job_id scheduled_job_id observation_key actor_user_id auth_kind authority_key state revision created_at changed_at command_started_at release_pending_at completed_at outcome cleanup_reason'


def _required_sqlite_guards(columns, allowed, present=()):
    from modules.system.backup_service import BackupValidationError
    required_tables = set()
    def require(table, names):
        if not set(names.split()).issubset(columns.get(table, set())):
            raise BackupValidationError('Backup has incomplete supported trigger dependencies')
        required_tables.add(table)
    if "audit_chain_state" in columns:
        require("audit_logs", "prev_hash row_hash chain_seq")
        require("audit_chain_state", "id genesis head_hash head_seq")
        require("audit_siem_destinations", "id enabled")
        require("audit_siem_outbox", "destination_id audit_id")
    def cost(table):
        if table in required_tables:
            return
        require(table, _COST_COLUMNS[table])
        for parent in _COST_DEPENDENCIES.get(table, ()):
            cost(parent)
    def source(table):
        if table in required_tables:
            return
        require(table, _SOURCE_COLUMNS[table])
        cost('cost_identities')
        if table == 'order_items':
            source('orders'); cost('cost_order_memberships')
        elif table == 'jobs':
            source('order_items'); cost('cost_job_bindings')
    def births():
        require('cost_source_lifetimes', _SOURCE_COLUMNS['cost_source_lifetimes'])
        require('spools', _SOURCE_COLUMNS['spools'])
        require('print_jobs', _SOURCE_COLUMNS['print_jobs'])
        source('jobs'); cost('cost_attempt_bindings')
    for table in _COST_COLUMNS:
        if table in columns:
            cost(table)
    for table in ('orders','order_items','jobs'):
        if 'economic_uid' in columns.get(table, ()):
            source(table)
    if 'cost_source_lifetimes' in columns or any(
        'economic_birth_nonce' in columns.get(t, ()) or 'economic_uid' in columns.get(t, ())
        for t in ('spools','print_jobs')
    ):
        births()
    membership = ('timelapse_retirement_lifetime_bindings', 'timelapse_retirement_members', 'timelapse_retirement_container_receipts')
    membership_active = any(t in columns for t in membership)
    if membership_active:
        for table, names in _MEMBERSHIP_COLUMNS.items():
            require(table, names)
    actor_columns = {'actor_user_id','actor_username','actor_auth_kind'}
    if 'audit_actor_erasures' in columns or actor_columns.intersection(columns.get('audit_logs', ())):
        require('audit_actor_erasures', 'user_id erased_at')
        require('audit_logs', 'actor_user_id actor_username actor_auth_kind')
    if 'spool_purchase_evidence' in columns or {'purchase_evidence_nonce','purchase_revision'}.intersection(columns.get('spools', ())):
        births()
        for table, names in _PURCHASE_COLUMNS.items():
            require(table, names)
    if any(t in columns for t in ('cost_global_order_origins','cost_global_origin_exclusions')) or 'economic_global_origin_nonce' in columns.get('orders', ()):
        source('orders')
        for table, names in _ORIGIN_COLUMNS.items():
            require(table, names)
    if 'power_target_operations' in columns:
        require('power_target_operations', _POWER_COLUMNS)
    if 'education_assessment_revisions' in columns:
        require('education_assessment_revisions', 'id submission_id org_id revision reviewed_by old_grade new_grade old_feedback new_feedback old_learning_status new_learning_status created_at')
    if 'education_audit_events' in columns:
        required_tables.add('education_audit_events')
    # Recovery decisions are append-only; a backup without their guards would restore editable history.
    for table in ('dispatch_recovery_history', 'dispatch_recovery_global_history'):
        if table in columns:
            required_tables.add(table)
    if any(t in columns for t in ('terminal_filament_measurements', 'filament_cost_events')):
        births()
        require('terminal_filament_measurements', 'observation_id attempt_uid attempt_birth owner_scope owner_key order_uid item_uid allocation_key outcome measurement_basis measured_grams snapshot_json reported_value reconciliation_reason created_at')
        require('filament_cost_events', 'event_id observation_id revision expected_revision audit_reason spool_uid purchase_evidence_nonce currency amount_decimal reason calculation_version created_at')
        for table, names in _PURCHASE_COLUMNS.items():
            require(table, names)
    if any(t in columns for t in ('job_summary_outbox','stock_snapshots','job_timeline_events','timeline_writer_state')):
        require('job_summary_outbox', 'print_attempt_uid attempt_birth job_id printer_id org_id outcome observed_at delivery_state')
        require('stock_snapshots', 'owner_scope owner_key variation location quantity calculated_at read_at')
        require('job_timeline_events', 'id job_id print_attempt_uid printer_id org_id kind code detail actor_user_id actor_token_id occurred_at recorded_at')
        require('timeline_writer_state', 'process_id started_at clean_shutdown last_drained_at drop_epoch error_epoch printer_ids epoch_history')
        require('jobs', 'id charged_to_org_id')
        require('printers', 'id')
        require('users', 'id')
        require('api_tokens', 'id')
    if 'job_id_high_water' in columns or {'jobs_high_water_insert', 'jobs_high_water_delete'}.intersection(present):
        require('job_id_high_water', 'singleton high_water')
        require('jobs', 'id')
    # Table requirements do not imply unrelated late families on the same source.
    required = set()
    for name, (table, _) in allowed.items():
        if table not in required_tables:
            continue
        if '_economic_birth_' in name and 'cost_source_lifetimes' not in required_tables:
            continue
        if table in ('orders','order_items','jobs') and '_economic_' in name and 'economic_uid' not in columns.get(table, ()):
            continue
        if '_membership_' in name and not membership_active:
            continue
        if name.startswith('spools_purchase_') and 'spool_purchase_evidence' not in required_tables:
            continue
        if name.startswith('jobs_high_water_') and 'job_id_high_water' not in columns:
            continue
        if name.startswith('orders_global_origin_') and 'cost_global_order_origins' not in required_tables:
            continue
        if name == 'odin_audit_append' and 'audit_chain_state' not in columns:
            continue
        required.add(name)
    return required


