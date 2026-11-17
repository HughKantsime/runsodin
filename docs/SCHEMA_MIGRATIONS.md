# Schema migrations after 027

This document explains how to add a database schema migration numbered 030 or higher. The first registered one is 032 (quiet-hours deferred deliveries); 030 was withdrawn and 031 is not registered.

## How it works

- **Startup always uses the live sources.** On every start, the container runs `python -m scripts.bootstrap_database`, which calls `core.schema.bootstrap_database`. That applies migrations 001 to 027, then each entry of `bootstrap.POST027_MIGRATIONS` in order, then records `orm-baseline:1` and validates the schema. SQLite and PostgreSQL share this one path.
- **Old PostgreSQL backups never run live sources at their old schema.** This covers 025-, 026- and 027-era archives, which are archives whose ledger records none of the post-027 migrations. They are validated and imported by `core/schema/preserved029/`. That is a byte-frozen copy of the restore-stage sources at commit `ba0b902`, run in an isolated child process (`python -I runner.py`). The child imports the archive into a fresh `odin_validate_*` database owned by the parent process. Live sources then upgrade that database, and its dump takes the ordinary current restore path. The routing lives in `modules/system/pre026_restore_stage.py`.
- **Archives from builds between releases are refused.** If an archive's ledger records only some of the post-027 migrations, it was made by an unreleased build. It is refused with a clear message.
- **Frozen artifacts are never edited or re-pinned.** They are:
  - `pre026_restore_contract.py`
  - `pre026_restore_integrity.py`
  - `prepriced_restore_contract.py`
  - `preserved027/`
  - `preserved029/bundle/`, its `manifest.json` and the `integrity.py` pin of that manifest

  `tests/test_contracts/test_preserved029_bundle.py::test_frozen_restore_artifacts_are_byte_identical` enforces this for the first four. `preserved029/` checks its own manifest pin when it runs (`integrity.check_package`). If the correct design seems to need an edit to any of them, stop and ask.
- **preserved029's wrapper files freeze at its first release.** The wrapper files are `preserved029/__init__.py` (the parent bridge) and `preserved029/runner.py` (the child). They are reviewed live code, pinned with the whole package by `PRESERVED029_TREE`. Until the first release that ships preserved029, a reviewed change to them updates that digest in the same commit, with a message saying so; the bundle never changes. Once a release ships preserved029, the whole package is frozen like `preserved027/`, and a change needs a new `preservedNNN` package.

## Procedure: register migration 0NN

1. **Write the migration.** Create `backend/core/schema/migrations/0NN_<name>.py` with:
   - `MIGRATION_ID = 'python:0NN-<name>'`
   - `apply(connection)`, which must be idempotent: create the object only if it is missing, and validate the shape of an existing one.
   - `validate(connection)`, which checks the exact shape on both SQLite and PostgreSQL.

   Rules for the migration:
   - **It is additive and can never fail because of existing user data.** It may create tables, columns and indexes that no stored row can violate, and validate their exact shape. It must not add a uniqueness, check or foreign-key constraint over data a released version could already hold, and it must not rewrite data. Enforce data-dependent invariants in the application layer (serialised API checks plus an admin warning for existing violations). See "Why data-dependent constraints are excluded" below.
   - It creates everything it needs itself. It must never rely on `Base.metadata.create_all`, because upgraded and restored databases only get the migration's DDL.
   - **Pin any schema module it delegates to, like the migration itself.** The ledger records only the migration file's SHA-256. If the DDL lives in another module (032 delegates to `core/quiet_hours_deferral_schema.py`), the migration pins that module's bytes: a `SOURCE_SHA256` constant and a `_source()` check in both `apply` and `validate` that raises `MigrationError` before any DDL. Add a test that hashes the module and one that refuses a changed pin. This is the pattern from 027 and 032 onward; a schema change then needs a new migration.
   - If it begins its own SQLite transaction, it uses `BEGIN IMMEDIATE` only when the connection is not already in a transaction.
   - The file is final once merged. The ledger records its SHA-256, so any later byte change makes every upgraded database refuse to boot.
