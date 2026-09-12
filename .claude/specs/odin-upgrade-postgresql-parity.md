---
title: ODIN Legacy Upgrade and PostgreSQL Parity
slug: odin-upgrade-postgresql-parity
project: O.D.I.N.
status: complete
review_passes: 2
review_status: passed
created: 2026-09-12
updated: 2026-09-12
---

# ODIN Legacy Upgrade and PostgreSQL Parity

## Objective

Make ODIN's advertised PostgreSQL deployment and legacy database upgrade path real, deterministic, recoverable, and release-blocking. A checked-out candidate image must be able to initialize both SQLite and PostgreSQL from empty state, converge representative legacy schemas without data loss, serve the same supported API/RBAC workflows on both dialects, run database-backed background components without SQLite fallbacks, and complete measured backup/restore drills with truthful sanitized HTML evidence.

This is objective 2 of the active ODIN engineering goal. It must preserve the completed deterministic candidate-image gate and existing SQLite safety behavior.

## Authority Boundary

- Do not push, tag, deploy, modify production/sandbox databases, or access customer/student data.
- Use only uniquely named disposable local containers, networks, volumes, databases, ports, and artifact directories.
- Build and test the checked-out Dockerfile; do not substitute a remote ODIN image.
- Do not print or retain database passwords, URLs containing credentials, signing keys, tokens, or generated application secrets.
- Do not weaken authentication, licensing, RBAC, tenant isolation, migration failure handling, or restore validation to obtain a passing result.
- Do not claim physical-printer or Telemetry V2 validation from this database-only work.
- PostgreSQL restore is an offline maintenance operation. Never replace a live database while ODIN API or worker sessions are active.

## Current State

- Objective 1 is committed locally as `0a967d9`; `main` is four commits ahead of `origin/main`. Nothing has been pushed, tagged, or deployed.
- SQLite is the only proven runtime. The deterministic candidate gate uses a fresh isolated SQLite volume and passes 4 API/RBAC plus 9 Playwright tests.
- `docker-compose.enterprise.yml` advertises PostgreSQL 16, but the shipped Python requirements contain no PostgreSQL driver and the final image contains no `pg_dump`/`pg_restore` client.
- `docker/entrypoint.sh` ignores compose commands, creates tables from an interpolated URL, invokes a SQLite-only restore coordinator for every dialect, then runs unconditional `sqlite3` enum, upgrade, and WAL blocks against `DATABASE_PATH`.
- `backend/core/db.py` translates SQLite migration text to PostgreSQL with string replacements, splits on semicolons, and suppresses broad `already exists`/`duplicate` errors. It has no migration ledger, checksum verification, or PostgreSQL advisory lock. PostgreSQL transaction-abort behavior makes the current duplicate suppression unsafe.
- Legacy additive migrations are split between SQL files and an inline SQLite block in the entrypoint. The inline block swallows every exception, so a real schema/data error can be mislabeled as an already-applied column.
- The application lifespan repeats `Base.metadata.create_all`, creates the WebSocket IPC table through a SQLite-only helper, and runs a PRAGMA-only drift check.
- Database-backed monitor/notification/archive code uses a shared `sqlite3` connection helper with qmark parameters, SQLite row behavior, and `lastrowid`. Several independent processes create SQLAlchemy engines with unconditional SQLite-only `check_same_thread` arguments.
- Multiple API routes still use SQLite-only identity retrieval (`last_insert_rowid()` or result `lastrowid`) without a PostgreSQL `RETURNING` path.
- Application-managed backup, staged restore, verification, and rollback support only file-backed SQLite. PostgreSQL routes return 501 and the runbook delegates to an untested manual `pg_dump`/`pg_restore` statement.
- The existing enterprise compose has fixed resource names, a weak default database password, a host-published PostgreSQL port, an unpinned go2rtc image, and no deterministic readiness/evidence gate. It must not be used as-is for release proof.
- Existing stress scripts accept a PostgreSQL URL but explicitly lack a fresh PostgreSQL bootstrap and do not prove behavioral parity.

## Decisions

### 1. One dialect-aware bootstrap owner

