#!/usr/bin/env python3
"""
Migration: Add job approval workflow columns and system config.
Run on server: python3 migrate_approval.py
"""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/opt/printfarm-scheduler/backend/printfarm.db")

# Try alternate paths
if not os.path.exists(DB_PATH):
    for alt in [
        "/data/printfarm.db",
        "/opt/printfarm-scheduler/printfarm.db",
        "printfarm.db"
    ]:
        if os.path.exists(alt):
            DB_PATH = alt
            break

print(f"Using database: {DB_PATH}")

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # --- Jobs table: add approval columns ---
    existing = [row[1] for row in cursor.execute("PRAGMA table_info(jobs)").fetchall()]

    cols_to_add = {
        "submitted_by": "INTEGER REFERENCES users(id)",
        "approved_by": "INTEGER REFERENCES users(id)",
        "approved_at": "DATETIME",
        "rejected_reason": "TEXT",
    }

    for col_name, col_type in cols_to_add.items():
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
            print(f"✓ Added jobs.{col_name}")
        else:
            print(f"· jobs.{col_name} already exists")

    # --- System config: require_job_approval ---
    row = cursor.execute(
        "SELECT value FROM system_config WHERE key = 'require_job_approval'"
    ).fetchone()
    
    if row is None:
        cursor.execute(
            "INSERT INTO system_config (key, value) VALUES ('require_job_approval', 'false')"
        )
        print("✓ Added system_config: require_job_approval = false")
    else:
        print(f"· require_job_approval already exists: {row[0]}")

    conn.commit()
    conn.close()
    print("\n✅ Migration complete.")

if __name__ == "__main__":
    migrate()
