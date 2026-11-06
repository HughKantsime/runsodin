"""Fail-closed PostgreSQL backup creation and pre-restore validation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from modules.system.backup_failure_alerts import report_backup_failure

from sqlalchemy import Enum as SQLAlchemyEnum, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url

from core.base import Base
from core.database_config import RESTORE_ADVISORY_LOCK, create_database_engine
from core.schema.bootstrap import (
    MIGRATION_TABLE,
    RAW_REQUIRED_COLUMNS,
    RAW_REQUIRED_TABLES,
    import_all_models,
    schema_manifest,
    schema_fingerprint,
    validate_schema,
)
from core.schema import bootstrap_database
from modules.system.backup_service import (
    BackupValidationError,
    MAX_BACKUP_BYTES,
    MIN_BACKUP_FREE_BYTES,
    _fsync_directory,
    _secure,
    _sha256,
    restore_lock,
)


@dataclass(frozen=True)
class PostgreSQLBackupPaths:
    backups: Path
    lock: Path
    pending: Path
    manifest: Path


@dataclass(frozen=True)
class PostgreSQLTOCInventory:
    structure: frozenset[str]
    table_data: frozenset[str]
    sequence_sets: frozenset[str]
    entries: int


# Strong ownership only for a failed recovery in this process. This does not
# survive process death; the durable unfinished manifest must gate startup.
_FAILED_RESTORE_BARRIERS: dict[str, tuple[object, object]] = {}


class ArchiveRefused(BackupValidationError):
    """Deterministic refusal of the archive's content; re-running cannot change it."""


class RestoreEnvironmentError(BackupValidationError):
    """Credentials, clients or local storage are not usable; the archive was not judged."""


class RestoreAcknowledgementRequired(BackupValidationError):
    """The operator has not acknowledged the staged archive's SHA-256."""


