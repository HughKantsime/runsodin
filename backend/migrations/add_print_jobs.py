"""
Migration: Add print_jobs table for MQTT-based job tracking
"""
import sqlite3

DB_PATH = '/opt/printfarm-scheduler/backend/printfarm.db'

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id INTEGER NOT NULL REFERENCES printers(id),
            job_id TEXT,
            filename TEXT,
            job_name TEXT,
            started_at DATETIME NOT NULL,
            ended_at DATETIME,
            status TEXT DEFAULT 'running',
            total_layers INTEGER,
            bed_temp_target REAL,
            nozzle_temp_target REAL,
            filament_slots TEXT,
            error_code INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(printer_id, job_id, started_at)
        )
    ''')
    
    cur.execute('CREATE INDEX IF NOT EXISTS idx_print_jobs_printer ON print_jobs(printer_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_print_jobs_started ON print_jobs(started_at)')
    
    conn.commit()
    conn.close()
    print("Done - print_jobs table created")

if __name__ == '__main__':
    migrate()
