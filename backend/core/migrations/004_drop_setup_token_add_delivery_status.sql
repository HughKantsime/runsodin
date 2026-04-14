-- core/migrations/004_drop_setup_token_add_delivery_status.sql
-- v1.8.8 — two unrelated schema changes in one file:
--
--   1. Delete the now-unused `setup_token` row from `system_config`.
--      The v1.8.6 setup-token gate was retired in v1.8.8 in favor of
--      the WordPress-style first-user-wins invariant. Leaving the row
--      behind would be dead state; one DELETE is the correct cleanup.
--
--   2. Add `delivery_status` column to the two quiet-hours digest-sends
--      tables so the Alerts page can surface "delivered / failed"
--      visibility. Default 'pending'; the runner writes 'sent' or
--      'failed:<reason>' after it ships the digest.
--
-- The loader handles "column already exists" / "duplicate column name"
-- on both SQLite and Postgres, so this migration is idempotent and
-- safe to re-run.

-- 1. Retire the setup-token row.
DELETE FROM system_config WHERE key = 'setup_token';

-- 2. Surface digest delivery outcome in the two sends tables.
ALTER TABLE quiet_hours_digest_sends
    ADD COLUMN delivery_status TEXT DEFAULT 'pending';

ALTER TABLE quiet_hours_org_digest_sends
    ADD COLUMN delivery_status TEXT DEFAULT 'pending';
