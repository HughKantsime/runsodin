"""System config routes — IP allowlist, retention, quiet hours, MQTT republish, metrics, HMS lookup."""

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role
import core.crypto as crypto

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== IP Allowlist ==============

@router.get("/config/ip-allowlist", tags=["Config"])
async def get_ip_allowlist(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get the IP allowlist configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'ip_allowlist'")).fetchone()
    if not row:
        return {"enabled": False, "cidrs": [], "mode": "api_and_ui"}
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return val


@router.put("/config/ip-allowlist", tags=["Config"])
async def set_ip_allowlist(request: Request, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set the IP allowlist. Includes lock-out protection."""
    import ipaddress
    enabled = body.get("enabled", False)
    cidrs = body.get("cidrs", [])
    mode = body.get("mode", "api_and_ui")

    for cidr in cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid CIDR: {cidr}")

    client_ip = request.client.host if request.client else "127.0.0.1"
    if enabled and cidrs:
        client_in_list = any(
            ipaddress.ip_address(client_ip) in ipaddress.ip_network(c, strict=False) for c in cidrs
        )
        if not client_in_list:
            cidrs.append(client_ip + "/32")

    config = {"enabled": enabled, "cidrs": cidrs, "mode": mode}
    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('ip_allowlist', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})
    db.commit()

    log_audit(db, "ip_allowlist_updated", details=f"Enabled={enabled}, {len(cidrs)} CIDRs")
    return config


# ============== Retention Config ==============

RETENTION_DEFAULTS = {
    "completed_jobs_days": 0,
    "audit_logs_days": 365,
    "timelapses_days": 30,
    "alert_history_days": 90,
}


@router.get("/config/retention", tags=["Config"])
async def get_retention_config(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get data retention policy configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    if not row:
        return RETENTION_DEFAULTS
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return {**RETENTION_DEFAULTS, **val}


@router.put("/config/retention", tags=["Config"])
async def set_retention_config(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set data retention policy configuration."""
    config = {}
    for key in RETENTION_DEFAULTS:
        if key in body:
            val = int(body[key])
            if val < 0:
                raise HTTPException(status_code=400, detail=f"{key} must be >= 0")
            config[key] = val

    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('data_retention', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})
    db.commit()

    log_audit(db, "retention_updated", details=f"Retention config: {config}")
    return {**RETENTION_DEFAULTS, **config}


@router.post("/admin/retention/cleanup", tags=["Config"])
async def run_retention_cleanup(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Manually trigger data retention cleanup."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    config = {**RETENTION_DEFAULTS}
    if row:
        val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
        config.update(val)

    deleted = {}
    now = datetime.now(timezone.utc)

    if config["completed_jobs_days"] > 0:
        cutoff = now - timedelta(days=config["completed_jobs_days"])
        r = db.execute(text("DELETE FROM jobs WHERE status IN ('completed','failed','cancelled') AND updated_at < :cutoff"), {"cutoff": cutoff})
        deleted["completed_jobs"] = r.rowcount

    if config["audit_logs_days"] > 0:
        cutoff = now - timedelta(days=config["audit_logs_days"])
        r = db.execute(text("DELETE FROM audit_logs WHERE timestamp < :cutoff"), {"cutoff": cutoff})
        deleted["audit_logs"] = r.rowcount

    if config["alert_history_days"] > 0:
        cutoff = now - timedelta(days=config["alert_history_days"])
        r = db.execute(text("DELETE FROM alerts WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["alerts"] = r.rowcount

    if config["timelapses_days"] > 0:
        cutoff = now - timedelta(days=config["timelapses_days"])
        r = db.execute(text("DELETE FROM timelapses WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["timelapses"] = r.rowcount

    db.execute(text("DELETE FROM token_blacklist WHERE expires_at < :now"), {"now": now})
    stale = now - timedelta(hours=48)
    db.execute(text("DELETE FROM active_sessions WHERE last_seen_at < :cutoff"), {"cutoff": stale})

    db.commit()
    return {"status": "ok", "deleted": deleted}


# ============== Quiet Hours Config ==============

@router.get("/config/quiet-hours")
async def get_quiet_hours_config(db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
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


@router.put("/config/quiet-hours")
async def update_quiet_hours_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update quiet hours settings. Admin only."""
    body = await request.json()
    for short_key, value in body.items():
        db_key = f"quiet_hours_{short_key}"
        str_val = str(value).lower() if isinstance(value, bool) else str(value)
        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"), {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"), {"k": db_key, "v": str_val})
    db.commit()
    try:
        from modules.notifications.quiet_hours import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
    return {"status": "ok"}


# ============== MQTT Republish Configuration ==============

try:
    import modules.notifications.mqtt_republish as mqtt_republish
except ImportError:
    mqtt_republish = None


@router.get("/config/mqtt-republish")
async def get_mqtt_republish_config(db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Get MQTT republish settings."""
    keys = [
        "mqtt_republish_enabled", "mqtt_republish_host", "mqtt_republish_port",
        "mqtt_republish_username", "mqtt_republish_password",
        "mqtt_republish_topic_prefix", "mqtt_republish_use_tls",
    ]
    config = {}
    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("mqtt_republish_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "use_tls"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            elif short_key == "port":
                config[short_key] = int(val) if val else 1883
            elif short_key == "password":
                raw = val
                try:
                    raw = crypto.decrypt(val)
                except Exception:
                    pass
                config[short_key] = "••••••••" if raw else ""
            else:
                config[short_key] = val
        else:
            defaults = {"enabled": False, "host": "", "port": 1883, "username": "",
                        "password": "", "topic_prefix": "odin", "use_tls": False}
            config[short_key] = defaults.get(short_key, "")
    return config


@router.put("/config/mqtt-republish")
async def update_mqtt_republish_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update MQTT republish settings. Admin only."""
    body = await request.json()
    for short_key, value in body.items():
        db_key = f"mqtt_republish_{short_key}"
        if short_key == "password" and value == "••••••••":
            continue
        if short_key == "password" and value:
            str_val = crypto.encrypt(str(value))
        else:
            str_val = str(value).lower() if isinstance(value, bool) else str(value)
        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"), {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"), {"k": db_key, "v": str_val})
    db.commit()
    if mqtt_republish:
        mqtt_republish.invalidate_cache()
    return {"status": "ok"}


@router.post("/config/mqtt-republish/test")
async def test_mqtt_republish(request: Request, current_user: dict = Depends(require_role("admin"))):
    """Test connection to external MQTT broker."""
    if not mqtt_republish:
        raise HTTPException(status_code=503, detail="MQTT republish module not available")
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = mqtt_republish.test_connection(
        host=body.get("host", ""), port=int(body.get("port", 1883)),
        username=body.get("username", ""), password=body.get("password", ""),
        use_tls=body.get("use_tls", False), topic_prefix=body.get("topic_prefix", "odin"),
    )
    return result


# ============== Prometheus Metrics ==============

@router.get("/metrics", tags=["Monitoring"])
async def prometheus_metrics(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Prometheus-compatible metrics endpoint. Requires viewer role or API key."""
    lines = []

    printers_all = db.execute(text("SELECT * FROM printers WHERE is_active = 1")).fetchall()
    total_printers = len(printers_all)
    online_count = 0
    printing_count = 0
    idle_count = 0
    error_count = 0

    now = datetime.now(timezone.utc)

    for p in printers_all:
        pm = dict(p._mapping)
        last_seen = pm.get("last_seen")
        is_online = False
        if last_seen:
            try:
                ls = datetime.fromisoformat(str(last_seen).replace("Z", ""))
                if ls.tzinfo is None:
                    ls = ls.replace(tzinfo=timezone.utc)
                is_online = (now - ls).total_seconds() < 90
            except Exception:
                pass

        if is_online:
            online_count += 1
            gcode_state = pm.get("gcode_state", "")
            if gcode_state in ("RUNNING", "PREPARE"):
                printing_count += 1
            elif gcode_state in ("FAILED", "UNKNOWN"):
                error_count += 1
            else:
                idle_count += 1

    lines.append("# HELP odin_printers_total Total registered printers")
    lines.append("# TYPE odin_printers_total gauge")
    lines.append(f"odin_printers_total {total_printers}")
    lines.append("# HELP odin_printers_online Online printers (seen in last 90s)")
    lines.append("# TYPE odin_printers_online gauge")
    lines.append(f"odin_printers_online {online_count}")
    lines.append("# HELP odin_printers_printing Currently printing")
    lines.append("# TYPE odin_printers_printing gauge")
    lines.append(f"odin_printers_printing {printing_count}")
    lines.append("# HELP odin_printers_idle Online but idle")
    lines.append("# TYPE odin_printers_idle gauge")
    lines.append(f"odin_printers_idle {idle_count}")
    lines.append("# HELP odin_printers_error Online with errors")
    lines.append("# TYPE odin_printers_error gauge")
    lines.append(f"odin_printers_error {error_count}")

    lines.append("# HELP odin_printer_nozzle_temp_celsius Current nozzle temperature")
    lines.append("# TYPE odin_printer_nozzle_temp_celsius gauge")
    lines.append("# HELP odin_printer_bed_temp_celsius Current bed temperature")
    lines.append("# TYPE odin_printer_bed_temp_celsius gauge")
    lines.append("# HELP odin_printer_progress Print progress 0-100")
    lines.append("# TYPE odin_printer_progress gauge")
    lines.append("# HELP odin_printer_print_hours_total Lifetime print hours")
    lines.append("# TYPE odin_printer_print_hours_total counter")
    lines.append("# HELP odin_printer_print_count_total Lifetime print count")
    lines.append("# TYPE odin_printer_print_count_total counter")

    for p in printers_all:
        pm = dict(p._mapping)
        name = pm.get("nickname") or pm.get("name", f"printer_{pm['id']}")
        pid = pm["id"]
        labels = f'printer="{name}",printer_id="{pid}"'
        nozzle = pm.get("nozzle_temp")
        bed = pm.get("bed_temp")
        progress = pm.get("print_progress")
        total_hours = pm.get("total_print_hours", 0) or 0
        total_prints = pm.get("total_print_count", 0) or 0
        if nozzle is not None:
            lines.append(f"odin_printer_nozzle_temp_celsius{{{labels}}} {nozzle}")
        if bed is not None:
            lines.append(f"odin_printer_bed_temp_celsius{{{labels}}} {bed}")
        if progress is not None:
            lines.append(f"odin_printer_progress{{{labels}}} {progress}")
        lines.append(f"odin_printer_print_hours_total{{{labels}}} {total_hours}")
        lines.append(f"odin_printer_print_count_total{{{labels}}} {total_prints}")

    job_counts = db.execute(text("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status")).fetchall()
    lines.append("# HELP odin_jobs_by_status Number of jobs by status")
    lines.append("# TYPE odin_jobs_by_status gauge")
    for row in job_counts:
        r = dict(row._mapping)
        lines.append(f'odin_jobs_by_status{{status="{r["status"]}"}} {r["cnt"]}')

    queue = db.execute(text("SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('pending', 'scheduled', 'submitted')")).fetchone()
    lines.append("# HELP odin_queue_depth Jobs waiting to print")
    lines.append("# TYPE odin_queue_depth gauge")
    lines.append(f"odin_queue_depth {dict(queue._mapping)['cnt']}")

    spool_data = db.execute(text("""
        SELECT COUNT(*) as total, SUM(CASE WHEN remaining_weight_g < 100 THEN 1 ELSE 0 END) as low
        FROM spools WHERE remaining_weight_g IS NOT NULL
    """)).fetchone()
    sd = dict(spool_data._mapping)
    lines.append("# HELP odin_spools_total Total tracked spools")
    lines.append("# TYPE odin_spools_total gauge")
    lines.append(f"odin_spools_total {sd['total'] or 0}")
    lines.append("# HELP odin_spools_low Spools under 100g remaining")
    lines.append("# TYPE odin_spools_low gauge")
    lines.append(f"odin_spools_low {sd['low'] or 0}")

    order_data = db.execute(text("SELECT status, COUNT(*) as cnt FROM orders GROUP BY status")).fetchall()
    lines.append("# HELP odin_orders_by_status Orders by status")
    lines.append("# TYPE odin_orders_by_status gauge")
    for row in order_data:
        r = dict(row._mapping)
        lines.append(f'odin_orders_by_status{{status="{r["status"]}"}} {r["cnt"]}')

    unread = db.execute(text("SELECT COUNT(*) as cnt FROM alerts WHERE is_read = 0")).fetchone()
    lines.append("# HELP odin_alerts_unread Unread alerts")
    lines.append("# TYPE odin_alerts_unread gauge")
    lines.append(f"odin_alerts_unread {dict(unread._mapping)['cnt']}")

    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


# ============== HMS Code Lookup ==============

@router.get("/hms-codes/{code}", tags=["Monitoring"])
async def lookup_hms(code: str, current_user: dict = Depends(require_role("viewer"))):
    """Look up human-readable description for a Bambu HMS error code."""
    try:
        from modules.printers.hms_codes import lookup_hms_code, get_code_count
        return {"code": code, "message": lookup_hms_code(code), "total_codes": get_code_count()}
    except Exception as e:
        log.error(f"HMS code lookup failed: {e}")
        raise HTTPException(500, "Internal server error")
