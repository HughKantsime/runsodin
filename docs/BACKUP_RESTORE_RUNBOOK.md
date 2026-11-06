# ODIN Backup and Restore Runbook

ODIN supports application-managed, verified backup and offline restore for SQLite and PostgreSQL 16. Database files and dumps may contain student or customer data. Never email them; move them only through approved encrypted storage.

## Paid license identity and recovery

A device-v1 paid license requires both the original installation ID and the Ed25519 private device key. Hardware replacement works without a transfer when both are preserved. In the configured `LICENSE_DIR` (normally `/data`), preserve `.odin-install-id`, `.odin-device.key`, and `odin.license` together in a separate encrypted host/volume backup. Keep the private key readable only by the ODIN service account (`0600`); never paste it into a ticket, log, request code, or chat.

Application database backups, portable exports, and support bundles do **not** transfer a bound license. Do not add the private key to these artifacts. Stop ODIN before restoring its identity files, restore both identity files and the signed license to the configured license directory, preserve ownership and key permissions, then restart and verify Settings > License. Do not overwrite another running installation's identity: cloning the private key clones its identity.

Legacy paid licenses remain valid indefinitely under their original installation and expiry rules. Conversion is voluntary and applies to the whole purchase, including every grant. Coordinate installation owners before confirming conversion; previous offline legacy files cannot be revoked.

For a mismatch, first restore the original identity files. If the old key is available and you are transferring to a new identity, unactivate the old installation before activating the new one. For an offline transfer, generate the signed unactivation request (which quarantines the local license immediately), process it on the portal, then use a fresh offline challenge for the replacement installation. Challenges expire after 15 minutes. If either identity file is lost, contact support for purchase verification and an authorized re-issue; do not generate replacement identity files expecting them to unlock the old license. Old offline files cannot be remotely revoked. Recovery and offline import remain available without a valid paid entitlement.

Older ODIN releases do not enforce paid device binding. Roll out the new ODIN verifier before enabling bound new-purchase issuance on the portal; keep existing purchases in legacy mode unless explicitly converted.

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

## SQLite automatic backups and integrity checks

A supervised process, `db_guardian`, runs these on single-container SQLite installs. On PostgreSQL it stays idle; use the PostgreSQL steps below.

- **Integrity check:** `PRAGMA integrity_check` and `foreign_key_check` on a read-only connection every 5 minutes. A failure raises a critical alert to platform admins and turns `/health` to `"status": "degraded"` with `"database_integrity": {"status": "failed"}`. It stays that way until a check passes. ODIN never repairs the database.
- **Other `/health` states:** `pending` means no check has finished yet. `retrying` means the last one or two checks couldn't finish, usually a passing lock, and overall status stays `ok`. `unavailable` means three in a row couldn't finish. `stale` means checks have stopped, so look at `/data/db_guardian.log` and at `supervisorctl status`.
- **Backup:** one verified `odin_backup_*.db` a day in `/data/backups`, compacted like a manual backup. It checks the live database first, verifies the new file, restore-tests it, and only then gives it that name and prunes. The restore test runs the real startup command on a throwaway copy and requires the same schema as live. A failure raises a critical "Database backup failed" alert, which reaches the inbox and the phone channels like the integrity alert, and tries again about an hour later. `/data/db_guardian.log` has the reason a restore test was refused.
- **Backups on `/health`:** `database_backup` shows a state and the time of the last good backup, nothing else. `pending` means no attempt yet, `ok` that the last attempt worked, and `failed` that it didn't. `overdue` means no good backup for 26 hours, and `stale` that the guardian has stopped recording. A backup problem never changes the top-level `status`, so it can't block a release that might fix it.
- **Disk:** a run holds two full copies at its peak, and won't start unless both fit and 128 MiB is left for the live database. If not, it alerts, keeps every existing backup and tries again hourly until there's room. Retained backups can use about 11 times the size of one, so that's where to look first. Backups are never pruned to make room.
- **Missed backups:** no verified backup for 26 hours raises "Backup missed", whatever the cause. If `db_guardian` itself stops, or won't even start, `report_runner` raises the integrity alert, once a day.
- **Quiet hours:** these alerts ignore them, both the system's and a phone's own, so they can arrive at night. Ordinary alerts still wait.
- **A disk with no room at all:** nothing can be saved, so no alert can be raised from ODIN. `/health` is what's left: `database_backup` keeps its last saved state, shows `stale` within about 15 minutes, and `overdue` 26 hours after the last good backup. The stopped checks turn the top-level `status` to `degraded`.
- **Retention:** the newest backup per day for 7 days and the newest per ISO week for 4 weeks. Pruning only touches files with an `.receipt.json` beside them, so manual backups and any other files stay until you remove them. A damaged database gets no new backup and nothing is pruned.
- **Limits:** these backups share a disk with the live database, so they don't protect against losing that disk; off-server copies below do. A check only finds damage SQLite can see; a page that is out of date but self-consistent can pass.

