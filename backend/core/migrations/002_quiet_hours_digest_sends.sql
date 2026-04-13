-- core/migrations/002_quiet_hours_digest_sends.sql
-- Idempotency table for quiet-hours digest delivery.
-- Shipped in v1.8.5 alongside the refactor of process_quiet_hours_digest()
-- that actually delivers digests (the previous framework formatted but
-- never dispatched).
--
-- The (user_id, window_ended_at) unique constraint is the idempotency key:
--   * A second daemon poll within the same window is a no-op.
--   * Sibling workers racing to send the same digest produce exactly one
--     row; the loser's INSERT raises IntegrityError and the helper
--     rollbacks + re-reads the winner's state.

CREATE TABLE IF NOT EXISTS quiet_hours_digest_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    org_id INTEGER,
    window_ended_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, window_ended_at)
);

CREATE INDEX IF NOT EXISTS idx_qhds_window_ended_at
    ON quiet_hours_digest_sends(window_ended_at);

CREATE INDEX IF NOT EXISTS idx_qhds_org_id
    ON quiet_hours_digest_sends(org_id);
