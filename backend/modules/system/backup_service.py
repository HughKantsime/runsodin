"""Safe SQLite backup and offline restore primitives.

The HTTP process may create and stage backups, but only the container
entrypoint may replace the live database. This avoids rebinding a running
SQLAlchemy engine while monitor processes can still write to SQLite.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_BACKUP_BYTES = 100 * 1024 * 1024
MIN_BACKUP_FREE_BYTES = 16 * 1024 * 1024
REQUIRED_TABLES = frozenset(
    {
        "active_sessions",
        "api_tokens",
        "audit_logs",
        "groups",
        "jobs",
        "login_attempts",
        "models",
        "printers",
        "system_config",
        "token_blacklist",
        "users",
    }
)
REQUIRED_COLUMNS = {
    "users": frozenset(
        {"id", "username", "email", "password_hash", "role", "is_active", "group_id"}
    ),
    "api_tokens": frozenset(
        {"id", "user_id", "token_hash", "token_prefix", "scopes", "expires_at"}
    ),
    "active_sessions": frozenset(
        {"id", "user_id", "token_jti", "created_at", "last_seen_at"}
    ),
    "token_blacklist": frozenset({"jti", "expires_at"}),
    "login_attempts": frozenset(
        {"id", "ip", "username", "attempted_at", "success"}
    ),
    "audit_logs": frozenset(
        {"id", "timestamp", "action", "entity_type", "entity_id", "details"}
    ),
    "system_config": frozenset({"key", "value", "updated_at"}),
    "groups": frozenset({"id", "name", "is_org"}),
    "jobs": frozenset(
        {"id", "item_name", "quantity", "status", "submitted_by", "created_at"}
    ),
    "models": frozenset(
        {"id", "name", "build_time_hours", "color_requirements", "org_id"}
    ),
    "printers": frozenset(
        {"id", "name", "api_type", "is_active", "org_id", "shared"}
    ),
}


class BackupValidationError(ValueError):
    """Raised when a candidate is not a safe ODIN SQLite database."""


@dataclass(frozen=True)
class BackupPaths:
    database: Path
    backups: Path
    lock: Path
    pending: Path
    manifest: Path


def paths_from_database_url(database_url: str, base_dir: Path | None = None) -> BackupPaths:
    if not database_url.startswith("sqlite:///"):
        raise BackupValidationError("Backup operations require a SQLite database URL")
    raw_path = database_url[len("sqlite:///") :]
    if not raw_path or raw_path == ":memory:":
        raise BackupValidationError("Backup operations require a file-backed SQLite database")
    database = Path(raw_path)
    if not database.is_absolute():
        database = (base_dir or Path.cwd()) / database
    database = database.resolve()
    backups = database.parent / "backups"
    return BackupPaths(
        database=database,
        backups=backups,
        lock=database.parent / ".odin-restore.lock",
        pending=database.parent / "restore-pending.db",
        manifest=database.parent / "restore-pending.json",
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure(path: Path) -> None:
    path.chmod(0o600)
    _fsync_file(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_schema_sql(value: str | None) -> str:
    return " ".join(str(value or "").lower().split())


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


@contextlib.contextmanager
def restore_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        path.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_database(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise BackupValidationError("Backup file does not exist")
    size = path.stat().st_size
    if size < 100 or size > MAX_BACKUP_BYTES:
        raise BackupValidationError("Backup file size is outside the supported range")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise BackupValidationError("Backup does not have a SQLite header")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise BackupValidationError("Backup failed SQLite integrity checking")
            foreign_key_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_violation:
                raise BackupValidationError("Backup failed relational integrity checking")
            objects = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'trigger', 'view')"
            ).fetchall()
            tables = {name for kind, name, _, _ in objects if kind == "table"}
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                raise BackupValidationError(
                    "Backup is missing required ODIN tables: " + ", ".join(missing)
                )
            for table, required_columns in REQUIRED_COLUMNS.items():
                columns = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM pragma_table_info(?)", (table,)
                    ).fetchall()
                }
                missing_columns = sorted(required_columns - columns)
                if missing_columns:
                    raise BackupValidationError(
                        f"Backup table {table} is missing required columns: "
                        + ", ".join(missing_columns)
                    )
            if any(kind == "view" for kind, _, _, _ in objects):
                raise BackupValidationError("Backup contains unexpected triggers or views")
            for kind, name, table_name, statement in objects:
                if kind != "trigger":
                    continue
                if (
                    table_name != "education_audit_events"
                    or _ALLOWED_SQLITE_TRIGGERS.get(name)
                    != _normalized_schema_sql(statement)
                ):
                    raise BackupValidationError(
                        "Backup contains unexpected triggers or views"
                    )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupValidationError("Backup is not a readable SQLite database") from exc
    return {"size_bytes": size, "sha256": _sha256(path), "table_count": len(tables)}


def _create_online_backup_unlocked(paths: BackupPaths, prefix: str) -> tuple[Path, dict[str, object]]:
    if not paths.database.is_file():
        raise BackupValidationError("Live database does not exist")
    paths.backups.mkdir(parents=True, exist_ok=True)
    required_free = max(paths.database.stat().st_size * 2, MIN_BACKUP_FREE_BYTES)
    available = shutil.disk_usage(paths.backups).free
    if available < required_free:
        raise BackupValidationError(
            "Insufficient free disk space for a verified backup"
        )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    target = paths.backups / f"{prefix}{timestamp}.db"
    source = sqlite3.connect(str(paths.database))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    _secure(target)
    _fsync_directory(paths.backups)
    return target, validate_database(target)


def create_online_backup(database_url: str, base_dir: Path | None = None) -> tuple[Path, dict[str, object]]:
    paths = paths_from_database_url(database_url, base_dir)
    with restore_lock(paths.lock):
        return _create_online_backup_unlocked(paths, "odin_backup_")


def stage_restore(
    candidate: Path,
    database_url: str,
    base_dir: Path | None = None,
    *,
    actor_id: int | None = None,
) -> dict[str, object]:
    paths = paths_from_database_url(database_url, base_dir)
    candidate_metadata = validate_database(candidate)
    paths.backups.mkdir(parents=True, exist_ok=True)
    with restore_lock(paths.lock):
        # Commit the audit record before making a boot-consumable pending pair.
        # This closes the crash gap where a staged restore existed without a
        # durable record in the live database/pre-restore snapshot.
        _insert_restore_audit(
            paths.database,
            "restore_staged",
            {
                "candidate_sha256": candidate_metadata["sha256"],
                "actor_id": actor_id,
                "restart_required": True,
            },
        )
        pre_restore, pre_metadata = _create_online_backup_unlocked(paths, "pre_restore_")
        fd, temporary_name = tempfile.mkstemp(prefix="restore-pending-", suffix=".db", dir=paths.database.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output, candidate.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            validate_database(temporary)
            os.replace(temporary, paths.pending)
            manifest = {
                "schema_version": 1,
                "sha256": candidate_metadata["sha256"],
                "size_bytes": candidate_metadata["size_bytes"],
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest_tmp = paths.manifest.with_suffix(".json.tmp")
            manifest_tmp.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            _secure(manifest_tmp)
            os.replace(manifest_tmp, paths.manifest)
            _fsync_directory(paths.database.parent)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "restart_required": True,
        "pre_restore_backup": pre_restore.name,
        "pre_restore_sha256": pre_metadata["sha256"],
        "candidate_sha256": candidate_metadata["sha256"],
    }


def _assert_no_live_process(pid_file: Path | None) -> None:
    if not pid_file or not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError):
        return
    except PermissionError as exc:
        raise RuntimeError("Cannot prove the ODIN process is stopped") from exc
    raise RuntimeError("Refusing restore while an ODIN process is running")


def _remove_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def _insert_restore_audit(database: Path, action: str, details: dict[str, object]) -> None:
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "INSERT INTO audit_logs (action, entity_type, details) VALUES (?, ?, ?)",
            (action, "system", json.dumps(details, sort_keys=True)),
        )
        connection.commit()
    finally:
        connection.close()


def _discard_invalid_pending(paths: BackupPaths, reason: str) -> None:
    """Remove a non-consumable pending pair and audit against the live DB."""
    paths.pending.unlink(missing_ok=True)
    paths.manifest.unlink(missing_ok=True)
    if paths.database.is_file():
        try:
            validate_database(paths.database)
            _insert_restore_audit(
                paths.database,
                "restore_failed",
                {
                    "reason": reason,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            # Never mask the original restore error when even the preserved
            # live database cannot accept an audit record.
            pass
    _fsync_directory(paths.database.parent)


def _read_manifest(paths: BackupPaths) -> dict[str, object]:
    try:
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BackupValidationError("Pending restore manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise BackupValidationError("Unsupported restore manifest version")
    return manifest


def _write_manifest(paths: BackupPaths, manifest: dict[str, object]) -> None:
    manifest_tmp = paths.manifest.with_suffix(".json.tmp")
    manifest_tmp.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    _secure(manifest_tmp)
    os.replace(manifest_tmp, paths.manifest)
    _fsync_directory(paths.database.parent)


def _rollback_path(paths: BackupPaths, manifest: dict[str, object]) -> Path:
    rollback_name = manifest.get("rollback_backup")
    if not isinstance(rollback_name, str):
        raise BackupValidationError("Restore finalization marker has no rollback backup")
    candidate = Path(rollback_name)
    if (
        candidate.name != rollback_name
        or not rollback_name.startswith("rollback_")
        or candidate.suffix != ".db"
    ):
        raise BackupValidationError("Restore finalization marker has an invalid rollback backup")
    rollback = (paths.backups / candidate).resolve()
    if rollback.parent != paths.backups.resolve():
        raise BackupValidationError("Rollback backup is outside the backup directory")
    return rollback


def _rollback_applied_unlocked(
    paths: BackupPaths, manifest: dict[str, object], reason: str
) -> dict[str, object]:
    rollback = _rollback_path(paths, manifest)
    validate_database(rollback)
    paths.database.unlink(missing_ok=True)
    _remove_sidecars(paths.database)
    os.replace(rollback, paths.database)
    _secure(paths.database)
    _remove_sidecars(paths.database)
    validate_database(paths.database)
    _insert_restore_audit(
        paths.database,
        "restore_failed",
        {"reason": reason, "failed_at": datetime.now(timezone.utc).isoformat()},
    )
    paths.pending.unlink(missing_ok=True)
    paths.manifest.unlink(missing_ok=True)
    _fsync_directory(paths.database.parent)
    return {"status": "rolled_back", "reason": reason}


def apply_pending_restore(
    database_url: str,
    *,
    base_dir: Path | None = None,
    pid_file: Path | None = Path("/var/run/supervisord.pid"),
) -> dict[str, object] | None:
    """Swap in a staged restore, leaving it pending until startup finalizes it."""
    paths = paths_from_database_url(database_url, base_dir)
    if not paths.pending.exists() and not paths.manifest.exists():
        return None
    _assert_no_live_process(pid_file)
    with restore_lock(paths.lock):
        manifest = None
        if paths.manifest.is_file():
            try:
                manifest = _read_manifest(paths)
            except BackupValidationError:
                _discard_invalid_pending(paths, "invalid_manifest")
                raise
            state = manifest.get("state")
            if state == "prepared_swap":
                rollback = _rollback_path(paths, manifest)
                if rollback.exists():
                    # A process interruption occurred after the live DB was
                    # renamed. Whether or not the candidate reached the live
                    # path, restore the durably journaled original first.
                    return _rollback_applied_unlocked(
                        paths, manifest, "unclean_startup_during_swap"
                    )
            elif state == "applied_pending_validation":
                return _rollback_applied_unlocked(
                    paths, manifest, "unclean_startup_before_finalize"
                )
        if not paths.pending.is_file() or not paths.manifest.is_file():
            _discard_invalid_pending(paths, "incomplete_pending_pair")
            raise BackupValidationError("Pending restore database and manifest must both exist")
        try:
            manifest = manifest or _read_manifest(paths)
        except BackupValidationError:
            _discard_invalid_pending(paths, "invalid_manifest")
            raise
        try:
            candidate = validate_database(paths.pending)
            if manifest.get("sha256") != candidate["sha256"] or manifest.get("size_bytes") != candidate["size_bytes"]:
                raise BackupValidationError("Pending restore does not match its manifest")
        except Exception as exc:
            _discard_invalid_pending(paths, type(exc).__name__)
            raise
        validate_database(paths.database)
        checkpoint = sqlite3.connect(str(paths.database))
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            checkpoint.close()
        _remove_sidecars(paths.database)
        if manifest.get("state") == "prepared_swap":
            rollback = _rollback_path(paths, manifest)
        else:
            rollback = paths.backups / (
                "rollback_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".db"
            )
            manifest.update(
                {
                    "state": "prepared_swap",
                    "rollback_backup": rollback.name,
                    "prepared_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            # Journal and fsync the rollback location before the first rename.
            # Recovery can therefore find the original DB after interruption at
            # any point in the two-file swap.
            _write_manifest(paths, manifest)
        os.replace(paths.database, rollback)
        _secure(rollback)
        try:
            os.replace(paths.pending, paths.database)
            _secure(paths.database)
            _remove_sidecars(paths.database)
            validate_database(paths.database)
            manifest.update(
                {
                    "state": "applied_pending_validation",
                    "rollback_backup": rollback.name,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _write_manifest(paths, manifest)
            return {
                "status": "applied_pending_validation",
                "rollback_backup": rollback.name,
            }
        except Exception as exc:
            paths.database.unlink(missing_ok=True)
            _remove_sidecars(paths.database)
            os.replace(rollback, paths.database)
            _secure(paths.database)
            _remove_sidecars(paths.database)
            validate_database(paths.database)
            _insert_restore_audit(
                paths.database,
                "restore_failed",
                {"reason": type(exc).__name__, "failed_at": datetime.now(timezone.utc).isoformat()},
            )
            # The candidate has either been moved or is unusable. Remove both
            # halves of the pending pair so the next startup is not trapped by
            # a stale manifest.
            paths.pending.unlink(missing_ok=True)
            paths.manifest.unlink(missing_ok=True)
            _fsync_directory(paths.database.parent)
            raise


def finalize_pending_restore(
    database_url: str,
    *,
    base_dir: Path | None = None,
    pid_file: Path | None = Path("/var/run/supervisord.pid"),
) -> dict[str, object] | None:
    """Commit a swapped restore only after all startup database work succeeds."""
    paths = paths_from_database_url(database_url, base_dir)
    if not paths.manifest.exists():
        return None
    _assert_no_live_process(pid_file)
    with restore_lock(paths.lock):
        manifest = _read_manifest(paths)
        if manifest.get("state") != "applied_pending_validation":
            return None
        try:
            validate_database(paths.database)
            _insert_restore_audit(
                paths.database,
                "restore_completed",
                {"restored_at": datetime.now(timezone.utc).isoformat()},
            )
            validate_database(paths.database)
            paths.manifest.unlink(missing_ok=True)
            _fsync_directory(paths.database.parent)
            return {
                "status": "restored",
                "rollback_backup": manifest["rollback_backup"],
            }
        except Exception:
            _rollback_applied_unlocked(paths, manifest, "startup_finalization_failure")
            raise


def rollback_pending_restore(
    database_url: str,
    *,
    reason: str = "startup_migration_failure",
    base_dir: Path | None = None,
    pid_file: Path | None = Path("/var/run/supervisord.pid"),
) -> dict[str, object] | None:
    """Roll back an applied-but-unfinalized restore after startup failure."""
    paths = paths_from_database_url(database_url, base_dir)
    if not paths.manifest.exists():
        return None
    _assert_no_live_process(pid_file)
    with restore_lock(paths.lock):
        manifest = _read_manifest(paths)
        if manifest.get("state") != "applied_pending_validation":
            return None
        return _rollback_applied_unlocked(paths, manifest, reason)
