"""Cert-E analytics (F10.1-F10.6): known-value HTTP behavior and producer wire fixtures.

Real FastAPI routers on a bootstrapped SQLite schema. Only the wall clock is fixed
(2026-10-13T12:00:00Z) so the captured producer fixtures are deterministic; the
frontend *.cert.test.tsx files consume these exact responses.
"""
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[2] / "frontend/src/test-fixtures"
GRAMS_100 = json.dumps({"slot1": {"color": "black", "grams": 100}})
FIXED_NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _wire(name, payload):
    """Pattern of test_chargeback_wire_fixture: write once, then pin the producer contract."""
    path = FIXTURES / f"cert-E-ana-{name}.json"
    if path.exists():
        assert json.loads(path.read_text()) == payload, f"Producer contract changed for {path.name}; reconcile the consumer explicitly"
    else:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def ana(bom_client, review_db, monkeypatch):
    from core.dependencies import get_current_user
    from modules.reporting.routes import analytics, exports
    from modules.jobs.routes import presets, jobs_lifecycle
    from modules.models_library.routes import pricing, models_crud
    from modules.printers import routes_smart_plug
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    monkeypatch.setattr(analytics, "datetime", _FrozenDatetime)
    app = bom_client.app
    for router in (analytics.router, exports.router, presets.router, jobs_lifecycle.router,
                   pricing.router, models_crud.router, routes_smart_plug.router):
        app.include_router(router)
    identity = {"id": 3, "role": "operator", "group_id": 1, "is_active": True}
    app.dependency_overrides[get_current_user] = lambda: identity
    db = review_db
    # Deterministic baseline: the review fixture's submitted job is outside every window.
    db.execute(text("UPDATE jobs SET created_at='2026-09-01 00:00:00' WHERE id=13"))
    db.execute(text("UPDATE models SET build_time_hours=2, markup_percent=200, units_per_bed=1 WHERE id=101"))
    db.execute(text("UPDATE models SET build_time_hours=1, units_per_bed=1 WHERE id=103"))
    rows = [
        # id, status, printer, model, qty, price, cost, hours, created, end, fail_reason
        (7001, "completed", 9, 101, 2, 10, 4, 3, "2026-10-11 09:00:00", "2026-10-11 12:00:00", None),
        (7002, "completed", 9, 101, 1, 6, 2, 1, "2026-10-12 09:00:00", "2026-10-12 10:00:00", None),
        (7003, "completed", 10, 103, 1, 5, 1, 2, "2026-10-12 11:00:00", "2026-10-12 13:00:00", None),
        (7004, "pending", None, 101, 1, 7, 3, None, "2026-10-12 12:00:00", None, None),
        (7005, "failed", 10, 103, 1, 5, 1, None, "2026-10-12 13:00:00", "2026-10-12 14:00:00", "spaghetti"),
    ]
    for r in rows:
        db.execute(text(
            "INSERT INTO jobs(id,item_name,status,printer_id,model_id,quantity,suggested_price,estimated_cost,"
            "duration_hours,created_at,actual_end,fail_reason,charged_to_org_id,hold) "
            "VALUES(:id,:name,:status,:printer,:model,:qty,:price,:cost,:hours,:created,:end,:reason,1,0)"),
            dict(id=r[0], name=f"Cert job {r[0]}", status=r[1], printer=r[2], model=r[3], qty=r[4], price=r[5],
                 cost=r[6], hours=r[7], created=r[8], end=r[9], reason=r[10]))
    # A foreign-tenant completed job must never reach the org-1 dashboard.
    db.execute(text("INSERT INTO jobs(id,item_name,status,model_id,quantity,suggested_price,estimated_cost,duration_hours,"
                    "created_at,actual_end,charged_to_org_id,hold) VALUES(7099,'Foreign','completed',102,1,1000,500,9,"
                    "'2026-10-12 09:00:00','2026-10-12 10:00:00',2,0)"))
    db.commit()
    return bom_client, identity, db


# ---------------- F10.1 Analytics dashboard ----------------

