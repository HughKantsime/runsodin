-- printers/migrations/001_initial.sql
-- Canonical source for printer telemetry raw-SQL tables.
-- Note: printers, filament_slots, nozzle_lifecycle are managed by SQLAlchemy ORM
--       (Base.metadata.create_all). See modules/printers/models.py.

-- Table managed by SQLAlchemy ORM in modules/printers/models.py
-- nozzle_lifecycle

CREATE TABLE IF NOT EXISTS printer_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    bed_temp REAL,
    nozzle_temp REAL,
    bed_target REAL,
    nozzle_target REAL,
    fan_speed INTEGER,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_printer_telemetry_printer ON printer_telemetry(printer_id);
CREATE INDEX IF NOT EXISTS idx_printer_telemetry_recorded ON printer_telemetry(recorded_at);

CREATE TABLE IF NOT EXISTS hms_error_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT,
    severity TEXT DEFAULT 'warning',
    source TEXT DEFAULT 'bambu_hms',
    occurred_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hms_history_printer ON hms_error_history(printer_id);
CREATE INDEX IF NOT EXISTS idx_hms_history_occurred ON hms_error_history(occurred_at);

CREATE TABLE IF NOT EXISTS ams_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    ams_unit INTEGER NOT NULL,
    humidity REAL,
    temperature REAL,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ams_telemetry_printer ON ams_telemetry(printer_id);
CREATE INDEX IF NOT EXISTS idx_ams_telemetry_recorded ON ams_telemetry(recorded_at);