- Add a Python bootstrap command that reads `DATABASE_URL` from the environment, imports all ORM models, creates current ORM-owned tables, runs versioned raw/module migrations, applies explicit legacy upgrades, normalizes enum values, validates the resulting schema, and exits nonzero on any unexpected error.
- Remove database URL interpolation and database SQL from shell heredocs. The entrypoint calls the bootstrap command without passing credentials on the process command line.
- The entrypoint honors its supplied command. The image receives an explicit default command for the monolithic supervisord runtime; enterprise API/worker commands are no longer discarded.
- Exactly one enterprise service is the database-bootstrap owner. Other roles wait on an explicit schema-ready condition and must not race migrations or restores.
- PostgreSQL bootstrap uses a fixed application name and a transaction-scoped advisory lock. SQLite retains file-backed serialization appropriate to a single mounted installation.
- All PostgreSQL connections are created through one engine factory and receive an allowlisted role-specific application name: `odin-api`, `odin-bootstrap`, `odin-monitor-bambu`, `odin-monitor-moonraker`, `odin-monitor-prusalink`, `odin-monitor-elegoo`, `odin-vision`, `odin-timelapse`, `odin-reports`, `odin-backup`, or `odin-restore`. Unknown caller-supplied application names are rejected rather than copied into a connection string.
- Application lifespan may verify readiness but must not independently mutate schema after the entrypoint bootstrap has declared it ready. Test/in-process development initialization uses the same bootstrap library rather than a divergent path.

### 2. Versioned, fail-closed migrations

- Add an `odin_schema_migrations` ledger containing stable migration identity, SHA-256 checksum, dialect, and applied timestamp.
- Ledger identity is explicit by ownership type:
  - each raw SQL migration is identified by its repository-relative path and checksummed from exact file bytes;
  - each Python legacy upgrade is one versioned single-purpose module under `core/schema/migrations/`, identified by its stable module ID and checksummed from exact source bytes;
  - the fresh-install ORM baseline is identified as `orm-baseline:<version>` and checksummed from the canonical schema manifest produced from `Base.metadata`;
  - later ORM changes require a new explicit versioned migration; changing the baseline alone must not mutate an existing installation.
- New installations record each migration only after its transaction commits. A checksum mismatch for a recorded migration is fatal and clearly identifies the migration without exposing credentials.
- Existing unledgered installations are adopted by running idempotent schema-aware migration operations, validating their postconditions, then recording them. Do not blindly mark an unknown legacy schema current.
- Replace text substitution and broad exception swallowing with dialect-aware statements or Python migration functions. Duplicate-column handling is based on schema inspection and exact expected state, not substring matching.
- Keep raw-SQL-only table ownership explicit. Fresh SQLite and PostgreSQL schemas must satisfy the same required table/column/index/constraint manifest, allowing documented dialect-specific storage details only.
- Legacy enum normalization runs through bound SQL on both dialects and reports row counts without row content.
- Bootstrap is idempotent: a second run performs no destructive change, preserves fixture rows, and produces the same schema fingerprint.

### 3. Representative legacy upgrade fixtures

- Commit deterministic schema-only/fictional-data fixture builders for at least:
  - a pre-MFA/pre-organization installation missing additive user/group/resource columns;
  - an intermediate print/job/profile schema missing later capability columns;
  - the earlier narrow `idempotency_keys` layout covered by migration 006;
  - uppercase legacy enum values;
  - a fully current database used to prove no-op idempotency.
- Build fixtures through source-controlled SQL/Python, not opaque customer-derived database binaries.
- Run every applicable fixture on SQLite and PostgreSQL. Verify exact retained sentinel rows, foreign-key relationships, normalized enum values, required columns/indexes, migration ledger entries/checksums, and second-run idempotency.
- A deliberately malformed/conflicting legacy fixture must fail closed, preserve the original data state where transactional semantics permit, and produce truthful failure evidence.

### 4. Runtime database compatibility

- Replace the SQLite-only shared daemon connection helper with a dialect-aware abstraction backed by the configured SQLAlchemy engine or explicitly migrate its call sites to SQLAlchemy connections.
- Use named bound parameters or driver-neutral SQL. Do not implement unsafe global string replacement of `?` placeholders.
- Replace every supported runtime `last_insert_rowid()`/`lastrowid` dependency with an explicit dialect-safe insert identity path, preferably `INSERT ... RETURNING id` where supported or ORM flush semantics.
- WebSocket IPC table creation, insert/read/cleanup, notification delivery queries, archive writes, and monitor job/telemetry writes must work on both dialects without silent SQLite fallback.
- Independent daemon engines use dialect-appropriate options; PostgreSQL must never receive `check_same_thread`.
- Schema drift/health inspection uses SQLAlchemy inspection for both dialects. SQLite-only integrity checks remain SQLite-specific and PostgreSQL health reports use a real read-only connectivity/schema check.
- Narrow exception swallowing in noncritical event delivery may remain only where explicitly intentional, logged at an appropriate level, and tested not to mask database dialect failures.

