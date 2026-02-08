#!/usr/bin/env python3
"""Ensure system_config table and MQTT republish defaults exist."""
import sqlite3

DB = "/opt/printfarm-scheduler/backend/printfarm.db"
conn = sqlite3.connect(DB)

# Create system_config if not exists
conn.execute("""
    CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")

# Insert defaults (ignore if exist)
defaults = [
    ("mqtt_republish_enabled", "false"),
    ("mqtt_republish_host", ""),
    ("mqtt_republish_port", "1883"),
    ("mqtt_republish_username", ""),
    ("mqtt_republish_password", ""),
    ("mqtt_republish_topic_prefix", "odin"),
    ("mqtt_republish_use_tls", "false"),
]

for key, val in defaults:
    conn.execute(
        "INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)",
        (key, val)
    )

conn.commit()
conn.close()
print("✅ system_config table ready with MQTT republish defaults")
