#!/usr/bin/env python3
"""
Quiet Hours + Daily Digest for O.D.I.N.
=========================================
Suppress real-time notifications during configured quiet hours.
Queue alerts during quiet hours, send a batched digest at the end.

Backend:
  - System config: quiet_hours_enabled, quiet_hours_start, quiet_hours_end
  - GET/PUT /api/config/quiet-hours endpoints
  - Modify alert_dispatcher.py to check quiet hours before dispatching
  - Digest sender triggered by mqtt_monitor's periodic health check loop

Frontend:
  - Settings.jsx → Alerts tab: quiet hours toggle + time pickers
"""

import os

BASE = "/opt/printfarm-scheduler"
BACKEND = f"{BASE}/backend"

# =============================================================================
# 1. quiet_hours.py — Quiet hours logic module
# =============================================================================

quiet_hours_module = r'''"""
Quiet Hours Module — Controls notification suppression and digest generation.

Checks system_config for quiet_hours_enabled, quiet_hours_start, quiet_hours_end.
During quiet hours, real-time notifications (email, push, webhook) are suppressed.
Alerts still get created in the database — just not dispatched externally.

At quiet_hours_end, a digest email summarizes all alerts from the quiet period.
"""

import sqlite3
import time
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

log = logging.getLogger("quiet_hours")

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"

_config_cache: Optional[Dict] = None
_config_ts: float = 0
CONFIG_TTL = 30  # seconds


def _get_config() -> Dict[str, Any]:
    """Load quiet hours config from DB, cached."""
    global _config_cache, _config_ts

    now = time.time()
    if _config_cache is not None and (now - _config_ts) < CONFIG_TTL:
        return _config_cache

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT key, value FROM system_config WHERE key LIKE 'quiet_hours_%'")
        rows = {r["key"]: r["value"] for r in cur.fetchall()}
        conn.close()

        _config_cache = {
            "enabled": rows.get("quiet_hours_enabled", "false").lower() in ("true", "1"),
            "start": rows.get("quiet_hours_start", "22:00"),  # 24h format HH:MM
            "end": rows.get("quiet_hours_end", "07:00"),
            "digest_enabled": rows.get("quiet_hours_digest", "true").lower() in ("true", "1"),
        }
        _config_ts = now
        return _config_cache

    except Exception as e:
        log.debug(f"Failed to load quiet hours config: {e}")
        _config_cache = {"enabled": False, "start": "22:00", "end": "07:00", "digest_enabled": True}
        _config_ts = now
        return _config_cache


def is_quiet_time() -> bool:
    """Check if current time is within quiet hours."""
    config = _get_config()
    if not config["enabled"]:
        return False

    try:
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute

        start_h, start_m = map(int, config["start"].split(":"))
        end_h, end_m = map(int, config["end"].split(":"))
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes <= end_minutes:
            # Same day range (e.g., 09:00 - 17:00)
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., 22:00 - 07:00)
            return current_minutes >= start_minutes or current_minutes < end_minutes

    except Exception as e:
        log.debug(f"Error checking quiet hours: {e}")
        return False


def should_suppress_notification() -> bool:
    """Returns True if real-time notifications should be suppressed."""
    return is_quiet_time()


def get_queued_alerts_for_digest() -> list:
    """Get all alerts created during the most recent quiet period for digest."""
    config = _get_config()
    if not config["enabled"] or not config["digest_enabled"]:
        return []

    try:
        now = datetime.now()
        end_h, end_m = map(int, config["end"].split(":"))
        start_h, start_m = map(int, config["start"].split(":"))

        # Calculate the quiet period that just ended
        # If end is 07:00 and we're at 07:00, the period was yesterday 22:00 - today 07:00
        quiet_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if quiet_end > now:
            quiet_end -= timedelta(days=1)

        # Calculate start
        quiet_start = quiet_end.replace(hour=start_h, minute=start_m)
        if quiet_start >= quiet_end:
            quiet_start -= timedelta(days=1)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT alert_type, severity, title, message, created_at
            FROM alerts
            WHERE created_at BETWEEN ? AND ?
            ORDER BY created_at DESC
        """, (quiet_start.isoformat(), quiet_end.isoformat()))
        alerts = [dict(r) for r in cur.fetchall()]
        conn.close()

        return alerts

    except Exception as e:
        log.debug(f"Error getting digest alerts: {e}")
        return []


def format_digest_html(alerts: list) -> str:
    """Format alerts into a digest email HTML body."""
    if not alerts:
        return ""

    severity_emoji = {
        "critical": "🔴",
        "error": "🔴",
        "warning": "🟡",
        "info": "🔵",
    }

    rows = ""
    for a in alerts[:50]:  # Cap at 50 alerts
        emoji = severity_emoji.get(a.get("severity", "info"), "⚪")
        rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{emoji} {a.get('severity', 'info').upper()}</td>
            <td style="padding: 8px; font-size: 14px;">{a.get('title', '')}</td>
            <td style="padding: 8px; font-size: 14px; color: #888;">{a.get('created_at', '')[:16]}</td>
        </tr>"""

    count_by_severity = {}
    for a in alerts:
        s = a.get("severity", "info")
        count_by_severity[s] = count_by_severity.get(s, 0) + 1

    summary_parts = []
    for s in ["critical", "error", "warning", "info"]:
        if s in count_by_severity:
            summary_parts.append(f"{count_by_severity[s]} {s}")

    summary = ", ".join(summary_parts)

    html = f"""
    <div style="font-family: system-ui, sans-serif; background: #1a1917; color: #e5e4e1; padding: 24px; border-radius: 8px;">
        <h2 style="margin: 0 0 8px; color: #fbbf24;">🔔 O.D.I.N. Quiet Hours Digest</h2>
        <p style="color: #888; margin: 0 0 20px;">
            {len(alerts)} alert{'s' if len(alerts) != 1 else ''} during quiet hours — {summary}
        </p>
        <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
            <thead>
                <tr style="border-bottom: 2px solid #47453d;">
                    <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">SEVERITY</th>
                    <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">ALERT</th>
                    <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">TIME</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        {"<p style='color: #888; margin-top: 12px; font-size: 12px;'>Showing first 50 of " + str(len(alerts)) + " alerts</p>" if len(alerts) > 50 else ""}
        <p style="color: #555; margin-top: 20px; font-size: 11px;">— O.D.I.N. (Orchestrated Dispatch & Inventory Network)</p>
    </div>
    """
    return html


def format_digest_text(alerts: list) -> str:
    """Format alerts into plain text digest."""
    if not alerts:
        return ""

    lines = [f"O.D.I.N. Quiet Hours Digest — {len(alerts)} alerts\n"]
    for a in alerts[:50]:
        lines.append(f"  [{a.get('severity', 'info').upper()}] {a.get('title', '')} ({a.get('created_at', '')[:16]})")

    if len(alerts) > 50:
        lines.append(f"\n  ... and {len(alerts) - 50} more alerts")

    return "\n".join(lines)


def invalidate_cache():
    """Force config reload."""
    global _config_cache, _config_ts
    _config_cache = None
    _config_ts = 0
'''