### 5. PostgreSQL backup and offline restore

- Preserve the existing SQLite `.db` online backup, staged restore, startup rollback journal, validation, permissions, and tests.
- Add a PostgreSQL provider using version-compatible `pg_dump`, `pg_restore`, and libpq environment variables derived from a parsed SQLAlchemy URL. Passwords are passed only through a restricted subprocess environment, never argv, logs, manifests, or HTML.
- PostgreSQL credentials are supplied as a passwordless connection definition plus a mounted mode-0400/0600 secret file. The resolved password exists only in process memory or a child `PGPASSWORD` environment. A credential-bearing `DATABASE_URL` is rejected by the shipped container entrypoint with migration guidance; no resolved URL or password is written to `/app/backend/.env`, `/data/.env.supervisor`, compose environment, Docker image metadata, `docker inspect`-visible environment, or a management-command argument.
- PostgreSQL backups use custom archive format, `--no-owner`, and `--no-acl`. Because current ODIN tables are intentionally unqualified in `public`, backup selects the canonical ODIN object manifest with repeated exact `--table=public.<allowlisted_table>` selectors rather than dumping the whole schema. Backup files and manifests are mode 0600, checksummed, size-bounded, atomically published, and stored under canonical names.
- Validation runs `pg_restore --list`, rejects malformed archives and every non-allowlisted table, table-data, sequence, sequence-set, index, and constraint entry, plus all role, ACL, owner, extension, function, trigger, schema, database, publication, subscription, foreign-server, and procedural objects. It restores only an approved generated TOC list into a uniquely named disposable validation database before a backup is considered verified. Validation checks the current ODIN schema manifest and relational sentinel queries without exposing row data.
- Disposable validation databases are created/dropped only by an optional dedicated maintenance identity supplied through a separate mounted secret and granted `CONNECT`, `TEMP`, and `CREATEDB` for the drill. The normal ODIN application identity does not require `CREATEDB`. If no maintenance identity is configured, application backup creation fails with an actionable configuration error rather than publishing an unvalidated archive.
- Existing superadmin backup create/list/download/delete and restore-stage routes work for both dialects with dialect-specific canonical extensions. Authorization and filename/path traversal protections remain unchanged or stronger.
- PostgreSQL restore staging validates and atomically publishes only the pending candidate/manifest pair. The HTTP process never applies the candidate and never creates the rollback snapshot, because staging-time state may become stale before maintenance begins.
- PostgreSQL apply is an explicit offline maintenance command. It refuses unless an operator acknowledgement flag is present. Through its `odin-restore` maintenance connection, it queries `pg_stat_activity` for the exact configured database and refuses when any other row has `backend_type = 'client backend'` and `pid <> pg_backend_pid()`; the only exclusion is the current maintenance connection. All normal ODIN engines carry the role-specific `odin-*` names above for diagnosis, but the safety predicate rejects unrelated client sessions too. Failure to query or classify activity fails closed.
- Only after the offline/session check and ODIN advisory lock succeed, apply creates and validates a fresh pre-restore rollback archive representing the last live state, durably journals it, then invokes `pg_restore` with the approved TOC list, `--clean --if-exists --no-owner --no-acl --single-transaction`. This occurs immediately before candidate restore, not during HTTP staging.
- Before `pg_restore`, apply queries PostgreSQL catalogs for dependencies from non-ODIN objects onto canonical ODIN objects. Any such dependency is reported by object identity only and blocks restore; ODIN never uses `CASCADE` to bypass it.
- The same session-level ODIN advisory lock is held continuously from the offline/session and dependency checks through rollback archive creation, candidate restore, bootstrap/schema validation, finalization, and any rollback recovery. Loss of the lock/session is a fatal restore failure that enters recovery rather than continuing unlocked.
- Restore may clean/create only objects in the canonical public-schema ODIN manifest selected by the approved TOC list. It must not clean or replace unrelated `public` objects. It then runs bootstrap/schema validation, records an audit event, and only then finalizes.
- If candidate restore or post-restore bootstrap/validation fails, the coordinator automatically restores the verified rollback archive, validates it, records a redacted failure audit when possible, and exits nonzero. A durable journal enables recovery after interruption.
- PostgreSQL restore apply must not drop/recreate the database, mutate roles, install extensions, restore ownership/ACLs, or target any database other than the exact parsed configured database. Backup validation intentionally targets only its uniquely named disposable validation database through the separately configured maintenance identity.

