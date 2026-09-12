# ODIN Backup and Restore Runbook

ODIN supports transactional application-managed backup/restore for SQLite. PostgreSQL deployments must use an operator-reviewed `pg_dump`/`pg_restore` procedure; the ODIN HTTP restore endpoint intentionally refuses PostgreSQL.

## Backup

1. Confirm free space on the data volume and its backup destination. ODIN refuses backup creation unless free space is at least twice the live database size or 16 MiB, whichever is greater; this is a floor, not a substitute for capacity monitoring.
2. Create the backup from the System panel or API.
3. Confirm the resulting file is mode `0600`, passes SQLite integrity checks, and contains the required ODIN schema.
4. Copy off-host only through approved encrypted storage. Never email raw database files.
5. Record the backup time, size, hash, storage location, and scheduled expiry without recording student rows.

Scheduled verification is read-only. Run `make verify-backup DATABASE_URL=sqlite:////data/odin.db BACKUP_NAME=latest PYTHON=python3`; it checks the SQLite header, integrity, required current ODIN schema, file bounds, and SHA-256 without opening the backup for writes. A specific canonical `odin_backup_*.db` basename may replace `latest`.

## Restore

1. Announce the maintenance window. The candidate may be uploaded while ODIN is still running: the endpoint only validates and stages it, creates a verified pre-restore snapshot, and writes a pending database plus SHA-256 manifest. It does not replace the live database.
2. Restart the container to begin the offline phase. Before FastAPI, SQLAlchemy, supervisord, or monitors start, the coordinator takes the restore lock, checkpoints WAL state, durably journals the rollback filename, atomically swaps the database, and validates it. If the process stops during either rename, the next startup restores the journaled original before continuing.
3. ODIN runs every startup schema migration and optional EDU seed before finalizing the restore. Any failure in this interval automatically restores the pre-restore database. An unclean restart before finalization also rolls back on the next boot.
4. After successful startup database work, the coordinator records a redacted completion audit and removes its pending-finalization marker.
5. Verify `/health`, login, printer inventory, jobs, permissions, and `PRAGMA integrity_check` before reopening service.
6. Retain the pre-restore rollback only for the approved recovery window, then securely expire it.

RPO and RTO are evidence, not aspirations: measure them during a scheduled drill using representative data, record the last successful backup and restore durations, and never claim values that have not been observed.
