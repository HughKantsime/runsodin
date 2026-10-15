"""System config routes — IP allowlist, retention, quiet hours, MQTT republish, metrics, HMS lookup."""

import json
import time
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import JSON, bindparam, text
from sqlalchemy.orm import Session
from starlette.responses import Response

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role, require_superadmin, get_org_scope, should_filter_org
import core.crypto as crypto

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== IP Allowlist ==============

@router.get("/config/ip-allowlist", tags=["Config"])
async def get_ip_allowlist(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Get the IP allowlist configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'ip_allowlist'")).fetchone()
    if not row:
        return {"enabled": False, "cidrs": [], "mode": "api_and_ui"}
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return val


class IPAllowlistRequest(PydanticBaseModel):
    enabled: bool = False
    cidrs: list[str] = []
    mode: str = "api_and_ui"


@router.put("/config/ip-allowlist", tags=["Config"])
async def set_ip_allowlist(request: Request, body: IPAllowlistRequest, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Set the IP allowlist. Includes lock-out protection."""
    import ipaddress
    enabled = body.enabled
    cidrs = list(body.cidrs)
    mode = body.mode

    for cidr in cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid CIDR: {cidr}")

    if enabled and cidrs:
        try:
            if request.client is None:
                raise ValueError("Missing client identity")
            client_address = ipaddress.ip_address(request.client.host)
            networks = [ipaddress.ip_network(c, strict=False) for c in cidrs]
            # CIDR membership ignores IPv6 interface zones, so a scoped identity
            # cannot safely grant the current administrator a host exception.
            if getattr(client_address, "scope_id", None) is not None or any(
                "%" in cidr for cidr in cidrs
            ):
                raise ValueError("Unsupported scoped identity")
        except ValueError:
            raise HTTPException(status_code=400, detail="Cannot enforce allowlist for this client identity")
        client_in_list = any(client_address in network for network in networks)
        if not client_in_list:
            cidrs.append(f"{client_address}/{client_address.max_prefixlen}")

    config = {"enabled": enabled, "cidrs": cidrs, "mode": mode}
    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('ip_allowlist', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})

    log_audit(db, "ip_allowlist_updated", details=f"Enabled={enabled}, {len(cidrs)} CIDRs")
    db.commit()
    return config


# ============== Retention Config ==============

RETENTION_DEFAULTS = {
    "completed_jobs_days": 0,
    "audit_logs_days": 365,
    "timelapses_days": 30,
    "alert_history_days": 90,
}


@router.get("/config/retention", tags=["Config"])
async def get_retention_config(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Get data retention policy configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    if not row:
        return RETENTION_DEFAULTS
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return {**RETENTION_DEFAULTS, **val}


class RetentionConfigRequest(PydanticBaseModel):
    completed_jobs_days: int | None = None
    audit_logs_days: int | None = None
    timelapses_days: int | None = None
    alert_history_days: int | None = None


@router.put("/config/retention", tags=["Config"])
async def set_retention_config(body: RetentionConfigRequest, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Set data retention policy configuration."""
    body_data = body.model_dump(exclude_unset=True)
    config = {}
    for key in RETENTION_DEFAULTS:
        if key in body_data:
            if body_data[key] is None:
                raise HTTPException(status_code=400, detail=f"{key} must be a nonnegative integer")
            val = int(body_data[key])
            if val < 0:
                raise HTTPException(status_code=400, detail=f"{key} must be >= 0")
            config[key] = val

    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('data_retention', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})

    log_audit(db, "retention_updated", details=f"Retention config: {config}")
    db.commit()
    return {**RETENTION_DEFAULTS, **config}


@router.post("/admin/retention/cleanup", tags=["Config"])
async def run_retention_cleanup(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Manually trigger data retention cleanup."""
    try:
        row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
        config = {**RETENTION_DEFAULTS}
        if row:
            val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
            config.update(val)

        deleted = {}
        now = datetime.now(timezone.utc)
        sqlite = db.get_bind().dialect.name == "sqlite"

        def expired(column):
            # SQLite stores timestamps as text with multiple valid UTC/offset
            # representations. Compare instants, not their lexical spelling.
            return f"julianday({column}) < julianday(:cutoff)" if sqlite else f"{column} < :cutoff"

        if config["completed_jobs_days"] > 0:
            cutoff = now - timedelta(days=config["completed_jobs_days"])
            if sqlite:
                # Own SQLite's writer transaction before enumerating candidates.
                if db.execute(text("UPDATE system_config SET value=value WHERE key='data_retention'")).rowcount != 1:
                    raise RuntimeError("Retention configuration unavailable")
            else:
                if db.connection().get_isolation_level() != "READ COMMITTED":
                    raise RuntimeError("Unsupported cleanup isolation")
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
            upper = db.execute(text("SELECT MAX(id) FROM jobs")).scalar_one()
            last = 0
            count = 0
            while upper is not None and last < upper:
                candidates = list(db.execute(text(
                    "SELECT id FROM jobs WHERE id>:last AND id<=:upper "
                    "AND status IN ('completed','failed','cancelled') AND " + expired("updated_at") +
                    " ORDER BY id LIMIT 256" + ("" if sqlite else " FOR UPDATE")
                ), {"last": last, "upper": upper, "cutoff": cutoff}).scalars())
                if not candidates:
                    break
                last = candidates[-1]
                # A separate statement observes authority committed while the
                # candidate lock waited. Never add unlocked/new candidates.
                statement = text("""DELETE FROM jobs WHERE id IN :candidate_ids
                    AND status IN ('completed','failed','cancelled')
                    AND order_item_id IS NULL
                    AND NOT EXISTS (SELECT 1 FROM order_production_jobs p WHERE p.job_id=jobs.id)
                    AND NOT EXISTS (SELECT 1 FROM education_submissions e WHERE e.job_id=jobs.id)
                    AND """ + expired("updated_at")).bindparams(bindparam("candidate_ids", expanding=True))
                removed = db.execute(statement, {"candidate_ids": candidates, "cutoff": cutoff}).rowcount
                if removed < 0:
                    raise RuntimeError("Unconfirmed cleanup count")
                count += removed
            deleted["completed_jobs"] = count

        if config["audit_logs_days"] > 0:
            cutoff = now - timedelta(days=config["audit_logs_days"])
            r = db.execute(text("DELETE FROM audit_logs WHERE " + expired("timestamp")), {"cutoff": cutoff})
            deleted["audit_logs"] = r.rowcount

        if config["alert_history_days"] > 0:
            cutoff = now - timedelta(days=config["alert_history_days"])
            r = db.execute(text("DELETE FROM alerts WHERE " + expired("created_at")), {"cutoff": cutoff})
            deleted["alerts"] = r.rowcount

        refused = {}
        if config["timelapses_days"] > 0:
            refused["timelapses"] = {
                "status": "refused",
                "reason": "lifetime_retirement_pending",
            }

        db.execute(text("DELETE FROM token_blacklist WHERE expires_at < :now"), {"now": now})
        stale = now - timedelta(hours=48)
        db.execute(text("DELETE FROM active_sessions WHERE last_seen_at < :cutoff"), {"cutoff": stale})
        expired_oauth = db.execute(
            text("DELETE FROM classroom_oauth_states WHERE expires_at < :now"),
            {"now": now.isoformat()},
        )
        deleted["classroom_oauth_states"] = expired_oauth.rowcount

        db.commit()
        return {"status": "partial" if refused else "ok", "deleted": deleted, "refused": refused}
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Retention cleanup could not be confirmed") from None



# ============== Quiet Hours Config ==============

@router.get("/config/quiet-hours")
async def get_quiet_hours_config(db: Session = Depends(get_db), current_user: dict = Depends(require_superadmin())):
    """Get quiet hours settings."""
    keys = ["quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end", "quiet_hours_digest"]
    config = {}
    defaults = {"enabled": False, "start": "22:00", "end": "07:00", "digest": True}
    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("quiet_hours_", "")
        if row:
            from modules.notifications.quiet_hours import config_text
            val = config_text(row[0])
            if short_key in ("enabled", "digest"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            else:
                config[short_key] = val
        else:
            config[short_key] = defaults.get(short_key, "")
    return config


@router.put("/config/quiet-hours")
async def update_quiet_hours_config(request: Request, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Update quiet hours settings. Admin only."""
    body = await request.json()
    for short_key, value in body.items():
        db_key = f"quiet_hours_{short_key}"
        str_val = str(value).lower() if isinstance(value, bool) else str(value)
        # system_config.value is a JSON column: bind as JSON so PostgreSQL
        # accepts text such as "22:00" (stored as a JSON string on both dialects).
        json_value = bindparam("v", type_=JSON)
        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k").bindparams(json_value), {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)").bindparams(json_value), {"k": db_key, "v": str_val})
    db.commit()
    try:
        from modules.notifications.quiet_hours import invalidate_cache
        invalidate_cache()
    except Exception as e:
        log.debug(f"Failed to invalidate quiet hours cache: {e}")
    return {"status": "ok"}


