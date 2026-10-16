"""Cert F / cluster D — F12-12.5-01 configurable TTL per data type, F12-12.5-02 manual cleanup trigger.

Real server (LiveOdin), real retention routes, real bootstrapped schema. Aged rows are seeded with SQL
(timestamps cannot be back-dated through the API); configuration and cleanup go over HTTP.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_F_00_live import LiveOdin

FIXTURE_OUT = Path(__file__).resolve().parents[2] / "artifacts/application-certification/cert-F/D-retention-wire.json"


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odinD_ret")).start()
    o.setup_admin()
    o.H = o.admin_headers()
    o.wire = {}
    yield o
    FIXTURE_OUT.write_text(json.dumps(o.wire, indent=1, sort_keys=True))
    o.stop()


def _exec(o, statement, **p):
    with o.engine.begin() as c:
        c.execute(text(statement), p)


def _age(days):
    return f"datetime('now', '-{days} days')"


def test_defaults_and_ttl_configurable_for_each_of_four_data_types(odin):
    o = odin
    r = o.get("/api/config/retention", headers=o.H)
    assert r.status_code == 200
    o.wire["get_defaults"] = r.json()
    assert r.json() == {"completed_jobs_days": 0, "audit_logs_days": 365, "timelapses_days": 30, "alert_history_days": 90}
    body = {"completed_jobs_days": 30, "audit_logs_days": 60, "timelapses_days": 7, "alert_history_days": 10}
    r = o.put("/api/config/retention", headers=o.H, json=body)
    assert r.status_code == 200 and r.json() == body
    o.wire["put_result"] = r.json()
    assert o.get("/api/config/retention", headers=o.H).json() == body
    stored = json.loads(o.sql("SELECT value FROM system_config WHERE key='data_retention'")[0][0])
    assert stored == body


@pytest.mark.parametrize("bad", [{"audit_logs_days": -1}, {"alert_history_days": None}, {"completed_jobs_days": "x"}])
def test_invalid_ttl_rejected_and_policy_unchanged(odin, bad):
    o = odin
    before = o.get("/api/config/retention", headers=o.H).json()
    r = o.put("/api/config/retention", headers=o.H, json=bad)
    assert r.status_code in (400, 422)
    assert o.get("/api/config/retention", headers=o.H).json() == before


def test_retention_config_and_cleanup_superadmin_only(odin):
    o = odin
    from tests.test_contracts.test_cert_F_00_live import strong_password
    pw = strong_password()
    assert o.post("/api/users", headers=o.H, json={"username": "certd_retop", "email": "certd_retop@example.test",
                  "password": pw, "role": "operator"}).status_code in (200, 201)
    h = o.bearer("certd_retop", pw)
    assert o.get("/api/config/retention", headers=h).status_code == 403
    assert o.put("/api/config/retention", headers=h, json={"audit_logs_days": 1}).status_code == 403
    assert o.post("/api/admin/retention/cleanup", headers=h).status_code == 403


def _seed(o):
    for name, status, age in (("old-done", "completed", 45), ("old-failed", "failed", 45), ("old-cancel", "cancelled", 45),
                              ("new-done", "completed", 5), ("old-queued", "pending", 45), ("old-printing", "printing", 45)):
        _exec(o, f"INSERT INTO jobs (item_name, status, quantity, created_at, updated_at) VALUES (:n, :s, 1, {_age(age)}, {_age(age)})",
              n="ret-" + name, s=status)
    for name, age in (("old", 70), ("new", 3)):
        _exec(o, f"INSERT INTO audit_logs (timestamp, action, entity_type, details) VALUES ({_age(age)}, 'cert.ret', 'system', :d)",
              d="ret-audit-" + name)
        _exec(o, f"INSERT INTO alerts (user_id, alert_type, severity, title, is_read, is_dismissed, created_at) "
                 f"VALUES (1, 'print_complete', 'info', :t, 0, 0, {_age(age // 5 if name == 'new' else 20)})", t="ret-alert-" + name)
    _exec(o, "INSERT INTO printers (name, slot_count, is_active, camera_enabled, created_at, updated_at) "
             "VALUES ('ret-printer', 1, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    pid = o.sql("SELECT id FROM printers WHERE name='ret-printer'")[0][0]
    _exec(o, f"INSERT INTO timelapses (printer_id, filename, status, created_at) VALUES (:p, 'ret-old.mp4', 'completed', {_age(40)})", p=pid)


@pytest.fixture(scope="module")
def cleaned(odin):
    o = odin
    body = {"completed_jobs_days": 30, "audit_logs_days": 60, "timelapses_days": 7, "alert_history_days": 10}
    assert o.put("/api/config/retention", headers=o.H, json=body).status_code == 200
    _seed(o)
    r = o.post("/api/admin/retention/cleanup", headers=o.H)
    assert r.status_code == 200, r.text
    o.wire["cleanup_result"] = r.json()
    return r.json()


def test_manual_cleanup_deletes_only_rows_older_than_each_ttl(odin, cleaned):
    o = odin
    jobs = {r[0] for r in o.sql("SELECT item_name FROM jobs WHERE item_name LIKE 'ret-%'")}
    assert jobs == {"ret-new-done", "ret-old-queued", "ret-old-printing"}       # terminal+expired removed only
    assert cleaned["deleted"]["completed_jobs"] == 3
    audits = {r[0] for r in o.sql("SELECT details FROM audit_logs WHERE action='cert.ret'")}
    assert audits == {"ret-audit-new"}
    alerts = {r[0] for r in o.sql("SELECT title FROM alerts WHERE title LIKE 'ret-alert-%'")}
    assert alerts == {"ret-alert-new"}
    assert cleaned["deleted"]["audit_logs"] >= 1 and cleaned["deleted"]["alerts"] == 1


def test_zero_ttl_keeps_forever(odin):
    o = odin
    _exec(o, f"INSERT INTO jobs (item_name, status, quantity, created_at, updated_at) VALUES ('ret-zero', 'completed', 1, {_age(3000)}, {_age(3000)})")
    assert o.put("/api/config/retention", headers=o.H, json={"completed_jobs_days": 0, "audit_logs_days": 0,
                 "timelapses_days": 0, "alert_history_days": 0}).status_code == 200
    r = o.post("/api/admin/retention/cleanup", headers=o.H)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert "completed_jobs" not in r.json()["deleted"]
    assert o.sql("SELECT COUNT(*) FROM jobs WHERE item_name='ret-zero'")[0][0] == 1


def test_timelapse_ttl_is_stored_but_cleanup_refuses_to_apply_it(odin, cleaned):
    """Behavior that makes the claim inexact: timelapses_days is configurable, but cleanup never
    applies it (explicit 'refused: lifetime_retirement_pending'); the 40-day-old timelapse survives a 7-day TTL."""
    o = odin
    assert cleaned["status"] == "partial"
    assert cleaned["refused"] == {"timelapses": {"status": "refused", "reason": "lifetime_retirement_pending"}}
    assert "timelapses" not in cleaned["deleted"]
    assert o.sql("SELECT COUNT(*) FROM timelapses WHERE filename='ret-old.mp4'")[0][0] == 1


def test_no_automatic_cleanup_runs_without_manual_trigger(tmp_path):
    """Expired rows persist across a server lifetime (startup + idle) until the manual trigger is called."""
    o = LiveOdin(tmp_path).start()
    try:
        o.setup_admin()
        h = o.admin_headers()
        assert o.put("/api/config/retention", headers=h, json={"audit_logs_days": 1}).status_code == 200
        _exec(o, f"INSERT INTO audit_logs (timestamp, action, entity_type, details) VALUES ({_age(30)}, 'cert.auto', 'system', 'x')")
        o.restart()
        import time
        time.sleep(3)
        assert o.sql("SELECT COUNT(*) FROM audit_logs WHERE action='cert.auto'")[0][0] == 1
        r = o.post("/api/admin/retention/cleanup", headers=o.bearer(*o.admin))
        assert r.status_code == 200
        assert o.sql("SELECT COUNT(*) FROM audit_logs WHERE action='cert.auto'")[0][0] == 0
    finally:
        o.stop()