## SQLite off-server copies

`ops/offsite_backup/odin-offsite-backup.mjs` runs on a separate machine and pulls the newest verified backup over SSH. It encrypts the stream with AES-256-GCM as it arrives and writes to the NAS, so plaintext never reaches a disk there. It compares what arrived with the hash the server recorded when it verified and restore-tested that backup, flushes the file to the NAS, then reads it back and authenticates it. Only then does the copy get its name and count. Once a day, and after every new copy, it reads back every retained copy. It keeps 7 daily and 4 weekly among the good ones and never deletes a bad one.

It alerts through ntfy if ODIN's newest backup is over 26 hours old, a check fails, or three hourly runs in a row fail. That includes a run stopped after two minutes with no progress, such as a hung NAS. Alerts carry no host, path or key; the cause is in `status.json` beside `secrets/`, under `detail`. ODIN alerts in turn if no copy is confirmed for 36 hours, counted from installation, so a job that stops or never works is noticed from either side. It holds that alert for 4 hours after ODIN itself restarts, to give the job time to catch up, but never once the last confirmed copy is more than 3 days old.

**Installing it needs the owner's go, and it changes two machines.**

1. On the machine that runs the job, with the NAS mounted: `node odin-offsite-backup.mjs init-key`. It writes the key to a private file and doesn't print it. Copy that file somewhere safe and separate, as the owner decides: without it no copy can be restored. Never paste it into a chat, ticket or log.
2. There, create `secrets/ssh_ed25519` beside the key, with no passphrase, and `config.json` beside `secrets/`, with `target` (`root@<ODIN host>`), `mount`, `share` (the NAS name in the mount line) and `ntfy` (the alert URL). This repository is public, so these stay out of it.
3. Trust the ODIN host. The job can't answer a host key prompt, so add it first. On the host, run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. On the job's machine, run `ssh-keyscan -t ed25519 <ODIN host> > /tmp/odin-host-key`, then `ssh-keygen -lf /tmp/odin-host-key`. If the two fingerprints match, append that file to `~/.ssh/known_hosts`.
4. On the ODIN host, add the job's public key to root's `authorized_keys`, limited to the one command:

   ```text
   command="docker exec -i -u odin -w /app/backend -e SSH_ORIGINAL_COMMAND odin python3 -m modules.system.offsite_export",restrict,from="<job machine IP>" ssh-ed25519 <public key>
   ```

5. Run `node odin-offsite-backup.mjs run` once and check `status.json`, then `install`, which registers the job with ODIN and starts the hourly schedule.

To stop expecting copies, remove the job and delete `/data/run/db-offsite.json` on the ODIN host.

If a copy fails its check, the job keeps alerting and reports not ok until that copy is gone. Find out why before you delete it, because the same NAS holds the others.

To restore, `node odin-offsite-backup.mjs decrypt <file.enc> <output>` writes the database, mode `0600`, after the file has authenticated. Keep the file's name: it's authenticated too, so a renamed copy won't decrypt. Then follow the restore steps below.

## Database failed its integrity check

ODIN refuses to start with "ODIN will not start: the database failed its integrity check", or a running ODIN alerts and shows `failed` on `/health`. This is damage in the database file. Rolling back the release won't help, because every release makes this check at startup. A running ODIN will keep working until it restarts, then refuse to start, so plan the repair before you restart it.

Older releases report the same failure as "Media ownership database integrity differs".

1. **Copy first.** Stop the ODIN container if it is still running; if a print is running, decide with the owner when. Copy `odin.db` with any `-wal` and `-shm` files to a new private directory, and record its SHA-256. Change nothing in the live directory.
2. **Find what is damaged.** On a second copy, run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Note which tables are named and when the damage began, from the newest good `odin_backup_*.db` and the rows around it.
3. **Choose with the owner.** Compare a repair with restoring the newest good backup, including what each one loses. Nothing touches live data without the owner's yes.
4. **Test on a copy.** Apply the repair or the restore to a copy. Then run the real startup check on it, which is the same one the container makes:

   ```bash
   DATABASE_URL=sqlite:////path/to/copy/odin.db ODIN_DB_ROLE=bootstrap python3 -m scripts.bootstrap_database
   ```

   It has to exit 0 and print the expected `schema_fingerprint`. Run it from `/app/backend` in the same image, as `odin`, with the copy in a directory that user owns.