# ============== MQTT Republish Configuration ==============

try:
    import modules.notifications.mqtt_republish as mqtt_republish
except ImportError:
    mqtt_republish = None


@router.get("/config/mqtt-republish")
async def get_mqtt_republish_config(db: Session = Depends(get_db), current_user: dict = Depends(require_superadmin(scope="admin"))):
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
                except Exception as e:
                    log.debug(f"Failed to decrypt MQTT password (using raw): {e}")
                config[short_key] = "••••••••" if raw else ""
            else:
                config[short_key] = val
        else:
            defaults = {"enabled": False, "host": "", "port": 1883, "username": "",
                        "password": "", "topic_prefix": "odin", "use_tls": False}
            config[short_key] = defaults.get(short_key, "")
    return config


@router.put("/config/mqtt-republish")
async def update_mqtt_republish_config(request: Request, current_user: dict = Depends(require_superadmin(scope="admin")), db: Session = Depends(get_db)):
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
async def test_mqtt_republish(request: Request, current_user: dict = Depends(require_superadmin(scope="admin"))):
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

_PROCESS_STARTED = time.monotonic()


def _odin_version() -> str:
    """Same source as the API's advertised version (core/app.py): the repo VERSION file."""
    from pathlib import Path
    try:
        return (Path(__file__).resolve().parents[3] / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


@router.get("/metrics", tags=["Monitoring"])
async def prometheus_metrics(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:analytics"))):
    """Tenant-scoped metrics; scoped alerts count the current recipient only."""
    lines = []
    org = get_org_scope(current_user)
    scoped = should_filter_org(current_user, org)
    params = {"org": org, "user_id": current_user.get("id")}

    def tenant_clause(column, *, shared=False):
        if not scoped:
            return ""
        shared_clause = " OR shared IS TRUE" if shared else ""
        return f" AND ({column} = :org OR {column} IS NULL{shared_clause})"

    def label_value(value):
        return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    printers_all = db.execute(text(
        "SELECT * FROM printers WHERE is_active IS TRUE" + tenant_clause("org_id", shared=True)
    ), params).fetchall()
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
                is_online = 0 <= (now - ls).total_seconds() < 90
            except Exception as e:
                log.debug(f"Failed to parse printer last_seen: {e}")

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

    # Live progress lives on the printer's running print_jobs row (monitor-updated
    # progress_percent); printers has no progress column.
    running_progress = {}
    for row in db.execute(text(
        "SELECT printer_id, progress_percent FROM print_jobs WHERE status = 'running' "
        "AND progress_percent IS NOT NULL ORDER BY started_at, id"
    )).fetchall():
        running_progress[row.printer_id] = row.progress_percent

    for p in printers_all:
        pm = dict(p._mapping)
        name = pm.get("nickname") or pm.get("name", f"printer_{pm['id']}")
        pid = pm["id"]
        labels = f'printer="{label_value(name)}",printer_id="{pid}"'
        nozzle = pm.get("nozzle_temp")
        bed = pm.get("bed_temp")
        progress = running_progress.get(pm["id"])
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

    job_counts = db.execute(text("SELECT status, COUNT(*) as cnt FROM jobs WHERE 1=1"
                                 + tenant_clause("charged_to_org_id") + " GROUP BY status"), params).fetchall()
    lines.append("# HELP odin_jobs_by_status Number of jobs by status")
    lines.append("# TYPE odin_jobs_by_status gauge")
    for row in job_counts:
        r = dict(row._mapping)
        lines.append(f'odin_jobs_by_status{{status="{label_value(r["status"])}"}} {r["cnt"]}')

    queue = db.execute(text("SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('pending', 'scheduled', 'submitted')"
                            + tenant_clause("charged_to_org_id")), params).fetchone()
    lines.append("# HELP odin_queue_depth Jobs waiting to print")
    lines.append("# TYPE odin_queue_depth gauge")
    lines.append(f"odin_queue_depth {dict(queue._mapping)['cnt']}")

    spool_data = db.execute(text("""
        SELECT COUNT(*) as total, SUM(CASE WHEN remaining_weight_g < 100 THEN 1 ELSE 0 END) as low
        FROM spools WHERE remaining_weight_g IS NOT NULL
    """ + tenant_clause("org_id")), params).fetchone()
    sd = dict(spool_data._mapping)
    lines.append("# HELP odin_spools_total Total tracked spools")
    lines.append("# TYPE odin_spools_total gauge")
    lines.append(f"odin_spools_total {sd['total'] or 0}")
    lines.append("# HELP odin_spools_low Spools under 100g remaining")
    lines.append("# TYPE odin_spools_low gauge")
    lines.append(f"odin_spools_low {sd['low'] or 0}")

    order_data = db.execute(text("SELECT status, COUNT(*) as cnt FROM orders WHERE 1=1"
                                 + tenant_clause("org_id") + " GROUP BY status"), params).fetchall()
    lines.append("# HELP odin_orders_by_status Orders by status")
    lines.append("# TYPE odin_orders_by_status gauge")
    for row in order_data:
        r = dict(row._mapping)
        lines.append(f'odin_orders_by_status{{status="{label_value(r["status"])}"}} {r["cnt"]}')

    alert_clause = " AND user_id = :user_id" if scoped else ""
    unread = db.execute(text("SELECT COUNT(*) as cnt FROM alerts WHERE is_read IS FALSE"
                            + alert_clause), params).fetchone()
    lines.append("# HELP odin_alerts_unread Unread alerts")
    lines.append("# TYPE odin_alerts_unread gauge")
    lines.append(f"odin_alerts_unread {dict(unread._mapping)['cnt']}")

    # System health: this scrape answered from a live database query, the
    # running build, and process uptime.
    lines.append("# HELP odin_database_up Database answered this scrape's queries (1 = up)")
    lines.append("# TYPE odin_database_up gauge")
    lines.append(f"odin_database_up {1 if db.execute(text('SELECT 1')).scalar() == 1 else 0}")
    lines.append("# HELP odin_build_info Running O.D.I.N. build")
    lines.append("# TYPE odin_build_info gauge")
    lines.append(f'odin_build_info{{version="{label_value(_odin_version())}"}} 1')
    lines.append("# HELP odin_process_uptime_seconds Seconds since this API process started")
    lines.append("# TYPE odin_process_uptime_seconds gauge")
    lines.append(f"odin_process_uptime_seconds {round(time.monotonic() - _PROCESS_STARTED, 3)}")

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