def test_analytics_known_values_and_wire_capture(ana):
    client, _, _ = ana
    response = client.get("/analytics")
    assert response.status_code == 200, response.text
    body = response.json()
    s = body["summary"]
    # Hero values: revenue = sum(price*qty), cost = sum(cost*qty) over completed tenant jobs.
    assert s["total_revenue"] == 31 and s["total_cost"] == 11
    assert s["total_margin"] == 20 and s["margin_percent"] == 64.5
    assert s["completed_jobs"] == 3 and s["pending_jobs"] == 1 and s["total_jobs"] == 6
    assert s["projected_revenue"] == 7 and s["projected_cost"] == 3
    assert s["total_print_hours"] == 6.0
    # Model rankings by completed job count, foreign model excluded.
    assert body["top_by_jobs"] == [{"id": 101, "name": "Own model", "job_count": 2},
                                   {"id": 103, "name": "Shared model", "job_count": 1}]
    # Fleet overview / per-printer.
    p = {row["id"]: row for row in body["printer_stats"]}
    assert p[9]["completed_jobs"] == 2 and p[9]["total_hours"] == 4.0
    assert p[10]["completed_jobs"] == 1 and p[10]["failed_jobs"] == 1 and p[10]["success_rate"] == 50.0
    assert body["jobs_by_date"]["2026-10-11"] == {"created": 1, "completed": 1}
    assert body["jobs_by_date"]["2026-10-12"] == {"created": 4, "completed": 2}
    _wire("analytics", body)


@pytest.mark.xfail(strict=True, reason="DEFECT: backend/modules/reporting/routes/analytics.py:340-349 jobs_by_date "
                   "carries only created/completed counts, so the Cost/revenue time-series chart has no daily "
                   "revenue/cost to plot (UI always shows 'Daily revenue and cost are unavailable.')")
def test_daily_series_carries_revenue_and_cost(ana):
    client, _, _ = ana
    day = client.get("/analytics").json()["jobs_by_date"]["2026-10-11"]
    assert day["revenue"] == 20 and day["cost"] == 8


@pytest.mark.xfail(strict=True, reason="DEFECT: backend/modules/reporting/routes/analytics.py:179 /analytics takes no days "
                   "window (and frontend/src/api/reporting.ts:72 sends none), so hero totals are lifetime values whatever "
                   "range the Analytics page selector shows")
def test_analytics_hero_totals_honor_selected_window(ana):
    client, _, _ = ana
    # 1-day window from 2026-10-12 12:00Z keeps only 7003 (price 5, cost 1).
    s = client.get("/analytics", params={"days": 1}).json()["summary"]
    assert s["total_revenue"] == 5 and s["total_cost"] == 1 and s["completed_jobs"] == 1


# ---------------- F10.2 Cost calculator ----------------

def test_pricing_config_margin_persists_and_is_read_back(ana):
    client, identity, db = ana
    identity.update(role="admin")
    saved = client.put("/pricing-config", json={"default_margin": 25, "failure_rate": 10, "spool_cost": 200,
                                                 "spool_weight": 1000, "hourly_rate": 12, "post_processing_min": 10,
                                                 "packing_min": 10, "support_min": 10, "electricity_rate": 1,
                                                 "printer_wattage": 500, "printer_cost": 1000, "printer_lifespan": 1000,
                                                 "packaging_cost": 3, "monthly_rent": 500, "parts_per_month": 100,
                                                 "other_costs": 4})
    assert saved.status_code == 200, saved.text
    with Session(db.get_bind()) as fresh:
        stored = fresh.execute(text("SELECT value FROM system_config WHERE key='pricing_config'")).scalar_one()
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored["default_margin"] == 25
    identity.update(role="viewer")
    got = client.get("/pricing-config").json()
    assert got["default_margin"] == 25
    # Margin is admin-configurable only.
    assert client.put("/pricing-config", json={"default_margin": 99}).status_code == 403
    assert client.put("/pricing-config", json={"default_margin": -1}).status_code in (403, 422)
    identity.update(role="admin")
    assert client.put("/pricing-config", json={"default_margin": -1}).status_code == 422
    _wire("pricing-config", got)


def test_models_list_wire_for_calculator(ana):
    client, _, db = ana
    db.execute(text("UPDATE models SET color_requirements=:c, build_time_hours=2, units_per_bed=2 WHERE id=101"), {"c": GRAMS_100})
    db.commit()
    response = client.get("/models")
    assert response.status_code == 200, response.text
    row = next(m for m in response.json() if m["id"] == 101)
    # Producer semantics: grams/hours are per bed; per-item values divide by units_per_bed.
    assert row["total_filament_grams"] == 100 and row["build_time_hours"] == 2 and row["units_per_bed"] == 2
    assert row["filament_per_item"] == 50 and row["time_per_item"] == 1
    for volatile in ("created_at", "updated_at"):
        row.pop(volatile)
    _wire("models", [row])


