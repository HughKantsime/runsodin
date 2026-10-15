# Schema migrations after 027

This document explains how to add a database schema migration numbered 030 or higher. The design decision is recorded in `.claude/specs/post027-migrations.md`.

## How it works

- **Startup always uses the live sources.** On every start, the container runs `python -m scripts.bootstrap_database`, which calls `core.schema.bootstrap_database`. That applies migrations 001 to 027, then each entry of `bootstrap.POST027_MIGRATIONS` in order, then records `orm-baseline:1` and validates the schema. SQLite and PostgreSQL share this one path.
- **Old PostgreSQL backups never run live sources at their old schema.** This covers 025-, 026- and 027-era archives, which are archives whose ledger records none of the post-027 migrations. They are validated and imported by `core/schema/preserved029/`. That is a byte-frozen copy of the restore-stage sources at commit `ba0b902`, run in an isolated child process (`python -I runner.py`). The child imports the archive into a fresh `odin_validate_*` database owned by the parent process. Live sources then upgrade that database, and its dump takes the ordinary current restore path. The routing lives in `modules/system/pre026_restore_stage.py`.
- **Archives from builds between releases are refused.** If an archive's ledger records only some of the post-027 migrations, it was made by an unreleased build. It is refused with a clear message.
- **Frozen artifacts are never edited or re-pinned.** They are:
  - `pre026_restore_contract.py`
  - `pre026_restore_integrity.py`
  - `prepriced_restore_contract.py`
  - `preserved027/`
  - `preserved029/`

  `tests/test_contracts/test_preserved029_bundle.py::test_frozen_restore_artifacts_are_byte_identical` enforces this for the first four. `preserved029/` checks its own manifest pin when it runs (`integrity.check_package`). If the correct design seems to need an edit to any of them, stop and ask.

## Procedure: register migration 0NN

1. **Write the migration.** Create `backend/core/schema/migrations/0NN_<name>.py` with:
   - `MIGRATION_ID = 'python:0NN-<name>'`
   - `apply(connection)`, which must be idempotent: create the object only if it is missing, and validate the shape of an existing one.
   - `validate(connection)`, which checks the exact shape on both SQLite and PostgreSQL.

   Rules for the migration:
   - It is additive.
   - It creates everything it needs itself. It must never rely on `Base.metadata.create_all`, because upgraded and restored databases only get the migration's DDL.
   - Data that the change would conflict with is refused loudly with a `MigrationError`, never rewritten.
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
6. **Add tests.** Put them in `tests/test_contracts/test_post027_migration_0NN.py`, modelled on `test_post027_migration_030.py`. They must cover:
   - fresh SQLite boot applies it once, and a second boot applies nothing with the same fingerprint;
   - a pre-0NN database upgrades and keeps its rows;
   - conflicting data is refused, with no rewrite and no ledger row;
   - a wrong existing shape is refused;
   - the published v1.9.24 fixture upgrades through it with the entrypoint command.

   `test_post027_registry_inventory.py` already checks, for every registered migration, that a booted schema holds exactly `canonical_tables()`. It catches a table missing from the inventory without any PostgreSQL run.
7. **Run the PostgreSQL proof.**
   `python ops/database_parity/run_post027_restore.py`
   It must cover:
   - fresh PostgreSQL boot, twice;
   - real 025, 026 and 027-era archives written by the preserved sources, each validated, staged and applied, ending at the current ledger;
   - tampered archives refused;
   - a current-schema backup→restore round trip.

   The round trip runs `create_postgres_backup`, which calls `validate_live_postgres_objects`, and then a full validate→stage→apply. So applying every registered migration is proved never to break PostgreSQL backup and restore. If the migration adds a table or column, extend step 5 of `post027_restore_native.py` to insert a row into it before the backup and assert that the row survives the restore. The receipt is written to `artifacts/application-certification/migrations/post027-restore-native.json`.
8. **Run the full contract batch.**
   `ops/run_contract_batches.sh <out-dir>`
   It must exit with status 0.

## Releases

- **Releasing post-027 migrations.** Once a release ships post-027 migrations, the next schema change makes that release's PostgreSQL archives "intermediate", and they would be refused. Before adding the first migration after such a release, freeze that release's restore-stage sources as a new `preserved0NN` bundle on the `preserved029` model. Then route archives whose ledger matches it through that bundle. This adds a new qualified contract with its own tests; it never edits an existing one.
- **SQLite is not affected.** SQLite databases upgrade in place through `bootstrap_database`, and SQLite backups restore as files and then upgrade on boot.
