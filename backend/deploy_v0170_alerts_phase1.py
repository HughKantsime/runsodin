"""
v0.17.0 Phase 1 Deploy Script — Alerts Foundation
Patches: models.py, schemas.py, main.py
Creates: alert_dispatcher.py

Run from /opt/printfarm-scheduler/backend/
    python3 deploy_v0170_alerts_phase1.py
"""
import os
import shutil

BACKEND_DIR = "/opt/printfarm-scheduler/backend"


def backup_file(filepath):
    """Create .bak backup before editing."""
    bak = filepath + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(filepath, bak)
        print(f"  Backed up {filepath} -> {bak}")


def patch_models():
    """Append Alert, AlertPreference, PushSubscription models to models.py."""
    filepath = os.path.join(BACKEND_DIR, "models.py")
    backup_file(filepath)

    addition = '''

# ============================================================
# Alerts & Notifications (v0.17.0)
# ============================================================

class AlertType(str, Enum):
    """Types of alerts the system can generate."""
    PRINT_COMPLETE = "print_complete"
    PRINT_FAILED = "print_failed"
    SPOOL_LOW = "spool_low"
    MAINTENANCE_OVERDUE = "maintenance_overdue"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(Base):
    """
    Individual alert/notification instance.
    
    Created by the alert dispatcher when an event triggers.
    Each user gets their own alert record based on their preferences.
    """
    __tablename__ = "alerts"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)  # References users table (raw SQL)
    
    # Alert classification
    alert_type = Column(SQLEnum(AlertType), nullable=False)
    severity = Column(SQLEnum(AlertSeverity), nullable=False)
    
    # Content
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=True)
    
    # State
    is_read = Column(Boolean, default=False, index=True)
    is_dismissed = Column(Boolean, default=False)
    
    # Optional references to related entities
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    spool_id = Column(Integer, ForeignKey("spools.id"), nullable=True)
    
    # Flexible extra data
    metadata_json = Column(JSON, nullable=True)
    
    # Timestamp
    created_at = Column(DateTime, server_default=func.now(), index=True)
    
    # Relationships
    printer = relationship("Printer", foreign_keys=[printer_id])
    job = relationship("Job", foreign_keys=[job_id])
    spool = relationship("Spool", foreign_keys=[spool_id])
    
    def __repr__(self):
        return f"<Alert {self.id}: {self.alert_type.value} for user {self.user_id}>"


class AlertPreference(Base):
    """
    Per-user, per-alert-type channel configuration.
    """
    __tablename__ = "alert_preferences"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    alert_type = Column(SQLEnum(AlertType), nullable=False)
    
    # Delivery channels
    in_app = Column(Boolean, default=True)
    browser_push = Column(Boolean, default=False)
    email = Column(Boolean, default=False)
    
    # Configurable threshold
    threshold_value = Column(Float, nullable=True)
    
    def __repr__(self):
        return f"<AlertPreference user={self.user_id} type={self.alert_type.value}>"


class PushSubscription(Base):
    """Browser push notification subscription."""
    __tablename__ = "push_subscriptions"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    endpoint = Column(Text, nullable=False)
    p256dh_key = Column(Text, nullable=False)
    auth_key = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    
    def __repr__(self):
        return f"<PushSubscription user={self.user_id}>"
'''

    with open(filepath, "r") as f:
        content = f.read()

    if "class Alert(Base):" in content:
        print("  models.py already has Alert model — skipping")
        return

    with open(filepath, "a") as f:
        f.write(addition)

    print("  models.py patched with Alert, AlertPreference, PushSubscription")


