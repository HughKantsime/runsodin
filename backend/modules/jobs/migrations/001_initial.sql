-- jobs/migrations/001_initial.sql
-- Canonical source for jobs domain raw-SQL tables.
-- Note: jobs (the scheduler queue), scheduler_runs, print_presets are managed by
--       SQLAlchemy ORM. See modules/jobs/models.py.
-- print_jobs and print_files track active/completed printer activity (telemetry log).
-- Depends on: printers (printers table must exist).

CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id),
    job_id TEXT,
    filename TEXT,
    job_name TEXT,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    status TEXT DEFAULT 'running',
    progress_percent REAL DEFAULT 0,
    remaining_minutes REAL,
    total_layers INTEGER,
    current_layer INTEGER,
    bed_temp_target REAL,
    nozzle_temp_target REAL,
    filament_slots TEXT,
    error_code TEXT,
    scheduled_job_id INTEGER,
    created_at DATETIME DEFAULT (datetime('now')),
    model_revision_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_print_jobs_printer ON print_jobs(printer_id);
CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status);
CREATE INDEX IF NOT EXISTS idx_print_jobs_started ON print_jobs(started_at);

CREATE TABLE IF NOT EXISTS print_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    original_filename TEXT,
    project_name TEXT,
    print_time_seconds REAL,
    total_weight_grams REAL,
    layer_count INTEGER,
    layer_height REAL,
    nozzle_diameter REAL,
    printer_model TEXT,
    supports_used BOOLEAN DEFAULT 0,
    bed_type TEXT,
    filaments_json TEXT,
    thumbnail_b64 TEXT,
    mesh_data TEXT,
    model_id INTEGER,
    job_id INTEGER,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    stored_path TEXT,
    filament_weight_grams REAL,
    bed_x_mm REAL,
    bed_y_mm REAL,
    compatible_api_types TEXT,
    file_hash TEXT,
    plate_count INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_print_files_model ON print_files(model_id);
CREATE INDEX IF NOT EXISTS idx_print_files_job ON print_files(job_id);
