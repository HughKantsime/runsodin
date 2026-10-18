import os
"""
Quiet Hours Module — Controls notification suppression and digest generation.

Checks system_config for quiet_hours_enabled, quiet_hours_start, quiet_hours_end.
During quiet hours, real-time notifications (email, push, webhook) are suppressed.
Alerts still get created in the database — just not dispatched externally.

At quiet_hours_end, a digest email summarizes all alerts from the quiet period.
"""

import html
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

from sqlalchemy import text

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine

log = logging.getLogger("quiet_hours")

_config_cache: Optional[Dict] = None
_config_ts: float = 0
CONFIG_TTL = 30  # seconds


from core.system_config_values import config_text  # noqa: E402 -- re-exported for callers


def _get_config() -> Dict[str, Any]:
    """Load quiet hours config from DB, cached."""
    global _config_cache, _config_ts

    now = time.time()
    if _config_cache is not None and (now - _config_ts) < CONFIG_TTL:
        return _config_cache

    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT key, value FROM system_config WHERE key LIKE 'quiet_hours_%'"))
            rows = {r._mapping["key"]: config_text(r._mapping["value"]) for r in result.fetchall()}

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


def _window_minutes(config):
    start_h,start_m=map(int,config.get("start","22:00").split(":"))
    end_h,end_m=map(int,config.get("end","07:00").split(":"))
    if not (0<=start_h<24 and 0<=end_h<24 and 0<=start_m<60 and 0<=end_m<60):
        raise ValueError("Invalid quiet window")
    start,end=start_h*60+start_m,end_h*60+end_m
    if start==end:
        raise ValueError("Empty quiet window")
    return start,end


def is_quiet_time() -> bool:
    return _is_quiet_time_for_config(_get_config())


def _is_quiet_time_for_config(config: Dict, now: datetime = None) -> bool:
    if not config.get("enabled",False):
        return False
    try:
        start,end=_window_minutes(config)
        now=now or datetime.now(timezone.utc)
        now=now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        minute=now.hour*60+now.minute
        return start<=minute<end if start<end else minute>=start or minute<end
    except (ValueError,TypeError,AttributeError):
        return False


def _org_quiet_hours_enabled(conn, org_id) -> bool:
    row = conn.execute(text("SELECT settings_json FROM groups WHERE id = :oid"), {"oid": org_id}).mappings().fetchone()
    if not row or not row["settings_json"]:
        return False
    raw = row["settings_json"]
    try:
        settings = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return False
    return bool(isinstance(settings, dict) and settings.get("quiet_hours_enabled"))


def governing_scope(org_id=None, conn=None) -> str:
    """Digest scope whose quiet hours govern a resource owned by ``org_id``:
    'org:<id>' when that org has its own quiet hours enabled, else 'system'
    (the same precedence should_suppress_notification applies)."""
    if not org_id:
        return "system"
    try:
        if conn is not None:
            return f"org:{int(org_id)}" if _org_quiet_hours_enabled(conn, org_id) else "system"
        with engine.connect() as own:
            return f"org:{int(org_id)}" if _org_quiet_hours_enabled(own, org_id) else "system"
    except Exception as e:
        log.debug(f"Error resolving quiet-hours scope: {e}")
        return "system"


def governed_by(owner, digest_org_id, conn=None) -> bool:
    """Whether an alert owned by ``owner`` belongs to the digest of ``digest_org_id``
    (None = system digest). Org digests exist only for orgs with their own quiet
    hours, so they take their own alerts; the system digest takes unscoped alerts
    and alerts of orgs without their own quiet hours."""
    if digest_org_id is not None:
        return owner == digest_org_id
    return owner is None or governing_scope(owner, conn) == "system"


