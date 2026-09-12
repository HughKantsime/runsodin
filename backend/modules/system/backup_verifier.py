"""Non-destructively verify an existing ODIN SQLite backup."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from modules.system.backup_service import BackupValidationError, paths_from_database_url, validate_database


def verify_backup(database_url: str, backup_name: str = "latest") -> dict[str, object]:
    paths = paths_from_database_url(database_url)
    if backup_name == "latest":
        candidates = sorted(paths.backups.glob("odin_backup_*.db"), reverse=True)
        if not candidates:
            raise BackupValidationError("No ODIN backups are available to verify")
        backup = candidates[0]
    else:
        supplied = Path(backup_name)
        if (
            supplied.name != backup_name
            or not backup_name.startswith("odin_backup_")
            or supplied.suffix != ".db"
        ):
            raise BackupValidationError("Backup name is invalid")
        backup = (paths.backups / supplied).resolve()
        if backup.parent != paths.backups.resolve():
            raise BackupValidationError("Backup is outside the backup directory")
    metadata = validate_database(backup)
    return {
        "status": "verified",
        "filename": backup.name,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        **metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="SQLite database URL; defaults to DATABASE_URL",
    )
    parser.add_argument("--backup", default="latest", help="odin_backup_*.db filename or latest")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        print(json.dumps(verify_backup(args.database_url, args.backup), sort_keys=True))
    except BackupValidationError as exc:
        parser.exit(1, f"backup verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
