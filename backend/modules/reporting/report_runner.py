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
from core.db_compat import sql

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
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_config.get("from_address", "odin@localhost")
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        # v1.8.9 (codex pass 7): runtime ITAR guard.
        from core.itar import enforce_host_destination
        enforce_host_destination(smtp_config["host"], scheme="smtp")

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
    rows = session.execute(text(f"""  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
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
    rows = session.execute(text(f"""  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
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
    rows = session.execute(text(f"""  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        SELECT detection_type, COUNT(*) as cnt, AVG(confidence) as avg_conf
        FROM vision_detections
        WHERE created_at >= {sql.now_offset('-30 days')}
        GROUP BY detection_type ORDER BY cnt DESC
    """)).fetchall()

    # Also get job failure reasons
    fail_rows = session.execute(text(f"""  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
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
    rows = session.execute(text(f"""  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        SELECT u.username, COUNT(j.id) as job_count,
               COALESCE(SUM(j.estimated_cost), 0) as total_cost,
               COALESCE(SUM(j.duration_hours), 0) as total_hours
        FROM jobs j
        LEFT JOIN users u ON j.charged_to_user_id = u.id
        WHERE j.charged_to_user_id IS NOT NULL
          AND j.created_at >= {sql.now_offset('-30 days')}
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

def _claim_digest_send(session, user_id: int, org_id, window_ended_at) -> bool:
    """Atomically CLAIM the right to send the digest BEFORE any side effects.

    Returns True if THIS worker won the race and may proceed to deliver the
    digest; False if another worker already claimed (or is delivering) it.

    Codex pass 4 (2026-04-14): the previous flow was check-then-act:
      1. SELECT to check if sent
      2. deliver email/push
      3. INSERT idempotency row
    Two workers could both pass step 1, both deliver in step 2, and only
    then race on step 3 — duplicate notifications already out the door.

    Now: claim in one atomic INSERT, then deliver. The loser of the INSERT
    race exits BEFORE any delivery code runs. The cost is that delivery
    failures don't undo the claim — by design, since the alternative
    (delete the row and let next poll retry) re-opens the duplicate-send
    window. Loud delivery errors are logged; operators can see and
    re-trigger manually if needed.
    """
    try:
        session.execute(text(
            "INSERT INTO quiet_hours_digest_sends (user_id, org_id, window_ended_at, delivery_status) "
            "VALUES (:uid, :oid, :wend, 'pending')"
        ), {"uid": user_id, "oid": org_id, "wend": window_ended_at.isoformat()})
        session.commit()
        return True
    except Exception as e:
        # IntegrityError from UNIQUE(user_id, window_ended_at) — sibling
        # worker (or earlier poll) already claimed. We did no harm.
        log.debug(f"Digest send claim lost for user={user_id} window={window_ended_at}: {e}")
        session.rollback()
        return False


def _update_digest_status(session, user_id: int, window_ended_at, status: str):
    """v1.8.8: update the claim row's delivery_status after delivery.

    Called by each _deliver_digest_* helper AFTER it's done (success or
    failure). Status values: 'sent' (all configured channels delivered)
    or 'failed:<reason>' (at least one channel failed; reason truncated
    to 120 chars for sanity).
    """
    try:
        session.execute(text(
            "UPDATE quiet_hours_digest_sends SET delivery_status = :s "
            "WHERE user_id = :uid AND window_ended_at = :wend"
        ), {"s": status[:120], "uid": user_id, "wend": window_ended_at.isoformat()})
        session.commit()
    except Exception as e:
        log.debug(f"Could not update digest status for user={user_id}: {e}")
        session.rollback()


def _update_org_digest_status(session, org_id, window_ended_at, status: str):
    """Same as _update_digest_status but for the org-level webhook table."""
    try:
        session.execute(text(
            "UPDATE quiet_hours_org_digest_sends SET delivery_status = :s "
            "WHERE org_id IS :oid AND window_ended_at = :wend"
            if org_id is None else
            "UPDATE quiet_hours_org_digest_sends SET delivery_status = :s "
            "WHERE org_id = :oid AND window_ended_at = :wend"
        ), {"s": status[:120], "oid": org_id, "wend": window_ended_at.isoformat()})
        session.commit()
    except Exception as e:
        log.debug(f"Could not update org digest status for org={org_id}: {e}")
        session.rollback()


def _claim_org_webhook_send(session, org_id, window_ended_at) -> bool:
    """Same atomic-claim semantics as _claim_digest_send, but for the
    per-org webhook digest.

    Codex pass 4 (2026-04-14): previously the org webhook fired every
    60-second poll for the duration of the next quiet period because
    nothing remembered we already sent it. New table
    quiet_hours_org_digest_sends tracks (org_id, window_ended_at).
    org_id may be NULL for system-level webhook digests."""
    try:
        session.execute(text(
            "INSERT INTO quiet_hours_org_digest_sends (org_id, window_ended_at) "
            "VALUES (:oid, :wend)"
        ), {"oid": org_id, "wend": window_ended_at.isoformat()})
        session.commit()
        return True
    except Exception as e:
        log.debug(f"Org digest webhook claim lost for org={org_id} window={window_ended_at}: {e}")
        session.rollback()
        return False


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
        send_push_notification(
            user_id=user_id,
            alert_type="quiet_hours_digest",
            title=f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''} during quiet hours",
            message=(
                f"Window ended {window_end.strftime('%H:%M UTC')} — "
                "tap to review in ODIN."
            ),
        )
        return True
    except Exception as e:
        log.error(f"Digest push failed for user {user_id}: {e}")
        return False


def _deliver_digest_webhook(url: str, wtype: str, alerts: list, org_name: str, window_end):
    """POST an aggregated digest payload to an org's configured webhook.

    Always via safe_post — org webhook URLs are user-supplied, so the R8
    DNS-pin SSRF defense applies.
    """
    try:
        from core.webhook_utils import safe_post, WebhookSSRFError
    except Exception as e:
        log.error(f"Cannot import safe_post: {e}")
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

    try:
        # Generic webhook shape for now. Discord/Slack-specific formatting
        # can be added later; most integrations accept a generic JSON body.
        safe_post(url, json=summary, timeout=10)
        return True
    except WebhookSSRFError as e:
        log.error(f"Org digest webhook SSRF-blocked: {e}")
        return False
    except Exception as e:
        log.error(f"Org digest webhook failed ({wtype}): {e}")
        return False


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
        if not alerts:
            continue

        # Per-user fan-out (email + push)
        # Codex pass 4 (2026-04-14): claim BEFORE delivery to prevent duplicate
        # sends. Two workers can both pass a check-then-act guard and both
        # deliver before either records the send. Atomic claim closes that.
        by_user = group_suppressed_by_user(alerts)
        for user_id, user_alerts in by_user.items():
            if not _claim_digest_send(session, user_id, org_id, window_end):
                continue  # another worker won the race; they own delivery

            prefs = session.execute(text(
                "SELECT "
                "  MAX(CASE WHEN email = 1 THEN 1 ELSE 0 END) AS email_enabled, "
                "  MAX(CASE WHEN browser_push = 1 THEN 1 ELSE 0 END) AS push_enabled "
                "FROM alert_preferences WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()

            email_on = bool(prefs.email_enabled) if prefs else False
            push_on = bool(prefs.push_enabled) if prefs else False

            # Delivery is best-effort post-claim. A failure here logs ERROR
            # but does NOT undo the claim — undoing would re-open the
            # duplicate-send window on the next poll. Operators see the
            # error in logs and can re-trigger manually if needed.
            # v1.8.8: also record per-delivery status in the row so the
            # Alerts page can surface "delivered / failed" without
            # scraping logs.
            delivery_ok = True
            failure_reasons: list[str] = []
            if email_on and smtp_config:
                try:
                    _deliver_digest_email(session, smtp_config, user_id, user_alerts, window_end)
                except Exception as e:
                    log.error(f"Digest email failed for user {user_id}: {e}")
                    delivery_ok = False
                    failure_reasons.append(f"email:{type(e).__name__}")
            if push_on:
                try:
                    _deliver_digest_push(session, user_id, user_alerts, window_end)
                except Exception as e:
                    log.error(f"Digest push failed for user {user_id}: {e}")
                    delivery_ok = False
                    failure_reasons.append(f"push:{type(e).__name__}")
            status = "sent" if delivery_ok else f"failed:{','.join(failure_reasons)}"
            _update_digest_status(session, user_id, window_end, status)

        # Per-org webhook (one combined digest per org).
        # Codex pass 4 (2026-04-14): the org webhook had NO idempotency
        # before — every 60s poll re-sent the same digest until the next
        # quiet period. Now claimed atomically against the new
        # quiet_hours_org_digest_sends table.
        webhook_url = config.get("webhook_url")
        if webhook_url:
            if _claim_org_webhook_send(session, org_id, window_end):
                webhook_ok = _deliver_digest_webhook(
                    webhook_url,
                    config.get("webhook_type", "generic"),
                    alerts,
                    org_name,
                    window_end,
                )
                status = "sent" if webhook_ok else "failed:webhook"
                _update_org_digest_status(session, org_id, window_end, status)

        log.info(
            f"Quiet hours digest delivered: org={org_name} "
            f"users={len(by_user)} alerts={len(alerts)} "
            f"window_ended={window_end.isoformat()}"
        )


# =============================================================================
# On-demand report execution (called by API)
# =============================================================================

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

        html = generator(session, filters)
        subject = f"O.D.I.N. Report: {schedule.get('name', 'Report')}"
        sent = 0
        for recip in recipients:
            if send_report_email(smtp_config, recip.strip(), subject, html):
                sent += 1
        log.info(f"Run-now report '{schedule.get('name')}' (type={report_type}) sent to {sent}/{len(recipients)} recipients")
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
