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

-- Backfill updated_at for rows that existed before this column did.
-- Any pre-existing row is stale by definition (the middleware's old
-- shape didn't track state transitions) and the 24h TTL will prune
-- it shortly. Populate with created_at so ordering/pruning stays
-- consistent until the cleanup runs.
UPDATE idempotency_keys
   SET updated_at = COALESCE(updated_at, '') || ''
 WHERE updated_at = '';
UPDATE idempotency_keys
   SET updated_at = created_at
 WHERE updated_at = '';

-- Legacy rows from the narrow schema were implicitly completed
-- (they were never in a 'pending' state since that column didn't
-- exist — they held a full response body). Mark them complete so
-- the new lookup path classifies them as HIT or EXPIRED, not
-- PENDING (which would block the key for 15 minutes).
UPDATE idempotency_keys SET state = 'complete' WHERE state = 'pending';

-- The pass-7 state index. IF NOT EXISTS so a fresh install that
-- already has it from 005 doesn't error.
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_state
    ON idempotency_keys(state);
