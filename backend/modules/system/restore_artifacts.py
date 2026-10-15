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


def journal_dir(backups: Path) -> Path:
    path = Path(backups) / JOURNAL
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    facts = path.lstat()
    if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != os.getuid() or stat.S_IMODE(facts.st_mode) & 0o077:
        from modules.system.backup_service import BackupValidationError
        raise BackupValidationError("Restore stage journal directory is unsafe")
    return path


def journal_entries(backups: Path) -> dict[str, float]:
    folder = journal_dir(backups)
    return {entry.name: entry.lstat().st_mtime for entry in folder.iterdir() if _NAME.fullmatch(entry.name) and entry.is_file()}


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
    if _NAME.fullmatch(name):
        (journal_dir(backups) / name).unlink(missing_ok=True)


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


def drop_and_forget(backups: Path, maintenance_url: str, maintenance_password_file: str | None, names) -> list[str]:
    dropped = drop_databases(maintenance_url, maintenance_password_file, names)
    for name in dropped:
        forget(backups, name)
    return dropped


def sweep_stale(backups: Path, maintenance_url: str, maintenance_password_file: str | None, *, older_than: float = STALE_SECONDS, now: float | None = None, drop=None) -> dict[str, list[str]]:
    """Drop journaled stage databases and remove working files older than ``older_than``."""
    backups = Path(backups)
    if not backups.is_dir():
        return {"databases": [], "files": []}
    cutoff = (time.time() if now is None else now) - older_than
    stale = [name for name, mtime in journal_entries(backups).items() if mtime < cutoff]
    dropped = (drop or (lambda names: drop_databases(maintenance_url, maintenance_password_file, names)))(stale) if stale else []
    for name in dropped:
        forget(backups, name)
    removed = []
    for entry in backups.iterdir():
        facts = entry.lstat()
        if facts.st_mtime >= cutoff:
            continue
        if _STALE_FILE.fullmatch(entry.name) and stat.S_ISREG(facts.st_mode):
            entry.unlink(missing_ok=True); removed.append(entry.name)
        elif _STALE_DIR.fullmatch(entry.name) and stat.S_ISDIR(facts.st_mode):
            shutil.rmtree(entry, ignore_errors=True); removed.append(entry.name)
    return {"databases": list(dropped), "files": sorted(removed)}