2. **Register it in two places.** Use the next number; numbers must strictly increase.
   - Add `"core.schema.migrations.0NN_<name>"` to `_ALLOWED_PYTHON_MIGRATIONS` in `core/schema/migrator.py`, under the post-027 comment.
   - Append `("python:0NN-<name>", "core.schema.migrations.0NN_<name>")` to `POST027_MIGRATIONS` in `core/schema/bootstrap.py`.
   - Never add it to a `contract` branch of `_bootstrap_database_contract`.
3. **Put every new table in the backup inventory.** `postgres_backup_service.canonical_tables()` is the union of the shared ORM `Base.metadata`, `RAW_REQUIRED_TABLES`, the keys of `RAW_REQUIRED_COLUMNS` and the ledger.
   - **Why it is required.** A PostgreSQL backup refuses a live schema whose tables differ from that set (`validate_live_postgres_objects`). A restore refuses table data outside it, or missing from it (`_ordered_restore_data`).
   - **Tables on their own MetaData object.** This applies, for example, to `core/quiet_hours_deferral_schema.py` for 032. Add the table name to `RAW_REQUIRED_TABLES`, and its exact column set to `RAW_REQUIRED_COLUMNS` (`frozenset("col1 col2 ...".split())`), as 025-027 did. Otherwise every backup fails once the migration has run.
   - **Columns added to existing tables.** Add them to `RAW_REQUIRED_COLUMNS[table]`, unless they are ORM columns.
   - **New PostgreSQL functions or triggers.** Add them to `modules/system/postgres_canonical_executables.py`.
   - **No separate create step is needed after restoring an older archive.** Archives taken before the migration are either 027-era, so the preserved029 stage imports them and live `bootstrap_database` then runs the migration on the stage, or current ones, which `_restore_postgres_archive` re-bootstraps after the data import. Either way the migration must create its objects when they are missing (step 1).
   - **Owned rows need lifecycle code.** If the table holds per-user or per-organisation rows, the same change must add their cleanup to user and organisation deletion, and to retention or pruning where the data is time-bound. It must also add tests for both. Backups only carry what the inventory lists; they do not make orphaned rows correct.
4. **Update the ORM if the change touches a model.**
   - **Declare the column last.** A new ORM column must be the last column of its model class, after any earlier post-027 additions to the same table. A fresh `create_all` then produces the same column order as an upgrade's `ALTER TABLE ... ADD COLUMN`. PostgreSQL restore validation compares `CREATE TABLE` text exactly.
   - **Add a new baseline entry.** Add an entry to `core/schema/orm_baselines.json` with `manifest` taken from `bootstrap._orm_manifest()` after `import_all_models()`, and `checksum` set to the sha256 of that manifest as compact sorted JSON. Point `CURRENT_ORM_BASELINE` at it.
   - **Never edit, re-checksum or delete an existing entry.** Existing databases recorded those checksums. A wrongly named entry may be superseded only by adding a correct one. Example: `released_1_9_24` came from the local pre-release build 9fb4ce77, not the published image. It stays, and `published_1_9_13_to_1_9_24` is the entry the published releases need.
5. **Regenerate the SQLite compatibility inventory.**
   `PYTHONPATH=backend:. python ops/database_parity/sqlite_compatibility_inventory.py --write`
6. **Add tests.** Put them in `tests/test_contracts/test_post027_migration_0NN.py`, modelled on `test_post027_migration_032.py`. They must cover:
   - the inventory lists every new table and its exact columns;
   - fresh SQLite boot applies it once, and a second boot applies nothing with the same fingerprint;
   - a pre-0NN database upgrades and keeps its rows;
   - a wrong existing shape is refused, with no ledger row;
   - the published v1.9.24 fixture upgrades through it with the entrypoint command;
   - deletion cleanup and retention for owned rows (for 032: `test_cert_E_deferred_cleanup.py` and the retention test).

   `test_post027_registry_inventory.py` already checks, for every registered migration, that a booted schema holds exactly `canonical_tables()`. It catches a table missing from the inventory without any PostgreSQL run.
