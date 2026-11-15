"""Run with --confcutdir=tests/mcp_contract: no live-server conftest or .env reads."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "ops/mcp_contract"


def probe(out):
    """Fresh interpreter: backend settings, engine, and audit fence are process local."""
    sys.path.insert(0, str(HARNESS))
    from seed_fixture import seed
    out.mkdir()
    ids = seed(out, 7125)
    from core.db import SessionLocal, engine
    from core.models import SystemConfig
    from modules.models_library.models import Model
    from modules.printers.models import Printer
    from modules.jobs.models import Job
    from modules.inventory.models import FilamentLibrary, Spool
    from modules.notifications.models import Alert
    from modules.orders.models import Order
    from modules.system.maintenance_status import compute_status
    from runtime import create_test_app, allow_simulator
    from core import webhook_utils
    import license_manager
    from fastapi.testclient import TestClient

    report = {"ids": ids, "modes": {name: (out / name).stat().st_mode & 0o777
                                     for name in ("token", "fixtures.json")}}
    assert webhook_utils.is_blocked_device_address("127.0.0.1")
    allow_simulator(7125)
    report["simulator_addresses"] = webhook_utils.device_destination_ips("127.0.0.1:7125")
    report["other_devices_refused"] = []
    for host in ("127.0.0.1:8000", "192.0.2.1:7125", "localhost:7125"):
        try:
            webhook_utils.device_destination_ips(host)
        except webhook_utils.DeviceDestinationBlocked:
            report["other_devices_refused"].append(host)
    with SessionLocal() as db:
        report["approval_enabled"] = db.get(SystemConfig, "require_job_approval").value
        printer = db.get(Printer, ids["printer_id"])
        report["model"] = db.get(Model, ids["model_id"]).name
        report["printer"] = dict(host=printer.api_host, type=printer.api_type, state=printer.gcode_state,
                                 hms=printer.hms_errors, error=printer.last_error_code,
                                 slots=[s.slot_number for s in printer.filament_slots])
        report["jobs"] = {key: db.get(Job, ids[key]).status.value for key in
                          ("job_id", "cancel_job_id", "approve_job_id", "reject_job_id")}
        report["job_count"] = db.query(Job).count()
        spool = db.get(Spool, ids["spool_id"])
        report["spool"] = dict(status=spool.status.value, weight=spool.remaining_weight_g,
                               printer=spool.location_printer_id, slot=spool.location_slot,
                               filament=spool.filament_id,
                               material=db.get(FilamentLibrary, ids["filament_id"]).material)
        alert = db.get(Alert, ids["alert_id"])
        report["alert"] = dict(user=alert.user_id, read=alert.is_read, dismissed=alert.is_dismissed)
        report["order"] = db.get(Order, ids["order_id"]).status.value
        report["maintenance"] = compute_status(db)
    info = license_manager.get_license()
    report["features"] = {name: info.has_feature(name) for name in ("job_approval", "maintenance")}
    token = (out / "token").read_text()
    # Real middleware and routes in process; no transport substitutes or bypassed auth.
    with TestClient(create_test_app(), base_url="http://127.0.0.1") as client:
        client.headers["X-API-Key"] = token
        me = client.get("/api/v1/auth/me")
        report["user"] = me.json()
        report["user_status"] = me.status_code
        paths = ["/printers", f"/printers/{ids['printer_id']}", "/jobs?status=failed",
                 f"/jobs/{ids['job_id']}", "/jobs?status=pending", "/alerts?is_read=false",
                 "/spools", "/filament-library", "/maintenance/tasks", "/maintenance/status",
                 "/orders?status_filter=pending"]
        report["reads"] = {path: client.get("/api/v1" + path).status_code for path in paths}
        response = client.get("/api/v1/version")
        report["version_response"] = dict(status=response.status_code, body=response.json())
        preview = client.post("/api/v1/jobs", json={"model_id": ids["model_id"], "item_name": "Contract preview"},
                              headers={"X-Dry-Run": "true"})
        report["queue_preview"] = dict(status=preview.status_code, body=preview.json())
        client.headers["X-API-Key"] = "odin_invalid_contract_credential"
        report["refusal"] = client.get(f"/api/v1/printers/{ids['printer_id']}").status_code
    try:
        seed(out, 7125)
    except ValueError:
        report["reseed_refused"] = True
    engine.dispose()
    print("FIXTURE_REPORT=" + json.dumps(report))


@pytest.fixture(scope="module")
def fixture_report(tmp_path_factory):
    out = tmp_path_factory.mktemp("fixture") / "records"
    env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(out)],
                            cwd=out.parent, env=env, capture_output=True, text=True, timeout=120)
    # The probe sanitizes exceptions; never include an unfiltered backend traceback.
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("FIXTURE_REPORT="))
    return json.loads(line.split("=", 1)[1])


def test_fixture_ids_and_private_files(fixture_report):
    r = fixture_report
    assert set(r["ids"]) == {"model_id", "printer_id", "job_id", "cancel_job_id", "approve_job_id",
                             "reject_job_id", "alert_id", "spool_id", "filament_id", "task_id", "order_id", "ams_slot"}
    assert all(type(value) is int and value > 0 for value in r["ids"].values())
    assert r["modes"] == {"token": 0o600, "fixtures.json": 0o600}
    assert r["reseed_refused"]


def test_model_and_simulated_printer(fixture_report):
    assert fixture_report["model"] == "Contract test model"
    assert fixture_report["printer"] == dict(host="127.0.0.1:7125", type="moonraker", state="RUNNING",
                                             hms=None, error=None, slots=[1])


def test_device_policy_allows_only_owned_simulator(fixture_report):
    assert fixture_report["simulator_addresses"] == ["127.0.0.1"]
    assert fixture_report["other_devices_refused"] == ["127.0.0.1:8000", "192.0.2.1:7125", "localhost:7125"]


def test_four_separate_jobs(fixture_report):
    assert fixture_report["jobs"] == dict(job_id="failed", cancel_job_id="pending",
                                          approve_job_id="submitted", reject_job_id="submitted")
    assert fixture_report["job_count"] == 4
    assert len({fixture_report["ids"][key] for key in fixture_report["jobs"]}) == 4


def test_alert_owner_and_authentication(fixture_report):
    r = fixture_report
    assert r["user_status"] == 200
    assert r["user"]["role"] == "admin"
    assert r["alert"] == dict(user=r["user"]["id"], read=False, dismissed=False)
    assert r["refusal"] in (401, 403)


def test_active_unassigned_pla_spool(fixture_report):
    assert fixture_report["spool"] == dict(status="active", weight=500, printer=None, slot=None,
                                          filament=fixture_report["ids"]["filament_id"], material="PLA")


def test_overdue_maintenance_and_pending_order(fixture_report):
    r = fixture_report
    row = next(row for row in r["maintenance"] if row["printer_id"] == r["ids"]["printer_id"])
    assert any(task["task_id"] == r["ids"]["task_id"] and task["status"] == "overdue" for task in row["tasks"])
    assert r["order"] == "pending"
    assert r["features"] == {"maintenance": True, "job_approval": True}
    assert r["approval_enabled"] is True


def test_read_routes_and_version(fixture_report):
    assert set(fixture_report["reads"].values()) == {200}, fixture_report["reads"]
    assert fixture_report["version_response"] == dict(status=200, body={"version": "2.2.0"})


def test_administrator_queues_pending_jobs(fixture_report):
    preview = fixture_report["queue_preview"]
    assert preview["status"] == 201
    assert preview["body"]["dry_run"] is True
    assert preview["body"]["would_execute"]["requires_approval"] is False
    # The released route serializes this enum with str(), as JobStatus.PENDING.
    assert preview["body"]["would_execute"]["initial_status"] == "JobStatus.PENDING"


@pytest.mark.parametrize("key,url", [
    ("DATABASE_URL", "sqlite:////tmp/existing-farm.db"),
    ("DATABASE_URL", "postgresql://127.0.0.1/odin"),
    ("DATABASE_URL", "sqlite:///:memory:"),
    ("database_url", "sqlite:////tmp/existing-farm.db"),
    ("ODIN_DATABASE_URL", "sqlite:////tmp/existing-farm.db"),
])
def test_runner_refuses_inherited_database_and_cleans_up(tmp_path, key, url):
    env = {"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path), key: url}
    result = subprocess.run([str(HARNESS / "run_live.sh"), str(ROOT)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "Refusing a database URL" in result.stderr
    assert url not in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("exit_code", [0, 7])
def test_shell_preserves_runner_exit_and_removes_directory(tmp_path, exit_code):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "python3.11"
    executable.write_text(f'#!/bin/bash\ntouch "$3/owned-file"\nexit {exit_code}\n')
    executable.chmod(0o700)
    runs = tmp_path / "runs"
    runs.mkdir()
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(runs)}
    result = subprocess.run([str(HARNESS / "run_live.sh"), str(ROOT)], env=env, timeout=15)
    assert result.returncode == exit_code
    assert list(runs.iterdir()) == []


def test_shell_signal_stops_owned_runner_and_cleans_up(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "python3.11"
    executable.write_text(f'#!{sys.executable}\nimport pathlib, sys, time\n'
                          'pathlib.Path(sys.argv[3], "ready").touch()\ntime.sleep(60)\n')
    executable.chmod(0o700)
    runs = tmp_path / "runs"
    runs.mkdir()
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(runs)}
    process = subprocess.Popen([str(HARNESS / "run_live.sh"), str(ROOT)], env=env)
    try:
        deadline = time.monotonic() + 5
        while not list(runs.glob("*/ready")):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 143
        assert list(runs.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("event,args", [
    ("socket.connect", (None, ("192.0.2.1", 80))),
    ("socket.getaddrinfo", ("example.invalid", 80)),
    ("socket.bind", (None, ("0.0.0.0", 80))),
    ("socket.sendto", (None, b"data", ("192.0.2.1", 80))),
])
def test_network_fence_refuses_non_loopback(event, args):
    sys.path.insert(0, str(HARNESS))
    from runtime import loopback_only
    with pytest.raises(PermissionError, match="only 127.0.0.1"):
        loopback_only(event, args)
    loopback_only("socket.connect", (None, ("127.0.0.1", 80)))


if __name__ == "__main__":
    try:
        probe(Path(sys.argv[2]))
    except Exception as error:
        print(f"Fixture probe failed ({type(error).__name__}).", file=sys.stderr)
        raise SystemExit(1)
