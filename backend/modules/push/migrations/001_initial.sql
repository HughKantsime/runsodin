-- push/migrations/001_initial.sql
-- Tables for native push notification devices and biometric refresh tokens.
-- push_devices and biometric_tokens are managed by SQLAlchemy ORM (modules/push/models.py).

CREATE TABLE IF NOT EXISTS push_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id VARCHAR(36) NOT NULL,
    platform VARCHAR(20) NOT NULL DEFAULT 'apns',
    token TEXT NOT NULL,
    live_activity_token TEXT,
    preferences_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_push_devices_user_id ON push_devices(user_id);
CREATE INDEX IF NOT EXISTS idx_push_devices_device_id ON push_devices(device_id);

CREATE TABLE IF NOT EXISTS biometric_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id VARCHAR(36) NOT NULL,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    is_revoked BOOLEAN NOT NULL DEFAULT 0,
    expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME,
    UNIQUE(user_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_biometric_tokens_user_id ON biometric_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_biometric_tokens_token_hash ON biometric_tokens(token_hash);
