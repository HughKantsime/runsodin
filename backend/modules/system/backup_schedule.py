"""Scheduled SQLite backups: verified before they count, and pruned only after a good one.

ODIN never had a scheduler; every odin_backup_* before this came from someone asking for one.
Pruning only ever touches backups this module made, recognised by their receipt, and only runs
after a new backup of a database that passed its integrity check has itself been verified.
Manual backups, older files and anything else in the directory are left alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 -- only runs this interpreter on a fixed module
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from modules.system.backup_failure_alerts import report_backup_failure
from modules.system.backup_service import (
    BackupValidationError,
    _create_online_backup_unlocked,
    _fsync_directory,
    paths_from_database_url,
    require_healthy_source,
    restore_lock,
)
from modules.system.backup_verifier import verify_backup

log = logging.getLogger("odin.backup_schedule")

KEEP_DAILY = 7
KEEP_WEEKLY = 4
PARTIAL_PREFIX = "partial_scheduled_"
RECEIPT_SUFFIX = ".receipt.json"
BACKUP_NAME = re.compile(r"odin_backup_\d{8}_\d{6}_\d{6}\.db")
RESTORE_TEST_SECONDS = 600
_BACKEND = Path(__file__).resolve().parents[2]


def _sweep_partial(backups: Path) -> None:
    # Under the restore lock, so these can only be what a killed run left behind.
    for leftover in backups.glob(f"{PARTIAL_PREFIX}*"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)


def _live_fingerprint(database: Path) -> str:
    from core.schema import schema_fingerprint
    from core.schema.bootstrap import import_all_models
    import_all_models()
    engine = create_engine(f"sqlite:///file:{quote(str(database))}?mode=ro&uri=true", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return schema_fingerprint(connection)
    finally:
        engine.dispose()


def rehearse_restore(backup: Path, database: Path) -> None:
    """A real restore test, on a throwaway copy: the real startup command has to accept it.

    That runs the integrity check and every migration validator. The result has to need no
    migrations and match the live schema. Nothing here opens the live database for writing.
    """
    scratch = Path(tempfile.mkdtemp(prefix=PARTIAL_PREFIX, dir=backup.parent))
    try:
        copy = scratch / "odin.db"
        shutil.copyfile(backup, copy)
        started = subprocess.run(  # nosec B603 -- this interpreter, fixed arguments, no shell
            [sys.executable, "-m", "scripts.bootstrap_database"], cwd=_BACKEND, capture_output=True, text=True,
            timeout=RESTORE_TEST_SECONDS,
            env={**os.environ, "DATABASE_URL": f"sqlite:///{copy}", "ODIN_DB_ROLE": "bootstrap"},
        )
        if started.returncode != 0:
            raise BackupValidationError("Backup failed its restore test: startup refused it")
        try:
            result = json.loads(started.stdout.splitlines()[-1])
        except (IndexError, ValueError) as error:
            raise BackupValidationError("Backup failed its restore test: no startup result") from error
        if result.get("applied"):
            raise BackupValidationError("Backup failed its restore test: it needs migrations")
        if result.get("schema_fingerprint") != _live_fingerprint(database):
            raise BackupValidationError("Backup failed its restore test: schema differs from live")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _write_receipt(backup: Path, created_at: datetime, verified: dict[str, object]) -> None:
    receipt = backup.with_name(backup.name + RECEIPT_SUFFIX)
    temporary = receipt.with_name(receipt.name + ".tmp")
    temporary.write_text(json.dumps({
        "schema_version": 1,
        "created_at": created_at.isoformat(),
        "source_integrity": "ok",
        "sha256": verified["sha256"],
        "size_bytes": verified["size_bytes"],
    }, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, receipt)
    _fsync_directory(backup.parent)


def _scheduled_backups(backups: Path) -> list[tuple[datetime, Path]]:
    """Backups with a readable receipt, newest first. Without one, a file isn't ours to prune."""
    found = []
    for receipt in backups.glob(f"odin_backup_*.db{RECEIPT_SUFFIX}"):
        name = receipt.name[:-len(RECEIPT_SUFFIX)]
        if not BACKUP_NAME.fullmatch(name):
            continue
        backup = backups / name
        if not backup.is_file():
            receipt.unlink(missing_ok=True)  # An admin deleted the backup from Settings.
            continue
        try:
            created_at = datetime.fromisoformat(json.loads(receipt.read_text(encoding="utf-8"))["created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found.append((created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc), backup))
    return sorted(found, key=lambda item: item[0], reverse=True)


def retained_backups(scheduled: list[tuple[datetime, Path]]) -> set[Path]:
    """The newest per day for KEEP_DAILY days and the newest per ISO week for KEEP_WEEKLY weeks."""
    daily: dict[object, Path] = {}
    weekly: dict[object, Path] = {}
    for created_at, backup in scheduled:
        daily.setdefault(created_at.date(), backup)
        weekly.setdefault(tuple(created_at.isocalendar())[:2], backup)
    return set(list(daily.values())[:KEEP_DAILY]) | set(list(weekly.values())[:KEEP_WEEKLY])


def prune_scheduled_backups(backups: Path) -> list[str]:
    scheduled = _scheduled_backups(backups)
    keep = retained_backups(scheduled)
    removed = []
    for _, backup in scheduled:
        if backup not in keep:
            backup.unlink(missing_ok=True)
            backup.with_name(backup.name + RECEIPT_SUFFIX).unlink(missing_ok=True)
            removed.append(backup.name)
    if removed:
        _fsync_directory(backups)
    return removed


@report_backup_failure
def create_scheduled_backup(database_url: str, *, now: datetime | None = None) -> dict[str, object]:
    """Check the source, back it up, verify and restore-test the result, then prune. Any failure stops there."""
    paths = paths_from_database_url(database_url)
    now = now or datetime.now(timezone.utc)
    with restore_lock(paths.lock):
        paths.backups.mkdir(parents=True, exist_ok=True)
        _sweep_partial(paths.backups)
        source = require_healthy_source(paths.database)
        partial, _ = _create_online_backup_unlocked(paths, PARTIAL_PREFIX)
        # Readers list odin_backup_*, so a half-written file never appears under that name.
        final = partial.with_name("odin_backup_" + partial.name[len(PARTIAL_PREFIX):])
        try:
            os.replace(partial, final)
            _fsync_directory(paths.backups)
            verified = verify_backup(database_url, final.name)
            rehearse_restore(final, paths.database)
            _write_receipt(final, now, verified)
        except Exception:
            partial.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        try:
            pruned = prune_scheduled_backups(paths.backups)
        except OSError as error:
            # The new backup is good; a failed cleanup only leaves extra files behind.
            log.warning("Backup retention could not prune (%s)", type(error).__name__)
            pruned = []
    return {"filename": final.name, "size_bytes": verified["size_bytes"], "sha256": verified["sha256"],
            "pruned": pruned, "source_report": source}
