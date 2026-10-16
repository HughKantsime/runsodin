"""
Report Runner Daemon

Polls report_schedules for due reports and generates + emails them.
Also sends quiet hours digest when the quiet period ends.
"""

import os
import time
import json
from html import escape
from modules.reporting.recipient_policy import normalize_recipients
from modules.reporting.chargeback_sql import ARCHIVE_GRAMS_BY_JOB_SQL
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from core.database_config import create_database_engine
from core.db_compat import sql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [report_runner] %(levelname)s %(message)s",
)
log = logging.getLogger("odin.report_runner")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
POLL_INTERVAL = 60  # seconds

engine = create_database_engine(
    DATABASE_URL,
    role=os.getenv("ODIN_DB_ROLE", "reports"),
    password_file=os.getenv("DATABASE_PASSWORD_FILE"),
)
SessionLocal = sessionmaker(bind=engine)


# =============================================================================
# SMTP helpers (same pattern as alert_dispatcher)
# =============================================================================

def get_smtp_config(session):
    """Read SMTP config from system_config."""
    row = session.execute(text(
        "SELECT value FROM system_config WHERE key = 'smtp_config'"
    )).fetchone()
    if not row:
        return None
    try:
        config = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (json.JSONDecodeError, TypeError):
        return None
    if not config.get("enabled") or not config.get("host"):
        return None
    # Decrypt password — migration-safe: crypto.decrypt() falls back to raw on failure
    if config.get("password"):
        try:
            import core.crypto as crypto
            config = dict(config)
            config["password"] = crypto.decrypt(config["password"])
        except Exception as e:
            log.debug(f"Failed to decrypt SMTP password (using raw): {e}")
    return config


def send_report_email(smtp_config, recipient, subject, html_body):
    """Send an HTML email to a single recipient."""
    try:
        recipient = normalize_recipients([recipient])[0]
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_config.get("from_address", "odin@localhost")
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        # v1.8.9 (codex pass 7): runtime ITAR guard.
        from core.itar import enforce_host_destination
        enforce_host_destination(smtp_config["host"], scheme="smtp")

        with smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 587 if smtp_config.get("use_tls", True) else 25)) as server:
            if smtp_config.get("use_tls", True):
                server.starttls()
            if smtp_config.get("username") and smtp_config.get("password"):
                server.login(smtp_config["username"], smtp_config["password"])
            refused = server.send_message(msg, to_addrs=[recipient])
            if refused:
                return False
        return True
    except Exception as e:
        log.error("Report email delivery failed (%s)", type(e).__name__)
        return False


# =============================================================================
# Report generation functions
# =============================================================================

def _wrap_report_html(title, content):
    """Wrap report content in branded email template."""
    return f"""
    <div style="font-family: system-ui, sans-serif; background: #1a1917; color: #e5e4e1; padding: 24px; border-radius: 8px;">
        <h2 style="margin: 0 0 16px; color: #fbbf24;">📊 {title}</h2>
        {content}
        <p style="color: #555; margin-top: 20px; font-size: 11px;">— O.D.I.N. (Orchestrated Dispatch &amp; Inventory Network)</p>
    </div>
    """


def generate_fleet_utilization(session, filters):
    """Printer count, utilization %, hours."""
    total = session.execute(text("SELECT COUNT(*) FROM printers")).scalar() or 0
    active = session.execute(text("SELECT COUNT(*) FROM printers WHERE is_active IS TRUE")).scalar() or 0
    printing = session.execute(text("SELECT COUNT(*) FROM printers WHERE gcode_state = 'RUNNING'")).scalar() or 0
    total_hours = session.execute(text(
        "SELECT COALESCE(SUM(total_print_hours), 0) FROM printers"
    )).scalar() or 0

    utilization = round((printing / active * 100) if active > 0 else 0, 1)

    content = f"""
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">TOTAL PRINTERS</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{total}</td>
        </tr>
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">ACTIVE</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{active}</td>
        </tr>
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">CURRENTLY PRINTING</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{printing}</td>
        </tr>
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">UTILIZATION</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{utilization}%</td>
        </tr>
        <tr>
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">TOTAL PRINT HOURS</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{round(total_hours, 1)}</td>
        </tr>
    </table>
    """
    return _wrap_report_html("Fleet Utilization Report", content)


