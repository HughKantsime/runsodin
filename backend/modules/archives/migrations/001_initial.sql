-- archives/migrations/001_initial.sql
-- Canonical source for archives domain raw-SQL tables.
-- timelapses is dual-defined: SQLAlchemy ORM is the canonical source.
-- print_archives and projects are raw-SQL only.
-- Depends on: printers (printers table), core (users table).

CREATE TABLE IF NOT EXISTS print_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    print_job_id INTEGER,
    printer_id INTEGER,
    user_id INTEGER,
    print_name TEXT,
    status TEXT,
    started_at DATETIME,
    completed_at DATETIME,
    actual_duration_seconds INTEGER,
    filament_used_grams REAL,
    cost_estimate REAL,
    thumbnail_b64 TEXT,
    file_path TEXT,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    tags TEXT DEFAULT '',
    plate_count INTEGER DEFAULT 1,
    plate_thumbnails TEXT,
    print_file_id INTEGER,
    project_id INTEGER,
    energy_kwh REAL,
    energy_cost REAL,
    consumption_json TEXT,
    file_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_print_archives_printer ON print_archives(printer_id);
CREATE INDEX IF NOT EXISTS idx_print_archives_status ON print_archives(status);
CREATE INDEX IF NOT EXISTS idx_print_archives_created ON print_archives(created_at);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER,
    org_id INTEGER,
    name TEXT NOT NULL,
    description TEXT,
    color TEXT DEFAULT '#6366f1',
    status TEXT DEFAULT 'active',
    expected_parts INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Table managed by SQLAlchemy ORM in modules/archives/models.py
-- timelapses