def test_backend_model_cost_breakdown_components(ana):
    """GET /models/{id}/cost exposes the same seven components with known values."""
    client, identity, db = ana
    identity.update(role="admin")
    client.put("/pricing-config", json={"default_margin": 50, "failure_rate": 10, "spool_cost": 200, "spool_weight": 1000,
                                        "hourly_rate": 12, "post_processing_min": 10, "packing_min": 10, "support_min": 10,
                                        "electricity_rate": 1, "printer_wattage": 500, "printer_cost": 1000,
                                        "printer_lifespan": 1000, "packaging_cost": 3, "monthly_rent": 500,
                                        "parts_per_month": 100, "other_costs": 4})
    db.execute(text("UPDATE models SET color_requirements=:c, build_time_hours=2, markup_percent=NULL WHERE id=101"), {"c": GRAMS_100})
    db.commit()
    body = client.get("/models/101/cost").json()
    assert body["costs"] == {"material": 20.0, "labor": 6.0, "electricity": 1.0, "depreciation": 2.0,
                             "packaging": 3.0, "failure": 3.6, "overhead": 5.0, "other": 4.0}
    assert body["subtotal"] == 44.6 and body["margin_percent"] == 50 and body["suggested_price"] == 66.9


# ---------------- F10.3 Utilization ----------------

def test_stats_per_printer_counts_follow_window_and_wire_capture(ana):
    client, _, db = ana
    db.execute(text("UPDATE printers SET total_print_hours=6 WHERE id=9"))
    db.execute(text("UPDATE printers SET total_print_hours=3 WHERE id=10"))
    db.commit()
    body = client.get("/stats", params={"days": 7})
    assert body.status_code == 200, body.text
    stats = {p["id"]: p for p in body.json()["printer_stats"]}
    assert stats[9]["completed_jobs"] == 2 and stats[9]["failed_jobs"] == 0 and stats[9]["total_hours"] == 6
    assert stats[10]["completed_jobs"] == 1 and stats[10]["failed_jobs"] == 1 and stats[10]["success_rate"] == 50.0
    assert stats[9]["utilization_pct"] == round(6 / 720 * 100, 1)
    # 1-day window (from 2026-10-12 12:00Z): only 7003 (ended 13:00) and 7005 (failed 14:00) remain.
    one = {p["id"]: p for p in client.get("/stats", params={"days": 1}).json()["printer_stats"]}
    assert one[9]["completed_jobs"] == 0 and one[10]["completed_jobs"] == 1 and one[10]["failed_jobs"] == 1
    assert client.get("/stats", params={"days": 0}).status_code == 422
    _wire("stats-7", body.json())


@pytest.mark.xfail(strict=True, reason="DEFECT: backend/modules/reporting/routes/analytics.py:140-146 /stats per-printer "
                   "total_hours is the lifetime printers.total_print_hours counter and utilization_pct is that lifetime "
                   "value / fixed 720h, ignoring the selected days window (a printer with 1440 lifetime hours shows 100% "
                   "for a 7-day window in which it printed 2h; UI labels it 'utilization in the selected window')")
def test_utilization_and_hours_reflect_selected_window(ana):
    client, _, db = ana
    db.execute(text("UPDATE printers SET total_print_hours=1440 WHERE id=10"))
    db.execute(text("UPDATE jobs SET duration_hours=2 WHERE id=7003"))
    db.commit()
    p10 = next(p for p in client.get("/stats", params={"days": 7}).json()["printer_stats"] if p["id"] == 10)
    assert p10["total_hours"] == 2
    assert p10["utilization_pct"] == round(2 / (7 * 24) * 100, 1)


# ---------------- F10.4 Energy ----------------