### 6. Deterministic exact-image parity gate

- Add `make test-database-parity` as the local/CI entry point, implemented under `ops/database_parity/`.
- The runner builds the checked-out ODIN image once, verifies every running ODIN role container uses that exact image ID, records the immutable PostgreSQL image digest actually used, and creates unique disposable resources with a cryptographically disambiguated run ID.
- Exercise a fresh SQLite topology and a PostgreSQL 16 topology. Pin the PostgreSQL image by immutable digest in the gate. PostgreSQL is bound only to an isolated Docker network; no host database port is published.
- Run bootstrap twice, representative legacy upgrade fixtures, common API/RBAC behavior, WebSocket/event persistence, selected background database write/read paths, and backup/restore drills for both dialects.
- The common live behavior suite covers setup lock, login/session, all three roles, organization scoping, printer/model/inventory/job relationships, safe create/update/delete operations, webhook/profile/report/project/file insert identity paths, backup authorization, and a WebSocket event round trip. It contains no skips, xfails, permissive status sets, or dialect-specific expectation weakening.
- PostgreSQL proof includes a restored-data round trip: seed sentinel graph, backup, mutate/delete it, stage/apply restore offline, restart the candidate, and verify exact sentinel recovery plus continued API behavior.
- SQLite proof reruns the established backup/restore suite and a live restored-data candidate round trip so the new abstraction cannot regress existing behavior.
- A fault-injection drill proves failed post-restore migration/validation rolls back to the pre-restore state for each dialect.
- All Docker cleanup is exact-name scoped and executes on success, failure, and interruption. Pre-existing exact-name resources are rejected without inspection or deletion.

### 7. Evidence, security, and CI

- Preserve `artifacts/database-parity/<run-id>/index.html` plus a redacted JSON manifest, schema fingerprints, migration/fixture results, strict JUnit XML, backup/restore measurements, sanitized candidate logs, and failure-only diagnostics.
- Reuse the strict JUnit and artifact sanitation contracts from the candidate gate. Zero tests, skips, xfails, failures, errors, cleanup ambiguity, identity mismatch, leaked generated secrets, or an unmeasured restore phase fails the gate.
- Evidence records observed backup size and measured backup/validation/restore durations; it makes no unsupported RPO/RTO promise and contains no database row payloads.
- Add a serialized `database-parity` job on `mac-mini-runner` with bounded artifact retention. It runs only `make test-database-parity` and never pushes an image or deploys.
- Add the PostgreSQL driver and client deliberately and pin Python dependencies. Container/package security scans, Semgrep, Gitleaks, contract tests, frontend checks where affected, and the deterministic candidate gate must remain green.
- Harden `docker-compose.enterprise.yml`: require secrets rather than weak defaults, remove the default host PostgreSQL publication, pin third-party images, add role/bootstrap ownership wiring, and document operator-only exposure overrides.
- Generate and enforce a static SQLite-compatibility inventory that enumerates runtime `sqlite3`, `PRAGMA`, `check_same_thread`, qmark-parameter, `last_insert_rowid`, and `lastrowid` sites. Every inventory row must be classified as SQLite-provider-only or migrated/proven dialect-neutral; newly introduced unclassified sites fail contracts.

## Implementation Targets

- `backend/core/db.py`
- `backend/core/db_compat.py`
- `backend/core/db_utils.py`
- `backend/core/app.py`
- `backend/core/ws_hub.py`
- `backend/core/schema/` (new bootstrap, migration ledger, manifest, and upgrade code)
- `backend/core/migrations/` and module migration files only where dialect-specific correction is required
- database-backed API/daemon modules identified by the parity gate
- `backend/modules/system/backup_service.py`
- `backend/modules/system/restore_coordinator.py`
- `backend/modules/system/routes_backup.py`
- `backend/modules/system/backup_verifier.py`
- `backend/modules/system/routes_health.py`
- `backend/scripts/bootstrap_database.py` (new)
- `backend/requirements.txt`
- `docker/entrypoint.sh`
- `Dockerfile`
- `docker-compose.enterprise.yml`
- `Makefile`
- `.github/workflows/ci.yml`
- `ops/database_parity/` (new)
- `tests/database_parity/` (new)
- `tests/fixtures/database_legacy/` (new)
- `tests/backup_restore/`
- `tests/test_contracts/`
- `docs/BACKUP_RESTORE_RUNBOOK.md`
- `docs/EDU_READINESS_GUIDE.md`

