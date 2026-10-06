-- Exact table/index schema from v1.9.12; synthetic test data only.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    auth_fingerprint TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    response_status INTEGER NOT NULL DEFAULT 0,
    response_body TEXT NOT NULL DEFAULT '',
    response_media_type TEXT NOT NULL DEFAULT 'application/json',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (key, user_id)
);

CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created_at
    ON idempotency_keys(created_at);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_state
    ON idempotency_keys(state);