def patch_schemas():
    """Append alert schemas to schemas.py."""
    filepath = os.path.join(BACKEND_DIR, "schemas.py")
    backup_file(filepath)

    addition = '''

# ============== Alert Schemas (v0.17.0) ==============

class AlertTypeEnum(str, Enum):
    PRINT_COMPLETE = "print_complete"
    PRINT_FAILED = "print_failed"
    SPOOL_LOW = "spool_low"
    MAINTENANCE_OVERDUE = "maintenance_overdue"


class AlertSeverityEnum(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    user_id: int
    alert_type: AlertTypeEnum
    severity: AlertSeverityEnum
    title: str
    message: Optional[str] = None
    is_read: bool = False
    is_dismissed: bool = False
    printer_id: Optional[int] = None
    job_id: Optional[int] = None
    spool_id: Optional[int] = None
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    
    # Populated by API for display
    printer_name: Optional[str] = None
    job_name: Optional[str] = None
    spool_name: Optional[str] = None


class AlertSummary(BaseModel):
    """Aggregated alert counts for dashboard widget."""
    print_failed: int = 0
    spool_low: int = 0
    maintenance_overdue: int = 0
    total: int = 0


class AlertPreferenceBase(BaseModel):
    alert_type: AlertTypeEnum
    in_app: bool = True
    browser_push: bool = False
    email: bool = False
    threshold_value: Optional[float] = None


class AlertPreferenceResponse(AlertPreferenceBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    user_id: int


class AlertPreferencesUpdate(BaseModel):
    """Bulk update of all alert preferences for a user."""
    preferences: List[AlertPreferenceBase]


class SmtpConfigBase(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = ""
    use_tls: bool = True


class SmtpConfigResponse(BaseModel):
    """SMTP config response (password masked)."""
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password_set: bool = False
    from_address: str = ""
    use_tls: bool = True


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    p256dh_key: str
    auth_key: str
'''

    with open(filepath, "r") as f:
        content = f.read()

    if "class AlertResponse" in content:
        print("  schemas.py already has AlertResponse — skipping")
        return

    with open(filepath, "a") as f:
        f.write(addition)

    print("  schemas.py patched with alert schemas")


def patch_main_imports():
    """Add alert imports to main.py."""
    filepath = os.path.join(BACKEND_DIR, "main.py")
    backup_file(filepath)

    with open(filepath, "r") as f:
        content = f.read()

    if "Alert, AlertPreference" in content:
        print("  main.py imports already patched — skipping")
        return

    # 1. Add model imports
    old_model_import = "from models import ("
    # Find the full import block and add to it
    content = content.replace(
        "    FilamentType, SchedulerRun, init_db, FilamentLibrary,",
        "    FilamentType, SchedulerRun, init_db, FilamentLibrary,\n"
        "    Alert, AlertPreference, AlertType, AlertSeverity, PushSubscription,"
    )

    # 2. Add schema imports — find existing schema import block
    content = content.replace(
        "    HealthCheck\n)",
        "    HealthCheck,\n"
        "    AlertResponse, AlertSummary, AlertPreferenceResponse,\n"
        "    AlertPreferencesUpdate, SmtpConfigBase, SmtpConfigResponse,\n"
        "    PushSubscriptionCreate, AlertTypeEnum, AlertSeverityEnum\n)"
    )

    with open(filepath, "w") as f:
        f.write(content)

    print("  main.py imports patched")