## Acceptance Criteria

- A fresh checked-out candidate image boots successfully against isolated SQLite and PostgreSQL 16, and its readiness endpoint reports the correct dialect plus a validated current schema.
- The shipped image contains a pinned compatible PostgreSQL Python driver and PostgreSQL client tools. PostgreSQL connections use a passwordless definition plus a mounted secret file; credentials and credential-bearing URLs do not appear in argv, logs, artifacts, committed files, generated runtime env files, supervisor configuration, Docker image metadata, compose/container environment, or `docker inspect` output.
- Entrypoint commands are honored. The monolithic default still launches the established supervised processes, while enterprise roles run only their configured command.
- Exactly one enterprise role owns bootstrap; concurrent bootstrap attempts serialize safely and a non-owner cannot serve against an incomplete schema.
- Migration history is transactional, checksum-verified, idempotent, and fail-closed. No broad duplicate/error swallowing remains in the migration path.
- Every committed legacy fixture converges on both dialects with sentinel data and relationships preserved; malformed/conflicting input fails truthfully.
- Required schemas on SQLite and PostgreSQL match the canonical manifest, modulo explicitly documented dialect type/default differences.
- The common API/RBAC/WebSocket/background-write suite passes identically on both dialects with zero skips/xfails and strict expected responses.
- No supported PostgreSQL runtime path opens `/data/odin.db`, issues PRAGMA, passes `check_same_thread`, or calls SQLite identity functions.
- Superadmin backup create/list/download/delete/stage works on both dialects; non-superadmins remain denied and canonical path protections reject traversal/noncanonical names.
- PostgreSQL backup validation restores into a disposable database and rejects corrupt or unsafe archives before publication or staging.
- Offline PostgreSQL restore refuses while another ODIN application session is active or without explicit acknowledgement; a successful drill restores exact sentinel state and resumes API behavior.
- Offline PostgreSQL restore also refuses any unrelated client connection to the target database, creates its rollback only after that refusal check and immediately before restore, and cannot alter non-ODIN objects in `public`.
- Injected post-restore failure automatically recovers the verified pre-restore state on both dialects and produces a redacted failure record.
- `make test-database-parity` is deterministic, exact-image, isolated, cleanup-safe, and produces truthful clickable HTML evidence with measured backup/restore durations.
- Complete contract/security checks and `make test-candidate` remain green after the database changes.
- Independent adversarial implementation review reports zero blockers.
- The implementation is committed locally. No push, tag, deploy, or production mutation occurs.

## Verification Plan

1. Run independent review against this exact spec path and resolve all blockers before implementation.
2. Add focused red contracts for entrypoint command honoring, driver/client presence, bootstrap ownership, migration ledger/checksums/locks, no broad migration exception swallowing, schema manifest parity, and no PostgreSQL SQLite fallbacks.
3. Implement the shared bootstrap and migration layer; prove fresh and second-run convergence for both dialects.
4. Add and run legacy fixture upgrades on both dialects, including fail-closed conflict injection.
5. Run the common API/RBAC/WebSocket/background database behavior suite and remove every proven dialect incompatibility.
6. Implement the PostgreSQL backup provider and offline restore coordinator while preserving all SQLite restore tests.
7. Run successful, corrupt-candidate, unsafe-archive, live-session-refusal, interruption, and rollback fault-injection drills.
8. Implement the exact-image parity runner, sanitized report, strict JUnit checks, and exact cleanup assertions; run it twice from fresh state.
9. Add CI wiring and documentation, then run complete contract, backup/restore, security, candidate-gate, build/lint, schema, and secret/static scans.
10. Inspect git diff/staging and generated artifacts for secrets, database payloads, unsafe commands, untracked binaries, and Docker residue.
11. Run independent adversarial implementation review, fix every blocker, rerun affected verification, and repeat until ship.
12. Commit locally with a descriptive message and stop before push/tag/deploy.

