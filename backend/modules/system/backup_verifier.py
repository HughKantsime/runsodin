"""Non-destructively verify an existing ODIN database backup."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from modules.system.backup_service import BackupValidationError, paths_from_database_url, validate_database
from modules.system.postgres_backup_service import (
    postgres_paths,
    validate_postgres_restore,
)


def verify_backup(
    database_url: str,
    backup_name: str = "latest",
    *,
    maintenance_url: str | None = None,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
) -> dict[str, object]:
    backend = make_url(database_url).get_backend_name()
    is_postgres = backend in {"postgresql", "postgres"}
    if backend != "sqlite" and not is_postgres:
        raise BackupValidationError("Unsupported database backend")
    paths = postgres_paths() if is_postgres else paths_from_database_url(database_url)
    suffix = ".dump" if is_postgres else ".db"
    if backup_name == "latest":
        candidates = sorted(paths.backups.glob(f"odin_backup_*{suffix}"), reverse=True)
        if not candidates:
            raise BackupValidationError("No ODIN backups are available to verify")
        backup = candidates[0]
    else:
        supplied = Path(backup_name)
        if (
            supplied.name != backup_name
            or not backup_name.startswith("odin_backup_")
            or supplied.suffix != suffix
        ):
            raise BackupValidationError("Backup name is invalid")
        backup = (paths.backups / supplied).resolve()
        if backup.parent != paths.backups.resolve():
            raise BackupValidationError("Backup is outside the backup directory")
    if is_postgres:
        if not maintenance_url:
            raise BackupValidationError(
                "PostgreSQL verification requires DATABASE_MAINTENANCE_URL"
            )
        metadata = validate_postgres_restore(
            backup,
            database_url,
            maintenance_url,
            password_file=password_file,
            maintenance_password_file=maintenance_password_file,
        )
    else:
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
        help="ODIN database URL; defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--maintenance-url",
        default=os.environ.get("DATABASE_MAINTENANCE_URL"),
        help="PostgreSQL maintenance URL; defaults to DATABASE_MAINTENANCE_URL",
    )
    parser.add_argument(
        "--password-file",
        default=os.environ.get("DATABASE_PASSWORD_FILE"),
    )
    parser.add_argument(
        "--maintenance-password-file",
        default=os.environ.get("DATABASE_MAINTENANCE_PASSWORD_FILE"),
    )
    parser.add_argument(
        "--backup", default="latest", help="canonical backup filename or latest"
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        print(
            json.dumps(
                verify_backup(
                    args.database_url,
                    args.backup,
                    maintenance_url=args.maintenance_url,
                    password_file=args.password_file,
                    maintenance_password_file=args.maintenance_password_file,
                ),
                sort_keys=True,
            )
        )
    except BackupValidationError as exc:
        parser.exit(1, f"backup verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