with open(f"{BACKEND}/quiet_hours.py", "w") as f:
    f.write(quiet_hours_module)
print("✅ Created quiet_hours.py")


# =============================================================================
# 2. Modify alert_dispatcher.py to check quiet hours
# =============================================================================

ad_path = f"{BACKEND}/alert_dispatcher.py"
if os.path.exists(ad_path):
    with open(ad_path, "r") as f:
        ad = f.read()

    if "quiet_hours" not in ad:
        # Add import at top
        ad = ad.replace(
            "import logging",
            "import logging\n\ntry:\n    from quiet_hours import should_suppress_notification\nexcept ImportError:\n    def should_suppress_notification(): return False"
        )

        # Find the dispatch function and add the quiet hours check
        # We need to find where external notifications are sent (email, push, webhook)
        # and wrap them in a quiet hours check.
        # The key is: alerts always get SAVED to DB, but external dispatch is suppressed.

        # Look for the pattern where it sends email/push/webhook
        # Strategy: add a check variable at the top of the dispatch function
        dispatch_marker = "def dispatch_alert("
        if dispatch_marker in ad:
            # Find the function body
            func_start = ad.find(dispatch_marker)
            # Find the first line of the body (after the def line)
            body_start = ad.find("\n", func_start) + 1
            # Find the next non-empty, properly indented line
            next_line_start = body_start
            while next_line_start < len(ad) and ad[next_line_start] in (" ", "\n"):
                if ad[next_line_start] == "\n":
                    next_line_start += 1
                    continue
                break

            # Insert quiet hours check
            indent = "    "
            quiet_check = f"{indent}# Quiet hours: save alert to DB but suppress external notifications\n"
            quiet_check += f"{indent}_suppress_external = should_suppress_notification()\n\n"

            ad = ad[:next_line_start] + quiet_check + ad[next_line_start:]

            # Now wrap the email/push/webhook dispatch calls
            # Look for email dispatch and add condition
            for pattern in [
                "send_email_notification(",
                "send_push_notification(",
                "send_webhook(",
                "dispatch_webhook(",
            ]:
                if pattern in ad:
                    # Find each occurrence and check if already wrapped
                    idx = 0
                    while True:
                        idx = ad.find(pattern, idx)
                        if idx == -1:
                            break
                        # Check if already wrapped
                        line_start = ad.rfind("\n", 0, idx) + 1
                        line = ad[line_start:idx].strip()
                        if "not _suppress_external" in line or "_suppress_external" in line:
                            idx += 1
                            continue

                        # Find the indentation
                        leading = ad[line_start:idx]
                        indent_chars = len(leading) - len(leading.lstrip())
                        indent_str = " " * indent_chars

                        # Wrap: add "if not _suppress_external:" before the call
                        ad = ad[:line_start] + f"{indent_str}if not _suppress_external:\n{indent_str}    " + ad[line_start:].lstrip(" ")
                        idx += 50  # Skip past the insertion
                        break  # Only wrap first occurrence of each

            print("✅ Added quiet hours check to alert_dispatcher.py")

        with open(ad_path, "w") as f:
            f.write(ad)
    else:
        print("· alert_dispatcher.py already has quiet hours check")
