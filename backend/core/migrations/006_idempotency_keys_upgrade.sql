-- core/migrations/006_idempotency_keys_upgrade.sql
-- v1.8.9 — upgrade path for environments that applied an earlier,
-- narrower version of `idempotency_keys`.
--
-- Codex pass 16 (2026-04-15) flagged that the agent-native branch
-- went through several iterations of the 005 migration during
-- development. An environment that deployed an intermediate commit
-- (e.g. 0c192bb, where `idempotency_keys` lacked `state`,
-- `updated_at`, `auth_fingerprint`) would keep that stale schema
-- because `CREATE TABLE IF NOT EXISTS` in the current 005 sees the
-- existing table and no-ops. The middleware then selects and writes
-- the new columns at runtime, and every Idempotency-Key request
-- raises a SQL error — converting a retry primitive into a 500
-- every single time.
--
-- This migration is idempotent on a FRESH install (ALTER TABLE ADD
-- COLUMN on an already-complete schema raises "duplicate column
-- name" which the migration loader swallows). On an UPGRADED install
-- it backfills the missing columns + index so the table matches the
-- current middleware contract. On a fully NEW install (first boot
-- with both 005 and 006) it's also a no-op because 005 already
-- created the full schema.

ALTER TABLE idempotency_keys ADD COLUMN state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE idempotency_keys ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
ALTER TABLE idempotency_keys ADD COLUMN auth_fingerprint TEXT NOT NULL DEFAULT '';

-- Backfill updated_at ONLY for rows that were already present when
-- the column was added (it now has the default ''). Populate with
-- created_at so ordering/pruning stays consistent. This is
-- structural, not semantic.
UPDATE idempotency_keys
   SET updated_at = created_at
 WHERE updated_at = '';

-- Codex pass 17 (2026-04-15): DO NOT rewrite `state='pending'` to
-- `'complete'`. The earlier draft blanket-UPDATE'd every pending
-- row assuming they were all legacy. During a rolling deploy or a
-- restart of a RUNNING middleware, real in-flight claims (written
-- seconds ago by the new middleware, waiting for finalize) ALSO sit
-- at state='pending'. That UPDATE would silently convert an active
-- claim into a forged complete row with an empty response body —
-- subsequent retries would replay that empty body as a success.
--
-- Instead: do nothing to state. Any legacy pending rows (from a
-- brief window of the intermediate schema, where state didn't
-- exist) are extremely few and will be handled by:
--   1. The 15-minute `_PENDING_WATCHDOG_SECONDS` in the middleware
--      reclassifies them as stuck_pending and the next request
--      takes them via CAS.
--   2. The hourly pruner deletes stuck-pending rows.
-- Losing at most 15 minutes of idempotency protection on a handful
-- of legacy rows during upgrade is a much better failure mode than
-- corrupting a live claim.

-- The state index. IF NOT EXISTS so a fresh install that already
-- has it from 005 doesn't error.
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_state
    ON idempotency_keys(state);