def patch_main_endpoints():
    """Append alert API endpoints to main.py."""
    filepath = os.path.join(BACKEND_DIR, "main.py")

    with open(filepath, "r") as f:
        content = f.read()

    if "/api/alerts" in content:
        print("  main.py already has alert endpoints — skipping")
        return

    endpoints = '''

# ============== Alerts & Notifications Endpoints (v0.17.0) ==============

@app.get("/api/alerts", response_model=List[AlertResponse], tags=["Alerts"])
async def list_alerts(
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    is_read: Optional[bool] = None,
    limit: int = 25,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List alerts for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    query = db.query(Alert).filter(Alert.user_id == current_user["id"])
    
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


@app.get("/api/alerts/unread-count", tags=["Alerts"])
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


@app.get("/api/alerts/summary", response_model=AlertSummary, tags=["Alerts"])
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


@app.patch("/api/alerts/{alert_id}/read", tags=["Alerts"])
async def mark_alert_read(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark a single alert as read."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"]
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_read = True
    db.commit()
    return {"status": "ok"}


@app.post("/api/alerts/mark-all-read", tags=["Alerts"])
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


@app.patch("/api/alerts/{alert_id}/dismiss", tags=["Alerts"])
async def dismiss_alert(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Dismiss an alert (hide from dashboard widget)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"]
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_dismissed = True
    alert.is_read = True
    db.commit()
    return {"status": "ok"}


# ============== Alert Preferences ==============

@app.get("/api/alert-preferences", response_model=List[AlertPreferenceResponse], tags=["Alerts"])
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
        from alert_dispatcher import seed_alert_preferences
        seed_alert_preferences(db, current_user["id"])
        prefs = db.query(AlertPreference).filter(
            AlertPreference.user_id == current_user["id"]
        ).all()
    
    return prefs


@app.put("/api/alert-preferences", tags=["Alerts"])
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

@app.get("/api/smtp-config", tags=["Alerts"])
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


@app.put("/api/smtp-config", tags=["Alerts"])
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
    
    if config:
        config.value = smtp_data
    else:
        db.add(SystemConfig(key="smtp_config", value=smtp_data))
    
    db.commit()
    return {"status": "ok", "message": "SMTP configuration updated"}


@app.post("/api/alerts/test-email", tags=["Alerts"])
async def send_test_email(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Send a test email to the current user (admin only)."""
    from alert_dispatcher import _get_smtp_config, _deliver_email
    
    smtp = _get_smtp_config(db)
    if not smtp:
        raise HTTPException(status_code=400, detail="SMTP not configured or not enabled")
    if not current_user.get("email"):
        raise HTTPException(status_code=400, detail="Your account has no email address")
    
    _deliver_email(
        db, current_user["id"],
        "Test Alert \\u2014 PrintFarm Scheduler",
        "This is a test notification. If you received this, SMTP is configured correctly.",
        "info"
    )
    return {"status": "ok", "message": f"Test email queued to {current_user['email']}"}


# ============== Browser Push Subscription ==============

@app.get("/api/push/vapid-key", tags=["Alerts"])
async def get_vapid_key():
    """Get VAPID public key for browser push subscription."""
    import os
    key = os.environ.get("VAPID_PUBLIC_KEY")
    if not key:
        raise HTTPException(status_code=404, detail="Browser push not configured (VAPID_PUBLIC_KEY not set)")
    return {"public_key": key}


@app.post("/api/push/subscribe", tags=["Alerts"])
async def subscribe_push(
    data: PushSubscriptionCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Store a browser push subscription for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    existing = db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"],
        PushSubscription.endpoint == data.endpoint
    ).first()
    
    if existing:
        existing.p256dh_key = data.p256dh_key
        existing.auth_key = data.auth_key
    else:
        db.add(PushSubscription(
            user_id=current_user["id"],
            endpoint=data.endpoint,
            p256dh_key=data.p256dh_key,
            auth_key=data.auth_key
        ))
    
    db.commit()
    return {"status": "ok", "message": "Push subscription registered"}


@app.delete("/api/push/subscribe", tags=["Alerts"])
async def unsubscribe_push(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove all push subscriptions for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"]
    ).delete()
    db.commit()
    return {"status": "ok", "message": "Push subscriptions removed"}
'''

    with open(filepath, "a") as f:
        f.write(endpoints)

    print("  main.py patched with alert endpoints")


