"""
Migration: Add print_files table for .3mf uploads
"""
import sqlite3

DB_PATH = '/opt/printfarm-scheduler/backend/printfarm.db'

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS print_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            project_name TEXT NOT NULL,
            print_time_seconds INTEGER DEFAULT 0,
            total_weight_grams REAL DEFAULT 0,
            layer_count INTEGER DEFAULT 0,
            layer_height REAL DEFAULT 0.2,
            nozzle_diameter REAL DEFAULT 0.4,
            printer_model TEXT,
            supports_used BOOLEAN DEFAULT FALSE,
            bed_type TEXT,
            filaments_json TEXT,
            thumbnail_b64 TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            job_id INTEGER REFERENCES jobs(id)
        )
    ''')
    
    cur.execute('CREATE INDEX IF NOT EXISTS idx_print_files_uploaded ON print_files(uploaded_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_print_files_job ON print_files(job_id)')
    
    conn.commit()
    conn.close()
    print("Created print_files table")

if __name__ == "__main__":
    migrate()