5. **Apply, with the owner's yes.** Check the live file's hash still matches the copy from step 1, then apply exactly what you tested. Keep the damaged copy until the owner agrees it can go.
6. **Afterwards,** confirm `/health` shows `ok` after the next check, and that a new `odin_backup_*.db` appears. If rows were lost, record which ones.

`POST /api/backups/restore` can refuse on a damaged database. It takes a safety copy of the live database first, and damage can make that copy fail. In that case use the offline steps above; don't delete the live file to get past it.

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

## Audit integrity files and restore baseline

Preserve both `.audit-hmac-keys.json` and `.audit-signed-head.json` from
`ODIN_AUDIT_KEY_DIR` (default `LICENSE_DIR`, normally `/data`). The first holds
active and retired receipt HMAC keys; the second holds signed-head history.
Keep them mode 0600, owned by the container's `odin` user. Do not paste their
contents into tickets or logs. A database alone cannot recover these secrets.

ODIN's manual SQLite/PostgreSQL and scheduled SQLite backups capture existing
audit files into a mode-0600 `<database filename>.audit.zip` companion. Its
manifest binds it to the database SHA-256 and lists any absent files (the signed
head does not exist before the first verification). Scheduled pruning and manual
deletion remove the companion with its database. Settings provides **Download
with audit keys**, or use `GET /api/backups/<filename>?bundle=true`, to download
one ZIP containing the native database plus the captured audit files. The ordinary
download remains database-only. Existing backups without a companion refuse the
bundle request; preserve their original installation key directory separately.

To recover onto a replacement installation, privately extract the bundle. Restore
the native .db/.dump through the normal staging flow, and install the audit files
in the configured key directory as `odin` before startup. No uploaded archive
automatically replaces live keys. When restoring only the database on an existing
installation, keep the current audit files: they preserve evidence of the rollback.
The existing off-server database-stream job still copies the database only; include
the audit companion or key directory in your separately secured volume backup.

A lost key directory with sealed evidence reports **audit_key_missing**, distinct
from tampering. ODIN will not auto-create a new keyring, sign exports, or create
authenticated erasure/retention receipts in that state. Required erasure and retention
continue with explicit unsealed `audit_key_missing` receipts/checkpoints; they remain
unverified deletion/redaction breaks after key recovery: they can't be told apart
from tampering. Recover the original keyring to resume signing.
Do not rotate as a recovery substitute. If the original keys cannot be restored,
an interactive platform superadmin can verify in Settings → Audit integrity and
choose **Initialize audit keyring**, supplying a reason. The Enterprise endpoint is
`POST /api/audit-logs/initialize-keyring` with `{"reason":"Original keys cannot be restored"}`.
It requires `audit_key_missing`, commits a chained `audit.keyring_initialized`
authorization and critical alerts before writing replacement keys, and never
replaces an existing keyring. A file-write failure leaves the authorization/alert
recorded and permits a retry. Earlier evidence and signed heads remain unverified;
initialization does not accept a restore baseline or repair history.

Both native restore flows seal `restore_completed` with the audit HMAC key.
Recover missing original keys before finalizing a restore that has sealed evidence. Daily verification continues to save
new heads after a break, marked after_break and retaining history, so further
deletions remain detectable. Investigate the restore and preserve the evidence.
Then an interactive superadmin can open Settings → Audit integrity, verify, and
**Accept restore baseline**, supplying a reason. This writes an acceptance audit
event with the actor and previous breaks. It does not erase head history and will
refuse any row/checkpoint corruption. Verify once more after acceptance.

Rotate an existing audit key as the directory owner:
`docker exec -u odin -w /app/backend odin python -c 'from core.audit_keys import rotate; rotate()'`.
Rotation refuses a different effective user, including root when the directory
belongs to odin, before changing the keyring.


Companion capture failure keeps the verified native database backup, returns
`audit_companion: failed` in its metadata, and raises an admin backup alert. Copy
the audit files separately and retry; the available database backup is not deleted.
Bundle downloads record `bundle: true` in `backup.downloaded`.

Baseline acceptance requires a fresh, unaccepted `restore_completed` marker from
the native SQLite or PostgreSQL restore flow. Its HMAC signature must authenticate;
unsigned or altered markers cannot authorize acceptance. Its `restored_at` must be newer than
the authenticated pre-restore anchor. Daily checks preserve this pending marker
while continuing to advance the head. Each restore can be accepted once; both
`restore_completed` and `audit.restore_baseline_accepted` survive retention.

Head-history compaction retains every restore acceptance entry permanently; the
210-entry cap applies only to routine heads.