def create_dispatcher():
    """Create alert_dispatcher.py."""
    filepath = os.path.join(BACKEND_DIR, "alert_dispatcher.py")
    if os.path.exists(filepath):
        print("  alert_dispatcher.py already exists — skipping")
        return

    content = '''"""
Alert Dispatcher for PrintFarm Scheduler (v0.17.0)

Central fan-out module that receives events and delivers alerts
to users via their configured channels (in-app, browser push, email).

Usage:
    from alert_dispatcher import dispatch_alert
    
    dispatch_alert(
        db=db,
        alert_type=AlertType.PRINT_FAILED,
        severity=AlertSeverity.CRITICAL,
        title="Print Failed: Baby Yoda (X1C)",
        message="Job #142 failed on X1C at 67% progress.",
        printer_id=1,
        job_id=142
    )
"""

import json
import logging
import smtplib
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import (
    Alert, AlertPreference, AlertType, AlertSeverity,
    PushSubscription, SystemConfig
)

logger = logging.getLogger("alert_dispatcher")


# ============================================================
# Default preferences for new users
# ============================================================

DEFAULT_PREFERENCES = [
    {"alert_type": AlertType.PRINT_COMPLETE, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.PRINT_FAILED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.SPOOL_LOW, "in_app": True, "browser_push": False, "email": False, "threshold_value": 100.0},
    {"alert_type": AlertType.MAINTENANCE_OVERDUE, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
]


def seed_alert_preferences(db: Session, user_id: int):
    """Create default alert preferences for a new user."""
    for pref in DEFAULT_PREFERENCES:
        existing = db.query(AlertPreference).filter(
            AlertPreference.user_id == user_id,
            AlertPreference.alert_type == pref["alert_type"]
        ).first()
        if not existing:
            db.add(AlertPreference(user_id=user_id, **pref))
    db.commit()


# ============================================================
# Deduplication
# ============================================================

def _should_deduplicate(db, user_id, alert_type, printer_id, spool_id, job_id):
    """
    Check if we should skip creating this alert.
    
    - spool_low: Skip if unread alert exists for same spool
    - maintenance_overdue: Skip if unread alert exists for same printer within 24h
    - print events: Never deduplicate
    """
    if alert_type == AlertType.SPOOL_LOW and spool_id:
        existing = db.query(Alert).filter(
            Alert.user_id == user_id,
            Alert.alert_type == AlertType.SPOOL_LOW,
            Alert.spool_id == spool_id,
            Alert.is_read == False,
            Alert.is_dismissed == False
        ).first()
        return existing is not None
    
    if alert_type == AlertType.MAINTENANCE_OVERDUE and printer_id:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        existing = db.query(Alert).filter(
            Alert.user_id == user_id,
            Alert.alert_type == AlertType.MAINTENANCE_OVERDUE,
            Alert.printer_id == printer_id,
            Alert.is_read == False,
            Alert.created_at > cutoff
        ).first()
        return existing is not None
    
    return False


# ============================================================
# Delivery: In-App
# ============================================================

def _deliver_in_app(db, user_id, alert_type, severity, title, message,
                    printer_id, job_id, spool_id, metadata):
    """Create an alert record in the database."""
    alert = Alert(
        user_id=user_id,
        alert_type=alert_type,
        severity=severity,
        title=title,
        message=message,
        printer_id=printer_id,
        job_id=job_id,
        spool_id=spool_id,
        metadata_json=metadata
    )
    db.add(alert)
    return alert


# ============================================================
# Delivery: Browser Push
# ============================================================

def _deliver_browser_push(db, user_id, title, message, severity):
    """Send browser push notification to all of a user's subscriptions."""
    try:
        from pywebpush import webpush
    except ImportError:
        logger.warning("pywebpush not installed — skipping browser push")
        return
    
    import os
    vapid_private_key = os.environ.get("VAPID_PRIVATE_KEY")
    vapid_email = os.environ.get("VAPID_EMAIL", "mailto:admin@example.com")
    
    if not vapid_private_key:
        logger.warning("VAPID_PRIVATE_KEY not set — skipping browser push")
        return
    
    subscriptions = db.query(PushSubscription).filter(
        PushSubscription.user_id == user_id
    ).all()
    
    payload = json.dumps({
        "title": title,
        "body": message or "",
        "severity": severity,
        "url": "/alerts"
    })
    
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key}
                },
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims={"sub": vapid_email}
            )
        except Exception as e:
            logger.error(f"Push failed for subscription {sub.id}: {e}")
            if "410" in str(e) or "404" in str(e):
                db.delete(sub)
                logger.info(f"Removed expired push subscription {sub.id}")


# ============================================================
# Delivery: SMTP Email
# ============================================================

def _get_smtp_config(db):
    """Get SMTP configuration from system_config."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config or not config.value:
        return None
    smtp = config.value
    if not smtp.get("enabled") or not smtp.get("host"):
        return None
    return smtp


def _deliver_email(db, user_id, title, message, severity):
    """Send email notification in a background thread."""
    smtp_config = _get_smtp_config(db)
    if not smtp_config:
        return
    
    user = db.execute(
        text("SELECT email FROM users WHERE id = :id"),
        {"id": user_id}
    ).fetchone()
    
    if not user or not user.email:
        return
    
    emoji_map = {"critical": "\\U0001f534", "warning": "\\U0001f7e1", "info": "\\U0001f7e2"}
    emoji = emoji_map.get(severity, "")
    
    subject = f"[PrintFarm] {emoji} {title}"
    body = f"""{title}

{message or ''}

---
You're receiving this because you enabled email alerts.
Manage preferences in Settings > Notifications.
"""
    
    user_email = user.email
    
    def send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_config["from_address"]
            msg["To"] = user_email
            msg.attach(MIMEText(body, "plain"))
            
            if smtp_config.get("use_tls", True):
                server = smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 587))
                server.starttls()
            else:
                server = smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 25))
            
            if smtp_config.get("username") and smtp_config.get("password"):
                server.login(smtp_config["username"], smtp_config["password"])
            
            server.send_message(msg)
            server.quit()
            logger.info(f"Email sent to {user_email}: {title}")
        except Exception as e:
            logger.error(f"Failed to send email to {user_email}: {e}")
    
    thread = threading.Thread(target=send, daemon=True)
    thread.start()


# ============================================================
# Main Dispatcher
# ============================================================

def dispatch_alert(
    db: Session,
    alert_type: AlertType,
    severity: AlertSeverity,
    title: str,
    message: str = "",
    printer_id: int = None,
    job_id: int = None,
    spool_id: int = None,
    metadata: dict = None
):
    """
    Fan out an alert to all users based on their preferences.
    
    Handles dedup, creates in-app records, sends push + email.
    """
    preferences = db.query(AlertPreference).filter(
        AlertPreference.alert_type == alert_type
    ).all()
    
    # Auto-seed preferences for existing users if none found
    if not preferences:
        users = db.execute(text("SELECT id FROM users WHERE is_active = 1")).fetchall()
        for user_row in users:
            seed_alert_preferences(db, user_row.id)
        preferences = db.query(AlertPreference).filter(
            AlertPreference.alert_type == alert_type
        ).all()
    
    alerts_created = 0
    
    for pref in preferences:
        if _should_deduplicate(db, pref.user_id, alert_type, printer_id, spool_id, job_id):
            continue
        
        if pref.in_app:
            _deliver_in_app(
                db, pref.user_id, alert_type, severity,
                title, message, printer_id, job_id, spool_id, metadata
            )
            alerts_created += 1
        
        if pref.browser_push:
            _deliver_browser_push(db, pref.user_id, title, message, severity.value)
        
        if pref.email:
            _deliver_email(db, pref.user_id, title, message, severity.value)
    
    if alerts_created > 0:
        db.commit()
    
    logger.info(f"Dispatched {alert_type.value} alert to {alerts_created} users: {title}")
    return alerts_created
'''

    with open(filepath, "w") as f:
        f.write(content)

    print("  alert_dispatcher.py created")


def main():
    print("=" * 60)
    print("v0.17.0 Phase 1 — Alerts Foundation")
    print("=" * 60)
    print()

    print("[1/4] Patching models.py...")
    patch_models()

    print("[2/4] Patching schemas.py...")
    patch_schemas()

    print("[3/4] Patching main.py imports...")
    patch_main_imports()

    print("[4/4] Patching main.py endpoints...")
    patch_main_endpoints()

    print("[5/5] Creating alert_dispatcher.py...")
    create_dispatcher()

    print()
    print("=" * 60)
    print("Done! Next steps:")
    print("  1. Restart backend: systemctl restart printfarm-backend")
    print("  2. Tables auto-create on startup (Base.metadata.create_all)")
    print("  3. Test: curl -s -H 'X-API-Key: ...' http://localhost:8000/api/alerts")
    print("  4. Test: curl -s -H 'X-API-Key: ...' http://localhost:8000/api/alert-preferences")
    print("=" * 60)


if __name__ == "__main__":
    main()
