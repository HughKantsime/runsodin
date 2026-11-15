#!/usr/bin/env python3.11
"""Seed a NEW disposable database: --out EMPTY_DIRECTORY --printer-port PORT.

Writes fixtures.json and token (0600). Never prints credentials. Existing files
are refused, even if the existing database is empty. No raw fixture SQL.
"""

import argparse
import json
import os
from pathlib import Path
import secrets

from runtime import configure, enable_test_license, create_test_app


def seed(out, printer_port):
    if not 1 <= printer_port <= 65535:
        raise ValueError("The simulator port must be between 1 and 65535.")
    out = out.resolve()
    if not out.is_dir() or any(out.iterdir()):
        raise ValueError("The fixture output directory must exist and be empty.")
    os.chmod(out, 0o700)
    configure(out)
    # Reserve the file exclusively before the application's engine opens it.
    with (out / "odin.db").open("xb"):
        pass
    from core.db import engine, SessionLocal
    from core.schema.bootstrap import bootstrap_database
    bootstrap_database(engine)
    enable_test_license(out)

    from core.auth import hash_password
    from core.models import SystemConfig
    from core.base import JobStatus, OrderStatus, AlertType, AlertSeverity, SpoolStatus
    from modules.organizations.services import create_user_record
    from modules.models_library.models import Model
    from modules.printers.models import Printer, FilamentSlot
    from modules.jobs.models import Job
    from modules.inventory.models import FilamentLibrary, Spool
    from modules.notifications.models import Alert
    from modules.orders.models import Order
    from modules.system.models import MaintenanceTask

    password = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        db.merge(SystemConfig(key="require_job_approval", value=True))
        user_id = create_user_record(db, username="contract-admin", email="contract@example.invalid",
                                     password_hash=hash_password(password), role="admin")
        model = Model(name="Contract test model")
        printer = Printer(name="Contract simulator", model="Moonraker simulator", api_type="moonraker",
                          api_host=f"127.0.0.1:{printer_port}", gcode_state="RUNNING", slot_count=1,
                          camera_enabled=False, plug_auto_on=False, plug_auto_off=False,
                          total_print_hours=20, hours_since_maintenance=20)
        filament = FilamentLibrary(brand="Test", name="PLA", material="PLA", is_custom=True)
        task = MaintenanceTask(name="Contract test inspection", interval_print_hours=10, is_active=True)
        order = Order(order_number="CONTRACT-1", customer_name="Test customer", status=OrderStatus.PENDING)
        db.add_all([model, printer, filament, task, order])
        db.flush()
        jobs = [Job(model_id=model.id, item_name=f"Contract {name}", status=state)
                for name, state in (("failed", JobStatus.FAILED), ("cancel", JobStatus.PENDING),
                                    ("approve", JobStatus.SUBMITTED), ("reject", JobStatus.SUBMITTED))]
        spool = Spool(filament_id=filament.id, status=SpoolStatus.ACTIVE,
                      initial_weight_g=1000, remaining_weight_g=500)
        alert = Alert(user_id=user_id, alert_type=AlertType.PRINT_FAILED, severity=AlertSeverity.WARNING,
                      title="Contract test alert", is_read=False, is_dismissed=False)
        slot = FilamentSlot(printer_id=printer.id, slot_number=1)
        db.add_all([*jobs, spool, alert, slot])
        db.commit()
        fixtures = dict(model_id=model.id, printer_id=printer.id, job_id=jobs[0].id,
                        cancel_job_id=jobs[1].id, approve_job_id=jobs[2].id, reject_job_id=jobs[3].id,
                        alert_id=alert.id, spool_id=spool.id, filament_id=filament.id,
                        task_id=task.id, order_id=order.id, ams_slot=slot.slot_number)

    # Use the real interactive login and mint routes, including their gates.
    from fastapi.testclient import TestClient
    app = create_test_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/auth/login", data={"username": "contract-admin", "password": password})
        if response.status_code != 200:
            raise RuntimeError(f"Fixture login failed (HTTP {response.status_code}).")
        # The version endpoint's global scope gate requires read, even though
        # agent:write is sufficient on the advertised tool routes themselves.
        response = client.post("/api/v1/tokens", json={"name": "Live contracts", "scopes": ["agent:write", "read"], "expires_days": 1})
        if response.status_code != 200:
            raise RuntimeError(f"Fixture token mint failed (HTTP {response.status_code}).")
        token = response.json()["token"]
    for name, content in (("fixtures.json", json.dumps(fixtures, sort_keys=True) + "\n"), ("token", token)):
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    return fixtures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--printer-port", required=True, type=int)
    args = parser.parse_args()
    try:
        seed(args.out, args.printer_port)
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
    except Exception as error:
        # Database exceptions can contain bound credentials. Do not dump them.
        parser.exit(2, f"Fixture setup failed ({type(error).__name__}).\n")


if __name__ == "__main__":
    main()