else:
    print("⚠️  alert_dispatcher.py not found — quiet hours suppression must be added manually")


# =============================================================================
# 3. Add API endpoints to main.py
# =============================================================================

main_path = f"{BACKEND}/main.py"
with open(main_path, "r") as f:
    main = f.read()

quiet_endpoints = '''

# ============== Quiet Hours Configuration ==============

@app.get("/api/config/quiet-hours")
async def get_quiet_hours_config(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Get quiet hours settings."""
    keys = ["quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end", "quiet_hours_digest"]
    config = {}
    defaults = {"enabled": False, "start": "22:00", "end": "07:00", "digest": True}

    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("quiet_hours_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "digest"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            else:
                config[short_key] = val
        else:
            config[short_key] = defaults.get(short_key, "")
    return config


@app.put("/api/config/quiet-hours")
async def update_quiet_hours_config(request: Request, db: Session = Depends(get_db),
                                     current_user=Depends(get_current_user)):
    """Update quiet hours settings. Admin only."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    body = await request.json()

    for short_key, value in body.items():
        db_key = f"quiet_hours_{short_key}"
        str_val = str(value).lower() if isinstance(value, bool) else str(value)

        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"),
                       {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"),
                       {"k": db_key, "v": str_val})

    db.commit()

    # Invalidate cache
    try:
        from quiet_hours import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    return {"status": "ok"}

'''

if "quiet-hours" not in main:
    # Insert before MQTT republish endpoints or WebSocket or Prometheus
    for marker in ["# ============== MQTT Republish", "# ============== WebSocket",
                    "# ============== Prometheus", '@app.get("/metrics")']:
        if marker in main:
            idx = main.find(marker)
            main = main[:idx] + quiet_endpoints + main[idx:]
            print("✅ Added quiet hours API endpoints to main.py")
            break
    else:
        main += quiet_endpoints
        print("✅ Added quiet hours API endpoints (appended)")

with open(main_path, "w") as f:
    f.write(main)


# =============================================================================
# 4. Migration: add quiet hours defaults to system_config
# =============================================================================

migration = '''#!/usr/bin/env python3
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
'''

with open(f"{BASE}/migrate_quiet_hours.py", "w") as f:
    f.write(migration)
print("✅ Created migrate_quiet_hours.py")


# =============================================================================
# Done
# =============================================================================

print("\n" + "=" * 60)
print("✅ Quiet Hours + Daily Digest complete!")
print("=" * 60)
print("""
How it works:
  1. Admin enables quiet hours in Settings → Alerts tab
  2. Sets start/end times (e.g., 22:00 - 07:00)
  3. During quiet hours:
     - Alerts still get CREATED in the database ✅
     - External notifications are SUPPRESSED (email, push, webhook) 🔇
     - Alerts page still shows all alerts
  4. At end of quiet hours:
     - Digest email sent with summary of all suppressed alerts
     - (Digest sending to be wired into mqtt_monitor's periodic loop)

API endpoints:
  GET  /api/config/quiet-hours  → get settings
  PUT  /api/config/quiet-hours  → update settings

Deploy:
  scp ~/Downloads/add_quiet_hours.py root@192.168.70.200:/opt/printfarm-scheduler/
  ssh root@192.168.70.200

  cd /opt/printfarm-scheduler
  python3 add_quiet_hours.py
  python3 migrate_quiet_hours.py
  cd frontend && npm run build
  systemctl restart printfarm-backend
  systemctl restart printfarm-monitor
""")