7. **Run the PostgreSQL proof.**
   `make test-database-parity CANDIDATE_PYTHON=python3.11`
   This builds the candidate image and runs the proof before removing it. A standalone
   `python3.11 -m ops.database_parity.run_post027_restore` requires
   `POST027_CANDIDATE_IMAGE=sha256:<built-candidate-image-id>`; it fails closed without it.
   This is also a required part of Trusted Validation CV05. Missing images, failed pulls, timeouts, incomplete facts and skipped checks fail the gate. It must cover:
   - a native backup made by the newest recorded release below `VERSION`, restored by candidate code to the live migration registry;
   - fresh PostgreSQL boot, twice;
   - real 025, 026 and 027-era archives written by the preserved sources, each validated, staged and applied, ending at the current ledger;
   - tampered archives refused;
   - a current-schema backup→restore round trip;
   - a database written by the published v1.9.24 image's own bootstrap, upgraded, backed up and restored;
   - a row in every new table surviving the current backup→restore round trip;
   - a killed preserved029 child leaving nothing behind;
   - a real archive of the newest frozen era, written by that release's own sources from Git objects, validated, staged and applied (`POST027_RELEASED056_SOURCE` for 056), with ledger and schema refusals and a killed child;
   - a staged historical restore applied by the coordinator under enterprise-compose secret ownership (uid 10001, mode 0400); a root-run coordinator keeps the restore staged and fails; a deterministic archive refusal is quarantined;
   - a maintenance URL that carries `?sslmode=`.

   The round trip runs `create_postgres_backup`, which calls `validate_live_postgres_objects`, and then a full validate→stage→apply. So applying every registered migration is proved never to break PostgreSQL backup and restore. If the migration adds a table or column, extend step 5 of `post027_restore_native.py` to insert a row into it before the backup and assert that the row survives the restore. The standalone receipt is written to `artifacts/application-certification/migrations/post027-restore-native.json`; CV05 writes it to `native/database-parity/restore-proof/post027-restore-native.json` under its validation run. It contains 32 native facts (the existing 31 plus the previous-release backup), six coordinator facts, baseline digest, candidate commit and durations. Its hash enters CV05 evidence and the workflow uploads the receipt alongside the promotion evidence. Run once on odin-ci outside an active Trusted Validation before merging; local wiring tests do not replace that run.
8. **Run the full contract batch.**
   `ops/run_contract_batches.sh <out-dir>`
   It must exit with status 0.

## Why data-dependent constraints are excluded

Migration 030 tried to add a unique index over Spoolman links that released databases could already violate. Deferring it while duplicates existed let an archive's schema differ from its target's, which broke restores in both directions: a deferred archive onto an applied target, and the reverse. Those failures surfaced after validation had passed and the staged copy was gone, and they conflicted with the release-ledger rule. So a post-027 migration's outcome must never depend on stored data. Such invariants live in the application layer instead: Spoolman link uniqueness is enforced by the API with real serialisation, and existing duplicates are reported to the admin, never rewritten.

Migration refusals raised while validating a restore (for example a ledger checksum mismatch) are `BackupValidationError`s, which are HTTP 400 at staging, never a 500.

## Restore safety

- **The restore coordinator runs as the app user.** For PostgreSQL, the entrypoint runs `restore_coordinator` via `runuser -u odin`, the same user as the app. Mounted secrets must therefore be readable by odin and owned by it with mode 0400 or 0600, as `docker-compose.enterprise.yml` mounts them.
- **Only a deterministic archive refusal is quarantined.** `ArchiveRefused` covers TOC, schema, ledger and history refusals, preserved029 refusals, and pg_restore failing to read the file. When it is raised while the archive is validated, before the target is touched, the coordinator moves the archive and a `failed_restore_<ts>.json` record (reason and next step) into the backups directory, records `restore_failed`, and lets startup continue on the untouched target. Superadmins see it as a system notice: `GET /api/v1/system/notices` and a dashboard banner linking to Settings > System > Backups. It is deliberately not an in-app alert, since alert types are routed by the notification rules (quiet hours, webhooks, digests). The notice clears when the archive is re-staged or deleted.
  - The quarantined archive is listed first in the backups list, with its reason. It can be re-staged (`POST /backups/{filename}/stage`, "Stage again" in Settings > System) or deleted.
  - It is counted in `/health/ready` (`notices.restore_quarantined`, no detail; orphan records are not counted) and listed in the admin diagnostics bundle (`restore_quarantine`).
  - It is kept for 30 days from the quarantine: the record's `failed_at`, which the archive's reset mtime backs up. A record whose archive is gone is swept at once.
  - If the restore body fails after the target was touched and rolled back, the archive is kept the same way with status `restore_failed_rolled_back`, so it is listed and noticed, not only audited.