## Blockers

- Final implementation review passes 6 and 7 returned `SHIP`. Snapshot drift, dictionary-key `check_same_thread`, generated placeholder lists, PostgreSQL topology, artifact sanitation, and Python-version-stable f-string inventory are all closed. No implementation-review blocker remains.
- PostgreSQL restore safety depends on proving all ODIN application sessions remain offline throughout dependency inspection, rollback capture, candidate apply, validation, and rollback; the implementation must fail closed where that cannot be established.
- This objective cannot provide physical printer protocol evidence; that remains objective 4.

## Verification Evidence

- 2026-09-12 implementation checkpoint: added the centralized credentialless database configuration/engine factory, checksum ledger, PostgreSQL advisory-lock bootstrap, canonical schema manifest, legacy Python/SQL migrations, readiness dialect/fingerprint reporting, command-honoring entrypoint, role-specific supervisor configurations, PostgreSQL 16 client plus psycopg 3.3.5, and dialect-safe runtime query/identity adapters.
- Legacy fixture evidence: all five deterministic fixtures (`pre_mfa_org`, `intermediate_capabilities`, `narrow_idempotency`, `uppercase_enums`, `current_noop`) converged on both SQLite and an actual PostgreSQL 16 container; malformed input failed closed without ledger rows.
- Schema evidence: 72 schema consistency/foundation assertions passed; an actual PostgreSQL bootstrap exposed exactly 59 canonical tables; the shared DBAPI adapter passed an actual PostgreSQL probe.
- Backup evidence: a custom-format, public-schema-only archive passed strict TOC validation and a disposable-database restore with 59 tables and 478 allowlisted entries. The exact-image offline drill passed wrong-acknowledgement refusal, live-session refusal, successful sentinel restore, injected post-restore rollback, and simulated process-interruption recovery.
- Security/operations checkpoint: PostgreSQL credentials remain in mode-restricted mounted files; empty application-secret `ENV` declarations were removed from image metadata; the enterprise topology now separates bootstrap-admin, non-superuser application, and non-superuser `CREATEDB` maintenance identities; the compose config renders successfully.
- Focused verification after backup/verifier changes: 34 SQLite/PostgreSQL backup, restore, bootstrap, and source-contract tests passed with one pre-existing Pydantic deprecation warning.
- Harness root cause/fix: Docker Desktop could not bind the original `/tmp` password path, and a `/tmp`-mounted Python test lacked `/app/backend` on `PYTHONPATH`; the runner now preflights shared-workspace secrets, mounts its named database volume, provides the exact backend import path, allows bounded slow initialization, and cleans all resources.
- 2026-09-12 pass-2 closure evidence: two fresh full parity runs passed seven phases each. The latest retained manifest records one SQLite and two PostgreSQL backup size/timing/table/TOC/schema/relationship-graph evidence rows; the relationship graph SHA is identical across providers and attempts. SQLite performs a real container restart and restored authenticated write; PostgreSQL performs exact graph restore plus a fresh authenticated process write. The HTML classifies isolated network/authentication posture and known test-only warnings.
- 2026-09-12 broad verification: 772 contract tests passed; security passed 14 operational tests, Gitleaks, pip-audit, npm audit, Bandit, Semgrep (0 findings), and Hadolint; candidate gate passed 4 API and 9 browser tests.
- 2026-09-12 pass-3 delta evidence: integrated parity run `20260912t224000z-topology-sanitize-2` passed. Both PostgreSQL attempts booted a named API/bootstrap-owner and non-owner reports worker through the candidate entrypoint using the immutable candidate image ID; manifest/HTML record matching per-role IDs and the actual pinned PostgreSQL digest. All retained text artifacts were redacted and rescanned with candidate-gate secret policy. The generated inventory now covers 176 executable sites and focused database verification passes 122 assertions.
- 2026-09-12 pass-7 delta evidence: the final inventory covers 179 sites, includes dictionary-key `check_same_thread`, generated qmark lists, and upstream dynamic f-string fragments, and produces the identical serialized SHA-256 `604fb6447b7445bcc9eaae022f5e3d347699270bce6159d5406009cce6dfcf77` under Python 3.11.15 and 3.14.5. All 21 focused foundation contracts passed locally; independent review pass 7 returned `SHIP` with no blockers.
- 2026-09-12 final broad verification: all 777 contract tests passed; the complete security gate passed 14 operational assertions, Gitleaks, pip-audit, npm audit, Bandit, Semgrep with zero findings, and Hadolint; the candidate-image gate passed 4 API/RBAC plus 9 browser tests.
- 2026-09-12 final database parity run `20260912t230457z-0a967d94-8ca1d4` passed all seven phases. It booted one exact-image SQLite runtime and two independent PostgreSQL API/bootstrap-owner plus reports-worker topologies, passed 124 host/provider assertions with zero skips/xfails, preserved the identical relational graph fingerprint across all three database evidence rows, recorded exact candidate/PostgreSQL image identities, sanitized retained artifacts, and removed every disposable resource.

