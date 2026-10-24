"""Generate and verify ODIN's classified runtime SQLite compatibility inventory."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
INVENTORY = ROOT / "ops" / "database_parity" / "sqlite_compatibility_inventory.json"


CLASSIFICATIONS: dict[str, tuple[str, str]] = {
    "backend/modules/inventory/slot_assignments.py": (
        "sqlite-provider-only",
        "SQLite BEGIN IMMEDIATE establishes the assignment writer boundary; PostgreSQL uses sorted NOWAIT row locks and a graph recheck.",
    ),
    "backend/core/schema/migrations/037_education_memberships.py": (
        "sqlite-provider-only",
        "SQLite rebuilds retained Education ownership on a dedicated connection and verifies enforced FK actions with PRAGMA; PostgreSQL swaps and validates native constraints atomically.",
    ),
    "backend/core/schema/education_archive_validation.py": (
        "sqlite-provider-only",
        "SQLite PRAGMA verifies the exact enforced additive archive actor FK; PostgreSQL uses its reflected constraints.",
    ),
    "backend/core/account_bound_session_schema.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/account_bound_session_validation.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/account_bound_tokens.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/account_lifetime_creation.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/account_lifetime_schema.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/account_lifetime_validation.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/priced_estimate_schema.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/priced_estimate_validation.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/project_import_transaction.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/project_import_validation.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/schema/prepriced_restore_contract.py": (
        "sqlite-provider-only",
        "Unregistered prototype: not imported by bootstrap, the app or any live module. SQLite branch of a dual-dialect contract; reclassify when wired into a runtime path.",
    ),
    "backend/core/log_redaction.py": (
        "dialect-neutral",
        "Credential-redaction regular expressions (Set-Cookie headers, optional quotes) contain qmark-like fragments; this logging helper performs no database operations or SQL.",
    ),
    "backend/modules/archives/project_zip_manifest.py": (
        "dialect-neutral",
        "The bounded UTC timestamp regular expression contains a qmark-like fragment; this closed ZIP metadata parser performs no database operations or SQL.",
    ),
    'backend/core/economic_source_creation.py': ('sqlite-provider-only', 'SQLite foreign-key checks and BEGIN IMMEDIATE are provider guarded; PostgreSQL fixed internal qmark templates translate through the caller-owned executor. Classification does not certify financial valuation.'),
    'backend/core/global_order_origin_schema.py': ('sqlite-provider-only', 'SQLite catalog, enforcement PRAGMAs and writer promotion are dialect guarded; PostgreSQL uses explicit native catalog and relation-lock validation.'),
    'backend/core/power_target_operation_schema.py': ('sqlite-provider-only', 'SQLite driver transaction and enforcement/catalog checks are dialect guarded; PostgreSQL uses separately typed catalog, finite-time and bounded lock checks.'),
    'backend/core/power_target_operations.py': ('dialect-neutral', 'Closed operation templates use the shared power schema executor for provider-specific qmark binding and timestamps; this storage helper does not authorize device commands.'),
    'backend/core/schema/migrations/021_economic_identity.py': ('sqlite-provider-only', 'SQLite declarations, trigger catalog and foreign-key PRAGMAs are guarded by dialect; PostgreSQL validates native constraints and exact guard definitions.'),
    'backend/core/schema/migrations/022_economic_source_lifetimes.py': ('sqlite-provider-only', 'SQLite lifetime DDL and retained enforcement checks are dialect guarded; PostgreSQL has its own source-owned constraints and guard validation.'),
    'backend/core/schema/migrations/033_timelapse_capture_tenancy.py': ('sqlite-provider-only', 'The foreign-key PRAGMA that reads ON DELETE and the BEGIN IMMEDIATE write lock are guarded by dialect; PostgreSQL validates the reflected constraint options.'),
    'backend/core/schema/migrations/034_alert_outbox.py': ('sqlite-provider-only', 'The foreign-key PRAGMA that reads ON DELETE and the BEGIN IMMEDIATE write lock are guarded by dialect; PostgreSQL validates the reflected constraint options.'),
    'backend/core/spool_purchase_evidence.py': ('sqlite-provider-only', 'SQLite foreign-key enforcement and writer transaction checks are dialect guarded; PostgreSQL uses finite native locks in the caller transaction.'),
    'backend/core/spool_purchase_schema.py': ('sqlite-provider-only', 'SQLite catalog, shadowing and foreign-key checks are dialect guarded; PostgreSQL uses native schema and exact canonical guard validation.'),
    'backend/modules/archives/timelapse_action_authority.py': ('sqlite-provider-only', 'SQLite foreign-key enforcement and BEGIN IMMEDIATE are dialect guarded; PostgreSQL action authority uses its own caller transaction and source locks.'),
    "backend/core/schema/migrations/020_ams_evidence.py": (
        "sqlite-provider-only",
        "Explicit SQLite branch uses canonical DDL, integrity/FK PRAGMAs and writer promotion; PostgreSQL uses native catalog reflection and locked authority",
    ),
    "backend/core/ams_source_inspection.py": (
        "dialect-neutral",
        "G-code header regular expression contains a qmark-like fragment; this bounded archive parser has no database operations or SQL.",
    ),
    "backend/core/schema/migrations/019_timelapse_publications.py": (
        "sqlite-provider-only",
        "Unregistered publication prototype: SQLite BEGIN IMMEDIATE, no-row writer promotion and retained-integrity PRAGMAs are guarded; PostgreSQL uses separate qualified native relation locks and metadata checks. Classification does not register the migration or certify restore/parity.",
    ),
    "backend/core/schema/migrations/018_timelapse_unlinks.py": (
        "sqlite-provider-only",
        "Dialect-guarded SQLite BEGIN IMMEDIATE owns additive DDL; native PostgreSQL validates exact inherited same-schema checked foreign authority and retained unlink/object phase correspondence",
    ),
    "backend/core/schema/migrations/017_timelapse_recovery.py": (
        "sqlite-provider-only",
        "SQLite-only BEGIN IMMEDIATE and table integrity/foreign-key checks; PostgreSQL separately validates exact checked same-schema nondeferrable foreign authorities and update actions",
    ),
    "backend/core/schema/migrations/016_timelapse_ownership.py": (
        "sqlite-provider-only",
        "SQLite additive DDL ownership and integrity PRAGMAs are dialect guarded; PostgreSQL uses strict native column and validated constraint inspection",
    ),
    "backend/core/schema/migrations/015_timelapse_retirements.py": (
        "sqlite-provider-only",
        "SQLite caller-owned BEGIN IMMEDIATE and PRAGMA retained-evidence checks are dialect guarded; PostgreSQL uses native validated constraint and parent-OID inspection with strict additive adoption",
    ),
    "backend/core/schema/mfa_enrollments.py": (
        "sqlite-provider-only",
        "SQLite PRAGMA foreign-key inspection is dialect guarded; PostgreSQL validates the canonical users relation and validated constraints through native metadata, with strict adoption tests on both providers",
    ),
    "backend/modules/archives/attempt_accounting.py": (
        "dialect-neutral",
        "Caller-owned SQLAlchemy or shared DBAPI transaction translates qmark SQL; actual PostgreSQL accounting, duplicate-attempt and inventory contention tests cover the boundary",
    ),
    "backend/core/schema/migrations/009_cost_foundation.py": (
        "sqlite-provider-only",
        "SQLite schema signature and statement-completeness checks are dialect guarded; PostgreSQL uses its own frozen schema contract and advisory transaction lock",
    ),
    "backend/core/schema/cost_contract_signature.py": (
        "sqlite-provider-only",
        "Explicit SQLite sqlite_master signature is separate from PostgreSQL catalog signature; both use bound values",
    ),
    "backend/core/schema/migrations/012_dispatch_receipts.py": (
        "sqlite-provider-only",
        "Dialect-guarded SQLite receipt integrity checks; PostgreSQL requires canonical validated constraints and timezone-aware contract",
    ),
    "backend/core/schema/migrations/014_dispatch_senders.py": (
        "sqlite-provider-only",
        "Explicit SQLite DDL transaction and retained-row integrity validation; PostgreSQL separately validates canonical constraints and timezone-aware storage",
    ),
    "backend/core/schema/migrations/011_dispatch_claims.py": (
        "sqlite-provider-only",
        "Dialect-guarded table-scoped integrity validation rejects incompatible retained SQLite claim rows; PostgreSQL separately requires canonical checks validated in its catalog",
    ),
    "backend/core/schema/migrations/010_h2d_nozzle_observations.py": (
        "sqlite-provider-only",
        "SQLite-only BEGIN IMMEDIATE protects additive DDL; timestamp SQL translates for PostgreSQL and adopted column shapes are checked before mutation",
    ),
    "backend/modules/printers/monitors/ams_inventory.py": (
        "dialect-neutral",
        "Caller-owned shared DBAPI adapter binds qmark SQL on both providers; guarded SQLite writer boundary and PostgreSQL NOWAIT locks have owned persistence tests",
    ),
    "backend/modules/printers/camera_relay.py": (
        "dialect-neutral",
        "Single read-only qmark lookup through the shared get_db DBAPI adapter, which binds qmark SQL on both providers; IS TRUE boolean predicates are valid on SQLite and PostgreSQL",
    ),
    "backend/modules/printers/monitors/light_state.py": (
        "dialect-neutral",
        "Caller-owned shared DBAPI qmark bindings; PostgreSQL timestamp cast and SQLite exact snapshot comparison protect concurrent light commands",
    ),
    "backend/modules/archives/ownership.py": (
        "dialect-neutral",
        "Authoritative job/file/Education graph reads use the shared attempt SQL adapter on SQLite and PostgreSQL; no device-sharing ownership inference",
    ),
    "backend/modules/archives/legacy_ownership.py": (
        "dialect-neutral",
        "Named SQLAlchemy queries and typed expanding paths; PostgreSQL NOWAIT source locks avoid terminal-policy inversion, SQLite no-op updates serialize its writer",
    ),
    "backend/core/schema/migrations/007_attempt_accounting.py": (
        "sqlite-provider-only",
        "BEGIN IMMEDIATE is guarded by the SQLite dialect; SQLAlchemy reflection and additive schema/index validation run on both providers with adoption and unknown-legacy preservation tests",
    ),
    "backend/core/order_production.py": (
        "dialect-neutral",
        "ORM named parameters and raw qmark operations use the shared DBAPI adapter; owned PostgreSQL production tests cover both boundaries",
    ),
    "backend/modules/orders/routes/show_orders.py": (
        "dialect-neutral",
        "Pragma is an HTTP cache-control response header, not a SQLite statement; this route module executes no SQL",
    ),
    "backend/modules/orders/production.py": (
        "dialect-neutral",
        "Caller-owned ORM or DBAPI transaction; named parameters translate to shared adapter qmark SQL for SQLite and PostgreSQL",
    ),
    "backend/core/schema/migrations/005_order_production_snapshots.py": (
        "sqlite-provider-only",
        "SQLite-only BEGIN IMMEDIATE guards DDL transactions; frozen SQLAlchemy tables and inspection run on both database providers",
    ),
    "backend/core/schema/migrations/006_print_job_measurements.py": (
        "sqlite-provider-only",
        "SQLite-only explicit BEGIN guards transactional ALTER; fixed nullable REAL additions and adopted-shape validation run on both providers",
    ),
    "backend/modules/system/sanitize.py": (
        "dialect-neutral",
        "Secret-filter regular expressions contain question marks; this utility executes no SQL",
    ),
    "backend/core/app.py": (
        "dialect-neutral",
        "Runtime SQL executes through the dialect-aware shared DBAPI compatibility adapter",
    ),
    "backend/core/database_config.py": (
        "sqlite-provider-only",
        "SQLite engine branch; PostgreSQL engine creation does not receive SQLite connect args",
    ),
    "backend/core/db.py": (
        "sqlite-provider-only",
        "SQLite connection event hooks guarded by the configured provider",
    ),
    "backend/core/db_compat.py": (
        "dialect-neutral",
        "Provider abstraction translates qmark SQL and normalizes insert identity semantics",
    ),
    "backend/core/db_utils.py": (
        "dialect-neutral",
        "Shared DBAPI adapter conditionally applies SQLite PRAGMAs and adapts PostgreSQL SQL",
    ),
    "backend/core/schema/migrator.py": (
        "sqlite-provider-only",
        "Dedicated SQLite branches use PRAGMA for schema inspection, migration FK toggling, and pre-commit integrity verification",
    ),
    "backend/core/schema/migrations/002_education_tenant_integrity.py": (
        "sqlite-provider-only",
        "Education migration uses guarded SQLite PRAGMA inspection while PostgreSQL uses SQLAlchemy reflection",
    ),
    "backend/core/credential_write_guard.py": (
        "dialect-neutral",
        "Runtime statement classifier: regexes contain SQL vocabulary (INSERT OR, REPLACE INTO) to recognise writes on either provider; it executes no database statement",
    ),
    "backend/modules/organizations/education_policy_inventory.py": (
        "dialect-neutral",
        "Static source-discovery regex contains SQL vocabulary and regex question marks; it executes no database statement",
    ),
    "backend/core/print_attempts.py": (
        "dialect-neutral",
        "qmark_runner translates named parameters for the shared DBAPI cursor adapter, which rewrites them for PostgreSQL",
    ),
    "backend/modules/jobs/state_machine.py": (
        "dialect-neutral",
        "transition_job translates named parameters to qmark only for the shared DBAPI cursor adapter, which rewrites them for PostgreSQL",
    ),
    "backend/modules/organizations/education_policy.py": (
        "dialect-neutral",
        "Central policy translates named parameters for the shared DBAPI adapter and uses sqlite3 only to classify integrity exceptions",
    ),
    "backend/modules/system/backup_service.py": (
        "sqlite-provider-only",
        "Dedicated SQLite backup, validation, and offline restore provider",
    ),
    "backend/modules/system/routes_admin.py": (
        "sqlite-provider-only",
        "SQLite integrity-check branch; PostgreSQL uses provider-specific health checks",
    ),
    "backend/modules/archives/archive.py": (
        "dialect-neutral",
        "Insert identity is supplied by the dialect-aware DBAPI result adapter",
    ),
    "backend/modules/notifications/channels.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/notifications/error_handling.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/core/quota.py": (
        "dialect-neutral",
        "qmark_runner binds named parameters as qmarks for the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/notifications/job_events.py": (
        "dialect-neutral",
        "Qmark and insert identity paths use the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/notifications/printer_health.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/printers/monitors/mqtt_job_lifecycle.py": (
        "dialect-neutral",
        "Qmark and insert identity paths use the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/printers/monitors/mqtt_printer.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/scripts/demo_seed_edu.py": (
        "sqlite-provider-only",
        "SQLite-only local EDU demo seed command",
    ),
    "backend/scripts/demo_seed_reviewer.py": (
        "sqlite-provider-only",
        "SQLite-only local reviewer demo seed command",
    ),
    "backend/scripts/seed_release_gate.py": (
        "dialect-neutral",
        "Candidate seed uses a dual-provider connection shim and provider-specific schema inspection",
    ),
    "backend/scripts/seed_edu_sandbox.py": (
        "dialect-neutral",
        "Education sandbox seed uses the dual-provider connection shim and provider-specific insert identity",
    ),
}


def _call_name(node: ast.Call) -> str:
    function = node.func
    return function.attr if isinstance(function, ast.Attribute) else ""


def _snippet(source_lines: list[str], lineno: int) -> str:
    return source_lines[lineno - 1].strip()[:240]


def _is_qmark_sql_fragment(value: str) -> bool:
    if "?" not in value:
        return False
    if re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|VALUES|WHERE|SET)\b", value, re.I):
        return True
    return bool(
        re.search(
            r"=\s*\?(?=\s*[,)]|$)|\?\s*[,)]|[(,]\s*\?(?=\s*[,)]|$)",
            value,
        )
    )


def scan_source(relative: str, source: str) -> list[dict[str, object]]:
    """Return classified compatibility sites for one repository-relative source."""
    lines = source.splitlines()
    tree = ast.parse(source, filename=relative)
    docstrings = {
        id(owner.body[0].value)
        for owner in ast.walk(tree)
        if isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and owner.body
        and isinstance(owner.body[0], ast.Expr)
        and isinstance(owner.body[0].value, ast.Constant)
        and isinstance(owner.body[0].value.value, str)
    }
    joined_string_members = {
        id(member)
        for joined in ast.walk(tree)
        if isinstance(joined, ast.JoinedStr)
        for member in ast.walk(joined)
        if member is not joined
    }
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "sqlite3" for alias in node.names
        ):
            hits.add((node.lineno, "sqlite3"))
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            hits.add((node.lineno, "sqlite3"))
        elif isinstance(node, ast.keyword) and node.arg == "check_same_thread":
            hits.add((node.lineno, "check_same_thread"))
        elif isinstance(node, ast.Attribute) and node.attr == "lastrowid":
            hits.add((node.lineno, "lastrowid"))
        elif isinstance(node, ast.JoinedStr):
            literal_text = "".join(
                member.value
                for member in node.values
                if isinstance(member, ast.Constant) and isinstance(member.value, str)
            )
            if _is_qmark_sql_fragment(literal_text):
                hits.add((node.lineno, "qmark-parameter"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            if id(node) not in docstrings and node.value == "check_same_thread":
                # SQLAlchemy supplies this SQLite-only option inside connect_args,
                # where it is represented as a dictionary key rather than a call
                # keyword argument.
                hits.add((node.lineno, "check_same_thread"))
            if "PRAGMA" in upper:
                hits.add((node.lineno, "PRAGMA"))
            if "LAST_INSERT_ROWID" in upper:
                hits.add((node.lineno, "last_insert_rowid"))
            if id(node) not in docstrings and id(node) not in joined_string_members and (
                _is_qmark_sql_fragment(node.value)
                or (
                    relative == "backend/core/db_utils.py"
                    and node.value.strip() == "?"
                )
            ):
                hits.add((node.lineno, "qmark-parameter"))
        if isinstance(node, ast.Call) and _call_name(node) in {"execute", "executemany"}:
            if node.args:
                # Walk the complete SQL-expression tree, not only a direct string
                # constant. This catches f-strings, concatenated fragments, and
                # generated placeholder lists such as ','.join('?' * len(ids)).
                for fragment in ast.walk(node.args[0]):
                    if isinstance(fragment, ast.JoinedStr):
                        literal_text = "".join(
                            member.value
                            for member in fragment.values
                            if isinstance(member, ast.Constant)
                            and isinstance(member.value, str)
                        )
                        if "?" in literal_text:
                            hits.add((fragment.lineno, "qmark-parameter"))
                    elif (
                        isinstance(fragment, ast.Constant)
                        and isinstance(fragment.value, str)
                        and "?" in fragment.value
                        and id(fragment) not in joined_string_members
                    ):
                        hits.add((fragment.lineno, "qmark-parameter"))

    classification = CLASSIFICATIONS.get(relative)
    if hits and classification is None:
        raise RuntimeError(f"unclassified SQLite compatibility site: {relative}")
    if not classification:
        return []
    label, reason = classification
    return [
        {
            "path": relative,
            "line": lineno,
            "pattern": pattern,
            "classification": label,
            "reason": reason,
            "source": _snippet(lines, lineno),
        }
        for lineno, pattern in sorted(hits)
    ]


def scan_file(path: Path) -> list[dict[str, object]]:
    relative = path.relative_to(ROOT).as_posix()
    return scan_source(relative, path.read_text(encoding="utf-8"))


# Hash-pinned frozen sources used only to stage historical restores; not live code.
PRESERVED_BUNDLES = (
    BACKEND / "core/schema/preserved027/bundle",
    BACKEND / "core/schema/preserved029/bundle",
    BACKEND / "core/schema/preserved034/bundle",
    BACKEND / "core/schema/preserved035/overlay",
    BACKEND / "core/schema/preserved036/overlay",
    BACKEND / "core/schema/preserved037/overlay",
)


def generate() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for path in sorted(BACKEND.rglob("*.py")):
        if any(bundle in path.parents for bundle in PRESERVED_BUNDLES):
            continue
        entries.extend(scan_file(path))
    return {
        "schema_version": 1,
        "patterns": [
            "sqlite3",
            "PRAGMA",
            "check_same_thread",
            "qmark-parameter",
            "last_insert_rowid",
            "lastrowid",
        ],
        "entries": entries,
    }


def serialized_inventory() -> str:
    return json.dumps(generate(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    generated = serialized_inventory()
    if args.write:
        INVENTORY.write_text(generated, encoding="utf-8")
        print(f"wrote {INVENTORY.relative_to(ROOT)}")
        return 0
    if not INVENTORY.is_file() or INVENTORY.read_text(encoding="utf-8") != generated:
        raise SystemExit(
            "SQLite compatibility inventory is stale; review changes and run "
            "python3 -m ops.database_parity.sqlite_compatibility_inventory --write"
        )
    print(f"SQLite compatibility inventory PASS: {len(generate()['entries'])} sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