- **The restore-staging routes are interactive-session only.** `routes_backup.BACKUP_ROUTES_REQUIRING_INTERACTIVE` lists `POST /backups/restore` and `POST /backups/{filename}/stage`. Each carries `require_interactive_session`, so API tokens cannot stage a restore; `test_cert_F_B_credential_census.py` checks every listed route is enforced.
- **Failures are classified by type, never by message text.** The preserved029 child exits with a sysexits code chosen from the exception's type and cause: 65 means the archive was judged (including pg_restore failing to read it), 75 means retryable (a client that talked to a server, any timeout, a connection error), and 78 or 70 mean an environment or internal failure. Live `PostgreSQLClientFailed.retryable` is set the same way, so a `pg_restore --list` timeout is retried, not quarantined.
- **Everything else keeps the staged restore.** Connection-level failures (PostgreSQL not ready, DNS, a client that could not reach the server, a probe timeout) are retried with a bounded backoff of about a minute, then fail. Operator and environment errors (a missing `ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT`, unreadable credentials, a restore-gate refusal) fail at once. In both cases the process exits non-zero as before, with a log line saying the restore is kept. A failure after the target was touched still stops startup, under the existing rollback and barrier rules.
- **Disposable databases and files are always cleaned up, and only this code's.** Every `odin_validate_*` database is journaled in `<backups>/.odin-stage-journal/` before it is created, and live code drops a database only if its own `CREATE DATABASE` succeeded.
  - The preserved029 child journals into its own subdirectory: `<name>.intent` before `CREATE DATABASE`, then `<name>` once it succeeded. It works in a private `.odin-preserved029-*` directory.
  - Whatever the child's outcome, including SIGKILL, the parent drops the child's confirmed names. It drops intended names only if no database of that name existed before the child started. It never touches another operation's names, and it removes the child's directory.
  - Startup and every restore validation also sweep journaled databases and `.odin-*` working files older than six hours. The sweep drops only confirmed names; it forgets a stale `.intent`-only entry without dropping anything, because it cannot prove that database is this code's. It also removes stale empty child journals, and `rollback_*` dumps older than seven days. It keeps the newest rollback dump, and any dump an in-progress restore names (all of them if that manifest is unreadable).
  - An apply attempt that fails before the restore body runs deletes its own rollback dump.
- **Historical-restore URLs never load credentials from elsewhere.** Non-credential libpq parameters such as `sslmode` pass. `password`, `sslpassword`, `passfile`, `service`, `sslkey`, `sslcert` and `application_name` are refused.

## Global dispatch recovery (056)

`python:056-dispatch-recovery-global` creates `dispatch_recovery_global_history`
for decisions on wholly global printer/claim/job chains. It carries the 038
decision, reason and handle constraints, omitting the two org columns; sanitized
facts record `owner_org_id: null` and `charged_org_id: null`. Actor identity and
audit references remain required. UPDATE/DELETE are blocked on SQLite and
PostgreSQL, and PostgreSQL TRUNCATE is blocked. Like 038, history has no resource
foreign keys or deletion cascades: it survives resource deletion as audit evidence.
Org decisions continue using the unchanged 038 table. No existing rows are rewritten.

