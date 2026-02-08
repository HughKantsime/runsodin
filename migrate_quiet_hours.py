#!/usr/bin/env python3
"""Ensure quiet hours defaults exist in system_config."""
import sqlite3

DB = "/opt/printfarm-scheduler/backend/printfarm.db"
conn = sqlite3.connect(DB)

conn.execute("""
    CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")

defaults = [
    ("quiet_hours_enabled", "false"),
    ("quiet_hours_start", "22:00"),
    ("quiet_hours_end", "07:00"),
    ("quiet_hours_digest", "true"),
]

for key, val in defaults:
    conn.execute(
        "INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)",
        (key, val)
    )

conn.commit()
conn.close()
print("✅ Quiet hours defaults added to system_config")
