import os
"""
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
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from db_utils import get_db

log = logging.getLogger("quiet_hours")

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
        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.execute("SELECT key, value FROM system_config WHERE key LIKE 'quiet_hours_%'")
            rows = {r["key"]: r["value"] for r in cur.fetchall()}

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
        now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
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

        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.execute("""
                SELECT alert_type, severity, title, message, created_at
                FROM alerts
                WHERE created_at BETWEEN ? AND ?
                ORDER BY created_at DESC
            """, (quiet_start.isoformat(), quiet_end.isoformat()))
            alerts = [dict(r) for r in cur.fetchall()]

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
