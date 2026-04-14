-- core/migrations/003_quiet_hours_org_digest_sends.sql
-- Idempotency table for the per-org webhook digest.
--
-- Codex pass 4 (2026-04-14): migration 002 covered per-user delivery
-- idempotency but the per-org webhook had no idempotency at all — every
-- 60s poll of the report_runner daemon would re-send the same digest
-- to the org's configured webhook for the duration of the next quiet
-- period. This table is the missing org-level claim record.
--
-- Schema mirrors quiet_hours_digest_sends but keys on (org_id,
-- window_ended_at) since webhook delivery is per-org, not per-user.
-- org_id may be NULL for system-level webhooks (no org context).

CREATE TABLE IF NOT EXISTS quiet_hours_org_digest_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER,
    window_ended_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, window_ended_at)
);

CREATE INDEX IF NOT EXISTS idx_qhods_window_ended_at
    ON quiet_hours_org_digest_sends(window_ended_at);
