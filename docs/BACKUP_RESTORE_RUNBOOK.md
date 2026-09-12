# ODIN Backup and Restore Runbook

ODIN supports application-managed, verified backup and offline restore for SQLite and PostgreSQL 16. Database files and dumps may contain student or customer data. Never email them; move them only through approved encrypted storage.

## Shared controls

- Backup creation, listing, download, deletion, and restore staging require a superadmin.
- A candidate is validated before publication or staging, is limited to the current canonical ODIN schema, and is addressed by a SHA-256 digest.
- Restore is an offline maintenance action. Stop every ODIN API/worker role before applying it.
- Record start/end timestamps, archive size/hash, operator, outcome, recovery action, and approved expiry. Do not copy row contents into the record.
- RPO and RTO are measured drill results, not promises. Run `make test-database-parity CANDIDATE_PYTHON=python3.11` and retain its HTML report.

## SQLite backup

1. Confirm the data volume has sufficient free space. ODIN requires at least twice the live database size or 16 MiB, whichever is greater.
2. Create the backup from the System panel or `POST /api/backups`.
3. Verify it independently:

   ```bash
   make verify-backup DATABASE_URL=sqlite:////data/odin.db BACKUP_NAME=latest PYTHON=python3.11
   ```

4. Confirm the resulting `odin_backup_*.db` is mode `0600`, passes integrity and foreign-key checks, and contains only the required schema.

## SQLite restore

1. Upload the candidate with `POST /api/backups/restore`. This validates and stages it; it does not replace the live database.
2. Restart the bootstrap-owner container. Before the application starts, ODIN takes the restore lock, checkpoints WAL, creates and journals a current rollback copy, and atomically swaps the database.
3. ODIN applies migrations and optional EDU seed work. Any failure restores the rollback. An interruption before finalization is recovered on the next start.
4. After final validation ODIN writes a redacted completion audit and clears the pending manifest.
5. Verify readiness, login, printer inventory, jobs, RBAC, and a fresh independent backup check before reopening service.

## PostgreSQL deployment identities

The enterprise compose topology uses three independent password files:

- `odin_admin`: initialization-only PostgreSQL superuser; its secret is mounted only into PostgreSQL.
- `odin`: application owner/login; `NOSUPERUSER`, `NOCREATEDB`, and `NOCREATEROLE`.
- `odin_maintenance`: validation-database login; `NOSUPERUSER`, `CREATEDB`, and `NOCREATEROLE`. Only the bootstrap-owner API receives this secret.

Set mode-`0600` host files through `POSTGRES_ADMIN_PASSWORD_FILE`, `POSTGRES_APP_PASSWORD_FILE`, and `POSTGRES_MAINTENANCE_PASSWORD_FILE`. Application URLs contain no passwords:

```text
DATABASE_URL=postgresql://odin@postgres:5432/odin
DATABASE_MAINTENANCE_URL=postgresql://odin_maintenance@postgres:5432/postgres
```

## PostgreSQL backup

1. Confirm no unrelated tables, views, functions, triggers, enum types, or external-schema dependencies exist in ODIN's managed `public` schema.
2. Create the backup from the System panel or `POST /api/backups`. ODIN runs `pg_dump` in custom format, captures only `public`, strips ownership/privileges/comments, validates the table/type TOC allowlist, restores into a disposable database, and verifies the resulting schema before publishing the file.
3. Verify a retained dump independently inside the configured ODIN environment:

   ```bash
   make verify-backup \
     DATABASE_URL=postgresql://odin@postgres:5432/odin \
     BACKUP_NAME=latest \
     PYTHON=python3.11
   ```

   `DATABASE_PASSWORD_FILE`, `DATABASE_MAINTENANCE_URL`, and `DATABASE_MAINTENANCE_PASSWORD_FILE` must already be set.

## PostgreSQL restore

1. Upload an `odin_backup_*.dump` through `POST /api/backups/restore`. ODIN performs the complete disposable-database validation and stages an immutable copy. Record the returned `candidate_sha256`.
2. Stop every API, monitor, vision, and report container. Leave PostgreSQL running. Isolate the target database network so no unrelated client can connect until verification completes; the database session checks are fail-closed point-in-time checks, while network quiescence is the operator's continuous exclusion boundary.
3. Configure the bootstrap owner with `ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT` equal to the exact staged SHA-256. A missing or different value is refused.
4. Start only the bootstrap-owner API. The coordinator obtains a PostgreSQL advisory lock, rejects every other target-database session, checks external dependencies, rechecks sessions, then creates the current rollback dump immediately before applying the candidate.
5. ODIN records `restore_in_progress`, performs an allowlisted object-scoped transactional `pg_restore --clean --if-exists`, bootstraps, validates, and audits the result while holding the lock. It never drops or recreates `public` and cannot restore non-ODIN TOC entries.
6. On an application/validation failure ODIN restores the verified rollback automatically. If the process is interrupted after replacement, the next bootstrap-owner start detects `restore_in_progress` and restores that rollback before serving.
7. Remove the acknowledgement after success. Start remaining roles, then verify `/health/ready`, login, RBAC, WebSocket events, background writes, printers, and jobs before reopening service.

## Failure rules

- Do not delete or edit a pending manifest to force startup.
- Do not use a credential-bearing PostgreSQL URL or `PGPASSWORD`.
- Do not bypass active-session refusal, archive allowlists, checksums, or disposable restore validation.
- If automatic rollback also fails, keep all application roles stopped, preserve the pending manifest and rollback archive, capture redacted logs, and escalate to database recovery. Do not retry with ad-hoc `pg_restore` flags.

The browser-readable version is [BACKUP_RESTORE_RUNBOOK.html](BACKUP_RESTORE_RUNBOOK.html).
