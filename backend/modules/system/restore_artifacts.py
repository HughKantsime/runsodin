"""Disposable PostgreSQL restore artifacts: journal, targeted cleanup and a stale sweep.

Every ``odin_validate_*`` database a restore or backup validation creates is journaled
as ``<backups>/.odin-stage-journal/<name>`` before it is created (the preserved029 child
journals its own through a statement hook). Normal paths drop and forget their entries;
a killed child is cleaned up by its parent; anything a killed parent left behind is
swept at startup and before each restore once it is older than ``STALE_SECONDS``.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import time

JOURNAL = ".odin-stage-journal"
STALE_SECONDS = 6 * 3600
_NAME = re.compile(r"odin_validate_[0-9a-f]{20}")
# Disposable working files and the preserved029 child work directories.
_STALE_FILE = re.compile(r"\.odin-(?:canonical-schema|restore-list|trusted025|upgraded027|upgraded029)-[A-Za-z0-9_]+(?:\.dump)?")
_STALE_DIR = re.compile(r"\.odin-preserved029-[0-9a-f]{20}")
_QUARANTINED = re.compile(r"failed_restore_\d{8}_\d{6}_\d{6}\.(?:dump|json)")
QUARANTINE_RETENTION_DAYS = 30


def journal_dir(backups: Path) -> Path:
    path = Path(backups) / JOURNAL
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    facts = path.lstat()
    if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != os.getuid() or stat.S_IMODE(facts.st_mode) & 0o077:
        from modules.system.backup_service import BackupValidationError
        raise BackupValidationError("Restore stage journal directory is unsafe")
    return path


def journal_entries(backups: Path, *, confirmed_only: bool = False) -> dict[str, float]:
    """Journaled names and their age, including preserved029 children's directories.

    A child's ``<name>.intent`` (CREATE DATABASE was about to run) is included unless
    ``confirmed_only``: the sweep cannot prove such a database is this code's.
    """
    folder = journal_dir(backups)
    entries = {}
    for entry in folder.iterdir():
        if _NAME.fullmatch(entry.name) and entry.is_file():
            entries[entry.name] = entry.lstat().st_mtime
        elif _STALE_DIR.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
            for child in entry.iterdir():
                intent = child.name.endswith(".intent")
                name = child.name[:-7] if intent else child.name
                if _NAME.fullmatch(name) and child.is_file() and not (intent and confirmed_only):
                    entries[name] = min(entries.get(name, child.lstat().st_mtime), child.lstat().st_mtime)
    return entries


def _sweep_child_journals(backups: Path, cutoff: float) -> None:
    """Forget stale intent-only entries (never dropped) and remove stale empty child journals.

    A child journal is touched only when it and everything in it are older than ``cutoff``,
    so a running child's fresh (possibly still empty) directory is never removed.
    """
    folder = journal_dir(backups)
    for entry in folder.iterdir():
        if not (_STALE_DIR.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()):
            continue
        children = list(entry.iterdir())
        if entry.lstat().st_mtime >= cutoff or any(child.lstat().st_mtime >= cutoff for child in children):
            continue
        for child in children:
            if child.name.endswith(".intent") and child.is_file():
                child.unlink(missing_ok=True)
        if not any(entry.iterdir()):
            entry.rmdir()


def record(backups: Path, name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError("unsafe stage database name")
    folder = journal_dir(backups)
    descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def forget(backups: Path, name: str) -> None:
    if not _NAME.fullmatch(name):
        return
    folder = journal_dir(backups)
    (folder / name).unlink(missing_ok=True)
    for child in folder.iterdir():
        if _STALE_DIR.fullmatch(child.name) and child.is_dir() and not child.is_symlink():
            (child / name).unlink(missing_ok=True); (child / (name + ".intent")).unlink(missing_ok=True)
            if not any(child.iterdir()):
                child.rmdir()


def drop_databases(maintenance_url: str, maintenance_password_file: str | None, names) -> list[str]:
    names = sorted(name for name in set(names) if _NAME.fullmatch(name))
    if not names:
        return []
    from core.database_config import create_database_engine
    engine = create_database_engine(maintenance_url, role="restore", password_file=maintenance_password_file).execution_options(isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for name in names:
                connection.exec_driver_sql('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
    finally:
        engine.dispose()
    return names


def existing_databases(maintenance_url: str, maintenance_password_file: str | None) -> frozenset[str]:
    """Names of the odin_validate_* databases that exist right now."""
    from sqlalchemy import text
    from core.database_config import create_database_engine
    engine = create_database_engine(maintenance_url, role="restore", password_file=maintenance_password_file)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'odin\\_validate\\_%'")).scalars()
            return frozenset(name for name in rows if _NAME.fullmatch(name))
    finally:
        engine.dispose()


def drop_and_forget(backups: Path, maintenance_url: str, maintenance_password_file: str | None, names) -> list[str]:
    dropped = drop_databases(maintenance_url, maintenance_password_file, names)
    for name in dropped:
        forget(backups, name)
    return dropped


def _quarantined_at(record_path: Path, archive: Path) -> float | None:
    import json
    from datetime import datetime
    try:
        failed_at = json.loads(record_path.read_text(encoding="utf-8")).get("failed_at")
        return datetime.fromisoformat(failed_at).timestamp()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        return archive.lstat().st_mtime
    except OSError:
        return None


ROLLBACK_RETENTION_DAYS = 7
_ROLLBACK = re.compile(r"rollback_\d{8}_\d{6}_\d{6}\.dump")


def _sweep_rollbacks(backups: Path, cutoff: float) -> list[str]:
    """Pre-restore rollback dumps older than ROLLBACK_RETENTION_DAYS, keeping the newest one
    and never one an in-progress restore names for its recovery."""
    import json
    protected = set()
    manifest = backups.parent / "restore-pending-postgres.json"
    try:
        named = json.loads(manifest.read_text(encoding="utf-8")).get("rollback_backup")
        if isinstance(named, str):
            protected.add(named)
    except (OSError, ValueError, AttributeError):
        if manifest.exists():
            return []  # An unreadable manifest may name any of them: keep all.
    dumps = sorted((entry for entry in backups.iterdir() if _ROLLBACK.fullmatch(entry.name) and stat.S_ISREG(entry.lstat().st_mode)),
                   key=lambda entry: entry.lstat().st_mtime)
    removed = []
    for entry in dumps[:-1]:
        if entry.name not in protected and entry.lstat().st_mtime < cutoff:
            entry.unlink(missing_ok=True); removed.append(entry.name)
    return removed


def _sweep_quarantine(backups: Path, cutoff: float) -> list[str]:
    removed = []
    stems = {entry.name.rsplit(".", 1)[0] for entry in backups.iterdir()
             if _QUARANTINED.fullmatch(entry.name) and stat.S_ISREG(entry.lstat().st_mode)}
    for stem in sorted(stems):
        record_path, archive = backups / (stem + ".json"), backups / (stem + ".dump")
        orphan = record_path.is_file() and not archive.is_file()
        when = _quarantined_at(record_path, archive)
        if orphan or (when is not None and when < cutoff):
            for path in (archive, record_path):
                if path.is_file():
                    path.unlink(missing_ok=True); removed.append(path.name)
    return removed


def sweep_stale(backups: Path, maintenance_url: str, maintenance_password_file: str | None, *, older_than: float = STALE_SECONDS, now: float | None = None, drop=None) -> dict[str, list[str]]:
    """Drop journaled stage databases and remove working files older than ``older_than``."""
    backups = Path(backups)
    if not backups.is_dir():
        return {"databases": [], "files": []}
    cutoff = (time.time() if now is None else now) - older_than
    stale = [name for name, mtime in journal_entries(backups, confirmed_only=True).items() if mtime < cutoff]
    _sweep_child_journals(backups, cutoff)  # before forgetting, which touches the directories
    dropped = (drop or (lambda names: drop_databases(maintenance_url, maintenance_password_file, names)))(stale) if stale else []
    for name in dropped:
        forget(backups, name)
    removed = []
    # Quarantined restores: kept QUARANTINE_RETENTION_DAYS from the quarantine (the record's
    # failed_at, else the archive's mtime, which the quarantine resets); orphan records
    # whose archive is gone are removed at once.
    removed.extend(_sweep_quarantine(backups, (time.time() if now is None else now) - QUARANTINE_RETENTION_DAYS * 86400))
    removed.extend(_sweep_rollbacks(backups, (time.time() if now is None else now) - ROLLBACK_RETENTION_DAYS * 86400))
    for entry in backups.iterdir():
        facts = entry.lstat()
        if facts.st_mtime >= cutoff:
            continue
        if _STALE_FILE.fullmatch(entry.name) and stat.S_ISREG(facts.st_mode):
            entry.unlink(missing_ok=True); removed.append(entry.name)
        elif _STALE_DIR.fullmatch(entry.name) and stat.S_ISDIR(facts.st_mode):
            shutil.rmtree(entry, ignore_errors=True); removed.append(entry.name)
    return {"databases": list(dropped), "files": sorted(removed)}
