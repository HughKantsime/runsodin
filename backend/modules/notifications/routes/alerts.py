"""O.D.I.N. — Alerts, Alert Preferences, and SMTP Configuration."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import text
from typing import List, Optional
import logging

import core.crypto as crypto
from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import AGENT_READ_SCOPE, AGENT_WRITE_SCOPE, require_any_scope, require_role
from core.responses import build_next_actions, next_action
from core.base import AlertType
from core.models import SystemConfig
from modules.notifications.models import Alert, AlertPreference
from modules.inventory.models import Spool
from modules.notifications.schemas import (
    AlertResponse, AlertSummary,
    AlertPreferenceResponse, AlertPreferencesUpdate,
    SmtpConfigBase, SmtpConfigResponse,
)

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Alerts"])


# ============== Alerts ==============

@router.get("/alerts/digest-status", tags=["Alerts"])
def get_digest_status(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the most recent quiet-hours digest delivery outcome for the
    current user (per-user) and their org (org-level webhook).

    v1.8.8: surfaces the `delivery_status` column added in migration 004
    so operators can see at a glance whether last night's digest actually
    landed instead of having to scrape report_runner logs.

    Response shape:
      {
        "user_digest": {
          "window_ended_at": "2026-04-14T07:00:00",
          "status": "sent" | "failed:email:SMTPError,..." | "pending" | null,
          "claimed": true | false
        } | null,
        "org_digest": { same shape, keyed on the user's org } | null
      }
    """
    user_id = current_user.get("id")
    org_id = current_user.get("group_id")

    # Most recent per-user row (might be None if no digest in recent history).
    user_row = None
    try:
        user_row = db.execute(text(
            "SELECT window_ended_at, delivery_status, sent_at "
            "FROM quiet_hours_digest_sends "
            "WHERE user_id = :uid "
            "ORDER BY window_ended_at DESC LIMIT 1"
        ), {"uid": user_id}).fetchone()
    except Exception as e:
        # Table doesn't exist yet (migration 002/004 hasn't applied).
        # Treat as "no digest data yet" rather than 500.
        log.debug(f"digest-status user query failed: {e}")

    user_digest = None
    if user_row:
        user_digest = {
            "window_ended_at": user_row[0],
            "status": user_row[1] or "pending",
            "sent_at": user_row[2],
            "claimed": True,
        }

    # Most recent org-level row.
    org_digest = None
    if org_id is not None:
        try:
            org_row = db.execute(text(
                "SELECT window_ended_at, delivery_status, sent_at "
                "FROM quiet_hours_org_digest_sends "
                "WHERE org_id = :oid "
                "ORDER BY window_ended_at DESC LIMIT 1"
            ), {"oid": org_id}).fetchone()
            if org_row:
                org_digest = {
                    "window_ended_at": org_row[0],
                    "status": org_row[1] or "pending",
                    "sent_at": org_row[2],
                    "claimed": True,
                }
        except Exception as e:
            log.debug(f"digest-status org query failed: {e}")

    return {"user_digest": user_digest, "org_digest": org_digest}


@router.get("/alerts", response_model=List[AlertResponse])
async def list_alerts(
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    is_read: Optional[bool] = None,
    limit: int = 25,
    offset: int = 0,
    # Stacked auth (Phase 2 canonical read shape). Replaces the ad-hoc
    # `get_current_user + if not current_user` pattern — require_role
    # raises the auth error structurally.
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE)
    ),
    db: Session = Depends(get_db),
):
    """List alerts for the current user."""

    query = db.query(Alert).options(
        selectinload(Alert.printer),
        selectinload(Alert.job),
        selectinload(Alert.spool).selectinload(Spool.filament),
    ).filter(Alert.user_id == current_user["id"])

    if severity:
        query = query.filter(Alert.severity == severity)
    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)
    if is_read is not None:
        query = query.filter(Alert.is_read == is_read)

    alerts = query.order_by(Alert.created_at.desc()).offset(offset).limit(limit).all()

    results = []
    for alert in alerts:
        data = AlertResponse.model_validate(alert)
        if alert.printer:
            data.printer_name = alert.printer.nickname or alert.printer.name
        if alert.job:
            data.job_name = alert.job.item_name
        if alert.spool and alert.spool.filament:
            data.spool_name = f"{alert.spool.filament.brand} {alert.spool.filament.name}"
        results.append(data)

    return results


