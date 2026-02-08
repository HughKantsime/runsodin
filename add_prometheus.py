#!/usr/bin/env python3
"""
Add Prometheus /metrics endpoint to O.D.I.N.
Run on server: python3 add_prometheus.py

Exposes printer telemetry, job counts, queue depth, fleet uptime
in Prometheus format at GET /metrics (no auth required).
"""

import re

MAIN_PY = "/opt/printfarm-scheduler/backend/main.py"

with open(MAIN_PY, "r") as f:
    content = f.read()

# ============================================================
# Add the /metrics endpoint near the end (before cameras endpoint)
# ============================================================

METRICS_ENDPOINT = '''

# ============================================================
# Prometheus Metrics (v0.18.0)
# ============================================================

@app.get("/metrics", tags=["Monitoring"])
async def prometheus_metrics(db: Session = Depends(get_db)):
    """Prometheus-compatible metrics endpoint. No auth required."""
    lines = []
    
    # --- Fleet metrics ---
    printers_all = db.execute(text("SELECT * FROM printers WHERE is_active = 1")).fetchall()
    total_printers = len(printers_all)
    online_count = 0
    printing_count = 0
    idle_count = 0
    error_count = 0
    
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    
    for p in printers_all:
        pm = dict(p._mapping)
        last_seen = pm.get("last_seen")
        is_online = False
        if last_seen:
            try:
                ls = datetime.fromisoformat(str(last_seen).replace("Z", ""))
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
    
    # --- Per-printer telemetry ---
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
    
    # --- Job metrics ---
    job_counts = db.execute(text("""
        SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status
    """)).fetchall()
    
    lines.append("# HELP odin_jobs_by_status Number of jobs by status")
    lines.append("# TYPE odin_jobs_by_status gauge")
    for row in job_counts:
        r = dict(row._mapping)
        lines.append(f'odin_jobs_by_status{{status="{r["status"]}"}} {r["cnt"]}')
    
    # Queue depth (pending + scheduled)
    queue = db.execute(text("""
        SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('pending', 'scheduled', 'submitted')
    """)).fetchone()
    lines.append("# HELP odin_queue_depth Jobs waiting to print")
    lines.append("# TYPE odin_queue_depth gauge")
    lines.append(f"odin_queue_depth {dict(queue._mapping)['cnt']}")
    
    # --- Spool metrics ---
    spool_data = db.execute(text("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN remaining_weight < 100 THEN 1 ELSE 0 END) as low
        FROM spools WHERE remaining_weight IS NOT NULL
    """)).fetchone()
    sd = dict(spool_data._mapping)
    
    lines.append("# HELP odin_spools_total Total tracked spools")
    lines.append("# TYPE odin_spools_total gauge")
    lines.append(f"odin_spools_total {sd['total'] or 0}")
    
    lines.append("# HELP odin_spools_low Spools under 100g remaining")
    lines.append("# TYPE odin_spools_low gauge")
    lines.append(f"odin_spools_low {sd['low'] or 0}")
    
    # --- Order metrics ---
    order_data = db.execute(text("""
        SELECT status, COUNT(*) as cnt FROM orders GROUP BY status
    """)).fetchall()
    
    lines.append("# HELP odin_orders_by_status Orders by status")
    lines.append("# TYPE odin_orders_by_status gauge")
    for row in order_data:
        r = dict(row._mapping)
        lines.append(f'odin_orders_by_status{{status="{r["status"]}"}} {r["cnt"]}')
    
    # --- Alert metrics ---
    unread = db.execute(text("SELECT COUNT(*) as cnt FROM alerts WHERE is_read = 0")).fetchone()
    lines.append("# HELP odin_alerts_unread Unread alerts")
    lines.append("# TYPE odin_alerts_unread gauge")
    lines.append(f"odin_alerts_unread {dict(unread._mapping)['cnt']}")
    
    from starlette.responses import Response
    return Response(
        content="\\n".join(lines) + "\\n",
        media_type="text/plain; version=0.0.4; charset=utf-8"
    )

'''

# Insert before the cameras endpoint
INSERT_BEFORE = '@app.get("/api/cameras", tags=["Cameras"])'

if "/metrics" not in content:
    if INSERT_BEFORE in content:
        content = content.replace(INSERT_BEFORE, METRICS_ENDPOINT + INSERT_BEFORE)
        with open(MAIN_PY, "w") as f:
            f.write(content)
        print("✅ Added Prometheus /metrics endpoint")
    else:
        # Fallback: append to end
        with open(MAIN_PY, "a") as f:
            f.write(METRICS_ENDPOINT)
        print("✅ Added Prometheus /metrics endpoint (appended)")
else:
    print("· /metrics endpoint already exists")
