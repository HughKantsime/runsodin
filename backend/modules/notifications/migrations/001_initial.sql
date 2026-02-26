-- notifications/migrations/001_initial.sql
-- Canonical source for notifications domain raw-SQL tables.
-- Note: alerts, alert_preferences, push_subscriptions are managed by SQLAlchemy ORM.
-- See modules/notifications/models.py.

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    webhook_type TEXT DEFAULT 'generic',
    alert_types TEXT,
    is_enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
