-- system/migrations/001_initial.sql
-- Canonical source for system domain raw-SQL tables.
-- Note: maintenance_tasks, maintenance_logs are managed by SQLAlchemy ORM.
-- See modules/system/models.py.
-- Depends on: core (users table), printers (printers table),
--             organizations (groups table).

CREATE TABLE IF NOT EXISTS printer_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER REFERENCES users(id),
    printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
    org_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT,
    slicer TEXT NOT NULL,
    category TEXT NOT NULL,
    file_format TEXT NOT NULL DEFAULT 'json',
    filament_type TEXT,
    raw_content TEXT NOT NULL,
    is_shared INTEGER DEFAULT 1,
    is_default INTEGER DEFAULT 0,
    tags TEXT,
    last_applied_at DATETIME,
    last_applied_printer_id INTEGER REFERENCES printers(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_printer_profiles_slicer ON printer_profiles(slicer);
CREATE INDEX IF NOT EXISTS idx_printer_profiles_category ON printer_profiles(category);
