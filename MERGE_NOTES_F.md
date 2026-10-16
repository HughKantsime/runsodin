# Merging cert-fix-F into cert-fix-D: print-attempt reconciliation

Both branches count print attempts per job in `system_config`, using the same
counter key `quota_attempt:seq:<job_id>`. cert-fix-D's `core/print_attempts.py`
is the shared module. cert-fix-F's `core/quota.py` should import the counter
from it, not keep its own copy.

## 1. `reset_job` bumps the counter once

- **cert-fix-F** (`jobs_lifecycle.reset_job`) calls `core.quota.bump_print_attempt(_sar(db), job_id)`.
- **cert-fix-D** calls `core.print_attempts.bump_print_attempt(sqlalchemy_runner(db), job_id)` after the
  state-machine transition succeeds. The reset is refused (409) unless the job is completed, failed or cancelled.

**Keep D's call and drop F's.** The counter is shared, so calling both would advance it by 2.
D's bump also writes `quota_attempt:started:<id>:<seq>`, the reset time that the filament ledger uses
as the attempt's anchor. F's bump does not, so a reset that goes through F's bump would leave the
ledger without an anchor.

In `core/quota.py`:
- Make `_attempt_seq` and `bump_print_attempt` thin wrappers over `core.print_attempts.attempt_seq` and
  `core.print_attempts.bump_print_attempt`, or delete them and import those functions directly.
- Keep F's manual quota marker `quota_attempt:job:<id>:<seq>`. D never writes it.

## 1a. F's quota markers and D's filament ledger coexist

Quota charging (F, `core/quota.py`) and filament deduction (D, `core/filament_ledger.py`) are separate
claims on separate keys. Both live in `system_config`, and neither reads or writes the other's keys:

| Key | Owner | Meaning |
|---|---|---|
| `quota_attempt:seq:<job>` | shared counter, written by D's `bump_print_attempt` | the job's attempt number |
| `quota_attempt:started:<job>:<seq>` | D | reset time; the ledger's attempt anchor |
| `quota_attempt:job:<job>:<seq>` | F | quota charged for that job attempt (operator completion) |
| `quota_attempt:print:<print_job>` | F | quota charged for that physical print (monitor) |
| `quota_attempt:filament:job:<job>:<seq>` | D ledger | filament deducted for the job attempt; value `{"source", "print_job_id"}` |
| `quota_attempt:filament:job:<job>:print:<print_job>` | D ledger | filament deducted for that physical print |
| `quota_attempt:filament:print:<print_job>` | D ledger | the same, for a print with no job |
| `quota_attempt:print_seq:<print_job>` | D | the job attempt a physical print belongs to, `{"job_id", "seq"}`; bound when the print is created or linked |

- **One claim does not imply the other.** A quota charge without a deduction is correct, for example
  when the spool balance is unknown (the ledger claims nothing then). So is a deduction without a quota
  charge, for a user without a quota period. Do not gate one on the other's marker.
- **Use the same scope on both sides.** The ledger claims per physical print when the print is known
  (archive, Moonraker), and per job attempt otherwise (operator `/complete`). F already uses the same
  split (`print:<id>` from monitors, `job:<id>:<seq>` from completion).
- **A print's attempt is the print's own, not the job's current one.** A late end or archive of a
  print, including `recover_missing_archives` at startup, can run after the job was reset. Resolve the
  attempt with `core.filament_ledger.print_attempt(run, job_id, print_job_id)`: the `print_seq` binding,
  else the reset window around `print_jobs.started_at`. Do not use `attempt_seq(run, job_id)`. This
  matters to F wherever a print-scoped quota charge falls back to a job-attempt key.
- **Lock order.** The ledger locks spool rows in id order and then claims keys; it never takes a spool
  lock after a claim. Keep F's quota writes (the claim, then the `quota_usage` upsert) after the ledger
  call in the same transaction. Never interleave them before it, or the archive and operator paths
  can deadlock on PostgreSQL again.