The live ledger, canonical table/column and PostgreSQL executable inventories
include 056. `preserved055` pins the released 2.0.49 sources at
`90d901ba86df7fee4ecaf99f69d08f48cd835c21` as an overlay on `preserved053`.
Exact migration-055 ledgers use its isolated restore child, followed by the live
056 upgrade. Existing preserved packages and published ledger entries are unchanged.
The native PostgreSQL round-trip fixture includes a global history row; running
that proof remains a release check (this change's verification is targeted contracts).

## Releases

- **Record the ledger before publishing.** `backend/core/schema/released_post027_ledgers.json` records the post-027 ids of every published release. Before publishing, run `python ops/release_control/record_post027_ledger.py` (it reads `VERSION`) and commit the entry into the release candidate. `publish-image.yml` refuses a `VERSION` that has no entry, or whose entry differs from the candidate's `POST027_MIGRATIONS`. The workflow cannot append the entry itself, because it has read-only contents and an immutable candidate SHA. Existing entries are never rewritten.
- **Record each released image before the next candidate.** Add its version, production receipt `target_digest`, and ledger schema era to `ops/database_parity/restore_baselines.json`, including releases with no schema change. The table begins at 2.2.0; its digest is recorded. The 2.2.1 entry is an explicit pending receipt and the offline TODO test fails until the real receipt is recorded. Never guess a digest or use a tag. `tests/test_contracts/test_restore_proof_gate.py` checks release coverage, strict newest-below-candidate selection, imports, facts, fail-closed paths and owned cleanup without Docker. The candidate itself is excluded because its image has not been published. A candidate equal to an existing release still selects the release below it; missing historical digests fail.
- **Freeze a bundle before stranding a release.** `test_post027_release_gate.py` requires every recorded ledger to equal the current `POST027_MIGRATIONS` or to be routed by a preserved bundle. So once a release ships post-027 migrations, the next schema change fails that test until the release's restore-stage sources are frozen as a new `preserved0NN` bundle on the `preserved029` model, and archives whose ledger matches it are routed through that bundle. This adds a new qualified contract with its own tests; it never edits an existing one.
  - **Generate it from the released commit.** `python -m ops.database_parity.freeze_preserved_bundle --commit <full sha> --release <version> --era <NN> --base-era <previous> --migration <last id> --write` selects the commit's complete `backend/core` and `backend/modules` sources from Git objects, so a refreshed module cannot lose a dependency. `--check` compares a committed bundle with the commit.
  - **Hashes do not prove it runs.** The bundle needs its own `test_preserved0NN_bundle.py`, `test_post027_release_gate.py::test_routed_bundles_can_import_their_restore_closure`, and a real archive written by the released sources in `post027_restore_native.py`. Bundles 039 to 045 cannot restore PostgreSQL archives: their manifests omit `modules/system/backup_failure_alerts.py`, so the child exits 78 and the target stays untouched. They are byte-pinned, so that repair is separate work.
- **SQLite is not affected.** SQLite databases upgrade in place through `bootstrap_database`, and SQLite backups restore as files and then upgrade on boot.

## Recipe configuration identity (043)

Migration `python:043-recipe-configuration` appends nullable
`stable_configuration_key VARCHAR(64)` and `stable_key_source VARCHAR(32)` to
`dispatch_attempt_receipts`. It does not rewrite rows. The current ORM additions
are in `core/recipe_configuration_schema.py`; the original receipt model source
and migration012 retain their byte pins. The live migrator validates012 against
043's exact historical/current receipt shapes so both pre-043 upgrades and
repeat current boots remain valid. A new ORM baseline records the added columns.

Dispatch records the new purpose-separated HMAC with source `dispatch`. Its inputs
match the full configuration identity except for `active_materials`,
`filament_slots`, and `plug_configuration`. Full dispatch identity, claims,
sender evidence, reconciliation and the AMS material gate keep their old meaning.
Recipe version 2 uses this stable identity in its existing payload
`configuration_key` field.

Legacy receipt backfill is lazy and transactional. The printer row is locked
while checking audit history, loading its current configuration, and updating
the receipt. Any printer lifecycle/edit audit at or after receipt creation
refuses backfill, including equal timestamps, missing/malformed audit times and
delete/recreate history. Earlier audits, other printers and AMS `sync` events
do not block it. Successful backfill records `backfilled_no_printer_audit`.
Legacy backfill also compares the job's `print_files.nozzle_diameter` (retained
slicer metadata) with the locked printer's current nozzle diameter. Known,
different diameters refuse without saving a key. Unknown file or printer
nozzle metadata permits backfill and retains source `backfilled_no_printer_audit`.

