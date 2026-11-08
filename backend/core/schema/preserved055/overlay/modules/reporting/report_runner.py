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
GUARDIAN_WATCH_DELAY_SECONDS = 300
_STARTED_AT = time.monotonic()

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

from modules.reporting.report_generators import (
    _wrap_report_html, generate_fleet_utilization, generate_job_summary,
    generate_filament_consumption, generate_failure_analysis, generate_chargeback_summary,
)

# Report types derived from the Pro "analytics" entitlement (same data as
# /api/analytics and /api/reports/chargebacks). job_summary and
# filament_consumption mirror the free CSV exports and stay free.
ANALYTICS_REPORT_TYPES = frozenset({"fleet_utilization", "failure_analysis", "chargeback_summary"})


def email_is_licensed() -> bool:
    """Report and digest emails are Pro (email_notifications), like alert email."""
    from license_manager import get_license
    return get_license().has_feature("email_notifications")


def report_type_is_licensed(report_type) -> bool:
    if report_type == 'program_report':
        from license_manager import get_license
        return get_license().has_feature('program_reports') and get_license().has_feature('education_workflows')
    if report_type not in ANALYTICS_REPORT_TYPES:
        return True
    from license_manager import get_license
    return get_license().has_feature("analytics")


def generate_program_report_email(session, filters):
    from modules.organizations.services import generate_program_report_email as generate
    return generate(session, filters)


GENERATORS = {
    "program_report": generate_program_report_email,
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
    from modules.organizations.services import deactivate_program_schedules
    deactivate_program_schedules(session)
    session.commit()
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
    if not email_is_licensed():
        log.warning("Reports due but the license has no email notifications (Pro), skipping")
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
            log.warning("Report '%s' (schedule %s, type=%s) skipped: report type entitlement is unavailable",
                        name, sched_id, report_type)
        else:
            try:
                if report_type == 'program_report':
                    from modules.organizations.services import validate_program_recipients
                    validate_program_recipients(session, filters, recipients)
                html = generator(session, filters)
                subject = f"O.D.I.N. Report: {name}"
                sent = 0
                for recip in recipients:
                    if report_type == 'program_report':
                        validate_program_recipients(session, filters, [recip])
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

from modules.reporting.report_digest import (
    _digest_scope_key,
    _claim_digest_send,
    _update_digest_status,
    _claim_org_webhook_send,
    _update_org_digest_status,
    _deliver_digest_email,
    _deliver_digest_push,
    _digest_webhook_text,
    _deliver_digest_webhook,
    _dedupe_alerts,
    _org_webhook_settings,
)

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
                                             _external_digest_rows(session, items), org_name, window_end)
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
                ok = _deliver_digest_webhook(destination[0], destination[1], _external_digest_rows(session, items), destination[2], window_end)
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
        ok = _deliver_digest_webhook(url, wtype, _external_digest_rows(session, selected), "system", window_end)
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
            email_unlicensed = email_on and not email_is_licensed()
            if email_unlicensed:
                email_on = False  # digest email needs email_notifications (Pro), like alert email

            # Delivery is best-effort post-claim. A failure here logs ERROR
            # but does NOT undo the claim — undoing would re-open the
            # duplicate-send window on the next poll. Operators see the
            # error in logs and can re-trigger manually if needed.
            # v1.8.8: also record per-delivery status in the row so the
            # Alerts page can surface "delivered / failed" without
            # scraping logs.
            delivery_ok = bool(email_on or push_on) and not email_unlicensed
            failure_reasons: list[str] = ["email:unlicensed"] if email_unlicensed else ([] if delivery_ok else ["no_enabled_channel"])
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
        if not email_is_licensed():
            raise RuntimeError("Report emails need a license with email notifications (Pro)")

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
        if not report_type_is_licensed(report_type):
            raise RuntimeError("Report type is not licensed")
        if report_type == 'program_report':
            from modules.organizations.services import deactivate_program_schedules, validate_program_recipients
            deactivate_program_schedules(session)
            session.commit()
            live = session.execute(text('SELECT is_active FROM report_schedules WHERE id=:i'),
                                   {'i': schedule.get('id')}).scalar()
            if not live:
                raise RuntimeError('Program report schedule is inactive')
            validate_program_recipients(session, filters, recipients)
        html = generator(session, filters)
        subject = f"O.D.I.N. Report: {schedule.get('name', 'Report')}"
        sent = 0
        for recip in recipients:
            if report_type == 'program_report':
                validate_program_recipients(session, filters, [recip])
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

def _check_database_guardian():
    """This loop is the watcher for the database guardian, which can't report its own death."""
    # After a long outage its last result looks old until its first check of this boot finishes.
    if time.monotonic() - _STARTED_AT < GUARDIAN_WATCH_DELAY_SECONDS:
        return
    try:
        from modules.system.db_guardian_status import alert_if_stopped
        alert_if_stopped(DATABASE_URL, session_factory=SessionLocal)
    except Exception as e:
        log.error(f"Database guardian check failed ({type(e).__name__})")


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

        _check_database_guardian()
        time.sleep(POLL_INTERVAL)




def _external_digest_rows(session, rows):
    from core.controlled_events import external_payload
    return [external_payload(session, row) for row in rows]

if __name__ == "__main__":
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    main_loop()