def test_print_jobs_energy_wire_capture(ana, monkeypatch):
    client, identity, db = ana
    from modules.printers import smart_plug
    monkeypatch.setattr(smart_plug, "engine", db.get_bind())
    monkeypatch.setattr(smart_plug, "_energy_starts", {})
    monkeypatch.setattr(smart_plug, "_energy_ended", {})
    reading = {"total": 12.0}
    # Outermost boundary: the plug's HTTP answer (Tasmota Status 8 payload).
    monkeypatch.setattr(smart_plug, "_http_request", lambda url: (200, '{"StatusSNS":{"ENERGY":{"Total":%s}}}' % reading["total"]))
    monkeypatch.setattr(smart_plug, "get_energy", lambda printer: smart_plug.tasmota_energy("owned.invalid"))
    db.execute(text("UPDATE printers SET plug_energy_kwh=0 WHERE id=9"))
    db.execute(text("INSERT OR REPLACE INTO system_config(key,value) VALUES('energy_cost_per_kwh','0.2')"))
    for pid, start, end in ((8801, "2026-10-11 10:00:00", "2026-10-11 12:00:00"), (8802, "2026-10-12 10:00:00", "2026-10-12 11:00:00")):
        db.execute(text("INSERT INTO print_jobs(id,printer_id,job_id,filename,status,started_at,ended_at) "
                        "VALUES(:id,9,:j,'cert.3mf','completed',:s,:e)"), {"id": pid, "j": f"cert-{pid}", "s": start, "e": end})
        db.execute(text("INSERT INTO print_archives(print_job_id,printer_id,print_name,status) VALUES(:id,9,'Cert energy','completed')"), {"id": pid})
    db.commit()
    for pid, delta in ((8801, 0.5), (8802, 0.25)):
        smart_plug.record_energy_start(9, pid)
        reading["total"] += delta
        assert smart_plug.record_energy_for_print(9, pid) == delta
    rows = [r for r in client.get("/print-jobs", params={"limit": 200}).json() if r["id"] in (8801, 8802)]
    by_id = {r["id"]: r for r in rows}
    assert by_id[8801]["energy_kwh"] == 0.5 and by_id[8801]["energy_cost"] == 0.1
    assert by_id[8802]["energy_kwh"] == 0.25 and by_id[8802]["energy_cost"] == 0.05
    rate = client.get("/settings/energy-rate").json()
    assert rate == {"energy_cost_per_kwh": 0.2}
    rows = sorted(rows, key=lambda r: r["id"])
    for r in rows:
        r.pop("created_at", None)  # DB default wall-clock; not consumed by the widget
    _wire("print-jobs-energy", {"rows": rows, "rate": rate})


# ---------------- F10.5 Failure analytics ----------------

def test_failure_logging_feeds_failure_analytics_and_wire_capture(ana):
    client, _, db = ana
    db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,model_id,quantity,created_at,actual_end,charged_to_org_id,hold) "
                    "VALUES(7006,'Cert job 7006','failed',10,103,1,'2026-10-12 15:00:00','2026-10-12 16:00:00',1,0)"))
    db.execute(text("INSERT INTO hms_error_history(printer_id,code,message,severity,source,occurred_at) "
                    "VALUES(10,'0300-0100','Synthetic nozzle clog','error','bambu_hms','2026-10-12T15:30:00+00:00')"))
    db.commit()
    # Failure logging through the real operator endpoint.
    logged = client.patch("/jobs/7006/failure", json={"fail_reason": "adhesion", "fail_notes": "corner lift"})
    assert logged.status_code == 200, logged.text
    assert client.patch("/jobs/7001/failure", json={"fail_reason": "x"}).status_code == 400  # completed job refused
    body = client.get("/analytics/failures", params={"days": 30}).json()
    assert body["total_failed"] == 2 and body["total_completed"] == 3
    printers = {p["name"]: p for p in body["by_printer"]}
    assert printers["P1S-10"]["failed"] == 2 and printers["P1S-10"]["completed"] == 1
    assert printers["P1S-10"]["mtbf_hours"] == 2.0
    assert printers["P1S-09"]["failed"] == 0
    models = {m["name"]: m for m in body["by_model"]}
    assert models["Shared model"] == {"name": "Shared model", "completed": 1, "failed": 2, "success_rate": 33.3}
    assert {r["reason"]: r["count"] for r in body["top_failure_reasons"]} == {"spaghetti": 1, "adhesion": 1}
    assert body["hms_errors"] == [{"code": "0300-0100", "message": "Synthetic nozzle clog", "count": 1}]
    _wire("failures", body)


# ---------------- F10.6 CSV exports (byte-level content of the downloads) ----------------

def test_csv_exports_download_parseable_known_rows(ana):
    client, _, _ = ana
    resp = client.get("/export/jobs", params={"status": "completed"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == "attachment; filename=jobs_export.csv"
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert [r["ID"] for r in rows] == ["7003", "7002", "7001"]  # newest first, foreign 7099 excluded
    assert rows[2] == {"ID": "7001", "Item Name": "Cert job 7001", "Model ID": "101", "Quantity": "2", "Status": "completed",
                       "Priority": rows[2]["Priority"], "Printer ID": "9", "Duration (hrs)": "3.0", "Estimated Cost": "4.0",
                       "Suggested Price": "10.0", "Scheduled Start": "", "Actual Start": "",
                       "Actual End": "2026-10-11T12:00:00", "Created At": "2026-10-11T09:00:00"}
    _wire("export-jobs", {"content_type": resp.headers["content-type"], "body": resp.text})