class PostgreSQLClientFailed(BackupValidationError):
    """A pg_dump/pg_restore run failed. ``retryable``: it talked to a server or timed out,
    so a later attempt can succeed; otherwise it failed reading the archive itself."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _as_archive_refusal(error: BaseException) -> BaseException:
    """Classify an error raised while the archive itself is being validated."""
    from core.schema.migrator import MigrationError
    if isinstance(error, (ArchiveRefused, RestoreEnvironmentError, RestoreAcknowledgementRequired)):
        return error
    if isinstance(error, PostgreSQLClientFailed):
        if error.retryable:
            return error
        refused = ArchiveRefused(str(error)); refused.__cause__ = error
        return refused
    if isinstance(error, (BackupValidationError, MigrationError)):
        refused = ArchiveRefused(str(error)); refused.__cause__ = error
        return refused
    return error


@contextmanager
def _archive_phase(manager):
    """Enter ``manager`` (archive validation) classifying its refusals; its body is not."""
    try:
        value = manager.__enter__()
    except BaseException as error:
        raise _as_archive_refusal(error)
    try:
        yield value
    except BaseException:
        if not manager.__exit__(*sys.exc_info()):
            raise
    else:
        manager.__exit__(None, None, None)

_SAFE_DATABASE_NAME = re.compile(r"^odin_validate_[0-9a-f]{20}$")
_SAFE_ROLLBACK_NAME = re.compile(
    r"^rollback_[0-9]{8}_[0-9]{6}_[0-9]{6}\.dump$"
)
_ALLOWED_TOC_KINDS = (
    "SEQUENCE OWNED BY",
    "FK CONSTRAINT",
    "TABLE DATA",
    "SEQUENCE SET",
    "CONSTRAINT",
    "SEQUENCE",
    "DEFAULT",
    "INDEX",
    "TRIGGER",
    "FUNCTION",
    "TABLE",
    "TYPE",
)
_FORBIDDEN_TOC_KINDS = (
    "PROCEDURE",
    "VIEW",
    "MATERIALIZED VIEW",
    "EVENT TRIGGER",
    "EXTENSION",
    "PUBLICATION",
    "SUBSCRIPTION",
    "FOREIGN DATA WRAPPER",
    "SERVER",
    "ACL",
    "COMMENT",
)

_CANONICAL_TRIGGER_FUNCTION = "odin_reject_education_audit_mutation"
_CANONICAL_AUDIT_TRIGGERS = frozenset(
    {
        ("education_audit_events", "trg_education_audit_no_delete", "DELETE"),
        ("education_audit_events", "trg_education_audit_no_update", "UPDATE"),
    }
)


def _require_distinct_maintenance_secret(
    password_file: str | None,
    maintenance_password_file: str | None,
) -> str:
    """Require maintenance credentials that cannot silently alias the app role."""
    if not maintenance_password_file:
        raise RestoreEnvironmentError(
            "PostgreSQL backup validation requires a dedicated maintenance password file"
        )
    if password_file and Path(password_file).resolve() == Path(
        maintenance_password_file
    ).resolve():
        raise RestoreEnvironmentError(
            "PostgreSQL maintenance password file must be distinct from the application password file"
        )
    return maintenance_password_file


def postgres_paths(base_dir: Path | None = None) -> PostgreSQLBackupPaths:
    root = (base_dir or Path(os.getenv("ODIN_DATA_DIR", "/data"))).resolve()
    return PostgreSQLBackupPaths(
        backups=root / "backups",
        lock=root / ".odin-postgres-restore.lock",
        pending=root / "restore-pending.dump",
        manifest=root / "restore-pending-postgres.json",
    )


def canonical_tables(*, _contract=None) -> frozenset[str]:
    from core.schema.pre026_restore_contract import check_contract,inventory_for
    check_contract(_contract)
    if _contract is not None:return frozenset(inventory_for(_contract)["tables"])
    import_all_models()
    return frozenset(
        set(Base.metadata.tables)
        | set(RAW_REQUIRED_TABLES)
        | set(RAW_REQUIRED_COLUMNS)
        | {MIGRATION_TABLE.name}
    )


def canonical_enum_types() -> frozenset[str]:
    import_all_models()
    return frozenset(
        column.type.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, SQLAlchemyEnum) and column.type.name
    )


def _normalize_postgres_definition(value: str) -> str:
    return " ".join(value.lower().split())


def _validate_canonical_public_executables(connection) -> None:
    from modules.system.postgres_canonical_executables import validate_executables
    validate_executables(connection)


def validate_live_postgres_objects(
    database_url: str, *, password_file: str | None = None
) -> None:
    """Refuse to capture unrelated objects from the live public schema."""
    engine = create_database_engine(
        database_url, role="backup", password_file=password_file
    )
    try:
        with engine.connect() as connection:
            validate_schema(connection)
            import importlib
            importlib.import_module("core.schema.migrations.026_global_order_origins").validate(connection)
            importlib.import_module("core.schema.migrations.027_power_target_operations").validate(connection)
            inspector = inspect(connection)
            if set(inspector.get_table_names(schema="public")) != set(canonical_tables()):
                raise BackupValidationError("Live public schema contains noncanonical tables")
            if inspector.get_view_names(schema="public"):
                raise BackupValidationError("Live public schema contains views")
            enums = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                        "WHERE n.nspname='public' AND t.typtype='e'"
                    )
                )
            }
            _validate_canonical_public_executables(connection)
            if enums != set(canonical_enum_types()):
                raise BackupValidationError(
                    "Live public schema contains noncanonical executable or type objects"
                )
    finally:
        engine.dispose()


def _credentialless_cli_url(database_url: str, application_name: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() not in {"postgresql", "postgres"}:
        raise BackupValidationError("PostgreSQL backup requires a PostgreSQL database URL")
    if url.password is not None:
        raise BackupValidationError("Credential-bearing PostgreSQL URLs are refused")
    if not url.host or not url.username or not url.database:
        raise BackupValidationError("PostgreSQL URL must include host, username, and database")
    if "application_name" in url.query:
        raise BackupValidationError("PostgreSQL application_name is managed by ODIN")
    # libpq clients cannot interpret SQLAlchemy driver-qualified URI schemes.
    return url.set(drivername="postgresql").update_query_dict({"application_name": application_name}).render_as_string(
        hide_password=True
    )


def _postgres_password(password_file: str | None) -> str:
    """Read a mounted PostgreSQL secret into memory for one child process."""
    secret_path = Path(password_file or os.getenv("DATABASE_PASSWORD_FILE", ""))
    try:
        mode = secret_path.stat().st_mode & 0o777
        secret = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise RestoreEnvironmentError("PostgreSQL password file is not readable") from exc
    if mode & 0o077 or not secret or "\n" in secret or "\r" in secret:
        raise RestoreEnvironmentError("PostgreSQL password file is invalid or too permissive")
    return secret


def _run_pg_tool(command: list[str], database_url: str, password_file: str | None, directory: Path) -> str:
    executable = shutil.which(command[0])
    if not executable:
        raise RestoreEnvironmentError(f"Required PostgreSQL client is unavailable: {command[0]}")
    command = [executable, *command[1:]]
    environment = os.environ.copy()
    environment.pop("PGPASSFILE", None)
    environment["PGPASSWORD"] = _postgres_password(password_file)
    try:
        result = subprocess.run(
            command,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        # A timeout (also of pg_restore --list) is not a judgement of the archive.
        raise PostgreSQLClientFailed(
            f"PostgreSQL client operation timed out: {Path(command[0]).name}", retryable=True
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PostgreSQLClientFailed(
            f"PostgreSQL client operation failed: {Path(command[0]).name}",
            retryable=any(item == "--dbname" or item.startswith("--dbname=") for item in command),
        ) from exc
    return result.stdout


@report_backup_failure
def create_postgres_backup(
    database_url: str,
    *,
    maintenance_url: str,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: Path | None = None,
    prefix: str = "odin_backup_",
) -> tuple[Path, dict[str, object]]:
    maintenance_password_file = _require_distinct_maintenance_secret(
        password_file, maintenance_password_file
    )
    paths = postgres_paths(base_dir)
    paths.backups.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(paths.backups).free < MIN_BACKUP_FREE_BYTES:
        raise BackupValidationError("Insufficient free disk space for a verified backup")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    target = paths.backups / f"{prefix}{timestamp}.dump"
    temporary = target.with_suffix(".dump.tmp")
    command = [
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        "--file",
        str(temporary),
        "--dbname",
        _credentialless_cli_url(database_url, "odin-backup"),
    ]
    for table_name in sorted(canonical_tables()):
        command.extend(["--table", f"public.{table_name}"])
    validate_live_postgres_objects(database_url, password_file=password_file)
    with restore_lock(paths.lock):
        try:
            dump_started = time.monotonic()
            _run_pg_tool(command, database_url, password_file, paths.backups)
            dump_duration = time.monotonic() - dump_started
            _secure(temporary)
            validation_started = time.monotonic()
            metadata = validate_postgres_restore(
                temporary,
                database_url,
                maintenance_url,
                password_file=password_file,
                maintenance_password_file=maintenance_password_file,
                base_dir=base_dir,
            )
            validation_duration = time.monotonic() - validation_started
            os.replace(temporary, target)
            _fsync_directory(paths.backups)
        finally:
            temporary.unlink(missing_ok=True)
    return target, {
        **metadata,
        "backup_duration_seconds": round(dump_duration, 3),
        "validation_duration_seconds": round(validation_duration, 3),
    }


def _maintenance_urls(
    maintenance_url: str, target_url: str, validation_database: str
) -> tuple[str, str]:
    maintenance = make_url(maintenance_url)
    target = make_url(target_url)
    if maintenance.password is not None or target.password is not None:
        raise BackupValidationError("Credential-bearing PostgreSQL URLs are refused")
    if maintenance.database == target.database:
        raise BackupValidationError("Maintenance URL must not target the live ODIN database")
    validation = maintenance.set(database=validation_database)
    return (
        _credentialless_cli_url(maintenance.render_as_string(hide_password=True), "odin-restore"),
        _credentialless_cli_url(validation.render_as_string(hide_password=True), "odin-restore"),
    )


def _insert_postgres_restore_audit(
    database_url: str,
    password_file: str | None,
    action: str,
    details: dict[str, object],
) -> None:
    engine = create_database_engine(
        database_url, role="restore", password_file=password_file
    )
    try:
        with engine.begin() as connection:
            from core.audit_restore_markers import seal_marker
            details = seal_marker(action, details, connection)
            connection.execute(
                text(
                    "INSERT INTO audit_logs (action, entity_type, details) "
                    "VALUES (:action, 'system', :details)"
                ),
                {"action": action, "details": json.dumps(details, sort_keys=True)},
            )
    finally:
        engine.dispose()


def _set_aside_failed_restore(paths, manifest, *, reason, error_type, status, action) -> dict[str, object]:
    """Keep a failed staged archive and an actionable record in backups/ (listed, noticed, retained)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    archive = paths.backups / f"failed_restore_{stamp}.dump"
    record_path = paths.backups / f"failed_restore_{stamp}.json"
    record = {
        "status": status,
        "reason": reason,
        "error_type": error_type,
        "candidate_sha256": manifest.get("sha256"),
        "staged_at": manifest.get("staged_at"),
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "archive": archive.name if paths.pending.is_file() else None,
        "action": action,
    }
    paths.backups.mkdir(parents=True, exist_ok=True)
    temporary = record_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    _secure(temporary)
    os.replace(temporary, record_path)
    if paths.pending.is_file():
        os.replace(paths.pending, archive)
        os.utime(archive)  # Retention ages from the quarantine, not from staging.
    paths.manifest.unlink(missing_ok=True)
    _fsync_directory(paths.backups)
    _fsync_directory(paths.manifest.parent)
    return record