- 2026-09-12: persistent goal confirmed active; objective 1 remains committed locally and the worktree was clean at rehydration.
- 2026-09-12: source audit confirmed `psycopg`/`psycopg2` and PostgreSQL client tools are absent from the shipped image.
- 2026-09-12: source audit confirmed unconditional SQLite restore, enum, upgrade, and WAL operations in the entrypoint.
- 2026-09-12: source audit confirmed the enterprise compose commands are passed to an entrypoint that currently discards them.
- 2026-09-12: source audit confirmed migration string translation, semicolon splitting, broad duplicate suppression, and absence of a ledger/advisory lock.
- 2026-09-12: source audit found SQLite-only shared daemon access across WebSocket, notification, archive, and monitor paths, plus remaining SQLite insert-identity calls in API/background modules.
- 2026-09-12: source audit confirmed PostgreSQL backup routes intentionally return 501 and no automated PostgreSQL backup/restore drill exists.
- Independent spec review pass 1: `FAIL` with four blockers—staging-time rollback could be stale, session-offline proof lacked exact connection identification/predicate, credential-bearing URLs could persist in runtime/container metadata, and whole-schema restore could mutate unrelated `public` objects.
- Pass 1 fixes: rollback is captured only after the offline check immediately before apply; all ODIN engines have allowlisted role names and restore rejects every other target-database client session; PostgreSQL uses a passwordless definition plus mounted secret file with no persisted resolved URL; dump/restore is restricted to a strict canonical ODIN object/TOC manifest. Advisories incorporated for health-route ownership, explicit ledger identities, static SQLite compatibility inventory, per-role candidate image checks, and recorded PostgreSQL digest.
- Independent spec review pass 2: `PASS` with zero blockers. Final advisories incorporated: distinguish validation-database targeting from restore apply, require a separate optional `CREATEDB` maintenance identity, block non-ODIN dependencies before clean restore, and hold the session advisory lock continuously through finalization or rollback.

## Next Concrete Action

Inspect the staged diff, make the local Objective 2 commit, generate one clean-tree parity artifact, and proceed to Objective 3.

## Review History

### Pass 1 — independent Codex — 2026-09-12

- Blocker: PostgreSQL rollback created during live HTTP staging could be stale by offline apply time.
- Blocker: connection naming and the exact `pg_stat_activity` safety predicate were under-specified.
- Blocker: credential acceptance criteria did not prohibit resolved URLs in runtime env files, supervisor configuration, or Docker metadata.
- Blocker: whole-`public` schema restore could clean unrelated objects.
- Advisories: include health readiness, explicit ledger ownership/checksums, a static SQLite compatibility inventory, all-role image identity, and PostgreSQL digest evidence.
- Result after iteration: all blockers and advisories incorporated; re-review required.

### Final implementation review pass 1 — independent Codex — 2026-09-12

- Blocker: PostgreSQL archive validation allowed non-table TOC entries without checking their exact canonical identity or parent.
- Blocker: unledgered legacy migrations skipped existing columns by name without validating type, nullability, or default shape.
- Blocker: a reconnecting writer could mutate the live database during rollback capture between point-in-time session checks.
- Blocker: missing maintenance credentials fell back to the application password instead of failing closed.
- Blocker: the database parity runner executed its SQLite phase on the host rather than booting and verifying the exact candidate image.
- Result: `FAIL`; all five blockers must be fixed and independently re-reviewed.

### Pass 2 — independent Codex — 2026-09-12

- Zero blockers; all four pass-1 safety defects are resolved.
- Advisories: clarify validation database targeting, specify maintenance-role privileges, reject external dependencies on ODIN objects, and hold the advisory lock throughout restore/recovery.
- Result: `PASS`; all advisories incorporated and implementation may proceed.

