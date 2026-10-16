# Merging cert-fix-F into cert-fix-D: print-attempt reconciliation

Both branches count print attempts per job in `system_config`, using the same
counter key `quota_attempt:seq:<job_id>`. cert-fix-D's `core/print_attempts.py`
is the shared module. cert-fix-F's `core/quota.py` should import the counter
from it, not keep its own copy.

## 1. `reset_job` bumps the counter once

- **cert-fix-F** (`jobs_lifecycle.reset_job`) calls `core.quota.bump_print_attempt(_sar(db), job_id)`.
- **cert-fix-D** calls `core.print_attempts.bump_print_attempt(sqlalchemy_runner(db), job_id)`, after the
  state-machine transition succeeds. The reset is refused (409) unless the job is completed, failed or cancelled.

**Keep D's call and drop F's.** The counter is shared, so calling both would advance it by 2.

In `core/quota.py`:
- Make `_attempt_seq` and `bump_print_attempt` thin wrappers over `core.print_attempts.attempt_seq` and
  `core.print_attempts.bump_print_attempt`, or delete them and import those functions directly.
- Keep F's manual quota marker `quota_attempt:job:<id>:<seq>`. D never writes it. D only writes
  `quota_attempt:seq:<id>` and `quota_attempt:filament:job:<id>:<seq>`.

## 2. Keep both delete hooks

- **cert-fix-F `delete_job`** calls `core.quota.clear_print_consumption_markers`. This removes `job:<id>:%`,
  `seq:<id>`, and `print:<print_jobs.id>` for the job's observed prints.
- **cert-fix-D** calls `core.print_attempts.clear_job_attempts`. This removes `seq:<id>`, `job:<id>:%` and
  `filament:job:<id>:%`. It runs in:
  - `delete_job`
  - bulk delete
  - the retention purge in `routes_config.py`

**Keep both calls** on every deletion path. Only F's hook removes the `print:<print_job_id>` markers, and only
D's removes the filament markers. Overlapping deletes are harmless.

`test_cert_D_job_attempt_cleanup.py::test_census_every_job_deletion_path_clears_attempt_markers` requires
`clear_job_attempts` on every path that deletes jobs. If F adds a deletion path, add `clear_job_attempts` to it.
Also add F's hook to the retention purge, which F's branch does not touch.

## 3. `record_print_consumption` follows the physical-vs-job split

cert-fix-D routes every `jobs.status` write through `modules.jobs.state_machine.transition_job`, and the census
test fails on any raw `UPDATE jobs SET status`. Each print-end handler now:
- always publishes the physical print completion, keyed by `print_job_id`;
- passes the job to job-scoped effects only when the job transition actually applied.

Make F's quota charge follow the same split.

- **`notifications/job_events.job_completed`**
  - F charges quota inside its own raw `UPDATE jobs SET status = ? ... AND status IN (...)`. Drop that raw
    UPDATE and keep D's `transition_job(cur, scheduled_job_id, status, 'monitor', ...)`.
  - Call `record_print_consumption(qmark_runner(cur), scheduled_job_id, measured_hours=...,
    attempt_key=f"print:{print_job_id}")` only when `job_applied` is true and `success` is true. Keep F's
    savepoint wrapper around it.
- **`jobs_lifecycle.complete_job`**
  - Keep F's `record_print_consumption(sqlalchemy_runner(db), job.id, grams=...)` after the deduction block.
  - It only runs on D's `applied` path (`if not applied:` returns before any side effect), so a retried
    completion is never charged twice.
  - F's manual marker `job:<id>:<seq>` stays on the same attempt for a complete → fail → complete cycle, the
    same as D's filament marker.
- **MQTT (`mqtt_job_lifecycle.record_job_ended`) and Moonraker end paths.** If F charges quota there, gate it
  on D's `job_applied` / `ended` result, not on the physical status.
- **`core/quota.py` insert-or-ignore.** F uses `_insert_ignore()` / `_on_conflict_ignore()`, which pick the
  dialect from `IS_SQLITE`. D's `mark_filament_deducted` uses `INSERT ... ON CONFLICT (key) DO NOTHING`, which
  is valid on SQLite 3.24+ and PostgreSQL alike. Either works, because `system_config.key` is the primary key.

## Tests to run after the merge

- **D:**
  - `test_cert_D_job_state_machine.py` (census and physical/job split)
  - `test_cert_D_jobs_lifecycle_idempotency.py` (attempt cycles, pre-upgrade data, Moonraker)
  - `test_cert_D_job_attempt_cleanup.py`
- **F:** `test_cert_F_C_quota_once.py`.
- **PostgreSQL probe:** `ops/database_parity/run_certd_fixes.py`, plus F's PG probe.