@router.get("/alerts/unread-count")
async def get_unread_count(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get unread alert count for bell badge."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    count = db.query(Alert).filter(
        Alert.user_id == current_user["id"],
        Alert.is_read == False
    ).count()
    return {"unread_count": count}


@router.get("/alerts/summary", response_model=AlertSummary)
async def get_alert_summary(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get aggregated alert counts for dashboard widget."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    uid = current_user["id"]
    base = db.query(Alert).filter(
        Alert.user_id == uid,
        Alert.is_dismissed == False,
        Alert.is_read == False
    )

    failed = base.filter(Alert.alert_type == AlertType.PRINT_FAILED).count()
    spool = base.filter(Alert.alert_type == AlertType.SPOOL_LOW).count()
    maint = base.filter(Alert.alert_type == AlertType.MAINTENANCE_OVERDUE).count()

    return AlertSummary(
        print_failed=failed,
        spool_low=spool,
        maintenance_overdue=maint,
        total=failed + spool + maint
    )


@router.patch("/alerts/{alert_id}/read")
async def mark_alert_read(
    alert_id: int,
    request: Request,
    # Stacked auth (Phase 2 canonical) — viewer floor for JWT, agent:write
    # for scoped tokens. See routes_controls.py::pause_printer for rationale.
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Mark a single alert as read.

    Agent-surface (v1.9.0 Phase 2): honors X-Dry-Run, emits next_actions.
    """
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"],
    ).first()
    if not alert:
        raise OdinError(
            ErrorCode.alert_not_found,
            f"Alert {alert_id} not found",
            status=404,
        )

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "mark_alert_read",
                "alert_id": alert_id,
                "from_is_read": alert.is_read,
                "to_is_read": True,
            },
            next_actions=[
                next_action(
                    "list_alerts",
                    {"unread_only": True},
                    "verify unread count decreased",
                ),
            ],
            notes="Would update alerts.is_read; no other side effects.",
        )

    alert.is_read = True
    db.commit()
    return {
        "status": "ok",
        "alert_id": alert_id,
        "is_read": True,
        "next_actions": build_next_actions(
            next_action(
                "list_alerts",
                {"unread_only": True},
                "confirm unread count",
            ),
        ),
    }


@router.post("/alerts/mark-all-read")
async def mark_all_read(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark all alerts as read for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(Alert).filter(
        Alert.user_id == current_user["id"],
        Alert.is_read == False
    ).update({"is_read": True})
    db.commit()
    return {"status": "ok"}


@router.patch("/alerts/{alert_id}/dismiss")
async def dismiss_alert(
    alert_id: int,
    request: Request,
    # Stacked auth (Phase 2 canonical).
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Dismiss an alert (hide from dashboard widget).

    Also marks read — dismissing implies reading. Agent-surface v1.9.0.
    """
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"],
    ).first()
    if not alert:
        raise OdinError(
            ErrorCode.alert_not_found,
            f"Alert {alert_id} not found",
            status=404,
        )

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "dismiss_alert",
                "alert_id": alert_id,
                "from_is_dismissed": alert.is_dismissed,
                "to_is_dismissed": True,
                "also_marks_read": True,
            },
            next_actions=[
                next_action(
                    "list_alerts",
                    {},
                    "verify alert no longer surfaces",
                ),
            ],
            notes="Would update alerts.is_dismissed+is_read; no other side effects.",
        )

    alert.is_dismissed = True
    alert.is_read = True
    db.commit()
    return {
        "status": "ok",
        "alert_id": alert_id,
        "is_dismissed": True,
        "is_read": True,
        "next_actions": build_next_actions(
            next_action(
                "list_alerts",
                {},
                "confirm alert dismissed",
            ),
        ),
    }