### Final implementation review pass 2 — independent Codex — 2026-09-12

- Blocker: the Python legacy-column migration skipped existing columns by name and validated presence rather than exact type/nullability/default shape.
- Blocker: `_pgpass_file` wrote the resolved PostgreSQL password to a generated filesystem artifact.
- Blocker: PostgreSQL backup selected the entire `public` schema rather than emitting repeated exact canonical table selectors.
- Blocker: external dependency inspection covered tables but not canonical enum types or sequences.
- Blocker: the common exact-image workflow omitted organization scoping and several domain/insert-identity/backup-authorization paths.
- Blocker: restore proof used one scalar marker rather than a relational graph and omitted restart/continued API checks plus exact-image SQLite restore proof.
- Blocker: retained parity evidence omitted structured backup size, schema fingerprints, TOC counts, and measured backup/validation/restore durations.
- Advisories: remove remaining wrapper-level maintenance-secret fallback expressions; eliminate or explicitly classify SQLite exact-image startup warnings.
- Result: `FAIL`; all seven blockers require implementation and independent re-review.

### Final implementation review pass 3 — independent Codex — 2026-09-12

- Confirmed all seven pass-2 blockers are substantively resolved.
- Blocker: PostgreSQL parity does not boot named API/bootstrap/non-owner role containers through the candidate entrypoint or record per-role image IDs and the PostgreSQL image digest.
- Blocker: parity artifact retention scans only credential-bearing URLs rather than reusing candidate-gate redaction and `scan_text_for_secrets` for generated secrets and recognizable token/assignment patterns.
- Blocker: the required generated/enforced static inventory of runtime `sqlite3`, `PRAGMA`, `check_same_thread`, qmark, `last_insert_rowid`, and `lastrowid` sites is missing.
- Advisory: final evidence should be generated from a clean committed tree.
- Result: `FAIL`; all three blockers require implementation and independent re-review.

### Final implementation review pass 4 — independent Codex — 2026-09-12

- Confirmed the named booted immutable PostgreSQL role topology/digest evidence and strict artifact redaction/rescanning blockers are resolved.
- Blocker: initial AST inventory only recognized qmark SQL passed as a direct string constant and omitted f-string/dynamically assembled SQL sites.
- Advisory: explicitly document network quiescence as the continuous exclusion boundary for unrelated PostgreSQL clients during offline restore; incorporated in Markdown and HTML runbooks.
- Advisory: final evidence must be generated from a clean committed tree.
- Result: `FAIL`; qmark inventory detection was expanded and the inventory regenerated from 120 to 176 sites; independent re-review required.

### Final implementation review pass 5 — independent Codex — 2026-09-12

- Confirmed the immutable PostgreSQL role topology/digest evidence and artifact sanitation blockers remain closed.
- Blocker: committed inventory drifted from the scanner output during review.
- Blocker: `check_same_thread` detection covered AST keyword arguments but not the actual dictionary key in `connect_args`.
- Blocker: bare qmark literals nested in generated placeholder lists such as `','.join('?' * len(ids))` were omitted.
- Result: `FAIL`; scanner and regression-test corrections implemented; regeneration and independent re-review required.

### Final implementation review pass 6 — independent Codex — 2026-09-12

- Confirmed all three pass-5 blockers closed and no regression to immutable PostgreSQL role topology/digest or artifact sanitation.
- Result: `SHIP`; scanner verification passed with 179 Python 3.11 sites and 11 focused source contracts passed in the read-only review sandbox.
- Advisory: Python 3.14 reported different f-string child line locations/counts than the pinned Python 3.11 toolchain. The scanner now inventories a `JoinedStr` at its stable parent location and ignores version-dependent child-constant locations; focused independent re-review is required after this delta.

### Final implementation review pass 7 — independent Codex — 2026-09-12

- Confirmed Python 3.11.15 and 3.14.5 generate the identical 179-site inventory and serialized SHA-256.
- Confirmed the dynamic `printer_health.py` fragment, direct execute f-strings, generated qmark placeholder lists, dictionary-key `check_same_thread`, topology assertions, and sanitizer assertions remain covered.
- Result: `SHIP`; zero blockers. Default Python lacks pytest, but the pinned Python 3.11 toolchain passed the focused regression tests and both interpreters passed scanner/direct probes.