def should_suppress_notification(org_id: int = None, db=None) -> bool:
    """Returns True if real-time notifications should be suppressed.
    Checks org-level quiet hours first (if org_id given), then system-level.
    ``db``: the caller's session or connection, reused for the org read (no new connection)."""
    if org_id:
        try:
            from contextlib import nullcontext
            with (nullcontext(db) if db is not None else engine.connect()) as conn:
                row = conn.execute(text("SELECT settings_json FROM groups WHERE id = :oid"), {"oid": org_id}).mappings().fetchone()
                if row and row["settings_json"]:
                    settings = json.loads(row["settings_json"])
                    org_qh = {
                        "enabled": settings.get("quiet_hours_enabled", False),
                        "start": settings.get("quiet_hours_start", "22:00"),
                        "end": settings.get("quiet_hours_end", "07:00"),
                    }
                    if org_qh["enabled"]:
                        return _is_quiet_time_for_config(org_qh)
        except Exception as e:
            log.debug(f"Error checking org quiet hours: {e}")
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

        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT alert_type, severity, title, message, created_at
                FROM alerts
                WHERE created_at BETWEEN :start AND :end
                ORDER BY created_at DESC
            """), {"start": quiet_start.isoformat(), "end": quiet_end.isoformat()})
            alerts = [dict(r._mapping) for r in result.fetchall()]

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
            <td style="padding: 8px; font-size: 14px;">{emoji} {html.escape(str(a.get('severity', 'info')).upper())}</td>
            <td style="padding: 8px; font-size: 14px;">{html.escape(str(a.get('title', '')))}</td>
            <td style="padding: 8px; font-size: 14px; color: #888;">{html.escape(str(a.get('created_at', ''))[:16])}</td>
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

    rendered = f"""
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
    return rendered


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


# ─────────────────────────────────────────────────────────────────────────
# v1.8.5 digest delivery helpers
#
# The original digest framework formatted alerts but never actually
# dispatched them per-user or per-org. These helpers let the refactored
# report_runner driver scope alerts to a specific window (given an org's
# own quiet-hours config) and group them by user or by org for per-channel
# fan-out.
#
# Kept deliberately thin — they do DB reads and pure Python. The delivery
# side-effects live in report_runner.py so failure modes have one owner.
# ─────────────────────────────────────────────────────────────────────────


def compute_last_window_end(config: Dict[str, Any], now: datetime = None) -> Optional[datetime]:
    """Return the datetime of the most recently ENDED quiet-hours window.

    Returns None if quiet hours are disabled, or if we're currently inside
    the window (no ended window to digest yet).

    Takes an explicit `config` dict so callers can pass an org-level config
    instead of the system-level one. `now` defaults to UTC wall-clock and
    is injectable for tests.
    """
    if not config.get("enabled", False):
        return None
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        _window_minutes(config)
    except (ValueError,TypeError,AttributeError):
        return None

    # If we're currently inside the window, it hasn't ended yet.
    if _is_quiet_time_for_config(config, now=now):
        return None

    try:
        end_h, end_m = map(int, config.get("end", "07:00").split(":"))
    except Exception:
        return None

    # Most recent end: today's end-of-window if it's already passed, else
    # yesterday's.
    quiet_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if quiet_end > now:
        quiet_end -= timedelta(days=1)
    return quiet_end


def compute_window_bounds(config: Dict[str, Any], window_end: datetime) -> Tuple[datetime, datetime]:
    """Given a window-end datetime, compute the matching window-start."""
    start_h, start_m = map(int, config.get("start", "22:00").split(":"))
    start = window_end.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if start >= window_end:
        start -= timedelta(days=1)
    return start, window_end