MQTT nozzle/model/machine-type observations, Anycubic model observations and
organization assignment now write printer audits in the same transaction, only
when the stored value changes. They therefore invalidate later legacy backfill.
Residual risk: before this fix those paths could change identity without audits.
The nozzle check catches a retained sliced/current mismatch, but missing metadata,
a change away and back, and old unaudited model/organization/bed/credential changes
cannot be reconstructed. Both legacy keys still derive from the current printer
row; matching backfilled keys are not independent proof of historical equality.
Retain audits and qualify a new revision whenever that history is uncertain.

A legacy recipe resolves through its exact reference job, receipt token, physical
observation and original full payload. Stored recipe JSON stays unchanged.
Unprovable history remains NULL and returns a 409 directing the operator to
qualify a new revision. Legacy outside-only bindings with no dispatch reference
also require a new revision. New outside qualification and binding use the
stable identity.

The recorded 2.0.18–2.0.23 ledgers end at040 and remain unchanged. Their restore
sources use the release branch's single `preserved040` bundle. The 2.0.24 ledger
adds 041 inventory-color, 042 dispatch-capability and 043 recipe-configuration,
in that order. Migration042 audits each H2D bed correction in its transaction,
so a corrected printer cannot backfill a receipt from before that correction.

## Audit integrity

The identity is defined in [audit_integrity_version.py](../backend/core/schema/audit_integrity_version.py).
The migration adds salted hash/sequence columns, chain state, redaction receipts,
retention checkpoints, SIEM destinations/outbox, coalesced license-gap counters,
and verification status. Backfill
uses canonical reads and batched updates inside one transaction. PostgreSQL also
adds the audit-integrity alert enum label; the forward validator preserves the
historical alert migration's accepted shapes without modifying its source.

Frozen through-046 sources remain unchanged for historical restore. The SQLite
append trigger is required only when the chain-state table exists. PostgreSQL
canonical executable guards include the current append function and trigger.
Native snapshot/contention/receipt proof lives in `ops/database_parity/audit_integrity_native.py`.
See [Audit integrity](AUDIT_INTEGRITY.md) for operational and privacy semantics.

## Training sign-off (048)

048 runs after the dedicated 037 membership step (alongside 039), so PostgreSQL
upgrades from pre-037 schemas have the referenced membership table. It includes
school-wide scope and durable quiz attempt/cooldown evidence. A separate `edit_version`
advances for every definition save; requirement `revision` changes only when completion
requirements change. Stale edit versions are refused without invalidating completion.
048 creates tenant-bound training definitions and completion evidence, registered in the
post027 ledger and canonical table inventory. No PostgreSQL functions/triggers are added.
The new preserved046 overlay pins the released 046 schema and routes its backups through
the existing isolated restore protocol before applying 048. Published ledger entries remain unchanged.

## Slot feed policy and S1 telemetry (057)

`python:057-slot-feed-policy` adds `filament_slots.do_not_feed` (false by
default), `filament_slots.do_not_feed_note`, and `printers.ace_status`.
It runs in the ordered release tail after 056. The audit-integrity migration
keeps its existing 047 identity; the unshipped S1 migration was renumbered.

`preserved056` freezes release 2.1.0 at
`9112d4071b88d33dbbc5b82da558dfdf74d663db`, over the unchanged preserved055
base. It routes through-056 archives into isolated released sources before
live bootstrap applies 057. Generate or check it with
`ops.database_parity.freeze_preserved_bundle`, using era 56, base era 55,
and migration `python:056-dispatch-recovery-global`. Existing preserved
packages and recorded release ledgers remain unchanged. The native proof
runner exports that release from Git objects to write its independent archive.


## Release 2.2.0 restore bundle (057)

`preserved057` freezes release 2.2.0 at
`3911bd94784e19fc47c3d0d5ffe430a13d4d15fc`, over the unchanged preserved056
base. Once the live registry advances beyond 057, exact through-057 archives
route through these isolated released sources before live bootstrap applies
only later migrations. While 057 is current, the ordinary current validator
continues to handle these archives. This bundle adds no migration or schema change.

Generate from Git objects, then check the generated bytes:

