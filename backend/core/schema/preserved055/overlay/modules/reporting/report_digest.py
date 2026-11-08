"""Quiet-hours digest implementation moved from report_runner for its line budget."""
import json
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from core.db_compat import sql

log = logging.getLogger('odin.report_runner')


def get_smtp_config(session):
    from modules.reporting import report_runner
    return report_runner.get_smtp_config(session)


def send_report_email(*args, **kwargs):
    from modules.reporting import report_runner
    return report_runner.send_report_email(*args, **kwargs)


def email_is_licensed():
    from modules.reporting import report_runner
    return report_runner.email_is_licensed()


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