def get_suppressed_alerts_for_window(
    window_start: datetime,
    window_end: datetime,
    org_id: Optional[int] = None,
) -> list:
    """Fetch alerts saved during a specific window.

    When org_id is provided, only alerts whose printer belongs to that
    org are returned. Without org_id, system-wide alerts are returned
    (used for the system-level quiet-hours digest).

    Returns rows with: id, user_id, alert_type, severity, title, message,
    printer_id, created_at.
    """
    from modules.notifications.recipient_policy import resolve_alert_scope
    try:
        with engine.connect() as conn:
            # SQLite contains both ORM space-separated UTC values and legacy
            # ISO values. Parse time instead of comparing incompatible strings.
            predicate = (
                "datetime(created_at)>=datetime(:start) AND datetime(created_at)<datetime(:end)"
                if conn.dialect.name == "sqlite" else "created_at>=:start AND created_at<:end"
            )
            query = "SELECT id,user_id,alert_type,severity,title,message,printer_id,job_id,spool_id,created_at FROM alerts WHERE " + predicate + " ORDER BY created_at DESC"
            rows = conn.execute(text(query), {"start": window_start, "end": window_end}).mappings().all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: predicate selected from a closed pair of literal dialect comparisons; values are bound
            result = []
            for row in rows:
                try:
                    eligible, owner = resolve_alert_scope(conn, printer_id=row['printer_id'],
                        job_id=row['job_id'], spool_id=row['spool_id'])
                except ValueError:
                    continue
                # The alert belongs to this digest when this scope's quiet hours
                # governed it: its own org's, or the system's for unscoped alerts
                # and for orgs without their own quiet hours.
                if row['user_id'] in eligible and governed_by(owner, org_id, conn):
                    result.append(dict(row))
            return result
    except Exception:
        log.error("Digest alert scope resolution failed")
        return []


def group_suppressed_by_user(alerts: list) -> Dict[int, list]:
    """Map user_id → list[alerts]. Drops rows with NULL user_id."""
    out: Dict[int, list] = {}
    for a in alerts:
        uid = a.get("user_id")
        if uid is None:
            continue
        out.setdefault(int(uid), []).append(a)
    return out


def _decrypt_org_webhook_url(value):
    """Decrypt a Fernet-encrypted org webhook URL; plaintext (legacy) passes through."""
    if not value:
        return value
    try:
        import core.crypto as crypto
        return crypto.decrypt(value)
    except Exception:
        return value


def iter_orgs_with_digest_enabled() -> list:
    """Yield orgs whose settings_json has quiet_hours_enabled=True AND
    quiet_hours_digest_enabled != False.

    Returns list of dicts: {id, name, settings_json (parsed), config (config dict)}.
    Also appends a virtual 'system' entry with id=None, config derived
    from the system-level _get_config(), so the digest driver iterates
    uniformly over orgs + system-level.
    """
    out: list = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, name, settings_json FROM groups "
                "WHERE settings_json IS NOT NULL"
            )).mappings().fetchall()
        for r in rows:
            raw = r.get("settings_json")
            if not raw:
                continue
            try:
                s = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not s.get("quiet_hours_enabled"):
                continue
            if s.get("quiet_hours_digest_enabled") is False:
                continue  # explicit opt-out
            config = {
                "enabled": True,
                "start": s.get("quiet_hours_start", "22:00"),
                "end": s.get("quiet_hours_end", "07:00"),
                "digest_enabled": s.get("quiet_hours_digest_enabled", True),
                # PUT /orgs/{id}/settings stores the URL Fernet-encrypted;
                # legacy rows may hold plaintext.
                "webhook_url": _decrypt_org_webhook_url(s.get("webhook_url")),
                "webhook_type": s.get("webhook_type", "generic"),
            }
            out.append({
                "id": r["id"],
                "name": r["name"],
                "settings_json": s,
                "config": config,
            })
    except Exception as e:
        log.error(f"iter_orgs_with_digest_enabled failed: {e}")

    # Add system-level row (org_id=None) if the top-level config says so.
    sys_config = _get_config()
    if sys_config.get("enabled") and sys_config.get("digest_enabled"):
        out.append({
            "id": None,
            "name": "system",
            "settings_json": {},
            "config": dict(sys_config),
        })

    return out