```sh
python3.11 -m ops.database_parity.freeze_preserved_bundle --commit 3911bd94784e19fc47c3d0d5ffe430a13d4d15fc --release 2.2.0 --era 57 --base-era 56 --migration python:057-slot-feed-policy --write
python3.11 -m ops.database_parity.freeze_preserved_bundle --commit 3911bd94784e19fc47c3d0d5ffe430a13d4d15fc --release 2.2.0 --era 57 --base-era 56 --migration python:057-slot-feed-policy --check
```

Existing bundles, frozen migration bytes and recorded ledgers remain unchanged.
The native PostgreSQL proof exports 2.2.0 from Git objects to write its archive
independently of this bundle. Until a later migration exists, that proof uses a
synthetic future registry only during archive identification to exercise the
preserved stage; live bootstrap still runs the real registry. The import matrix also confirms the documented 039–045 missing
`backup_failure_alerts` defect in preserved046. These existing PostgreSQL restore
defects remain separate, explicitly expected failures; no old bundle is repaired here.

### 059 — six exact filament materials (Stage 1)

`python:059-six-materials` appends `ASA_AERO`, `ASA_CF`, `PET_CF`, `PPA_CF`,
`TPU_AMS` to PostgreSQL's shared `filamenttype` enum with `ALTER TYPE ... ADD VALUE`.
`PPS_CF` already exists. The migration pins the old and current label order and
validates all four consumers (slots, jobs, models, presets), refusing drift before
recording its checksum. SQLite's `VARCHAR(11)` needs no DDL. No table is rebuilt
and no stored material is relabelled by the migration. The column manifest and
its ORM baseline remain unchanged.

After schema application, startup backfills only non-custom catalogue entries
with a catalogue code, material `OTHER`, and one of the six known catalogue type
aliases. Each conditional update is audited in the same transaction; repeat
startup is a no-op. Existing slot family values are left intact.

The `020` durable AMS evidence schema and its pins remain frozen. Ordinary Bambu
AMS dispatch uses the fresh in-memory report and succeeds on an exact match;
the Education AMS-read workflow fails persistence and returns 409 for these six.
Stage 2 is not included. Released 056/057 native restore assertions retain
`current_era` and `post_ids`; both now also validate enum 059 after upgrade.


## Release 2.3.0 restore bundle (060)

`preserved060` freezes release 2.3.0 at
`39dd3b8f97fe716058606d92b03fa9023f0fcdd4`, over the unchanged preserved057
base. Like preserved053 over base era 46, this overlay covers several migrations:
058 (filament catalogue), 059 (six materials), and 060 (stored-path index).
The generator checks the complete released registry against the independently
bootstrapped ledger and pins all three additions in the generated integrity check.
Existing single-migration bundles still reproduce their original generated bytes.

Generate from Git objects, then check the generated bytes:

```sh
python3.11 -m ops.database_parity.freeze_preserved_bundle --commit 39dd3b8f97fe716058606d92b03fa9023f0fcdd4 --release 2.3.0 --era 60 --base-era 57 --migration python:060-print-files-stored-path-index --write
python3.11 -m ops.database_parity.freeze_preserved_bundle --commit 39dd3b8f97fe716058606d92b03fa9023f0fcdd4 --release 2.3.0 --era 60 --base-era 57 --migration python:060-print-files-stored-path-index --check
```

While 060 is current, the ordinary current validator handles these archives.
Once the registry advances, exact through-060 ledgers route through the isolated
bundle before live bootstrap applies only later migrations. Routing compares
ledger content, so a 2.3.1 hotfix with the same ledger uses the same bundle; the
release-gate test exercises that equal-ledger case with a synthetic future registry.
No migration, schema change, existing bundle edit or recorded ledger edit is needed.

The native PostgreSQL proof exports the released commit separately from Git,
writes a real archive, then validates, stages and applies it through preserved060.
It checks catalogue data, exact material labels, shared file paths, relationships,
repeat bootstrap and cleanup. Before a later migration exists it uses a synthetic
registry only for identification. This proof requires the test machine's Docker
and PostgreSQL environment. The existing 039–046 import defects remain explicitly
identified in the closure matrix and are not changed by this bundle.
