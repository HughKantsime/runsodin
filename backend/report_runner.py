"""
Report Runner Daemon

Polls report_schedules for due reports and generates + emails them.
Also sends quiet hours digest when the quiet period ends.
"""

import os
import time
import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [report_runner] %(levelname)s %(message)s",
)
log = logging.getLogger("odin.report_runner")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
POLL_INTERVAL = 60  # seconds

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
    return config


def send_report_email(smtp_config, recipient, subject, html_body):
    """Send an HTML email to a single recipient."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_config.get("from_address", "odin@localhost")
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        if smtp_config.get("use_tls", True):
            server = smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 587))
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 25))

        if smtp_config.get("username") and smtp_config.get("password"):
            server.login(smtp_config["username"], smtp_config["password"])

        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        log.error(f"Failed to send email to {recipient}: {e}")
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
    active = session.execute(text("SELECT COUNT(*) FROM printers WHERE is_active = 1")).scalar() or 0
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
    rows = session.execute(text("""
        SELECT status, COUNT(*) as cnt
        FROM jobs
        WHERE created_at >= datetime('now', '-7 days')
        GROUP BY status ORDER BY cnt DESC
    """)).fetchall()

    table_rows = ""
    total = 0
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{r[0]}</td>
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
    rows = session.execute(text("""
        SELECT fl.material, COALESCE(SUM(su.weight_used_g), 0) as total_g
        FROM spool_usage su
        JOIN spools s ON su.spool_id = s.id
        JOIN filament_library fl ON s.filament_id = fl.id
        WHERE su.used_at >= datetime('now', '-30 days')
        GROUP BY fl.material ORDER BY total_g DESC
    """)).fetchall()

    table_rows = ""
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{r[0] or 'Unknown'}</td>
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
    rows = session.execute(text("""
        SELECT detection_type, COUNT(*) as cnt, AVG(confidence) as avg_conf
        FROM vision_detections
        WHERE created_at >= datetime('now', '-30 days')
        GROUP BY detection_type ORDER BY cnt DESC
    """)).fetchall()

    # Also get job failure reasons
    fail_rows = session.execute(text("""
        SELECT fail_reason, COUNT(*) as cnt
        FROM jobs
        WHERE status = 'failed' AND fail_reason IS NOT NULL
          AND created_at >= datetime('now', '-30 days')
        GROUP BY fail_reason ORDER BY cnt DESC
    """)).fetchall()

    detect_rows_html = ""
    for r in rows:
        detect_rows_html += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{r[0]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[2] * 100, 1)}%</td>
        </tr>"""

    fail_rows_html = ""
    for r in fail_rows:
        fail_rows_html += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{r[0]}</td>
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
    """Reuse query pattern from /api/reports/chargebacks."""
    rows = session.execute(text("""
        SELECT u.username, COUNT(j.id) as job_count,
               COALESCE(SUM(j.estimated_cost), 0) as total_cost,
               COALESCE(SUM(j.duration_hours), 0) as total_hours
        FROM jobs j
        LEFT JOIN users u ON j.charged_to_user_id = u.id
        WHERE j.charged_to_user_id IS NOT NULL
          AND j.created_at >= datetime('now', '-30 days')
        GROUP BY j.charged_to_user_id
        ORDER BY total_cost DESC
    """)).fetchall()

    table_rows = ""
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{r[0] or 'Unknown'}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">${round(r[2], 2)}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[3], 1)}h</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days chargebacks by user</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">USER</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">JOBS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COST</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">HOURS</th>
        </tr></thead>
        <tbody>{table_rows if table_rows else '<tr><td style="padding: 8px; color: #666;" colspan="4">No chargebacks</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Chargeback Summary Report", content)


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
        WHERE is_active = 1 AND next_run_at <= :now
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

        try:
            html = generator(session, filters)
            subject = f"O.D.I.N. Report: {name}"
            sent = 0
            for recip in recipients:
                if send_report_email(smtp_config, recip.strip(), subject, html):
                    sent += 1
            log.info(f"Report '{name}' (type={report_type}) sent to {sent}/{len(recipients)} recipients")
        except Exception as e:
            log.error(f"Failed to generate report '{name}': {e}")

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

def process_quiet_hours_digest(session):
    """Send digest email when quiet hours end."""
    try:
        from quiet_hours import is_quiet_time, get_queued_alerts_for_digest, format_digest_html, _get_config
    except ImportError:
        return

    config = _get_config()
    if not config["enabled"] or not config.get("digest_enabled", True):
        return

    # Only act when we're just past the end of quiet hours
    if is_quiet_time():
        return

    now = datetime.now(timezone.utc)
    end_h, end_m = map(int, config["end"].split(":"))

    # Check if we're within POLL_INTERVAL of quiet hours ending
    quiet_end_today = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    minutes_since_end = (now - quiet_end_today).total_seconds() / 60
    if minutes_since_end < 0 or minutes_since_end > (POLL_INTERVAL / 60 + 1):
        return

    # Check if we already sent digest today
    last_sent = session.execute(text(
        "SELECT value FROM system_config WHERE key = 'last_digest_sent'"
    )).fetchone()
    if last_sent:
        try:
            val = json.loads(last_sent[0]) if isinstance(last_sent[0], str) else last_sent[0]
            last_date = str(val)[:10]
            if last_date == now.strftime("%Y-%m-%d"):
                return  # Already sent today
        except Exception:
            pass

    alerts = get_queued_alerts_for_digest()
    if not alerts:
        return

    smtp_config = get_smtp_config(session)
    if not smtp_config:
        return

    html = format_digest_html(alerts)
    subject = f"O.D.I.N. Quiet Hours Digest — {len(alerts)} alerts"

    # Send to all users with email alerts enabled
    users = session.execute(text("""
        SELECT DISTINCT u.email FROM users u
        JOIN alert_preferences ap ON u.id = ap.user_id
        WHERE ap.email = 1 AND u.email IS NOT NULL AND u.email != ''
    """)).fetchall()

    sent = 0
    for user_row in users:
        if send_report_email(smtp_config, user_row[0], subject, html):
            sent += 1

    log.info(f"Quiet hours digest: {len(alerts)} alerts sent to {sent} users")

    # Track that we sent digest today
    session.execute(text("""
        INSERT INTO system_config (key, value) VALUES ('last_digest_sent', :val)
        ON CONFLICT(key) DO UPDATE SET value = :val
    """), {"val": json.dumps(now.isoformat())})
    session.commit()


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