- **Values must be valid JSON.** `system_config.value` is a JSON column on PostgreSQL, and a bare word
  there raises InvalidTextRepresentation. F's `"true"` is fine. Keep any new value JSON.
- **F's quota grams.** If F's `record_print_consumption` should charge what was actually deducted
  rather than the estimate, pass `sum(entry["grams"] for entry in outcome.deductions)` from the ledger's
  `LedgerResult` in `complete_job`. Today D passes nothing, and F's `job_material_grams` estimate applies.
- **Census.** `test_cert_D_filament_ledger.py::test_census_no_filament_stock_writer_outside_the_ledger`
  runs on the shared AST+SQL scanner (`tests/test_contracts/job_census.py`, targets `SPOOLS` and
  `SPOOL_USAGE`). It fails on any new `spool_usage` insert or spool weight write outside the ledger.
  F's quota code writes neither. If F adds a dynamic-table SQL helper, the census lists it per call
  site, and it needs a checked allowance proving it never addresses spools or `spool_usage`.

## 2. Keep both delete hooks

- **cert-fix-F `delete_job`** calls `core.quota.clear_print_consumption_markers`. This removes `job:<id>:%`,
  `seq:<id>`, and `print:<print_jobs.id>` for the job's observed prints.
- **cert-fix-D** calls `core.print_attempts.clear_job_attempts`. This removes `seq:<id>`, `job:<id>:%`,
  `filament:job:<id>:%` (the ledger's per-attempt and per-print markers for the job) and `started:<id>:%`.
  It runs in:
  - `delete_job`
  - bulk delete
  - the retention purge in `routes_config.py`

**Keep both calls** on every deletion path. Only F's hook removes the `print:<print_job_id>` markers, and only
D's removes the filament markers. Overlapping deletes are harmless.

`test_cert_D_job_attempt_cleanup.py::test_census_every_job_deletion_site_clears_attempt_markers` requires
`clear_job_attempts` in every function that deletes jobs. This covers SQL, quoted names and aliases,
`session.delete(job)`, `delete(Job)` and `query(Job).delete()`. If F adds a deletion path, call
`clear_job_attempts` in that same function.
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
  - Keep F's `record_print_consumption(sqlalchemy_runner(db), job.id, grams=...)` after the ledger call
    (`record_job_filament(..., source="operator_complete")`).
  - It only runs on D's `applied` path (`if not applied:` returns before any side effect), so a retried
    completion is never charged twice.
  - F's manual marker `job:<id>:<seq>` stays on the same attempt for a complete → fail → complete cycle, the
    same as D's filament marker.
- **MQTT (`mqtt_job_lifecycle.record_job_ended`) and Moonraker end paths.** If F charges quota there, gate it
  on D's `job_applied` / `ended` result, not on the physical status. In the Education branches, use
  `applied` (authorised and transitioned), not `physical`. A refused Education end now still records the
  physical end (`physical_ended`), but that is not a job charge.
- **`core/quota.py` insert-or-ignore.** F uses `_insert_ignore()` / `_on_conflict_ignore()`, which pick the
  dialect from `IS_SQLITE`. D's `mark_filament_deducted` uses `INSERT ... ON CONFLICT (key) DO NOTHING`, which
  is valid on SQLite 3.24+ and PostgreSQL alike. Either works, because `system_config.key` is the primary key.

## Tests to run after the merge

- **D:**
  - `test_cert_D_filament_ledger.py` (ledger paths, concurrency, writer census)
  - `test_cert_D_job_state_machine.py` (census and physical/job split)
  - `test_cert_D_jobs_lifecycle_idempotency.py` (attempt cycles, pre-upgrade data, Moonraker)
  - `test_cert_D_job_attempt_cleanup.py`
- **F:** `test_cert_F_C_quota_once.py`.
- **PostgreSQL probe:** `ops/database_parity/run_certd_fixes.py`, plus F's PG probe.
