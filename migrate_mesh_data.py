#!/usr/bin/env python3
"""Add mesh_data column to print_files table for 3D viewer."""

import sqlite3

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Check if column exists
cur.execute("PRAGMA table_info(print_files)")
columns = [col[1] for col in cur.fetchall()]

if "mesh_data" not in columns:
    cur.execute("ALTER TABLE print_files ADD COLUMN mesh_data TEXT")
    conn.commit()
    print("✅ Added mesh_data column to print_files")
else:
    print("⚠️  mesh_data column already exists")

conn.close()
