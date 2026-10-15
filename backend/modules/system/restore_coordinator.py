"""Container-startup entry point for applying a staged SQLite restore."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from modules.system.backup_service import (
    apply_pending_restore,
    finalize_pending_restore,
    rollback_pending_restore,
)
from modules.system import restore_artifacts
from modules.system.postgres_backup_service import (
    apply_pending_postgres_restore,
    postgres_paths,
    quarantine_failed_pending_postgres_restore,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url")
    parser.add_argument("--pid-file", type=Path, default=Path("/var/run/supervisord.pid"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--finalize", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    if database_url.startswith(("postgresql://", "postgres://")):
        if args.finalize or args.rollback:
            return 0
        maintenance_url = os.getenv("DATABASE_MAINTENANCE_URL")
        if not maintenance_url:
            parser.error("DATABASE_MAINTENANCE_URL is required for PostgreSQL restore")
        password_file = os.getenv("DATABASE_PASSWORD_FILE")
        maintenance_password_file = os.getenv("DATABASE_MAINTENANCE_PASSWORD_FILE")
        try:
            swept = restore_artifacts.sweep_stale(postgres_paths().backups, maintenance_url, maintenance_password_file)
            if swept["databases"] or swept["files"]:
                print(json.dumps({"stale_restore_artifacts_removed": swept}, sort_keys=True))
        except Exception as exc:  # A sweep failure must not block startup; it is retried.
            print(json.dumps({"stale_restore_artifact_sweep_failed": type(exc).__name__}), file=sys.stderr)
        try:
            result = apply_pending_postgres_restore(
                database_url,
                maintenance_url,
                acknowledgement=os.getenv("ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT"),
                password_file=password_file,
                maintenance_password_file=maintenance_password_file,
            )
        except Exception as exc:
            # Boot-loop guard: a restore that never touched the target is moved aside.
            result = quarantine_failed_pending_postgres_restore(database_url, error=exc, password_file=password_file)
            if result is None:
                raise
            print(json.dumps(result, sort_keys=True))
            print("PostgreSQL restore failed before changing the database: " + str(result["reason"]), file=sys.stderr)
            return 0
        if result:
            print(json.dumps(result, sort_keys=True))
        return 0
    if args.finalize:
        result = finalize_pending_restore(database_url, pid_file=args.pid_file)
    elif args.rollback:
        result = rollback_pending_restore(database_url, pid_file=args.pid_file)
    else:
        result = apply_pending_restore(database_url, pid_file=args.pid_file)
    if result:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
