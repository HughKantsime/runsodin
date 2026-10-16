"""Container-startup entry point for applying a staged SQLite restore."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from modules.system.backup_service import (
    apply_pending_restore,
    finalize_pending_restore,
    rollback_pending_restore,
)
from modules.system import restore_artifacts
from modules.system.postgres_backup_service import (
    ArchiveRefused,
    PostgreSQLClientFailed,
    apply_pending_postgres_restore,
    postgres_paths,
    quarantine_failed_pending_postgres_restore,
)


# Bounded backoff for errors that a later attempt can clear (PostgreSQL not ready yet, DNS,
# a dropped connection, a client that could not reach the server).
RETRY_DELAYS = (2, 4, 8, 16, 30)


def _retryable(error: BaseException) -> bool:
    from sqlalchemy.exc import SQLAlchemyError
    if isinstance(error, PostgreSQLClientFailed):
        return error.retryable
    return isinstance(error, (SQLAlchemyError, ConnectionError, TimeoutError)) or (
        isinstance(error, OSError) and not isinstance(error, (FileNotFoundError, PermissionError)))


def _apply_postgres_with_retry(database_url, maintenance_url, password_file, maintenance_password_file):
    """Apply a staged restore. Only a deterministic archive refusal is quarantined.

    Transient failures are retried with a bounded backoff, then re-raised. Operator and
    environment errors (missing acknowledgement, unreadable credentials, restore-gate
    refusals) are re-raised at once. In both cases the staged restore is kept and the
    process exits non-zero as before.
    """
    attempt = 0
    while True:
        try:
            return apply_pending_postgres_restore(
                database_url,
                maintenance_url,
                acknowledgement=os.getenv("ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT"),
                password_file=password_file,
                maintenance_password_file=maintenance_password_file,
            )
        except ArchiveRefused as exc:
            result = quarantine_failed_pending_postgres_restore(database_url, error=exc, password_file=password_file)
            if result is None:
                raise
            print("PostgreSQL restore refused before changing the database; archive quarantined: " + str(result["reason"]), file=sys.stderr)
            return result
        except Exception as exc:
            if not _retryable(exc) or attempt >= len(RETRY_DELAYS):
                print(f"PostgreSQL restore not applied; the staged restore is kept ({type(exc).__name__}: {str(exc)[:300]})", file=sys.stderr)
                raise
            delay = RETRY_DELAYS[attempt]; attempt += 1
            print(f"PostgreSQL restore attempt {attempt} failed ({type(exc).__name__}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)


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
        result = _apply_postgres_with_retry(database_url, maintenance_url, password_file, maintenance_password_file)
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