def quarantine_failed_pending_postgres_restore(
    target_url: str,
    *,
    error: BaseException,
    password_file: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, object] | None:
    """Move aside a staged restore that failed before touching the target.

    Only for state 'staged' (no data was replaced) and never while this process holds a
    failed-recovery barrier. The archive and an actionable record stay in the backups
    directory; startup then continues on the untouched target instead of restarting
    forever. Returns None when the failure is not quarantinable, and the caller re-raises.
    """
    if not isinstance(error, ArchiveRefused) or target_url in _FAILED_RESTORE_BARRIERS:
        return None
    paths = postgres_paths(base_dir)
    try:
        manifest = _read_pending_manifest(paths)
    except BackupValidationError:
        return None
    if manifest.get("state") != "staged":
        return None
    reason = (str(error) or type(error).__name__)[:500]
    record = _set_aside_failed_restore(paths, manifest, reason=reason, error_type=type(error).__name__,
        status="restore_failed_target_untouched",
        action="The current database was not changed. Fix the reason above, then re-stage the archive "
               "and restart with ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT set to its SHA-256.")
    try:
        _insert_postgres_restore_audit(target_url, password_file, "restore_failed",
            {"reason": reason, "candidate_sha256": manifest.get("sha256"), "target_untouched": True})
    except Exception:  # The record file and the returned status already carry the reason.
        pass
    return record


