-- reporting/migrations/001_initial.sql
-- Canonical source for reporting domain raw-SQL tables.
-- Depends on: core (users table).

CREATE TABLE IF NOT EXISTS report_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    report_type VARCHAR(50) NOT NULL,
    frequency VARCHAR(20) NOT NULL DEFAULT 'weekly',
    recipients TEXT NOT NULL,
    filters TEXT DEFAULT '{}',
    is_active BOOLEAN DEFAULT 1,
    next_run_at DATETIME,
    last_run_at DATETIME,
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
