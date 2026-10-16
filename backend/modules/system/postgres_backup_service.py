"""Fail-closed PostgreSQL backup creation and pre-restore validation."""

from __future__ import annotations

import hashlib
import json
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
    """A pg_dump/pg_restore run failed. ``connects`` says whether it talked to a server."""

    def __init__(self, message: str, *, connects: bool):
        super().__init__(message)
        self.connects = connects


def _as_archive_refusal(error: BaseException) -> BaseException:
    """Classify an error raised while the archive itself is being validated."""
    from core.schema.migrator import MigrationError
    if isinstance(error, (ArchiveRefused, RestoreEnvironmentError, RestoreAcknowledgementRequired)):
        return error
    if isinstance(error, PostgreSQLClientFailed):
        if error.connects:
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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PostgreSQLClientFailed(
            f"PostgreSQL client operation failed: {Path(command[0]).name}",
            connects=any(item == "--dbname" or item.startswith("--dbname=") for item in command),
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
    descriptors: set[str] = set()
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
        if descriptor in descriptors:
            raise BackupValidationError('PostgreSQL archive contains duplicate object entries')
        descriptors.add(descriptor)
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
    _contract=None,
) -> dict[str, object]:
    """Reject archive objects outside the canonical ODIN public-schema set."""
    tables = (canonical_tables() if _contract is None else canonical_tables(_contract=_contract))
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
        elif kind == "FUNCTION":
            from modules.system.postgres_canonical_executables import canonical_functions
            match = re.search(r" FUNCTION public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in {name+'()' for name in (canonical_functions() if _contract is None else canonical_functions(_contract=_contract))}:
                raise BackupValidationError("PostgreSQL archive contains a forbidden object type")
        elif kind == "TRIGGER":
            from modules.system.postgres_canonical_executables import canonical_triggers
            match = re.search(r" TRIGGER public ([^ ]+) ([^ ]+) ", f" {line} ")
            if not match or tuple(match.groups()) not in (canonical_triggers() if _contract is None else canonical_triggers(_contract=_contract)):
                raise BackupValidationError("PostgreSQL archive contains a forbidden object type")
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
    elif {entry for entry in inventory.structure if not entry.startswith(('FUNCTION public ', 'TYPE public '))} != {entry for entry in allowed_structure if not entry.startswith(('FUNCTION public ', 'TYPE public '))}:
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


def _ordered_restore_data(lines, expected_tables, foreign_keys):
    """Retain verified TOC records; order COPY by trusted target constraints."""
    tables={};sequences=[]
    for line in lines:
        match=re.fullmatch(r'\d+;\s+\d+\s+\d+\s+(TABLE DATA|SEQUENCE SET) public ([^ ]+) [^ ]+',line.strip())
        if not match:raise BackupValidationError('Restore data TOC entry is invalid')
        kind,name=match.groups()
        if kind=='SEQUENCE SET':sequences.append(line);continue
        if name not in expected_tables or name in tables:
            raise BackupValidationError('Restore table data is foreign or duplicated')
        tables[name]=line
    if set(tables)!=set(expected_tables):raise BackupValidationError('Restore table data is missing')
    parents={name:set() for name in tables}
    for child,parent,deferrable,deferred in foreign_keys:
        if child not in tables or parent not in tables:
            raise BackupValidationError('Restore foreign key parent is missing or foreign')
        if child==parent or (deferrable and deferred):continue
        parents[child].add(parent)
    ordered=[]
    while parents:
        ready=sorted(name for name,dependencies in parents.items() if not dependencies)
        if not ready:raise BackupValidationError('Restore immediate foreign key cycle is unsupported')
        # One lexical choice per iteration gives deterministic table-name ties.
        name=ready[0];ordered.append(tables[name]);del parents[name]
        for dependencies in parents.values():dependencies.discard(name)
    return ordered+sequences


def _trusted_restore_foreign_keys(connection):
    return connection.execute(text("""SELECT child.relname,CASE WHEN pn.nspname='public' THEN parent.relname ELSE pn.nspname||'.'||parent.relname END,k.condeferrable,k.condeferred
        FROM pg_constraint k JOIN pg_class child ON child.oid=k.conrelid
        JOIN pg_namespace cn ON cn.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=k.confrelid
        JOIN pg_namespace pn ON pn.oid=parent.relnamespace
        WHERE k.contype='f' AND cn.nspname='public'
        ORDER BY child.relname,parent.relname,k.conname""")).all()


def _validated_restore_list(
    archive: Path,
    database_url: str,
    password_file: str | None,
    directory: Path,
    *,
    allowed_structure: frozenset[str] | None = None,
    trusted_prevalidated: bool = False,
    phase: str = 'data',
    schema_descriptors: frozenset[str] | None = None,
    data_connection=None,
    _contract=None,
) -> Path:
    toc = _run_pg_tool(
        ["pg_restore", "--list", str(archive)], database_url, password_file, directory
    )
    validate_archive_toc(
        toc,
        allowed_structure=allowed_structure,
        allow_unresolved_structure=trusted_prevalidated,
        _contract=_contract,
    )
    if phase=='data' and data_connection is None:
        raise BackupValidationError('Restore data requires validated canonical target metadata')
    selected=[]
    descriptor, filename = tempfile.mkstemp(prefix=".odin-restore-list-", dir=directory)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if phase not in ('data', 'schema-evidence'):
                raise BackupValidationError('Unsupported restore phase')
            for line in toc.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(';'):
                    continue
                if " SCHEMA - public " in f" {line} ":
                    continue
                match = re.match(r'^\d+;\s+\d+\s+\d+\s+(.+)$', stripped)
                if not match:
                    raise BackupValidationError('PostgreSQL archive TOC entry is malformed')
                descriptor = match.group(1).rpartition(' ')[0]
                kind = _toc_kind(descriptor)
                if phase == 'data':
                    if kind not in ('TABLE DATA', 'SEQUENCE SET'):
                        continue
                elif kind in ('TABLE DATA', 'SEQUENCE SET') or (schema_descriptors is not None and descriptor not in schema_descriptors):
                    continue
                selected.append(line)
            if phase=='data':
                selected=_ordered_restore_data(selected,(canonical_tables() if _contract is None else canonical_tables(_contract=_contract)),_trusted_restore_foreign_keys(data_connection))
            handle.write(''.join(line+'\n' for line in selected))
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def inspect_postgres_archive(
    archive: Path,
    database_url: str,
    *,
    password_file: str | None = None,
    work_dir: Path | None = None,
    allowed_structure: frozenset[str] | None = None,
    trusted_prevalidated: bool = False,
    _contract=None,
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
        _contract=_contract,
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


def _validate_current_postgres_restore(
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
    from modules.system import restore_artifacts
    restore_artifacts.record(paths.backups, validation_database)
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
            with validation_engine.connect() as connection:
                canonical_manifest = schema_manifest(connection)
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
        canonical_command.extend(['--schema', 'public'])
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
        from modules.system.postgres_canonical_executables import validate_schema_sql
        original_toc = _run_pg_tool(['pg_restore', '--list', str(archive)], target_url, password_file, paths.backups)
        original_structure = _toc_inventory(original_toc).structure
        original_schema = _schema_sql_evidence(archive, target_url, password_file, paths.backups,
            allowed_structure=allowed_structure)
        canonical_schema = _schema_sql_evidence(canonical_archive, maintenance_url, maintenance_password_file,
            paths.backups, schema_descriptors=original_structure)
        validate_schema_sql(original_schema, canonical_schema)
        # Only data enters a canonical source-created schema. Archive DDL is never executed.
        _restore_postgres_archive(archive, validation_url, maintenance_password_file, paths.backups)
        validation_engine = create_database_engine(
            validation_url,
            role="restore",
            password_file=maintenance_password_file,
        )
        try:
            with validation_engine.connect() as connection:
                validate_schema(connection)
                restored_manifest = schema_manifest(connection)
                if schema_fingerprint(connection) != canonical_fingerprint:
                    differing_tables = sorted(
                        table_name
                        for table_name in set(canonical_manifest)
                        | set(restored_manifest)
                        if canonical_manifest.get(table_name)
                        != restored_manifest.get(table_name)
                    )[:12]
                    differing_components: dict[str, list[str]] = {}
                    for table_name in differing_tables:
                        canonical_table = canonical_manifest.get(table_name, {})
                        restored_table = restored_manifest.get(table_name, {})
                        if not isinstance(canonical_table, dict) or not isinstance(
                            restored_table, dict
                        ):
                            differing_components[table_name] = ["table"]
                            continue
                        differing_components[table_name] = sorted(
                            key
                            for key in set(canonical_table) | set(restored_table)
                            if canonical_table.get(key) != restored_table.get(key)
                        )
                    raise BackupValidationError(
                        "Restored archive schema differs from the canonical ODIN schema; "
                        f"components={differing_components}"
                    )
                inspector = inspect(connection)
                actual_tables = set(inspector.get_table_names(schema="public"))
                if actual_tables != set(canonical_tables()):
                    raise BackupValidationError("Restored archive has noncanonical public tables")
                if inspector.get_view_names(schema="public"):
                    raise BackupValidationError("Restored archive contains public views")
                _validate_canonical_public_executables(connection)
        finally:
            validation_engine.dispose()
    finally:
        if canonical_archive is not None:
            canonical_archive.unlink(missing_ok=True)
        if created:  # Only a database this code created.
            with maintenance_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{validation_database}" WITH (FORCE)'
                )
        restore_artifacts.forget(paths.backups, validation_database)
        maintenance_engine.dispose()
    return {
        **metadata,
        "schema_fingerprint": canonical_fingerprint,
        "restore_validated": True,
    }


def _inspect_compatible_archive(archive,target_url,password_file,directory):
    from modules.system.pre026_restore_stage import identify_contract,archive_metadata
    if identify_contract(archive,target_url,password_file,directory) is None:
        return inspect_postgres_archive(archive,target_url,password_file=password_file,work_dir=directory,trusted_prevalidated=True)
    # Historical archives are validated in full by the preserved029 stage in prepared_archive.
    return archive_metadata(archive)


def validate_postgres_restore(archive,target_url,maintenance_url,*,password_file=None,maintenance_password_file=None,base_dir=None):
    """Validate an archive; a migration refusal is an actionable validation error, not a 500."""
    from core.schema.migrator import MigrationError
    try:
        return _validate_postgres_restore(archive,target_url,maintenance_url,password_file=password_file,maintenance_password_file=maintenance_password_file,base_dir=base_dir)
    except MigrationError as error:
        raise BackupValidationError('PostgreSQL archive cannot be upgraded by this ODIN version: '+str(error)[:300]) from error


def _validate_postgres_restore(archive,target_url,maintenance_url,*,password_file=None,maintenance_password_file=None,base_dir=None):
    from modules.system.pre026_restore_stage import identify_contract,prepared_archive,archive_metadata,current_schema_version
    from modules.system import restore_artifacts
    directory=postgres_paths(base_dir).backups
    restore_artifacts.sweep_stale(directory,maintenance_url,maintenance_password_file)
    contract=identify_contract(archive,target_url,password_file,directory)
    if contract is None:
        return _validate_current_postgres_restore(archive,target_url,maintenance_url,password_file=password_file,maintenance_password_file=maintenance_password_file,base_dir=base_dir)
    original=archive_metadata(archive);era=[]
    with prepared_archive(archive,target_url,maintenance_url,password_file,maintenance_password_file,directory,_era=era) as prepared:
        current=_validate_current_postgres_restore(prepared,target_url,maintenance_url,password_file=password_file,maintenance_password_file=maintenance_password_file,base_dir=base_dir)
        return {**original,'restore_validated':True,'schema_fingerprint':current['schema_fingerprint'],'source_schema_version':era[0],'prepared_schema_version':current_schema_version(),'upgrade_required':True,'prepared_sha256':_sha256(prepared)}


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


def _schema_sql_evidence(archive, database_url, password_file, directory, *, allowed_structure=None, schema_descriptors=None, _contract=None):
    listing = _validated_restore_list(archive, database_url, password_file, directory,
        allowed_structure=allowed_structure, trusted_prevalidated=True, phase='schema-evidence',
        schema_descriptors=schema_descriptors,_contract=_contract)
    try:
        # No --dbname: emitted SQL is evidence only, including untrusted executable definitions.
        return _run_pg_tool(['pg_restore', '--schema-only', '--no-owner', '--no-privileges',
            '--use-list', str(listing), '--file', '-', str(archive)], database_url, password_file, directory)
    finally:
        listing.unlink(missing_ok=True)


def _restore_postgres_archive(archive: Path, target_url: str, password_file: str | None, directory: Path, *, _contract=None, _stage=None) -> None:
    from core.schema.pre026_restore_contract import check_contract
    check_contract(_contract)
    if _contract is not None:
        if _stage is None or _stage.contract is not _contract:raise BackupValidationError("Owned historical stage profile differs")
        bound=_stage.verify().url;requested=make_url(target_url)
        if (bound.host,bound.port or 5432,bound.username,bound.database)!=(requested.host,requested.port or 5432,requested.username,requested.database):raise BackupValidationError("Owned025 stage target differs")
    from modules.system.postgres_canonical_executables import remove_trusted_guards, install_trusted_guards
    restore_list = None
    engine = create_database_engine(target_url, role='restore', password_file=password_file)
    try:
        # Existing offline barrier spans these phases. This is not one transaction.
        with engine.begin() as connection:
            if _contract is not None:_stage.verify_connection(connection)
            validate_schema(connection,_contract=_contract)
            if set(inspect(connection).get_table_names(schema='public')) != set((canonical_tables() if _contract is None else canonical_tables(_contract=_contract))):
                raise BackupValidationError('Restore target schema has unsupported historical compatibility')
            restore_list = _validated_restore_list(archive, target_url, password_file, directory,
                trusted_prevalidated=True, data_connection=connection,_contract=_contract)
            remove_trusted_guards(connection,_contract=_contract)
            names = ','.join('public."'+name+'"' for name in sorted((canonical_tables() if _contract is None else canonical_tables(_contract=_contract))))
            connection.exec_driver_sql('TRUNCATE '+names+' RESTART IDENTITY')
        _run_pg_tool(['pg_restore', '--exit-on-error', '--single-transaction', '--data-only',
            '--no-owner', '--no-privileges', '--use-list', str(restore_list), '--dbname',
            _credentialless_cli_url(target_url, 'odin-restore'), str(archive)], target_url, password_file, directory)
        with engine.begin() as connection:
            install_trusted_guards(connection,_contract=_contract)
        # Migration adoption/data associations are verified after guard installation.
        if _contract is None:bootstrap_database(engine)
    finally:
        engine.dispose()
        if restore_list is not None:restore_list.unlink(missing_ok=True)


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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    archive = paths.backups / f"failed_restore_{stamp}.dump"
    record_path = paths.backups / f"failed_restore_{stamp}.json"
    record = {
        "status": "restore_failed_target_untouched",
        "reason": reason,
        "error_type": type(error).__name__,
        "candidate_sha256": manifest.get("sha256"),
        "staged_at": manifest.get("staged_at"),
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "archive": archive.name if paths.pending.is_file() else None,
        "action": "The current database was not changed. Fix the reason above, then re-stage the archive "
                  "and restart with ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT set to its SHA-256.",
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
    try:
        _insert_postgres_restore_audit(target_url, password_file, "restore_failed",
            {"reason": reason, "candidate_sha256": manifest.get("sha256"), "target_untouched": True})
    except Exception:  # The record file and the returned status already carry the reason.
        pass
    return record


_QUARANTINED = re.compile(r"failed_restore_\d{8}_\d{6}_\d{6}\.(dump|json)")


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
    if target_url in _FAILED_RESTORE_BARRIERS:
        raise BackupValidationError('PostgreSQL recovery incomplete; this process retains the offline barrier')
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
            raise RestoreAcknowledgementRequired(
                "PostgreSQL restore acknowledgement must exactly match the staged SHA-256"
            )
        try:
            candidate = _inspect_compatible_archive(paths.pending,target_url,password_file,paths.backups)
            if (
                candidate["sha256"] != manifest.get("sha256")
                or candidate["size_bytes"] != manifest.get("size_bytes")
            ):
                raise BackupValidationError(
                    "Pending PostgreSQL restore does not match its manifest"
                )
        except BaseException as error:
            raise _as_archive_refusal(error)

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
    recovery_incomplete = False
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
                from modules.system.pre026_restore_stage import prepared_archive
                with _archive_phase(prepared_archive(paths.pending,target_url,maintenance_url,password_file,maintenance_password_file,paths.backups)) as prepared:
                    with restore_lock(paths.lock):
                        _assert_no_target_sessions(
                            maintenance_connection,
                            target.database,
                            excluded_pids=excluded_pids,
                        )
                        if _sha256(paths.pending)!=manifest['sha256'] or paths.pending.stat().st_size!=manifest['size_bytes']:raise BackupValidationError('Staged original archive changed')
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
                                prepared, target_url, password_file, paths.backups
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
            except BaseException as exc:
                if lock_held and paths.manifest.exists() and manifest.get('state') == 'restore_in_progress':
                    recovery_incomplete = True
                    _FAILED_RESTORE_BARRIERS[target_url] = (barrier_connection, barrier_engine)
                    manifest.update({'recovery_incomplete': True, 'recovery_error': type(exc).__name__})
                    _write_pending_manifest(paths, manifest)
                    raise BackupValidationError('PostgreSQL recovery incomplete; offline barrier retained in this process') from exc
                raise
            finally:
                if lock_held and not recovery_incomplete:
                    barrier_connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": RESTORE_ADVISORY_LOCK},
                    )
                    barrier_connection.commit()
    finally:
        if not recovery_incomplete:
            if barrier_connection is not None:
                barrier_connection.close()
            barrier_engine.dispose()
        maintenance_engine.dispose()