_QUARANTINED = re.compile(r"failed_restore_\d{8}_\d{6}_\d{6}\.(dump|json)")


def quarantined_restore_count(dialect_name: str, base_dir: Path | None = None):
    """Quarantined archives still present (orphan records are not counted); None if unknown."""
    try:
        if dialect_name != "postgresql":
            return 0
        return sum(1 for record in quarantined_restores(base_dir) if record["archive"] is not None)
    except Exception:
        return None


def quarantined_restores(base_dir: Path | None = None) -> list[dict[str, object]]:
    """Quarantined restore records (newest first) for the backups list and diagnostics."""
    backups = postgres_paths(base_dir).backups
    if not backups.is_dir():
        return []
    entries = []
    for record_path in sorted(backups.glob("failed_restore_*.json"), reverse=True):
        if not _QUARANTINED.fullmatch(record_path.name):
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {"reason": "Quarantine record is unreadable"}
        archive = backups / (record_path.stem + ".dump")
        entries.append({"record": record_path.name, "archive": archive.name if archive.is_file() else None,
                        "reason": record.get("reason"), "action": record.get("action"),
                        "candidate_sha256": record.get("candidate_sha256"), "failed_at": record.get("failed_at")})
    return entries



logger = logging.getLogger(__name__)

# Preserve the service entry points and patchable dependencies across split modules.
from modules.system.postgres_archive_inventory import (
    _toc_kind,
    _toc_inventory,
    validate_archive_toc,
    _ordered_restore_data,
    _trusted_restore_foreign_keys,
    _validated_restore_list,
    inspect_postgres_archive,
)

from modules.system.postgres_restore_validation import (
    _validate_current_postgres_restore,
    _inspect_compatible_archive,
    validate_postgres_restore,
    _validate_postgres_restore,
    stage_postgres_restore,
)

from modules.system.postgres_restore_guards import (
    TARGET_SESSION_DRAIN_SECONDS,
    TARGET_SESSION_DRAIN_POLL_SECONDS,
    _read_pending_manifest,
    _pending_rollback_path,
    _write_pending_manifest,
    _assert_no_target_sessions,
    _assert_no_external_dependencies,
    _schema_sql_evidence,
    _restore_postgres_archive,
)

from modules.system.postgres_restore_apply import (
    apply_pending_postgres_restore,
)