def generate_job_summary(session, filters):
    """Jobs by status in period."""
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT status, COUNT(*) as cnt
        FROM jobs
        WHERE created_at >= {sql.now_offset('-7 days')}
        GROUP BY status ORDER BY cnt DESC
    """)).fetchall()

    table_rows = ""
    total = 0
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
        </tr>"""
        total += r[1]

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 7 days — {total} total jobs</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">STATUS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COUNT</th>
        </tr></thead>
        <tbody>{table_rows}</tbody>
    </table>
    """
    return _wrap_report_html("Job Summary Report", content)


def generate_filament_consumption(session, filters):
    """Usage by material type."""
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT fl.material, COALESCE(SUM(su.weight_used_g), 0) as total_g
        FROM spool_usage su
        JOIN spools s ON su.spool_id = s.id
        JOIN filament_library fl ON s.filament_id = fl.id
        WHERE su.used_at >= {sql.now_offset('-30 days')}
        GROUP BY fl.material ORDER BY total_g DESC
    """)).fetchall()

    table_rows = ""
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0] or 'Unknown'))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[1], 1)}g</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days filament consumption</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">MATERIAL</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">USED</th>
        </tr></thead>
        <tbody>{table_rows if table_rows else '<tr><td style="padding: 8px; color: #666;" colspan="2">No usage recorded</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Filament Consumption Report", content)


def generate_failure_analysis(session, filters):
    """Detection counts, failure reasons."""
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT detection_type, COUNT(*) as cnt, AVG(confidence) as avg_conf
        FROM vision_detections
        WHERE created_at >= {sql.now_offset('-30 days')} AND (status IS NULL OR status != 'training')
        GROUP BY detection_type ORDER BY cnt DESC
    """)).fetchall()

    # Also get job failure reasons
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    fail_rows = session.execute(text(f"""
        SELECT fail_reason, COUNT(*) as cnt
        FROM jobs
        WHERE status = 'failed' AND fail_reason IS NOT NULL
          AND created_at >= {sql.now_offset('-30 days')}
        GROUP BY fail_reason ORDER BY cnt DESC
    """)).fetchall()

    detect_rows_html = ""
    for r in rows:
        detect_rows_html += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[2] * 100, 1)}%</td>
        </tr>"""

    fail_rows_html = ""
    for r in fail_rows:
        fail_rows_html += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days</p>
    <h3 style="color: #fbbf24; font-size: 14px; margin: 16px 0 8px;">AI Detections</h3>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">TYPE</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COUNT</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">AVG CONF</th>
        </tr></thead>
        <tbody>{detect_rows_html if detect_rows_html else '<tr><td style="padding: 8px; color: #666;" colspan="3">No detections</td></tr>'}</tbody>
    </table>
    <h3 style="color: #fbbf24; font-size: 14px; margin: 16px 0 8px;">Job Failure Reasons</h3>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">REASON</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COUNT</th>
        </tr></thead>
        <tbody>{fail_rows_html if fail_rows_html else '<tr><td style="padding: 8px; color: #666;" colspan="2">No failures</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Failure Analysis Report", content)


def generate_chargeback_summary(session, filters):
    """Reuse query pattern from /api/reports/chargebacks.

    Rows are per user and charged organization so each cost carries that
    organization's configured finance currency (no currency is presumed and
    none is converted). Jobs without an estimated cost are counted and labeled
    as unpriced; they are never valued as 0.
    """
    from core.finance_currency import read_currency
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT u.username, COUNT(j.id) as job_count,
               SUM(j.estimated_cost) as total_cost,
               COALESCE(SUM(j.duration_hours), 0) as total_hours,
               COALESCE(SUM(pa.grams), 0) as total_grams,
               SUM(CASE WHEN j.estimated_cost IS NULL THEN 1 ELSE 0 END) as unpriced_jobs,
               j.charged_to_org_id as org_id
        FROM jobs j
        LEFT JOIN users u ON j.charged_to_user_id = u.id
        LEFT JOIN ({ARCHIVE_GRAMS_BY_JOB_SQL}) pa ON pa.job_id = j.id
        WHERE j.charged_to_user_id IS NOT NULL
          AND j.status IN ('completed', 'failed')
          AND j.created_at >= {sql.now_offset('-30 days')}
        GROUP BY j.charged_to_user_id, u.username, j.charged_to_org_id
        ORDER BY total_cost DESC
    """)).fetchall()

    currencies = {}

    def _currency(org_id):
        if type(org_id) is not int or org_id <= 0:
            return None
        if org_id not in currencies:
            currencies[org_id] = read_currency(session, owner_scope='org', owner_key=org_id)
        return currencies[org_id]

    def _cost_cell(total, unpriced, org_id):
        if total is None:
            return "Unknown"
        currency = _currency(org_id)
        amount = f"{currency} {total:.2f}" if currency else f"{total:.2f}"
        return amount + (f" (+{int(unpriced)} unpriced)" if unpriced else "")

    table_rows = ""
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0] or 'Unknown'))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{escape(_cost_cell(r[2], r[5], r[6]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[3], 1)}h</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[4])}g</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days chargebacks by user. Costs are job estimates in the charged organization's configured currency (no currency shown when none is configured); unpriced jobs are counted, not valued.</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">USER</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">JOBS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COST</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">HOURS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">GRAMS</th>
        </tr></thead>
        <tbody>{table_rows if table_rows else '<tr><td style="padding: 8px; color: #666;" colspan="5">No chargebacks</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Chargeback Summary Report", content)


# Report types derived from the Pro "analytics" entitlement (same data as
# /api/analytics and /api/reports/chargebacks). job_summary and
# filament_consumption mirror the free CSV exports and stay free.
ANALYTICS_REPORT_TYPES = frozenset({"fleet_utilization", "failure_analysis", "chargeback_summary"})


def report_type_is_licensed(report_type) -> bool:
    if report_type not in ANALYTICS_REPORT_TYPES:
        return True
    from license_manager import get_license
    return get_license().has_feature("analytics")


GENERATORS = {
    "fleet_utilization": generate_fleet_utilization,
    "job_summary": generate_job_summary,
    "filament_consumption": generate_filament_consumption,
    "failure_analysis": generate_failure_analysis,
    "chargeback_summary": generate_chargeback_summary,
}


# =============================================================================
# Scheduled report processing
# =============================================================================

def process_due_reports(session):
    """Find and execute all due report schedules."""
    now = datetime.now(timezone.utc)
    rows = session.execute(text("""
        SELECT id, name, report_type, frequency, recipients, filters
        FROM report_schedules
        WHERE is_active IS TRUE AND next_run_at <= :now
    """), {"now": now.isoformat()}).fetchall()

    if not rows:
        return

    smtp_config = get_smtp_config(session)
    if not smtp_config:
        log.warning("Reports due but SMTP not configured, skipping")
        return

    for row in rows:
        sched_id = row[0]
        name = row[1]
        report_type = row[2]
        frequency = row[3]
        try:
            recipients = json.loads(row[4]) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            recipients = []
        try:
            filters = json.loads(row[5]) if row[5] else {}
        except (json.JSONDecodeError, TypeError):
            filters = {}

        generator = GENERATORS.get(report_type)
        if not generator:
            log.warning(f"Unknown report type '{report_type}' for schedule {sched_id}")
            continue

        if not report_type_is_licensed(report_type):
            # Not sent, but rescheduled below so the skip is logged once per period.
            log.warning("Report '%s' (schedule %s, type=%s) skipped: requires the Pro analytics entitlement",
                        name, sched_id, report_type)
        else:
            try:
                html = generator(session, filters)
                subject = f"O.D.I.N. Report: {name}"
                sent = 0
                for recip in recipients:
                    if send_report_email(smtp_config, recip.strip(), subject, html):
                        sent += 1
                log.info(f"Report '{name}' (type={report_type}) sent to {sent}/{len(recipients)} recipients")
            except Exception as e:
                log.error("Report generation failed for schedule %s (%s)", sched_id, type(e).__name__)

        # Update schedule timing
        if frequency == "daily":
            next_run = now + timedelta(days=1)
        elif frequency == "weekly":
            next_run = now + timedelta(weeks=1)
        else:
            next_run = now + timedelta(days=30)
        next_run = next_run.replace(hour=8, minute=0, second=0, microsecond=0)

        session.execute(text("""
            UPDATE report_schedules
            SET last_run_at = :now, next_run_at = :next
            WHERE id = :id
        """), {"now": now.isoformat(), "next": next_run.isoformat(), "id": sched_id})
        session.commit()


# =============================================================================
# Quiet hours digest
# =============================================================================

def _digest_scope_key(org_id):
    return "system" if org_id is None else f"org:{int(org_id)}"


def _claim_digest_send(session, user_id: int, org_id, window_ended_at) -> bool:
    """Claim before delivery; historical claims block all scopes conservatively."""
    window = window_ended_at.isoformat()
    try:
        legacy = session.execute(text(
            "SELECT 1 FROM quiet_hours_digest_sends WHERE user_id=:uid "
            "AND window_ended_at=:window AND scope_key='legacy'"
        ), {"uid": user_id, "window": window}).first()
        if legacy:
            return False
        session.execute(text(
            "INSERT INTO quiet_hours_digest_sends "
            "(user_id,org_id,scope_key,window_ended_at,delivery_status) "
            "VALUES (:uid,:org,:scope,:window,'pending')"
        ), {"uid": user_id, "org": org_id, "scope": _digest_scope_key(org_id), "window": window})
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False


def _update_digest_status(session, user_id, window_ended_at, status, org_id=None):
    try:
        session.execute(text(
            "UPDATE quiet_hours_digest_sends SET delivery_status=:status "
            "WHERE user_id=:uid AND scope_key=:scope AND window_ended_at=:window"
        ), {"status": status[:120], "uid": user_id, "scope": _digest_scope_key(org_id),
            "window": window_ended_at.isoformat()})
        session.commit()
    except Exception:
        session.rollback()


def _claim_org_webhook_send(session, org_id, window_ended_at, scope_key=None):
    window = window_ended_at.isoformat()
    scope_key = scope_key or _digest_scope_key(org_id)
    try:
        legacy = session.execute(text(
            "SELECT 1 FROM quiet_hours_org_digest_sends WHERE scope_key LIKE 'legacy:%' "
            "AND window_ended_at=:window AND (org_id=:org OR (org_id IS NULL AND :org IS NULL))"
        ), {"window": window, "org": org_id}).first()
        if legacy:
            return False
        session.execute(text(
            "INSERT INTO quiet_hours_org_digest_sends(org_id,scope_key,window_ended_at,delivery_status) "
            "VALUES (:org,:scope,:window,'pending')"
        ), {"org": org_id, "scope": scope_key, "window": window})
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False


def _update_org_digest_status(session, org_id, window_ended_at, status, scope_key=None):
    try:
        session.execute(text(
            "UPDATE quiet_hours_org_digest_sends SET delivery_status=:status "
            "WHERE scope_key=:scope AND window_ended_at=:window"
        ), {"status": status[:120], "scope": scope_key or _digest_scope_key(org_id),
            "window": window_ended_at.isoformat()})
        session.commit()
    except Exception:
        session.rollback()


def _deliver_digest_email(session, smtp_config, user_id, alerts, window_end):
    """Send a digest email to one user via the existing SMTP plumbing."""
    user_row = session.execute(text(
        "SELECT email FROM users WHERE id = :id"
    ), {"id": user_id}).fetchone()
    if not user_row or not user_row[0]:
        return False

    from modules.notifications.quiet_hours import format_digest_html

    html = format_digest_html(alerts)
    subject = (
        f"O.D.I.N. Quiet Hours Digest — {len(alerts)} alert"
        f"{'s' if len(alerts) != 1 else ''} "
        f"(window ended {window_end.strftime('%Y-%m-%d %H:%M UTC')})"
    )
    return send_report_email(smtp_config, user_row[0], subject, html)


def _deliver_digest_push(session, user_id, alerts, window_end):
    """Fire a single push notification summarizing the digest to a user's
    subscribed devices. Reuses the existing `send_push_notification` path
    so VAPID / subscription bookkeeping stays centralized."""
    try:
        from modules.notifications.channels import send_push_notification
    except Exception as e:
        log.error(f"Cannot import send_push_notification: {e}")
        return False
    try:
        return send_push_notification(
            user_id=user_id,
            alert_type="quiet_hours_digest",
            title=f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''} during quiet hours",
            message=(
                f"Window ended {window_end.strftime('%H:%M UTC')} — "
                "tap to review in ODIN."
            ),
        ) is True
    except Exception as e:
        log.error(f"Digest push failed for user {user_id}: {e}")
        return False


def _digest_webhook_text(alerts: list, org_name: str, window_end):
    count = len(alerts)
    title = f"O.D.I.N. quiet hours digest: {count} alert{'s' if count != 1 else ''}"
    if org_name:
        title += f" ({org_name})"
    lines = [f"Window ended {window_end.strftime('%Y-%m-%d %H:%M UTC')}"]
    for alert in alerts[:50]:
        lines.append(f"- [{alert.get('severity') or 'info'}] {alert.get('title') or ''}")
    if count > 50:
        lines.append(f"...and {count - 50} more")
    return title, "\n".join(lines)


def _deliver_digest_webhook(url: str, wtype: str, alerts: list, org_name: str, window_end):
    """POST an aggregated digest in the webhook type's payload shape.

    Returns True only when the receiver accepted it (HTTP 2xx). User-supplied
    URLs go through safe_post (R8 DNS-pin SSRF defense).
    """
    try:
        from modules.notifications.channels import post_typed_webhook
    except Exception as e:
        log.error(f"Cannot import post_typed_webhook: {e}")
        return False

    summary = {
        "event": "quiet_hours_digest",
        "org": org_name,
        "window_ended_at": window_end.isoformat(),
        "alert_count": len(alerts),
        "alerts": [
            {
                "alert_type": a.get("alert_type"),
                "severity": a.get("severity"),
                "title": a.get("title"),
                "created_at": a.get("created_at"),
            }
            for a in alerts[:50]  # cap payload size
        ],
        "truncated": len(alerts) > 50,
    }
    severities = {a.get("severity") for a in alerts}
    severity = next((level for level in ("critical", "error", "warning") if level in severities), "info")
    title, message = _digest_webhook_text(alerts, org_name, window_end)
    return post_typed_webhook(url, wtype, event="quiet_hours_digest", title=title, message=message,
                              severity=severity, generic_body=summary)


def _dedupe_alerts(session, alerts: list) -> list:
    """One entry per alert from per-recipient in-app rows, with its owning org."""
    from core.recipient_policy import resolve_alert_scope
    seen, out = set(), []
    for alert in alerts:
        key = (alert.get("alert_type"), alert.get("title"), alert.get("printer_id"), alert.get("job_id"),
               alert.get("spool_id"), str(alert.get("created_at"))[:19])
        if key in seen:
            continue
        seen.add(key)
        try:
            _eligible, owner = resolve_alert_scope(session, printer_id=alert.get("printer_id"),
                                                   job_id=alert.get("job_id"), spool_id=alert.get("spool_id"))
        except ValueError:
            continue
        out.append({**alert, "org_id": owner})
    return out


def _org_webhook_settings(session, org_id):
    """(url, type, name) of an org's settings webhook, URL decrypted; None when unset."""
    import json
    row = session.execute(text("SELECT name, settings_json FROM groups WHERE id=:id"), {"id": org_id}).fetchone()
    if not row or not row[1]:
        return None
    try:
        settings = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    except ValueError:
        return None
    from modules.notifications.quiet_hours import _decrypt_org_webhook_url
    url = _decrypt_org_webhook_url(settings.get("webhook_url")) if isinstance(settings, dict) else None
    return (url, settings.get("webhook_type", "generic"), row[0]) if url else None


def _deliver_webhook_digests(session, org_id, org_name, config, window_end, entries: list, *, from_rows: bool):
    """Route each suppressed alert to the webhook(s) it would have reached in real
    time: its owning org's settings webhook, or the global webhooks for unscoped
    alerts. One claimed digest per destination per window; rows marked once."""
    from modules.notifications.channels import mark_deferred
    by_owner = {}
    for entry in entries:
        by_owner.setdefault(entry.get("org_id"), []).append(entry)
    for owner, items in sorted(by_owner.items(), key=lambda kv: (kv[0] is not None, kv[0] or 0)):
        if owner is None:
            outcome = _deliver_global_webhook_digests(session, items, window_end)
            # A system-level target that carries its own digest webhook URL also gets them.
            if org_id is None and config.get("webhook_url") and _claim_org_webhook_send(session, None, window_end):
                ok = _deliver_digest_webhook(config["webhook_url"], config.get("webhook_type", "generic"),
                                             items, org_name, window_end)
                _update_org_digest_status(session, None, window_end, "sent" if ok else "failed:webhook")
                for item in items:
                    previous = outcome.get(id(item))
                    if previous != "claimed_elsewhere":
                        outcome[id(item)] = ok if previous is None else (previous and ok)
        else:
            if owner == org_id and config.get("webhook_url"):
                destination = (config["webhook_url"], config.get("webhook_type", "generic"), org_name)
            else:
                destination = _org_webhook_settings(session, owner)
            outcome = None
            if destination and _claim_org_webhook_send(session, owner, window_end):
                ok = _deliver_digest_webhook(destination[0], destination[1], items, destination[2], window_end)
                _update_org_digest_status(session, owner, window_end, "sent" if ok else "failed:webhook")
                outcome = {id(item): ok for item in items}
            elif not destination:
                outcome = {id(item): None for item in items}
        if from_rows and outcome:
            for item in items:
                result = outcome.get(id(item), "claimed_elsewhere")
                if result == "claimed_elsewhere":
                    continue
                if result is True:
                    mark_deferred(session, [item["id"]], "sent")
                else:
                    mark_deferred(session, [item["id"]], "failed",
                                  "webhook:no_destination" if result is None else "webhook:not_delivered")
            session.commit()


def _deliver_global_webhook_digests(session, alerts: list, window_end):
    """Digest system-scope alerts to the global webhook channels (webhooks table).

    System-wide quiet hours suppress these channels in real time, so each
    enabled webhook receives one digest of the suppressed alerts it selects,
    claimed per (webhook, window) so a poll never re-sends it.

    Returns {id(alert): True | False | None | "claimed_elsewhere"} (None = no
    selecting destination).
    """
    from core.license_manager import get_license
    outcome = {id(a): None for a in alerts}
    if not get_license().has_feature("webhooks"):
        return outcome
    from modules.notifications.channels import selects_event
    import core.crypto as crypto
    hooks = session.execute(text(
        "SELECT id, url, webhook_type, alert_types FROM webhooks WHERE is_enabled IS TRUE ORDER BY id"
    )).fetchall()
    for hook_id, raw_url, wtype, alert_types in hooks:
        selected = [a for a in alerts if selects_event(alert_types, a.get("alert_type"))]
        if not selected:
            continue
        scope = f"webhook:{int(hook_id)}"
        if not _claim_org_webhook_send(session, None, window_end, scope_key=scope):
            for a in selected:
                outcome[id(a)] = "claimed_elsewhere"
            continue
        try:
            url = crypto.decrypt(raw_url)
        except Exception:
            url = raw_url
        ok = _deliver_digest_webhook(url, wtype, selected, "system", window_end)
        _update_org_digest_status(session, None, window_end, "sent" if ok else "failed:webhook", scope_key=scope)
        for a in selected:
            previous = outcome[id(a)]
            if previous == "claimed_elsewhere":
                continue
            outcome[id(a)] = ok if previous is None else (previous and ok)
    return outcome


def process_quiet_hours_digest(session):
    """Deliver quiet-hours digests to every configured channel.

    v1.8.5 refactor: previously this only sent email, broadcast to every
    email-enabled user, and used a global "last_digest_sent" flag that
    failed under multi-worker / multi-org deployments. Now:

    - Iterates every org with digest enabled + the system-level config
      (via iter_orgs_with_digest_enabled())
    - Per-user fan-out: respect alert_preferences.email and
      alert_preferences.browser_push; deliver via whichever channels the
      user enabled.
    - Per-org webhook: if the org has a webhook_url configured,
      dispatch a single aggregated digest webhook via safe_post().
    - Idempotency: (user_id, window_ended_at) UNIQUE on
      quiet_hours_digest_sends. A second poll within the same window is
      a no-op per user. Sibling workers race on INSERT; loser rolls back.
    - Failures log ERROR and continue to the next user/org. One bad
      SMTP endpoint can't block the whole digest run.
    """
    try:
        from modules.notifications.quiet_hours import (
            iter_orgs_with_digest_enabled,
            compute_last_window_end,
            compute_window_bounds,
            get_suppressed_alerts_for_window,
            group_suppressed_by_user,
        )
    except ImportError as e:
        log.error(f"Cannot import quiet_hours helpers: {e}")
        return

    from modules.notifications.channels import (deferred_available, governed_by, mark_deferred,
                                                pending_for_window, purge_deferred)
    try:
        if purge_deferred(session):
            session.commit()
    except Exception as e:
        session.rollback()
        log.error(f"Deferred delivery retention purge failed: {type(e).__name__}")

    targets = iter_orgs_with_digest_enabled()
    if not targets:
        return

    smtp_config = get_smtp_config(session)  # may be None — email is best-effort

    for target in targets:
        org_id = target["id"]
        org_name = target["name"]
        config = target["config"]

        window_end = compute_last_window_end(config)
        if window_end is None:
            # Still inside the window (or config disabled) — try again next poll.
            continue

        window_start, _ = compute_window_bounds(config, window_end)
        alerts = get_suppressed_alerts_for_window(window_start, window_end, org_id=org_id)
        scope = _digest_scope_key(org_id)
        # Deliveries deferred under this scope (032): external channels of
        # recipients without an in-app row, and one webhook row per alert.
        try:
            deferred = pending_for_window(session, scope, window_start, window_end)
            webhook_rows = pending_for_window(session, scope, window_start, window_end, channels=("webhook",))
            table_present = deferred_available(session)
        except Exception as e:
            session.rollback()
            log.error(f"Deferred delivery read failed: {type(e).__name__}")
            deferred, webhook_rows, table_present = [], [], False
        if not alerts and not deferred and not webhook_rows:
            continue

        # Per-user fan-out (email + push)
        # Codex pass 4 (2026-04-14): claim BEFORE delivery to prevent duplicate
        # sends. Two workers can both pass a check-then-act guard and both
        # deliver before either records the send. Atomic claim closes that.
        by_user = group_suppressed_by_user(alerts)
        deferred_by_user = {}
        for row in deferred:
            deferred_by_user.setdefault(int(row["user_id"]), []).append(row)
            by_user.setdefault(int(row["user_id"]), [])
        for user_id, user_alerts in by_user.items():
            if not _claim_digest_send(session, user_id, org_id, window_end):
                continue  # another worker won the race; they own delivery

            from core.recipient_policy import resolve_alert_scope
            active = session.execute(text("SELECT 1 FROM users WHERE id=:id AND is_active IS TRUE"),
                                     {"id": user_id}).first()
            authorized = []
            authorized_deferred = []
            if active:
                for alert in deferred_by_user.get(user_id, []):
                    try:
                        eligible, owner = resolve_alert_scope(session, printer_id=alert.get("printer_id"),
                            job_id=alert.get("job_id"), spool_id=alert.get("spool_id"))
                    except ValueError:
                        continue
                    # The row's owning org was recorded at suppression time.
                    if user_id in eligible and owner == alert.get("org_id"):
                        authorized_deferred.append(alert)
                for alert in user_alerts:
                    try:
                        eligible, owner = resolve_alert_scope(session, printer_id=alert.get("printer_id"),
                            job_id=alert.get("job_id"), spool_id=alert.get("spool_id"))
                    except ValueError:
                        continue
                    # Governed by this digest's quiet hours: the owning org's own, or
                    # the system's for unscoped alerts and orgs without their own.
                    if user_id in eligible and governed_by(owner, org_id, session):
                        authorized.append(alert)
            preferences = session.execute(text(
                "SELECT alert_type,email,browser_push FROM alert_preferences WHERE user_id=:id"
            ), {"id": user_id}).mappings().all()
            prefs = {row["alert_type"]: row for row in preferences}
            email_alerts = [a for a in authorized if prefs.get(a.get("alert_type"),{}).get("email")]
            push_alerts = [a for a in authorized if prefs.get(a.get("alert_type"),{}).get("browser_push")]
            # Deferred rows already name their channel (the preference at suppression time).
            email_alerts += [a for a in authorized_deferred if a["channel"] == "email"]
            push_alerts += [a for a in authorized_deferred if a["channel"] == "push"]
            email_on,push_on = bool(email_alerts),bool(push_alerts)
            email_ok = push_ok = None

            # Delivery is best-effort post-claim. A failure here logs ERROR
            # but does NOT undo the claim — undoing would re-open the
            # duplicate-send window on the next poll. Operators see the
            # error in logs and can re-trigger manually if needed.
            # v1.8.8: also record per-delivery status in the row so the
            # Alerts page can surface "delivered / failed" without
            # scraping logs.
            delivery_ok = bool(email_on or push_on)
            failure_reasons: list[str] = [] if delivery_ok else ["no_enabled_channel"]
            if email_on and not smtp_config:
                delivery_ok = False
                failure_reasons.append("email:unconfigured")
            if email_on and smtp_config:
                try:
                    email_ok = _deliver_digest_email(session, smtp_config, user_id, email_alerts, window_end) is True
                    if not email_ok:
                        delivery_ok = False
                        failure_reasons.append("email:rejected")
                except Exception as e:
                    log.error(f"Digest email failed for user {user_id}: {e}")
                    delivery_ok = False
                    failure_reasons.append(f"email:{type(e).__name__}")
            if push_on:
                try:
                    push_ok = _deliver_digest_push(session, user_id, push_alerts, window_end) is True
                    if not push_ok:
                        delivery_ok = False
                        failure_reasons.append("push:rejected")
                except Exception as e:
                    log.error(f"Digest push failed for user {user_id}: {e}")
                    delivery_ok = False
                    failure_reasons.append(f"push:{type(e).__name__}")
            status = "sent" if delivery_ok else f"failed:{','.join(failure_reasons)}"
            _update_digest_status(session, user_id, window_end, status, org_id=org_id)
            # Record each deferred row's outcome; this claim owns them, so a later
            # poll never re-sends them.
            try:
                outcome = {"email": email_ok, "push": push_ok}
                authorized_ids = {a["id"] for a in authorized_deferred}
                for row in deferred_by_user.get(user_id, []):
                    if row["id"] not in authorized_ids:
                        mark_deferred(session, [row["id"]], "failed", "recipient_not_authorized")
                    elif outcome[row["channel"]] is True:
                        mark_deferred(session, [row["id"]], "sent")
                    else:
                        mark_deferred(session, [row["id"]], "failed", f"{row['channel']}:not_delivered")
                session.commit()
            except Exception as e:
                session.rollback()
                log.error(f"Deferred delivery outcome update failed for user {user_id}: {type(e).__name__}")

        # Webhook digests: one entry per suppressed alert. With 032 present the
        # per-alert webhook rows are authoritative (they also cover alerts that
        # had no in-app recipient); otherwise in-app rows are deduplicated.
        try:
            _deliver_webhook_digests(session, org_id, org_name, config, window_end,
                                     webhook_rows if table_present else _dedupe_alerts(session, alerts),
                                     from_rows=table_present)
        except Exception as e:
            session.rollback()
            log.error(f"Webhook digest failed: {type(e).__name__}")

        log.info(
            f"Quiet hours digest delivered: org={org_name} "
            f"users={len(by_user)} alerts={len(alerts)} "
            f"window_ended={window_end.isoformat()}"
        )


# =============================================================================
# On-demand report execution (called by API)
# =============================================================================

class ReportDeliveryError(RuntimeError):
    """Safe delivery counts, without recipients or transport exception details."""
    def __init__(self,sent,total):
        super().__init__('Report delivery incomplete')
        self.sent=sent
        self.failed=total-sent


def run_report(schedule: dict) -> None:
    """Generate and send a single scheduled report immediately.

    Args:
        schedule: dict with keys name, report_type, recipients (JSON string),
                  filters (JSON string). Matches the report_schedules row shape.
    Raises:
        RuntimeError: if SMTP is not configured or report_type is unknown.
    """
    session = SessionLocal()
    try:
        smtp_config = get_smtp_config(session)
        if not smtp_config:
            raise RuntimeError("SMTP not configured")

        report_type = schedule.get("report_type")
        generator = GENERATORS.get(report_type)
        if not generator:
            raise RuntimeError(f"Unknown report type '{report_type}'")

        try:
            recipients = json.loads(schedule["recipients"]) if schedule.get("recipients") else []
        except (json.JSONDecodeError, TypeError):
            recipients = []

        try:
            filters = json.loads(schedule["filters"]) if schedule.get("filters") else {}
        except (json.JSONDecodeError, TypeError):
            filters = {}

        try:
            recipients = normalize_recipients(recipients)
        except ValueError:
            raise RuntimeError("Report recipients are invalid") from None
        html = generator(session, filters)
        subject = f"O.D.I.N. Report: {schedule.get('name', 'Report')}"
        sent = 0
        for recip in recipients:
            if send_report_email(smtp_config, recip.strip(), subject, html):
                sent += 1
        log.info(f"Run-now report '{schedule.get('name')}' (type={report_type}) sent to {sent}/{len(recipients)} recipients")
        if sent != len(recipients):
            raise ReportDeliveryError(sent,len(recipients))
    finally:
        session.close()


# =============================================================================
# Main loop
# =============================================================================

def main_loop():
    """Main polling loop."""
    log.info("Report runner daemon started")

    while True:
        try:
            session = SessionLocal()
            try:
                process_due_reports(session)
                process_quiet_hours_digest(session)
            finally:
                session.close()
        except Exception as e:
            log.error(f"Main loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