# ============== Alert Preferences ==============

@router.get("/alert-preferences", response_model=List[AlertPreferenceResponse])
async def get_alert_preferences(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user's alert preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    prefs = db.query(AlertPreference).filter(
        AlertPreference.user_id == current_user["id"]
    ).all()

    if not prefs:
        from modules.notifications.alert_dispatcher import seed_alert_preferences
        seed_alert_preferences(db, current_user["id"])
        prefs = db.query(AlertPreference).filter(
            AlertPreference.user_id == current_user["id"]
        ).all()

    return prefs


@router.put("/alert-preferences")
async def update_alert_preferences(
    data: AlertPreferencesUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Bulk update alert preferences for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    uid = current_user["id"]
    for pref_data in data.preferences:
        existing = db.query(AlertPreference).filter(
            AlertPreference.user_id == uid,
            AlertPreference.alert_type == pref_data.alert_type
        ).first()

        if existing:
            existing.in_app = pref_data.in_app
            existing.browser_push = pref_data.browser_push
            existing.email = pref_data.email
            existing.threshold_value = pref_data.threshold_value
        else:
            db.add(AlertPreference(
                user_id=uid,
                alert_type=pref_data.alert_type,
                in_app=pref_data.in_app,
                browser_push=pref_data.browser_push,
                email=pref_data.email,
                threshold_value=pref_data.threshold_value
            ))

    db.commit()
    return {"status": "ok", "message": "Preferences updated"}


# ============== SMTP Config (Admin Only) ==============

@router.get("/smtp-config")
async def get_smtp_config(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Get SMTP configuration (admin only, password masked)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config:
        return SmtpConfigResponse()
    smtp = config.value
    return SmtpConfigResponse(
        enabled=smtp.get("enabled", False),
        host=smtp.get("host", ""),
        port=smtp.get("port", 587),
        username=smtp.get("username", ""),
        password_set=bool(smtp.get("password")),
        from_address=smtp.get("from_address", ""),
        use_tls=smtp.get("use_tls", True)
    )


@router.put("/smtp-config")
async def update_smtp_config(
    data: SmtpConfigBase,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update SMTP configuration (admin only)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp_data = data.dict()

    if not smtp_data.get("password") and config and config.value.get("password"):
        smtp_data["password"] = config.value["password"]

    # Encrypt password if a new plaintext value was provided
    if smtp_data.get("password") and not crypto.is_encrypted(smtp_data["password"]):
        smtp_data["password"] = crypto.encrypt(smtp_data["password"])

    if config:
        config.value = smtp_data
    else:
        db.add(SystemConfig(key="smtp_config", value=smtp_data))

    log_audit(db, "smtp_config_updated", "system", details={"enabled": smtp_data.get("enabled"), "host": smtp_data.get("host")})
    db.commit()
    return {"status": "ok", "message": "SMTP configuration updated"}


@router.post("/alerts/test-email")
async def send_test_email(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Send a test email to the current user (admin only)."""
    from modules.notifications.alert_dispatcher import _get_smtp_config, _deliver_email

    smtp = _get_smtp_config(db)
    if not smtp:
        raise HTTPException(status_code=400, detail="SMTP not configured or not enabled")
    if not current_user.get("email"):
        raise HTTPException(status_code=400, detail="Your account has no email address")

    _deliver_email(
        db, current_user["id"],
        "Test Alert \u2014 O.D.I.N.",
        "This is a test notification. If you received this, SMTP is configured correctly.",
        "info"
    )
    return {"status": "ok", "message": f"Test email queued to {current_user['email']}"}
