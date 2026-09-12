"""Fail-closed PostgreSQL backup creation and pre-restore validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Enum as SQLAlchemyEnum, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url

from core.base import Base
from core.database_config import RESTORE_ADVISORY_LOCK, create_database_engine
from core.schema.bootstrap import (
    MIGRATION_TABLE,
    RAW_REQUIRED_COLUMNS,
    import_all_models,
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
    "TABLE",
    "TYPE",
)
_FORBIDDEN_TOC_KINDS = (
    "FUNCTION",
    "PROCEDURE",
    "TRIGGER",
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


def _require_distinct_maintenance_secret(
    password_file: str | None,
    maintenance_password_file: str | None,
) -> str:
    """Require maintenance credentials that cannot silently alias the app role."""
    if not maintenance_password_file:
        raise BackupValidationError(
            "PostgreSQL backup validation requires a dedicated maintenance password file"
        )
    if password_file and Path(password_file).resolve() == Path(
        maintenance_password_file
    ).resolve():
        raise BackupValidationError(
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


def canonical_tables() -> frozenset[str]:
    import_all_models()
    return frozenset(set(Base.metadata.tables) | set(RAW_REQUIRED_COLUMNS) | {MIGRATION_TABLE.name})


def canonical_enum_types() -> frozenset[str]:
    import_all_models()
    return frozenset(
        column.type.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, SQLAlchemyEnum) and column.type.name
    )


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
            inspector = inspect(connection)
            if set(inspector.get_table_names(schema="public")) != set(canonical_tables()):
                raise BackupValidationError("Live public schema contains noncanonical tables")
            if inspector.get_view_names(schema="public"):
                raise BackupValidationError("Live public schema contains views")
            function_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='public'"
                )
            ).scalar_one()
            trigger_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND NOT t.tgisinternal"
                )
            ).scalar_one()
            enums = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                        "WHERE n.nspname='public' AND t.typtype='e'"
                    )
                )
            }
            if function_count or trigger_count or enums != set(canonical_enum_types()):
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
    return url.update_query_dict({"application_name": application_name}).render_as_string(
        hide_password=True
    )


def _postgres_password(password_file: str | None) -> str:
    """Read a mounted PostgreSQL secret into memory for one child process."""
    secret_path = Path(password_file or os.getenv("DATABASE_PASSWORD_FILE", ""))
    try:
        mode = secret_path.stat().st_mode & 0o777
        secret = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise BackupValidationError("PostgreSQL password file is not readable") from exc
    if mode & 0o077 or not secret or "\n" in secret or "\r" in secret:
        raise BackupValidationError("PostgreSQL password file is invalid or too permissive")
    return secret


def _run_pg_tool(command: list[str], database_url: str, password_file: str | None, directory: Path) -> str:
    executable = shutil.which(command[0])
    if not executable:
        raise BackupValidationError(f"Required PostgreSQL client is unavailable: {command[0]}")
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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BackupValidationError(
            f"PostgreSQL client operation failed: {Path(command[0]).name}"
        ) from exc
    return result.stdout


def _toc_kind(line: str) -> str | None:
    haystack = f" {line} "
    for kind in _ALLOWED_TOC_KINDS:
        if f" {kind} " in haystack:
            return kind
    return None


def _toc_inventory(toc: str) -> PostgreSQLTOCInventory:
    structure: set[str] = set()
    table_data: set[str] = set()
    sequence_sets: set[str] = set()
    entries = 0
    for raw_line in toc.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        entries += 1
        if " SCHEMA - public " in f" {line} ":
            continue
        match = re.match(r"^\d+;\s+\d+\s+\d+\s+(.+)$", line)
        if not match:
            raise BackupValidationError("PostgreSQL archive TOC entry is malformed")
        body = match.group(1)
        descriptor, separator, _owner = body.rpartition(" ")
        if not separator or not descriptor:
            raise BackupValidationError("PostgreSQL archive TOC entry is malformed")
        kind = _toc_kind(descriptor)
        if kind == "TABLE DATA":
            table_data.add(descriptor)
        elif kind == "SEQUENCE SET":
            sequence_sets.add(descriptor)
        else:
            structure.add(descriptor)
    return PostgreSQLTOCInventory(
        structure=frozenset(structure),
        table_data=frozenset(table_data),
        sequence_sets=frozenset(sequence_sets),
        entries=entries,
    )


def validate_archive_toc(
    toc: str,
    *,
    allowed_structure: frozenset[str] | None = None,
    allow_unresolved_structure: bool = False,
) -> dict[str, object]:
    """Reject archive objects outside the canonical ODIN public-schema set."""
    tables = canonical_tables()
    archive_tables: set[str] = set()
    for raw_line in toc.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if " SCHEMA - public " in f" {line} ":
            continue
        upper = f" {line.upper()} "
        if any(f" {kind} " in upper for kind in _FORBIDDEN_TOC_KINDS):
            raise BackupValidationError("PostgreSQL archive contains a forbidden object type")
        kind = _toc_kind(line)
        if not kind:
            raise BackupValidationError("PostgreSQL archive contains an unsupported object type")
        if " public " not in f" {line} ":
            raise BackupValidationError("PostgreSQL archive contains an object outside public schema")
        if kind in {"TABLE", "TABLE DATA"}:
            match = re.search(rf" {re.escape(kind)} public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in tables:
                raise BackupValidationError("PostgreSQL archive contains a non-ODIN table")
            if kind == "TABLE":
                archive_tables.add(match.group(1))
        elif kind == "TYPE":
            match = re.search(r" TYPE public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in canonical_enum_types():
                raise BackupValidationError("PostgreSQL archive contains a non-ODIN type")
        elif kind in {"DEFAULT", "CONSTRAINT", "FK CONSTRAINT"}:
            match = re.search(rf" {re.escape(kind)} public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in tables:
                raise BackupValidationError(
                    "PostgreSQL archive contains a non-ODIN structural object"
                )
    missing = sorted(tables - archive_tables)
    if missing:
        raise BackupValidationError(
            "PostgreSQL archive is missing required ODIN tables: " + ", ".join(missing)
        )
    inventory = _toc_inventory(toc)
    unresolved = {
        descriptor
        for descriptor in inventory.structure
        if not descriptor.startswith("TABLE public ")
        and not descriptor.startswith("TYPE public ")
    }
    if allowed_structure is None:
        if (unresolved or inventory.sequence_sets) and not allow_unresolved_structure:
            raise BackupValidationError(
                "PostgreSQL archive contains a non-ODIN structural object"
            )
    elif inventory.structure != allowed_structure:
        missing_structure = sorted(allowed_structure - inventory.structure)[:12]
        extra_structure = sorted(inventory.structure - allowed_structure)[:12]
        raise BackupValidationError(
            "PostgreSQL archive structural objects do not match the canonical ODIN schema; "
            f"missing={missing_structure}; extra={extra_structure}"
        )

    if allowed_structure is not None:
        expected_table_data = frozenset(
            f"TABLE DATA public {table_name}" for table_name in tables
        )
        expected_sequence_sets = frozenset(
            descriptor.replace("SEQUENCE public ", "SEQUENCE SET public ", 1)
            for descriptor in allowed_structure
            if descriptor.startswith("SEQUENCE public ")
        )
        if inventory.table_data != expected_table_data:
            raise BackupValidationError(
                "PostgreSQL archive table-data objects do not match the canonical ODIN schema"
            )
        if inventory.sequence_sets != expected_sequence_sets:
            raise BackupValidationError(
                "PostgreSQL archive sequence-set objects do not match the canonical ODIN schema"
            )
    return {
        "toc_entries": inventory.entries,
        "table_count": len(archive_tables),
        "toc_fingerprint": hashlib.sha256(
            "\n".join(
                sorted(
                    inventory.structure
                    | inventory.table_data
                    | inventory.sequence_sets
                )
            ).encode("utf-8")
        ).hexdigest(),
    }


def _validated_restore_list(
    archive: Path,
    database_url: str,
    password_file: str | None,
    directory: Path,
    *,
    allowed_structure: frozenset[str] | None = None,
    trusted_prevalidated: bool = False,
) -> Path:
    toc = _run_pg_tool(
        ["pg_restore", "--list", str(archive)], database_url, password_file, directory
    )
    validate_archive_toc(
        toc,
        allowed_structure=allowed_structure,
        allow_unresolved_structure=trusted_prevalidated,
    )
    descriptor, filename = tempfile.mkstemp(prefix=".odin-restore-list-", dir=directory)
    path = Path(filename)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for line in toc.splitlines():
            if " SCHEMA - public " not in f" {line} ":
                handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return path


def inspect_postgres_archive(
    archive: Path,
    database_url: str,
    *,
    password_file: str | None = None,
    work_dir: Path | None = None,
    allowed_structure: frozenset[str] | None = None,
    trusted_prevalidated: bool = False,
) -> dict[str, object]:
    if not archive.is_file():
        raise BackupValidationError("PostgreSQL backup file does not exist")
    size = archive.stat().st_size
    if size < 100 or size > MAX_BACKUP_BYTES:
        raise BackupValidationError("PostgreSQL backup size is outside the supported range")
    directory = work_dir or archive.parent
    toc = _run_pg_tool(
        ["pg_restore", "--list", str(archive)], database_url, password_file, directory
    )
    metadata = validate_archive_toc(
        toc,
        allowed_structure=allowed_structure,
        allow_unresolved_structure=trusted_prevalidated,
    )
    return {"size_bytes": size, "sha256": _sha256(archive), **metadata}


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


def validate_postgres_restore(
    archive: Path,
    target_url: str,
    maintenance_url: str,
    *,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, object]:
    """Restore an allowlisted archive into a disposable database and inspect it."""
    maintenance_password_file = _require_distinct_maintenance_secret(
        password_file, maintenance_password_file
    )
    paths = postgres_paths(base_dir)
    metadata = inspect_postgres_archive(
        archive,
        target_url,
        password_file=password_file,
        work_dir=paths.backups,
        trusted_prevalidated=True,
    )
    validation_database = "odin_validate_" + uuid.uuid4().hex[:20]
    if not _SAFE_DATABASE_NAME.fullmatch(validation_database):
        raise AssertionError("Generated unsafe validation database name")
    _maintenance_cli, validation_cli = _maintenance_urls(
        maintenance_url, target_url, validation_database
    )
    maintenance_engine = create_database_engine(
        maintenance_url,
        role="restore",
        password_file=maintenance_password_file,
    ).execution_options(isolation_level="AUTOCOMMIT")
    created = False
    canonical_archive: Path | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{validation_database}"')
            created = True
        validation_url = (
            make_url(maintenance_url)
            .set(database=validation_database)
            .render_as_string(hide_password=True)
        )
        validation_engine = create_database_engine(
            validation_url,
            role="restore",
            password_file=maintenance_password_file,
        )
        try:
            canonical_result = bootstrap_database(validation_engine)
            canonical_fingerprint = str(canonical_result["schema_fingerprint"])
        finally:
            validation_engine.dispose()

        descriptor, canonical_name = tempfile.mkstemp(
            prefix=".odin-canonical-schema-", suffix=".dump", dir=paths.backups
        )
        os.close(descriptor)
        canonical_archive = Path(canonical_name)
        canonical_command = [
            "pg_dump",
            "--format=custom",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--no-comments",
            "--file",
            str(canonical_archive),
            "--dbname",
            validation_cli,
        ]
        for table_name in sorted(canonical_tables()):
            canonical_command.extend(["--table", f"public.{table_name}"])
        _run_pg_tool(
            canonical_command,
            maintenance_url,
            maintenance_password_file,
            paths.backups,
        )
        canonical_toc = _run_pg_tool(
            ["pg_restore", "--list", str(canonical_archive)],
            maintenance_url,
            maintenance_password_file,
            paths.backups,
        )
        validate_archive_toc(canonical_toc, allow_unresolved_structure=True)
        allowed_structure = _toc_inventory(canonical_toc).structure
        metadata = inspect_postgres_archive(
            archive,
            target_url,
            password_file=password_file,
            work_dir=paths.backups,
            allowed_structure=allowed_structure,
        )
        restore_list = _validated_restore_list(
            archive,
            target_url,
            password_file,
            paths.backups,
            allowed_structure=allowed_structure,
        )
        try:
            _run_pg_tool(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "--use-list",
                    str(restore_list),
                    "--dbname",
                    validation_cli,
                    str(archive),
                ],
                maintenance_url,
                maintenance_password_file,
                paths.backups,
            )
        finally:
            restore_list.unlink(missing_ok=True)
        validation_engine = create_database_engine(
            validation_url,
            role="restore",
            password_file=maintenance_password_file,
        )
        try:
            with validation_engine.connect() as connection:
                validate_schema(connection)
                if schema_fingerprint(connection) != canonical_fingerprint:
                    raise BackupValidationError(
                        "Restored archive schema differs from the canonical ODIN schema"
                    )
                inspector = inspect(connection)
                actual_tables = set(inspector.get_table_names(schema="public"))
                if actual_tables != set(canonical_tables()):
                    raise BackupValidationError("Restored archive has noncanonical public tables")
                if inspector.get_view_names(schema="public"):
                    raise BackupValidationError("Restored archive contains public views")
                function_count = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public'"
                    )
                ).scalar_one()
                trigger_count = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='public' AND NOT t.tgisinternal"
                    )
                ).scalar_one()
                if function_count or trigger_count:
                    raise BackupValidationError("Restored archive contains executable public objects")
        finally:
            validation_engine.dispose()
    finally:
        if canonical_archive is not None:
            canonical_archive.unlink(missing_ok=True)
        if created:
            with maintenance_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{validation_database}" WITH (FORCE)'
                )
        maintenance_engine.dispose()
    return {
        **metadata,
        "schema_fingerprint": canonical_fingerprint,
        "restore_validated": True,
    }


def stage_postgres_restore(
    candidate: Path,
    target_url: str,
    maintenance_url: str,
    *,
    actor_id: int | None = None,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, object]:
    paths = postgres_paths(base_dir)
    paths.backups.mkdir(parents=True, exist_ok=True)
    metadata = validate_postgres_restore(
        candidate,
        target_url,
        maintenance_url,
        password_file=password_file,
        maintenance_password_file=maintenance_password_file,
        base_dir=base_dir,
    )
    audit_engine = create_database_engine(
        target_url,
        role="backup",
        password_file=password_file,
    )
    try:
        with audit_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_logs (action, entity_type, details) "
                    "VALUES ('restore_staged', 'system', :details)"
                ),
                {
                    "details": json.dumps(
                        {
                            "actor_id": actor_id,
                            "candidate_sha256": metadata["sha256"],
                            "restart_required": True,
                        },
                        sort_keys=True,
                    )
                },
            )
    finally:
        audit_engine.dispose()
    with restore_lock(paths.lock):
        descriptor, filename = tempfile.mkstemp(
            prefix="restore-pending-", suffix=".dump", dir=paths.pending.parent
        )
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as output, candidate.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            _secure(temporary)
            if _sha256(temporary) != metadata["sha256"]:
                raise BackupValidationError("Staged PostgreSQL backup checksum changed")
            os.replace(temporary, paths.pending)
            manifest = {
                "schema_version": 1,
                "dialect": "postgresql",
                "state": "staged",
                "sha256": metadata["sha256"],
                "size_bytes": metadata["size_bytes"],
                "actor_id": actor_id,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest_tmp = paths.manifest.with_suffix(".json.tmp")
            manifest_tmp.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            _secure(manifest_tmp)
            os.replace(manifest_tmp, paths.manifest)
            _fsync_directory(paths.pending.parent)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "restart_required": True,
        "candidate_sha256": metadata["sha256"],
        "restore_validated": True,
    }


def _read_pending_manifest(paths: PostgreSQLBackupPaths) -> dict[str, object]:
    try:
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupValidationError("Pending PostgreSQL restore manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("dialect") != "postgresql"
        or manifest.get("state") not in {"staged", "restore_in_progress"}
    ):
        raise BackupValidationError("Pending PostgreSQL restore manifest is unsupported")
    return manifest


def _pending_rollback_path(
    paths: PostgreSQLBackupPaths, manifest: dict[str, object]
) -> Path:
    name = manifest.get("rollback_backup")
    checksum = manifest.get("rollback_sha256")
    size = manifest.get("rollback_size_bytes")
    if (
        not isinstance(name, str)
        or not _SAFE_ROLLBACK_NAME.fullmatch(name)
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        or not isinstance(size, int)
        or size < 100
        or size > MAX_BACKUP_BYTES
    ):
        raise BackupValidationError(
            "Interrupted PostgreSQL restore has invalid rollback metadata"
        )
    rollback = (paths.backups / name).resolve()
    if rollback.parent != paths.backups.resolve() or not rollback.is_file():
        raise BackupValidationError(
            "Interrupted PostgreSQL restore rollback archive is unavailable"
        )
    if rollback.stat().st_size != size or _sha256(rollback) != checksum:
        raise BackupValidationError(
            "Interrupted PostgreSQL restore rollback archive changed"
        )
    return rollback


def _write_pending_manifest(
    paths: PostgreSQLBackupPaths, manifest: dict[str, object]
) -> None:
    temporary = paths.manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    _secure(temporary)
    os.replace(temporary, paths.manifest)
    _fsync_directory(paths.manifest.parent)


def _assert_no_target_sessions(
    connection,
    database_name: str,
    *,
    excluded_pids: frozenset[int] = frozenset(),
) -> None:
    sessions = connection.execute(
        text(
            "SELECT pid, COALESCE(application_name, ''), COALESCE(client_addr::text, 'local') "
            "FROM pg_stat_activity WHERE datname=:database_name AND pid<>pg_backend_pid()"
        ),
        {"database_name": database_name},
    ).all()
    sessions = [row for row in sessions if int(row[0]) not in excluded_pids]
    if sessions:
        raise BackupValidationError(
            f"Refusing PostgreSQL restore while {len(sessions)} target database session(s) are active"
        )


def _assert_no_external_dependencies(connection) -> None:
    """Reject noncanonical objects that depend on canonical ODIN relations."""
    # Dependency catalogs live in the target database, so this check uses a
    # short-lived target connection created by the caller after session refusal.
    dependencies = connection.execute(
        text(
            "WITH target_relations AS ("
            " SELECT c.oid, c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname='public' AND c.relname = ANY(:table_names)"
            "), target_types AS ("
            " SELECT t.oid, t.typname FROM pg_type t"
            " JOIN pg_namespace n ON n.oid=t.typnamespace"
            " WHERE n.nspname='public' AND t.typname = ANY(:type_names)"
            "), target_sequences AS ("
            " SELECT sequence.oid, sequence.relname FROM pg_class sequence"
            " JOIN pg_namespace sn ON sn.oid=sequence.relnamespace"
            " JOIN pg_depend owned ON owned.objid=sequence.oid"
            "  AND owned.classid='pg_class'::regclass"
            "  AND owned.refclassid='pg_class'::regclass"
            "  AND owned.deptype IN ('a', 'i')"
            " JOIN target_relations target ON target.oid=owned.refobjid"
            " WHERE sn.nspname='public' AND sequence.relkind='S'"
            "), dependencies AS ("
            " SELECT format('relation:%I.%I', dn.nspname, dependent.relname) AS identity"
            " FROM target_relations target"
            " JOIN pg_depend d ON d.refobjid=target.oid"
            "  AND d.classid='pg_rewrite'::regclass"
            " JOIN pg_rewrite rewrite ON rewrite.oid=d.objid"
            " JOIN pg_class dependent ON dependent.oid=rewrite.ev_class"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('constraint:%I.%I:%I', dn.nspname, dependent.relname, constraint_row.conname)"
            " FROM target_relations target"
            " JOIN pg_constraint constraint_row ON constraint_row.confrelid=target.oid"
            "  AND constraint_row.contype='f'"
            " JOIN pg_class dependent ON dependent.oid=constraint_row.conrelid"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('function:%I.%I', pn.nspname, procedure.proname)"
            " FROM target_relations target"
            " JOIN pg_depend d ON d.refobjid=target.oid"
            "  AND d.classid='pg_proc'::regclass"
            " JOIN pg_proc procedure ON procedure.oid=d.objid"
            " JOIN pg_namespace pn ON pn.oid=procedure.pronamespace"
            " WHERE pn.nspname NOT IN ('pg_catalog', 'information_schema')"
            " UNION"
            " SELECT format('inheritance:%I.%I', cn.nspname, child.relname)"
            " FROM target_relations target"
            " JOIN pg_inherits inheritance ON inheritance.inhparent=target.oid"
            " JOIN pg_class child ON child.oid=inheritance.inhrelid"
            " JOIN pg_namespace cn ON cn.oid=child.relnamespace"
            " WHERE NOT (cn.nspname='public' AND child.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('type-use:%I.%I', dn.nspname, dependent.relname)"
            " FROM target_types target_type"
            " JOIN pg_depend d ON d.refobjid=target_type.oid"
            "  AND d.refclassid='pg_type'::regclass"
            "  AND d.classid='pg_class'::regclass"
            " JOIN pg_class dependent ON dependent.oid=d.objid"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('sequence-use:%I.%I', dn.nspname, dependent.relname)"
            " FROM target_sequences target_sequence"
            " JOIN pg_depend d ON d.refobjid=target_sequence.oid"
            "  AND d.refclassid='pg_class'::regclass"
            "  AND d.classid='pg_attrdef'::regclass"
            " JOIN pg_attrdef attribute_default ON attribute_default.oid=d.objid"
            " JOIN pg_class dependent ON dependent.oid=attribute_default.adrelid"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            ") SELECT identity FROM dependencies ORDER BY identity LIMIT 13"
        ),
        {
            "table_names": sorted(canonical_tables()),
            "type_names": sorted(canonical_enum_types()),
        },
    ).scalars().all()
    if dependencies:
        visible = dependencies[:12]
        suffix = ", …" if len(dependencies) > 12 else ""
        raise BackupValidationError(
            "PostgreSQL restore is blocked by non-ODIN object dependencies: "
            + ", ".join(visible)
            + suffix
        )


def _restore_postgres_archive(
    archive: Path,
    target_url: str,
    password_file: str | None,
    directory: Path,
) -> None:
    restore_list = _validated_restore_list(
        archive,
        target_url,
        password_file,
        directory,
        trusted_prevalidated=True,
    )
    try:
        _run_pg_tool(
            [
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--use-list",
                str(restore_list),
                "--dbname",
                _credentialless_cli_url(target_url, "odin-restore"),
                str(archive),
            ],
            target_url,
            password_file,
            directory,
        )
    finally:
        restore_list.unlink(missing_ok=True)


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
            connection.execute(
                text(
                    "INSERT INTO audit_logs (action, entity_type, details) "
                    "VALUES (:action, 'system', :details)"
                ),
                {"action": action, "details": json.dumps(details, sort_keys=True)},
            )
    finally:
        engine.dispose()


def apply_pending_postgres_restore(
    target_url: str,
    maintenance_url: str,
    *,
    acknowledgement: str | None,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: Path | None = None,
    fail_after_restore: bool = False,
) -> dict[str, object] | None:
    """Apply, bootstrap, validate, finalize, or roll back a staged archive offline."""
    maintenance_password_file = _require_distinct_maintenance_secret(
        password_file, maintenance_password_file
    )
    paths = postgres_paths(base_dir)
    if not paths.pending.exists() and not paths.manifest.exists():
        return None
    if not paths.manifest.is_file():
        raise BackupValidationError("Pending PostgreSQL restore pair is incomplete")
    manifest = _read_pending_manifest(paths)
    state = manifest["state"]
    candidate: dict[str, object] | None = None
    if state == "staged":
        if not paths.pending.is_file():
            raise BackupValidationError("Pending PostgreSQL restore pair is incomplete")
        if acknowledgement != manifest.get("sha256"):
            raise BackupValidationError(
                "PostgreSQL restore acknowledgement must exactly match the staged SHA-256"
            )
        candidate = inspect_postgres_archive(
            paths.pending,
            target_url,
            password_file=password_file,
            work_dir=paths.backups,
            trusted_prevalidated=True,
        )
        if (
            candidate["sha256"] != manifest.get("sha256")
            or candidate["size_bytes"] != manifest.get("size_bytes")
        ):
            raise BackupValidationError(
                "Pending PostgreSQL restore does not match its manifest"
            )

    target = make_url(target_url)
    maintenance = make_url(maintenance_url)
    if not target.database or maintenance.database == target.database:
        raise BackupValidationError("Maintenance URL must target a different database")
    maintenance_engine = create_database_engine(
        maintenance_url,
        role="restore",
        password_file=maintenance_password_file,
    )
    barrier_engine = create_database_engine(
        target_url,
        role="restore",
        password_file=password_file,
    )
    barrier_connection = None
    barrier_pid: int | None = None
    rollback: Path | None = None
    try:
        with maintenance_engine.connect() as maintenance_connection:
            _assert_no_target_sessions(maintenance_connection, target.database)
            barrier_connection = barrier_engine.connect()
            barrier_pid = int(
                barrier_connection.execute(text("SELECT pg_backend_pid()"))
                .scalar_one()
            )
            barrier_connection.commit()
            barrier_connection.execute(text("SET lock_timeout = '5s'"))
            barrier_connection.commit()
            lock_held = False
            try:
                try:
                    barrier_connection.execute(
                        text("SELECT pg_advisory_lock(:key)"),
                        {"key": RESTORE_ADVISORY_LOCK},
                    )
                    barrier_connection.commit()
                    lock_held = True
                except SQLAlchemyError as exc:
                    barrier_connection.rollback()
                    raise BackupValidationError(
                        "PostgreSQL restore could not establish the offline application barrier"
                    ) from exc
                excluded_pids = frozenset({barrier_pid})
                _assert_no_target_sessions(
                    maintenance_connection,
                    target.database,
                    excluded_pids=excluded_pids,
                )
                if state == "restore_in_progress":
                    rollback = _pending_rollback_path(paths, manifest)
                    rollback_metadata = inspect_postgres_archive(
                        rollback,
                        target_url,
                        password_file=password_file,
                        work_dir=paths.backups,
                        trusted_prevalidated=True,
                    )
                    if (
                        rollback_metadata["sha256"]
                        != manifest.get("rollback_sha256")
                        or rollback_metadata["size_bytes"]
                        != manifest.get("rollback_size_bytes")
                    ):
                        raise BackupValidationError(
                            "Interrupted PostgreSQL restore rollback archive is invalid"
                        )
                    with restore_lock(paths.lock):
                        _assert_no_target_sessions(
                            maintenance_connection,
                            target.database,
                            excluded_pids=excluded_pids,
                        )
                        _restore_postgres_archive(
                            rollback, target_url, password_file, paths.backups
                        )
                        validate_live_postgres_objects(
                            target_url, password_file=password_file
                        )
                        _insert_postgres_restore_audit(
                            target_url,
                            password_file,
                            "restore_failed",
                            {
                                "reason": "unclean_restore_interruption",
                                "recovered_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        paths.pending.unlink(missing_ok=True)
                        paths.manifest.unlink(missing_ok=True)
                        _fsync_directory(paths.manifest.parent)
                    return {
                        "status": "rolled_back_after_interruption",
                        "rollback_backup": rollback.name,
                    }

                assert candidate is not None
                target_engine = create_database_engine(
                    target_url, role="restore", password_file=password_file
                )
                try:
                    with target_engine.connect() as target_connection:
                        _assert_no_external_dependencies(target_connection)
                finally:
                    target_engine.dispose()
                _assert_no_target_sessions(
                    maintenance_connection,
                    target.database,
                    excluded_pids=excluded_pids,
                )
                rollback, rollback_metadata = create_postgres_backup(
                    target_url,
                    maintenance_url=maintenance_url,
                    password_file=password_file,
                    maintenance_password_file=maintenance_password_file,
                    base_dir=base_dir,
                    prefix="rollback_",
                )
                with restore_lock(paths.lock):
                    _assert_no_target_sessions(
                        maintenance_connection,
                        target.database,
                        excluded_pids=excluded_pids,
                    )
                    manifest.update(
                        {
                            "state": "restore_in_progress",
                            "rollback_backup": rollback.name,
                            "rollback_sha256": rollback_metadata["sha256"],
                            "rollback_size_bytes": rollback_metadata["size_bytes"],
                            "apply_started_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    _write_pending_manifest(paths, manifest)
                    try:
                        _restore_postgres_archive(
                            paths.pending, target_url, password_file, paths.backups
                        )
                        if fail_after_restore:
                            raise RuntimeError("Injected post-restore failure")
                        bootstrap_engine = create_database_engine(
                            target_url, role="restore", password_file=password_file
                        )
                        try:
                            bootstrap_database(bootstrap_engine)
                        finally:
                            bootstrap_engine.dispose()
                        validate_live_postgres_objects(
                            target_url, password_file=password_file
                        )
                        _insert_postgres_restore_audit(
                            target_url,
                            password_file,
                            "restore_completed",
                            {
                                "candidate_sha256": candidate["sha256"],
                                "restored_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                    except Exception as exc:
                        if rollback is not None:
                            _restore_postgres_archive(
                                rollback, target_url, password_file, paths.backups
                            )
                            validate_live_postgres_objects(
                                target_url, password_file=password_file
                            )
                            _insert_postgres_restore_audit(
                                target_url,
                                password_file,
                                "restore_failed",
                                {
                                    "reason": type(exc).__name__,
                                    "failed_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                        paths.pending.unlink(missing_ok=True)
                        paths.manifest.unlink(missing_ok=True)
                        _fsync_directory(paths.manifest.parent)
                        raise
                    paths.pending.unlink(missing_ok=True)
                    paths.manifest.unlink(missing_ok=True)
                    _fsync_directory(paths.manifest.parent)
                    return {
                        "status": "restored",
                        "rollback_backup": rollback.name,
                        "candidate_sha256": candidate["sha256"],
                    }
            finally:
                if lock_held:
                    barrier_connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": RESTORE_ADVISORY_LOCK},
                    )
                    barrier_connection.commit()
    finally:
        if barrier_connection is not None:
            barrier_connection.close()
        barrier_engine.dispose()
        maintenance_engine.dispose()
